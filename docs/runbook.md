# Runbook — Operations

Operational reference for the trading bot. For architecture see
`DECISIONS.md`; for build status see `PHASES.md`.

---

## Deployment

Code runs in two places:

| Where | Service(s) | How it deploys |
|---|---|---|
| **Railway** | `dashboard`, `scheduler` | **Auto** on `git push` (Railway watches `main`). |
| **GCP VM** (Mumbai, `34.14.200.220`) | `bot-worker.service` | **Auto** within ~60s via the pull-timer (below). |

### How auto-deploy works (GCP VM)

A systemd timer (`bot-deploy.timer`) runs `ops/deploy.sh` every ~60s. Each cycle:

1. `git fetch origin main`. If nothing new → exits silently.
2. Fast-forwards the VM to `origin/main` (`--ff-only`; refuses to clobber a diverged VM).
3. Re-installs deps **only if** `requirements.txt` / `pyproject.toml` changed (aborts the restart if the install fails, so the old code keeps running).
4. Restarts `bot-worker.service` **only if** a bot-relevant path changed:
   `src/{strategies,shared,core,safety,order_manager,data_sources,brokers,entrypoints}/`, `buckets.yaml`, or the dep manifests.
   Dashboard/docs-only pushes do **not** restart the bot.
5. Health-checks the restart and reports on Telegram.

So the everyday workflow is just: **edit locally → `git push` → wait ~60s → read the Telegram confirmation.** No SSH.

### CI deploy gate (Layer 1)

Two guards sit between `git push` and the running bot:

1. **GitHub Actions CI** (`.github/workflows/ci.yml`): every push runs ruff +
   the full unit suite (which includes the bucket config-load smoke) on
   Python 3.11 — the VM's runtime. `deploy.sh` polls the commit's check-runs
   and **refuses to fast-forward until they're green**: pending → retries next
   cycle; failed/missing → blocks and Telegrams
   `🚨 Deploy BLOCKED for <sha>: CI status '…'` once, old code keeps running.
2. **Selfcheck** (`python -m src.entrypoints.selfcheck`): before restarting,
   deploy.sh boots the NEW code's settings + bucket configs + a DB ping. A
   failure leaves the old (working) process untouched.

Net effect: a commit that fails tests, lints dirty, has a broken bucket yaml,
or can't reach the DB **physically cannot replace the running bot**. To deploy
in an emergency while CI is red: fix the code — or, truly exceptionally, SSH in
and `git pull && sudo systemctl restart bot-worker` by hand (the gate only
guards the auto-deploy path).

This covers **allocation** (`allocator.yaml`), **trading** (`strategy_master.csv`), **scanner** (`scanner.yaml`), **regime** (`regime.yaml`), and strategy code (`strategies/*.py`) — all live under `src/strategies/<bucket>/` and trigger a restart.

### Telegram deploy alerts — what they mean

| Message | Meaning | Action |
|---|---|---|
| `✅ Deployed <sha> — bot restarted OK (<n> files)` | New code/config is live. | None. |
| `ℹ️ Deployed <sha> — dashboard/docs only, bot not restarted` | Push didn't touch bot code; Railway handled it. | None. |
| `🚨 Deployed <sha> — bot FAILED to start` | New code crashed on boot. | `journalctl -u bot-worker -n 50`; revert the bad commit (next cycle auto-recovers). |
| `🚨 Deploy: pip install FAILED` | Dep install broke; bot **not** restarted, old code still running. | Fix the manifest, push again. |
| `🚨 Deploy: ff-only merge failed — VM diverged` | Someone committed directly on the VM. | SSH in, reconcile git state by hand. |

### Emergency: stop auto-deploy

```bash
sudo systemctl stop bot-deploy.timer     # halt the timer
sudo systemctl disable bot-deploy.timer  # also prevent it starting on boot
```

Manual deploy still works while the timer is stopped:

```bash
cd ~/trading-bot && git pull origin main && sudo systemctl restart bot-worker.service
```

Re-enable: `sudo systemctl enable --now bot-deploy.timer`.

### First-time VM setup (one-off)

```bash
cd ~/trading-bot && git pull origin main
chmod +x ops/deploy.sh
sudo cp ops/bot-deploy.service ops/bot-deploy.timer /etc/systemd/system/
sudo systemctl daemon-reload

# Allow the deploy (runs as kohinoor_abhi) to restart the bot without a password:
echo 'kohinoor_abhi ALL=(root) NOPASSWD: /usr/bin/systemctl restart bot-worker.service' \
  | sudo tee /etc/sudoers.d/bot-deploy
sudo chmod 440 /etc/sudoers.d/bot-deploy

sudo systemctl enable --now bot-deploy.timer
systemctl list-timers | grep bot-deploy   # confirm next-fire
```

---

## Regime retrain (runs on the VM, not Railway)

The weekly regime-model retrain trains HMMs on **Binance Futures klines**.
Binance geo-blocks Railway's region, so the retrain **must run on the
Mumbai VM**, where Binance is reachable. It used to run in the Railway
`scheduler` and silently `fetch_failed` every week (models went stale).
Now it runs via a VM `systemd` timer — Decision 020.

| Piece | What |
|---|---|
| `regime-retrain.timer` | Fires daily 02:00 UTC. |
| `regime-retrain.service` | `oneshot`: `python -m src.shared.regime.retrain_job --due`. |
| `--due` | Retrains only buckets whose `regime.yaml` `retrain_cadence` is due today (weekly → Mondays; daily → every day; manual → never). |

The job posts a Telegram summary per bucket (`Regime retrain <bucket>:
trained=N fetch_failed=N skipped=N`); a `⚠️` prefix means something
failed.

### Install (one-off; the deploy timer does NOT apply unit files)

```bash
cd ~/trading-bot && git pull origin main
sudo cp ops/regime-retrain.service ops/regime-retrain.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now regime-retrain.timer
systemctl list-timers | grep regime-retrain   # confirm next-fire
```

### Run / inspect

```bash
sudo systemctl start regime-retrain.service                 # run now (respects --due)
sudo journalctl -u regime-retrain -n 50 --no-pager          # last run's output

# Force a full retrain of every enabled bucket, ignoring cadence:
cd ~/trading-bot && set -a && . ~/.env && set +a && \
  .venv/bin/python -m src.shared.regime.retrain_job --all
# Or a single bucket:  … retrain_job --bucket longterm-crypto
```

---

## Swing-Indian bucket (Dhan sandbox) — Phase 4

`swing-indian` runs the Blasting Momentum strategy on Dhan equities + MTF,
inside the same deterministic `BucketRunner` loop as the crypto buckets. It's
**sandbox-only** for now (`TRADING_MODE=testnet` → Dhan sandbox orders +
`api.dhan.co` live data; the sandbox has no data feed). The old standalone
`scripts/dhan-scanner/` tool is retired once this soaks clean.

Two moving parts beyond the bot loop:

| Piece | What |
|---|---|
| `dhan-prepare.timer` | Fires daily 12:30 UTC (18:00 IST). |
| `dhan-prepare.service` | `oneshot`: `python -m src.shared.scanner.prepare_job --due` — the heavy ~4,600-symbol daily indicator pass; writes the passing shortlist to `ScannerSnapshot`. The per-tick `run_equity_scan` reads it (tolerates a shortlist up to ~4 days old, so Fri→Mon is fine). |
| regime | The **existing** `regime-retrain.timer` already covers swing-indian (NIFTYBEES-proxy HMM, weekly Mondays) — no separate timer. |

**Fail-soft:** if the Dhan creds are missing/broken on the VM, `run_bot` alerts
`Dhan account init FAILED` and skips the bucket — **the crypto buckets keep
running**. So enabling swing-indian can never take down live crypto.

### First-time setup (one-off, on the VM)

```bash
# 1. Add the Dhan creds to the bot's env (same values as your local .env):
#    DHAN_CLIENT_ID, DHAN_PIN, DHAN_TOTP_SECRET,
#    DHAN_SANDBOX_CLIENT_ID, DHAN_SANDBOX_ACCESS_TOKEN
nano ~/.env

# 2. Pull the latest code, then run the idempotent setup (checks env-var
#    presence, ensures pyotp, installs+enables the prepare timer):
cd ~/trading-bot && git pull origin main
bash ops/setup-dhan.sh
```

The VM's outbound IP `34.14.200.220` must be whitelisted in the Dhan DevPortal
(Static IP Setting) for the live data account.

### Verify + soak

```bash
# Bot picked up the Dhan account on restart:
sudo journalctl -u bot-worker -n 60 --no-pager | grep -iE "dhan|swing-indian"
#   expect "dhan_account_ready"; a "Dhan account init FAILED" alert = a bad cred.

# Build today's shortlist on demand (~40-60 min):
sudo systemctl start dhan-prepare.service
sudo journalctl -u dhan-prepare -n 30 --no-pager

# During 09:45-10:30 IST, watch entries fire (only in the entry window):
sudo journalctl -u bot-worker -f | grep -iE "equity_scan|swing-indian"
```

Then confirm on the dashboard `/buckets` page: swing-indian shows positions,
the wide (20%) protective stop rests on each, and exits fire on Supertrend
flip / 30-day cap. The reconciler should agree with the sandbox each sweep.

---

## Common checks

```bash
# Bot status + recent logs
sudo systemctl status bot-worker.service --no-pager
sudo journalctl -u bot-worker -n 100 --no-pager

# Auto-deploy timer
systemctl list-timers | grep bot-deploy
sudo journalctl -u bot-deploy -n 30 --no-pager

# Confirm the VM is on the latest commit
cd ~/trading-bot && git log --oneline -1
```

---

## Kill switch

The kill switch is a DB row, checked every tick — no deploy needed to stop
trading. Flip it from the dashboard (`/kill-switch`) or directly in Postgres.
This is independent of the deploy machinery above.
