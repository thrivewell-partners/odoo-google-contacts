from odoo import fields, models
from odoo.exceptions import AccessError

USER_FIELDS = ["google_contacts_state", "google_contacts_email"]


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

    @property
    def SELF_READABLE_FIELDS(self):
        return super().SELF_READABLE_FIELDS + USER_FIELDS

    def _compute_google_contacts(self):
        accounts = self.env["twp.google.contacts.account"].sudo().search(
            [("user_id", "in", self.ids)])
        by_user = {a.user_id.id: a for a in accounts}
        for user in self:
            account = by_user.get(user.id)
            user.google_contacts_state = account.state if account else "none"
            user.google_contacts_email = account.google_email if account else False

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

    def action_google_contacts_disconnect(self):
        account = self._google_contacts_account()
        if account:
            account._disconnect()
        return {"type": "ir.actions.client", "tag": "soft_reload"}
