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
# The bot's secrets live here (same EnvironmentFile the systemd unit uses).
# We source it so notify()'s send_alert() sees TELEGRAM_* — otherwise the
# deploy alerts silently no-op.
ENV_FILE=/home/kohinoor_abhi/.env

# Paths whose change requires a bot restart. Dashboard/docs/ops changes
# alone do not (Railway serves the dashboard; the timer reloads itself).
RESTART_PATHS='^(src/(strategies|shared|core|safety|order_manager|data_sources|brokers|entrypoints)/|buckets\.yaml$|migrations/|requirements\.txt$|pyproject\.toml$)'
DEP_PATHS='^(requirements\.txt|pyproject\.toml)$'
MIGRATION_PATHS='^migrations/'

cd "$REPO" || { echo "repo not found: $REPO" >&2; exit 1; }

# Load the bot's env so send_alert() (and any other Settings read) works.
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

# Send a Telegram alert through the bot's own plumbing. Best-effort.
notify() {
    "$PY" - "$1" <<'PYEOF' || true
import sys
from src.core.alerts import send_alert
send_alert(sys.argv[1])
PYEOF
}

# CI gate (Layer 1): GitHub check-runs for a SHA. Prints one of
# success | pending | none | failure | error. Repo is public — no token.
ci_status() {
    "$PY" - "$1" <<'PYEOF' || echo error
import sys
import httpx
sha = sys.argv[1]
url = f"https://api.github.com/repos/abhipatratrade/trading-bot/commits/{sha}/check-runs"
try:
    r = httpx.get(url, headers={"Accept": "application/vnd.github+json"}, timeout=15)
    r.raise_for_status()
    runs = r.json().get("check_runs", [])
except Exception:
    print("error"); raise SystemExit(0)
if not runs:
    print("none"); raise SystemExit(0)
if any(c.get("status") != "completed" for c in runs):
    print("pending"); raise SystemExit(0)
ok = all(c.get("conclusion") in ("success", "skipped", "neutral") for c in runs)
print("success" if ok else "failure")
PYEOF
}

BEFORE=$(git rev-parse HEAD)

if ! git fetch --quiet origin "$BRANCH"; then
    notify "🚨 Deploy: git fetch failed on the VM — check network/credentials"
    exit 1
fi

# Nothing new on the remote → quiet no-op (the common case every minute).
REMOTE_SHA=$(git rev-parse "origin/$BRANCH")
if [ "$REMOTE_SHA" = "$BEFORE" ]; then
    exit 0
fi

# ── CI gate: refuse to deploy a commit whose checks are not green ──────
# pending/error → quiet retry next cycle (CI takes a few minutes).
# failure/none  → block + notify ONCE per SHA (state file), keep old code.
CI_STATE=/tmp/deploy-ci-notified
CI=$(ci_status "$REMOTE_SHA")
case "$CI" in
    success) ;;
    pending|error)
        exit 0 ;;
    failure|none)
        if [ "$(cat "$CI_STATE" 2>/dev/null)" != "$REMOTE_SHA:$CI" ]; then
            echo "$REMOTE_SHA:$CI" > "$CI_STATE"
            notify "🚨 Deploy BLOCKED for ${REMOTE_SHA:0:7}: CI status '$CI' — old code keeps running. Fix and push (or check the Actions tab)."
        fi
        exit 0 ;;
esac

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

# Apply schema migrations before the new code (which expects them) boots.
# On failure, do NOT restart — old code keeps running against the old
# schema, and the alert tells the user to run alembic by hand.
if printf '%s\n' "$CHANGED" | grep -qE "$MIGRATION_PATHS"; then
    if ! "$PY" -m alembic upgrade head; then
        notify "🚨 Deployed $SHA — alembic upgrade FAILED, bot NOT restarted (run migrations manually)"
        exit 1
    fi
    notify "🗄️ Deployed $SHA — alembic migrations applied"
fi

# Restart only when something the bot actually runs has changed.
if printf '%s\n' "$CHANGED" | grep -qE "$RESTART_PATHS"; then
    # Selfcheck with the NEW code before killing the old (working) process:
    # settings, bucket configs, DB reachability. No broker/network probes —
    # those are fail-soft inside run_bot itself.
    if ! "$PY" -m src.entrypoints.selfcheck; then
        notify "🚨 Deployed $SHA — selfcheck FAILED, bot NOT restarted (old code still running). journalctl -u bot-deploy for details."
        exit 1
    fi
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
