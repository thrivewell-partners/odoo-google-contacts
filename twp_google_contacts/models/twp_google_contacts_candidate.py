from odoo import api, fields, models
from odoo.exceptions import UserError


class TwpGoogleContactsCandidate(models.Model):
    """A Google contact under the label that is not linked to Odoo yet, waiting for its user to
    say what it is: a partner Odoo already has, a new one, or neither.

    The first import of every account comes here in full, because linking a Google contact to the
    wrong partner merges two people. After that, only contacts that look like a partner Odoo
    already has come here; the rest are added to Odoo as they arrive.

    A contact Google merged into another linked one comes here too, as a "merge" row: two Odoo
    partners are now one Google contact, and only the user can say whether Odoo merges them.
    """

    _name = "twp.google.contacts.candidate"
    _description = "Google contact waiting for review"
    _order = "state, name, id"

    account_id = fields.Many2one("twp.google.contacts.account", required=True, ondelete="cascade",
                                 index=True)
    user_id = fields.Many2one(related="account_id.user_id", store=True, index=True)
    kind = fields.Selection(
        [("import", "New in Google"), ("merge", "Merged in Google")],
        default="import", required=True, readonly=True)
    resource_name = fields.Char(required=True, readonly=True,
                                help="The Google contact, or for a merge, the one Google removed")
    name = fields.Char(string="Name in Google", readonly=True)
    email = fields.Char(string="Email in Google", readonly=True)
    phone = fields.Char(string="Phone in Google", readonly=True)
    company_name = fields.Char(string="Company in Google", readonly=True)
    match_id = fields.Many2one(
        "res.partner", string="Odoo contact",
        domain=[("is_company", "=", False)],
        help="The Odoo contact this Google contact is. Leave it empty to add a new contact.")
    match_reason = fields.Selection(
        [("email", "Same email"), ("phone", "Same phone"), ("merged", "Merged in Google")],
        string="Found by", readonly=True)
    survivor_id = fields.Many2one(
        "res.partner", string="Merged into", readonly=True, ondelete="cascade",
        help="The Odoo contact linked to the Google contact Google kept")
    state = fields.Selection(
        [("pending", "To review"), ("done", "Done"), ("skipped", "Not in Odoo")],
        default="pending", required=True, readonly=True)

    _account_resource_unique = models.Constraint(
        "UNIQUE(account_id, resource_name)", "A Google contact is reviewed once per account.")

    def action_link(self):
        """Link each to the chosen Odoo contact, or add it to Odoo when none is chosen."""
        candidates = (self or self.search([("state", "=", "pending"),
                                           ("user_id", "=", self.env.uid)]))
        # A merge is not accepted in bulk: each one is a merge in Odoo, made in Odoo's own tool.
        candidates = candidates.filtered(lambda c: c.kind == "import")
        for account, group in candidates._by_account():
            account._review(group.filtered(lambda c: c.state == "pending"), "link")
        return self._reload()

    def action_create(self):
        """Add each to Odoo as a new contact, whatever match was found."""
        for account, group in self._by_account():
            account._review(group.filtered(lambda c: c.state == "pending"), "create")
        return self._reload()

    def action_merge(self):
        """Open Odoo's merge for the two partners Google made one."""
        self.ensure_one()
        for _account, candidate in self._by_account():
            if candidate.kind != "merge" or not candidate.match_id or not candidate.survivor_id:
                raise UserError(self.env._("There is nothing left to merge."))
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Merge"),
            "res_model": "base.partner.merge.automatic.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"active_model": "res.partner",
                        "active_ids": [self.survivor_id.id, self.match_id.id]},
        }

    def action_skip(self):
        """Keep each out of Odoo, or for a merge, keep both partners. It is not asked about
        again."""
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
