from odoo import models


class TwpGoogleContactsAccount(models.Model):
    _inherit = "twp.google.contacts.account"

    def _note(self, partner, message):
        """A note in the partner's chatter, written as the user whose Google it came from."""
        partner.sudo().message_post(body=message, subtype_xmlid="mail.mt_note",
                                    author_id=self.user_id.partner_id.id)
