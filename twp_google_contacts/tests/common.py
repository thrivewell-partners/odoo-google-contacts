import json
from datetime import timedelta
from unittest.mock import patch

import requests

from odoo import fields

BROKER = "odoo.addons.twp_google_contacts.models.broker.requests.post"
GOOGLE = "odoo.addons.twp_google_contacts.models.twp_google_contacts_account.requests.request"
SLEEP = "odoo.addons.twp_google_contacts.models.twp_google_contacts_account.time.sleep"


def response(status, body=None, headers=None):
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(body).encode() if body is not None else b""
    r.headers.update(headers or {})
    return r


class FakeBroker:
    """Stands in for ThriveWell Connect. `answers` maps a path to (status, body)."""

    def __init__(self):
        self.calls = []
        self.tokens = 0
        self.answers = {
            "/v1/connect/start": (200, {"authorize_url": "https://accounts.google.com/o/oauth2/v2/auth?x=1"}),
            "/v1/connect/finish": (200, {"connection_id": "conn_1", "connector": "google-contacts",
                                         "account_email": "test@example.com"}),
            "/v1/disconnect": (200, {"disconnected": True, "revoked_at_google": True}),
        }

    def __call__(self, url, json=None, timeout=None, headers=None):
        path = url.split("connect.test", 1)[1]
        self.calls.append((path, json, headers))
        if path == "/v1/token" and path not in self.answers:
            self.tokens += 1
            return response(200, {"access_token": "at-%s" % self.tokens, "expires_in": 3599})
        status, body = self.answers[path]
        return response(status, body)

    def paths(self):
        return [path for path, _, _ in self.calls]


class FakeGoogle:
    """Stands in for people.googleapis.com. Answers are taken in order; the last one repeats."""

    def __init__(self, *answers):
        self.answers = list(answers) or [response(200, {"totalItems": 3})]
        self.calls = []

    def __call__(self, method, url, params=None, json=None, timeout=None, headers=None):
        self.calls.append((method, url, headers["Authorization"]))
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


class GoogleContactsCase:
    """Mixin: a configured database, a fake ThriveWell Connect, and helpers."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        params = cls.env["ir.config_parameter"].sudo()
        params.set_param("twp_google_contacts.broker_url", "http://connect.test")
        params.set_param("twp_google_contacts.broker_key", "twc_test")
        cls.Account = cls.env["twp.google.contacts.account"]

    def setUp(self):
        super().setUp()
        self.broker = FakeBroker()
        patcher = patch(BROKER, self.broker)
        patcher.start()
        self.addCleanup(patcher.stop)

    def connect(self, user=None):
        Account = self.Account.with_user(user) if user else self.Account
        return Account._connect_finish("handoff-code")

    def expire_token(self, account):
        account.sudo().access_token_expiry = fields.Datetime.now() - timedelta(minutes=1)
