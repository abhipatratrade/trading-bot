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
