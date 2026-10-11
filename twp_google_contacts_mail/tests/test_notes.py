from odoo.tests import TransactionCase, tagged

from odoo.addons.twp_google_contacts.tests.common import SyncCase


@tagged("post_install", "-at_install")
class TestNotes(SyncCase, TransactionCase):

    def notes(self, partner):
        return partner.message_ids.filtered(
            lambda m: m.subtype_id == self.env.ref("mail.mt_note")
            and "Google Contacts" in (m.body or "")).mapped("body")

    def test_a_change_from_google_is_noted(self):
        self.sync()
        link = self.link(self.ada)
        self.google.change(link.resource_name,
                           emailAddresses=[{"value": "ada@new.example"}],
                           organizations=[{"title": "Countess"}])
        self.sync()
        notes = self.notes(self.ada)
        self.assertEqual(len(notes), 1)
        self.assertIn("company and job title, email", notes[0])
        self.assertIn("test@example.com", notes[0])
        message = self.ada.message_ids[:1]
        self.assertEqual(message.author_id, self.env.user.partner_id)

    def test_a_change_from_odoo_is_not_noted(self):
        self.sync()
        self.ada.email = "ada@odoo.example"
        self.sync()
        self.assertFalse(self.notes(self.ada))

    def test_a_contact_added_from_google_is_noted(self):
        self.account.sudo().import_reviewed = True
        self.grace()
        self.sync()
        grace = self.account.link_ids.partner_id.filtered(lambda p: p.name == "Grace Hopper")
        self.assertEqual(len(self.notes(grace)), 1)
        self.assertIn("Added to Odoo", self.notes(grace)[0])

    def test_an_archive_after_a_delete_in_google_is_noted(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "twp_google_contacts.archive_deleted", True)
        self.sync()
        self.google.delete(self.link(self.ada).resource_name)
        self.sync()
        self.assertFalse(self.ada.active)
        self.assertIn("Archived", self.notes(self.ada)[0])
