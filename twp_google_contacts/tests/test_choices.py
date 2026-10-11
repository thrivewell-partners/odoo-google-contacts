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
        deleted = self.Partner.create({"name": "Deleted Since"})
        anchored = self.google.add(
            emailAddresses=[{"value": "ada@example.com"}],
            clientData=[{"key": "thrivewell.anchor",
                         "value": "odoo:%s:res.partner:%s" % (db_uuid, deleted.id)}])
        deleted.unlink()
        self.sync()
        self.assertNotEqual(self.link(self.ada).resource_name, anchored)
        self.assertEqual(len(self.google.people), 3)

    def test_a_contact_kept_out_of_odoo_is_not_a_twin(self):
        kept_out = self.google.add(labels=["contactGroups/odoo"],
                                   names=[{"givenName": "Ada", "familyName": "Elsewhere"}],
                                   emailAddresses=[{"value": "ada@example.com"}])
        self.sync()
        self.assertFalse(self.link(self.ada))  # waits for the review, which matched it
        self.pending().action_skip()
        self.sync()
        self.assertNotEqual(self.link(self.ada).resource_name, kept_out)


@tagged("post_install", "-at_install")
class TestDetails(SyncCase, TransactionCase):
    """M5: Settings leave chosen details out of the sync."""

    def leave_out(self, *details, on=True):
        settings = self.env["res.config.settings"].create({})
        for detail in details:
            settings["twp_google_contacts_sync_" + detail] = not on
        settings.execute()

    def turn_on(self, *details):
        self.leave_out(*details, on=False)

    def test_settings_show_every_detail_on_by_default(self):
        settings = self.env["res.config.settings"].create({})
        self.assertTrue(settings.twp_google_contacts_sync_note)
        self.leave_out("note", "photo")
        settings = self.env["res.config.settings"].create({})
        self.assertFalse(settings.twp_google_contacts_sync_note)
        self.assertTrue(settings.twp_google_contacts_sync_email)

    def test_a_detail_left_out_is_neither_sent_nor_taken(self):
        self.leave_out("note", "photo")
        self.ada.write({"comment": "<p>Private to Odoo</p>", "image_1920": png("green")})
        self.sync()
        resource = self.link(self.ada).resource_name
        self.assertNotIn("biographies", self.google.people[resource])
        self.assertNotIn(resource, self.google.photos)

        self.google.change(resource, biographies=[{"value": "Google's note"}],
                           emailAddresses=[{"value": "ada@google-side.example"}])
        self.sync()
        self.assertEqual(self.ada.email, "ada@google-side.example")
        self.assertIn("Private to Odoo", self.ada.comment)

    def test_a_detail_turned_back_on_is_sent(self):
        self.leave_out("note")
        self.ada.comment = "<p>Now shared</p>"
        self.sync()
        resource = self.link(self.ada).resource_name
        self.turn_on("note")
        self.sync()
        self.assertEqual(self.google.people[resource]["biographies"][0]["value"], "Now shared")

    def test_a_contact_brought_in_takes_only_what_syncs(self):
        self.leave_out("phone")
        self.account.sudo().import_reviewed = True
        self.grace()
        self.sync()
        grace = self.Partner.search([("name", "=", "Grace Hopper")])
        self.assertEqual(grace.email, "grace@navy.example")
        self.assertFalse(grace.phone)


@tagged("post_install", "-at_install")
class TestTagsAndLabels(SyncCase, TransactionCase):
    """M5: rules pair tags with labels and say which way they sync."""

    def setUp(self):
        super().setUp()
        self.params = self.env["ir.config_parameter"].sudo()
        self.Tag = self.env["res.partner.category"]
        self.Rule = self.env["twp.google.contacts.tag.rule"]
        self.vip = self.Tag.create({"name": "VIP"})

    def members(self, name):
        return self.google.members.get(self.google.label(name), set())

    def test_a_rule_both_ways_follows_google_label_changes(self):
        self.Rule.create({"category_id": self.vip.id, "label_name": "Key accounts"})
        self.ada.category_id |= self.vip
        self.sync()
        resource = self.link(self.ada).resource_name
        self.assertIn(resource, self.members("Key accounts"))
        self.assertFalse(any(g["name"] == "VIP" for g in self.google.groups.values()))
        self.sync()
        self.assertEqual(self.account.last_sync_result, "Nothing to change")

        self.google.relabel(resource, remove=[self.google.label("Key accounts")])
        self.sync()
        self.assertNotIn(self.vip, self.ada.category_id)
        self.assertNotIn(resource, self.members("Key accounts"))  # not put back

        self.google.relabel(resource, add=[self.google.label("Key accounts")])
        self.sync()
        self.assertIn(self.vip, self.ada.category_id)

    def test_a_rule_google_to_odoo_only_sends_nothing(self):
        friends = self.Tag.create({"name": "Friends"})
        self.Rule.create({"category_id": friends.id, "label_name": "Family",
                          "direction": "to_odoo"})
        self.ada.category_id |= friends
        self.sync()
        resource = self.link(self.ada).resource_name
        self.assertNotIn(resource, self.members("Family"))
        self.google.relabel(resource, add=["contactGroups/family"])
        self.sync()
        self.assertIn(friends, self.ada.category_id)
        self.ada.category_id -= friends
        self.sync()
        self.assertIn(resource, self.members("Family"))  # Odoo never takes it out

    def test_a_rule_odoo_to_google_only_ignores_google(self):
        self.Rule.create({"category_id": self.vip.id, "label_name": "Key accounts",
                          "direction": "to_google"})
        self.ada.category_id |= self.vip
        self.sync()
        resource = self.link(self.ada).resource_name
        self.google.relabel(resource, remove=[self.google.label("Key accounts")])
        self.sync()
        self.assertIn(self.vip, self.ada.category_id)

    def test_tags_with_no_rule_can_stay_out_of_google(self):
        self.params.set_param("twp_google_contacts.tags_default", "none")
        self.ada.category_id |= self.vip
        self.sync()
        self.assertFalse(any(g["name"] == "VIP" for g in self.google.groups.values()))

    def test_a_sub_tag_is_sent_by_its_full_name(self):
        gold = self.Tag.create({"name": "Gold", "parent_id": self.Tag.create({"name": "Customer"}).id})
        self.ada.category_id |= gold
        self.sync()
        self.assertIn(self.link(self.ada).resource_name, self.members("Customer / Gold"))

    def test_google_labels_can_become_tags(self):
        self.sync()
        resource = self.link(self.ada).resource_name
        settings = self.env["res.config.settings"].create({})
        settings.twp_google_contacts_labels_default = "tag"
        settings.execute()
        self.assertFalse(self.account.sudo().sync_token)

        self.google.relabel(resource, add=["contactGroups/family"])
        self.sync()
        family = self.Tag.search([("name", "=", "Family")])
        self.assertIn(family, self.ada.category_id)
        self.assertNotIn(self.account.label_resource_name, self.ada.category_id.mapped("name"))

        self.google.relabel(resource, remove=["contactGroups/family"])
        self.sync()
        self.assertNotIn(family, self.ada.category_id)

    def test_labels_already_there_become_tags_when_the_default_changes(self):
        self.sync()
        resource = self.link(self.ada).resource_name
        self.google.relabel(resource, add=["contactGroups/family"])
        self.sync()
        self.assertFalse(self.Tag.search([("name", "=", "Family")]))
        settings = self.env["res.config.settings"].create({})
        settings.twp_google_contacts_labels_default = "tag"
        settings.execute()
        self.sync()
        self.assertIn("Family", self.ada.category_id.mapped("name"))

    def test_a_contact_brought_in_gets_the_tag_of_its_label(self):
        self.Rule.create({"category_id": self.vip.id, "label_name": "Key accounts"})
        self.google.groups["contactGroups/key"] = {"name": "Key accounts",
                                                   "groupType": "USER_CONTACT_GROUP"}
        self.account.sudo().import_reviewed = True
        self.google.add(labels=["contactGroups/odoo", "contactGroups/key"],
                        names=[{"givenName": "Grace", "familyName": "Hopper"}])
        self.sync()
        grace = self.Partner.search([("name", "=", "Grace Hopper")])
        self.assertEqual(grace.category_id, self.vip)
