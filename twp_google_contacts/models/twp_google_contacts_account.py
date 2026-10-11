import logging
import random
import time
from collections import Counter, defaultdict
from datetime import timedelta

import requests

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import html2plaintext

from . import broker, google_person
from .res_config_settings import DEFAULT_LABEL

_logger = logging.getLogger(__name__)

PEOPLE_API = "https://people.googleapis.com/v1/"
TIMEOUT = 30
MAX_ATTEMPTS = 5
MAX_WAIT = 60


def state_message(env, state):
    """What a user is told when a connection cannot be used, in words they can act on."""
    return {
        "reconnect": env._(
            "Google no longer accepts this connection. It was revoked, or it expired. Open your "
            "preferences and press Connect again."),
        "blocked": env._(
            "Your Google Workspace administrator has not allowed ThriveWell's Google Contacts "
            "app. Ask them to allow it in the Google Admin console, under Security, API "
            "controls. ThriveWell's setup guide gives the client ID they need."),
    }.get(state, env._("Google Contacts is not connected."))


class GoogleReconnect(UserError):
    """The connection cannot be used until the user acts. The account's state is already set.

    Raising rolls back that state change, so an entry point catches this, reports it, and lets the
    transaction commit.
    """

    def __init__(self, env, state):
        super().__init__(state_message(env, state))
        self.state = state


class GoogleApiError(UserError):
    """Google answered a request with an error. `google_status` is Google's own name for it, such
    as FAILED_PRECONDITION for a stale etag."""

    def __init__(self, env, status, google_status, message):
        super().__init__(env._("Google refused the request: %(message)s", message=message))
        self.status = status
        self.google_status = google_status


class TwpGoogleContactsAccount(models.Model):
    _name = "twp.google.contacts.account"
    _description = "Connected Google Contacts account"
    _rec_name = "google_email"

    user_id = fields.Many2one("res.users", required=True, ondelete="cascade", index=True)
    google_email = fields.Char(string="Google account", readonly=True)
    state = fields.Selection(
        [("connected", "Connected"),
         ("reconnect", "Needs reconnecting"),
         ("blocked", "Blocked by Workspace admin")],
        default="connected", required=True, readonly=True)
    connection_id = fields.Char(readonly=True, groups="base.group_system")
    access_token = fields.Char(readonly=True, groups="base.group_system")
    access_token_expiry = fields.Datetime(readonly=True, groups="base.group_system")
    link_ids = fields.One2many("twp.google.contacts.link", "account_id")
    label_name = fields.Char(readonly=True, help="The Google label every synced contact is under")
    label_resource_name = fields.Char(readonly=True)
    last_sync = fields.Datetime(readonly=True)
    last_sync_result = fields.Char(readonly=True)

    _user_unique = models.Constraint("UNIQUE(user_id)", "A user connects one Google account.")

    # Connecting and disconnecting, through ThriveWell Connect.

    @api.model
    def _connect_start(self, return_url, state):
        """Return the Google sign-in address for the current user."""
        body = broker.call(self.env, "/v1/connect/start", {
            "connector": broker.CONNECTOR, "return_url": return_url, "state": state})
        return body["authorize_url"]

    @api.model
    def _connect_finish(self, code):
        """Redeem the one-time code from the sign-in and keep the connection for the current user."""
        body = broker.call(self.env, "/v1/connect/finish", {"code": code})
        values = {
            "google_email": body.get("account_email"),
            "connection_id": body["connection_id"],
            "state": "connected",
            "access_token": False,
            "access_token_expiry": False,
        }
        account = self.sudo().search([("user_id", "=", self.env.uid)])
        if account:
            account._release_connection()
            if account.google_email != values["google_email"]:
                # Another Google account: its contacts and labels are not the ones linked.
                account.link_ids.unlink()
                self.env["twp.google.contacts.label"].sudo().search(
                    [("account_id", "=", account.id)]).unlink()
                values.update(label_name=False, label_resource_name=False)
            account.write(values)
        else:
            account = self.sudo().create(dict(values, user_id=self.env.uid))
        return account

    def _release_connection(self):
        """Give the connection back to ThriveWell Connect, which revokes it at Google."""
        for account in self.sudo():
            try:
                broker.call(self.env, "/v1/disconnect", {"connection_id": account.connection_id})
            except broker.BrokerError as e:
                if e.status != 404:  # already gone is the outcome we wanted
                    raise UserError(self.env._(
                        "ThriveWell Connect could not disconnect Google (%(error)s). Nothing has "
                        "changed; try again in a few minutes.", error=e.error)) from None

    def _disconnect(self):
        self._release_connection()
        self.sudo().unlink()

    # One-hour access tokens.

    def _get_access_token(self):
        self.ensure_one()
        account = self.sudo()
        if account.state != "connected":
            raise GoogleReconnect(self.env, account.state)
        now = fields.Datetime.now()
        if account.access_token and account.access_token_expiry > now + timedelta(seconds=60):
            return account.access_token
        try:
            body = broker.call(self.env, "/v1/token", {"connection_id": account.connection_id})
        except broker.BrokerError as e:
            state = {"reconnect_required": "reconnect", "unknown_connection": "reconnect",
                     "admin_policy_enforced": "blocked"}.get(e.error)
            if state:
                account.write({"state": state, "access_token": False, "access_token_expiry": False})
                raise GoogleReconnect(self.env, state) from None
            if e.error == "connector_not_allowed":
                raise UserError(self.env._(
                    "This database's ThriveWell Connect key does not include Google Contacts. "
                    "Ask ThriveWell to add it.")) from None
            raise UserError(self.env._(
                "Google did not answer ThriveWell Connect. Syncing pauses and nothing is lost; "
                "try again later.")) from None
        account.write({
            "access_token": body["access_token"],
            "access_token_expiry": now + timedelta(seconds=int(body.get("expires_in", 3600))),
        })
        return body["access_token"]

    # The request layer for people.googleapis.com.

    def _google_request(self, method, path, params=None, json=None):
        """Call the People API for this account and return the decoded answer.

        Retries a lapsed token once, and waits out 429 and 5xx answers with exponential backoff.
        Writes take a lock on the account row first, because Google asks for one write at a time
        per account; a second write in another worker waits for the first to commit.
        """
        self.ensure_one()
        if method != "GET":
            self.env.cr.execute(
                "SELECT id FROM twp_google_contacts_account WHERE id = %s FOR UPDATE", [self.id])
        renewed = False
        response = None
        for attempt in range(MAX_ATTEMPTS):
            token = self._get_access_token()
            try:
                response = requests.request(
                    method, PEOPLE_API + path, params=params, json=json, timeout=TIMEOUT,
                    headers={"Authorization": "Bearer " + token})
            except requests.RequestException as e:
                _logger.info("People API %s %s: %s", method, path, type(e).__name__)
                response = None
            if response is not None and response.status_code == 401 and not renewed:
                renewed = True
                self.sudo().write({"access_token": False, "access_token_expiry": False})
                continue
            if response is None or response.status_code == 429 or response.status_code >= 500:
                if attempt + 1 < MAX_ATTEMPTS:
                    time.sleep(self._backoff(attempt, response))
                    continue
            break
        if response is None:
            raise UserError(self.env._(
                "Google could not be reached. Nothing has changed; try again later."))
        try:
            body = response.json() if response.content else {}
        except ValueError:
            body = {}
        if response.status_code < 400:
            return body
        error = body.get("error") if isinstance(body.get("error"), dict) else {}
        reasons = {d.get("reason") for d in error.get("details", []) if isinstance(d, dict)}
        if response.status_code in (401, 403) and (
                response.status_code == 401 or "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in reasons):
            # The token no longer carries the Contacts permission.
            self.sudo().write({"state": "reconnect", "access_token": False,
                               "access_token_expiry": False})
            raise GoogleReconnect(self.env, "reconnect")
        raise GoogleApiError(self.env, response.status_code, error.get("status"),
                             error.get("message") or "HTTP %s" % response.status_code)

    @staticmethod
    def _backoff(attempt, response):
        retry_after = response is not None and response.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            return min(int(retry_after), MAX_WAIT)
        return min(2 ** attempt, MAX_WAIT) + random.random()

    # Odoo to Google.

    def _check_may_sync(self):
        params = self.env["ir.config_parameter"].sudo()
        if (params.get_param("database.is_neutralized")
                and not params.get_param("twp_google_contacts.test_database")):
            raise UserError(self.env._(
                "This database is a copy, so it does not sync with Google: it would claim the "
                "contacts of the database it was copied from. If every connected Google account is "
                "a test account, an administrator can turn on \"Sync from this copy\" in "
                "Settings."))

    def _partners_to_push(self):
        """The partners this account's user can see that belong in Google, per Settings."""
        self.ensure_one()
        scope = self.env["ir.config_parameter"].sudo().get_param(
            "twp_google_contacts.scope", "contactable")
        domain = [("is_company", "=", False), ("type", "=", "contact")]
        if scope == "tagged":
            tags = self.user_id.company_id.twp_google_contacts_tag_ids
            if not tags:
                return self.env["res.partner"]
            domain.append(("category_id", "in", tags.ids))
        else:
            domain += ["|", ("email", "!=", False), ("phone", "!=", False)]
        # Colleagues are users of this database, not contacts to keep in Google.
        colleagues = self.env["res.users"].sudo().search([("share", "=", False)]).partner_id
        domain.append(("id", "not in", colleagues.ids))
        return self.env["res.partner"].with_user(self.user_id).search(domain).sudo()

    def _push(self):
        """Send this account's partners to Google: create what is new, merge what changed in
        Odoo, and keep labels in step with tags. Each contact is committed once Google has it."""
        self.ensure_one()
        self._check_may_sync()
        account = self.sudo()
        partners = account._partners_to_push()
        links = {link.partner_id.id: link for link in account.link_ids}
        main = account._main_label()
        # In tagged mode the scope tags only choose who syncs; they are not labels of their own.
        scope_tags = self.env["res.partner.category"]
        params = self.env["ir.config_parameter"].sudo()
        if params.get_param("twp_google_contacts.scope") == "tagged":
            scope_tags = account.user_id.company_id.twp_google_contacts_tag_ids
        tag_labels = account._tag_labels(partners.category_id - scope_tags)
        db_uuid = self.env["ir.config_parameter"].sudo().get_param("database.uuid")
        counts = Counter()
        label_changes = []
        for partner in partners:
            link = links.get(partner.id)
            if link and link.state == "gone":
                # Deleted in Google. Never re-created without asking (proof 6).
                continue
            values = google_person.partner_values(partner, html2plaintext)
            try:
                if not link:
                    link = account._create_contact(partner, values, db_uuid)
                    counts["created"] += 1
                elif google_person.changed_groups(values, link.pushed):
                    if account._update_contact(link, values):
                        counts["updated"] += 1
            except GoogleApiError as e:
                counts["failed"] += 1
                _logger.warning("Google Contacts: partner %s not synced for account %s: %s %s",
                                partner.id, account.id, e.status, e.google_status)
                continue
            wanted = sorted({main} | {tag_labels[tag.id]
                                      for tag in partner.category_id - scope_tags})
            if link.state == "active" and wanted != (link.label_resource_names or []):
                label_changes.append((link, wanted))
            self._commit_step()
        account._apply_labels(label_changes)
        counts["labelled"] = len(label_changes)
        account.write({"last_sync": fields.Datetime.now(),
                       "last_sync_result": self._describe(counts)})
        self._commit_step()
        return counts

    def _create_contact(self, partner, values, db_uuid):
        person = google_person.new_person(values, google_person.anchor(db_uuid, partner.id))
        body = self._google_request("POST", "people:createContact",
                                    params={"personFields": google_person.PERSON_FIELDS},
                                    json=person)
        return self.env["twp.google.contacts.link"].sudo().create({
            "account_id": self.id,
            "partner_id": partner.id,
            "resource_name": body["resourceName"],
            "etag": body.get("etag"),
            "pushed": values,
            "google_hash": google_person.person_hash(body),
            "label_resource_names": [],
            "synced_at": fields.Datetime.now(),
        })

    def _update_contact(self, link, values):
        """Merge the groups that changed in Odoo into the contact as Google holds it now. A stale
        etag means Google changed it meanwhile: read it again and merge again (proof 3)."""
        groups = google_person.changed_groups(values, link.pushed)
        update_fields = ",".join(google_person.GROUPS[group] for group in groups)
        for attempt in range(3):
            try:
                person = self._google_request("GET", link.resource_name, params={
                    "personFields": google_person.PERSON_FIELDS,
                    "sources": "READ_SOURCE_TYPE_CONTACT"})
                body = google_person.merge(person, values, link.pushed, groups)
                body["etag"] = person["etag"]
                result = self._google_request(
                    "PATCH", link.resource_name + ":updateContact",
                    params={"updatePersonFields": update_fields,
                            "personFields": google_person.PERSON_FIELDS},
                    json=body)
                break
            except GoogleApiError as e:
                if e.status == 404:
                    link.state = "gone"
                    return False
                if e.google_status == "FAILED_PRECONDITION" and attempt < 2:
                    continue
                raise
        link.write({
            "etag": result.get("etag"),
            "pushed": values,
            "google_hash": google_person.person_hash(result),
            "synced_at": fields.Datetime.now(),
        })
        return True

    # Labels.

    def _main_label(self):
        """The label every synced contact goes under, found by name or created."""
        name = self.env["ir.config_parameter"].sudo().get_param(
            "twp_google_contacts.label") or DEFAULT_LABEL
        if self.label_resource_name and self.label_name == name:
            return self.label_resource_name
        resource = self._find_or_create_label(name)
        self.write({"label_name": name, "label_resource_name": resource})
        return resource

    def _tag_labels(self, tags):
        """Map each tag id to its Google label, creating labels Google does not have yet."""
        Label = self.env["twp.google.contacts.label"].sudo()
        known = Label.search([("account_id", "=", self.id), ("category_id", "in", tags.ids)])
        result = {label.category_id.id: label.resource_name for label in known}
        for tag in tags.filtered(lambda t: t.id not in result):
            resource = self._find_or_create_label(tag.name)
            Label.create({"account_id": self.id, "category_id": tag.id,
                          "resource_name": resource})
            result[tag.id] = resource
        return result

    def _find_or_create_label(self, name):
        groups = self._google_request("GET", "contactGroups", params={
            "pageSize": 1000, "groupFields": "name,groupType"}).get("contactGroups", [])
        for group in groups:
            if group.get("groupType") == "USER_CONTACT_GROUP" and group.get("name") == name:
                return group["resourceName"]
        created = self._google_request("POST", "contactGroups",
                                       json={"contactGroup": {"name": name}})
        return created["resourceName"]

    def _apply_labels(self, changes):
        """Add and remove contacts from labels, one call per label. Only labels Odoo put a
        contact in are ever taken away; the user's own labels are never touched."""
        by_label = defaultdict(lambda: {"resourceNamesToAdd": [], "resourceNamesToRemove": []})
        for link, wanted in changes:
            current = set(link.label_resource_names or [])
            for label in set(wanted) - current:
                by_label[label]["resourceNamesToAdd"].append(link.resource_name)
            for label in current - set(wanted):
                by_label[label]["resourceNamesToRemove"].append(link.resource_name)
        failed = set()
        for label, body in by_label.items():
            try:
                self._google_request("POST", label + "/members:modify",
                                     json={k: v for k, v in body.items() if v})
            except GoogleApiError as e:
                failed.add(label)
                _logger.warning("Google Contacts: label %s not updated for account %s: %s %s",
                                label, self.id, e.status, e.google_status)
        for link, wanted in changes:
            current, wanted = set(link.label_resource_names or []), set(wanted)
            # A label whose call failed keeps its old membership, so the next sync retries it.
            link.label_resource_names = sorted(
                (current & wanted) | ((wanted - current) - failed) | ((current - wanted) & failed))
        self._commit_step()

    def _commit_step(self):
        self.env["ir.cron"]._commit_progress(processed=1)

    def _describe(self, counts):
        return self.env._(
            "%(created)s created, %(updated)s updated, %(failed)s failed",
            created=counts["created"], updated=counts["updated"], failed=counts["failed"])

    @api.model
    def _cron_sync(self):
        for account in self.sudo().search([("state", "=", "connected")]):
            try:
                account._push()
            except GoogleReconnect:
                pass  # the account's state now says so, and it commits below
            except UserError as e:
                _logger.info("Google Contacts: account %s not synced: %s", account.id, e.args[0])
                account.last_sync_result = e.args[0]
            self._commit_step()

    # Buttons.

    def action_check(self):
        """Ask Google for one label, to prove the whole chain works. Reports, never raises."""
        self.ensure_one()
        try:
            body = self._google_request("GET", "contactGroups", params={"pageSize": 1})
        except GoogleReconnect as e:
            return self._notify(e.args[0], "danger")
        except UserError as e:
            return self._notify(e.args[0], "warning")
        message = self.env._(
            "Google Contacts is working for %(email)s. Google answered with %(count)s labels.",
            email=self.google_email, count=body.get("totalItems", 0))
        return self._notify(message, "success")

    def action_sync_now(self):
        """Sync this account now and report. Reports, never raises."""
        self.ensure_one()
        try:
            counts = self._push()
        except GoogleReconnect as e:
            return self._notify(e.args[0], "danger")
        except UserError as e:
            return self._notify(e.args[0], "warning")
        kind = "warning" if counts["failed"] else "success"
        return self._notify(self._describe(counts), kind)

    def action_disconnect(self):
        self._disconnect()
        return {"type": "ir.actions.client", "tag": "soft_reload"}

    @api.model
    def _notify(self, message, kind):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": self.env._("Google Contacts"), "message": message, "type": kind,
                       "sticky": kind != "success"},
        }
