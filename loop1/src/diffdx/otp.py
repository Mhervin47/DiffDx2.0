"""Email OTP for patient registration (week1.md follow-up — doctors are
seeded via scripts/seed_doctors.py, never self-register, so there is no
doctor flow to attach this to).

A 6-digit code, hashed (never stored in plaintext) via EmailOtpRepository,
expires after OTP_TTL_MINUTES, and is invalidated after MAX_ATTEMPTS wrong
guesses (forcing a resend) — a 6-digit code is only 1-in-a-million, so the
attempt cap is what actually makes it safe, not the code length alone.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import urllib.request
from datetime import datetime, timedelta, timezone

_log = logging.getLogger(__name__)

OTP_TTL_MINUTES = 10
MAX_ATTEMPTS = 5


def generate_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def code_matches(code: str, code_hash: str) -> bool:
    return secrets.compare_digest(hash_code(code), code_hash)


def expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=OTP_TTL_MINUTES)


def is_expired(expires_at: datetime) -> bool:
    exp = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) > exp


def send_otp_email(to_email: str, name: str, code: str) -> bool:
    """Resend first (matches the app's documented primary email path),
    SMTP fallback via legacy_store._send_email_notification, silent no-op
    if neither is configured — same "email is optional in dev" contract
    every other notification in this app already follows."""
    resend_key = os.environ.get("RESEND_API_KEY", "")
    if resend_key:
        try:
            from_addr = os.environ.get("RESEND_FROM", "reminders@diffdx.app")
            html = f"""
            <div style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;padding:32px 24px;background:#f8fafc;border-radius:12px;">
              <div style="background:#0a2540;border-radius:10px 10px 0 0;padding:24px 28px;">
                <h1 style="color:#fff;font-size:20px;margin:0;font-weight:800;">DiffDx</h1>
                <p style="color:rgba(255,255,255,.7);font-size:13px;margin:6px 0 0;">Verify your email</p>
              </div>
              <div style="background:#fff;border-radius:0 0 10px 10px;padding:28px;border:1px solid #e2ecf4;border-top:none;">
                <p style="font-size:15px;color:#0a2540;">Hi {name},</p>
                <p style="font-size:14px;color:#334155;line-height:1.6;">Use this code to verify your email and finish creating your account:</p>
                <div style="background:#f0f9ff;border:1px solid #d0e4ef;border-radius:8px;padding:16px 20px;margin:20px 0;text-align:center;">
                  <div style="font-size:32px;font-weight:800;letter-spacing:.2em;color:#0a2540;">{code}</div>
                </div>
                <p style="font-size:12px;color:#64748b;">This code expires in {OTP_TTL_MINUTES} minutes. If you didn't request this, you can ignore this email.</p>
              </div>
            </div>"""
            payload = json.dumps({
                "from": from_addr,
                "to": [to_email],
                "subject": f"Your DiffDx verification code: {code}",
                "html": html,
            }).encode()
            req = urllib.request.Request(
                "https://api.resend.com/emails",
                data=payload,
                headers={"Authorization": f"Bearer {resend_key}", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                if r.status < 300:
                    return True
        except Exception as exc:
            _log.warning("Resend OTP email failed for %s: %s", to_email, exc)

    from diffdx.legacy_store import _send_email_notification
    try:
        _send_email_notification(
            to_email,
            f"Your DiffDx verification code: {code}",
            f"Hi {name},\n\nYour DiffDx verification code is {code}. It expires in {OTP_TTL_MINUTES} minutes.\n\nIf you didn't request this, you can ignore this email.",
        )
        return bool(os.environ.get("SMTP_HOST"))
    except Exception as exc:
        _log.warning("SMTP OTP email failed for %s: %s", to_email, exc)
        return False
