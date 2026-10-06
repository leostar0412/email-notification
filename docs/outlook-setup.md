# Outlook setup

Connect a Microsoft 365 or Outlook.com mailbox. Graph posts to
`/outlook/push` when new inbox mail arrives. One Azure app registration is
shared by every Outlook account.

## 1. Register an app

1. Open <https://entra.microsoft.com> (Microsoft Entra admin center) and sign in.
2. Go to **Identity** > **Applications** > **App registrations** > **New registration**.
3. Name it `email-notifier`.
4. Supported account types: **Accounts in any organizational directory and personal Microsoft accounts**.
5. Redirect URI: platform **Public client/native (mobile & desktop)**, URI `http://localhost`.
6. Click **Register**. Copy the **Application (client) ID**.

Personal Outlook.com accounts and work accounts can both use tenant `common`.
Use your tenant ID instead if every mailbox is in one organization.

## 2. API permission

1. **API permissions** > **Add a permission** > **Microsoft Graph** > **Delegated**.
2. Add **Mail.Read** and **User.Read**.
3. Work or school tenants: an admin may need to click **Grant admin consent**. Personal Microsoft accounts do not.

## 3. Config

`email-notifier serve` must be reachable at a public `https://` URL before the
first `watch`. Graph calls that URL to validate the subscription.

```toml
[outlook]
client_id = "<application-client-id>"
tenant = "common"
notification_url = "https://your-host/outlook/push"
client_state = "change-me"

[[accounts]]
name = "work"
email = "you@outlook.com"
provider = "outlook"
token_file = "tokens/work-outlook.json"
```

`client_state` is a shared secret (128 characters maximum, including `:` and
the account name). `OUTLOOK_CLIENT_STATE` overrides the file.
`label_ids` is Gmail-only. Outlook always watches the inbox.

## 4. Authorize, watch, serve

```bash
email-notifier auth work
email-notifier serve    # leave this running
email-notifier watch    # in another terminal, the first time
```

The browser consent must be the mailbox in `email`. `watch` creates a Graph
subscription of about 3 days and prints the real expiry. Run
`email-notifier watch` at least daily so it does not lapse. The same cron that
renews Gmail watches covers Outlook.

## If it does not notify

| What you see | What to do |
| --- | --- |
| `watch` times out or says the notification URL is invalid | `serve` is not running, or the URL is not public HTTPS. |
| Pushes arrive and nothing is posted | `client_state` in the config does not match the subscription. Run `watch` again after fixing it. |
| Admin approval required during `auth` | A tenant admin must grant **Mail.Read**. |
| Token refresh errors | Run `email-notifier auth <account>` again. |
