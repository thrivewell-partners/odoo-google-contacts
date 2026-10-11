import base64
from datetime import timedelta

from odoo import fields
from odoo.tests import TransactionCase, new_test_user, tagged

from .common import SyncCase, png


@tagged("post_install", "-at_install")
class TestDirection(SyncCase, TransactionCase):
    """M5: each user chooses which way their contacts sync."""

    def setUp(self):
        super().setUp()
        self.params = self.env["ir.config_parameter"].sudo()
        self.later = fields.Datetime.now() + timedelta(hours=1)

    def direction(self, value):
        self.env.user.google_contacts_direction = value
        self.assertEqual(self.account.direction, value)

    def writes_to(self, resource):
        return [(method, path) for method, path in self.google.writes if path.startswith(resource)]

    # The choice.

    def test_each_user_starts_from_settings_and_changes_their_own(self):
        self.assertEqual(self.account.direction, "both")
        self.params.set_param("twp_google_contacts.direction", "to_google")
        other = new_test_user(self.env, "second.user", groups="base.group_user")
        self.assertEqual(self.connect(other).direction, "to_google")
        self.assertEqual(self.account.direction, "both")

        self.sync()
        self.assertTrue(self.account.sudo().sync_token)
        self.direction("to_odoo")
        self.assertEqual(self.env.user.google_contacts_direction, "to_odoo")
        self.assertFalse(self.account.sudo().sync_token)  # the next pull reads everything

    # Odoo to Google only.

    def test_odoo_to_google_brings_nothing_in(self):
        self.direction("to_google")
        self.account.sudo().import_reviewed = True
        self.grace()
        self.sync()
        self.assertFalse(self.Partner.search([("name", "=", "Grace Hopper")]))
        self.assertFalse(self.account.candidate_ids)
        self.assertTrue(self.link(self.ada))  # and Odoo's contacts still go to Google

    def test_odoo_to_google_puts_back_what_google_changed(self):
        self.direction("to_google")
        self.sync()
        resource = self.link(self.ada).resource_name
        self.google.change(resource, at=self.later,
                           emailAddresses=[{"value": "ada@google-side.example"}],
                           names=[{"givenName": "Countess"}])
        self.sync()
        self.assertEqual(self.ada.email, "ada@example.com")
        self.assertEqual(self.ada.name, "Ada Lovelace")
        self.assertEqual(self.google.emails(resource), ["ada@example.com"])
        self.assertEqual(self.google.people[resource]["names"][0]["familyName"], "Lovelace")
        self.sync()
        self.assertEqual(self.account.last_sync_result, "Nothing to change")

    def test_odoo_to_google_puts_back_the_photo(self):
        self.direction("to_google")
        self.sync()
        resource = self.link(self.ada).resource_name
        self.google.set_photo(resource, png("blue"))
        self.sync()
        self.assertFalse(self.ada.image_1920)
        self.assertNotIn(resource, self.google.photos)

        self.ada.image_1920 = png("green")
        self.sync()
        self.google.set_photo(resource, png("blue"))
        self.sync()
        self.assertEqual(self.google.photo_bytes[self.google.photos[resource]],
                         base64.b64decode(self.ada.image_1024))

    def test_odoo_to_google_never_archives_after_a_delete(self):
        self.direction("to_google")
        self.params.set_param("twp_google_contacts.archive_deleted", True)
        self.sync()
        link = self.link(self.ada)
        self.google.delete(link.resource_name)
        self.sync()
        self.assertEqual(link.state, "gone")
        self.assertTrue(self.ada.active)

    # Google to Odoo only.

    def test_google_to_odoo_sends_only_the_anchor(self):
        self.direction("to_odoo")
        self.account.sudo().import_reviewed = True
        resource = self.grace()
        self.sync()
        grace = self.Partner.search([("name", "=", "Grace Hopper")])
        self.assertTrue(grace)
        self.assertFalse(self.link(self.ada))  # Odoo's own contacts are not sent
        self.assertNotIn(("POST", "people:createContact"), self.google.writes)
        self.assertEqual(self.writes_to(resource), [("PATCH", resource + ":updateContact")])
        self.assertTrue(self.google.people[resource]["clientData"])
        self.assertEqual(self.google.emails(resource), ["grace@navy.example"])

    def test_google_to_odoo_never_sends_odoos_changes_and_googles_win(self):
        self.direction("to_odoo")
        self.account.sudo().import_reviewed = True
        resource = self.grace()
        self.sync()
        grace = self.Partner.search([("name", "=", "Grace Hopper")])
        writes = len(self.google.writes)

        grace.write({"email": "grace@odoo-side.example", "function": "Admiral",
                     "image_1920": png("green")})
        self.sync()
        self.assertEqual(len(self.google.writes), writes)
        self.assertEqual(self.google.emails(resource), ["grace@navy.example"])

        # Google's later change wins even where Odoo changed the same detail after it.
        self.google.change(resource, at=fields.Datetime.now() - timedelta(hours=1),
                           emailAddresses=[{"value": "grace@google-side.example"}])
        grace.email = "grace@odoo-again.example"
        self.sync()
        self.assertEqual(grace.email, "grace@google-side.example")
        self.assertEqual(grace.function, "Admiral")  # Odoo's edit stands until Google changes it
        self.assertEqual(len(self.google.writes), writes)

    def test_google_to_odoo_leaves_labels_alone_on_archive(self):
        self.sync()
        link = self.link(self.ada)
        self.direction("to_odoo")
        self.sync()
        writes = len(self.google.writes)
        self.ada.active = False
        self.sync()
        self.assertEqual(link.state, "archived")
        self.assertEqual(len(self.google.writes), writes)

    def test_all_contacts_can_come_in(self):
        self.google.add(names=[{"givenName": "Unlabelled", "familyName": "Friend"}])
        self.sync()
        self.assertFalse(self.account.candidate_ids)
        self.params.set_param("twp_google_contacts.import", "all")
        self.account.sudo().sync_token = False
        self.sync()
        self.assertEqual(self.pending().mapped("name"), ["Unlabelled Friend"])

    def test_back_to_both_ways_sends_what_odoo_changed_meanwhile(self):
        self.sync()
        resource = self.link(self.ada).resource_name
        self.direction("to_odoo")
        self.sync()
        self.ada.email = "ada@changed-while-one-way.example"
        self.sync()
        self.assertEqual(self.google.emails(resource), ["ada@example.com"])
        self.direction("both")
        self.sync()
        self.assertEqual(self.google.emails(resource), ["ada@changed-while-one-way.example"])


@tagged("post_install", "-at_install")
class TestOwnership(SyncCase, TransactionCase):
    """M5: whose contacts go to whose Google, and a contact Google already has."""

    def setUp(self):
        super().setUp()
        self.other = new_test_user(self.env, "owner.other", groups="base.group_user")
        self.mine = self.Partner.create({"name": "Mine Person", "email": "mine@example.com",
                                         "user_id": self.env.uid, "category_id": self.scope.ids})

    def owners(self, value):
        self.env.user.google_contacts_owners = value
        self.assertEqual(self.account.owners, value)

    def label(self):
        return self.google.members[self.account.label_resource_name]

    def test_only_my_contacts(self):
        self.ada.user_id = self.other
        self.owners("mine")
        self.sync()
        self.assertTrue(self.link(self.mine))
        self.assertFalse(self.link(self.ada))

    def test_mine_and_unowned(self):
        theirs = self.Partner.create({"name": "Their Person", "email": "theirs@example.com",
                                      "user_id": self.other.id, "category_id": self.scope.ids})
        self.owners("mine_unowned")
        self.sync()
        self.assertTrue(self.link(self.mine))
        self.assertTrue(self.link(self.ada))
        self.assertFalse(self.link(theirs))

    def test_a_person_takes_their_companys_owner(self):
        self.engines.user_id = self.env.user
        self.ada.user_id = False
        self.ada.parent_id = self.engines
        self.owners("mine")
        self.sync()
        self.assertTrue(self.link(self.ada))

    def test_a_contact_handed_to_another_owner_leaves_my_labels_and_comes_back(self):
        self.owners("mine")
        self.sync()
        link = self.link(self.mine)
        resource = link.resource_name
        self.assertIn(resource, self.label())

        self.mine.user_id = self.other
        self.sync()
        self.assertEqual(link.state, "not_mine")
        self.assertNotIn(resource, self.label())
        self.assertIn(resource, self.google.people)  # never deleted in Google

        self.google.change(resource, emailAddresses=[{"value": "mine@while-away.example"}])
        self.sync()
        self.assertEqual(self.mine.email, "mine@example.com")  # not synced while it is theirs

        self.mine.user_id = self.env.user
        self.sync()
        self.assertEqual(link.state, "active")
        self.assertIn(resource, self.label())
        self.assertEqual(self.mine.email, "mine@while-away.example")

    def test_a_contact_brought_in_is_owned_by_the_importing_user(self):
        self.account.sudo().import_reviewed = True
        self.grace()
        self.sync()
        grace = self.Partner.search([("name", "=", "Grace Hopper")])
        self.assertEqual(grace.user_id, self.env.user)

    # A contact Google already has.

    def test_a_contact_google_already_has_is_linked_not_duplicated(self):
        twin = self.google.add(
            names=[{"givenName": "Ada", "familyName": "L."}],
            emailAddresses=[{"value": "ADA@example.com", "type": "home"}],
            addresses=[{"streetAddress": "1 Private Lane", "city": "Carmel", "type": "home"}])
        self.sync()
        link = self.link(self.ada)
        self.assertEqual(link.resource_name, twin)
        self.assertEqual(len(self.google.people), 2)  # the twin and Mine Person, no second Ada
        self.assertIn(twin, self.label())
        self.assertTrue(self.google.people[twin]["clientData"])
        self.assertFalse(self.ada.street)  # nothing from outside the label came into Odoo
        phones = [p["value"] for p in self.google.people[twin]["phoneNumbers"]]
        self.assertEqual(phones, ["(317) 555-0140"])
        self.assertIn("1 matched to contacts already in Google", self.account.last_sync_result)

    def test_a_contact_anchored_to_another_partner_is_not_a_twin(self):
        db_uuid = self.env["ir.config_parameter"].sudo().get_param("database.uuid")
        self.google.add(emailAddresses=[{"value": "ada@example.com"}],
                        clientData=[{"key": "thrivewell.anchor",
                                     "value": "odoo:%s:res.partner:%s" % (db_uuid, self.mine.id)}])
        self.sync()
        self.assertTrue(self.link(self.ada))
        self.assertEqual(len(self.google.people), 3)
