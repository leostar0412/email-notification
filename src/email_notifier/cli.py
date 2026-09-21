"""Command-line interface.

Commands:
    auth        Run the Google OAuth consent flow for one configured account.
    watch       Register or renew Gmail push watches (all accounts, or one).
    serve       Run the Pub/Sub push endpoint.
    test-slack  Send a test message to the configured Slack webhook.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

import uvicorn
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import AccountConfig, AppConfig, ConfigError, load_config
from .notifier import SlackError, SlackNotifier
from .providers import create_provider
from .providers.base import ProviderError
from .providers.gmail import run_oauth_flow
from .server import create_app
from .state import CursorStore

console = Console()
error_console = Console(stderr=True, style="bold red")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="email-notifier",
        description="Send a Slack notification whenever a new email arrives.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.toml"),
        help="Path to the TOML config file (default: config.toml)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    auth = subparsers.add_parser("auth", help="Authorize one account with Google OAuth")
    auth.add_argument("account", help="Account name from the config file")

    watch = subparsers.add_parser("watch", help="Register or renew push watches")
    watch.add_argument("--account", help="Only this account (default: all accounts)")

    serve = subparsers.add_parser("serve", help="Run the push-notification endpoint")
    serve.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    serve.add_argument("--port", type=int, default=8000, help="Port (default: 8000)")

    subparsers.add_parser("test-slack", help="Send a test message to Slack")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        error_console.print(f"Configuration error: {exc}")
        return 2
    try:
        if args.command == "auth":
            return _cmd_auth(config, args.account)
        if args.command == "watch":
            return _cmd_watch(config, args.account)
        if args.command == "serve":
            return _cmd_serve(config, args.host, args.port)
        return _cmd_test_slack(config)
    except (ProviderError, SlackError) as exc:
        error_console.print(str(exc))
        return 1


def _resolve_account(config: AppConfig, name: str) -> AccountConfig | None:
    account = config.account_by_name(name)
    if account is None:
        known = ", ".join(a.name for a in config.accounts)
        error_console.print(f"No account named {name!r} in the config (known: {known})")
    return account


def _cmd_auth(config: AppConfig, name: str) -> int:
    account = _resolve_account(config, name)
    if account is None:
        return 2
    if account.provider != "gmail":
        error_console.print(
            f"Account {name!r} uses provider {account.provider!r}; "
            "`auth` currently supports only gmail accounts."
        )
        return 2
    console.print(f"Opening a browser to authorize [bold]{account.email}[/bold]…")
    authorized_email = run_oauth_flow(config.gmail.credentials_file, account.token_file)
    console.print(f"[green]✓[/green] Token saved to [bold]{account.token_file}[/bold]")
    if authorized_email.lower() != account.email.lower():
        console.print(
            f"[yellow]Warning:[/yellow] you authorized [bold]{authorized_email}[/bold] "
            f"but this account is configured as [bold]{account.email}[/bold]. "
            "Update the config or re-run auth with the right Google account."
        )
    return 0


def _cmd_watch(config: AppConfig, name: str | None) -> int:
    if name is None:
        accounts: tuple[AccountConfig, ...] = config.accounts
    else:
        account = _resolve_account(config, name)
        if account is None:
            return 2
        accounts = (account,)

    store = CursorStore(config.state_file)
    table = Table(title="Push watches registered")
    table.add_column("Account", style="bold")
    table.add_column("Email")
    table.add_column("Cursor")
    table.add_column("Expires (UTC)")
    for account in accounts:
        info = create_provider(account, config).start_watch()
        # Seed the cursor only on first registration. A renewal must keep the
        # stored position: overwriting it with "now" would skip any mail that
        # arrived while the server was down or failing.
        if store.get(account.name) is None:
            store.set(account.name, info.cursor)
        expires = f"{info.expires_at:%Y-%m-%d %H:%M}" if info.expires_at else "—"
        table.add_row(account.name, account.email, info.cursor, expires)
    console.print(table)
    console.print(
        "Watches expire after about 7 days — re-run [bold]email-notifier watch[/bold] "
        "at least daily (e.g. from cron) to keep notifications flowing."
    )
    return 0


def _cmd_serve(config: AppConfig, host: str, port: int) -> int:
    console.print(
        f"Serving push endpoint on [bold]http://{host}:{port}/gmail/push[/bold] "
        f"for {len(config.accounts)} account(s)…"
    )
    uvicorn.run(create_app(config), host=host, port=port)
    return 0


def _cmd_test_slack(config: AppConfig) -> int:
    with SlackNotifier(config.slack.webhook_url) as notifier:
        notifier.send_text("👋 email-notifier is connected to this channel.")
    console.print("[green]✓[/green] Test message sent — check your Slack channel.")
    return 0
