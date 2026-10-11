from odoo import models

from . import google_person

# Set on writes the sync makes from Google, which are not changes made in Odoo.
FROM_GOOGLE = "twp_google_contacts_from_google"


class ResPartner(models.Model):
    _inherit = "res.partner"

    def write(self, vals):
        result = super().write(vals)
        if not self.env.context.get(FROM_GOOGLE):
            self._twp_google_contacts_stamp(vals)
        return result

    def _twp_google_contacts_stamp(self, vals):
        """Note on each link when the synced groups changed in Odoo, so a conflict goes to the
        side that changed last. A company's new name is a change to its people's organization."""
        Link = self.env["twp.google.contacts.link"].sudo()
        groups = google_person.groups_written(vals)
        by_field = self.env["twp.google.contacts.field.map"]._groups_by_field()
        groups += [group for name in vals for group in by_field.get(name, ())]
        if groups:
            Link.search([("partner_id", "in", self.ids)])._stamp(groups)
        if "name" in vals:
            Link.search([("partner_id.parent_id", "in", self.ids)])._stamp(["organization"])
