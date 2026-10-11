import copy
from datetime import timedelta

from odoo import fields
from odoo.tests import TransactionCase, tagged

from .common import SyncCase, png


@tagged("post_install", "-at_install")
class TestTwoWay(SyncCase, TransactionCase):
    """M4: which side wins, deletes, Google's merges, and photos."""

    def setUp(self):
        super().setUp()
        self.params = self.env["ir.config_parameter"].sudo()
        now = fields.Datetime.now()
        self.earlier, self.later = now - timedelta(hours=1), now + timedelta(hours=1)

    def linked(self, partner=None):
        self.sync()
        return self.link(partner or self.ada)

    def label(self):
        return self.google.members[self.account.label_resource_name]

    # Both sides changed.

    def test_the_later_change_wins_each_way(self):
        resource = self.linked().resource_name
        self.ada.email = "ada@odoo-side.example"
        self.google.change(resource, at=self.later,
                           emailAddresses=[{"value": "ada@google-side.example"}])
        self.sync()
        self.assertEqual(self.ada.email, "ada@google-side.example")

        self.google.change(resource, at=self.earlier,
                           phoneNumbers=[{"value": "+1 317 555 0199", "type": "mobile"}])
        self.ada.phone = "(317) 555-0177"
        self.sync()
        self.assertEqual(self.ada.phone, "(317) 555-0177")
        phones = [p["value"] for p in self.google.people[resource]["phoneNumbers"]]
        self.assertEqual(phones, ["(317) 555-0177"])

    def test_a_group_only_google_changed_comes_in_whatever_its_time(self):
        resource = self.linked().resource_name
        self.ada.function = "Analyst"
        self.google.change(resource, at=self.earlier,
                           emailAddresses=[{"value": "ada@google-side.example"}])
        self.sync()
        self.assertEqual(self.ada.email, "ada@google-side.example")
        self.assertEqual(self.google.people[resource]["organizations"][0]["title"], "Analyst")

    def test_settings_can_keep_one_side_always(self):
        resource = self.linked().resource_name
        self.params.set_param("twp_google_contacts.conflict", "odoo")
        self.ada.email = "ada@odoo-side.example"
        self.google.change(resource, at=self.later,
                           emailAddresses=[{"value": "ada@google-side.example"}])
        self.sync()
        self.assertEqual(self.ada.email, "ada@odoo-side.example")

        self.params.set_param("twp_google_contacts.conflict", "google")
        self.google.change(resource, at=self.earlier,
                           emailAddresses=[{"value": "ada@google-again.example"}])
        self.ada.email = "ada@odoo-again.example"
        self.sync()
        self.assertEqual(self.ada.email, "ada@google-again.example")

    def test_a_change_the_sync_makes_is_not_counted_as_odoos(self):
        resource = self.linked().resource_name
        self.google.change(resource, emailAddresses=[{"value": "ada@google-side.example"}])
        self.sync()
        self.assertNotIn("email", self.link(self.ada).odoo_changed or {})
        self.ada.phone = "(317) 555-0177"
        self.assertIn("phone", self.link(self.ada).odoo_changed)

    # Deletes.

    def test_with_the_setting_on_a_delete_archives_and_a_restore_brings_back(self):
        self.params.set_param("twp_google_contacts.archive_deleted", True)
        link = self.linked()
        resource = link.resource_name
        kept = copy.deepcopy(self.google.people[resource])
        self.google.delete(resource)
        result = self.sync()
        self.assertEqual(link.state, "gone")
        self.assertFalse(self.ada.active)
        self.assertIn("1 archived after a delete in Google", result["message"])

        self.google.restore(resource, kept)
        self.sync()
        self.assertEqual(link.state, "active")
        self.assertTrue(self.ada.active)

    def test_archived_in_odoo_leaves_the_label_and_picks_up_on_return(self):
        link = self.linked()
        resource = link.resource_name
        self.ada.active = False
        self.sync()
        self.assertEqual(link.state, "archived")
        self.assertIn(resource, self.google.people)  # never deleted in Google
        self.assertNotIn(resource, self.label())

        self.google.change(resource, emailAddresses=[{"value": "ada@while-away.example"}])
        self.sync()
        self.assertEqual(self.ada.email, "ada@example.com")

        self.ada.active = True
        self.sync()
        self.assertEqual(link.state, "active")
        self.assertEqual(self.ada.email, "ada@while-away.example")
        self.assertIn(resource, self.label())

    def test_deleted_in_odoo_leaves_the_label_and_does_not_come_back(self):
        resource = self.linked().resource_name
        self.ada.unlink()
        self.sync()
        self.assertIn(resource, self.google.people)
        self.assertNotIn(resource, self.label())
        self.assertFalse(self.account.link_ids)
        # Put back under the label in Google, and read in full: Odoo does not re-create it.
        self.label().add(resource)
        self.account.sudo().write({"sync_token": False, "import_reviewed": True})
        self.sync()
        self.assertFalse(self.Partner.search([("email", "=", "ada@example.com")]))
        self.assertFalse(self.pending())

    # Google's merges (proof 6).

    def test_two_linked_contacts_merged_in_google_wait_for_review(self):
        self.params.set_param("twp_google_contacts.archive_deleted", True)
        king = self.Partner.create({"name": "Ada King", "email": "ada.king@example.com",
                                    "category_id": self.scope.ids})
        lost = self.linked()
        kept = self.link(king)
        self.google.merge(kept.resource_name, lost.resource_name)
        self.sync()
        self.assertEqual(lost.state, "merged")
        self.assertTrue(self.ada.active)
        self.assertEqual(len(self.google.people), 1)  # not sent back to Google
        row = self.pending()
        self.assertEqual((row.kind, row.match_id, row.survivor_id), ("merge", self.ada, king))
        self.assertTrue(self.env.user.google_contacts_review_count)

        action = row.action_merge()
        wizard = self.env[action["res_model"]].with_context(action["context"]).create({})
        self.assertEqual(wizard.partner_ids, self.ada | king)
        wizard._merge((self.ada | king).ids, king)
        self.assertEqual(row.state, "done")
        self.assertEqual(self.account.link_ids, kept)
        self.assertEqual(kept.partner_id, king)

    def test_not_a_merge_is_a_delete(self):
        king = self.Partner.create({"name": "Ada King", "email": "ada.king@example.com",
                                    "category_id": self.scope.ids})
        lost = self.linked()
        self.google.merge(self.link(king).resource_name, lost.resource_name)
        self.sync()
        self.pending().action_skip()
        self.assertEqual(lost.state, "gone")
        self.assertTrue(self.ada.active)  # the setting is off

    def test_merged_into_its_unlinked_duplicate_the_link_follows(self):
        link = self.linked()
        duplicate = self.google.add(labels=["contactGroups/odoo"],
                                    names=[{"givenName": "Ada", "familyName": "Lovelace"}],
                                    emailAddresses=[{"value": "ada@home.example"}])
        self.sync()
        self.assertEqual(self.pending().name, "Ada Lovelace")
        self.google.merge(duplicate, link.resource_name)
        result = self.sync()
        self.assertEqual((link.resource_name, link.state), (duplicate, "active"))
        self.assertFalse(self.pending())
        self.assertEqual(self.Partner.search_count([("name", "=", "Ada Lovelace")]), 1)
        self.assertIn("1 followed into a merge in Google", result["message"])
        anchors = [c["value"] for c in self.google.people[duplicate]["clientData"]]
        self.assertTrue(anchors[0].endswith(":res.partner:%s" % self.ada.id))

    def test_in_a_full_read_following_a_merge_is_asked_first(self):
        link = self.linked()
        duplicate = self.google.add(labels=["contactGroups/odoo"],
                                    names=[{"givenName": "Ada", "familyName": "Lovelace"}],
                                    emailAddresses=[{"value": "ada@home.example"}])
        self.sync()
        self.google.merge(duplicate, link.resource_name)
        self.google.expire_tokens = True
        self.sync()
        self.assertEqual(link.state, "merged")
        row = self.pending()
        self.assertEqual((row.resource_name, row.match_id, row.match_reason),
                         (duplicate, self.ada, "merged"))
        row.action_link()
        self.assertEqual((link.resource_name, link.state), (duplicate, "active"))

    # Photos.

    def test_a_photo_added_in_google_comes_into_odoo_once(self):
        resource = self.linked().resource_name
        self.assertFalse(self.ada.image_1920)  # Google's initials are not a photo
        self.google.set_photo(resource, png("red"))
        result = self.sync()
        self.assertTrue(self.ada.image_1920)
        self.assertIn("1 updated from Google", result["message"])
        self.google.writes.clear()
        self.assertEqual(self.sync()["message"], "Nothing to change")
        self.assertEqual(self.google.writes, [])

    def test_a_photo_in_odoo_goes_to_google_and_its_removal_follows(self):
        self.ada.image_1920 = png("blue")
        resource = self.linked().resource_name
        self.assertIn(resource, self.google.photos)
        self.google.writes.clear()
        self.sync()
        self.assertEqual(self.google.writes, [])  # the upload coming back is not taken again
        self.ada.image_1920 = False
        self.sync()
        self.assertNotIn(resource, self.google.photos)

    def test_the_first_comparison_keeps_both_photos(self):
        resource = self.linked().resource_name
        link = self.link(self.ada)
        link.photo_synced = False  # as a link made before photos synced
        self.google.set_photo(resource, png("red"))
        self.ada.image_1920 = png("blue")
        before = self.ada.image_1920
        self.sync()
        self.assertEqual(self.ada.image_1920, before)
        self.assertTrue(link.photo_synced)
