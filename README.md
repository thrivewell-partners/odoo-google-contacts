# odoo-google-contacts

An Odoo 19 Community module that keeps a user's Google Contacts and Odoo's contacts in step, both
ways, through ThriveWell's verified Google app. Built in milestones; the status below says how far.

| Module | Status | What it does |
|---|---|---|
| `twp_google_contacts` | M3 of 5: connect, Odoo to Google, and Google to Odoo | Each user connects their own Google account from Odoo. Contacts sync both ways on a schedule, with Google's lists merged rather than replaced, deletes off unless the admin turns them on, and only Google contacts under one label coming into Odoo |

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

## Design

ThriveWell's design docs, in `work/design/`:

* `2026-10-10-google-contacts-connector-map.md` - what to build, the field crosswalk, the decisions
* `2026-10-10-google-people-api-reference.md` - Google's side, with the sync gotchas
* `2026-10-10-google-app-verification-reference.md` - what Google's review requires
* `2026-10-11-google-contacts-plan.md` - the order of work
