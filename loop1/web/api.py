"""DiffDx Web API — thin entry-point shim.

`app` is assembled in diffdx/main.py (app factory, middleware, exception
handlers, lifespan — week1.md Task 4's target layout). This file exists
only so:

- `web.api:app` keeps working for every deploy config that already
  targets it — Procfile, render.yaml, Dockerfile — none of which need to
  change.
- `from web.api import <helper>` keeps working for tests/scripts that
  import shared helpers directly from here (tests/test_auth.py,
  tests/test_appointment_composer.py, tests/test_appointment_repositories.py,
  scripts/concurrency_demo.py) — re-exporting is enough since
  `from X import Y` binds Y in this module's own namespace, same
  backward-compat mechanism Task 21 already established for
  diffdx.legacy_store.

No `sys.path` manipulation needed here — every real way this module gets
imported (Procfile, render.yaml, Dockerfile, pytest) already sets
PYTHONPATH=src independently before this file is ever reached, and
diffdx.main (imported below) does its own belt-and-suspenders sys.path
insert besides.
"""
from __future__ import annotations

from diffdx.main import app
from diffdx.legacy_store import (
    _CASES_DIR,
    _MAX_FILE_BYTES,
    _add_session_to_user,
    _compose_appointment_dict,
    _compose_user_dict,
    _db_load,
    _db_save,
    _dt_iso,
    _ensure_relational_appointment,
    _get_final_differential,
    _get_user_from_request,
    _hash_password,
    _load_appointments,
    _load_blocked_dates,
    _load_doctors,
    _load_file_data,
    _load_report_from_disk,
    _load_session_report_from_db,
    _load_session_uploads,
    _load_user_sessions,
    _load_users,
    _load_waitlist,
    _require_doctor,
    _sarvam_translate,
    _sarvam_tts_b64,
    _save_appointments,
    _save_blocked_dates,
    _save_doctors,
    _save_file_data,
    _save_session_report,
    _save_session_uploads,
    _save_user_sessions,
    _save_waitlist,
    _send_email_notification,
    _session_test_uploads,
    _static_dir,
    _update_session_in_user,
    _user_from_access_token,
    _verify_password,
)
