#!/usr/bin/env bash
#
# ops/deploy.sh — pull-based auto-deploy for the GCP bot-worker VM.
#
# Run on a 60s systemd timer (ops/bot-deploy.timer). Each cycle:
#   1. Fetch origin/main; if HEAD is unchanged, exit silently (no spam).
#   2. Fast-forward only (never clobber a diverged VM — alert instead).
#   3. pip install if dependency manifests changed (abort restart on failure).
#   4. Restart bot-worker.service ONLY if a bot-relevant path changed
#      (dashboard/docs-only pushes don't interrupt trading).
#   5. Health-check the restart and notify the outcome on Telegram.
#
# Telegram notification reuses the bot's own send_alert() via the venv so
# it picks up the existing .env credentials — the shell never touches the
# token directly.
#
# Safe to run by hand for a one-off deploy: `ops/deploy.sh`.

set -uo pipefail

REPO=/home/kohinoor_abhi/trading-bot
PY="$REPO/.venv/bin/python"
PIP="$REPO/.venv/bin/pip"
SERVICE=bot-worker.service
BRANCH=main

# Paths whose change requires a bot restart. Dashboard/docs/ops changes
# alone do not (Railway serves the dashboard; the timer reloads itself).
RESTART_PATHS='^(src/(strategies|shared|core|safety|order_manager|data_sources|brokers|entrypoints)/|buckets\.yaml$|requirements\.txt$|pyproject\.toml$)'
DEP_PATHS='^(requirements\.txt|pyproject\.toml)$'

cd "$REPO" || { echo "repo not found: $REPO" >&2; exit 1; }

# Send a Telegram alert through the bot's own plumbing. Best-effort.
notify() {
    "$PY" - "$1" <<'PYEOF' || true
import sys
from src.core.alerts import send_alert
send_alert(sys.argv[1])
PYEOF
}

BEFORE=$(git rev-parse HEAD)

if ! git fetch --quiet origin "$BRANCH"; then
    notify "🚨 Deploy: git fetch failed on the VM — check network/credentials"
    exit 1
fi

# Nothing new on the remote → quiet no-op (the common case every minute).
if [ "$(git rev-parse "origin/$BRANCH")" = "$BEFORE" ]; then
    exit 0
fi

# Fast-forward only. If the VM has local commits the remote can't ff over,
# refuse rather than clobber — surfaces the divergence loudly.
if ! git merge --ff-only --quiet "origin/$BRANCH"; then
    notify "🚨 Deploy: ff-only merge failed — VM diverged from origin/$BRANCH, manual fix needed"
    exit 1
fi

AFTER=$(git rev-parse HEAD)
CHANGED=$(git diff --name-only "$BEFORE" "$AFTER")
N=$(printf '%s\n' "$CHANGED" | grep -c . || true)
SHA=${AFTER:0:7}

# Reinstall deps if the manifests moved. On failure, do NOT restart —
# keep the old (working) process alive rather than boot a broken env.
if printf '%s\n' "$CHANGED" | grep -qE "$DEP_PATHS"; then
    if ! "$PIP" install -q -r requirements.txt; then
        notify "🚨 Deployed $SHA — pip install FAILED, bot NOT restarted (old code still running)"
        exit 1
    fi
fi

# Restart only when something the bot actually runs has changed.
if printf '%s\n' "$CHANGED" | grep -qE "$RESTART_PATHS"; then
    sudo systemctl restart "$SERVICE"
    sleep 6
    if systemctl is-active --quiet "$SERVICE"; then
        notify "✅ Deployed $SHA — bot restarted OK ($N file(s) changed)"
    else
        notify "🚨 Deployed $SHA — bot FAILED to start! Check: journalctl -u $SERVICE"
        exit 1
    fi
else
    notify "ℹ️ Deployed $SHA — dashboard/docs only, bot not restarted ($N file(s))"
fi
