import base64
import hashlib
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
from .res_config_settings import DEFAULT_LABEL, DETAILS_OFF, DIRECTIONS, OWNERS
from .res_partner import FROM_GOOGLE
from .twp_google_contacts_field_map import Unreadable
from .twp_google_contacts_link import HELD, LIVE, STOPPED

_logger = logging.getLogger(__name__)

PEOPLE_API = "https://people.googleapis.com/v1/"
TIMEOUT = 30
MAX_ATTEMPTS = 5
MAX_WAIT = 60
# A photo Google changed while Odoo's is the one kept: the push puts Odoo's back.
PUT_BACK = "put-back"


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
    direction = fields.Selection(
        DIRECTIONS, required=True, default=lambda self: self._default_choice("direction", "both"),
        help="The user's own choice, starting from the Settings default when they connect")
    owners = fields.Selection(
        OWNERS, required=True, default=lambda self: self._default_choice("owners", "all"),
        help="Whose contacts go to this Google: the user's own choice, by Salesperson")
    keys_indexed = fields.Boolean(
        readonly=True, help="A full read has filled the index of the emails and phones Google "
                            "holds, so a contact Google already has is found before creating one")

    _user_unique = models.Constraint("UNIQUE(user_id)", "A user connects one Google account.")

    @api.model
    def _default_choice(self, name, default):
        return self.env["ir.config_parameter"].sudo().get_param(
            "twp_google_contacts." + name) or default

    def write(self, vals):
        if "direction" in vals:
            # A new direction starts from a full read, so both sides are compared afresh.
            changed = self.filtered(lambda a: a.direction != vals["direction"])
            changed.sudo().write({"sync_token": False})
        return super().write(vals)

    @api.depends("candidate_ids.state")
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
                self.env["twp.google.contacts.key"].sudo().search(
                    [("account_id", "=", account.id)]).unlink()
                values.update(label_name=False, label_resource_name=False, sync_token=False,
                              sync_fields=False, last_full_sync=False, import_reviewed=False,
                              keys_indexed=False)
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

    def _synced_groups(self):
        """The groups that sync: per Settings, the name always and the rest unless left out,
        then each mapped field."""
        off = set((self.env["ir.config_parameter"].sudo().get_param(DETAILS_OFF) or "").split(","))
        return [group for group in google_person.GROUPS if group == "name" or group not in off] + [
            group for group in self._maps()]

    def _maps(self):
        """The mapped fields, by group, read once per sync."""
        cache = self.env.cr.cache.setdefault("twp_google_contacts_maps", {})
        if self.id not in cache:
            maps = self.env["twp.google.contacts.field.map"].sudo().search([])
            cache[self.id] = {row._group(): row for row in maps}
        return cache[self.id]

    def _values(self, partner):
        """The partner as the values Odoo sends: the fixed groups, then each mapped field."""
        values = google_person.partner_values(partner, html2plaintext)
        for group, row in self._maps().items():
            values[group] = row._odoo_value(partner)
        return values

    def _google(self, person, pushed=None):
        """The person in the shape of _values."""
        claimed = defaultdict(set)
        for row in self._maps().values():
            if row.target in ("phone", "email"):
                claimed[row.target].add((row.google_key or "").strip().lower())
        values = google_person.google_values(person, pushed, claimed)
        for group, row in self._maps().items():
            values[group] = google_person.mapped_value(person, row.target, row.google_key)
        return values

    def _syncs_photo(self):
        off = (self.env["ir.config_parameter"].sudo().get_param(DETAILS_OFF) or "").split(",")
        return "photo" not in off

    def _masked(self, values, pushed):
        """Odoo's values with each group left out of the sync as it last was, so it is neither
        sent nor seen as changed."""
        synced = self._synced_groups()
        maps = self._maps()
        return {group: value if group in synced and (group not in maps or maps[group]._sends())
                else (pushed or {}).get(group) for group, value in values.items()}

    def _owner_domain(self):
        """Whose contacts go to this Google, as a partner domain: the user's choice, by
        Salesperson."""
        if self.owners == "mine":
            return [("user_id", "=", self.user_id.id)]
        if self.owners == "mine_unowned":
            return [("user_id", "in", [self.user_id.id, False])]
        return []

    def _partners_to_push(self):
        """The partners this account's user can see that belong in Google, per Settings and the
        user's choice of whose, and every partner already linked to a Google contact: a link keeps
        syncing once made, whether it was made from Odoo or from Google, until the partner is
        archived or, when the user chose their own contacts, owned by someone else."""
        self.ensure_one()
        Partner = self.env["res.partner"].with_user(self.user_id)
        linked = Partner.search([("id", "in", self.link_ids.filtered(
            lambda l: l.state == "active").partner_id.ids)])
        scope = self.env["ir.config_parameter"].sudo().get_param(
            "twp_google_contacts.scope", "contactable")
        domain = [("is_company", "=", False), ("type", "=", "contact")] + self._owner_domain()
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
        for key in ("twp_google_contacts_labels", "twp_google_contacts_maps"):
            self.env.cr.cache.get(key, {}).pop(self.id, None)
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
        Odoo, and keep labels and photos in step. A partner archived or deleted in Odoo leaves
        Odoo's labels in Google and is never deleted there. Each contact is committed once Google
        has it. Google to Odoo only sends nothing."""
        self.ensure_one()
        account = self.sudo()
        label_changes = account._stop_or_resume(counts)
        if account.direction == "to_odoo":
            account.link_ids.filtered(lambda l: not l.partner_id).unlink()
            return counts
        partners = account._partners_to_push()
        links = {link.partner_id.id: link for link in account.link_ids if link.partner_id}
        main = account._main_label()
        # In tagged mode the scope tags only choose who syncs; they are not labels of their own.
        scope_tags = self.env["res.partner.category"]
        params = self.env["ir.config_parameter"].sudo()
        if params.get_param("twp_google_contacts.scope") == "tagged":
            scope_tags = account.user_id.company_id.twp_google_contacts_tag_ids
        tag_labels = account._tag_labels(account._tags_out(partners.category_id, scope_tags))
        db_uuid = self.env["ir.config_parameter"].sudo().get_param("database.uuid")
        photos = account._photo_sums(partners)
        reviewing = account.candidate_ids.filtered(
            lambda c: c.kind == "import" and c.state == "pending").match_id
        for partner in partners:
            link = links.get(partner.id)
            if link and link.state in STOPPED:
                # Deleted or merged away in Google. Never re-created without asking (proof 6).
                continue
            if not link and partner in reviewing:
                continue  # the likely match of a Google contact: the user's review links them
            values = account._masked(account._values(partner), link.pushed if link else {})
            try:
                twin = not link and account._twin(partner, values)
                if twin:
                    link = account._link_twin(partner, twin, counts)
                    account._push_photo(link, partner, photos.get(partner.id))
                elif not link:
                    link = account._create_contact(partner, values, db_uuid)
                    counts["created"] += 1
                    account._push_photo(link, partner, photos.get(partner.id))
                else:
                    updated = (google_person.changed_groups(values, link.pushed)
                               and account._update_contact(link, values))
                    if link.state == "active" and account._push_photo(
                            link, partner, photos.get(partner.id)):
                        updated = True
                    counts["updated"] += bool(updated)
            except GoogleApiError as e:
                counts["failed"] += 1
                _logger.warning("Google Contacts: partner %s not synced for account %s: %s %s",
                                partner.id, account.id, e.status, e.google_status)
                continue
            wanted = sorted({main} | {tag_labels[tag.id] for tag in partner.category_id
                                      if tag.id in tag_labels})
            if link.state == "active" and wanted != (link.label_resource_names or []):
                label_changes.append((link, wanted))
            self._commit_step()
        account._apply_labels(label_changes)
        # A partner deleted in Odoo: once out of Odoo's labels, its link has nothing left to do.
        account.link_ids.filtered(lambda l: not l.partner_id and (
            l.state in STOPPED or not l.label_resource_names)).unlink()
        return counts

    def _stop_or_resume(self, counts):
        """A link whose partner was archived or deleted in Odoo, or, when the user chose their
        own contacts, is owned by another user now, takes the contact out of Odoo's labels in
        Google and stops syncing. One whose partner came back first takes what changed in Google
        meanwhile. Returns the label changes to make."""
        changes = []
        owner_domain = self._owner_domain() if self.direction != "to_odoo" else []
        links = self.link_ids.filtered(lambda l: l.state in LIVE)
        mine = links.partner_id
        if owner_domain:
            mine = mine.filtered_domain(owner_domain)
        for link in links:
            hold = ("archived" if not link.partner_id.active
                    else "not_mine" if link.partner_id not in mine else False)
            if hold:
                if link.label_resource_names and self.direction != "to_odoo":
                    changes.append((link, []))
                if link.partner_id:
                    link.state = hold
            elif link.state in HELD:
                try:
                    person = self._get_person(link.resource_name)
                except GoogleApiError as e:
                    if e.status != 404:
                        raise
                    link.state = "gone"
                    continue
                link.state = "active"
                self._take_changes(link, person, counts)
                self._commit_step()
        return changes

    def _photo_sums(self, partners):
        """Each partner's image, by the checksum Odoo stores it under."""
        attachments = self.env["ir.attachment"].sudo().search_read(
            [("res_model", "=", "res.partner"), ("res_field", "=", "image_1920"),
             ("res_id", "in", partners.ids)], ["res_id", "checksum"])
        return {a["res_id"]: a["checksum"] for a in attachments}

    def _push_photo(self, link, partner, checksum):
        """Send the partner's photo to Google when it changed in Odoo, or take Google's away when
        Odoo's was removed. Returns whether Google was changed.

        The hash is left as it was: the photo coming back is then compared value by value, which
        finds it unchanged, and a change Google made meanwhile is not mistaken for Odoo's."""
        if (not link.photo_synced or not self._syncs_photo()
                or (checksum or None) == (link.photo_checksum or None)):
            return False
        result = None
        if checksum:
            result = self._google_request(
                "PATCH", link.resource_name + ":updateContactPhoto",
                json={"photoBytes": partner.image_1024.decode(),
                      "personFields": google_person.PERSON_FIELDS,
                      "sources": ["READ_SOURCE_TYPE_CONTACT"]})
        elif link.photo_url:
            result = self._google_request(
                "DELETE", link.resource_name + ":deleteContactPhoto",
                params={"personFields": google_person.PERSON_FIELDS,
                        "sources": "READ_SOURCE_TYPE_CONTACT"})
        person = (result or {}).get("person") or {}
        link.write({"photo_checksum": checksum or False,
                    "photo_url": google_person.photo_url(person) if result else link.photo_url,
                    "etag": person.get("etag") or link.etag})
        return bool(result)

    def _create_contact(self, partner, values, db_uuid):
        person = google_person.new_person(values, google_person.anchor(db_uuid, partner.id))
        for group, row in self._maps().items():
            if values.get(group) is not None:
                field = google_person.MAPPED_FIELDS[row.target]
                person[field] = google_person.merge_mapped(
                    person.get(field, []), row.target, row.google_key, values[group])
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
            "photo_synced": True,
            "synced_at": fields.Datetime.now(),
        })

    def _update_contact(self, link, values, anchor=None, groups=None):
        """Merge the groups that changed in Odoo into the contact as Google holds it now, and
        write Odoo's anchor when given one. A stale etag means Google changed the contact
        meanwhile: read it again and merge again (proof 3).

        Google's answer becomes the hash Odoo's write is known by when it comes back, unless the
        contact had already changed in Google in a way Odoo has not taken yet (Google's list of
        changes runs about 30 s behind). Then no hash is kept, so that change still comes in.
        `groups` limits what is sent; an empty list sends the anchor alone."""
        if groups is None:
            groups = google_person.changed_groups(values, link.pushed)
        maps = self._maps()
        mapped = [group for group in groups if group in maps]
        update_fields = [google_person.GROUPS[group] for group in groups if group not in maps]
        update_fields += [google_person.MAPPED_FIELDS[maps[group].target] for group in mapped]
        update_fields = list(dict.fromkeys(update_fields))
        if anchor:
            update_fields.append("clientData")
        for attempt in range(3):
            try:
                person = self._get_person(link.resource_name)
                known = google_person.person_hash(person) == link.google_hash
                body = google_person.merge(person, values, link.pushed, groups)
                for group in mapped:
                    row = maps[group]
                    field = google_person.MAPPED_FIELDS[row.target]
                    items = body[field] if field in body else person.get(field, [])
                    body[field] = google_person.merge_mapped(items, row.target, row.google_key,
                                                             values.get(group))
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
            "google_hash": google_person.person_hash(result) if known else False,
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
        token = (self.sync_token if self.sync_fields == google_person.PERSON_FIELDS
                 and self.keys_indexed else False)
        try:
            self._pull_pages(token, counts)
        except GoogleApiError as e:
            if not token or not (e.status == 410 or "EXPIRED_SYNC_TOKEN" in e.reasons):
                raise
            self._pull_pages(False, counts)

    def _brings_in(self):
        """Whether Google contacts come into Odoo, and which: "label" or "all"."""
        if self.direction == "to_google":
            return False
        return self._default_choice("import", "label")

    def _pull_pages(self, token, counts):
        label = self._main_label()
        if self._brings_in() == "all":
            label = None
        params = {"personFields": google_person.PERSON_FIELDS,
                  "sources": "READ_SOURCE_TYPE_CONTACT", "pageSize": 1000,
                  "requestSyncToken": "true"}
        if token:
            params["syncToken"] = token
        started = fields.Datetime.now()
        seen, deleted = set(), set()
        waiting = {}  # resource name -> person: under the label, not in Odoo yet
        keys = defaultdict(set)  # email or phone -> the live contacts in this batch holding it
        while True:
            body = self._google_request("GET", "people/me/connections", params=params)
            for person in body.get("connections", []):
                resource = person["resourceName"]
                seen.add(resource)
                if person.get("metadata", {}).get("deleted"):
                    deleted.add(resource)
                    continue
                for key in google_person.person_keys(person):
                    keys[key].add(resource)
                self._step(resource, counts, self._take_person, person, label, counts, waiting)
            if not body.get("nextPageToken"):
                break
            params = dict(params, pageToken=body["nextPageToken"])
        # A full read lists every contact, so a linked one it did not list is gone too.
        if token:
            lost = self.link_ids.filtered(
                lambda l: l.state in LIVE and l.resource_name in deleted)
            dropped = self.candidate_ids.filtered(lambda c: c.resource_name in deleted)
        else:
            lost = self.link_ids.filtered(
                lambda l: l.state in LIVE and l.resource_name not in seen)
            dropped = self.candidate_ids.filtered(lambda c: c.resource_name not in seen)
        dropped.filtered(lambda c: c.kind == "import" and c.state == "pending").unlink()
        for link in lost:
            self._step(link.resource_name, counts, self._lose, link, keys, waiting,
                       not token, counts)
        if self._brings_in():
            for resource, person in list(waiting.items()):
                self._step(resource, counts, self._offer, person, counts)
        self._index_keys(keys, seen, full=not token)
        values = {"sync_token": body.get("nextSyncToken"),
                  "sync_fields": google_person.PERSON_FIELDS}
        if not token:
            values.update(last_full_sync=started, keys_indexed=True)
        self.write(values)
        self._commit_step()

    def _key_hash(self, key):
        db_uuid = self.env["ir.config_parameter"].sudo().get_param("database.uuid")
        return hashlib.sha256(("%s:%s:%s" % (db_uuid, self.id, key)).encode()).hexdigest()[:32]

    def _index_keys(self, keys, seen, full):
        """Record, hashed, which contacts hold which email or phone, for the contacts this pull
        read. A full read replaces the whole index."""
        Key = self.env["twp.google.contacts.key"].sudo()
        old = [("account_id", "=", self.id)]
        if not full:
            old.append(("resource_name", "in", list(seen)))
        Key.search(old).unlink()
        Key.create([{"account_id": self.id, "key": self._key_hash(key), "resource_name": resource}
                    for key, resources in keys.items() for resource in resources])

    def _twin(self, partner, values):
        """A contact this Google already has for the partner, by email or phone, that is not
        linked to another partner: linked instead of creating a second. None when there is none."""
        keys = google_person.contact_keys([values["email"]], [values["phone"]])
        if not keys:
            return None
        found = self.env["twp.google.contacts.key"].sudo().search(
            [("account_id", "=", self.id), ("key", "in", [self._key_hash(k) for k in keys])])
        # Linked already, waiting for the user's review, or kept out of Odoo by the user.
        linked = set(self.link_ids.mapped("resource_name")) | set(self.candidate_ids.filtered(
            lambda c: c.kind == "import" and c.state in ("pending", "skipped")).mapped("resource_name"))
        db_uuid = self.env["ir.config_parameter"].sudo().get_param("database.uuid")
        for resource in sorted(set(found.mapped("resource_name")) - linked):
            try:
                person = self._get_person(resource)
            except GoogleApiError as e:
                if e.status == 404:
                    continue
                raise
            if google_person.anchored_partner_id(person, db_uuid) in (None, partner.id):
                return person
        return None

    def _link_twin(self, partner, person, counts):
        """Link the partner to the contact Google already has. Nothing comes into Odoo from it on
        this first link: it may be a personal contact outside the label. Odoo's values are added
        beside Google's, and from now on it syncs like any other."""
        self.env["twp.google.contacts.candidate"].sudo().search([
            ("account_id", "=", self.id), ("resource_name", "=", person["resourceName"])]).unlink()
        link = self.env["twp.google.contacts.link"].sudo().create({
            "account_id": self.id,
            "partner_id": partner.id,
            "resource_name": person["resourceName"],
            "pushed": {},
            "label_resource_names": sorted(
                google_person.label_resource_names(person) & self._our_labels()),
        })
        self._take_changes(link, person, counts, first=True, fill=False)
        counts["matched"] += 1
        return link

    def _step(self, resource, counts, method, *args):
        """Run one contact's part of a pull on its own, so one bad contact fails alone."""
        try:
            with self.env.cr.savepoint():
                method(*args)
        except (GoogleApiError, AccessError, ValidationError) as e:
            counts["failed"] += 1
            _logger.warning("Google Contacts: %s not taken for account %s: %s",
                            resource, self.id, e)
        self._commit_step()

    def _take_person(self, person, label, counts, waiting):
        """One live contact from Google's list: follow it to its link, or keep it in `waiting`
        to be offered for import once the batch is read."""
        resource = person["resourceName"]
        names = [resource] + person.get("metadata", {}).get("previousResourceNames", [])
        Link = self.env["twp.google.contacts.link"].sudo()
        link = Link.search([("account_id", "=", self.id), ("resource_name", "in", names)],
                           limit=1)
        candidate = self.env["twp.google.contacts.candidate"].sudo().search(
            [("account_id", "=", self.id), ("kind", "=", "import"),
             ("resource_name", "in", names)], limit=1)
        if not link:
            link = self._link_by_anchor(person)
            if link is None:
                return  # Odoo's partner was deleted in Odoo: not brought back from Google
        if link:
            candidate.unlink()
            if link.resource_name != resource:
                link.resource_name = resource  # Google renamed it
            if not link.partner_id:
                return  # deleted in Odoo; the push takes it out of Odoo's labels
            if link.state in STOPPED:
                # Back from Google's bin, or the merge undone.
                link.state = "active"
                if link.archived_by_sync:
                    link.partner_id.with_context(**{FROM_GOOGLE: True}).active = True
                    link.archived_by_sync = False
            if link.state in HELD or not link.partner_id.active:
                return  # not synced for now; the push picks it up again when it comes back
            self._take_changes(link, person, counts, first=not link.pushed)
            return
        if label and not google_person.in_label(person, label):
            candidate.filtered(lambda c: c.state == "pending").unlink()
            return
        if candidate.resource_name and candidate.resource_name != resource:
            candidate.resource_name = resource
        if candidate.state != "skipped":
            waiting[resource] = person

    def _lose(self, link, keys, waiting, full, counts):
        """A linked contact Google no longer has: deleted, or merged into another.

        Google gives a merge as a bare delete of one contact and a change to the other, which took
        its email or phone (proof 6). So a lost contact whose email or phone is now held by
        another contact in the same batch was merged there. When that one is linked too, two Odoo
        partners have become one Google contact, and the pair waits for the user. When it is not
        linked and carries the same name, the link follows it. In a full read, which lists every
        contact whether it changed or not, a shared email or phone proves less, so the user is
        asked instead. A contact merged away is never archived and never sent back to Google.
        """
        if not link.partner_id:
            link.unlink()
            return
        pushed = link.pushed or {}
        held = set().union(*(keys.get(key, set()) for key in google_person.contact_keys(
            [pushed.get("email")], [pushed.get("phone")]))) - {link.resource_name}
        Link = self.env["twp.google.contacts.link"].sudo()
        for resource in sorted(held):
            survivor = Link.search([("account_id", "=", self.id), ("resource_name", "=", resource)],
                                   limit=1)
            if survivor:
                if not survivor.partner_id or survivor.partner_id == link.partner_id:
                    continue
                link.state = "merged"
                self._ask_merge(link, survivor)
                return
            person = waiting.get(resource)
            if person is None:
                continue  # not under the label, or kept out of Odoo
            del waiting[resource]
            name = (person.get("names") or [{}])[0]
            if not full and google_person.same_name(name, pushed.get("name")):
                self.env["twp.google.contacts.candidate"].sudo().search([
                    ("account_id", "=", self.id), ("kind", "=", "import"),
                    ("resource_name", "=", resource)]).unlink()
                link.write({"resource_name": resource, "state": "active", "pushed": {},
                            "google_hash": False, "photo_synced": False,
                            "label_resource_names": sorted(
                                google_person.label_resource_names(person) & self._our_labels())})
                self._take_changes(link, person, counts, first=True)
                counts["followed"] += 1
                return
            link.state = "merged"
            if self._brings_in():
                self._offer(person, counts, match=(link.partner_id, "merged"))
            return
        link.state = "gone"
        counts["gone"] += 1
        self._archive_deleted(link, counts)

    def _ask_merge(self, link, survivor):
        """Ask the user whether Odoo merges the two partners Google made one contact."""
        pushed = link.pushed or {}
        Candidate = self.env["twp.google.contacts.candidate"].sudo()
        values = {
            "kind": "merge",
            "name": link.partner_id.name,
            "email": pushed.get("email"),
            "phone": pushed.get("phone"),
            "company_name": (pushed.get("organization") or {}).get("name"),
            "match_id": link.partner_id.id,
            "match_reason": "merged",
            "survivor_id": survivor.partner_id.id,
            "state": "pending",
        }
        candidate = Candidate.search([("account_id", "=", self.id),
                                      ("resource_name", "=", link.resource_name)])
        if candidate:
            candidate.write(values)
        else:
            Candidate.create(dict(values, account_id=self.id, resource_name=link.resource_name))

    def _archive_deleted(self, link, counts):
        """Archive the partner of a contact deleted in Google, when Settings say so and no other
        user's Google still has it."""
        partner = link.partner_id
        if self.direction == "to_google" or not (
                partner.active and self.env["ir.config_parameter"].sudo().get_param(
                    "twp_google_contacts.archive_deleted")):
            return  # Odoo to Google only: Odoo's contacts are the source, never archived by Google
        if self.env["twp.google.contacts.link"].sudo().search_count([
                ("partner_id", "=", partner.id), ("id", "!=", link.id),
                ("state", "in", LIVE)]):
            return
        try:
            with self.env.cr.savepoint():
                partner.with_user(self.user_id).with_context(**{FROM_GOOGLE: True}).active = False
        except AccessError:
            _logger.info("Google Contacts: partner %s not archived; account %s may not archive",
                         partner.id, self.id)
            return
        link.archived_by_sync = True
        counts["archived"] += 1

    def _our_labels(self):
        return {self.label_resource_name} | set(self.env["twp.google.contacts.label"].sudo().search(
            [("account_id", "=", self.id)]).mapped("resource_name"))

    def _link_by_anchor(self, person):
        """A contact Odoo once sent carries its partner id, which survives a lost link. None when
        that partner was deleted in Odoo, so the contact is not brought back."""
        Link = self.env["twp.google.contacts.link"].sudo()
        db_uuid = self.env["ir.config_parameter"].sudo().get_param("database.uuid")
        partner_id = google_person.anchored_partner_id(person, db_uuid)
        if not partner_id:
            return Link
        partner = self.env["res.partner"].sudo().with_context(active_test=False).browse(
            partner_id).exists()
        if not partner:
            return None
        link = Link.search([("account_id", "=", self.id), ("partner_id", "=", partner.id)])
        if link:
            return link
        return Link.create({
            "account_id": self.id,
            "partner_id": partner.id,
            "resource_name": person["resourceName"],
            "pushed": {},
            "label_resource_names": sorted(
                google_person.label_resource_names(person) & self._our_labels()),
        })

    def _take_changes(self, link, person, counts, first=False, fill=True):
        """Bring the contact's changes into its partner, group by group.

        A group Google changed comes into Odoo. When Odoo changed it too, the Settings rule
        decides: by default the side that changed it last, judged by Google's update time for the
        contact and the time Odoo noted for that group. When Odoo's stands, the push merges it
        into Google. On a first link, Google only fills what the partner lacks; where both have a
        value and they differ, Odoo's is added to Google's list beside Google's own, so neither
        is lost. The photo follows the same rules.

        `fill` False takes nothing on a first link either.

        Odoo to Google only takes nothing: a group Google changed is put back by the push. Google
        to Odoo only takes every group Google changed, and records Google's values as the ones in
        step, since Odoo sends nothing back but its anchor.
        """
        partner = link.partner_id.with_user(self.user_id).with_context(**{FROM_GOOGLE: True})
        labelled = self._take_labels(link, partner, person, fill)
        if not first and link.google_hash == google_person.person_hash(person):
            # Odoo's own write coming back, or a change to something Odoo does not sync.
            if link.etag != person.get("etag"):
                link.etag = person.get("etag")
            counts["changed_from_google"] += labelled
            return
        pushed = link.pushed or {}
        synced = self._synced_groups()
        maps = self._maps()
        google = self._google(person, pushed)
        for group in synced:
            # Google writes some values its own way, such as "IN" for Indiana: not a change.
            if google[group] != pushed.get(group) and self._same_value(
                    group, google[group], pushed.get(group)):
                google[group] = pushed[group]
        odoo = self._values(partner)
        takes = [g for g in synced if g not in maps or maps[g]._takes()]
        if self.direction == "to_google" or not fill:
            take = []
        elif first:
            take = [g for g in takes if odoo[g] is None and google[g] is not None]
        else:
            # A partner keeps its name when Google's is cleared.
            take = [g for g in takes
                    if google[g] != pushed.get(g) and (google[g] is not None or g != "name")
                    and (odoo[g] == pushed.get(g) or self._google_wins(link, partner, g, person))]
        if take:
            partner.write(self._partner_vals(google, take, partner))
        if self._take_photo(link, partner, person, fill) or take or labelled:
            counts["changed_from_google"] += 1
        after = self._values(partner)
        new_pushed = dict(pushed)
        for group in synced:
            same = self._same_value(group, after[group], google[group])
            if group in take or (not first and same):
                new_pushed[group] = after[group]
            elif first:
                new_pushed[group] = after[group] if same else None
            elif google[group] != pushed.get(group) and (
                    odoo[group] != pushed.get(group) or self.direction == "to_google"):
                # Odoo's stands: it takes the place of Google's new value, not a place beside it.
                new_pushed[group] = google[group]
        if self.direction == "to_odoo":
            new_pushed.update({group: google[group] for group in synced})
        link.write({"pushed": new_pushed, "etag": person.get("etag"),
                    "google_hash": google_person.person_hash(person),
                    "synced_at": fields.Datetime.now()})
        if first:
            self._write_anchor(link, self._masked(after, new_pushed))

    def _write_anchor(self, link, values):
        """Write Odoo's anchor on a contact linked from Google, with what Odoo adds to it. Google
        to Odoo only writes the anchor alone."""
        db_uuid = self.env["ir.config_parameter"].sudo().get_param("database.uuid")
        anchor = google_person.anchor(db_uuid, link.partner_id.id)
        if self.direction == "to_odoo":
            self._update_contact(link, link.pushed, anchor, groups=[])
        else:
            self._update_contact(link, values, anchor)

    def _same_value(self, group, a, b):
        maps = self._maps()
        if group in maps:
            return maps[group]._same(a, b)
        if group != "address" or a is None or b is None:
            return google_person.same_value(group, a, b)
        return self._address_vals(a) == self._address_vals(b)

    def _google_wins(self, link, partner, group, person):
        """Whether Google's value stands for a group both sides changed."""
        if self.direction != "both":
            return self.direction == "to_odoo"
        rule = self.env["ir.config_parameter"].sudo().get_param(
            "twp_google_contacts.conflict") or "latest"
        if rule != "latest":
            return rule == "google"
        google_time = google_person.update_time(person)
        stamp = (link.odoo_changed or {}).get(group)
        odoo_time = fields.Datetime.to_datetime(stamp) if stamp else partner.write_date
        return bool(google_time) and (not odoo_time or google_time > odoo_time)

    def _take_photo(self, link, partner, person, fill=True):
        """Bring Google's photo into the partner when it changed in Google, or, the first time
        the two are compared, when the partner has none. Returns whether the partner changed.

        The first comparison keeps both photos where both have one, and leaves a photo only Odoo
        has for the push to send."""
        if not self._syncs_photo():
            return False
        url = google_person.photo_url(person)
        checksum = self._photo_sums(partner).get(partner.id)
        if self.direction == "to_google":
            if not link.photo_synced:
                link.write({"photo_synced": True, "photo_url": url or False,
                            "photo_checksum": url and checksum or False})
            elif url != (link.photo_url or None):
                # Changed in Google: the push puts Odoo's photo back, or takes Google's away.
                link.write({"photo_url": url or False, "photo_checksum": PUT_BACK})
            return False
        if not link.photo_synced:
            if not url or checksum or not fill:
                link.write({"photo_synced": True, "photo_url": url or False,
                            "photo_checksum": url and checksum or False})
                return False
        elif url == (link.photo_url or None) or (
                (checksum or None) != (link.photo_checksum or None)
                and not self._google_wins(link, partner, "photo", person)):
            return False
        data = self._download_photo(url) if url else False
        if data is None:
            return False  # Google did not hand it over; the next change to the contact retries
        changed = False
        try:
            with self.env.cr.savepoint():
                partner.image_1920 = data
            changed = bool(data or checksum)
        except UserError as e:
            _logger.warning("Google Contacts: photo of %s not taken for account %s: %s",
                            link.resource_name, self.id, e)
        link.write({"photo_synced": True, "photo_url": url or False,
                    "photo_checksum": self._photo_sums(partner).get(partner.id) or False})
        return changed

    def _download_photo(self, url):
        try:
            response = requests.request("GET", google_person.photo_download_url(url),
                                        timeout=TIMEOUT)
        except requests.RequestException as e:
            _logger.info("Google Contacts: photo not downloaded: %s", type(e).__name__)
            return None
        if response.status_code != 200 or not response.content:
            _logger.info("Google Contacts: photo not downloaded: HTTP %s", response.status_code)
            return None
        return base64.b64encode(response.content)

    def _offer(self, person, counts, match=None):
        """A Google contact under the label that Odoo does not know. Before the first review, and
        whenever it looks like a partner Odoo has, it waits for the user; otherwise it is added.
        `match` is the partner and reason when the caller already knows them."""
        candidate = self.env["twp.google.contacts.candidate"].sudo().search(
            [("account_id", "=", self.id), ("kind", "=", "import"),
             ("resource_name", "=", person["resourceName"])])
        if candidate.state == "skipped":
            return
        match, reason = match or self._match(person)
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
        if not candidate.match_id or reason == "merged":
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
        google = self._google(person)
        synced = self._synced_groups()
        maps = self._maps()
        values = self._partner_vals(google, [g for g in synced
                                             if g not in maps or maps[g]._takes()])
        values["name"] = values.get("name") or self._display_name(google)
        values["user_id"] = self.user_id.id  # Tyler's decision, 2026-10-11
        # Labels Odoo made from tags become those tags again, as do labels whose changes come in.
        Label = self.env["twp.google.contacts.label"].sudo()
        in_google = google_person.label_resource_names(person)
        labels = Label.search([("account_id", "=", self.id),
                               ("resource_name", "in", list(in_google))])
        incoming = self._labels_in()
        tags = labels.category_id | self.env["res.partner.category"].union(*(
            self._tag_for(incoming[r][0]) for r in in_google if r in incoming))
        values["category_id"] = [Command.set(tags.ids)]
        partner = self.env["res.partner"].with_user(self.user_id).create(values)
        after = self._values(partner)
        if self.direction == "to_odoo":
            pushed = {group: google[group] if group in synced else None for group in after}
        else:
            pushed = {group: after[group] if group in synced and self._same_value(
                group, after[group], google[group]) else None for group in after}
        link = self.env["twp.google.contacts.link"].sudo().create({
            "account_id": self.id,
            "partner_id": partner.id,
            "resource_name": person["resourceName"],
            "etag": person.get("etag"),
            "pushed": pushed,
            "google_hash": google_person.person_hash(person),
            "label_resource_names": sorted({self.label_resource_name}
                                           | set(labels.mapped("resource_name"))
                                           | {r for r in in_google
                                              if r in incoming and incoming[r][1]}),
            "google_labels": sorted(in_google & set(self._label_names())),
            "synced_at": fields.Datetime.now(),
        })
        self._take_photo(link, partner.with_context(**{FROM_GOOGLE: True}), person)
        self._write_anchor(link, self._masked(after, pushed))
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
            if candidate.kind == "merge":
                if decision == "skip":
                    # Not a merge after all: the contact was deleted in Google.
                    link = Link.search([("account_id", "=", self.id),
                                        ("resource_name", "=", candidate.resource_name),
                                        ("state", "=", "merged")])
                    if link:
                        link.state = "gone"
                        self._archive_deleted(link, Counter())
                    candidate.state = "skipped"
                continue
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
                link = Link.search([("account_id", "=", self.id),
                                    ("partner_id", "=", partner.id)])
                if link.state in LIVE:
                    raise UserError(self.env._(
                        "%(name)s is already linked to another Google contact.",
                        name=partner.display_name))
                values = {"resource_name": person["resourceName"], "state": "active",
                          "pushed": {}, "google_hash": False, "photo_synced": False,
                          "archived_by_sync": False,
                          "label_resource_names": [self.label_resource_name]}
                if link:
                    # Its own Google contact is gone, merged into this one or deleted.
                    link.write(values)
                else:
                    link = Link.create(dict(values, account_id=self.id, partner_id=partner.id))
                self._take_changes(link, person, Counter(), first=True)
            else:
                self._create_from_google(person)
            candidate.state = "done"
            self._commit_step()
        if not self.candidate_ids.filtered(lambda c: c.state == "pending" and c.kind == "import"):
            self.import_reviewed = True

    def _partner_vals(self, google, groups, partner=None):
        """Partner field values from Google's, for the given groups. A mapped value this Odoo
        field cannot hold, such as a choice Odoo does not have, is left in Google and not
        taken."""
        vals = {}
        maps = self._maps()
        partner = partner or self.env["res.partner"].with_user(self.user_id)
        for group in groups:
            if group in maps:
                try:
                    vals[maps[group].field_id.name] = maps[group]._odoo_write_value(
                        partner, google[group])
                except Unreadable:
                    _logger.info("Google Contacts: a value for %s was not taken for account %s",
                                 maps[group].field_id.name, self.id)
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

    def _tags_out(self, tags, scope_tags):
        """The tags Odoo sends to Google as labels, with each one's label name: per its rule, or
        the Settings default for tags with none. In tagged mode the scope tags only choose who
        syncs; they are not labels unless a rule says so."""
        rules = self._tag_rules()
        default = self._default_choice("tags_default", "label")
        names = {}
        for tag in tags:
            rule = rules.get(tag.id)
            if rule:
                if rule.direction in ("both", "to_google"):
                    names[tag] = rule.label_name
            elif default == "label" and tag not in scope_tags:
                names[tag] = tag.display_name
        return names

    def _tag_rules(self):
        return {rule.category_id.id: rule
                for rule in self.env["twp.google.contacts.tag.rule"].sudo().search([])}

    def _tag_labels(self, names):
        """Map each tag id to its Google label, found by name or created."""
        Label = self.env["twp.google.contacts.label"].sudo()
        tags = self.env["res.partner.category"].union(*names)
        known = {label.category_id: label for label in Label.search(
            [("account_id", "=", self.id), ("category_id", "in", tags.ids)])}
        result = {}
        for tag, name in names.items():
            label = known.get(tag)
            if label and (label.name or label.category_id.name) == name:
                result[tag.id] = label.resource_name
                continue
            resource = self._find_or_create_label(name)
            values = {"resource_name": resource, "name": name}
            if label:
                label.write(values)
            else:
                Label.create(dict(values, account_id=self.id, category_id=tag.id))
            result[tag.id] = resource
        return result

    def _label_names(self):
        """The labels the user made in Google, resource name to name, read once per sync."""
        cache = self.env.cr.cache.setdefault("twp_google_contacts_labels", {})
        if self.id not in cache:
            groups = self._google_request("GET", "contactGroups", params={
                "pageSize": 1000, "groupFields": "name,groupType"}).get("contactGroups", [])
            cache[self.id] = {group["resourceName"]: group.get("name") for group in groups
                              if group.get("groupType") == "USER_CONTACT_GROUP"}
        return cache[self.id]

    def _find_or_create_label(self, name):
        labels = self._label_names()
        for resource, label_name in labels.items():
            if label_name == name:
                return resource
        created = self._google_request("POST", "contactGroups",
                                       json={"contactGroup": {"name": name}})
        labels[created["resourceName"]] = name
        return created["resourceName"]

    def _labels_in(self):
        """The labels whose changes in Google come into Odoo's tags: resource name to (the tag,
        or the name of the tag to find or make, and whether Odoo also sends that tag as this
        label). Per each tag's rule, or the Settings defaults for labels with none."""
        if self.direction == "to_google":
            return {}
        rules = self._tag_rules()
        by_label = {rule.label_name: rule for rule in rules.values()}
        labels_default = self._default_choice("labels_default", "ignore")
        tags_default = self._default_choice("tags_default", "label")
        Label = self.env["twp.google.contacts.label"].sudo()
        made = {label.resource_name: label.category_id for label in Label.search(
            [("account_id", "=", self.id)]) if label.category_id.id not in rules}
        result = {}
        for resource, name in self._label_names().items():
            if resource == self.label_resource_name:
                continue
            rule = by_label.get(name)
            if rule:
                if rule.direction in ("both", "to_odoo"):
                    result[resource] = (rule.category_id, rule.direction == "both")
            elif labels_default == "tag":
                tag = made.get(resource) or name
                if not (isinstance(tag, str) and self._tag_for(tag, create=False).id in rules):
                    result[resource] = (tag, tags_default == "label")
        return result

    def _tag_for(self, tag, create=True):
        """The tag itself, or the tag of that name, made when Odoo has none and `create`."""
        if not isinstance(tag, str):
            return tag
        Tag = self.env["res.partner.category"].sudo()
        found = Tag.search([("name", "=ilike", escape_psql(tag))], limit=1)
        if not found and create:
            found = Tag.create({"name": tag})
        return found

    def _take_labels(self, link, partner, person, fill=True):
        """Bring the user's label changes in Google into the partner's tags, where a rule or the
        Settings default lets them in. Returns whether the tags changed.

        The labels the contact was in at the last sync tell what was added or removed. The first
        time, there is no last sync, so labels only add tags. A contact linked without filling
        (one Google already had) only has its labels noted."""
        names = self._label_names()
        now = google_person.label_resource_names(person) & set(names)
        before = set(link.google_labels or [])
        incoming = self._labels_in() if fill else {}
        add, remove = self.env["res.partner.category"], self.env["res.partner.category"]
        managed = set(link.label_resource_names or [])
        for resource in now - before:
            if resource in incoming:
                add |= self._tag_for(incoming[resource][0])
                if incoming[resource][1]:
                    managed.add(resource)
        for resource in (before - now) if link.google_labels else ():
            if resource in incoming:
                remove |= self._tag_for(incoming[resource][0], create=False)
                managed.discard(resource)
        add -= partner.category_id
        remove &= partner.category_id
        if add or remove:
            partner.write({"category_id": [Command.link(t.id) for t in add]
                           + [Command.unlink(t.id) for t in remove]})
        values = {"google_labels": sorted(now), "label_resource_names": sorted(managed)}
        if any(link[field] != value for field, value in values.items()):
            link.write(values)
        return bool(add or remove)

    @api.model
    def _labels_changed(self):
        """Tag rules or label defaults changed: every account reads all of Google again, and
        each contact's labels count as new, so labels that now bring tags do."""
        accounts = self.sudo().search([])
        accounts.write({"sync_token": False})
        self.env["twp.google.contacts.link"].sudo().search(
            [("account_id", "in", accounts.ids)]).write({"google_labels": False})

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
            done = (current ^ wanted) - failed
            link.label_resource_names = sorted(
                (current & wanted) | ((wanted - current) - failed) | ((current - wanted) & failed))
            if done:
                # Google's labels now include Odoo's change, so it is not read back as the user's.
                google = set(link.google_labels or [])
                link.google_labels = sorted((google | (wanted & done)) - (current & done))
        self._commit_step()

    def _commit_step(self):
        self.env["ir.cron"]._commit_progress(processed=1)

    def _describe(self, counts):
        _ = self.env._
        parts = [
            (counts["added_from_google"], _("%(n)s added from Google")),
            (counts["changed_from_google"], _("%(n)s updated from Google")),
            (counts["created"], _("%(n)s sent to Google")),
            (counts["matched"], _("%(n)s matched to contacts already in Google")),
            (counts["updated"], _("%(n)s updated in Google")),
            (counts["followed"], _("%(n)s followed into a merge in Google")),
            (counts["archived"], _("%(n)s archived after a delete in Google")),
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
