"""Browser-level rendering test: HTML-looking values in the fields the audit
flagged (the AI recommendation block, pattern, reasoning, risks) must appear
as literal text in a real browser — no elements created, no script run —
and the page's normal controls must still be present.

Runs the real backend (in-process uvicorn, DEMO_MODE) and the real static
frontend server, with ``analyze_chart`` replaced by a probe payload. Skips
when Playwright/Chromium is unavailable or the app's fixed ports are busy.
"""
from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
import urllib.request

import pytest

pw_api = pytest.importorskip("playwright.sync_api")
import uvicorn  # noqa: E402

from conftest import OPERATOR, ROOT, app, provision  # noqa: E402
from app.routers import market as market_router  # noqa: E402
from app.schemas import IndicatorSnapshot  # noqa: E402

BACKEND_PORT, FRONTEND_PORT = 8000, 3000      # Frontend/config.js hard-codes :8000

PROBE_FIELDS = {
    "pattern": "<b>probe-pattern</b>",
    "setup": "<b>probe-setup</b>",
    "entry_zone": '<img src=x onerror="window.__xss=1">probe-entry',
    "stop_loss": "<i>probe-stop</i>",
    "target": "<u>probe-target</u>",
    "risk_reward": "<s>probe-rr</s>",
    "reasoning": "<script>window.__xss=2</script>probe-reasoning",
    "risks": "<b>probe-risks</b>",
}


def _probe_analysis() -> dict:
    ind = {}
    for name, f in IndicatorSnapshot.model_fields.items():
        ann = str(f.annotation)
        ind[name] = "probe-ind" if "str" in ann else 1.0
    return {
        "trend": "bullish", "trend_strength": "strong",
        "pattern": PROBE_FIELDS["pattern"],
        "key_levels": {"support": 1.0, "resistance": 2.0, "vwap": 1.5},
        "recommendation": {
            "direction": "long", "setup": PROBE_FIELDS["setup"],
            "entry_zone": PROBE_FIELDS["entry_zone"], "stop_loss": PROBE_FIELDS["stop_loss"],
            "target": PROBE_FIELDS["target"], "risk_reward": PROBE_FIELDS["risk_reward"],
            "confidence": 0.9,
        },
        "reasoning": PROBE_FIELDS["reasoning"], "risks": PROBE_FIELDS["risks"],
        "indicators": ind,
    }


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def _wait_http(url: str, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError(f"not reachable: {url}")


@pytest.fixture
def stack(monkeypatch):
    if not (_port_free(BACKEND_PORT) and _port_free(FRONTEND_PORT)):
        pytest.skip(f"ports {BACKEND_PORT}/{FRONTEND_PORT} are in use")
    monkeypatch.setattr(market_router, "analyze_chart",
                        lambda symbol, bars, **kw: _probe_analysis())
    provision(OPERATOR)

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=BACKEND_PORT,
                                           log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    frontend = subprocess.Popen([sys.executable, "serve.py", str(FRONTEND_PORT)],
                                cwd=ROOT / "Frontend",
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait_http(f"http://127.0.0.1:{BACKEND_PORT}/health")
        _wait_http(f"http://127.0.0.1:{FRONTEND_PORT}/app.html")
        yield f"http://127.0.0.1:{FRONTEND_PORT}"
    finally:
        frontend.terminate()
        server.should_exit = True
        thread.join(15)


def test_untrusted_analysis_text_renders_literally(stack):
    base = stack
    with pw_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # browser binaries not installed
            pytest.skip(f"chromium unavailable: {exc}")
        page = browser.new_page()
        page.goto(f"{base}/login.html")
        page.get_by_role("textbox", name="Email").fill(OPERATOR["email"])
        page.get_by_role("textbox", name="Password").fill(OPERATOR["password"])
        page.get_by_role("button", name="Login").click()
        page.wait_for_url("**/app.html")

        page.goto(f"{base}/analysis.html?symbol=AAPL")
        page.get_by_text("probe-setup").first.wait_for(timeout=15_000)

        # every probe appears as LITERAL text (tags visible as characters) …
        for value in PROBE_FIELDS.values():
            assert page.get_by_text(value, exact=False).count() >= 1, value
        # … and created no elements, ran no script, loaded no resource
        assert page.locator("b", has_text="probe").count() == 0
        assert page.locator("i, u, s", has_text="probe").count() == 0
        assert page.locator("img[src='x']").count() == 0
        assert page.evaluate("typeof window.__xss") == "undefined"
        # normal controls are still there and usable
        assert page.get_by_role("button", name="Re-analyze").is_visible()
        assert page.get_by_role("button", name="5m", exact=True).is_visible()
        page.get_by_role("button", name="5m", exact=True).click()
        assert page.get_by_role("spinbutton", name="Shares").is_visible()
        browser.close()
