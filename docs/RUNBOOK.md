# Operating runbook (paper trading)

How the system is run day to day, what to check, and how to stop it. Everything
here assumes the paper-only configuration in `Backend/.env.example`.

## The operator account

The first account registered on an empty database is the **operator** — the
only kind of account that can see the broker account, place or close trades,
start the autotrader, or subscribe to the live feed. Registration closes
after that (`REGISTRATION_MODE=closed`); add emails to `OPERATOR_EMAILS` to
grant another person operator access, and set `SCHEDULER_USER_EMAIL` to the
operator the unattended scheduler should trade under. The build is paper-only
(`PAPER_ONLY=true`): the mode switch to live is refused at the API and the
broker client refuses any non-paper destination.

## Two ways to operate

**Automated (default on a hosted box).** With the backend running continuously
and `AUTO_APPLY_AFTER_BUILD=true`, `AUTO_START_TRADING=true`,
`SCHEDULER_USER_EMAIL` set to the operator account and `AUTONOMOUS_TRADING_ENABLED=true`,
the in-process scheduler does the whole day:

| Time (ET) | Scheduler event |
|---|---|
| 08:30 | build the morning universe, apply it |
| 09:30 | start the autotrader (refuses if today's universe isn't applied or the account isn't paper) |
| 16:05 | stop the autotrader (positions are already flat from the 15:45 EOD exit) |
| 16:30 | email the daily summary |

The operator's job is to glance at the readiness banner before the open and read
the summary email after the close. Failure alerts arrive by email if SMTP is
configured.

**Manual.** Same steps by hand: build the universe, inspect it, apply it, start
the autotrader, leave it alone, review after the close.

```bash
cd Backend
./venv/bin/python -m scripts.build_morning_universe      # ~2 min, writes data/morning_universe_<date>.json
```

Then in `app.html`: check the Morning Universe panel (snapshot age green, top
candidates carry visible catalysts, no single "ghost" score with no news), click
**Apply today's universe**, and click **Start AI Trading** a few minutes before
09:30. With Autonomous off, each qualifying match becomes an approval card you
accept or let expire.

### When not to apply the morning universe

- Snapshot older than 6 hours — rebuild instead.
- All candidate scores below ~0.5 — let the audited base universe run alone.
- One candidate dominates by 3× with no obvious news — likely a data glitch.

Skipping the apply is safe: the autotrader runs on `TRAINING_UNIVERSE` and tags
trades `universe_source = "base"`.

## During the session

Hands off. Intervene only for genuine broker problems (Alpaca outage, rejected
orders, account-level errors). A trade that looks like it will lose is the
experiment, not a reason to intervene.

## After the close

Check the Recent Trades panel for anything left open or any fill error. The
database (`Backend/data/tradehack-dev.db`) is the record; no journaling needed.

## Weekly / monthly audit

```bash
cd Backend
./venv/bin/python -m scripts.audit_universe_sources --days 7    # Mondays
./venv/bin/python -m scripts.audit_universe_sources --days 30   # after 30+ trading days
```

The audit reports post-friction average R and win rate for base vs. dynamic
universe trades and flags bleed cells (symbol × source with n ≥ 5 and
average R below −0.30). Act on bleed cells at the monthly decision, not
mid-window — changing scoring weights, gates or universe size mid-window makes
the record harder to interpret.

## Kill switches

| Action | How |
|---|---|
| Stop the autotrader | dashboard **Stop AI Trading** |
| Flatten everything | dashboard close-all, or `DELETE /trading/positions` |
| Reset the universe to base | dashboard **↻ Default** on the Trading Universe editor |
| Hard stop | `pkill -f "uvicorn app.main"` — on restart the lifespan restores today's applied universe if it exists, else falls back to base |

None of these touch the audit trail; trade rows and apply-snapshot files stay on
disk.

## Files that matter

| Path | What it is |
|---|---|
| `Backend/.env` | configuration (never committed) |
| `Backend/data/tradehack-dev.db` | the trade database |
| `Backend/data/morning_universe_<date>.json` | daily screener output |
| `Backend/data/applied_morning_universe_<date>_<time>.json` | apply audit trail |
| `Backend/data/readiness/` | readiness-guard events |
| `~/.tradehack/` | scheduler state, scan stats, matched-outcome telemetry |

## Frozen-config discipline

Treat scoring weights, gates, the base universe and the dynamic cap as frozen for
the length of any evaluation window. Real bugs get fixed and noted; tuning
mid-window restarts the window.
