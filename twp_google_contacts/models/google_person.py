"""The crosswalk between an Odoo partner and a Google person, as pure functions.

Odoo owns one item in each of Google's lists: one email, one phone, one work address, one website,
one organization. A sync replaces that item and leaves every other item as Google has it, because
Google replaces a whole list when one is sent (proof 4). Which item is Odoo's is decided by the value
Odoo last sent, kept on the link. Coming back, the same item is the one read into the partner.
"""
import hashlib
import json
import re
from datetime import datetime

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
# Google's field for each place an Odoo field can be mapped to (M5).
MAPPED_FIELDS = {
    "custom": "userDefined",
    "birthday": "birthdays",
    "nickname": "nicknames",
    "phone": "phoneNumbers",
    "email": "emailAddresses",
    "external_id": "externalIds",
}
EXTRA_FIELDS = ["userDefined", "birthdays", "nicknames", "externalIds"]
PERSON_FIELDS = ",".join(list(GROUPS.values()) + EXTRA_FIELDS
                         + ["clientData", "memberships", "metadata", "photos"])

# The partner fields behind each group, and behind the photo, to tell which a write changed.
PARTNER_FIELDS = {
    "name": {"name"},
    "organization": {"parent_id", "function"},
    "email": {"email"},
    "phone": {"phone"},
    "address": {"street", "street2", "city", "zip", "state_id", "country_id"},
    "website": {"website"},
    "note": {"comment"},
    "photo": {"image_1920"},
}

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


def with_anchor(person, anchor_value):
    """The person's clientData with this database's anchor set. Other apps' keys, and other
    databases' anchors, are kept, because Google replaces the whole list on update."""
    prefix = anchor_value.rsplit(":", 1)[0] + ":"
    others = [_clean(item) for item in person.get("clientData", [])
              if not (item.get("key") == ANCHOR_KEY
                      and (item.get("value") or "").startswith(prefix))]
    return others + [{"key": ANCHOR_KEY, "value": anchor_value}]


def anchored_partner_id(person, db_uuid):
    """The partner id in this database's anchor on the person, or None. An anchor written by
    another database, such as the one this was copied from, is not ours."""
    prefix = "odoo:%s:res.partner:" % db_uuid
    for item in person.get("clientData", []):
        value = item.get("value") or ""
        if item.get("key") == ANCHOR_KEY and value.startswith(prefix):
            tail = value[len(prefix):]
            if tail.isdigit():
                return int(tail)
    return None


def in_label(person, label_resource_name):
    return any(m.get("contactGroupMembership", {}).get("contactGroupResourceName")
               == label_resource_name for m in person.get("memberships", []))


def label_resource_names(person):
    return {m["contactGroupMembership"]["contactGroupResourceName"]
            for m in person.get("memberships", [])
            if m.get("contactGroupMembership", {}).get("contactGroupResourceName")}


def google_values(person, pushed=None, claimed=None):
    """The person in the shape of partner_values: Odoo's item from each of Google's lists.

    Odoo's item is the one still holding what Odoo last sent, so a value Google only reformatted
    reads as unchanged. When that item is gone, Google's primary item stands in, then the first,
    leaving out the types `claimed` by mapped fields, such as a mobile phone mapped to its own
    Odoo field.
    """
    pushed = pushed or {}
    claimed = claimed or {}
    names = person.get("names") or [{}]
    given, family = names[0].get("givenName") or None, names[0].get("familyName") or None
    values = {"name": {"givenName": given, "familyName": family} if given or family else None}

    old = pushed.get("organization")
    org = _pick(person.get("organizations"), lambda i: _ours_organization(i, old))
    values["organization"] = None
    if org and (org.get("name") or org.get("title")):
        values["organization"] = {"name": org.get("name") or None, "title": org.get("title") or None}

    for group, same in (("email", _same_text), ("phone", _same_phone), ("website", _same_url)):
        old = pushed.get(group)
        items = person.get(GROUPS[group]) or []
        free = [i for i in items if (i.get("type") or "").lower() not in claimed.get(group, ())]
        item = next((i for i in items if old is not None and same(i.get("value") or "", old)),
                    None) or _pick(free, None)
        value = ((item or {}).get("value") or "").strip() or None
        values[group] = old if value and old is not None and same(value, old) else value

    old = pushed.get("address")
    item = _pick(person.get("addresses"), lambda i: _ours_address(i, old),
                 lambda i: i.get("type") == "work")
    address = {key: (item or {}).get(key) or None for key in ADDRESS_KEYS}
    values["address"] = address if any(address.values()) else None

    notes = person.get("biographies") or [{}]
    values["note"] = (notes[0].get("value") or "").strip() or None
    return values


ADDRESS_KEYS = ("streetAddress", "extendedAddress", "city", "region", "postalCode", "country",
                "countryCode")


def _pick(items, ours, preferred=None):
    """Odoo's item if it is still there, else the preferred one, else the primary, else the first."""
    items = items or []
    for test in (ours, preferred, lambda i: i.get("metadata", {}).get("primary")):
        found = next((i for i in items if test and test(i)), None)
        if found:
            return found
    return items[0] if items else None


def same_value(group, a, b):
    """Whether two values of a group say the same thing, written however each side writes it.
    Addresses need Odoo's country and state names, so the account compares those."""
    if a is None or b is None:
        return a is b
    if group == "name":
        return " ".join(filter(None, (a.get("givenName"), a.get("familyName")))) == " ".join(
            filter(None, (b.get("givenName"), b.get("familyName"))))
    same = {"email": _same_text, "phone": _same_phone, "website": _same_url}.get(group)
    if same:
        return same(a, b)
    if group == "note":
        return a.strip() == b.strip()
    if group == "organization":
        # Odoo joins a company by its name ignoring case, so case is not a difference.
        return (_lower(a.get("name")), a.get("title")) == (_lower(b.get("name")), b.get("title"))
    return a == b


def _lower(text):
    return (text or "").strip().lower() or None


def changed_groups(values, pushed):
    """The groups, fixed and mapped, whose value is not what Odoo last sent."""
    return [group for group in values if values[group] != (pushed or {}).get(group)]


def merge(person, values, pushed, groups=None):
    """Return the Google lists for `groups`, with Odoo's item replaced and the rest kept.

    `person` is the contact as Google holds it now, `values` what Odoo has, `pushed` what Odoo
    sent last time. The result maps Google field names to the lists to send.
    """
    out = {}
    for group in GROUPS if groups is None else [g for g in groups if g in GROUPS]:
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


def phone_tail(number):
    """The last ten digits a phone number is matched on, or None when it is too short to be one."""
    digits = _digits(number)
    return digits[-10:] if len(digits) >= 7 else None


def _same_url(a, b):
    """Odoo adds http:// to a website it is given, so the scheme and a closing slash are not a
    difference."""
    def bare(url):
        return re.sub(r"^[a-z]+://", "", url.strip().lower()).rstrip("/")
    return bare(a) == bare(b)


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


def _ours_organization(item, old):
    """Odoo's organization is found by the company name it sent, or by the job title when it sent
    no company name."""
    if old is None:
        return False
    if old.get("name"):
        return _lower(item.get("name")) == _lower(old["name"])
    return bool(old.get("title")) and (item.get("title") or None) == old["title"]


def _merge_organization(items, new, old):
    index = next((i for i, item in enumerate(items) if _ours_organization(item, old)), None)
    if new is None:
        return [item for i, item in enumerate(items) if i != index]
    if index is None:
        return [_drop_empty(dict(new))] + items
    updated = dict(items[index])
    for key, value in new.items():
        if value == (old or {}).get(key):
            continue  # Odoo did not change this part, so Google's stands
        if value:
            updated[key] = value
        else:
            updated.pop(key, None)
    return items[:index] + [updated] + items[index + 1:]


def _ours_address(item, old):
    keys = ("streetAddress", "city", "postalCode")
    return old is not None and all((item.get(k) or None) == old.get(k) for k in keys)


def _merge_address(items, new, old):
    index = next((i for i, item in enumerate(items) if _ours_address(item, old)), None)
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
    "website": lambda items, new, old: _merge_value(items, new, old, "value", _same_url),
    "note": _merge_note,
}


def groups_written(vals):
    """The groups, and "photo", whose partner fields a write touches."""
    return [group for group, names in PARTNER_FIELDS.items() if names & set(vals)]


def update_time(person):
    """When the contact last changed in Google, as a naive UTC datetime, or None."""
    for source in person.get("metadata", {}).get("sources", []):
        if source.get("type") == "CONTACT" and source.get("updateTime"):
            # RFC 3339 with up to nine decimals: keep the seconds.
            return datetime.strptime(source["updateTime"][:19], "%Y-%m-%dT%H:%M:%S")
    return None


def photo_url(person):
    """The contact's own photo, or None. Google's generated initials are not a photo."""
    return next((photo.get("url") for photo in person.get("photos", [])
                 if photo.get("url") and not photo.get("default")), None)


def photo_download_url(url, size=1024):
    """Google's photo address at the given size: its "=s100" ending changes, or "sz" is added."""
    if re.search(r"=s\d+[^/=?]*$", url):
        return re.sub(r"=s\d+[^/=?]*$", "=s%s" % size, url)
    return url + ("&" if "?" in url else "?") + "sz=%s" % size


def contact_keys(emails=(), phones=()):
    """What makes two contacts look like one person: their emails, ignoring case, and the last ten
    digits of their phone numbers."""
    keys = {"e:" + e.strip().lower() for e in emails if e and e.strip()}
    keys |= {"p:" + tail for tail in map(phone_tail, filter(None, phones)) if tail}
    return keys


def person_keys(person):
    return contact_keys([i.get("value") for i in person.get("emailAddresses", [])],
                        [i.get("value") for i in person.get("phoneNumbers", [])])


def same_name(a, b):
    def words(name):
        return " ".join(filter(None, ((name or {}).get("givenName"),
                                      (name or {}).get("familyName")))).lower().split()
    return bool(words(a)) and words(a) == words(b)


def person_hash(person):
    """A hash of the fields Odoo syncs, and the photo, as Google holds them. Equal hashes after a
    sync mean the change Google reports is Odoo's own write coming back (proof 5)."""
    projection = {}
    for field in list(GROUPS.values()) + EXTRA_FIELDS:
        projection[field] = [_clean(item) for item in person.get(field, [])]
    projection["photo"] = photo_url(person)
    return hashlib.sha256(json.dumps(projection, sort_keys=True).encode()).hexdigest()[:32]


# Mapped fields: one Odoo field in one place in Google.

def _mapped_index(items, target, key):
    """Where the mapped value sits in Google's list, or None."""
    key = (key or "").strip().lower()
    for index, item in enumerate(items):
        if target == "custom":
            found = (item.get("key") or "").strip().lower() == key
        elif target == "nickname":
            found = (item.get("type") or "DEFAULT") == "DEFAULT"
        else:
            found = (item.get("type") or "").strip().lower() == key
        if found:
            return index
    return None


def mapped_value(person, target, key):
    """The text Google holds in a mapped place, or None. A birthday reads as YYYY-MM-DD, with
    0000 when Google has no year."""
    items = person.get(MAPPED_FIELDS[target]) or []
    if target == "birthday":
        date = (items[0].get("date") if items else None) or {}
        if not date.get("month"):
            return None
        return "%04d-%02d-%02d" % (date.get("year") or 0, date["month"], date.get("day") or 0)
    index = _mapped_index(items, target, key)
    value = items[index].get("value") if index is not None else None
    return (value or "").strip() or None


def merge_mapped(items, target, key, new):
    """Google's list with the mapped value replaced, added or taken away, and every other item
    kept: the user's own custom fields, other phones, other IDs."""
    items = [_clean(item) for item in items]
    if target == "birthday":
        if not new:
            return []
        try:
            year, month, day = (int(part) for part in new.strip()[:10].split("-"))
        except ValueError:
            return items
        date = {"month": month, "day": day}
        if year:
            date["year"] = year
        return [{"date": date}]
    index = _mapped_index(items, target, key)
    if new is None:
        return [item for i, item in enumerate(items) if i != index]
    if target == "custom":
        item = {"key": key, "value": new}
    elif target == "nickname":
        item = {"value": new}
    else:
        item = {"value": new, "type": key}
    if index is None:
        return items + [item]
    return items[:index] + [dict(items[index], **item)] + items[index + 1:]


def same_mapped(target, a, b):
    if a is None or b is None:
        return a is b
    if target == "phone":
        return _same_phone(a, b)
    if target == "email":
        return _same_text(a, b)
    return a.strip() == b.strip()
