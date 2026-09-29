#!/bin/bash
# Copyright (c) 2026 Leo Chen <leo.chen0412@outlook.com>
#
# Renew the Gmail -> Pub/Sub push watches from cron.
#
# Gmail watches expire after ~7 days, so this must run at least daily or
# notifications stop arriving with no other symptom. Install with:
#
#   crontab -e
#   0 6,18 * * * /bin/bash /Volumes/leo_disk/Freelancer/email-notification/scripts/watch-cron.sh
#
# Logs to ~/Library/Logs/email-notifier/watch.log (internal disk, so the
# "external volume was not mounted" case is still recorded).

set -uo pipefail
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
export NO_COLOR=1

PROJECT_DIR="/Volumes/leo_disk/Freelancer/email-notification"
BIN="$PROJECT_DIR/.venv/bin/email-notifier"
CONFIG="$PROJECT_DIR/config.toml"
LOG_DIR="$HOME/Library/Logs/email-notifier"
LOG="$LOG_DIR/watch.log"
MAX_LOG_LINES=2000

mkdir -p "$LOG_DIR"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S%z')" "$*" >>"$LOG"; }

trim_log() {
    if [ -f "$LOG" ] && [ "$(wc -l <"$LOG")" -gt "$MAX_LOG_LINES" ]; then
        tail -n "$MAX_LOG_LINES" "$LOG" >"$LOG.tmp" && mv "$LOG.tmp" "$LOG"
    fi
}

# Optional: ping Slack when a renewal fails, so a dead watch is not silent.
# Enable by exporting SLACK_WEBHOOK_URL in the crontab line.
notify_failure() {
    [ -n "${SLACK_WEBHOOK_URL:-}" ] || return 0
    curl -fsS -m 10 -X POST -H 'Content-type: application/json' \
        --data "{\"text\":\":warning: email-notifier watch renewal failed on $(hostname -s) - see $LOG\"}" \
        "$SLACK_WEBHOOK_URL" >/dev/null 2>&1 || true
}

# The project lives on an external volume. If it is not mounted there is
# nothing to renew, and that is not a failure worth alerting on.
if [ ! -x "$BIN" ] || [ ! -f "$CONFIG" ]; then
    log "SKIP  project unavailable (is $PROJECT_DIR mounted?)"
    trim_log
    exit 0
fi

log "START watch renewal"
output=$("$BIN" --config "$CONFIG" watch 2>&1)
status=$?
printf '%s\n' "$output" | sed 's/^/      /' >>"$LOG"

case "$status" in
    0) log "OK    watches renewed" ;;
    2) log "FAIL  configuration error (exit 2)"; notify_failure ;;
    *) log "FAIL  watch renewal failed (exit $status)"; notify_failure ;;
esac

trim_log
exit "$status"
