"""HTML page routes — serve clean URLs without the .html extension. Task
4's sixth split-out router. Logic unchanged from the original.

_static_dir is defined much later in web/api.py (near the StaticFiles
mount at the bottom of the file) but was already being looked up lazily
in the original code — the `_html` lambda only resolves it when called,
by which point the whole module has finished loading. Preserved here the
same way via a lazy import inside the function (lambdas can't hold an
import statement, so this became a small function instead — same
behavior, since it's still only evaluated at call time).

Patient/doctor role gating was tried here via `Depends(require_role(...))`
and reverted: this app's auth is a bearer token (Authorization header) read
from localStorage by JS, never a cookie — a plain browser navigation (an
<a href>, window.location.replace, typing a URL) has no way to attach that
header, only a fetch()/XHR call can. So a server-side dependency on a page
ROUTE 401s on every normal navigation, including a legitimately signed-in
patient/doctor landing here right after login — that's not a hardened
check, it's a broken page. Enforcement for these pages now lives client-
side instead (each page's own inline script: getAuthUser(), redirect on
missing/wrong role), the same working pattern doctor-portal.html/
doctor-profile.html/doctor-analytics.html already used before any of this —
see those files, and patient-overview.html/patient-info.html/
doctor-overview.html which now match them.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import FileResponse
from diffdx.legacy_store import (
    _static_dir,
)

router = APIRouter(tags=["pages"])


def _html(name: str) -> FileResponse:

    return FileResponse(
        _static_dir / name,
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/patient-info")
async def page_patient_info(): return _html("patient-info.html")


@router.get("/patient-info.html")
async def page_patient_info_html(): return _html("patient-info.html")


@router.get("/patient-overview.html")
async def page_patient_overview(): return _html("patient-overview.html")


@router.get("/patient-landing.html")
async def page_patient_landing(): return _html("patient-landing.html")


@router.get("/history")
async def page_history(): return _html("history.html")


@router.get("/login")
async def page_login(): return _html("login.html")


@router.get("/doctor-portal")
async def page_doctor_portal(): return _html("doctor-portal.html")


@router.get("/doctor-portal.html")
async def page_doctor_portal_html(): return _html("doctor-portal.html")


@router.get("/doctor-overview.html")
async def page_doctor_overview(): return _html("doctor-overview.html")


@router.get("/doctor-landing.html")
async def page_doctor_landing(): return _html("doctor-landing.html")


@router.get("/session/{session_id}")
async def page_session(session_id: str): return _html("session.html")


@router.get("/report/{session_id}")
async def page_report(session_id: str): return _html("report.html")
