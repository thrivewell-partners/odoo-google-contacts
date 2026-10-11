import logging
import random
import time
from collections import Counter, defaultdict
from datetime import timedelta

import requests

from odoo import Command, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import escape_psql, html2plaintext, plaintext2html

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
    as FAILED_PRECONDITION for a stale etag, and `reasons` the finer ones, such as
    EXPIRED_SYNC_TOKEN."""

    def __init__(self, env, status, google_status, message, reasons=()):
        super().__init__(env._("Google refused the request: %(message)s", message=message))
        self.status = status
        self.google_status = google_status
        self.reasons = set(reasons)


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
    sync_token = fields.Char(readonly=True, groups="base.group_system",
                             help="Google's bookmark: the next pull asks only for what changed "
                                  "since it was given")
    sync_fields = fields.Char(readonly=True, groups="base.group_system",
                              help="The fields the sync token was asked for. Asking for other "
                                   "fields needs a full read")
    last_full_sync = fields.Datetime(readonly=True)
    import_reviewed = fields.Boolean(
        readonly=True,
        help="The user has reviewed the first import. From then on, Google contacts under the "
             "label that match no Odoo contact are added without asking")
    candidate_ids = fields.One2many("twp.google.contacts.candidate", "account_id")
    review_count = fields.Integer(string="To review", compute="_compute_review_count")

    _user_unique = models.Constraint("UNIQUE(user_id)", "A user connects one Google account.")

    def _compute_review_count(self):
        counts = dict(self.env["twp.google.contacts.candidate"].sudo()._read_group(
            [("account_id", "in", self.ids), ("state", "=", "pending")],
            ["account_id"], ["__count"]))
        for account in self:
            account.review_count = counts.get(account, 0)

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
                account.candidate_ids.unlink()
                self.env["twp.google.contacts.label"].sudo().search(
                    [("account_id", "=", account.id)]).unlink()
                values.update(label_name=False, label_resource_name=False, sync_token=False,
                              sync_fields=False, last_full_sync=False, import_reviewed=False)
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
                             error.get("message") or "HTTP %s" % response.status_code, reasons)

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
        """The partners this account's user can see that belong in Google, per Settings, and
        every partner already linked to a Google contact: a link keeps syncing once made, whether
        it was made from Odoo or from Google."""
        self.ensure_one()
        Partner = self.env["res.partner"].with_user(self.user_id)
        linked = Partner.search([("id", "in", self.link_ids.filtered(
            lambda l: l.state == "active").partner_id.ids)])
        scope = self.env["ir.config_parameter"].sudo().get_param(
            "twp_google_contacts.scope", "contactable")
        domain = [("is_company", "=", False), ("type", "=", "contact")]
        if scope == "tagged":
            tags = self.user_id.company_id.twp_google_contacts_tag_ids
            if not tags:
                return linked.sudo()
            domain.append(("category_id", "in", tags.ids))
        else:
            domain += ["|", ("email", "!=", False), ("phone", "!=", False)]
        # Colleagues are users of this database, not contacts to keep in Google.
        colleagues = self.env["res.users"].sudo().search([("share", "=", False)]).partner_id
        domain.append(("id", "not in", colleagues.ids))
        return (Partner.search(domain) | linked).sudo()

    def _sync(self):
        """Pull Google's changes into Odoo, then push Odoo's to Google, and record the result.
        Pulling first means a change made in Google is in Odoo before Odoo sends its own."""
        self.ensure_one()
        self._check_may_sync()
        account = self.sudo()
        counts = Counter()
        account._pull(counts)
        account._push(counts)
        account.write({"last_sync": fields.Datetime.now(),
                       "last_sync_result": account._describe(counts)})
        self._commit_step()
        return counts

    def _push(self, counts):
        """Send this account's partners to Google: create what is new, merge what changed in
        Odoo, and keep labels in step with tags. Each contact is committed once Google has it."""
        self.ensure_one()
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

    def _update_contact(self, link, values, anchor=None):
        """Merge the groups that changed in Odoo into the contact as Google holds it now, and
        write Odoo's anchor when given one. A stale etag means Google changed the contact
        meanwhile: read it again and merge again (proof 3)."""
        groups = google_person.changed_groups(values, link.pushed)
        update_fields = [google_person.GROUPS[group] for group in groups]
        if anchor:
            update_fields.append("clientData")
        for attempt in range(3):
            try:
                person = self._get_person(link.resource_name)
                body = google_person.merge(person, values, link.pushed, groups)
                if anchor:
                    body["clientData"] = google_person.with_anchor(person, anchor)
                body["etag"] = person["etag"]
                result = self._google_request(
                    "PATCH", link.resource_name + ":updateContact",
                    params={"updatePersonFields": ",".join(update_fields),
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
            "resource_name": result.get("resourceName") or link.resource_name,
            "etag": result.get("etag"),
            "pushed": values,
            "google_hash": google_person.person_hash(result),
            "synced_at": fields.Datetime.now(),
        })
        return True

    def _get_person(self, resource_name):
        return self._google_request("GET", resource_name, params={
            "personFields": google_person.PERSON_FIELDS, "sources": "READ_SOURCE_TYPE_CONTACT"})

    # Google to Odoo.

    def _pull(self, counts):
        """Bring into Odoo what changed in Google since the last pull. With no sync token, or one
        Google has expired (they last 7 days), read every contact and reconcile the links with
        what is there."""
        self.ensure_one()
        token = self.sync_token if self.sync_fields == google_person.PERSON_FIELDS else False
        try:
            self._pull_pages(token, counts)
        except GoogleApiError as e:
            if not token or not (e.status == 410 or "EXPIRED_SYNC_TOKEN" in e.reasons):
                raise
            self._pull_pages(False, counts)

    def _pull_pages(self, token, counts):
        label = self._main_label()
        params = {"personFields": google_person.PERSON_FIELDS,
                  "sources": "READ_SOURCE_TYPE_CONTACT", "pageSize": 1000,
                  "requestSyncToken": "true"}
        if token:
            params["syncToken"] = token
        started = fields.Datetime.now()
        seen = set()
        while True:
            body = self._google_request("GET", "people/me/connections", params=params)
            for person in body.get("connections", []):
                seen.add(person["resourceName"])
                try:
                    with self.env.cr.savepoint():
                        self._take_person(person, label, counts)
                except (GoogleApiError, AccessError, ValidationError) as e:
                    counts["failed"] += 1
                    _logger.warning("Google Contacts: %s not taken for account %s: %s",
                                    person["resourceName"], self.id, e)
                self._commit_step()
            if not body.get("nextPageToken"):
                break
            params = dict(params, pageToken=body["nextPageToken"])
        values = {"sync_token": body.get("nextSyncToken"),
                  "sync_fields": google_person.PERSON_FIELDS}
        if not token:
            # A full read lists every contact, so a linked one it did not list was deleted.
            missing = self.link_ids.filtered(
                lambda l: l.state == "active" and l.resource_name not in seen)
            missing.state = "gone"
            counts["gone"] += len(missing)
            self.candidate_ids.filtered(
                lambda c: c.state == "pending" and c.resource_name not in seen).unlink()
            values["last_full_sync"] = started
        self.write(values)
        self._commit_step()

    def _take_person(self, person, label, counts):
        """One contact from Google's list: follow it to its link, or offer it for import."""
        resource = person["resourceName"]
        names = [resource] + person.get("metadata", {}).get("previousResourceNames", [])
        Link = self.env["twp.google.contacts.link"].sudo()
        link = Link.search([("account_id", "=", self.id), ("resource_name", "in", names)],
                           limit=1)
        candidate = self.env["twp.google.contacts.candidate"].sudo().search(
            [("account_id", "=", self.id), ("resource_name", "in", names)], limit=1)
        if person.get("metadata", {}).get("deleted"):
            # Deletes are not carried into Odoo yet (M4); the link only stops syncing.
            if link.state == "active":
                link.state = "gone"
                counts["gone"] += 1
            candidate.filtered(lambda c: c.state == "pending").unlink()
            return
        if not link:
            link = self._link_by_anchor(person)
        if link:
            # Google renamed it, or it came back from the bin.
            if link.resource_name != resource or link.state != "active":
                link.write({"resource_name": resource, "state": "active"})
            candidate.unlink()
            self._take_changes(link, person, counts, first=not link.pushed)
            return
        if not google_person.in_label(person, label):
            candidate.filtered(lambda c: c.state == "pending").unlink()
            return
        self._offer(person, candidate, counts)

    def _link_by_anchor(self, person):
        """A contact Odoo once sent carries its partner id, which survives a lost link."""
        Link = self.env["twp.google.contacts.link"].sudo()
        db_uuid = self.env["ir.config_parameter"].sudo().get_param("database.uuid")
        partner_id = google_person.anchored_partner_id(person, db_uuid)
        partner = self.env["res.partner"].sudo().browse(partner_id).exists() if partner_id else None
        if not partner:
            return Link
        link = Link.search([("account_id", "=", self.id), ("partner_id", "=", partner.id)])
        if link:
            return link
        ours = {self.label_resource_name} | set(self.env["twp.google.contacts.label"].sudo().search(
            [("account_id", "=", self.id)]).mapped("resource_name"))
        return Link.create({
            "account_id": self.id,
            "partner_id": partner.id,
            "resource_name": person["resourceName"],
            "pushed": {},
            "label_resource_names": sorted(google_person.label_resource_names(person) & ours),
        })

    def _take_changes(self, link, person, counts, first=False):
        """Bring the contact's changes into its partner, group by group.

        A group Google changed comes into Odoo unless Odoo changed it too, in which case Odoo's
        stands and the push merges it into Google (most-recent-wins is M4). On a first link,
        Google only fills what the partner lacks; where both have a value and they differ, Odoo's
        is added to Google's list beside Google's own, so neither is lost.
        """
        if not first and link.google_hash == google_person.person_hash(person):
            # Odoo's own write coming back, or a change to something Odoo does not sync.
            if link.etag != person.get("etag"):
                link.etag = person.get("etag")
            return
        partner = link.partner_id.with_user(self.user_id)
        pushed = link.pushed or {}
        google = google_person.google_values(person, pushed)
        odoo = google_person.partner_values(partner, html2plaintext)
        if first:
            take = [g for g in google_person.GROUPS if odoo[g] is None and google[g] is not None]
        else:
            # A partner keeps its name when Google's is cleared.
            take = [g for g in google_person.GROUPS
                    if google[g] != pushed.get(g) and odoo[g] == pushed.get(g)
                    and (google[g] is not None or g != "name")]
        if take:
            partner.write(self._partner_vals(google, take))
            counts["changed_from_google"] += 1
        after = google_person.partner_values(partner, html2plaintext)
        new_pushed = dict(pushed)
        for group in google_person.GROUPS:
            if group in take:
                new_pushed[group] = after[group]
            elif first:
                new_pushed[group] = after[group] if after[group] == google[group] else None
        link.write({"pushed": new_pushed, "etag": person.get("etag"),
                    "google_hash": google_person.person_hash(person),
                    "synced_at": fields.Datetime.now()})
        if first:
            db_uuid = self.env["ir.config_parameter"].sudo().get_param("database.uuid")
            self._update_contact(link, after, google_person.anchor(db_uuid, partner.id))

    def _offer(self, person, candidate, counts):
        """A Google contact under the label that Odoo does not know. Before the first review, and
        whenever it looks like a partner Odoo has, it waits for the user; otherwise it is added."""
        if candidate.state == "skipped":
            return
        match, reason = self._match(person)
        if not match and self.import_reviewed:
            candidate.unlink()
            self._create_from_google(person)
            counts["added_from_google"] += 1
            return
        google = google_person.google_values(person)
        values = {
            "name": self._display_name(google),
            "email": google["email"],
            "phone": google["phone"],
            "company_name": (google["organization"] or {}).get("name"),
            "match_reason": reason,
            "state": "pending",
        }
        if not candidate.match_id:
            values["match_id"] = match.id
        if candidate:
            candidate.write(values)
        else:
            self.env["twp.google.contacts.candidate"].sudo().create(
                dict(values, account_id=self.id, resource_name=person["resourceName"]))

    def _match(self, person):
        """The Odoo contact this Google contact most likely is: same email first, then the same
        phone number. Contacts already linked in this account, and colleagues, are never
        matched."""
        Partner = self.env["res.partner"].with_user(self.user_id)
        colleagues = self.env["res.users"].sudo().search([("share", "=", False)]).partner_id
        base = [("is_company", "=", False),
                ("id", "not in", (self.link_ids.partner_id | colleagues).ids)]
        for item in person.get("emailAddresses", []):
            email = (item.get("value") or "").strip()
            match = email and Partner.search(base + [("email", "=ilike", escape_psql(email))],
                                             limit=1)
            if match:
                return match, "email"
        tails = {google_person.phone_tail(item.get("value") or "")
                 for item in person.get("phoneNumbers", [])} - {None}
        if tails:
            self.env.cr.execute(
                "SELECT id FROM res_partner WHERE phone IS NOT NULL"
                r" AND right(regexp_replace(phone, '\D', '', 'g'), 10) = ANY(%s)",
                [list(tails)])
            ids = [row[0] for row in self.env.cr.fetchall()]
            match = ids and Partner.search(base + [("id", "in", ids)], limit=1)
            if match:
                return match, "phone"
        return Partner, False

    def _create_from_google(self, person):
        """Add a Google contact to Odoo as a new partner, linked, with Odoo's anchor written."""
        google = google_person.google_values(person)
        values = self._partner_vals(google, list(google_person.GROUPS))
        values["name"] = values.get("name") or self._display_name(google)
        # Labels Odoo made from tags become those tags again.
        Label = self.env["twp.google.contacts.label"].sudo()
        labels = Label.search([("account_id", "=", self.id), ("resource_name", "in",
                               list(google_person.label_resource_names(person)))])
        values["category_id"] = [Command.set(labels.category_id.ids)]
        params = self.env["ir.config_parameter"].sudo()
        partner = self.env["res.partner"].with_user(self.user_id).create(values)
        pushed = google_person.partner_values(partner, html2plaintext)
        link = self.env["twp.google.contacts.link"].sudo().create({
            "account_id": self.id,
            "partner_id": partner.id,
            "resource_name": person["resourceName"],
            "etag": person.get("etag"),
            "pushed": pushed,
            "google_hash": google_person.person_hash(person),
            "label_resource_names": sorted({self.label_resource_name}
                                           | set(labels.mapped("resource_name"))),
            "synced_at": fields.Datetime.now(),
        })
        db_uuid = params.get_param("database.uuid")
        self._update_contact(link, pushed, google_person.anchor(db_uuid, partner.id))
        return partner

    def _review(self, candidates, decision):
        """Carry out the user's decision on Google contacts waiting for review: "link" each to its
        chosen Odoo contact, or add it when none is chosen; "create" adds each as new; "skip"
        keeps each out of Odoo for good."""
        self.ensure_one()
        self._check_may_sync()
        self._main_label()
        Link = self.env["twp.google.contacts.link"].sudo()
        for candidate in candidates:
            if decision == "skip":
                candidate.state = "skipped"
                continue
            try:
                person = self._get_person(candidate.resource_name)
            except GoogleApiError as e:
                if e.status != 404:
                    raise
                candidate.unlink()  # deleted in Google since
                continue
            partner = candidate.match_id if decision == "link" else None
            if partner:
                if Link.search_count([("account_id", "=", self.id),
                                      ("partner_id", "=", partner.id)]):
                    raise UserError(self.env._(
                        "%(name)s is already linked to another Google contact.",
                        name=partner.display_name))
                link = Link.create({
                    "account_id": self.id,
                    "partner_id": partner.id,
                    "resource_name": person["resourceName"],
                    "pushed": {},
                    "label_resource_names": [self.label_resource_name],
                })
                self._take_changes(link, person, Counter(), first=True)
            else:
                self._create_from_google(person)
            candidate.state = "done"
            self._commit_step()
        if not self.candidate_ids.filtered(lambda c: c.state == "pending"):
            self.import_reviewed = True

    def _partner_vals(self, google, groups):
        """Partner field values from Google's, for the given groups."""
        vals = {}
        if "name" in groups and google["name"]:
            vals["name"] = " ".join(filter(None, (google["name"]["givenName"],
                                                  google["name"]["familyName"])))
        if "organization" in groups:
            organization = google["organization"] or {}
            vals["function"] = organization.get("title") or False
            if organization.get("name"):
                vals["parent_id"] = self._company_named(organization["name"]).id
        for group in ("email", "phone", "website"):
            if group in groups:
                vals[group] = google[group] or False
        if "address" in groups:
            vals.update(self._address_vals(google["address"]))
        if "note" in groups:
            vals["comment"] = plaintext2html(google["note"]) if google["note"] else False
        return vals

    def _company_named(self, name):
        """The Odoo company of that name, ignoring case, or a new one when Odoo has none
        (Tyler's decision, 2026-10-11)."""
        Partner = self.env["res.partner"].with_user(self.user_id)
        name = name.strip()
        company = Partner.search([("is_company", "=", True),
                                  ("name", "=ilike", escape_psql(name))], limit=1)
        return company or Partner.create({"name": name, "is_company": True})

    def _address_vals(self, address):
        address = address or {}
        Country = self.env["res.country"]
        country = Country
        if address.get("countryCode"):
            country = Country.search([("code", "=ilike", address["countryCode"])], limit=1)
        if not country and address.get("country"):
            country = Country.search([("name", "=ilike", escape_psql(address["country"]))],
                                     limit=1)
        state = self.env["res.country.state"]
        if address.get("region"):
            region = escape_psql(address["region"])
            domain = ["|", ("name", "=ilike", region), ("code", "=ilike", region)]
            if country:
                domain.append(("country_id", "=", country.id))
            state = state.search(domain, limit=1)
            country = country or state.country_id
        return {
            "street": address.get("streetAddress") or False,
            "street2": address.get("extendedAddress") or False,
            "city": address.get("city") or False,
            "zip": address.get("postalCode") or False,
            "state_id": state.id,
            "country_id": country.id,
        }

    def _display_name(self, google):
        name = google["name"] or {}
        return (" ".join(filter(None, (name.get("givenName"), name.get("familyName"))))
                or (google["organization"] or {}).get("name") or google["email"]
                or google["phone"] or self.env._("Google contact"))

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
        _ = self.env._
        parts = [
            (counts["added_from_google"], _("%(n)s added from Google")),
            (counts["changed_from_google"], _("%(n)s updated from Google")),
            (counts["created"], _("%(n)s sent to Google")),
            (counts["updated"], _("%(n)s updated in Google")),
            (self.review_count, _("%(n)s waiting for your review")),
            (counts["failed"], _("%(n)s failed")),
        ]
        said = [text % {"n": n} for n, text in parts if n]
        return ", ".join(said) if said else _("Nothing to change")

    @api.model
    def _cron_sync(self):
        for account in self.sudo().search([("state", "=", "connected")]):
            try:
                account._sync()
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
            counts = self._sync()
        except GoogleReconnect as e:
            return self._notify(e.args[0], "danger")
        except UserError as e:
            return self._notify(e.args[0], "warning")
        kind = "warning" if counts["failed"] or self.review_count else "success"
        return self._notify(self._describe(counts), kind)

    def action_review(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Google contacts to review"),
            "res_model": "twp.google.contacts.candidate",
            "view_mode": "list",
            "domain": [("account_id", "=", self.id)],
            "context": {"search_default_pending": 1},
        }

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
