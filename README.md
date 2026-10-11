# odoo-google-contacts

An Odoo 19 Community module that keeps a user's Google Contacts and Odoo's contacts in step, both
ways, through ThriveWell's verified Google app. No module code yet: this repo is at the proof stage.

| Module | Status | What it does |
|---|---|---|
| `twp_google_contacts` | not started | Each user connects their own Google account from Odoo. Contacts sync both ways on a schedule, with Google's lists merged rather than replaced, deletes off unless the admin turns them on, and only Google contacts under one label coming into Odoo |

This repo is the master copy. Releases are copied into each client's code repo, which is that
client's addons path on Oduflow.

## How it signs in to Google

The module never holds the Google app's secret. A ThriveWell service, ThriveWell Connect at
`connect.thrivewellag.com`, holds it along with each user's long-lived Google sign-in, and hands
the module a one-hour access token on request. The module then talks to Google directly. Contacts
never pass through ThriveWell Connect. Each client database needs a key issued by ThriveWell.

## Design

ThriveWell's design docs, in `work/design/`:

* `2026-10-10-google-contacts-connector-map.md` - what to build, the field crosswalk, the decisions
* `2026-10-10-google-people-api-reference.md` - Google's side, with the sync gotchas
* `2026-10-10-google-app-verification-reference.md` - what Google's review requires
* `2026-10-11-google-contacts-plan.md` - the order of work
