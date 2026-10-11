from odoo import api, fields, models

from .res_config_settings import DIRECTIONS


class TwpGoogleContactsTagRule(models.Model):
    """How one Odoo tag and one Google label sync, for every connected account."""

    _name = "twp.google.contacts.tag.rule"
    _description = "Odoo tag and Google label rule"
    _order = "label_name"

    category_id = fields.Many2one("res.partner.category", string="Odoo tag", required=True,
                                  ondelete="cascade")
    label_name = fields.Char(string="Google label", required=True,
                             compute="_compute_label_name", store=True, readonly=False)
    direction = fields.Selection(DIRECTIONS, required=True, default="both")

    _category_unique = models.Constraint("UNIQUE(category_id)", "A tag has one rule.")
    _label_unique = models.Constraint("UNIQUE(label_name)", "A Google label has one rule.")

    @api.depends("category_id")
    def _compute_label_name(self):
        for rule in self:
            if not rule.label_name and rule.category_id:
                rule.label_name = rule.category_id.display_name

    @api.model_create_multi
    def create(self, vals_list):
        rules = super().create(vals_list)
        self.env["twp.google.contacts.account"]._labels_changed()
        return rules

    def write(self, vals):
        result = super().write(vals)
        self.env["twp.google.contacts.account"]._labels_changed()
        return result

    def unlink(self):
        result = super().unlink()
        self.env["twp.google.contacts.account"]._labels_changed()
        return result
