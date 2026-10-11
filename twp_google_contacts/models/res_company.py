from odoo import fields, models


class ResCompany(models.Model):
    _inherit = "res.company"

    twp_google_contacts_tag_ids = fields.Many2many(
        "res.partner.category", string="Google Contacts tags",
        help="When only tagged contacts sync, the tags that send a contact to Google.")
