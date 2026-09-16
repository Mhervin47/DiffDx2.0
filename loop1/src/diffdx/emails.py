"""Transactional emails sent via Resend: the account-creation welcome
email and the forgot-password reset code. Same "best-effort, silent
no-op if RESEND_API_KEY isn't set" contract as otp.py's send_otp_email
and main.py's _send_reminder_email — a failed or skipped send never
blocks the request that triggered it.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

_log = logging.getLogger(__name__)


def _send_via_resend(to_email: str, subject: str, html: str) -> bool:
    resend_key = os.environ.get("RESEND_API_KEY", "")
    if not resend_key:
        _log.info("RESEND_API_KEY not set — skipping email to %s (%s)", to_email, subject)
        return False
    from_addr = os.environ.get("RESEND_FROM", "reminders@diffdx.app")
    try:
        payload = json.dumps({
            "from": from_addr,
            "to": [to_email],
            "subject": subject,
            "html": html,
        }).encode()
        req = urllib.request.Request(
            "https://api.resend.com/emails",
            data=payload,
            headers={"Authorization": f"Bearer {resend_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status < 300
    except urllib.error.HTTPError as exc:
        # Resend's error body names the actual problem (e.g. "from" domain
        # not verified, invalid recipient) — str(exc) alone is just
        # "HTTP Error 403: Forbidden", not useful for diagnosing a silent
        # "no mail received" report, so log the body too.
        body = ""
        try:
            body = exc.read().decode(errors="replace")
        except Exception:
            pass
        _log.warning("Resend email failed for %s (%s) from=%s: HTTP %s %s", to_email, subject, from_addr, exc.code, body)
        return False
    except Exception as exc:
        _log.warning("Resend email failed for %s (%s) from=%s: %s", to_email, subject, from_addr, exc)
        return False


def send_welcome_email(to_email: str, name: str) -> bool:
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;padding:32px 24px;background:#f8fafc;border-radius:12px;">
      <div style="background:linear-gradient(135deg,#0a2540 0%,#0077a8 100%);border-radius:10px 10px 0 0;padding:28px;">
        <h1 style="color:#fff;font-size:22px;margin:0;font-weight:800;">Welcome to DiffDx</h1>
        <p style="color:rgba(255,255,255,.75);font-size:13px;margin:6px 0 0;">Your account is ready</p>
      </div>
      <div style="background:#fff;border-radius:0 0 10px 10px;padding:28px;border:1px solid #e2ecf4;border-top:none;">
        <p style="font-size:15px;color:#0a2540;">Hi {name},</p>
        <p style="font-size:14px;color:#334155;line-height:1.6;">
          Thanks for creating a DiffDx account. You can now start a diagnostic session, keep track of
          your visit history, and book appointments with our partner doctors — all from your portal.
        </p>
        <a href="https://diffdx.app/patient-overview.html" style="display:inline-block;background:#0077a8;color:#fff;text-decoration:none;padding:12px 24px;border-radius:8px;font-size:14px;font-weight:700;margin-top:8px;">Go to My Portal</a>
        <p style="font-size:12px;color:#64748b;margin-top:20px;">If you didn't create this account, you can safely ignore this email.</p>
      </div>
    </div>"""
    return _send_via_resend(to_email, "Welcome to DiffDx!", html)


def send_password_reset_email(to_email: str, name: str, code: str) -> bool:
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;padding:32px 24px;background:#f8fafc;border-radius:12px;">
      <div style="background:#0a2540;border-radius:10px 10px 0 0;padding:24px 28px;">
        <h1 style="color:#fff;font-size:20px;margin:0;font-weight:800;">DiffDx</h1>
        <p style="color:rgba(255,255,255,.7);font-size:13px;margin:6px 0 0;">Reset your password</p>
      </div>
      <div style="background:#fff;border-radius:0 0 10px 10px;padding:28px;border:1px solid #e2ecf4;border-top:none;">
        <p style="font-size:15px;color:#0a2540;">Hi {name},</p>
        <p style="font-size:14px;color:#334155;line-height:1.6;">Use this code to reset your DiffDx password:</p>
        <div style="background:#f0f9ff;border:1px solid #d0e4ef;border-radius:8px;padding:16px 20px;margin:20px 0;text-align:center;">
          <div style="font-size:32px;font-weight:800;letter-spacing:.2em;color:#0a2540;">{code}</div>
        </div>
        <p style="font-size:12px;color:#64748b;">This code expires in 10 minutes. If you didn't request a password reset, you can safely ignore this email — your password won't be changed.</p>
      </div>
    </div>"""
    return _send_via_resend(to_email, f"Reset your DiffDx password: {code}", html)
