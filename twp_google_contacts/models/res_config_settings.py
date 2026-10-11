from odoo import fields, models

from .broker import DEFAULT_URL


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
