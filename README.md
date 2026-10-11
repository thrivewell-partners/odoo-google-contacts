# odoo-google-contacts

An Odoo 19 Community module that keeps a user's Google Contacts and Odoo's contacts in step, both
ways, through ThriveWell's verified Google app. Built in milestones; the status below says how far.

| Module | Status | What it does |
|---|---|---|
| `twp_google_contacts` | M5 of 6: connect, both directions, the two-way rules, and each client's choices | Each user connects their own Google account from Odoo. Contacts sync both ways, or one way, on a schedule, with Google's lists merged rather than replaced, deletes off unless the admin turns them on, and only Google contacts under one label coming into Odoo |

This repo is the master copy. Releases are copied into each client's code repo, which is that
client's addons path on Oduflow.

## How it signs in to Google

The module never holds the Google app's secret. A ThriveWell service, ThriveWell Connect at
`connect.thrivewellag.com`, holds it along with each user's long-lived Google sign-in, and hands
the module a one-hour access token on request. The module then talks to Google directly. Contacts
never pass through ThriveWell Connect. Each client database needs a key issued by ThriveWell.

An administrator enters that key in Settings, under Integrations. Each user then connects from their
own preferences, on the Google Contacts tab, after a screen that says what the connection does.
On Google's screen the Contacts permission is a checkbox of its own, and it must be ticked.

## What comes into Odoo

Only Google contacts under the label (default "Odoo") come into Odoo. The first time, each one
waits on a review list, reached from the user's preferences, with the Odoo contact it most likely
is: the same email, then the same phone number. The user accepts the match, picks another, adds it
as new, or keeps it out. After that review, new contacts under the label that match nothing are
added on their own, and any that look like a contact Odoo already has still wait for the user.

## When both sides change

* **The later change wins**, for each group of details (name, company and job title, email, phone,
  address, website, notes, photo). Odoo notes when each group changes; Google gives the time the
  contact last changed. Settings can instead always keep Odoo's, or always Google's. A group only
  one side changed simply comes across.
* **Deletes.** A contact deleted in Google stops syncing and stays in Odoo, unless Settings turn on
  "Archive in Odoo when deleted in Google". A contact archived or deleted in Odoo is taken out of
  Odoo's labels in Google and is never deleted there; brought back from the archive, it picks up
  what changed in Google meanwhile.
* **Google merges.** Google reports a merge as a delete of one contact and a change to the other.
  When the other one now holds the lost contact's email or phone, it is treated as a merge, never
  a delete. Two linked contacts merged in Google wait on the review list, where "Merge in Odoo"
  opens Odoo's own merge. A contact merged into an unlinked duplicate of the same name is followed.
* **Photos** sync both ways. Google's generated initials are not a photo.

## Each client's choices

Not every client uses Google Contacts and Odoo the same way, so these are settings, not code.

* **Which way.** Both ways, Odoo to Google only, or Google to Odoo only. Each user picks their own
  on their Google Contacts tab, starting from the Settings default. Odoo to Google only keeps
  Google as a copy: nothing comes into Odoo, and a synced detail edited in Google is put back.
  Google to Odoo only keeps Odoo as a copy: Google's changes always win, and Odoo sends nothing but
  its hidden anchor. Settings also choose whether Google contacts come in only from the label or
  all of them; every Odoo user sees what comes in.
* **Whose contacts.** A contact's owner is its Salesperson. Each user chooses whether their Google
  gets everyone in scope, only contacts they own, or theirs plus those with no owner. A contact
  handed to someone else leaves the first user's labels and is never deleted. A contact brought in
  from Google is owned by the user it came from. Before creating a contact in Google, Odoo looks
  for one already there with the same email or phone and links it instead, taking nothing from it
  into Odoo the first time.
* **Which details.** One box per detail in Settings; the name always syncs.
* **Tags and labels.** A list of rules pairs an Odoo tag with a Google label and a direction. With
  a rule both ways, adding a contact to the label in Google adds the tag in Odoo, and taking it out
  removes it. Tags with no rule become labels of their full name, or are not sent; Google labels
  with no rule are left alone, or become tags.
* **Other fields.** A list pairs any Odoo contact field, Studio's included, with a place in Google:
  a custom field, the birthday, a nickname, a phone or email of a given type (such as the mobile
  Odoo 19 no longer has), or an ID. Odoo writes only the custom fields named there.

## Design

ThriveWell's design docs, in `work/design/`:

* `2026-10-10-google-contacts-connector-map.md` - what to build, the field crosswalk, the decisions
* `2026-10-10-google-people-api-reference.md` - Google's side, with the sync gotchas
* `2026-10-10-google-app-verification-reference.md` - what Google's review requires
* `2026-10-11-google-contacts-plan.md` - the order of work
