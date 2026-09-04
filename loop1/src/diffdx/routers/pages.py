"""HTML page routes — serve clean URLs without the .html extension. Task
4's sixth split-out router. Logic unchanged from the original.

_static_dir is defined much later in web/api.py (near the StaticFiles
mount at the bottom of the file) but was already being looked up lazily
in the original code — the `_html` lambda only resolves it when called,
by which point the whole module has finished loading. Preserved here the
same way via a lazy import inside the function (lambdas can't hold an
import statement, so this became a small function instead — same
behavior, since it's still only evaluated at call time).
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


@router.get("/history")
async def page_history(): return _html("history.html")


@router.get("/login")
async def page_login(): return _html("login.html")


@router.get("/doctor-portal")
async def page_doctor_portal(): return _html("doctor-portal.html")


@router.get("/doctor-portal.html")
async def page_doctor_portal_html(): return _html("doctor-portal.html")


@router.get("/session/{session_id}")
async def page_session(session_id: str): return _html("session.html")


@router.get("/report/{session_id}")
async def page_report(session_id: str): return _html("report.html")
