"""
Email alerts for scheduler events (v7.16+, 2026-05-13)
=======================================================
Two public entry points:
  * ``send_failure_alert(subject, body)`` — one-shot when a scheduler step
    fails (build / apply / start / stop / summary).
  * ``send_daily_summary(subject, body)`` — EOD digest sent at close+30min.

Both wrap a private ``_send_email`` helper that owns all SMTP behavior in
one place, so any future change (different provider, TLS variant, retries)
lands in exactly one spot.

Graceful when unconfigured
--------------------------
If ``SMTP_USER`` / ``SMTP_APP_PASSWORD`` / ``SMTP_TO`` are empty OR the
password is still one of the documented placeholder values, the function
logs at INFO and returns ``False`` without raising. The scheduler keeps
running. Reason: an email-config bug should never break unattended trading.

Environment identity
--------------------
Every email's subject is prefixed with the environment mode
(``TradeHack PAPER`` / ``TradeHack LIVE``) so the first glance at any
landing email tells you which mode sent it. The body's footer adds
hostname, ``app_env``, ``alpaca_paper``, ``alpaca_trading_url``, and
``LIVE_TRADING_ENABLED`` so you can trace back to the exact box/config
that produced the alert.
"""
from __future__ import annotations

import logging
import os
import smtplib
import socket
from email.message import EmailMessage

from .settings import settings

logger = logging.getLogger("tradehack.email_alerts")


# Plans have used two different placeholder strings; accept either as
# "unconfigured" so a typo doesn't accidentally send an email with a
# bogus password (which would then fail noisily at smtp.login).
_PASSWORD_PLACEHOLDERS = {
    "PASTE_NEW_APP_PASSWORD_HERE",
    "PASTE_YOUR_APP_PASSWORD_HERE",
}


def _is_configured() -> bool:
    """True iff all required SMTP fields are non-empty AND the password
    is not one of the documented placeholder values."""
    return bool(
        settings.smtp_user
        and settings.smtp_app_password
        and settings.smtp_app_password not in _PASSWORD_PLACEHOLDERS
        and settings.smtp_to
    )


def _env_subject_prefix() -> str:
    """``TradeHack PAPER`` if paper mode, ``TradeHack LIVE`` otherwise.
    Surfaces paper-vs-live at a glance in the inbox."""
    mode = "PAPER" if settings.alpaca_paper else "LIVE"
    return f"TradeHack {mode}"


def _build_env_footer() -> str:
    """Identifying footer appended to every email body.

    Reads ``LIVE_TRADING_ENABLED`` directly from the environment because
    it isn't surfaced on the Settings dataclass today. If that flag gets
    promoted to a real Settings field in the future, switch the read
    here — the email body stays consistent either way.
    """
    return (
        "\n\n---\n"
        f"app_env:                {settings.app_env}\n"
        f"alpaca_paper:           {settings.alpaca_paper}\n"
        f"alpaca_trading_url:     {settings.alpaca_trading_url}\n"
        f"live_trading_enabled:   {os.getenv('LIVE_TRADING_ENABLED', '(unset)')}\n"
        f"hostname:               {socket.gethostname()}\n"
    )


def _send_email(subject: str, body: str, *, kind: str) -> bool:
    """Send one plaintext email via Gmail SMTP submission (587 STARTTLS).

    ``kind`` is a short tag ("alert" | "summary") used in the subject and
    in log lines for grouping.

    Returns ``True`` on success, ``False`` on either "not configured" or
    any send failure. Never raises. Logs at INFO when skipping due to
    missing config; at WARNING on send failure with exc_info.
    """
    if not _is_configured():
        logger.info("email_skip_not_configured kind=%s subject=%r", kind, subject)
        return False

    full_subject = f"{_env_subject_prefix()} {kind}: {subject}"
    full_body = body + _build_env_footer()

    msg = EmailMessage()
    msg["From"] = settings.smtp_from or settings.smtp_user
    msg["To"] = settings.smtp_to
    msg["Subject"] = full_subject
    msg.set_content(full_body)

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as srv:
            srv.starttls()
            srv.login(settings.smtp_user, settings.smtp_app_password)
            srv.send_message(msg)
        logger.info("email_sent kind=%s subject=%r", kind, full_subject)
        return True
    except Exception as exc:
        logger.warning(
            "email_send_failed kind=%s subject=%r err=%s",
            kind, full_subject, exc, exc_info=True,
        )
        return False


def send_failure_alert(subject: str, body: str) -> bool:
    """Send a one-shot scheduler failure alert.

    Subject becomes: ``TradeHack PAPER alert: <subject>``.
    Use for build/apply/start/stop/summary step failures.
    """
    return _send_email(subject, body, kind="alert")


def send_daily_summary(subject: str, body: str) -> bool:
    """Send the EOD digest.

    Subject becomes: ``TradeHack PAPER summary: <subject>``.
    """
    return _send_email(subject, body, kind="summary")
