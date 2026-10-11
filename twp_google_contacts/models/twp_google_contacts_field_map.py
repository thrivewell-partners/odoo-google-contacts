import logging

from odoo import api, fields, models, tools
from odoo.exceptions import ValidationError
from odoo.tools import escape_psql, html2plaintext, plaintext2html

from . import google_person
from .res_config_settings import DIRECTIONS

_logger = logging.getLogger(__name__)

TARGETS = [("custom", "Google custom field"),
           ("birthday", "Birthday"),
           ("nickname", "Nickname"),
           ("phone", "Phone of a type"),
           ("email", "Email of a type"),
           ("external_id", "ID")]
FIELD_TYPES = ("char", "text", "html", "integer", "float", "monetary", "selection", "boolean",
               "date", "many2one", "many2many")
YES = ("yes", "true", "y", "1")


class Unreadable(ValueError):
    """A value from Google that this Odoo field cannot hold."""


class TwpGoogleContactsFieldMap(models.Model):
    """One Odoo contact field synced to one place in Google, for every connected account.

    Each map is a group of its own in the sync, named "f<id>", with the same rules as the fixed
    groups: the later change wins, or the Settings rule, and Odoo notes when the field changes.
    """

    _name = "twp.google.contacts.field.map"
    _description = "Odoo contact field synced to Google"
    _order = "sequence, id"

    sequence = fields.Integer(default=10)
    field_id = fields.Many2one(
        "ir.model.fields", string="Odoo field", required=True, ondelete="cascade",
        domain=[("model", "=", "res.partner"), ("store", "=", True),
                ("ttype", "in", FIELD_TYPES)])
    field_name = fields.Char(related="field_id.name")
    target = fields.Selection(TARGETS, string="In Google", required=True, default="custom")
    google_key = fields.Char(
        string="Name or type",
        help="A custom field: its name in Google. A phone or email: its type, such as mobile or "
             "home. An ID: its type, such as customer or account.")
    direction = fields.Selection(DIRECTIONS, required=True, default="both")
    create_missing = fields.Boolean(
        string="Make missing records",
        help="For a field that points to another record: when Google names one Odoo does not "
             "have, make it. Off, the value is left in Google and not taken.")

    @api.constrains("field_id", "target", "google_key", "direction")
    def _check_map(self):
        for row in self:
            ttype = row.field_id.ttype
            if ttype == "many2many" and row.direction != "to_google":
                raise ValidationError(self.env._(
                    "%(field)s holds several records, so it can only be sent to Google.",
                    field=row.field_id.field_description))
            if row.target == "birthday" and ttype not in ("date", "char"):
                raise ValidationError(self.env._("A birthday needs a date or text field."))
            if row.target in ("custom", "phone", "email", "external_id") and not (
                    row.google_key or "").strip():
                raise ValidationError(self.env._("Say the name or type it has in Google."))
            twins = self.search([("id", "!=", row.id), ("target", "=", row.target)])
            if row.target in ("birthday", "nickname") and twins:
                raise ValidationError(self.env._("Only one field can be the %(target)s.",
                                                 target=dict(TARGETS)[row.target].lower()))
            if any((t.google_key or "").strip().lower() == (row.google_key or "").strip().lower()
                   for t in twins):
                raise ValidationError(self.env._("Another field already syncs to that place."))

    @api.onchange("field_id", "target")
    def _onchange_google_key(self):
        if self.target == "custom" and self.field_id and not self.google_key:
            self.google_key = self.field_id.field_description

    @api.model_create_multi
    def create(self, vals_list):
        rows = super().create(vals_list)
        self._maps_changed()
        return rows

    def write(self, vals):
        result = super().write(vals)
        self._maps_changed()
        return result

    def unlink(self):
        result = super().unlink()
        self._maps_changed()
        return result

    def _maps_changed(self):
        """Every account compares all of Google again, so a new pairing is settled at once."""
        self.env.registry.clear_cache()
        self.env["twp.google.contacts.account"].sudo().search([]).write({"sync_token": False})

    @api.model
    @tools.ormcache()
    def _groups_by_field(self):
        """Odoo field name to the groups of the maps on it, for noting changes made in Odoo."""
        result = {}
        for row in self.sudo().search([]):
            result.setdefault(row.field_id.name, []).append(row._group())
        return {name: tuple(groups) for name, groups in result.items()}

    def _group(self):
        return "f%s" % self.id

    def _sends(self):
        return self.direction in ("both", "to_google")

    def _takes(self):
        return self.direction in ("both", "to_odoo")

    # Odoo's value as the text Google holds, and back.

    def _odoo_value(self, partner):
        name = self.field_id.name
        if name not in partner._fields:
            return None
        field, value = partner._fields[name], partner[name]
        if field.type == "boolean":
            return "Yes" if value else None
        if not value:
            return None
        if field.type == "html":
            return html2plaintext(value).strip() or None
        if field.type == "selection":
            return dict(field._description_selection(self.env)).get(value) or value
        if field.type == "date":
            return value.isoformat()
        if field.type == "many2one":
            return value.display_name
        if field.type == "many2many":
            return ", ".join(value.mapped("display_name"))
        return str(value).strip() or None

    def _odoo_write_value(self, partner, text):
        """The value to write in the partner's field for Google's text. Raises Unreadable."""
        field = partner._fields[self.field_id.name]
        if text is None:
            return False
        if field.type == "boolean":
            return text.strip().lower() in YES
        if field.type == "html":
            return plaintext2html(text)
        try:
            if field.type == "integer":
                return int(float(text))
            if field.type in ("float", "monetary"):
                return float(text)
            if field.type == "date":
                return fields.Date.to_date(text.strip()[:10])
        except ValueError:
            raise Unreadable(text) from None
        if field.type == "selection":
            for key, label in field._description_selection(self.env):
                if text.strip().lower() in (str(key).lower(), str(label).lower()):
                    return key
            raise Unreadable(text)
        if field.type == "many2one":
            Comodel = self.env[field.comodel_name].with_user(partner.env.user)
            rec_name = Comodel._rec_name or "name"
            found = Comodel.search([(rec_name, "=ilike", escape_psql(text.strip()))], limit=1)
            if not found and self.create_missing:
                found = Comodel.create({rec_name: text.strip()})
            if not found:
                raise Unreadable(text)
            return found.id
        if field.type == "many2many":
            raise Unreadable(text)  # sent one way only
        return text

    def _same(self, a, b):
        return google_person.same_mapped(self.target, a, b)
