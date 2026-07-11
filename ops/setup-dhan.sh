#!/usr/bin/env bash
#
# ops/setup-dhan.sh — one-time Mumbai-VM setup for the swing-indian (Dhan) bucket.
#
# Run ON THE VM as the bot user (kohinoor_abhi), with sudo available:
#   bash ops/setup-dhan.sh
#
# It: (1) checks the Dhan env vars are present in the bot's .env (WITHOUT
# printing their values), (2) ensures pyotp is in the venv, (3) installs +
# enables the dhan-prepare systemd timer. It NEVER writes secrets — you add
# those to /home/kohinoor_abhi/.env yourself (checklist printed below).
#
# The regime-retrain timer already exists and now also covers swing-indian
# (retrain_enabled_buckets includes Indian buckets), so NO new regime timer.
#
# Safe to re-run (idempotent).

set -uo pipefail

REPO=/home/kohinoor_abhi/trading-bot
VENV_PY="$REPO/.venv/bin/python"
VENV_PIP="$REPO/.venv/bin/pip"
ENV_FILE=/home/kohinoor_abhi/.env

echo "== swing-indian (Dhan) VM setup =="
echo

# 1. Env-var checklist — presence only, never echo the secret value.
REQUIRED=(
    DHAN_CLIENT_ID
    DHAN_PIN
    DHAN_TOTP_SECRET
    DHAN_SANDBOX_CLIENT_ID
    DHAN_SANDBOX_ACCESS_TOKEN
)
missing=0
for k in "${REQUIRED[@]}"; do
    val="$(grep -E "^${k}=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)"
    if [ -n "$val" ]; then
        echo "  [ok]  $k present"
    else
        echo "  [!!]  $k MISSING in $ENV_FILE"
        missing=1
    fi
done
# TRADING_MODE must be testnet for Dhan sandbox orders (House Rule 6).
mode="$(grep -E '^TRADING_MODE=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)"
echo "  [info] TRADING_MODE=${mode:-<unset>} (want 'testnet' → Dhan sandbox orders + live data)"

if [ "$missing" -ne 0 ]; then
    echo
    echo ">> Add the missing Dhan vars to $ENV_FILE (same values as your local .env):"
    echo "     DHAN_CLIENT_ID=...          DHAN_PIN=...        DHAN_TOTP_SECRET=..."
    echo "     DHAN_SANDBOX_CLIENT_ID=...  DHAN_SANDBOX_ACCESS_TOKEN=..."
    echo "   Then re-run this script. (The bucket stays dark until they're set —"
    echo "   run_bot fail-soft keeps the crypto bot running regardless.)"
    echo
fi

# 2. pyotp in the venv (deploy.sh installs it from requirements.txt on the next
#    push; install now so a manual prepare works immediately).
if ! "$VENV_PY" -c "import pyotp" 2>/dev/null; then
    echo "  installing pyotp into the venv..."
    "$VENV_PIP" install -q "pyotp>=2.9" || { echo "  [!!] pyotp install failed"; exit 1; }
fi
echo "  [ok]  pyotp available in the venv"

# 3. Install + enable the dhan-prepare timer.
sudo cp "$REPO/ops/dhan-prepare.service" "$REPO/ops/dhan-prepare.timer" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dhan-prepare.timer
echo "  [ok]  dhan-prepare.timer installed + enabled"
echo
systemctl list-timers --all 2>/dev/null | grep -E "dhan-prepare|regime-retrain" || true

cat <<'NEXT'

== Next steps ==
  1. From your dev box, push the M7 commit so the bot restarts with the Dhan
     wiring (VM auto-pulls within 60s):
         git push origin main
     Then watch the restart:
         journalctl -u bot-worker -n 40 --no-pager | grep -iE "dhan|swing-indian"
     Expect "dhan_account_ready" (or a "Dhan account init FAILED" alert if a
     cred is missing — crypto keeps running either way).

  2. Build today's shortlist on demand (one-off, ~40-60 min):
         sudo systemctl start dhan-prepare.service
         journalctl -u dhan-prepare -n 30 --no-pager

  3. During the next 09:45-10:30 IST window, confirm entries:
         journalctl -u bot-worker -f | grep -iE "equity_scan|swing-indian"
     and check the dashboard /buckets page for swing-indian.

  4. Retire the interim tool (scripts/dhan-scanner/) + its Windows Task
     Scheduler jobs once this soaks clean for a few days.
NEXT
