from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, new_test_user, tagged

from .common import GOOGLE, FakePeople, GoogleContactsCase

ACCOUNT = "odoo.addons.twp_google_contacts.models.twp_google_contacts_account"


@tagged("post_install", "-at_install")
class TestPull(GoogleContactsCase, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        params = cls.env["ir.config_parameter"].sudo()
        params.set_param("twp_google_contacts.test_database", True)
        # Only tagged partners go to Google, so the database's other partners stay out of the way.
        params.set_param("twp_google_contacts.scope", "tagged")
        cls.scope = cls.env["res.partner.category"].create({"name": "To Google"})
        cls.env.user.company_id.twp_google_contacts_tag_ids = cls.scope
        cls.Partner = cls.env["res.partner"]
        cls.Candidate = cls.env["twp.google.contacts.candidate"]
        cls.engines = cls.Partner.create({"name": "Lovelace Engines", "is_company": True})
        cls.ada = cls.Partner.create({"name": "Ada Lovelace", "email": "ada@example.com",
                                      "phone": "(317) 555-0140", "category_id": cls.scope.ids})

    def setUp(self):
        super().setUp()
        self.google = FakePeople()
        self.google.groups["contactGroups/odoo"] = {"name": "Odoo",
                                                     "groupType": "USER_CONTACT_GROUP"}
        for target, value in ((GOOGLE, self.google),
                              (ACCOUNT + ".TwpGoogleContactsAccount._commit_step", lambda s: None)):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.account = self.connect()

    def sync(self):
        return self.account.action_sync_now()["params"]

    def link(self, partner):
        return self.account.link_ids.filtered(lambda l: l.partner_id == partner)

    def pending(self):
        return self.account.candidate_ids.filtered(lambda c: c.state == "pending")

    def grace(self, **extra):
        """A contact the user keeps under the Odoo label in Google."""
        fields = {"names": [{"givenName": "Grace", "familyName": "Hopper"}],
                  "emailAddresses": [{"value": "grace@navy.example", "type": "work"}],
                  "phoneNumbers": [{"value": "+1 317 555 0150", "type": "mobile"}]}
        fields.update(extra)
        return self.google.add(labels=["contactGroups/odoo"], **fields)

    # The first import.

    def test_first_import_waits_for_review(self):
        self.grace()
        self.google.add(names=[{"givenName": "Not", "familyName": "Labelled"}])
        result = self.sync()
        self.assertEqual(self.pending().mapped("name"), ["Grace Hopper"])
        self.assertFalse(self.Partner.search([("email", "=", "grace@navy.example")]))
        self.assertIn("1 waiting for your review", result["message"])
        self.assertEqual(self.env.user.google_contacts_review_count, 1)

    def test_a_match_is_found_by_email_then_by_phone(self):
        self.grace()
        self.google.add(labels=["contactGroups/odoo"],
                        names=[{"givenName": "Ada"}],
                        phoneNumbers=[{"value": "+1 317-555-0140"}])
        hopper = self.Partner.create({"name": "G. Hopper", "email": "GRACE@navy.example"})
        self.sync()
        by_name = {c.name: c for c in self.pending()}
        self.assertEqual((by_name["Grace Hopper"].match_id, by_name["Grace Hopper"].match_reason),
                         (hopper, "email"))
        self.assertEqual((by_name["Ada"].match_id, by_name["Ada"].match_reason),
                         (self.ada, "phone"))

    def test_accepting_a_match_links_and_fills_only_gaps(self):
        resource = self.grace(organizations=[{"name": "Lovelace Engines", "title": "Admiral"}])
        hopper = self.Partner.create({"name": "Grace B. Hopper", "email": "grace@navy.example"})
        self.sync()
        self.pending().action_link()
        link = self.link(hopper)
        self.assertEqual(link.resource_name, resource)
        # Google filled what Odoo lacked; Odoo's own name stands.
        self.assertEqual((hopper.phone, hopper.function, hopper.parent_id),
                         ("+1 317 555 0150", "Admiral", self.engines))
        self.assertEqual(hopper.name, "Grace B. Hopper")
        person = self.google.people[resource]
        self.assertEqual(person["names"][0]["givenName"], "Grace B.")
        uuid = self.env["ir.config_parameter"].sudo().get_param("database.uuid")
        self.assertIn({"key": "thrivewell.anchor",
                       "value": "odoo:%s:res.partner:%s" % (uuid, hopper.id)},
                      person["clientData"])
        self.assertTrue(self.account.import_reviewed)

    def test_accepting_with_no_match_adds_the_contact(self):
        vip = self.env["res.partner.category"].create({"name": "VIP"})
        self.env["twp.google.contacts.label"].sudo().create({
            "account_id": self.account.id, "category_id": vip.id,
            "resource_name": "contactGroups/family"})
        resource = self.grace(
            organizations=[{"name": "Unknown Navy", "title": "Admiral"}],
            addresses=[{"streetAddress": "1 Pier Rd", "city": "Arlington", "region": "Virginia",
                        "postalCode": "22201", "countryCode": "US", "type": "work"}],
            urls=[{"value": "navy.example"}],
            biographies=[{"value": "Wrote the first compiler.", "contentType": "TEXT_PLAIN"}])
        self.google.members["contactGroups/family"] = {resource}
        self.sync()
        self.Candidate.action_link()  # Accept, with nothing selected: every one to review
        partner = self.link(self.Partner.search([("email", "=", "grace@navy.example")])).partner_id
        self.assertEqual(partner.name, "Grace Hopper")
        self.assertEqual((partner.function, partner.parent_id.id), ("Admiral", False))
        self.assertEqual((partner.street, partner.city, partner.zip, partner.state_id.code,
                          partner.country_id.code), ("1 Pier Rd", "Arlington", "22201", "VA", "US"))
        self.assertIn("first compiler", partner.comment)
        self.assertEqual(partner.category_id, vip | self.scope)
        self.assertEqual(self.google.people[resource]["clientData"][0]["key"], "thrivewell.anchor")
        # Nothing more goes back: the next sync makes no writes.
        self.google.writes.clear()
        self.sync()
        self.assertEqual(self.google.writes, [])

    def test_kept_out_is_not_asked_again(self):
        self.grace()
        self.sync()
        self.pending().action_skip()
        self.sync()
        self.assertFalse(self.pending())
        self.assertTrue(self.account.import_reviewed)

    def test_after_the_review_new_contacts_come_in_unless_they_look_known(self):
        self.account.sudo().import_reviewed = True
        self.grace()
        self.google.add(labels=["contactGroups/odoo"], names=[{"givenName": "Ada", "familyName": "L"}],
                        emailAddresses=[{"value": "ada@example.com"}])
        result = self.sync()
        self.assertTrue(self.Partner.search([("email", "=", "grace@navy.example")]))
        self.assertEqual(self.pending().match_id, self.ada)
        self.assertIn("1 added from Google", result["message"])

    def test_only_the_owner_sees_their_review(self):
        self.grace()
        self.sync()
        other = new_test_user(self.env, "reviewer2", groups="base.group_user")
        self.assertFalse(self.Candidate.with_user(other).search([]))
        # Refused by the record rules, or by the owner check when the row is already in cache.
        with self.assertRaises((AccessError, UserError)):
            self.pending().with_user(other).action_link()
        self.assertEqual(len(self.pending()), 1)

    # Changes made in Google.

    def test_a_change_in_google_comes_into_odoo_and_is_not_sent_back(self):
        self.sync()
        resource = self.link(self.ada).resource_name
        self.google.change(resource, emailAddresses=[{"value": "ada@engines.example"}],
                           phoneNumbers=[{"value": "+1 (317) 555-0140", "type": "mobile"}])
        self.google.writes.clear()
        result = self.sync()
        self.assertEqual(self.ada.email, "ada@engines.example")
        # The same number, written differently, is not a change.
        self.assertEqual(self.ada.phone, "(317) 555-0140")
        self.assertIn("1 updated from Google", result["message"])
        self.assertEqual(self.google.writes, [])

    def test_when_both_changed_odoos_change_stands(self):
        self.sync()
        resource = self.link(self.ada).resource_name
        self.google.change(resource, emailAddresses=[{"value": "ada@google-side.example"}])
        self.ada.email = "ada@odoo-side.example"
        self.sync()
        self.assertEqual(self.ada.email, "ada@odoo-side.example")
        self.assertEqual(self.google.emails(resource)[0], "ada@odoo-side.example")

    def test_odoos_own_write_coming_back_is_ignored(self):
        self.sync()
        self.ada.phone = "(317) 555-0141"
        self.sync()
        written = self.ada.write_date
        result = self.sync()
        self.assertEqual(self.ada.write_date, written)
        self.assertEqual(result["message"], "Nothing to change")

    def test_deleted_in_google_stops_syncing_and_archives_nothing(self):
        self.sync()
        link = self.link(self.ada)
        self.google.delete(link.resource_name)
        self.sync()
        self.assertEqual(link.state, "gone")
        self.assertTrue(self.ada.active)

    def test_a_renamed_contact_keeps_its_link(self):
        self.sync()
        link = self.link(self.ada)
        new = self.google.rename(link.resource_name)
        self.sync()
        self.assertEqual((link.resource_name, link.state), (new, "active"))

    def test_pages_are_followed(self):
        self.google.page_size = 2
        for n in range(5):
            self.google.add(labels=["contactGroups/odoo"], names=[{"givenName": "P%s" % n}])
        self.sync()
        self.assertEqual(len(self.pending()), 5)

    # Sync tokens.

    def test_an_expired_token_falls_back_to_a_full_reconcile(self):
        self.sync()
        link = self.link(self.ada)
        token = self.account.sudo().sync_token
        # Gone while the token was dead: no tombstone will ever say so.
        del self.google.people[link.resource_name]
        self.google.expire_tokens = True
        self.grace()
        self.sync()
        self.assertEqual(link.state, "gone")
        self.assertEqual(self.pending().name, "Grace Hopper")
        self.assertNotEqual(self.account.sudo().sync_token, token)
        self.assertTrue(self.account.last_full_sync)

    def test_a_lost_link_is_found_again_by_the_anchor(self):
        self.sync()
        resource = self.link(self.ada).resource_name
        self.account.link_ids.unlink()
        self.account.sudo().sync_token = False
        self.sync()
        self.assertEqual(self.link(self.ada).resource_name, resource)
        self.assertFalse(self.pending())
        self.assertEqual(len(self.google.people), 1)
