# Hosting TradeHack on a cloud VM (or Raspberry Pi)

Goal: run the app 24/7 so the scheduler (build 08:30 → start 09:30 → stop
16:00 → summary 16:30 ET) fires every weekday without your laptop being on.

**Topology:** one small Linux box runs both processes (backend uvicorn on
`:8000`, frontend no-cache server on `:3000`). You reach the dashboard
**privately over Tailscale** — nothing is exposed to the public internet.
`systemd` keeps both processes alive across crashes and reboots.

This kit is host-agnostic: the same steps work on a Hetzner/DigitalOcean
VM or a Raspberry Pi (both Linux + systemd). Recommended: **Hetzner CX22
(~€4/mo) or DigitalOcean $6/mo droplet, Ubuntu 24.04 LTS.**

---

## 0. Critical constraint: Python 3.14

`requirements.txt` pins `numpy==2.4.4` / `pandas==3.0.2`, which only have
wheels for **Python 3.14**. Older Python → they build from source and fail.
Ubuntu 24.04 ships 3.12, so you must install 3.14. Easiest path is `uv`
(handles the interpreter + venv in one tool). Steps below use it.

---

## 1. Provision the VM

- Hetzner Cloud → CX22 (2 vCPU / 4GB) or DigitalOcean → Basic $6 droplet
- Image: **Ubuntu 24.04 LTS**
- Add your SSH key during creation
- 1GB RAM is technically enough; 2-4GB gives headroom for the bars cache + backtests

SSH in as root, then harden the basics:

```bash
# Create the service user (the app runs as this non-root user)
adduser --disabled-password --gecos "" tradehack
mkdir -p /opt/tradehack
chown tradehack:tradehack /opt/tradehack

# Timezone → ET so the scheduler's clock + journald logs line up
timedatectl set-timezone America/New_York

apt update && apt -y upgrade
apt -y install rsync curl git build-essential
```

---

## 2. Tailscale (private access — do this before opening any ports)

```bash
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up
# follow the printed URL to authenticate; note the assigned 100.x.y.z IP
tailscale ip -4        # → your VM's tailnet IP, e.g. 100.101.102.103
```

Install Tailscale on your phone/laptop too (same account). Now your devices
can reach the VM at `100.x.y.z` with no public exposure.

### Firewall: allow SSH + tailnet only, block the app ports publicly

```bash
apt -y install ufw
ufw allow 22/tcp                      # SSH
ufw allow in on tailscale0            # anything over the tailnet
ufw default deny incoming
ufw default allow outgoing            # Alpaca / Anthropic / SMTP need egress
ufw enable
```

`:3000` and `:8000` are now reachable **only** over Tailscale (and localhost).
Even though uvicorn binds `0.0.0.0`, ufw blocks the public internet.

---

## 3. Install Python 3.14 + uv

```bash
su - tradehack
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc            # put uv on PATH
uv python install 3.14
```

---

## 4. Get the code onto the VM

**From your Mac**, send the `.env` once (it's excluded from `sync.sh` so
code re-syncs never clobber it), then push the code:

```bash
cd /path/to/tradehack   # the repo root on your workstation

# one-time: create the dir + copy the .env securely
ssh tradehack@100.x.y.z 'mkdir -p /opt/tradehack/Backend'
scp Backend/.env tradehack@100.x.y.z:/opt/tradehack/Backend/.env

# push the code (repeat this whenever you change app code)
chmod +x deploy/sync.sh
./deploy/sync.sh tradehack@100.x.y.z
```

---

## 5. Build the venv + smoke test (on the VM)

```bash
ssh tradehack@100.x.y.z
cd /opt/tradehack/Backend
uv venv --python 3.14 venv
uv pip install --python venv/bin/python -r requirements.txt

# sanity check — must print "All v7 smoke checks passed"
env PYTHONPATH=. venv/bin/python scripts/smoke_ict_v7_playbook.py
```

---

## 6. Adjust `.env` for hosted access

Edit `/opt/tradehack/Backend/.env`. The paper-only safety flags stay exactly
as they are (`ALPACA_PAPER=true`, `LIVE_TRADING_ENABLED=no`). Two changes:

1. **Add your Tailscale dashboard origin to `CORS_ORIGINS`** so the browser's
   API calls aren't blocked. Append (use YOUR tailnet IP):
   ```
   ,http://100.x.y.z:3000
   ```
   (`config.js` derives the API base from the page's hostname, so loading the
   dashboard at `http://100.x.y.z:3000` makes it call `http://100.x.y.z:8000`
   — that origin must be allow-listed.)

2. Leave `SESSION_COOKIE_SECURE=false` (you're on HTTP over Tailscale, not
   HTTPS). If you later put a TLS reverse proxy in front, flip it to `true`.

---

## 7. Install + start the systemd services

```bash
# (still on the VM, as a sudoer — exit back to root or use sudo)
sudo cp /opt/tradehack/deploy/tradehack-backend.service /etc/systemd/system/
sudo cp /opt/tradehack/deploy/tradehack-frontend.service /etc/systemd/system/
# NOTE: sync.sh excludes deploy/, so copy the two .service files up manually
#       once (scp), or paste them — see "service files" note below.

sudo systemctl daemon-reload
sudo systemctl enable --now tradehack-backend tradehack-frontend
sudo systemctl status tradehack-backend --no-pager
```

> **Service files:** `sync.sh` excludes `deploy/` (it's Mac-side tooling), so
> the two `.service` files won't be on the VM via sync. Either `scp` them up
> once (`scp deploy/*.service tradehack@100.x.y.z:/tmp/ && ssh ... sudo mv
> /tmp/*.service /etc/systemd/system/`) or paste their contents directly.

---

## 8. Verify

```bash
# on the VM
curl -s http://127.0.0.1:8000/health        # → {"status":"ok",...}
journalctl -u tradehack-backend -n 30 --no-pager   # scheduler_started line?

# from your phone/laptop (on Tailscale)
# open http://100.x.y.z:3000/app.html  → log in, dashboard loads
```

Confirm the scheduler logged its next event:
```
scheduler_next_event name=... when=...ET
```

---

## 9. Day-to-day

| Task | Command |
|---|---|
| Watch live logs | `journalctl -u tradehack-backend -f` |
| Restart after a code change | `sudo systemctl restart tradehack-backend tradehack-frontend` |
| Push new code from Mac | `./deploy/sync.sh tradehack@100.x.y.z` then restart |
| Check it survived a reboot | `sudo reboot`, wait, `curl …/health` |
| Pull the trade DB back to inspect | `scp tradehack@100.x.y.z:/opt/tradehack/Backend/data/tradehack-dev.db ./` |

---

## Notes / gotchas

- **Persistence:** the trade DB (`data/tradehack-dev.db`), `~/.tradehack/`
  state/scan_stats/matched_outcomes, and bars cache all live on the VM disk —
  persistent across restarts. `sync.sh` never overwrites the DB.
- **Email summaries** still work (Gmail SMTP egress is allowed by ufw). They'll
  now arrive whether or not your laptop is on — that's the whole point.
- **Autonomous mode** boots ON (`AUTONOMOUS_TRADING_ENABLED=true` in `.env`),
  so the VM will auto-trade paper the moment the scheduler starts at 09:30 ET.
  If you want a few days of observation first, set it `false` before step 7.
- **Cost control:** a CX22/$6 droplet idles cheaply. You can stop it on
  weekends if you want, but the scheduler is fail-closed (it won't trade
  without a fresh applied universe), so leaving it on is harmless.
- **Moving to a Pi later:** identical from step 3 onward (install uv, build
  venv, same systemd units). Only difference is you skip the cloud provider.
