# Slack setup: creating the incoming webhook

email-notifier posts every notification through **one Slack incoming
webhook**. An incoming webhook is a unique URL that Slack gives you; anything
POSTed to that URL appears as a message in one specific channel of your
workspace.

You only ever need **one** webhook, no matter how many email accounts you
configure — every `[[accounts]]` entry in `config.toml` posts to the same
channel through this single URL. Adding a third or tenth Gmail account later
requires no Slack changes at all.

## Prerequisites

- A Slack workspace you belong to.
- Permission to install apps in that workspace. In many workspaces anyone can
  do this; some restrict it — see
  [Troubleshooting](#troubleshooting) if you hit an approval screen.
- A channel for the notifications. If you want a dedicated one, create it in
  Slack first (e.g. `#email-alerts`) so it shows up in the picker in step 7.

## Step 1–8: create the app and webhook

1. Open <https://api.slack.com/apps> in your browser and sign in to your
   Slack workspace if prompted.

2. Click the green **Create New App** button.

3. In the dialog, choose **From scratch**.

4. Fill in the form:
   - **App Name** — anything you like; `Email Notifier` works well. This name
     appears as the "sender" of the messages in Slack.
   - **Pick a workspace to develop your app in** — select the workspace whose
     channel should receive the notifications.

   Click **Create App**. You land on the app's **Basic Information** page.

5. In the left sidebar, under **Features**, click **Incoming Webhooks**.

6. Flip the **Activate Incoming Webhooks** toggle to **On** (top right of the
   page). A new section titled **Webhook URLs for Your Workspace** appears
   below.

7. Scroll down and click **Add New Webhook to Workspace**. Slack shows an
   authorization screen: *"Email Notifier is requesting permission to access
   the … workspace"* with a channel dropdown. Pick the channel that should
   receive the email notifications and click **Allow**.

8. You are returned to the Incoming Webhooks page. Your new webhook is listed
   under **Webhook URLs for Your Workspace**. Click **Copy** next to it. The
   URL looks like:

   ```
   https://hooks.slack.com/services/<team-id>/<webhook-id>/<secret>
   ```

That URL is everything email-notifier needs from Slack.

## Step 9: give the URL to email-notifier

There are two ways; pick one.

**Option A — config file.** Put it in `config.toml` (copied from
`config.example.toml`):

```toml
[slack]
webhook_url = "https://hooks.slack.com/services/<team-id>/<webhook-id>/<secret>"
```

**Option B — environment variable.** Export it in the environment where you
run the app:

```bash
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/<team-id>/<webhook-id>/<secret>"
```

If both are set, **the environment variable wins** — `SLACK_WEBHOOK_URL`
overrides `[slack].webhook_url`. Either way the value must start with
`https://`, or the app refuses to start with a configuration error.

## Step 10: verify it works

With the config in place, send a test message:

```bash
email-notifier test-slack
```

You should see:

```
✓ Test message sent — check your Slack channel.
```

and, in the chosen channel, a message from *Email Notifier*:
"👋 email-notifier is connected to this channel."

(If your `config.toml` is not in the current directory, add
`--config /path/to/config.toml` — that flag works on every command.)

To rule out any config issue and test the webhook itself, you can also POST
to it directly with `curl`:

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -d '{"text": "Hello from curl"}' \
  https://hooks.slack.com/services/<team-id>/<webhook-id>/<secret>
```

A healthy webhook replies with HTTP 200 and the body `ok`. Anything else —
see [Troubleshooting](#troubleshooting).

## Security: treat the URL as a secret

The webhook URL **is a credential**. There is no separate password: anyone
who has the URL can post arbitrary messages into your channel. Handle it
accordingly:

- **Never commit it.** `config.toml` is listed in this repository's
  `.gitignore` precisely so the URL (and your other secrets) stay out of
  version control. Only the placeholder in `config.example.toml` is
  committed.
- Prefer the `SLACK_WEBHOOK_URL` environment variable on shared or deployed
  machines, so the secret never touches disk.
- Don't paste it into chat, issues, or logs.
- **If it leaks, rotate it:** on the app's **Incoming Webhooks** page, delete
  the compromised webhook (trash icon next to it), click **Add New Webhook to
  Workspace** again to create a fresh URL for the same channel, and update
  your config. The old URL stops working immediately.

## Troubleshooting

Errors from `email-notifier test-slack` appear as
`Slack webhook returned <status>: <body>` (exit code 1). The body is Slack's
short error string:

- **`no_service` or `channel_is_archived` (HTTP 404 / 410)** — the webhook no
  longer points anywhere: the channel was deleted or archived, the webhook
  was removed, or the app was uninstalled from the workspace. Create a new
  webhook (steps 6–8) for a live channel and update your config.
- **`invalid_payload` (HTTP 400)** — the JSON body was malformed. With
  `email-notifier` itself this shouldn't happen; if you see it from `curl`,
  check your quoting — the `-d` argument must be valid JSON like
  `'{"text": "hi"}'`.
- **"This app requires permission to be installed" / request sent to
  admins** — your workspace restricts app installs. Clicking **Allow** in
  step 7 sends an approval request to the workspace admins instead of
  creating the webhook. Ask an admin to approve *Email Notifier* (they'll
  find it under **Manage apps**), then repeat step 7.
- **`Could not reach Slack: …`** — a network problem between your machine
  and `hooks.slack.com` (DNS, proxy, firewall). The URL wasn't rejected;
  the request never got through.
- **`Configuration error: No Slack webhook configured …` (exit code 2)** —
  neither `[slack].webhook_url` nor `SLACK_WEBHOOK_URL` is set; revisit
  step 9.

## Next steps

Slack is done. Continue with [gmail-setup.md](gmail-setup.md) to create the
Google Cloud OAuth client and Pub/Sub topic, then authorize your accounts
with `email-notifier auth <account>`.
