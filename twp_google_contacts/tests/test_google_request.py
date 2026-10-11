from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from ..models.twp_google_contacts_account import GoogleApiError, GoogleReconnect
from .common import GOOGLE, SLEEP, FakeGoogle, GoogleContactsCase, response

STALE_ETAG = response(400, {"error": {"code": 400, "status": "FAILED_PRECONDITION",
                                      "message": "Request person.etag is different"}})
SCOPE_GONE = response(403, {"error": {"code": 403, "status": "PERMISSION_DENIED",
                                      "message": "Insufficient scopes",
                                      "details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}})


@tagged("post_install", "-at_install")
class TestGoogleRequest(GoogleContactsCase, TransactionCase):

    def setUp(self):
        super().setUp()
        self.account = self.connect()
        sleeper = patch(SLEEP)
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)

    def request(self, google, method="GET", path="contactGroups"):
        with patch(GOOGLE, google):
            return self.account._google_request(method, path)

    def test_token_fetched_once_and_reused_within_the_hour(self):
        google = FakeGoogle()
        self.request(google)
        self.request(google)
        self.assertEqual(self.broker.paths().count("/v1/token"), 1)
        self.assertEqual(google.calls[0][1], "https://people.googleapis.com/v1/contactGroups")
        self.assertEqual(google.calls[1][2], "Bearer at-1")

    def test_token_renewed_when_it_lapses(self):
        google = FakeGoogle()
        self.request(google)
        self.expire_token(self.account)
        self.request(google)
        self.assertEqual(google.calls[1][2], "Bearer at-2")

    def test_rejected_token_renewed_once(self):
        google = FakeGoogle(response(401, {"error": {"code": 401}}), response(200, {"ok": 1}))
        self.assertEqual(self.request(google), {"ok": 1})
        self.assertEqual([c[2] for c in google.calls], ["Bearer at-1", "Bearer at-2"])

    def test_rate_limit_waits_and_retries(self):
        google = FakeGoogle(response(429, {}), response(503, {}, {"Retry-After": "7"}),
                            response(200, {"ok": 1}))
        self.assertEqual(self.request(google), {"ok": 1})
        waits = [c.args[0] for c in self.sleep.call_args_list]
        self.assertEqual(len(waits), 2)
        self.assertTrue(1 <= waits[0] < 2)
        self.assertEqual(waits[1], 7)

    def test_rate_limit_gives_up_after_five_tries(self):
        google = FakeGoogle(response(429, {"error": {"status": "RESOURCE_EXHAUSTED",
                                                     "message": "Quota"}}))
        with self.assertRaises(GoogleApiError) as caught:
            self.request(google)
        self.assertEqual(len(google.calls), 5)
        self.assertEqual(caught.exception.google_status, "RESOURCE_EXHAUSTED")

    def test_stale_etag_is_handed_to_the_caller(self):
        with self.assertRaises(GoogleApiError) as caught:
            self.request(FakeGoogle(STALE_ETAG), "PATCH", "people/c1:updateContact")
        self.assertEqual((caught.exception.status, caught.exception.google_status),
                         (400, "FAILED_PRECONDITION"))
        self.assertEqual(self.sleep.call_count, 0)

    def test_lost_contacts_permission_marks_reconnect(self):
        with self.assertRaises(GoogleReconnect):
            self.request(FakeGoogle(SCOPE_GONE))
        # Escaping, the error rolls its own state change back. The entry point catches it, so
        # the change sticks.
        with patch(GOOGLE, FakeGoogle(SCOPE_GONE)):
            action = self.account.action_check()
        self.assertEqual(action["params"]["type"], "danger")
        self.assertEqual(self.account.state, "reconnect")

    def test_check_reports_the_label_count(self):
        with patch(GOOGLE, FakeGoogle()):
            action = self.account.action_check()
        self.assertEqual(action["params"]["type"], "success")
        self.assertIn("3 labels", action["params"]["message"])
