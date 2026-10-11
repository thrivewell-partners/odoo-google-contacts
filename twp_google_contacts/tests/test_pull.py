from datetime import timedelta

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, new_test_user, tagged

from .common import SyncCase


@tagged("post_install", "-at_install")
class TestPull(SyncCase, TransactionCase):

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
        # A company Odoo did not have is made, once.
        self.assertEqual((partner.function, partner.parent_id.name, partner.parent_id.is_company),
                         ("Admiral", "Unknown Navy", True))
        self.assertEqual((partner.street, partner.city, partner.zip, partner.state_id.code,
                          partner.country_id.code), ("1 Pier Rd", "Arlington", "22201", "VA", "US"))
        self.assertIn("first compiler", partner.comment)
        self.assertEqual(partner.category_id, vip)
        self.assertEqual(self.google.people[resource]["clientData"][0]["key"], "thrivewell.anchor")
        # Nothing more goes back: the next sync makes no writes.
        self.google.writes.clear()
        self.sync()
        self.assertEqual(self.google.writes, [])

    def test_a_contact_linked_from_google_keeps_syncing_outside_the_scope(self):
        resource = self.grace()
        hopper = self.Partner.create({"name": "Grace Hopper", "email": "grace@navy.example"})
        self.sync()
        self.pending().action_link()
        self.assertNotIn(self.scope, hopper.category_id)
        hopper.email = "grace@hopper.example"
        self.sync()
        self.assertEqual(self.google.emails(resource)[0], "grace@hopper.example")

    def test_a_company_odoo_has_is_joined_not_doubled(self):
        self.account.sudo().import_reviewed = True
        self.grace(organizations=[{"name": "lovelace engines", "title": "Admiral"}])
        self.google.add(labels=["contactGroups/odoo"], names=[{"givenName": "Alan"}],
                        organizations=[{"name": "Bletchley Works"}])
        self.google.add(labels=["contactGroups/odoo"], names=[{"givenName": "Joan"}],
                        organizations=[{"name": "Bletchley Works"}])
        self.sync()
        grace = self.Partner.search([("email", "=", "grace@navy.example")])
        self.assertEqual(grace.parent_id, self.engines)
        # Google's own spelling of the company is kept, not joined by a second one.
        resource = self.link(grace).resource_name
        self.assertEqual(self.google.people[resource]["organizations"],
                         [{"name": "lovelace engines", "title": "Admiral"}])
        works = self.Partner.search([("name", "=", "Bletchley Works"), ("is_company", "=", True)])
        self.assertEqual(len(works), 1)
        self.assertEqual(len(works.child_ids), 2)

    def test_what_odoo_adds_to_a_contact_from_google_goes_to_google(self):
        self.account.sudo().import_reviewed = True
        self.engines.write({"street": "1 Engine Row", "city": "Indianapolis", "zip": "46204"})
        resource = self.grace(organizations=[{"name": "Lovelace Engines"}])
        self.sync()
        grace = self.Partner.search([("email", "=", "grace@navy.example")])
        self.assertEqual(grace.street, "1 Engine Row")  # Odoo gives people their company's address
        self.assertEqual([a.get("streetAddress") for a in self.google.people[resource]["addresses"]],
                         ["1 Engine Row"])
        self.google.writes.clear()
        self.sync()
        self.assertEqual(self.google.writes, [])
        self.assertEqual(grace.street, "1 Engine Row")

    def test_an_address_google_writes_its_own_way_is_not_a_change(self):
        indiana = self.env.ref("base.state_us_in")
        self.ada.write({"street": "1 Main St", "city": "Indianapolis", "zip": "46204",
                        "state_id": indiana.id, "country_id": indiana.country_id.id})
        self.sync()
        resource = self.link(self.ada).resource_name
        self.google.change(resource, addresses=[{
            "streetAddress": "1 Main St", "city": "Indianapolis", "region": "IN",
            "postalCode": "46204", "country": "United States", "type": "work"}])
        written = self.ada.write_date
        self.assertEqual(self.sync()["message"], "Nothing to change")
        self.assertEqual(self.ada.write_date, written)

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
        theirs = self.pending().with_user(other)
        self.env.invalidate_all()  # so the read goes to the database, through the record rules
        with self.assertRaises(AccessError):
            theirs.action_link()
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

    def test_when_both_changed_odoos_later_change_stands(self):
        self.sync()
        resource = self.link(self.ada).resource_name
        self.google.change(resource, at=fields.Datetime.now() - timedelta(hours=1),
                           emailAddresses=[{"value": "ada@google-side.example"}])
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
