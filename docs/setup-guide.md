# Google Contacts in Odoo: setting it up

This guide is for the people at your business who set up Google Contacts in Odoo and the people
who use it. ThriveWell does the setup with you, so most of it is ours to do. The parts that are
yours are marked.

## What it does

Each person who uses Odoo connects their own Google account. Their Odoo contacts then appear in
their Google Contacts, and so on their phone, under a label called "Odoo". Changes made on either
side reach the other within the hour, or straight away with **Sync now**.

The sync reads and changes only contacts. It never reads email, calendars or files. It looks at
the rest of your Google contacts only to avoid making a second copy of someone already there, and
doesn't bring them into Odoo unless you choose that in question 2 below.

## Before you start: your Google Workspace admin (yours)

If your business uses Google Workspace, your admin may have limited which outside apps can reach
company accounts. If so, they need to allow ours once:

1. Sign in to the Google Admin console.
2. Go to **Security**, then **Access and data control**, then **API controls**.
3. Under third-party app access, add an app by its **client ID**. ThriveWell gives you the ID.
4. Mark it **Trusted**.

Skip this and a user who presses Connect sees a message saying the admin hasn't allowed the app.
Nothing breaks; the step just has to happen first. People using a personal Gmail account need no
admin step.

## Four questions we'll ask you (yours to answer)

Not every business uses Google Contacts and Odoo the same way. Your answers become Odoo's settings.

**1. Which way should contacts flow?**
- **Both ways** (most businesses): a change on either side reaches the other.
- **Odoo to Google only:** Google keeps a copy of Odoo's contacts. A detail someone edits in
  Google is put back the way Odoo has it.
- **Google to Odoo only:** Odoo keeps a copy of your Google contacts and sends nothing back.

Each person can change their own direction later, on their Google Contacts tab.

**2. Whose contacts go to whose Google?** A contact's owner in Odoo is its Salesperson.
- Everyone gets every contact (people with an email or phone, or only contacts with chosen tags).
- Each person gets only the contacts they own.
- Each person gets theirs, plus contacts with no owner.

A contact that changes hands leaves the old owner's "Odoo" label. It is never deleted from their
Google. We also ask whether Odoo should bring in only the contacts under the "Odoo" label (our
advice) or every contact in each person's Google.

**3. What should happen to tags and labels?** By default each Odoo tag on a contact becomes a
Google label of the same name, and the person's own Google labels are left alone. You can pair
any tag with a label and say which way it flows, or leave tags out entirely.

**4. Are there other details you want in Google?** Name, company and job title, email, phone,
address, website, notes and photo sync by default. Any of them can be left out, notes being the
usual one. Any other contact field can be added, including one made in Studio: a mobile number,
a birthday, a customer number, or a Google custom field with the name you choose. Each person's
own custom fields in Google are never touched.

## What ThriveWell sets up

- Installs the module and enters your ThriveWell Connect key in **Settings**, under
  **Integrations**, **Google Contacts**. The key belongs to your database. Don't share it.
- Sets your four answers in the same place.
- Connects first with you, checks the first sync together, and shows you the sync log.

## Connecting (each person)

1. In Odoo, open the menu under your picture, top right, choose your preferences, and open the
   **Google Contacts** tab.
2. Press **Connect**, read what the app reads and changes, and press **Continue to Google**.
3. Sign in to Google. **Tick the Contacts box** on Google's screen. Without it, Odoo can't reach
   your contacts and asks you to connect again.

The first sync sends your Odoo contacts to Google under the "Odoo" label. If you already have
contacts under that label, Odoo lists them for you to review first: **Accept** links one to the
Odoo contact it matches, **Add as new** brings it in, and **Keep out of Odoo** leaves it alone
for good. After that first review, new contacts under the label come in on their own, unless they
look like a contact Odoo already has.

**Keep personal contacts out of the "Odoo" label.** Anything brought into Odoo can be seen by
everyone who uses Odoo. The label is how you choose what comes in.

## Day to day

- **The sync runs every hour.** **Sync now** on your Google Contacts tab runs it at once and
  tells you what changed.
- **When both sides changed the same detail,** the more recent change wins. We can set it to
  always keep Odoo's or always keep Google's instead.
- **Deleting in Google** only stops that contact syncing. We can set Odoo to archive it instead.
  Archiving or deleting in Odoo takes the contact out of the "Odoo" label and never deletes it
  from Google.
- **Merging two contacts in Google** puts the pair on your review list, where **Merge in Odoo**
  merges them in Odoo too.
- **With Discuss installed,** each contact's chatter notes what Google changed, added, linked or
  archived, and whose Google it came from.

## When something goes wrong

- **Your administrator has a sync log**, in **Settings**, **Google Contacts**, **Sync log**. It
  lists every sync for 90 days: what changed each way, and each contact that failed, with
  Google's reason.
- **Google limits how fast one account can change.** A large first sync can run into that limit.
  Odoo spaces its changes out and, if Google still asks it to slow down, stops, keeps what it
  did, and carries on a few minutes later. A sync too large for one run carries on the same way.
- **"Needs reconnecting"** means Google no longer accepts the connection: the person removed the
  app's access, changed their password, or the Contacts box wasn't ticked. Press **Connect**
  again. Nothing synced is lost.
- **If ThriveWell Connect can't be reached,** the sync pauses and nothing is lost. It picks up on
  the next run.

## Disconnecting or removing it

- **Disconnect** on the Google Contacts tab gives up that person's Google access. Their contacts
  stay in Google and in Odoo, and stop syncing.
- **Uninstalling the module** gives up every person's Google access first and removes its
  settings and everything it kept in Odoo: the links, the review list and the sync log. If
  ThriveWell Connect can't be reached at that moment, the uninstall stops rather than leave access
  behind. Try again a few minutes later.
- **What stays in Google:** the contacts themselves, the "Odoo" label, and a hidden marker on
  each contact that only this app reads. If you install it again later, the marker lets Odoo find
  its contacts instead of creating doubles.

## Help

Contact ThriveWell. We set it up with you, and we look into it with you when it misbehaves.
