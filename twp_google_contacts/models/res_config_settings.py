from odoo import fields, models

from .broker import DEFAULT_URL

DEFAULT_LABEL = "Odoo"


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    twp_google_contacts_broker_key = fields.Char(
        string="ThriveWell Connect key",
        config_parameter="twp_google_contacts.broker_key",
        help="Issued by ThriveWell for this database. It lets Odoo ask for Google sign-ins.")
    twp_google_contacts_broker_url = fields.Char(
        string="ThriveWell Connect address",
        config_parameter="twp_google_contacts.broker_url",
        default=DEFAULT_URL)
    twp_google_contacts_label = fields.Char(
        string="Google label",
        config_parameter="twp_google_contacts.label",
        default=DEFAULT_LABEL,
        help="Every contact Odoo sends to Google goes under this label.")
    twp_google_contacts_scope = fields.Selection(
        [("contactable", "People with an email or a phone"),
         ("tagged", "Only contacts with these tags")],
        string="Contacts sent to Google",
        config_parameter="twp_google_contacts.scope",
        default="contactable")
    twp_google_contacts_tag_ids = fields.Many2many(
        related="company_id.twp_google_contacts_tag_ids", readonly=False)
    twp_google_contacts_test_database = fields.Boolean(
        string="Sync from this copy",
        config_parameter="twp_google_contacts.test_database",
        help="This database is a copy. Turn this on only when every connected Google account is a "
             "test account, because a copy claims the same contacts as the database it came "
             "from.")
