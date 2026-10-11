import copy
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from .common import GOOGLE, GoogleContactsCase, response

ACCOUNT = "odoo.addons.twp_google_contacts.models.twp_google_contacts_account"


class FakePeople:
    """An in-memory Google Contacts for one account, strict about etags as Google is."""

    def __init__(self):
        self.people = {}
        self.groups = {"contactGroups/myContacts": {"name": "myContacts", "groupType": "SYSTEM_CONTACT_GROUP"},
                       "contactGroups/family": {"name": "Family", "groupType": "USER_CONTACT_GROUP"}}
        self.members = {}
        self.writes = []
        self.serial = 0
        self.before_write = None  # called before an update is applied, to stage a Google change

    def _next(self, prefix):
        self.serial += 1
        return "%s%s" % (prefix, self.serial)

    def __call__(self, method, url, params=None, json=None, timeout=None, headers=None):
        path = url.split("/v1/", 1)[1]
        params = params or {}
        if method != "GET":
            self.writes.append((method, path))
        if path == "contactGroups" and method == "GET":
            return response(200, {"contactGroups": [dict(g, resourceName=r)
                                                    for r, g in self.groups.items()]})
        if path == "contactGroups" and method == "POST":
            resource = self._next("contactGroups/g")
            self.groups[resource] = {"name": json["contactGroup"]["name"],
                                     "groupType": "USER_CONTACT_GROUP"}
            return response(200, dict(self.groups[resource], resourceName=resource))
        if path.endswith("/members:modify"):
            group = path[:-len("/members:modify")]
            members = self.members.setdefault(group, set())
            members |= set(json.get("resourceNamesToAdd", []))
            members -= set(json.get("resourceNamesToRemove", []))
            return response(200, {})
        if path == "people:createContact":
            resource = self._next("people/c")
            self.people[resource] = dict(copy.deepcopy(json), resourceName=resource,
                                         etag=self._next("e"))
            return response(200, self.people[resource])
        resource = path.split(":")[0]
        if resource not in self.people:
            return response(404, {"error": {"code": 404, "status": "NOT_FOUND",
                                            "message": "Requested entity was not found."}})
        if method == "GET":
            return response(200, copy.deepcopy(self.people[resource]))
        if path.endswith(":updateContact"):
            if self.before_write:
                self.before_write(self.people[resource])
                self.before_write = None
            if json["etag"] != self.people[resource]["etag"]:
                return response(400, {"error": {"code": 400, "status": "FAILED_PRECONDITION",
                                                "message": "Request person.etag is different"}})
            for field in params["updatePersonFields"].split(","):
                self.people[resource][field] = copy.deepcopy(json.get(field, []))
            self.people[resource]["etag"] = self._next("e")
            return response(200, copy.deepcopy(self.people[resource]))
        raise AssertionError("unexpected call %s %s" % (method, path))

    def change(self, resource, **fields):
        """A change made by the user in Google."""
        self.people[resource].update(fields)
        self.people[resource]["etag"] = self._next("e")

    def emails(self, resource):
        return [e["value"] for e in self.people[resource].get("emailAddresses", [])]


@tagged("post_install", "-at_install")
class TestPush(GoogleContactsCase, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        params = cls.env["ir.config_parameter"].sudo()
        # The sandbox these run on is itself a copy.
        params.set_param("twp_google_contacts.test_database", True)
        params.set_param("twp_google_contacts.scope", "contactable")
        cls.Partner = cls.env["res.partner"]
        cls.company = cls.Partner.create({"name": "Lovelace Engines", "is_company": True,
                                          "email": "office@engines.example"})
        cls.ada = cls.Partner.create({"name": "Ada King Lovelace", "email": "ada@example.com",
                                      "phone": "(317) 555-0140", "parent_id": cls.company.id,
                                      "function": "Analyst", "city": "Indianapolis"})
        cls.silent = cls.Partner.create({"name": "No Way To Reach"})

    def setUp(self):
        super().setUp()
        self.google = FakePeople()
        for target, value in ((GOOGLE, self.google),
                              (ACCOUNT + ".TwpGoogleContactsAccount._commit_step", lambda s: None)):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.account = self.connect()

    def push(self):
        return self.account.action_sync_now()["params"]

    def link(self, partner):
        return self.account.link_ids.filtered(lambda l: l.partner_id == partner)

    def test_creates_people_with_the_anchor_under_the_label(self):
        result = self.push()
        self.assertEqual(result["type"], "success", result["message"])
        link = self.link(self.ada)
        person = self.google.people[link.resource_name]
        uuid = self.env["ir.config_parameter"].sudo().get_param("database.uuid")
        self.assertEqual(person["clientData"], [{"key": "thrivewell.anchor",
                                                 "value": "odoo:%s:res.partner:%s" % (uuid, self.ada.id)}])
        self.assertEqual(person["names"], [{"givenName": "Ada King", "familyName": "Lovelace"}])
        self.assertEqual(person["organizations"], [{"name": "Lovelace Engines", "title": "Analyst"}])
        label = self.account.label_resource_name
        self.assertEqual(self.google.groups[label]["name"], "Odoo")
        self.assertIn(link.resource_name, self.google.members[label])
        self.assertEqual(link.etag, person["etag"])

    def test_companies_colleagues_and_unreachable_people_stay_out(self):
        self.push()
        sent = self.account.link_ids.partner_id
        self.assertIn(self.ada, sent)
        self.assertNotIn(self.company, sent)
        self.assertNotIn(self.silent, sent)
        self.assertFalse(sent & self.env["res.users"].search([("share", "=", False)]).partner_id)

    def test_an_existing_label_of_that_name_is_reused(self):
        self.google.groups["contactGroups/mine"] = {"name": "Odoo", "groupType": "USER_CONTACT_GROUP"}
        self.push()
        self.assertEqual(self.account.label_resource_name, "contactGroups/mine")

    def test_nothing_changed_means_no_writes(self):
        self.push()
        self.google.writes.clear()
        self.push()
        self.assertEqual(self.google.writes, [])

    def test_a_change_in_odoo_merges_and_keeps_googles_extras(self):
        self.push()
        resource = self.link(self.ada).resource_name
        self.google.change(resource,
                           emailAddresses=self.google.people[resource]["emailAddresses"]
                           + [{"value": "ada@home.example", "type": "home"}],
                           phoneNumbers=[{"value": "(317) 555-0140", "type": "work"},
                                         {"value": "(317) 555-0199", "type": "mobile"}])
        self.ada.email = "ada@engines.example"
        self.push()
        self.assertEqual(self.google.emails(resource), ["ada@engines.example", "ada@home.example"])
        self.assertEqual(len(self.google.people[resource]["phoneNumbers"]), 2)
        self.assertEqual(self.google.writes[-1][1], resource + ":updateContact")

    def test_a_stale_etag_is_read_again_and_merged_again(self):
        self.push()
        resource = self.link(self.ada).resource_name

        def user_adds_an_email(person):
            self.google.change(resource, emailAddresses=person["emailAddresses"]
                               + [{"value": "ada@home.example", "type": "home"}])

        self.google.before_write = user_adds_an_email
        self.ada.email = "ada@engines.example"
        result = self.push()
        self.assertEqual(result["type"], "success", result["message"])
        self.assertEqual(self.google.emails(resource), ["ada@engines.example", "ada@home.example"])

    def test_deleted_in_google_is_never_recreated(self):
        self.push()
        link = self.link(self.ada)
        del self.google.people[link.resource_name]
        self.ada.phone = "(317) 555-0141"
        self.push()
        self.assertEqual(link.state, "gone")
        count = len(self.google.people)
        self.push()
        self.assertEqual(len(self.google.people), count)

    def test_tags_become_labels_and_only_odoos_are_taken_away(self):
        vip = self.env["res.partner.category"].create({"name": "VIP"})
        self.ada.category_id = vip
        self.push()
        resource = self.link(self.ada).resource_name
        vip_label = next(r for r, g in self.google.groups.items() if g["name"] == "VIP")
        self.assertIn(resource, self.google.members[vip_label])
        self.google.members.setdefault("contactGroups/family", set()).add(resource)
        self.ada.category_id = False
        self.push()
        self.assertNotIn(resource, self.google.members[vip_label])
        self.assertIn(resource, self.google.members["contactGroups/family"])
        self.assertIn(resource, self.google.members[self.account.label_resource_name])

    def test_tagged_scope_sends_only_tagged(self):
        tag = self.env["res.partner.category"].create({"name": "Google test"})
        tagged_one = self.Partner.create({"name": "Tess Tagged", "category_id": tag.ids})
        self.env.user.company_id.twp_google_contacts_tag_ids = tag
        self.env["ir.config_parameter"].sudo().set_param("twp_google_contacts.scope", "tagged")
        self.push()
        self.assertEqual(self.account.link_ids.partner_id, tagged_one)
        self.assertNotIn("Google test", [g["name"] for g in self.google.groups.values()])

    def test_a_copied_database_does_not_sync(self):
        params = self.env["ir.config_parameter"].sudo()
        params.set_param("database.is_neutralized", True)
        params.set_param("twp_google_contacts.test_database", False)
        result = self.push()
        self.assertEqual(result["type"], "warning")
        self.assertIn("copy", result["message"])
        self.assertEqual(self.google.writes, [])

    def test_another_google_account_drops_the_links(self):
        self.push()
        self.assertTrue(self.account.link_ids)
        self.broker.answers["/v1/connect/finish"] = (200, {
            "connection_id": "conn_2", "account_email": "someone.else@example.com"})
        self.connect()
        self.assertFalse(self.account.link_ids)
        self.assertFalse(self.account.label_resource_name)
