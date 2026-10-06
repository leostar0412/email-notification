# Extending email-notifier with a new provider

This guide walks through adding support for another email service. Gmail and
Outlook are built in. Everything here matches the code in
`src/email_notifier/`: read it alongside `providers/base.py`,
`providers/gmail.py`, and `server.py`.

## Architecture in brief

One **provider instance handles one configured account** (one `[[accounts]]`
block in `config.toml`). The server builds and caches a provider per account
name the first time a push arrives for it.

The flow for every notification, regardless of provider:

```
mail service push -> provider-specific HTTP route (for example POST /gmail/push)
                        | validate + decode the push
                        | map it to an AccountConfig
                        v
                    provider.fetch_messages_since(cursor)   cursor from CursorStore
                        | returns (messages, new_cursor)
                        v
                    SlackNotifier.notify(message)  for each message
                        |
                        v
                    CursorStore.set(account, new_cursor)    only after Slack succeeds
```

Key ideas:

- **Pushes carry no mail.** A push only says "this mailbox changed". The
  provider recovers the actual messages by asking the upstream service for
  everything after a stored *cursor* (Gmail stores a `historyId`).
- **`CursorStore`** (`state.py`) persists the per-account cursor as a small
  JSON file with atomic writes, so pushes are processed incrementally across
  restarts. Keys are account names; values are opaque cursor strings.
- **`SlackNotifier`** (`notifier.py`) is provider-agnostic: it takes an
  `EmailMessage` and posts a Block Kit payload to the one configured webhook.
  A new provider never touches Slack code. It only has to produce
  `EmailMessage` values.

## The `EmailProvider` contract

Defined in `providers/base.py`. Implement all of it; raise `ProviderError`
(from the same module) when the upstream service misbehaves.

### `name: ClassVar[str]`

The registry key. This is exactly the string users write as
`provider = "..."` in an `[[accounts]]` block. Gmail uses `"gmail"`. A new
class would set `name = "example"`.

### `from_config(cls, account: AccountConfig, config: AppConfig) -> EmailProvider`

Classmethod constructor used by the registry's `create_provider()`. It
receives the one account this instance will serve plus the full `AppConfig`,
from which it should pluck its provider-wide settings section
(`GmailProvider.from_config` returns `cls(account, config.gmail)`).

Keep `__init__` separate and richer than `from_config`: the Gmail
implementation accepts a keyword-only `service_factory` there so tests can
inject a fake API client (see [Testing conventions](#testing-conventions)).

### `start_watch(self) -> WatchInfo`

Register (or renew: the two must be idempotent from the caller's point of
view) push notifications for the account. Called by the `email-notifier watch`
CLI command, which loops over *all* configured accounts via
`create_provider(...).start_watch()`. Once your provider is registered,
`watch` covers it with no CLI changes.

The returned `WatchInfo` (`models.py`) has:

- `cursor`: an opaque string marking **"now"**. Only mail arriving *after*
  this point will ever be reported by `fetch_messages_since`. The CLI stores
  it in the `CursorStore` only when the account has no stored cursor yet.
  A renewal keeps the existing position so no mail is skipped.
- `expires_at`: optional `datetime` for when the watch lapses (shown in the
  CLI table so users know their renewal cadence).

### `fetch_messages_since(self, cursor: str) -> tuple[list[EmailMessage], str]`

Return every message that arrived after `cursor`, plus the new cursor to
store. Messages are provider-agnostic `EmailMessage` dataclasses
(`models.py`): `account` (the account *name* from config), `provider` (your
`name`), `message_id`, `sender`, `subject`, `snippet`, and an optional
`received_at`.

**Required: stale-cursor recovery.** When `cursor` is too old for the
upstream service, you must *not* raise. Return no messages plus a fresh
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
from the gap is not notified.

Individually vanished messages should be skipped, not fatal. Gmail treats a
404 on `users.messages.get` as "deleted between the push and our fetch" and
drops that one message.

## The registry (`providers/__init__.py`)

- `register(provider_class)` maps `provider_class.name` to the class and
  returns the class, so it is usable as a decorator.
- `get_provider_class(name)` looks up a class. It raises `ProviderError`
  listing the registered names when the name is unknown.
- `create_provider(account, config)` is the one-liner the server and CLI use:
  `get_provider_class(account.provider).from_config(account, config)`.

## Checklist: what a new provider must add

### 1. The provider class + registration

Create `src/email_notifier/providers/example.py` with an `ExampleProvider`
subclass, then in `providers/__init__.py`:

```python
from .example import ExampleProvider

...
register(ExampleProvider)
```

(and add it to `__all__`). That alone makes `provider = "example"` valid in
`[[accounts]]` blocks and routes those accounts through your class in both
the server and the `watch` command.

### 2. A provider-wide config section

Account-level fields (`name`, `email`, `token_file`, `provider`, `label_ids`)
already exist on `AccountConfig`. Settings shared by *all* accounts of your
provider belong in a new TOML section mirroring `[gmail]` / `GmailSettings`.
This requires extending `config.py`:

- a frozen dataclass, for example `ExampleSettings`, mirroring `GmailSettings`;
- a loader like `_load_gmail`. Resolve relative paths against the config
  file's directory (`base / value`) and allow env-var overrides for secrets,
  as `PUBSUB_VERIFICATION_TOKEN` does;
- a new field on `AppConfig` (for example `example: ExampleSettings | None`)
  filled in by `load_config`. Leave it `None` when the section is absent, so
  existing configs keep working, and require it when an account actually uses
  the provider.

Your `from_config` then reads `config.example`. Update `config.example.toml`
too.

### 3. A push route in `server.py`

Each provider gets its own route because push formats differ. Add for example
`POST /example/push` inside `create_app`, following the `/gmail/push`
pattern:

1. **Validate** the request is really from your push service. Gmail compares
   a shared-secret `?token=` query parameter.
2. **Decode** the payload into the account and any pushed position. Keep the
   parsing in a small standalone helper like `_decode_pubsub_message` so it
   is unit-testable without HTTP.
3. **Map to an account** with `config.account_for_email(...)` (or your own
   mapping) and get its cached provider via the route's `provider_for(account)`.
4. **The cursor dance.** Read `store.get(account.name)`:
   - `None` (first push ever seen for this account, because its watch predates
     this server's state file): store a cursor meaning "now" and return 2xx
     *without* fetching. Notifications start from the next change.
   - Otherwise call `fetch_messages_since(cursor)`, `notifier.notify(...)`
     each returned message, and only *then* `store.set(account.name, new_cursor)`.
5. **Ack semantics, 2xx vs 5xx.**
   - Return **2xx** (the Gmail route uses 204) to acknowledge, including for
     malformed or unroutable notifications. Redelivering those can never
     fix anything, so log a warning and ack.
   - Return **5xx** (the Gmail route raises `HTTPException(500)` on
     `ProviderError` / `SlackError`) to make the push service **redeliver**.
     Because the cursor is advanced only after every Slack notification
     succeeded, a transient failure means a retry re-runs the same fetch:
     mail is never silently dropped, at the cost of at-least-once (possibly
     duplicated) notifications.

Mount the route only when that provider's settings are present, the way
`/gmail/push` is mounted only when `[gmail]` is configured.

## Testing conventions

- **No network in tests, ever.** Nothing may contact real Google, Microsoft,
  or Slack services.
- **Fake services are injected via the constructor**, the way
  `GmailProvider.__init__` takes a keyword-only `service_factory` returning a
  stand-in for the Gmail API client. Give a new provider the same kind of
  hook. `run_oauth_flow` follows the same idea with `flow_factory` /
  `service_builder` parameters.
- **The server is testable the same way**: `create_app` accepts `notifier`,
  `store`, and `provider_factory` keyword arguments, so route tests drive a
  FastAPI test client against fakes end to end.
- **Coverage gate: 95% branch coverage** across the package. pytest's
  configured `addopts` already include
  `--cov=email_notifier --cov-branch --cov-fail-under=95`. While iterating on
  a single file, append `--cov-fail-under=0` on the command line to override
  the gate temporarily, for example
  `.venv/bin/pytest tests/test_example.py --cov-fail-under=0`.
- **Lint with ruff, line length 100**: `.venv/bin/ruff check` (and
  `.venv/bin/ruff format`) before calling a change done.
