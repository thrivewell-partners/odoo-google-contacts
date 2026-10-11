"""The crosswalk between an Odoo partner and a Google person, as pure functions.

Odoo owns one item in each of Google's lists: one email, one phone, one work address, one website,
one organization. A sync replaces that item and leaves every other item as Google has it, because
Google replaces a whole list when one is sent (proof 4). Which item is Odoo's is decided by the value
Odoo last sent, kept on the link.
"""
import hashlib
import json
import re

ANCHOR_KEY = "thrivewell.anchor"

# Google's field for each group of partner values, in the order they are compared.
GROUPS = {
    "name": "names",
    "organization": "organizations",
    "email": "emailAddresses",
    "phone": "phoneNumbers",
    "address": "addresses",
    "website": "urls",
    "note": "biographies",
}
PERSON_FIELDS = ",".join(list(GROUPS.values()) + ["clientData", "memberships", "metadata"])

# Output-only parts of a list item. Sending them back is at best ignored.
OUTPUT_ONLY = ("metadata", "formattedType", "canonicalForm", "formattedValue")


def partner_values(partner, html2plaintext):
    """The partner as the plain values Odoo sends, one entry per group, None when empty."""
    given, family = split_name(partner.name or "")
    organization = None
    company = partner.parent_id.name if partner.parent_id else None
    if company or partner.function:
        organization = {"name": company, "title": partner.function or None}
    address = None
    if any((partner.street, partner.street2, partner.city, partner.zip, partner.state_id,
            partner.country_id)):
        address = {
            "streetAddress": partner.street or None,
            "extendedAddress": partner.street2 or None,
            "city": partner.city or None,
            "region": partner.state_id.name or None,
            "postalCode": partner.zip or None,
            "country": partner.country_id.name or None,
            "countryCode": partner.country_id.code or None,
        }
    note = html2plaintext(partner.comment).strip() if partner.comment else ""
    return {
        "name": {"givenName": given, "familyName": family} if given or family else None,
        "organization": organization,
        "email": (partner.email or "").strip() or None,
        "phone": (partner.phone or "").strip() or None,
        "address": address,
        "website": (partner.website or "").strip() or None,
        "note": note or None,
    }


def split_name(name):
    """'Ada King Lovelace' -> ('Ada King', 'Lovelace'). One word is a given name."""
    words = name.split()
    if len(words) < 2:
        return (words[0] if words else None), None
    return " ".join(words[:-1]), words[-1]


def anchor(db_uuid, partner_id):
    return "odoo:%s:res.partner:%s" % (db_uuid, partner_id)


def new_person(values, anchor_value):
    """The body of a createContact call."""
    person = {field: items for field, items in merge({}, values, {}).items() if items}
    person["clientData"] = [{"key": ANCHOR_KEY, "value": anchor_value}]
    return person


def changed_groups(values, pushed):
    return [group for group in GROUPS if values.get(group) != (pushed or {}).get(group)]


def merge(person, values, pushed, groups=None):
    """Return the Google lists for `groups`, with Odoo's item replaced and the rest kept.

    `person` is the contact as Google holds it now, `values` what Odoo has, `pushed` what Odoo
    sent last time. The result maps Google field names to the lists to send.
    """
    out = {}
    for group in groups or GROUPS:
        field = GROUPS[group]
        items = [_clean(item) for item in person.get(field, [])]
        out[field] = MERGERS[group](items, values.get(group), (pushed or {}).get(group))
    return out


def _clean(item):
    return {k: v for k, v in item.items() if k not in OUTPUT_ONLY}


def _merge_value(items, new, old, key, same, item_type="work"):
    """One plain value in a list: drop the item holding the old value, put the new one first."""
    if old is not None and (new is None or not same(old, new)):
        items = [i for i in items if not same(i.get(key) or "", old)]
    if new is not None and not any(same(i.get(key) or "", new) for i in items):
        items.insert(0, {key: new, "type": item_type})
    return items


def _same_text(a, b):
    return a.strip().lower() == b.strip().lower()


def _digits(number):
    return re.sub(r"\D", "", number)


def _same_phone(a, b):
    a, b = _digits(a), _digits(b)
    # A country code on one side only still matches: compare the national tail.
    return bool(a) and bool(b) and (a == b or a[-10:] == b[-10:])


def _merge_name(items, new, old):
    if new is None:
        return items if old is None else []
    rest = items[1:]
    first = dict(items[0]) if items else {}
    for derived in ("displayName", "displayNameLastFirst", "unstructuredName", "middleName"):
        first.pop(derived, None)
    first.update({k: v for k, v in new.items() if v})
    for key, value in new.items():
        if not value:
            first.pop(key, None)
    return [first] + rest


def _merge_organization(items, new, old):
    def ours(item):
        return old is not None and (item.get("name") or None) == old.get("name")

    index = next((i for i, item in enumerate(items) if ours(item)), None)
    if new is None:
        return [item for i, item in enumerate(items) if i != index]
    if index is None:
        return [_drop_empty(dict(new))] + items
    updated = dict(items[index])
    for key, value in new.items():
        if value:
            updated[key] = value
        else:
            updated.pop(key, None)
    return items[:index] + [updated] + items[index + 1:]


def _merge_address(items, new, old):
    def ours(item):
        keys = ("streetAddress", "city", "postalCode")
        return old is not None and all((item.get(k) or None) == old.get(k) for k in keys)

    index = next((i for i, item in enumerate(items) if ours(item)), None)
    kept = [item for i, item in enumerate(items) if i != index]
    if new is None:
        return kept
    address = _drop_empty(dict(new, type="work"))
    position = index if index is not None else 0
    return kept[:position] + [address] + kept[position:]


def _merge_note(items, new, old):
    return [{"value": new, "contentType": "TEXT_PLAIN"}] if new else []


def _drop_empty(item):
    return {k: v for k, v in item.items() if v}


MERGERS = {
    "name": _merge_name,
    "organization": _merge_organization,
    "email": lambda items, new, old: _merge_value(items, new, old, "value", _same_text),
    "phone": lambda items, new, old: _merge_value(items, new, old, "value", _same_phone),
    "address": _merge_address,
    "website": lambda items, new, old: _merge_value(items, new, old, "value", _same_text),
    "note": _merge_note,
}


def person_hash(person):
    """A hash of the fields Odoo syncs, as Google holds them. Equal hashes after a sync mean the
    change Google reports is Odoo's own write coming back (proof 5)."""
    projection = {}
    for field in GROUPS.values():
        projection[field] = [_clean(item) for item in person.get(field, [])]
    return hashlib.sha256(json.dumps(projection, sort_keys=True).encode()).hexdigest()[:32]
