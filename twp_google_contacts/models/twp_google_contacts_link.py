from odoo import fields, models

STOPPED = ("gone", "merged")


class TwpGoogleContactsLink(models.Model):
    """One Odoo partner as one Google contact, in one connected account.

    A partner can sit in several users' Google accounts, each with its own resource name and etag,
    so this is a table of its own rather than a field on the partner.
    """

    _name = "twp.google.contacts.link"
    _description = "Odoo contact synced to a Google contact"

    account_id = fields.Many2one("twp.google.contacts.account", required=True, ondelete="cascade",
                                 index=True)
    # Kept when the partner is deleted, so the next sync can take the contact out of Odoo's
    # labels in Google before the link goes.
    partner_id = fields.Many2one("res.partner", ondelete="set null", index=True)
    resource_name = fields.Char(required=True, index=True)
    etag = fields.Char()
    pushed = fields.Json(help="The partner values Odoo last sent, which say which item in each of "
                              "Google's lists is Odoo's")
    google_hash = fields.Char(help="A hash of the synced fields as Google held them after Odoo's "
                                   "last write, to recognise that write when it comes back")
    label_resource_names = fields.Json(help="The labels Odoo last put the contact in")
    odoo_changed = fields.Json(help="When each group of the partner's fields last changed in "
                                    "Odoo, to tell which side changed last")
    photo_synced = fields.Boolean(help="The photos on both sides have been compared once")
    photo_checksum = fields.Char(help="The partner's image as last synced")
    photo_url = fields.Char(help="Google's photo as last synced")
    archived_by_sync = fields.Boolean(help="The partner was archived because the contact was "
                                           "deleted in Google, so it comes back if the contact does")
    synced_at = fields.Datetime()
    state = fields.Selection(
        [("active", "Synced"),
         ("archived", "Archived in Odoo"),
         ("gone", "Deleted in Google"),
         ("merged", "Merged in Google")], default="active", required=True)

    _account_partner_unique = models.Constraint(
        "UNIQUE(account_id, partner_id)", "A partner is linked once per Google account.")

    def _stamp(self, groups):
        """Note that these groups changed in Odoo just now."""
        now = fields.Datetime.to_string(fields.Datetime.now())
        for link in self:
            link.odoo_changed = dict(link.odoo_changed or {}, **dict.fromkeys(groups, now))

    def _partners_merged(self, sources, target):
        """Odoo is merging `sources` into `target`. In each account, the best of their links is
        kept for `target`, preferring one still synced, and the rest are dropped, so the merge does
        not trip over one partner linked twice."""
        links = self.sudo().search([("partner_id", "in", (sources | target).ids)])
        for account in links.account_id:
            mine = links.filtered(lambda l: l.account_id == account)
            keep = mine.sorted(lambda l: (l.state != "active", l.state != "archived",
                                          l.partner_id != target))[0]
            (mine - keep).unlink()
            keep.partner_id = target
        self.env["twp.google.contacts.candidate"].sudo().search([
            ("kind", "=", "merge"), ("state", "=", "pending"),
            "|", ("match_id", "in", sources.ids), ("survivor_id", "in", sources.ids),
        ]).state = "done"
