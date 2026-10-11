from odoo import fields, models

from .broker import DEFAULT_URL

DEFAULT_LABEL = "Odoo"

DIRECTIONS = [("both", "Both ways"),
              ("to_google", "Odoo to Google only"),
              ("to_odoo", "Google to Odoo only")]


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
    twp_google_contacts_direction = fields.Selection(
        DIRECTIONS,
        string="Sync direction",
        config_parameter="twp_google_contacts.direction",
        default="both",
        help="Where each user starts when they connect. Each user can change their own on the "
             "Google Contacts tab of their preferences. Odoo to Google only: Google keeps a copy "
             "of Odoo's contacts, and a synced detail edited in Google is put back. Google to "
             "Odoo only: Odoo keeps a copy of the Google contacts, and sends nothing to Google.")
    twp_google_contacts_import = fields.Selection(
        [("label", "Only those under the label"),
         ("all", "All of each user's contacts")],
        string="Google contacts brought into Odoo",
        config_parameter="twp_google_contacts.import",
        default="label",
        help="All: every contact in a user's Google comes into Odoo, where every Odoo user can "
             "see it. The label is how a user keeps personal contacts out of Odoo.")
    twp_google_contacts_conflict = fields.Selection(
        [("latest", "The most recent change"),
         ("odoo", "Odoo's"),
         ("google", "Google's")],
        string="When both changed, keep",
        config_parameter="twp_google_contacts.conflict",
        default="latest",
        help="When a contact's email, phone, address or other detail changed in both Odoo and "
             "Google since the last sync, which one stays.")
    twp_google_contacts_archive_deleted = fields.Boolean(
        string="Archive in Odoo when deleted in Google",
        config_parameter="twp_google_contacts.archive_deleted",
        help="Off, a contact deleted in Google stays in Odoo and only stops syncing. A contact "
             "Google merged into another is never archived; it waits for review.")
    twp_google_contacts_test_database = fields.Boolean(
        string="Sync from this copy",
        config_parameter="twp_google_contacts.test_database",
        help="This database is a copy. Turn this on only when every connected Google account is a "
             "test account, because a copy claims the same contacts as the database it came "
             "from.")
