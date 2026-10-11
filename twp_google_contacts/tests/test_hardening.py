import sys
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from .. import uninstall_hook
from .common import ACCOUNT, SLEEP, SyncCase, response

QUOTA = response(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                 "message": "Quota exceeded"}})
REFUSED = response(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                   "message": "Invalid phone number"}})
COMMIT_PROGRESS = "odoo.addons.base.models.ir_cron.IrCron._commit_progress"


def in_push():
    frame = sys._getframe(1)
    while frame:
        if frame.f_code.co_name == "_push":
            return True
        frame = frame.f_back
    return False


@tagged("post_install", "-at_install")
class TestHardening(SyncCase, TransactionCase):
    """M6: Google's limits, Odoo's time limit, batches, the sync log, and a clean uninstall."""

    def setUp(self):
        super().setUp()
        self.params = self.env["ir.config_parameter"].sudo()
        sleeper = patch(SLEEP)
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)

    def people(self, count, prefix="Pat"):
        return self.Partner.create([{"name": "%s %s" % (prefix, n),
                                     "email": "%s%s@example.com" % (prefix.lower(), n),
                                     "category_id": self.scope.ids} for n in range(count)])

    def last_log(self):
        return self.env["twp.google.contacts.log"].search(
            [("account_id", "=", self.account.id)], limit=1)

    # Google's limits.

    def test_googles_limit_stops_the_sync_and_keeps_what_was_done(self):
        self.sync()
        self.people(2)
        self.google.refuse["people:createContact"] = QUOTA
        result = self.sync()
        self.assertEqual(result["type"], "warning")
        self.assertIn("slow down", result["message"])
        log = self.last_log()
        self.assertEqual(log.outcome, "quota")
        self.assertTrue(self.link(self.ada), "what was done before is kept")
        due = self.account.next_sync - fields.Datetime.now()
        self.assertTrue(timedelta(minutes=4) < due <= timedelta(minutes=5))
        # One wait of about a minute for the first new contact, not one for each.
        self.assertEqual(self.sleep.call_count, 6)

        del self.google.refuse["people:createContact"]
        self.sync()
        self.assertEqual(len(self.account.link_ids), 3)
        self.assertEqual(self.last_log().outcome, "done")

    # Odoo's time limit.

    def test_a_push_out_of_time_stops_and_the_next_run_goes_on(self):
        self.people(3)
        self.sync()  # the first sync: the review list is empty, so it links all four
        self.assertEqual(len(self.account.link_ids), 4)
        more = self.people(3, "Quinn")
        calls = []

        def out_of_time(account):
            if not in_push():
                return False
            calls.append(1)
            return len(calls) > 5  # the time runs out after five partners, one of them new

        with patch(ACCOUNT + ".TwpGoogleContactsAccount._out_of_time", out_of_time):
            result = self.sync()
        self.assertEqual(result["type"], "info")
        self.assertIn("continues in the background", result["message"])
        self.assertEqual(self.last_log().outcome, "partial")
        self.assertLessEqual(self.account.next_sync, fields.Datetime.now())
        self.assertEqual(len(self.account.link_ids.partner_id & more), 1)
        self.sync()
        self.assertEqual(len(self.account.link_ids.partner_id & more), 3)
        for link in self.account.link_ids:
            self.assertIn(link.resource_name,
                          self.google.members[self.account.label_resource_name])

    def test_a_pull_out_of_time_keeps_no_new_token(self):
        self.sync()
        token = self.account.sudo().sync_token
        self.google.change(self.link(self.ada).resource_name, at=fields.Datetime.now(),
                           organizations=[{"title": "Countess"}])
        with patch(ACCOUNT + ".TwpGoogleContactsAccount._out_of_time", lambda a: True):
            self.sync()
        self.assertEqual(self.account.sudo().sync_token, token, "the same changes are read again")
        self.assertFalse(self.ada.function)
        self.sync()
        self.assertEqual(self.ada.function, "Countess")

    def test_the_deadline_is_a_share_of_odoos_limit(self):
        Account = self.env["twp.google.contacts.account"]
        with patch(ACCOUNT + ".time.monotonic", lambda: 1000.0), \
                patch(ACCOUNT + ".config", {"limit_time_real": 100, "limit_time_real_cron": -1}):
            self.assertEqual(Account._deadline(), 1060.0)
            cron = Account.with_context(cron_end_time=1010.0)
            self.assertEqual(cron._deadline(), 1060.0, "counted from the job's start")

    # Batches.

    def test_many_new_contacts_go_in_batches(self):
        self.params.set_param("twp_google_contacts.batch_from", "3")
        batch = self.people(4)
        self.sync()
        self.assertEqual(self.google.batches, [5])
        self.assertNotIn(("POST", "people:createContact"), self.google.writes)
        for partner in batch | self.ada:
            link = self.link(partner)
            self.assertTrue(link)
            person = self.google.people[link.resource_name]
            self.assertEqual(person["names"][0]["givenName"], partner.name.split()[0])
        members = self.google.members[self.account.label_resource_name]
        self.assertEqual(set(self.account.link_ids.mapped("resource_name")), members)
        self.assertIn("5 sent to Google", self.account.last_sync_result)

    def test_a_refused_batch_is_sent_one_at_a_time(self):
        self.params.set_param("twp_google_contacts.batch_from", "2")
        self.people(2)
        self.google.refuse["people:batchCreateContacts"] = REFUSED
        self.sync()
        self.assertEqual(len(self.account.link_ids), 3)
        self.assertEqual(self.google.writes.count(("POST", "people:createContact")), 3)

    def test_label_members_change_a_thousand_at_a_time(self):
        self.people(4)
        with patch(ACCOUNT + ".MEMBERS_MAX", 2):
            self.sync()
        label = self.account.label_resource_name
        self.assertEqual(self.google.writes.count(("POST", label + "/members:modify")), 3)
        self.assertEqual(len(self.google.members[label]), 5)

    def test_every_page_of_labels_is_read(self):
        self.google.group_page_size = 1
        self.sync()
        self.assertEqual(self.account.label_name, "Odoo")
        self.assertEqual(len([g for g in self.google.groups.values() if g["name"] == "Odoo"]), 1,
                         "the label on a later page is found, not made twice")

    # The sync log.

    def test_each_sync_is_logged_with_its_failures(self):
        self.sync()
        log = self.last_log()
        self.assertEqual((log.outcome, log.trigger, log.to_google, log.failed),
                         ("done", "manual", 1, 0))
        self.assertEqual(log.user_id, self.env.user)
        bad = self.people(1, "Broken")
        self.google.refuse["people:createContact"] = REFUSED
        self.sync()
        log = self.last_log()
        self.assertEqual(log.failed, 1)
        self.assertEqual(log.line_ids.partner_id, bad)
        self.assertIn("Invalid phone number", log.line_ids.message)
        self.assertIn("1 failed", log.summary)

    def test_an_unexpected_error_is_logged_and_not_raised(self):
        with patch(ACCOUNT + ".TwpGoogleContactsAccount._push",
                   lambda *a: 1 / 0):
            result = self.sync()
        self.assertEqual(result["type"], "warning")
        self.assertIn("unexpected", result["message"])
        self.assertEqual(self.last_log().outcome, "failed")

    def test_a_lost_connection_is_logged_and_kept(self):
        self.broker.answers["/v1/token"] = (400, {"error": "reconnect_required"})
        self.expire_token(self.account)
        result = self.sync()
        self.assertEqual(result["type"], "danger")
        self.assertEqual(self.account.state, "reconnect")
        self.assertEqual(self.last_log().outcome, "reconnect")

    def test_old_runs_are_cleared(self):
        self.sync()
        log = self.last_log()
        log.started = fields.Datetime.now() - timedelta(days=91)
        self.env["twp.google.contacts.log"]._gc_old_runs()
        self.assertFalse(log.exists())

    # The scheduled sync.

    def only_test_accounts(self, *accounts):
        """The database's own connected accounts wait, so only the test's are due."""
        self.Account.sudo().search([("id", "not in", [a.id for a in accounts])]).write(
            {"next_sync": fields.Datetime.now() + timedelta(days=1)})

    def test_the_schedule_takes_each_due_account_and_goes_on_past_a_failure(self):
        other_user = self.env["res.users"].create({
            "name": "Second User", "login": "second.user@example.com",
            "group_ids": [(6, 0, self.env.ref("base.group_user").ids)]})
        other = self.connect(other_user)
        self.only_test_accounts(self.account, other)
        later = fields.Datetime.now() + timedelta(hours=1)
        synced, progress = [], []
        real_sync = type(self.account)._sync

        def sync(account, trigger="manual"):
            synced.append(account.id)
            if account == other:
                raise RuntimeError("boom")
            return real_sync(account, trigger)

        with patch(ACCOUNT + ".TwpGoogleContactsAccount._sync", sync), \
                patch(COMMIT_PROGRESS, lambda self, *a, **k: progress.append(k) or 1.0):
            self.Account._cron_sync()
            self.assertEqual(sorted(synced), sorted([self.account.id, other.id]))
            self.assertEqual(progress[-1], {"remaining": 0})
            self.assertEqual(self.last_log().trigger, "cron")
            self.account.next_sync = later
            other.sudo().next_sync = later
            synced.clear()
            self.Account._cron_sync()
            self.assertEqual(synced, [], "neither is due")

    def test_the_schedule_stops_when_its_time_is_up(self):
        progress = []
        self.only_test_accounts(self.account)
        with patch(ACCOUNT + ".TwpGoogleContactsAccount._deadline", lambda self: 0), \
                patch(COMMIT_PROGRESS, lambda self, *a, **k: progress.append(k) or 0.0):
            self.Account._cron_sync()
        self.assertFalse(self.last_log(), "nothing started")
        self.assertEqual(progress, [{"remaining": 1}], "Odoo runs the job again for the rest")

    # Uninstalling.

    def test_uninstall_gives_connections_back_and_removes_settings(self):
        uninstall_hook(self.env)
        self.assertIn(("/v1/disconnect", {"connection_id": "conn_1"}),
                      [(path, body) for path, body, _ in self.broker.calls])
        self.assertFalse(self.params.search([("key", "=like", "twp_google_contacts.%")]))

    def test_uninstall_from_a_copy_gives_nothing_back(self):
        self.params.set_param("database.is_neutralized", True)
        self.params.set_param("twp_google_contacts.test_database", False)
        uninstall_hook(self.env)
        self.assertNotIn("/v1/disconnect", self.broker.paths())
        self.assertFalse(self.params.get_param("twp_google_contacts.broker_key"))

    def test_uninstall_stops_when_connect_cannot_be_reached(self):
        self.broker.answers["/v1/disconnect"] = (503, {"error": "unavailable"})
        with self.assertRaisesRegex(UserError, "not uninstalled"):
            uninstall_hook(self.env)
        self.assertTrue(self.params.get_param("twp_google_contacts.broker_key"))

    def test_uninstall_goes_on_when_the_key_is_refused(self):
        self.broker.answers["/v1/disconnect"] = (401, {"error": "invalid_key"})
        uninstall_hook(self.env)
        self.assertFalse(self.params.get_param("twp_google_contacts.broker_key"))
