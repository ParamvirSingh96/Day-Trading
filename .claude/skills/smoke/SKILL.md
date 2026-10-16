---
name: smoke
description: Boot the local app end-to-end and run the ICT pattern-scanner smoke test, reporting PASS/FAIL per scanner. Use when asked to smoke-test, verify the build runs end-to-end, or run the pattern/scanner checks — especially after modifying scanner or strategy logic and before declaring a build complete (per CLAUDE.md).
---

# Smoke — boot app end-to-end + ICT pattern-scanner checks

Working dir: the project root (the folder containing `Backend/` and `Frontend/`).

> **Stack reality (verified 2026-06-06):** the frontend is **vanilla JS/HTML
> served by `Frontend/serve.py`** — there is **no React/TypeScript build**
> (no `package.json`, no vite/webpack, zero `.tsx`/`.jsx`). Ignore the
> "React + TypeScript" line in CLAUDE.md; do **not** run `npm`/`vite`.

## Safety first
- **Never restart an already-running backend** — it may be a live paper-trading
  session. Only start a server that is currently down.
- Paper/dev only. The smoke test is import-and-assert; it places no orders.

## 1. Backend — FastAPI / uvicorn on :8000
- If `:8000` is already listening → leave it; just health-check:
  `curl -s -w " HTTP %{http_code}\n" http://127.0.0.1:8000/health` (expect 200).
- If down → `cd Backend && nohup ./venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 > /tmp/smoke_backend.log 2>&1 & disown`,
  then poll `/health` until 200 (fail after ~15s).

## 2. Frontend — file server on :3000
- If `:3000` is already listening → leave it; confirm:
  `curl -sI http://127.0.0.1:3000/app.html | head -1` (expect `200`).
- If down → `cd Frontend && nohup python3 serve.py 3000 > /tmp/smoke_frontend.log 2>&1 & disown`,
  then confirm `app.html` returns 200.

(`./start-lan.sh` from the project root does steps 1–2 with skip-if-running in
one command, but it runs in the foreground.)

## 3. ICT pattern-scanner smoke test
From `Backend/`:
```
env PYTHONPATH=. venv/bin/python scripts/smoke_ict_v7_playbook.py
```
Asserts the live ICT playbook scanners (Judas Swing, Power of 3, Turtle Soup,
Multi-Day PD, …) and prints a `PASS`/`FAIL` line per check. Succeeds only when it
ends with **"All v7 smoke checks passed"** (exit 0).

## 4. Report
- **Backend:** up + `/health` 200?
- **Frontend:** serving `app.html`?
- **Scanners:** list each `PASS`/`FAIL` line from the smoke output. If any `FAIL`
  (or non-zero exit), the build is **not** complete — name the failing scanner(s).
