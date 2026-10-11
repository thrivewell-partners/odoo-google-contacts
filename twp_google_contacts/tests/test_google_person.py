from odoo.tests.common import BaseCase

from ..models import google_person as gp


class TestGooglePerson(BaseCase):

    def test_names_split_on_the_last_word(self):
        self.assertEqual(gp.split_name("Ada King Lovelace"), ("Ada King", "Lovelace"))
        self.assertEqual(gp.split_name("Cher"), ("Cher", None))
        self.assertEqual(gp.split_name("  "), (None, None))

    def test_email_replaces_only_odoos_item(self):
        person = {"emailAddresses": [
            {"value": "old@example.com", "type": "work", "metadata": {"primary": True}},
            {"value": "home@example.com", "type": "home"}]}
        out = gp.merge(person, {"email": "new@example.com"}, {"email": "old@example.com"},
                       ["email"])
        self.assertEqual(out["emailAddresses"], [
            {"value": "new@example.com", "type": "work"},
            {"value": "home@example.com", "type": "home"}])

    def test_email_already_in_google_is_not_doubled(self):
        person = {"emailAddresses": [{"value": "Ada@Example.com", "type": "home"}]}
        out = gp.merge(person, {"email": "ada@example.com"}, {}, ["email"])
        self.assertEqual(out["emailAddresses"], [{"value": "Ada@Example.com", "type": "home"}])

    def test_phone_compared_by_digits(self):
        person = {"phoneNumbers": [
            {"value": "+1 317-555-0140", "type": "mobile", "canonicalForm": "+13175550140"},
            {"value": "(317) 555-0199", "type": "home"}]}
        same = gp.merge(person, {"phone": "(317) 555-0140"}, {"phone": "317.555.0140"}, ["phone"])
        self.assertEqual(len(same["phoneNumbers"]), 2)
        changed = gp.merge(person, {"phone": "(317) 555-0141"}, {"phone": "317.555.0140"},
                           ["phone"])
        self.assertEqual([p["value"] for p in changed["phoneNumbers"]],
                         ["(317) 555-0141", "(317) 555-0199"])

    def test_clearing_in_odoo_removes_only_odoos_item(self):
        person = {"urls": [{"value": "https://ours.example"}, {"value": "https://theirs.example"}]}
        out = gp.merge(person, {"website": None}, {"website": "https://ours.example"}, ["website"])
        self.assertEqual(out["urls"], [{"value": "https://theirs.example"}])

    def test_address_replaces_ours_and_keeps_home(self):
        home = {"streetAddress": "1 Home St", "city": "Carmel", "type": "home"}
        ours = {"streetAddress": "9 Work Rd", "city": "Indy", "postalCode": "46204",
                "type": "work", "formattedValue": "9 Work Rd, Indy"}
        old = {"streetAddress": "9 Work Rd", "city": "Indy", "postalCode": "46204"}
        new = dict(old, streetAddress="10 Work Rd", region="Indiana")
        out = gp.merge({"addresses": [home, ours]}, {"address": new}, {"address": old},
                       ["address"])
        self.assertEqual(out["addresses"], [home, {
            "streetAddress": "10 Work Rd", "city": "Indy", "postalCode": "46204",
            "region": "Indiana", "type": "work"}])

    def test_organization_keeps_others(self):
        person = {"organizations": [{"name": "Old Co", "title": "Buyer"}, {"name": "Board"}]}
        out = gp.merge(person, {"organization": {"name": "New Co", "title": None}},
                       {"organization": {"name": "Old Co", "title": "Buyer"}}, ["organization"])
        self.assertEqual(out["organizations"], [{"name": "New Co"}, {"name": "Board"}])

    def test_name_keeps_prefix_and_drops_derived(self):
        person = {"names": [{"givenName": "Ada", "familyName": "Byron", "honorificPrefix": "Dr",
                             "displayName": "Dr Ada Byron", "unstructuredName": "Dr Ada Byron"}]}
        out = gp.merge(person, {"name": {"givenName": "Ada", "familyName": "Lovelace"}}, {},
                       ["name"])
        self.assertEqual(out["names"], [{"givenName": "Ada", "familyName": "Lovelace",
                                         "honorificPrefix": "Dr"}])

    def test_new_person_carries_the_anchor_and_no_empty_lists(self):
        person = gp.new_person({"name": {"givenName": "Ada", "familyName": None},
                                "email": "ada@example.com"}, "odoo:u:res.partner:7")
        self.assertEqual(person, {
            "names": [{"givenName": "Ada"}],
            "emailAddresses": [{"value": "ada@example.com", "type": "work"}],
            "clientData": [{"key": "thrivewell.anchor", "value": "odoo:u:res.partner:7"}]})

    def test_hash_ignores_what_google_adds(self):
        a = {"emailAddresses": [{"value": "a@example.com", "metadata": {"source": {"id": "1"}}}],
             "etag": "x", "memberships": []}
        b = {"emailAddresses": [{"value": "a@example.com"}], "etag": "y"}
        self.assertEqual(gp.person_hash(a), gp.person_hash(b))
        self.assertNotEqual(gp.person_hash(a), gp.person_hash(
            {"emailAddresses": [{"value": "b@example.com"}]}))

    def test_google_values_read_odoos_item(self):
        person = {"emailAddresses": [{"value": "home@example.com", "metadata": {"primary": True}},
                                     {"value": "Ada@Example.com"}],
                  "phoneNumbers": [{"value": "+1 317-555-0140"}],
                  "urls": [{"value": "https://ada.example/"}]}
        pushed = {"email": "ada@example.com", "phone": "(317) 555-0140",
                  "website": "http://ada.example"}
        values = gp.google_values(person, pushed)
        # Odoo's items are still there, only written differently: unchanged.
        self.assertEqual((values["email"], values["phone"], values["website"]),
                         ("ada@example.com", "(317) 555-0140", "http://ada.example"))
        # With Odoo's item gone, Google's primary stands in.
        self.assertEqual(gp.google_values(person, {"email": "gone@example.com"})["email"],
                         "home@example.com")

    def test_google_values_of_an_empty_person(self):
        self.assertEqual(set(gp.google_values({}).values()), {None})

    def test_anchor_of_another_database_is_not_ours(self):
        person = {"clientData": [{"key": "thrivewell.anchor", "value": "odoo:other:res.partner:7"},
                                 {"key": "someone.else", "value": "x"}]}
        self.assertIsNone(gp.anchored_partner_id(person, "ours"))
        mine = gp.with_anchor(person, "odoo:ours:res.partner:9")
        self.assertEqual(mine, person["clientData"] + [
            {"key": "thrivewell.anchor", "value": "odoo:ours:res.partner:9"}])
        # Writing it again replaces only this database's own anchor.
        self.assertEqual(gp.with_anchor({"clientData": mine}, "odoo:ours:res.partner:10")[-1],
                         {"key": "thrivewell.anchor", "value": "odoo:ours:res.partner:10"})
        self.assertEqual(len(gp.with_anchor({"clientData": mine}, "odoo:ours:res.partner:10")), 3)
