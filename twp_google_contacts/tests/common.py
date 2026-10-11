import base64
import copy
import io
import json
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import requests

from odoo import fields

ACCOUNT = "odoo.addons.twp_google_contacts.models.twp_google_contacts_account"
BROKER = "odoo.addons.twp_google_contacts.models.broker.requests.post"
GOOGLE = "odoo.addons.twp_google_contacts.models.twp_google_contacts_account.requests.request"
SLEEP = "odoo.addons.twp_google_contacts.models.twp_google_contacts_account.time.sleep"


def response(status, body=None, headers=None, raw=None):
    r = requests.Response()
    r.status_code = status
    r._content = raw if raw is not None else json.dumps(body).encode() if body is not None else b""
    r.headers.update(headers or {})
    return r


def png(color="red"):
    """A small image, base64 encoded as Odoo keeps images."""
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(out, "PNG")
    return base64.b64encode(out.getvalue())


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


# A client's choices in Settings, cleared before each test class.
CHOICES = ["twp_google_contacts." + key for key in (
    "label", "scope", "direction", "owners", "import", "details_off", "tags_default",
    "labels_default", "conflict", "archive_deleted", "test_database")]


class GoogleContactsCase:
    """Mixin: a configured database, a fake ThriveWell Connect, and helpers."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        params = cls.env["ir.config_parameter"].sudo()
        params.set_param("twp_google_contacts.broker_url", "http://connect.test")
        params.set_param("twp_google_contacts.broker_key", "twc_test")
        # Writes are not spaced out in tests, and new contacts are made one at a time unless a
        # test asks for batches.
        params.set_param("twp_google_contacts.write_gap", "0")
        params.set_param("twp_google_contacts.batch_from", "100000")
        # Each test starts from the default choices, whatever the database it runs on has set.
        for key in CHOICES:
            params.set_param(key, False)
        cls.Account = cls.env["twp.google.contacts.account"]

    def setUp(self):
        super().setUp()
        self.broker = FakeBroker()
        for target, value in ((BROKER, self.broker),
                              (ACCOUNT + ".TwpGoogleContactsAccount._rollback", lambda s: None)):
            patcher = patch(target, value)
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
        self.updated = {}  # resource name -> when it last changed, as Google reports it
        self.photos = {}  # resource name -> photo address
        self.photo_bytes = {}  # photo address -> the image
        self.refuse = {}  # a path, or the end of one -> the answer Google gives it instead
        self.batches = []  # the size of each batch create
        self.group_page_size = 1000

    def _next(self, prefix):
        self.serial += 1
        return "%s%s" % (prefix, self.serial)

    def _touch(self, resource, at=None):
        self.people[resource]["etag"] = self._next("e")
        self.changed[resource] = self.serial
        at = at or datetime.now(timezone.utc).replace(tzinfo=None)
        self.updated[resource] = at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def __call__(self, method, url, params=None, json=None, timeout=None, headers=None):
        if url.startswith("https://lh3.example/"):
            image = self.photo_bytes.get(re.sub(r"=s\d+$", "", url))
            return response(200, raw=image) if image else response(404, raw=b"")
        path = url.split("/v1/", 1)[1]
        params = params or {}
        for key, answer in self.refuse.items():
            if path == key or path.endswith(key):
                return answer
        if method != "GET":
            self.writes.append((method, path))
        if path == "people/me/connections":
            return self._connections(params)
        if path == "contactGroups" and method == "GET":
            groups = [dict(g, resourceName=r) for r, g in self.groups.items()]
            start = int(params.get("pageToken") or 0)
            body = {"contactGroups": groups[start:start + self.group_page_size]}
            if start + self.group_page_size < len(groups):
                body["nextPageToken"] = str(start + self.group_page_size)
            return response(200, body)
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
        if path == "people:batchCreateContacts":
            self.batches.append(len(json["contacts"]))
            created = []
            for item in reversed(json["contacts"]):  # Google promises no order
                resource = self._next("people/c")
                self.people[resource] = dict(copy.deepcopy(item["contactPerson"]),
                                             resourceName=resource)
                self._touch(resource)
                created.append({"httpStatusCode": 200, "requestedResourceName": resource,
                                "person": self.view(resource)})
            return response(200, {"createdPeople": created})
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
        if path.endswith(":updateContactPhoto"):
            self.set_photo(resource, json["photoBytes"])
            return response(200, {"person": self.view(resource)})
        if path.endswith(":deleteContactPhoto"):
            self.photos.pop(resource, None)
            self._touch(resource)
            return response(200, {"person": self.view(resource)})
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
        person = dict(copy.deepcopy(self.people[resource]), resourceName=resource)
        groups = ["contactGroups/myContacts"] + sorted(
            g for g, members in self.members.items() if resource in members)
        person["memberships"] = [{"contactGroupMembership": {"contactGroupResourceName": g}}
                                 for g in groups]
        person["metadata"] = {"sources": [{"type": "CONTACT", "etag": person.get("etag"),
                                           "updateTime": self.updated.get(resource)}]}
        if resource in self.photos:
            person["photos"] = [{"url": self.photos[resource] + "=s100",
                                 "metadata": {"primary": True, "source": {"type": "CONTACT"}}}]
        else:
            # Google's generated initials, which are not a photo.
            person["photos"] = [{"url": "https://lh3.example/initials=s100", "default": True}]
        return person

    def add(self, labels=(), **fields):
        """A contact the user made in Google."""
        resource = self._next("people/g")
        self.people[resource] = copy.deepcopy(fields)
        for label in labels:
            self.members.setdefault(label, set()).add(resource)
        self._touch(resource)
        return resource

    def change(self, resource, at=None, **fields):
        """A change made by the user in Google, at the given time or now."""
        self.people[resource].update(fields)
        self._touch(resource, at)

    def set_photo(self, resource, image):
        """A photo put on the contact, from base64."""
        url = "https://lh3.example/%s" % self._next("photo")
        self.photos[resource] = url
        self.photo_bytes[url] = base64.b64decode(image)
        self._touch(resource)

    def delete(self, resource):
        del self.people[resource]
        self.deleted[resource] = self.serial = self.serial + 1

    def restore(self, resource, person):
        """The contact brought back from Google's bin."""
        self.people[resource] = copy.deepcopy(person)
        del self.deleted[resource]
        self._touch(resource)

    def merge(self, keep, lose):
        """The user merges `lose` into `keep`, as Google does it (proof 6): `keep` takes the other's
        emails and phones and keeps only its own anchor, and `lose` is deleted."""
        for field in ("emailAddresses", "phoneNumbers"):
            self.people[keep][field] = (self.people[keep].get(field, [])
                                        + self.people[lose].get(field, []))
        for members in self.members.values():
            if lose in members:
                members.add(keep)
        self._touch(keep)
        self.delete(lose)

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

    def relabel(self, resource, add=(), remove=()):
        """The user adds the contact to labels, or takes it out, in Google."""
        for group in add:
            self.members.setdefault(group, set()).add(resource)
        for group in remove:
            self.members.get(group, set()).discard(resource)
        self._touch(resource)

    def label(self, name):
        return next(r for r, g in self.groups.items() if g["name"] == name)

    def emails(self, resource):
        return [e["value"] for e in self.people[resource].get("emailAddresses", [])]


class SyncCase(GoogleContactsCase):
    """Mixin: one connected account syncing with a FakePeople, and partners to sync."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        params = cls.env["ir.config_parameter"].sudo()
        params.set_param("twp_google_contacts.test_database", True)
        # Only tagged partners go to Google, so the database's other partners stay out of the way.
        params.set_param("twp_google_contacts.scope", "tagged")
        cls.scope = cls.env["res.partner.category"].create({"name": "To Google"})
        cls.env.user.company_id.twp_google_contacts_tag_ids = cls.scope
        cls.Partner = cls.env["res.partner"]
        cls.Candidate = cls.env["twp.google.contacts.candidate"]
        cls.engines = cls.Partner.create({"name": "Lovelace Engines", "is_company": True})
        cls.ada = cls.Partner.create({"name": "Ada Lovelace", "email": "ada@example.com",
                                      "phone": "(317) 555-0140", "category_id": cls.scope.ids})

    def setUp(self):
        super().setUp()
        self.google = FakePeople()
        self.google.groups["contactGroups/odoo"] = {"name": "Odoo",
                                                     "groupType": "USER_CONTACT_GROUP"}
        for target, value in ((GOOGLE, self.google),
                              (ACCOUNT + ".TwpGoogleContactsAccount._commit_step", lambda s: None)):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.account = self.connect()

    def sync(self):
        return self.account.action_sync_now()["params"]

    def link(self, partner):
        return self.account.link_ids.filtered(lambda l: l.partner_id == partner)

    def pending(self):
        return self.account.candidate_ids.filtered(lambda c: c.state == "pending")

    def grace(self, **extra):
        """A contact the user keeps under the Odoo label in Google."""
        fields = {"names": [{"givenName": "Grace", "familyName": "Hopper"}],
                  "emailAddresses": [{"value": "grace@navy.example", "type": "work"}],
                  "phoneNumbers": [{"value": "+1 317 555 0150", "type": "mobile"}]}
        fields.update(extra)
        return self.google.add(labels=["contactGroups/odoo"], **fields)
