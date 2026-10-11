from odoo import fields, models


class TwpGoogleContactsLabel(models.Model):
    """One Odoo tag as one Google label, in one connected account."""

    _name = "twp.google.contacts.label"
    _description = "Odoo tag synced to a Google label"

    account_id = fields.Many2one("twp.google.contacts.account", required=True, ondelete="cascade",
                                 index=True)
    category_id = fields.Many2one("res.partner.category", required=True, ondelete="cascade")
    resource_name = fields.Char(required=True)
    name = fields.Char(help="The label's name when Odoo found or made it. Empty on labels made "
                            "before M5, which were named after the tag")

    _account_category_unique = models.Constraint(
        "UNIQUE(account_id, category_id)", "A tag has one label per Google account.")
