import secrets

from odoo import _, fields, models
from odoo.exceptions import UserError
from odoo.http import request

SESSION_KEY = "twp_google_contacts_state"
RETURN_PATH = "/twp_google_contacts/return"


class TwpGoogleContactsConnect(models.TransientModel):
    """The screen before Connect: what the connection does, and the user's agreement to it."""

    _name = "twp.google.contacts.connect"
    _description = "Connect Google Contacts"

    consent = fields.Boolean(string="I agree")

    def action_continue(self):
        self.ensure_one()
        if not self.consent:
            raise UserError(_("Tick “I agree” to continue to Google."))
        if not request:
            raise UserError(_("Connect from Odoo in a web browser."))
        # The state ties Google's answer to this browser session, so a sign-in started by someone
        # else cannot be finished into this user's account.
        state = secrets.token_urlsafe(32)
        request.session[SESSION_KEY] = state
        return_url = request.httprequest.url_root.rstrip("/") + RETURN_PATH
        url = self.env["twp.google.contacts.account"]._connect_start(return_url, state)
        return {"type": "ir.actions.act_url", "url": url, "target": "self"}
