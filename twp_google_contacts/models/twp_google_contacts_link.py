from odoo import fields, models


class TwpGoogleContactsLink(models.Model):
    """One Odoo partner as one Google contact, in one connected account.

    A partner can sit in several users' Google accounts, each with its own resource name and etag,
    so this is a table of its own rather than a field on the partner.
    """

    _name = "twp.google.contacts.link"
    _description = "Odoo contact synced to a Google contact"

    account_id = fields.Many2one("twp.google.contacts.account", required=True, ondelete="cascade",
                                 index=True)
    partner_id = fields.Many2one("res.partner", required=True, ondelete="cascade", index=True)
    resource_name = fields.Char(required=True, index=True)
    etag = fields.Char()
    pushed = fields.Json(help="The partner values Odoo last sent, which say which item in each of "
                              "Google's lists is Odoo's")
    google_hash = fields.Char(help="A hash of the synced fields as Google held them after Odoo's "
                                   "last write, to recognise that write when it comes back")
    label_resource_names = fields.Json(help="The labels Odoo last put the contact in")
    synced_at = fields.Datetime()
    state = fields.Selection(
        [("active", "Synced"), ("gone", "Deleted in Google")], default="active", required=True)

    _account_partner_unique = models.Constraint(
        "UNIQUE(account_id, partner_id)", "A partner is linked once per Google account.")
