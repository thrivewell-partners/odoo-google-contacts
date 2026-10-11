from odoo import fields, models


class TwpGoogleContactsKey(models.Model):
    """An email or phone a Google contact holds, hashed, so that Odoo can find a contact Google
    already has before creating another. Kept from what each sync reads; the contact details
    themselves are not stored."""

    _name = "twp.google.contacts.key"
    _description = "Google contact email or phone, hashed"

    account_id = fields.Many2one("twp.google.contacts.account", required=True, ondelete="cascade",
                                 index=True)
    key = fields.Char(required=True, index=True)
    resource_name = fields.Char(required=True, index=True)
