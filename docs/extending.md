# Extending email-notifier with a new provider

This guide walks through adding support for another email service. Gmail is
built in; Outlook (Microsoft 365 / Graph) is used as the running example
because it is the planned next provider. Everything here matches the code in
`src/email_notifier/` — read alongside `providers/base.py`, `providers/gmail.py`,
and `server.py`.

## Architecture in brief

One **provider instance handles one configured account** (one `[[accounts]]`
block in `config.toml`). The server builds and caches a provider per account
name the first time a push arrives for it.

The flow for every notification, regardless of provider:

```
mail service push ──► provider-specific HTTP route (e.g. POST /gmail/push)
                          │ validate + decode the push
                          │ map it to an AccountConfig
                          ▼
                      provider.fetch_messages_since(cursor)   cursor from CursorStore
                          │ returns (messages, new_cursor)
                          ▼
                      SlackNotifier.notify(message)  for each message
                          │
                          ▼
                      CursorStore.set(account, new_cursor)    only after Slack succeeds
```

Key ideas:

- **Pushes carry no mail.** A push only says "this mailbox changed". The
  provider recovers the actual messages by asking the upstream service for
  everything after a stored *cursor* (Gmail: a `historyId`; Outlook: a delta
  token).
- **`CursorStore`** (`state.py`) persists the per-account cursor as a small
  JSON file with atomic writes, so pushes are processed incrementally across
  restarts. Keys are account names; values are opaque cursor strings.
- **`SlackNotifier`** (`notifier.py`) is provider-agnostic: it takes an
  `EmailMessage` and posts a Block Kit payload to the one configured webhook.
  A new provider never touches Slack code — it only has to produce
  `EmailMessage` values.

## The `EmailProvider` contract

Defined in `providers/base.py`. Implement all of it; raise `ProviderError`
(from the same module) when the upstream service misbehaves.

### `name: ClassVar[str]`

The registry key. This is exactly the string users write as
`provider = "..."` in an `[[accounts]]` block. Gmail uses `"gmail"`; your
Outlook class would set `name = "outlook"`.

### `from_config(cls, account: AccountConfig, config: AppConfig) -> EmailProvider`

Classmethod constructor used by the registry's `create_provider()`. It
receives the one account this instance will serve plus the full `AppConfig`,
from which it should pluck its provider-wide settings section
(`GmailProvider.from_config` returns `cls(account, config.gmail)`).

Keep `__init__` separate and richer than `from_config`: the Gmail
implementation accepts a keyword-only `service_factory` there so tests can
inject a fake API client (see [Testing conventions](#testing-conventions)).

### `start_watch(self) -> WatchInfo`

Register (or renew — the two must be idempotent from the caller's point of
view) push notifications for the account. Called by the `email-notifier watch`
CLI command, which loops over *all* configured accounts via
`create_provider(...).start_watch()` — so once your provider is registered,
`watch` covers it with no CLI changes.

The returned `WatchInfo` (`models.py`) has:

- `cursor` — an opaque string marking **"now"**. Only mail arriving *after*
  this point will ever be reported by `fetch_messages_since`. The CLI stores
  it in the `CursorStore` only when the account has no stored cursor yet —
  a renewal keeps the existing position so no mail is skipped.
- `expires_at` — optional `datetime` for when the watch lapses (shown in the
  CLI table so users know their renewal cadence).

### `fetch_messages_since(self, cursor: str) -> tuple[list[EmailMessage], str]`

Return every message that arrived after `cursor`, plus the new cursor to
store. Messages are provider-agnostic `EmailMessage` dataclasses
(`models.py`): `account` (the account *name* from config), `provider` (your
`name`), `message_id`, `sender`, `subject`, `snippet`, and an optional
`received_at`.

**Required: stale-cursor recovery.** When `cursor` is too old for the
upstream service, you must *not* raise — return no messages plus a fresh
cursor meaning "now":

```python
return [], <fresh cursor>
```

Rationale: the server stores whatever cursor you return, so a raise would
repeat on every future push and wedge the account forever (the 5xx response
makes the push service redeliver endlessly). `GmailProvider` does this when
`users.history.list` returns HTTP 404 (the stored `historyId` fell out of
Gmail's roughly one-week history window): it resynchronizes via
`current_cursor()` (the profile's present `historyId`) and accepts that mail
from the gap is not notified. An Outlook provider hits the same situation
when a delta token expires.

Individually vanished messages should be skipped, not fatal — Gmail treats a
404 on `users.messages.get` as "deleted between the push and our fetch" and
drops that one message.

## The registry (`providers/__init__.py`)

- `register(provider_class)` — maps `provider_class.name` to the class;
  returns the class, so it is usable as a decorator.
- `get_provider_class(name)` — lookup; raises `ProviderError` listing the
  registered names when unknown.
- `create_provider(account, config)` — the one-liner the server and CLI use:
  `get_provider_class(account.provider).from_config(account, config)`.

## Checklist: what a new provider must add

### 1. The provider class + registration

Create `src/email_notifier/providers/outlook.py` with an `OutlookProvider`
subclass, then in `providers/__init__.py`:

```python
from .outlook import OutlookProvider

...
register(OutlookProvider)
```

(and add it to `__all__`). That alone makes `provider = "outlook"` valid in
`[[accounts]]` blocks and routes those accounts through your class in both
the server and the `watch` command.

### 2. A provider-wide config section

Account-level fields (`name`, `email`, `token_file`, `provider`, `label_ids`)
already exist on `AccountConfig`. Settings shared by *all* accounts of your
provider belong in a new TOML section mirroring `[gmail]` / `GmailSettings`.
This requires extending `config.py`:

- a frozen dataclass, e.g. `OutlookSettings` (say: `client_id`,
  `notification_url`, `client_state`), mirroring `GmailSettings`;
- a loader like `_load_gmail` — resolve relative paths against the config
  file's directory (`base / value`) and allow env-var overrides for secrets,
  as `PUBSUB_VERIFICATION_TOKEN` does;
- a new field on `AppConfig` (e.g. `outlook: OutlookSettings | None`) filled
  in by `load_config`. Make it optional/`None` when the section is absent, so
  Gmail-only configs keep working; validate it is present when an account
  actually uses the provider.

Your `from_config` then reads `config.outlook`. Update
`config.example.toml` too.

### 3. A push route in `server.py`

Each provider gets its own route because push formats differ; add e.g.
`POST /outlook/push` inside `create_app`, following the `/gmail/push`
pattern step by step:

1. **Validate** the request is really from your push service. Gmail compares
   a shared-secret `?token=` query parameter; Microsoft Graph instead sends a
   `clientState` secret *inside* the notification body — check it against
   your `OutlookSettings.client_state`.
2. **Decode** the payload into `(address or subscription id, pushed
   position)`. Keep the parsing in a small standalone helper like
   `_decode_pubsub_message` so it is unit-testable without HTTP.
3. **Map to an account** with `config.account_for_email(...)` (or your own
   subscription-id → account mapping) and get its cached provider via the
   route's `provider_for(account)`.
4. **The cursor dance.** Read `store.get(account.name)`:
   - `None` (first push ever seen for this account — its watch predates this
     server's state file): adopt the pushed position with `store.set(...)`
     and return 2xx *without* fetching; notifications start from the next
     change.
   - Otherwise call `fetch_messages_since(cursor)`, `notifier.notify(...)`
     each returned message, and only *then* `store.set(account.name,
     new_cursor)`.
5. **Ack semantics — 2xx vs 5xx.** This is the crux and it is shared by
   Pub/Sub and Graph alike:
   - Return **2xx** (the Gmail route uses 204) to acknowledge, including for
     malformed or unroutable notifications — redelivering those can never
     fix anything, so log a warning and ack.
   - Return **5xx** (the Gmail route raises `HTTPException(500)` on
     `ProviderError` / `SlackError`) to make the push service **redeliver**.
     Because the cursor is advanced only after every Slack notification
     succeeded, a transient failure means a retry re-runs the same fetch:
     mail is never silently dropped, at the cost of at-least-once (possibly
     duplicated) notifications.

**Where Microsoft Graph webhooks differ from Pub/Sub:**

- **validationToken handshake.** When you create (and sometimes when Graph
  revalidates) a subscription, Graph POSTs to your `notification_url` with a
  `?validationToken=...` query parameter and *no* notification body. You must
  respond within ~10 seconds with **200** and the decoded token echoed back
  as `text/plain` — before any of the steps above. The Gmail route has no
  equivalent; put this check first in `/outlook/push`.
- **Batched notifications.** A Graph POST body is `{"value": [...]}` — a
  list; loop over it (each item carries `subscriptionId`, `clientState`,
  `resourceData`). Graph also wants a fast 202-style ack; the cursor dance
  still applies per account.
- **Renewal is much more frequent.** Gmail watches last about 7 days (the
  CLI already tells users to re-run `email-notifier watch` daily). Graph
  mail subscriptions max out around **3 days** (4230 minutes), so
  `start_watch` must renew-or-create and users must run `watch` on a cron at
  least daily — document the cadence, don't assume Gmail's.

### 4. Illustrative skeleton

**ILLUSTRATIVE ONLY — this does not run.** Every `graph.*` call is
pseudocode standing in for real Microsoft Graph HTTP calls; it exists to show
how the contract maps onto Graph change notifications + delta queries.

```python
from ..models import EmailMessage, WatchInfo
from .base import EmailProvider, ProviderError


class OutlookProvider(EmailProvider):
    name = "outlook"

    def __init__(self, account, settings, *, graph_factory=None):
        self._account = account
        self._settings = settings  # OutlookSettings (see config section above)
        self._graph_factory = graph_factory or (lambda: build_graph_client(account.token_file))
        self._graph = None

    @classmethod
    def from_config(cls, account, config):
        return cls(account, config.outlook)

    def start_watch(self) -> WatchInfo:
        graph = self._graph_factory()
        sub = graph.create_or_renew_subscription(  # POST /subscriptions
            change_type="created",
            resource="me/mailFolders/inbox/messages",
            notification_url=self._settings.notification_url,
            client_state=self._settings.client_state,  # echoed back in every push
        )
        cursor = graph.delta_token_for_now()  # GET .../delta?$deltatoken=latest
        return WatchInfo(cursor=cursor, expires_at=sub.expires_at)  # ~3 days out

    def fetch_messages_since(self, cursor):
        graph = self._graph_factory()
        try:
            items, new_cursor = graph.delta(cursor)  # GET .../delta with stored token
        except GraphDeltaTokenExpired:  # HTTP 410 Gone from Graph
            return [], graph.delta_token_for_now()  # REQUIRED stale-cursor recovery
        messages = [
            EmailMessage(
                account=self._account.name,
                provider=self.name,
                message_id=item["id"],
                sender=item["from"],
                subject=item["subject"],
                snippet=item["bodyPreview"],
            )
            for item in items
        ]
        return messages, new_cursor
```

The pattern to notice: the **delta token is the cursor**. `start_watch`
returns a token meaning "now"; each `fetch_messages_since` exchanges the old
token for new items plus the next token; an expired token (Graph answers
410 Gone) triggers the mandatory resync-to-now instead of an error.

## Testing conventions

- **No network in tests, ever.** Nothing may contact real Google, Microsoft,
  or Slack services.
- **Fake services are injected via the constructor**, the way
  `GmailProvider.__init__` takes a keyword-only `service_factory` returning a
  stand-in for the Gmail API client. Give `OutlookProvider` the equivalent
  (`graph_factory` above). `run_oauth_flow` follows the same idea with
  `flow_factory` / `service_builder` parameters.
- **The server is testable the same way**: `create_app` accepts `notifier`,
  `store`, and `provider_factory` keyword arguments, so route tests drive a
  FastAPI test client against fakes end to end.
- **Coverage gate: 95% branch coverage** across the package. pytest's
  configured `addopts` already include
  `--cov=email_notifier --cov-branch --cov-fail-under=95`; while iterating on
  a single file, append `--cov-fail-under=0` on the command line to override
  the gate temporarily, e.g.
  `.venv/bin/pytest tests/test_outlook.py --cov-fail-under=0`.
- **Lint with ruff, line length 100**: `.venv/bin/ruff check` (and
  `.venv/bin/ruff format`) before calling a change done.
