import copy
import json
from datetime import timedelta
from unittest.mock import patch

import requests

from odoo import fields

BROKER = "odoo.addons.twp_google_contacts.models.broker.requests.post"
GOOGLE = "odoo.addons.twp_google_contacts.models.twp_google_contacts_account.requests.request"
SLEEP = "odoo.addons.twp_google_contacts.models.twp_google_contacts_account.time.sleep"


def response(status, body=None, headers=None):
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(body).encode() if body is not None else b""
    r.headers.update(headers or {})
    return r


class FakeBroker:
    """Stands in for ThriveWell Connect. `answers` maps a path to (status, body)."""

    def __init__(self):
        self.calls = []
        self.tokens = 0
        self.answers = {
            "/v1/connect/start": (200, {"authorize_url": "https://accounts.google.com/o/oauth2/v2/auth?x=1"}),
            "/v1/connect/finish": (200, {"connection_id": "conn_1", "connector": "google-contacts",
                                         "account_email": "test@example.com"}),
            "/v1/disconnect": (200, {"disconnected": True, "revoked_at_google": True}),
        }

    def __call__(self, url, json=None, timeout=None, headers=None):
        path = url.split("connect.test", 1)[1]
        self.calls.append((path, json, headers))
        if path == "/v1/token" and path not in self.answers:
            self.tokens += 1
            return response(200, {"access_token": "at-%s" % self.tokens, "expires_in": 3599})
        status, body = self.answers[path]
        return response(status, body)

    def paths(self):
        return [path for path, _, _ in self.calls]


class FakeGoogle:
    """Stands in for people.googleapis.com. Answers are taken in order; the last one repeats."""

    def __init__(self, *answers):
        self.answers = list(answers) or [response(200, {"totalItems": 3})]
        self.calls = []

    def __call__(self, method, url, params=None, json=None, timeout=None, headers=None):
        self.calls.append((method, url, headers["Authorization"]))
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


class GoogleContactsCase:
    """Mixin: a configured database, a fake ThriveWell Connect, and helpers."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        params = cls.env["ir.config_parameter"].sudo()
        params.set_param("twp_google_contacts.broker_url", "http://connect.test")
        params.set_param("twp_google_contacts.broker_key", "twc_test")
        cls.Account = cls.env["twp.google.contacts.account"]

    def setUp(self):
        super().setUp()
        self.broker = FakeBroker()
        patcher = patch(BROKER, self.broker)
        patcher.start()
        self.addCleanup(patcher.stop)

    def connect(self, user=None):
        Account = self.Account.with_user(user) if user else self.Account
        return Account._connect_finish("handoff-code")

    def expire_token(self, account):
        account.sudo().access_token_expiry = fields.Datetime.now() - timedelta(minutes=1)


class FakePeople:
    """An in-memory Google Contacts for one account, strict about etags as Google is, with sync
    tokens: every change takes the next serial, and a token is the serial it was given at."""

    def __init__(self):
        self.people = {}
        self.groups = {"contactGroups/myContacts": {"name": "myContacts", "groupType": "SYSTEM_CONTACT_GROUP"},
                       "contactGroups/family": {"name": "Family", "groupType": "USER_CONTACT_GROUP"}}
        self.members = {}
        self.writes = []
        self.serial = 0
        self.changed = {}  # resource name -> serial of its last change
        self.deleted = {}  # resource name -> serial of its delete
        self.renamed = {}  # new resource name -> old one, reported once in a sync
        self.page_size = 1000
        self.expire_tokens = False
        self.before_write = None  # called before an update is applied, to stage a Google change

    def _next(self, prefix):
        self.serial += 1
        return "%s%s" % (prefix, self.serial)

    def _touch(self, resource):
        self.people[resource]["etag"] = self._next("e")
        self.changed[resource] = self.serial

    def __call__(self, method, url, params=None, json=None, timeout=None, headers=None):
        path = url.split("/v1/", 1)[1]
        params = params or {}
        if method != "GET":
            self.writes.append((method, path))
        if path == "people/me/connections":
            return self._connections(params)
        if path == "contactGroups" and method == "GET":
            return response(200, {"contactGroups": [dict(g, resourceName=r)
                                                    for r, g in self.groups.items()]})
        if path == "contactGroups" and method == "POST":
            resource = self._next("contactGroups/g")
            self.groups[resource] = {"name": json["contactGroup"]["name"],
                                     "groupType": "USER_CONTACT_GROUP"}
            return response(200, dict(self.groups[resource], resourceName=resource))
        if path.endswith("/members:modify"):
            group = path[:-len("/members:modify")]
            members = self.members.setdefault(group, set())
            members |= set(json.get("resourceNamesToAdd", []))
            members -= set(json.get("resourceNamesToRemove", []))
            for resource in json.get("resourceNamesToAdd", []) + json.get("resourceNamesToRemove", []):
                self._touch(resource)
            return response(200, {})
        if path == "people:createContact":
            resource = self._next("people/c")
            self.people[resource] = dict(copy.deepcopy(json), resourceName=resource)
            self._touch(resource)
            return response(200, self.view(resource))
        resource = path.split(":")[0]
        if resource not in self.people:
            return response(404, {"error": {"code": 404, "status": "NOT_FOUND",
                                            "message": "Requested entity was not found."}})
        if method == "GET":
            return response(200, self.view(resource))
        if path.endswith(":updateContact"):
            if self.before_write:
                self.before_write(self.people[resource])
                self.before_write = None
            if json["etag"] != self.people[resource]["etag"]:
                return response(400, {"error": {"code": 400, "status": "FAILED_PRECONDITION",
                                                "message": "Request person.etag is different"}})
            for field in params["updatePersonFields"].split(","):
                self.people[resource][field] = copy.deepcopy(json.get(field, []))
            self._touch(resource)
            return response(200, self.view(resource))
        raise AssertionError("unexpected call %s %s" % (method, path))

    def _connections(self, params):
        token = params.get("syncToken")
        if token and self.expire_tokens:
            return response(410, {"error": {
                "code": 410, "status": "FAILED_PRECONDITION", "message": "Sync token is expired.",
                "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                             "reason": "EXPIRED_SYNC_TOKEN"}]}})
        since = int(token[1:]) if token else None
        if since is None:
            found = [self.view(r) for r in self.people]
        else:
            found = [self.view(r) for r in self.people if self.changed.get(r, 0) > since]
            found += [{"resourceName": r, "etag": "gone", "metadata": {"deleted": True}}
                      for r, serial in self.deleted.items() if serial > since]
            for person in found:
                if person["resourceName"] in self.renamed:
                    person.setdefault("metadata", {})["previousResourceNames"] = [
                        self.renamed[person["resourceName"]]]
        start = int(params.get("pageToken") or 0)
        page = found[start:start + self.page_size]
        body = {"connections": page, "totalItems": len(found)}
        if start + self.page_size < len(found):
            body["nextPageToken"] = str(start + self.page_size)
        else:
            body["nextSyncToken"] = "s%s" % self.serial
        return response(200, body)

    def view(self, resource):
        """The person as Google returns it, with its labels."""
        person = copy.deepcopy(self.people[resource])
        groups = ["contactGroups/myContacts"] + sorted(
            g for g, members in self.members.items() if resource in members)
        person["memberships"] = [{"contactGroupMembership": {"contactGroupResourceName": g}}
                                 for g in groups]
        return person

    def add(self, labels=(), **fields):
        """A contact the user made in Google."""
        resource = self._next("people/g")
        self.people[resource] = copy.deepcopy(fields)
        for label in labels:
            self.members.setdefault(label, set()).add(resource)
        self._touch(resource)
        return resource

    def change(self, resource, **fields):
        """A change made by the user in Google."""
        self.people[resource].update(fields)
        self._touch(resource)

    def delete(self, resource):
        del self.people[resource]
        self.deleted[resource] = self.serial = self.serial + 1

    def rename(self, resource):
        """Google gives the contact a new resource name, as when it is linked to a profile."""
        new = self._next("people/r")
        self.people[new] = self.people.pop(resource)
        for members in self.members.values():
            if resource in members:
                members.discard(resource)
                members.add(new)
        self.renamed[new] = resource
        self._touch(new)
        return new

    def label(self, name):
        return next(r for r, g in self.groups.items() if g["name"] == name)

    def emails(self, resource):
        return [e["value"] for e in self.people[resource].get("emailAddresses", [])]
