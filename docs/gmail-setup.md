# Gmail setup: push notifications via Google Cloud Pub/Sub

This guide wires Gmail to email-notifier the way Google intends it: Gmail
publishes a tiny "something changed" event to a Cloud Pub/Sub topic, and
Pub/Sub immediately POSTs it to this app's `/gmail/push` endpoint. No
polling, no delays — a new email typically reaches Slack in a few seconds.

It assumes nothing about prior Google Cloud experience. Every step names the
exact button or menu item in the Google Cloud console. You do all of this
**once**; adding more Gmail accounts later only repeats steps 3 (test user),
8 (config block), and 9 (authorize), plus an `email-notifier watch` run
(step 10) to register the new account's watch.

What you will end up with:

| Piece | Created in | Used by |
| --- | --- | --- |
| A Google Cloud project | step 1 | everything below lives inside it |
| Gmail API + Pub/Sub API enabled | step 2 | `watch`, history reads, push delivery |
| OAuth consent screen (Testing) | step 3 | the browser consent in `email-notifier auth` |
| One OAuth client (`credentials.json`) | step 4 | shared by **every** Gmail account |
| One Pub/Sub topic (`gmail-notify`) | step 5 | Gmail publishes change events to it |
| Publisher grant for Gmail's service account | step 6 | without it, **nothing ever arrives** |
| One push subscription → your HTTPS URL | step 7 | delivers events to `email-notifier serve` |

> Everything here is free at this scale: Gmail API calls cost nothing, and
> Pub/Sub's free tier covers vastly more than a personal mailbox produces.

## 1. Create a Google Cloud project

1. Open <https://console.cloud.google.com> and sign in with any Google
   account (it does **not** have to be one of the Gmail accounts you want to
   monitor — the project is just the container).
2. Click the **project picker** in the top bar (it shows the current project
   name, or "Select a project"), then click **New project**.
3. Give it a name like `email-notifier` and click **Create**.
4. Note the **Project ID** shown under the name (something like
   `email-notifier-473112` — it may differ from the display name). You will
   need it for the topic name in step 5. You can always find it again in the
   project picker or on the console **Dashboard**.
5. Make sure the new project is **selected** in the project picker before
   continuing — every following step applies to the selected project.

## 2. Enable the Gmail API and the Cloud Pub/Sub API

1. Open the navigation menu (☰) → **APIs & Services** → **Library**.
2. Search for **Gmail API**, open it, click **Enable**.
3. Go back to the Library, search for **Cloud Pub/Sub API**, open it, click
   **Enable**.

Both must be enabled in the **same** project. If either is missing you will
get `403 accessNotConfigured` errors later.

## 3. Configure the OAuth consent screen

Gmail data is protected, so Google requires a consent screen even for an app
only you will use. In newer consoles this section is titled **Google Auth
Platform**; it is the same thing.

1. Navigation menu (☰) → **APIs & Services** → **OAuth consent screen**
   (you may be redirected to **Google Auth Platform** → click
   **Get started** if this is the project's first time).
2. Fill in the basics: **App name** (e.g. `email-notifier`) and a
   **User support email** (your own address).
3. **Audience / User type**: choose **External**. (Choose **Internal** only
   if you have a Google Workspace organization and *all* monitored addresses
   belong to it — Internal apps need no test users and never expire tokens
   after 7 days, so it is the better choice when available.)
4. Add a developer contact email, accept the terms, and finish the wizard.
5. **Add every Gmail address you plan to monitor as a test user.** On the
   consent screen / Google Auth Platform page, open **Audience**, find the
   **Test users** section, click **+ Add users**, and enter each address
   (e.g. `you@gmail.com`, `you.work@gmail.com`). Only listed test users can
   complete the consent flow in step 9.

### About "Testing" mode, the 7-day caveat, and verification

Leave the app's **Publishing status** as **Testing**. That is perfectly fine
for personal use — up to 100 test users can authorize it.

Two things to know:

- **Token expiry in Testing mode.** For External apps in Testing that
  request sensitive/restricted scopes, Google may expire refresh tokens
  after **7 days**. If that happens to you, `email-notifier` starts failing
  with `invalid_grant` and you must re-run `email-notifier auth <account>`
  weekly — annoying but harmless. In practice many personal setups keep
  working far longer; if yours does not, the fixes are: use **Internal**
  (Workspace only), or publish the app to **In production**.
- **The scary warning is expected.** This app requests exactly one scope,
  `https://www.googleapis.com/auth/gmail.readonly` (read-only access to your
  mail — it can never send, delete, or modify anything). Google classifies
  it as a **restricted** scope, so during consent you will see
  **"Google hasn't verified this app"**. As a test user of your own app you
  simply click **Continue** (or **Advanced** → **Go to email-notifier
  (unsafe)**). Full Google **verification** (a security review) is only
  required to offer the app to the general public; it is not required — and
  not worth it — for a self-hosted tool. If you click **Publish app** to
  escape the 7-day expiry, the app becomes "In production, unverified": the
  warning stays, your own accounts can still click through it, and nothing
  else changes.

## 4. Create the OAuth client (`credentials.json`)

1. Navigation menu (☰) → **APIs & Services** → **Credentials**.
2. Click **+ Create credentials** → **OAuth client ID**.
3. **Application type**: **Desktop app**. (This matters: `email-notifier
   auth` runs Google's installed-app flow, which spins up a temporary local
   redirect server on a random port — only the Desktop app type allows
   that.) Name it anything, e.g. `email-notifier-cli`.
4. Click **Create**, then **Download JSON** in the confirmation dialog (or
   later via the download icon next to the client in the credentials list).
5. Save the file as **`credentials.json` in the project root** (next to
   `config.toml`). That is the path the example config expects
   (`[gmail].credentials_file = "credentials.json"`; relative paths are
   resolved against the config file's directory).

> **One OAuth client serves every Gmail account.** Do not create a client
> per address. The same `credentials.json` identifies the *app*; which
> *mailbox* it may read is decided per account in step 9, producing one
> token file per account.

Treat `credentials.json` and everything under `tokens/` like passwords: do
not commit them.

## 5. Create the Pub/Sub topic

1. Navigation menu (☰) → **Pub/Sub** → **Topics**.
2. Click **Create topic**.
3. **Topic ID**: `gmail-notify` (any name works; the docs assume this one).
4. **Untick** "Add a default subscription" — you will create a proper push
   subscription yourself in step 7.
5. Click **Create**.

Record the topic's **full name**:

```
projects/<your-project-id>/topics/gmail-notify
```

using the Project ID from step 1 (the topic details page shows the full
name). This exact string goes into `[gmail].topic` in step 8 — the config
loader rejects anything not shaped like `projects/<id>/topics/<name>`. All
accounts share this one topic; Gmail tags each event with the mailbox it
came from, and the server routes it to the right account.

## 6. CRITICAL: let Gmail publish to your topic

Gmail publishes change events from a Google-owned service account, and it
has **no permission on your topic until you grant it**. Skipping this step
is the classic failure mode: `email-notifier watch` may even succeed, yet
**no notification will ever arrive**.

1. On **Pub/Sub** → **Topics**, click the `gmail-notify` topic to open it
   (or tick its checkbox and use the info panel — click **Show info panel**
   if it is hidden).
2. Open the **Permissions** tab and click **Add principal** (older consoles:
   **Add member**).
3. **New principals**:

   ```
   gmail-api-push@system.gserviceaccount.com
   ```

   (Exactly that address — it is the same for everyone; it is Google's
   Gmail push service account, not something in your project.)
4. **Assign roles** → **Pub/Sub** → **Pub/Sub Publisher**
   (`roles/pubsub.publisher`).
5. Click **Save**.

If this grant is missing, `users.watch` calls can fail with a 403 telling
you the topic doesn't permit publishing — or worse, appear fine while events
silently go nowhere.

## 7. Create the push subscription (Pub/Sub → your server)

Pub/Sub must deliver each event to your running server via an HTTPS POST.

### 7a. Decide your endpoint URL

The server (started with `email-notifier serve` in step 10) accepts pushes
at **`POST /gmail/push`** and answers health checks at **`GET /healthz`**.
The delivery endpoint must be **publicly reachable over HTTPS** — Pub/Sub
will not push to `http://` or to `localhost`.

Pick a shared secret (any random string, e.g. from `openssl rand -hex 16`)
and build the URL:

```
https://YOUR-HOST/gmail/push?token=YOUR-SECRET
```

The `token` query parameter is how the server tells real Pub/Sub traffic
from random internet noise: set the same secret as
`[gmail].pubsub_verification_token` in `config.toml` (or in the
`PUBSUB_VERIFICATION_TOKEN` environment variable, which takes precedence
over the file). When it is configured, the server answers **403** to any
push whose `?token=` does not match. If you leave it unset, the check is
skipped — fine for a first test, unwise for anything left running.

**Local testing with ngrok** (easiest way to try everything end to end):

```bash
ngrok http 8000
```

ngrok prints a forwarding line like
`https://a1b2c3d4.ngrok-free.app -> http://localhost:8000`. Use the
**https** URL as `YOUR-HOST`:

```
https://a1b2c3d4.ngrok-free.app/gmail/push?token=YOUR-SECRET
```

On ngrok's free plan the hostname changes every time you restart ngrok, so
you must edit the subscription's endpoint URL each time (Pub/Sub →
Subscriptions → your subscription → **Edit**).

**Production**: run `email-notifier serve` on any small always-on host with
a stable HTTPS URL — a tiny VM behind a reverse proxy (Caddy/nginx with
Let's Encrypt) or a container platform such as Cloud Run. The app is a
single lightweight FastAPI process; the smallest instance available is
plenty.

### 7b. Create the subscription

1. Navigation menu (☰) → **Pub/Sub** → **Subscriptions** → **Create
   subscription**.
2. **Subscription ID**: e.g. `gmail-notify-push`.
3. **Select a Cloud Pub/Sub topic**: pick
   `projects/<your-project-id>/topics/gmail-notify`.
4. **Delivery type**: **Push**.
5. **Endpoint URL**: the full URL from 7a, **including** `?token=...`.
6. **Acknowledgement deadline**: set it to **30–60 seconds** (the default
   10 s is tight: on each push the server calls Gmail's history and message
   APIs and then Slack before acknowledging).
7. Leave the rest at defaults (choosing **Exponential backoff** under Retry
   policy is a nice-to-have that spaces out retries) and click **Create**.

**How delivery and retries work** — and why the app depends on them: Pub/Sub
considers a push delivered only when the endpoint answers 2xx. On any other
response (or a timeout) it **redelivers** with backoff, for up to 7 days.
email-notifier leans on this deliberately: when Gmail or Slack calls fail
mid-push it returns **500**, so Pub/Sub retries and the notification is
delivered later instead of being lost. The flip side is at-least-once
delivery — after a failure a retry can occasionally repeat a Slack message
(see Troubleshooting). Malformed events and pushes for accounts not in your
config are acknowledged with 204 and dropped, since retrying them cannot
help.

## 8. Fill in `config.toml`

```bash
cp config.example.toml config.toml    # if you haven't already
```

Then edit the Gmail parts (Slack setup is covered in
[docs/slack-setup.md](slack-setup.md)):

```toml
state_file = "state.json"            # per-account history cursors live here

[slack]
webhook_url = "https://hooks.slack.com/services/..."   # or env SLACK_WEBHOOK_URL

[gmail]
credentials_file = "credentials.json"                  # from step 4
topic = "projects/your-project-id/topics/gmail-notify" # from step 5
pubsub_verification_token = "YOUR-SECRET"              # from step 7a; or env
                                                       # PUBSUB_VERIFICATION_TOKEN

# One block per Gmail account to monitor:
[[accounts]]
name = "personal"                    # your label; must be unique
email = "you@gmail.com"              # must match the mailbox exactly
provider = "gmail"
token_file = "tokens/personal.json"  # created by `email-notifier auth`
label_ids = ["INBOX"]                # which labels trigger notifications

[[accounts]]
name = "work"
email = "you.work@gmail.com"
provider = "gmail"
token_file = "tokens/work.json"
label_ids = ["INBOX"]
```

Notes:

- Relative paths (`credentials.json`, `tokens/…`, `state.json`) are resolved
  against the directory containing the config file.
- `email` is how the server routes incoming pushes (Gmail events carry only
  the mailbox address), so it must be the account's real address; names and
  emails must each be unique.
- `label_ids` restricts which mailbox changes generate pushes
  (`["INBOX"]` by default — i.e. archived mail and drafts don't notify).
  When checking what actually arrived, the app matches against **all** the
  labels in the list: a message is notified if it carries **any** of the
  configured labels (an empty list notifies on everything).

## 9. Authorize each account

For every `[[accounts]]` block, run (with the venv active, from the project
root):

```bash
email-notifier auth personal
email-notifier auth work
```

Each run opens a browser for Google's consent flow:

1. **Sign in with / choose the matching Gmail account** — the one named in
   that account's `email`. This is the easiest place to slip up when you
   monitor several accounts.
2. You'll see **"Google hasn't verified this app"** (expected — step 3):
   click **Continue**. If the address is not on the Test users list, Google
   blocks you with "access_denied" / "has not completed the Google
   verification process" — go back to step 3.5 and add it.
3. Approve the single permission, "View your email messages and settings"
   (the read-only scope).

On success the CLI prints `✓ Token saved to tokens/<name>.json`. It also
checks which address you *actually* authorized and prints a warning if it
differs from the configured `email` — if you see that warning, re-run `auth`
and pick the right Google account (or fix the config).

## 10. Start it up

```bash
email-notifier watch    # register Gmail → Pub/Sub push for every account
email-notifier serve    # receive the pushes (default: 0.0.0.0:8000)
```

`watch` calls Gmail's `users.watch` for each account and prints a table with
each account's starting cursor (`historyId`) and the watch's expiry
(roughly 7 days out). It also seeds `state.json`, so the very next email is
notified. Run `watch` **before** `serve` at least once — if the server
receives a push for an account with no stored cursor, it just adopts the
pushed position and starts notifying from the *following* change.

`serve` listens on port 8000 by default (`--host`/`--port` to change),
matching the `ngrok http 8000` example above. Check it responds:

```bash
curl https://YOUR-HOST/healthz     # → {"status":"ok"}
```

Now **send yourself a test email**. Within a few seconds you should see the
POST hit the server (ngrok's console shows it too) and the message appear in
Slack. If not, walk the Troubleshooting table below top to bottom.

## Keeping it running

**Watches expire.** A Gmail `users.watch` registration lasts about 7 days
and is *not* renewed automatically by Google. Re-run `email-notifier watch`
at least daily so the expiry never gets close — cron makes this painless:

```cron
0 6 * * * cd /path/to/email-notification && .venv/bin/email-notifier watch
```

(`watch` is cheap and idempotent; re-running it just renews the
registration. `--account NAME` renews a single account if you ever need
that.)

**Long outages lose the gap, gracefully.** Gmail keeps mailbox history for
only about a week. If the server has been down (or watches lapsed) long
enough that its stored cursor falls out of that window, the next push gets a
404 from the history API and the app automatically resynchronizes the
cursor to "now". Notifications resume immediately for new mail, but **mail
that arrived during the gap is not retro-notified** — the app fails safe
rather than failing forever. Short outages are fine: Pub/Sub retries
undelivered pushes for up to 7 days, and the history read catches every
message since the cursor even when pushes were dropped or coalesced.

## Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| No notifications at all | Work through the chain: (1) Is the watch registered? Run `email-notifier watch` — it must print a row for the account, and the expiry must be in the future. (2) Is the endpoint reachable? `curl https://YOUR-HOST/healthz` from outside; check the Pub/Sub subscription's metrics/unacked-message count in the console; if using ngrok, remember the URL changes on restart and the subscription must be edited to match. (3) Is the publisher role granted? Topic → Permissions must list `gmail-api-push@system.gserviceaccount.com` as **Pub/Sub Publisher** (step 6) — this is the most commonly skipped step. |
| Server logs show HTTP **403** on `/gmail/push` | Verification-token mismatch: the `?token=` in the subscription's endpoint URL differs from `[gmail].pubsub_verification_token` / `PUBSUB_VERIFICATION_TOKEN`. Fix one side to match the other (Pub/Sub keeps retrying, so the pending push comes through once fixed). |
| `invalid_grant` when the server or `watch` starts | The stored refresh token was expired or revoked — typical for External apps in **Testing** after 7 days (step 3). Re-run `email-notifier auth <account>`. Recurs weekly? Publish the app or switch to an Internal audience. |
| Push arrives (visible in server/ngrok logs) but no Slack message | Label filtering: when reading history the app keeps only messages carrying at least **one** of the labels in `label_ids` (`INBOX` by default), so mail that skips the inbox — filtered to "Skip Inbox", auto-archived — is invisible to it. Note also that only **newly arrived** messages are notified: a message that only *later* gains a watched label (e.g. rescued from Spam into INBOX) is not. Also check `email-notifier test-slack` to rule out the webhook, and remember the first push after deleting `state.json` only records a starting position. |
| Duplicate Slack messages for one email | Pub/Sub is at-least-once: after a failed or timed-out delivery (the app returns 500 whenever Gmail or Slack errored mid-push) the retry re-processes messages since the last saved cursor. Occasional duplicates after a hiccup are expected and preferred to dropped mail; raising the acknowledgement deadline (step 7b) reduces timeout-induced repeats. |
| `watch` fails with 403 mentioning the topic or `accessNotConfigured` | Either the publisher grant is missing (step 6) or an API was never enabled in this project (step 2). Also confirm `[gmail].topic` names the right project ID. |

Once mail flows, the remaining docs cover the Slack side
([slack-setup.md](slack-setup.md)) and adding other providers
([extending.md](extending.md)).
