from types import SimpleNamespace
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, TransactionCase, new_test_user, tagged

from ..models.twp_google_contacts_account import GoogleReconnect
from ..wizard.twp_google_contacts_connect import RETURN_PATH, SESSION_KEY
from .common import GoogleContactsCase


@tagged("post_install", "-at_install")
class TestConnect(GoogleContactsCase, TransactionCase):

    def mine(self):
        # Other users' real connections may exist on the database the tests run on.
        return self.Account.search([("user_id", "=", self.env.uid)])

    def test_start_sends_return_address_and_state_with_the_key(self):
        url = self.Account._connect_start("http://odoo.test" + RETURN_PATH, "s1")
        self.assertTrue(url.startswith("https://accounts.google.com/"))
        path, body, headers = self.broker.calls[0]
        self.assertEqual(path, "/v1/connect/start")
        self.assertEqual(body, {"connector": "google-contacts",
                                "return_url": "http://odoo.test" + RETURN_PATH, "state": "s1"})
        self.assertEqual(headers["Authorization"], "Bearer twc_test")

    def test_continue_returns_to_the_databases_own_address(self):
        self.env["ir.config_parameter"].sudo().set_param("web.base.url", "https://odoo.test")
        browser = SimpleNamespace(session={},
                                  httprequest=SimpleNamespace(url_root="http://odoo.test/"))
        wizard = self.env["twp.google.contacts.connect"].create({"consent": True})
        with patch("odoo.addons.twp_google_contacts.wizard.twp_google_contacts_connect.request",
                   browser):
            action = wizard.action_continue()
        self.assertTrue(action["url"].startswith("https://accounts.google.com/"))
        self.assertEqual(self.broker.calls[0][1]["return_url"], "https://odoo.test" + RETURN_PATH,
                         "the address behind an https proxy, not the request's http one")
        self.assertEqual(browser.session[SESSION_KEY], self.broker.calls[0][1]["state"])

    def test_no_key_says_who_must_act(self):
        self.env["ir.config_parameter"].sudo().set_param("twp_google_contacts.broker_key", False)
        with self.assertRaisesRegex(UserError, "administrator"):
            self.Account._connect_start("http://odoo.test" + RETURN_PATH, "s1")
        self.assertEqual(self.broker.calls, [])

    def test_refused_key_says_who_must_act(self):
        self.broker.answers["/v1/connect/start"] = (401, {"error": "bad_key"})
        with self.assertRaisesRegex(UserError, "refused this database's key"):
            self.Account._connect_start("http://odoo.test" + RETURN_PATH, "s1")

    def test_finish_keeps_the_connection_for_the_user(self):
        account = self.connect()
        self.assertEqual((account.user_id, account.google_email, account.connection_id,
                          account.state), (self.env.user, "test@example.com", "conn_1", "connected"))
        self.assertEqual(self.env.user.google_contacts_state, "connected")
        self.assertEqual(self.env.user.google_contacts_email, "test@example.com")

    def test_connecting_again_gives_the_old_connection_back(self):
        first = self.connect()
        self.broker.answers["/v1/connect/finish"] = (200, {
            "connection_id": "conn_2", "account_email": "other@example.com"})
        second = self.connect()
        self.assertEqual(first, second)
        self.assertEqual(second.connection_id, "conn_2")
        self.assertIn(("/v1/disconnect", {"connection_id": "conn_1"}),
                      [(p, b) for p, b, _ in self.broker.calls])

    def test_disconnect_revokes_then_forgets(self):
        self.connect()
        self.env.user.action_google_contacts_disconnect()
        self.assertEqual(self.broker.paths()[-1], "/v1/disconnect")
        self.assertFalse(self.mine())
        self.assertEqual(self.env.user.google_contacts_state, "none")

    def test_disconnect_of_a_connection_the_broker_lost_still_forgets(self):
        self.connect()
        self.broker.answers["/v1/disconnect"] = (404, {"error": "unknown_connection"})
        self.env.user.action_google_contacts_disconnect()
        self.assertFalse(self.mine())

    def test_disconnect_that_cannot_revoke_keeps_the_connection(self):
        account = self.connect()
        self.broker.answers["/v1/disconnect"] = (502, {"error": "google_unavailable"})
        with self.assertRaises(UserError):
            self.env.user.action_google_contacts_disconnect()
        self.assertTrue(account.exists())

    def test_users_act_only_on_their_own_connection(self):
        alice = new_test_user(self.env, "alice", groups="base.group_user")
        bob = new_test_user(self.env, "bob", groups="base.group_user")
        self.connect(alice)
        with self.assertRaises(AccessError):
            alice.with_user(bob).action_google_contacts_disconnect()
        with self.assertRaises(AccessError):
            alice.with_user(bob).action_google_contacts_connect()
        self.assertEqual(alice.google_contacts_state, "connected")
        # Users read their own state from the preferences form.
        self.assertEqual(alice.with_user(alice).read(["google_contacts_state"])[0]
                         ["google_contacts_state"], "connected")

    def test_consent_needed_before_google(self):
        wizard = self.env["twp.google.contacts.connect"].create({})
        with self.assertRaisesRegex(UserError, "I agree"):
            wizard.action_continue()
        self.assertEqual(self.broker.calls, [])

    def test_expired_grant_marks_reconnect_and_check_reports_it(self):
        account = self.connect()
        self.broker.answers["/v1/token"] = (409, {"error": "reconnect_required"})
        action = self.env.user.action_google_contacts_check()
        self.assertEqual(action["params"]["type"], "danger")
        self.assertIn("Connect again", action["params"]["message"])
        self.assertEqual(account.state, "reconnect")
        with self.assertRaises(GoogleReconnect):
            account._get_access_token()

    def test_workspace_block_is_named(self):
        account = self.connect()
        self.broker.answers["/v1/token"] = (403, {"error": "admin_policy_enforced"})
        action = account.action_check()
        self.assertIn("Workspace administrator", action["params"]["message"])
        self.assertEqual(account.state, "blocked")

    def test_neutralize_drops_the_key(self):
        self.env["ir.config_parameter"].sudo().set_param("twp_google_contacts.broker_key", "twc_x")
        self.env.cr.execute(open(__file__.rsplit("/tests/", 1)[0] + "/data/neutralize.sql").read())
        self.assertFalse(self.env["ir.config_parameter"].sudo().get_param(
            "twp_google_contacts.broker_key"))


@tagged("post_install", "-at_install")
class TestReturnPage(GoogleContactsCase, HttpCase):

    def setUp(self):
        super().setUp()
        self.user = new_test_user(self.env, "returner", password="returner-pass-1",
                                  groups="base.group_user")

    def mine(self):
        return self.Account.search([("user_id", "=", self.user.id)])

    def back_from_google(self, session_state, **params):
        self.authenticate("returner", "returner-pass-1")
        if session_state:
            self.session[SESSION_KEY] = session_state
            self.session.is_dirty = True
            from odoo.http import root
            root.session_store.save(self.session)
        return self.url_open(RETURN_PATH + "?" + "&".join(f"{k}={v}" for k, v in params.items()))

    def test_return_finishes_the_connection(self):
        res = self.back_from_google("s1", state="s1", twc_code="handoff")
        self.assertEqual(res.status_code, 200)
        self.assertIn("connected to test@example.com", res.text)
        self.assertEqual(res.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(self.mine().google_email, "test@example.com")

    def test_return_from_another_session_is_refused(self):
        res = self.back_from_google("s1", state="someone-elses", twc_code="handoff")
        self.assertIn("did not start in this browser session", res.text)
        self.assertNotIn("/v1/connect/finish", self.broker.paths())

    def test_unticked_contacts_box_is_explained(self):
        res = self.back_from_google("s1", state="s1", twc_error="scope_not_granted")
        self.assertIn("tick the box", res.text)
        self.assertFalse(self.mine())
