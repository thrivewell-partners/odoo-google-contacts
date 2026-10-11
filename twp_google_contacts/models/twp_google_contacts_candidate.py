from odoo import api, fields, models
from odoo.exceptions import UserError


class TwpGoogleContactsCandidate(models.Model):
    """A Google contact under the label that is not linked to Odoo yet, waiting for its user to
    say what it is: a partner Odoo already has, a new one, or neither.

    The first import of every account comes here in full, because linking a Google contact to the
    wrong partner merges two people. After that, only contacts that look like a partner Odoo
    already has come here; the rest are added to Odoo as they arrive.
    """

    _name = "twp.google.contacts.candidate"
    _description = "Google contact waiting for review"
    _order = "state, name, id"

    account_id = fields.Many2one("twp.google.contacts.account", required=True, ondelete="cascade",
                                 index=True)
    user_id = fields.Many2one(related="account_id.user_id", store=True, index=True)
    resource_name = fields.Char(required=True, readonly=True)
    name = fields.Char(string="Name in Google", readonly=True)
    email = fields.Char(string="Email in Google", readonly=True)
    phone = fields.Char(string="Phone in Google", readonly=True)
    company_name = fields.Char(string="Company in Google", readonly=True)
    match_id = fields.Many2one(
        "res.partner", string="Odoo contact",
        domain=[("is_company", "=", False)],
        help="The Odoo contact this Google contact is. Leave it empty to add a new contact.")
    match_reason = fields.Selection(
        [("email", "Same email"), ("phone", "Same phone")], string="Found by", readonly=True)
    state = fields.Selection(
        [("pending", "To review"), ("done", "Done"), ("skipped", "Not in Odoo")],
        default="pending", required=True, readonly=True)

    _account_resource_unique = models.Constraint(
        "UNIQUE(account_id, resource_name)", "A Google contact is reviewed once per account.")

    def action_link(self):
        """Link each to the chosen Odoo contact, or add it to Odoo when none is chosen."""
        candidates = self or self.search([("state", "=", "pending"),
                                          ("user_id", "=", self.env.uid)])
        for account, group in candidates._by_account():
            account._review(group.filtered(lambda c: c.state == "pending"), "link")
        return self._reload()

    def action_create(self):
        """Add each to Odoo as a new contact, whatever match was found."""
        for account, group in self._by_account():
            account._review(group.filtered(lambda c: c.state == "pending"), "create")
        return self._reload()

    def action_skip(self):
        """Keep each out of Odoo. It is not asked about again."""
        for account, group in self._by_account():
            account._review(group.filtered(lambda c: c.state == "pending"), "skip")
        return self._reload()

    def _by_account(self):
        """The candidates grouped by account, once the caller is shown to own them."""
        for candidate in self:  # reading it at all goes through the record rules
            if candidate.user_id != self.env.user and not self.env.is_admin():
                raise UserError(self.env._("Only %(name)s can review their Google contacts.",
                                           name=candidate.user_id.name))
        candidates = self.sudo()
        for account in candidates.account_id:
            yield account, candidates.filtered(lambda c: c.account_id == account)

    @api.model
    def _reload(self):
        return {"type": "ir.actions.client", "tag": "soft_reload"}
