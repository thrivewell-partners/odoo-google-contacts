from odoo import fields, models
from odoo.exceptions import AccessError

from .res_config_settings import DIRECTIONS, OWNERS

# Each user's own choices about their sync, kept on their connected account.
CHOICE_FIELDS = {"google_contacts_direction": "direction", "google_contacts_owners": "owners"}
USER_FIELDS = ["google_contacts_state", "google_contacts_email", "google_contacts_review_count",
               *CHOICE_FIELDS]


class ResUsers(models.Model):
    _inherit = "res.users"

    google_contacts_state = fields.Selection(
        [("none", "Not connected"),
         ("connected", "Connected"),
         ("reconnect", "Needs reconnecting"),
         ("blocked", "Blocked by Workspace admin")],
        string="Google Contacts", compute="_compute_google_contacts")
    google_contacts_email = fields.Char(
        string="Google account", compute="_compute_google_contacts")
    google_contacts_review_count = fields.Integer(
        string="Google contacts to review", compute="_compute_google_contacts")
    google_contacts_direction = fields.Selection(
        DIRECTIONS, string="Sync direction", compute="_compute_google_contacts",
        inverse="_inverse_google_contacts_choices",
        help="Odoo to Google only: your Google keeps a copy of Odoo's contacts, and a detail "
             "edited in Google is put back. Google to Odoo only: Odoo keeps a copy of your Google "
             "contacts and sends nothing to Google. Changing it compares both sides afresh on the "
             "next sync.")
    google_contacts_owners = fields.Selection(
        [("all", "Everyone in scope"),
         ("mine", "Only contacts I own"),
         ("mine_unowned", "Mine, and contacts with no owner")],
        string="Contacts sent to my Google", compute="_compute_google_contacts",
        inverse="_inverse_google_contacts_choices",
        help="A contact's owner is its Salesperson. A contact that stops being yours leaves "
             "Odoo's labels in your Google and is never deleted there.")

    @property
    def SELF_READABLE_FIELDS(self):
        return super().SELF_READABLE_FIELDS + USER_FIELDS

    @property
    def SELF_WRITEABLE_FIELDS(self):
        return super().SELF_WRITEABLE_FIELDS + list(CHOICE_FIELDS)

    def _compute_google_contacts(self):
        accounts = self.env["twp.google.contacts.account"].sudo().search(
            [("user_id", "in", self.ids)])
        by_user = {a.user_id.id: a for a in accounts}
        for user in self:
            account = by_user.get(user.id)
            user.google_contacts_state = account.state if account else "none"
            user.google_contacts_email = account.google_email if account else False
            user.google_contacts_review_count = account.review_count if account else 0
            for name, account_field in CHOICE_FIELDS.items():
                user[name] = account[account_field] if account else False

    def _inverse_google_contacts_choices(self):
        for user in self:
            account = user._google_contacts_account()
            values = {account_field: user[name] for name, account_field in CHOICE_FIELDS.items()
                      if user[name] and user[name] != account[account_field]}
            if account and values:
                account.write(values)

    def _google_contacts_account(self):
        """The user's own account. Only the user, or an administrator, may act on it."""
        self.ensure_one()
        if self != self.env.user and not self.env.is_admin():
            raise AccessError(self.env._("Only %(name)s can change their Google connection.",
                                name=self.name))
        return self.env["twp.google.contacts.account"].sudo().search(
            [("user_id", "=", self.id)])

    def action_google_contacts_connect(self):
        self.ensure_one()
        if self != self.env.user:
            raise AccessError(self.env._("Each user connects their own Google account."))
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Connect Google Contacts"),
            "res_model": "twp.google.contacts.connect",
            "view_mode": "form",
            "target": "new",
        }

    def action_google_contacts_check(self):
        account = self._google_contacts_account()
        if not account:
            return {"type": "ir.actions.client", "tag": "soft_reload"}
        return account.action_check()

    def action_google_contacts_sync(self):
        account = self._google_contacts_account()
        if not account:
            return {"type": "ir.actions.client", "tag": "soft_reload"}
        return account.action_sync_now()

    def action_google_contacts_review(self):
        account = self._google_contacts_account()
        if not account:
            return {"type": "ir.actions.client", "tag": "soft_reload"}
        return account.action_review()

    def action_google_contacts_disconnect(self):
        account = self._google_contacts_account()
        if account:
            account._disconnect()
        return {"type": "ir.actions.client", "tag": "soft_reload"}
