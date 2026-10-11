from datetime import timedelta

from odoo import Command, api, fields, models

OUTCOMES = [("done", "Done"),
            ("partial", "Continues"),
            ("quota", "Slowed by Google"),
            ("failed", "Failed"),
            ("reconnect", "Needs reconnecting"),
            ("blocked", "Blocked by Workspace admin")]
# How long the log is kept.
KEEP_DAYS = 90


class TwpGoogleContactsLog(models.Model):
    """One sync of one Google account: when, what it changed, and what failed. For the
    administrator; each user sees only their last result on their own tab."""

    _name = "twp.google.contacts.log"
    _description = "Google Contacts sync log"
    _order = "started desc, id desc"
    _rec_name = "started"

    account_id = fields.Many2one("twp.google.contacts.account", ondelete="set null", index=True)
    user_id = fields.Many2one("res.users", string="User", ondelete="cascade", index=True)
    google_email = fields.Char(string="Google account")
    trigger = fields.Selection([("cron", "Scheduled"), ("manual", "Sync now")])
    started = fields.Datetime()
    duration = fields.Float(string="Seconds", digits=(16, 1))
    outcome = fields.Selection(OUTCOMES)
    summary = fields.Char()
    from_google = fields.Integer(string="From Google",
                                 help="Contacts added, changed or archived in Odoo")
    to_google = fields.Integer(string="To Google",
                               help="Contacts created, matched or changed in Google")
    failed = fields.Integer()
    line_ids = fields.One2many("twp.google.contacts.log.line", "log_id", string="Failures")

    @api.model
    def _record(self, account, run, trigger, started, duration, summary):
        return self.create({
            "account_id": account.id,
            "user_id": account.user_id.id,
            "google_email": account.google_email,
            "trigger": trigger,
            "started": started,
            "duration": duration,
            "outcome": run.outcome,
            "summary": summary,
            "from_google": sum(run[k] for k in ("added_from_google", "changed_from_google",
                                                "archived", "followed")),
            "to_google": sum(run[k] for k in ("created", "matched", "updated")),
            "failed": run["failed"],
            "line_ids": [Command.create(line) for line in run.failures],
        })

    @api.autovacuum
    def _gc_old_runs(self):
        self.search([("started", "<", fields.Datetime.now() - timedelta(days=KEEP_DAYS))]).unlink()


class TwpGoogleContactsLogLine(models.Model):
    """A contact, or a label, that did not sync in a run, and Google's reason."""

    _name = "twp.google.contacts.log.line"
    _description = "Google Contacts sync failure"

    log_id = fields.Many2one("twp.google.contacts.log", required=True, ondelete="cascade",
                             index=True)
    partner_id = fields.Many2one("res.partner", string="Contact", ondelete="set null")
    resource_name = fields.Char(string="In Google")
    message = fields.Char(string="Reason")
