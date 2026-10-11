from odoo import models


class BasePartnerMergeAutomaticWizard(models.TransientModel):
    _inherit = "base.partner.merge.automatic.wizard"

    def _update_foreign_keys(self, src_partners, dst_partner):
        # Before Odoo moves every reference across, so two links of one partner in the same
        # account do not make it drop them all.
        self.env["twp.google.contacts.link"]._partners_merged(src_partners, dst_partner)
        return super()._update_foreign_keys(src_partners, dst_partner)
