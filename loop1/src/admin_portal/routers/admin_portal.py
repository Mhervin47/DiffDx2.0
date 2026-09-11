"""
Read-only admin portal API + page serving — admin portal, Phase 1.

Imported by diffdx.main, which has already put loop1/src on sys.path by the
time this import happens (see build brief Section 3.4) — no sys.path
manipulation needed in this file. Verify that with the standalone import
check before wiring this into main.py:

    cd loop1/src
    python -c "from admin_portal.routers.admin_portal import router"

Why this file also serves web/admin_portal/*.html and js/*.js, not just the
/api/admin/* JSON endpoints: main.py's only static mount points at
web/static/ (the patient/doctor portal), and diffdx.routers.pages (existing,
read-only) only serves pages out of that same directory. Nothing mounts
web/admin_portal/ anywhere, so without routes here the pages we built would
never be reachable through the running app at all — only through a separate
static file server, which is not how this app is actually deployed.

The natural fix — `router.mount("/admin_portal", StaticFiles(...))` — was
tried and empirically does NOT work: a Mount added to an APIRouter silently
404s once that router is included into the app via app.include_router(); only
a mount registered directly on the FastAPI app object is honored. Since the
one approved main.py edit (Section 5.6) is already spent and is not to be
touched again for this, page serving is done here instead via explicit,
path-validated FileResponse routes — the same pattern diffdx.routers.pages
already uses for the main site.

Router has NO prefix (unlike a typical sub-router) so that route paths below
can be spelled out in full on each decorator — this keeps the already-tested
external URLs (/api/admin/evidence, /api/admin/quality, /api/admin/config)
byte-for-byte unchanged while giving the new page routes their own clean
/admin_portal/... paths, all through the one `router` object main.py already
imports. No second app.include_router() call, no main.py changes at all
beyond the two lines already made.

Scaling note: config.yaml content and the git SHA are read/computed ONCE at
module import time (this module is imported exactly once per process, at
app startup) rather than on every request to /api/admin/config — avoids
spawning a `git` subprocess and re-parsing YAML on every hit. Neither value
can change without a redeploy (= a new process) anyway.
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from diffdx.dependencies import require_role

_log = logging.getLogger(__name__)

router = APIRouter(tags=["admin_portal"])

# routers/admin_portal.py -> routers -> admin_portal -> src -> loop1 (4 parent hops)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent

# The PUBLISHED copy under web/, not the working copy under data/admin_portal/
# — only web/ is what the Dockerfile's `COPY ./web` step ships, and only the
# published copy is guaranteed current in a real deployment (see
# publish_report.py and build brief Section 3.4).
_PUBLISHED_REPORT_PATH = _REPO_ROOT / "web" / "admin_portal" / "data" / "comparison_report.json"
_CONFIG_PATH = _REPO_ROOT / "config.yaml"

_WEB_ADMIN_DIR = (_REPO_ROOT / "web" / "admin_portal").resolve()
_WEB_ADMIN_JS_DIR = (_WEB_ADMIN_DIR / "js").resolve()

_NOT_GENERATED_MESSAGE = "Run compare_report.py then publish_report.py to generate this report."


# ---------------------------------------------------------------------------
# Config — read once at import time, not per-request (see module docstring)
# ---------------------------------------------------------------------------

def _load_static_config() -> dict[str, Any]:
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        _log.warning("Failed to read config.yaml at %s", _CONFIG_PATH, exc_info=True)
        return {}


def _load_git_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


_STATIC_CONFIG = _load_static_config()
_GIT_SHA = _load_git_sha()


# ---------------------------------------------------------------------------
# JSON API — unchanged external paths, spelled out in full (see module docstring)
# ---------------------------------------------------------------------------

def _load_published_report() -> dict[str, Any] | None:
    if not _PUBLISHED_REPORT_PATH.exists():
        return None
    with open(_PUBLISHED_REPORT_PATH, encoding="utf-8") as f:
        return json.load(f)


@router.get("/api/admin/evidence")
def get_evidence(_admin: dict = Depends(require_role("admin"))) -> dict[str, Any]:
    """Full comparison report, or a graceful not-generated placeholder — never a 404/500.

    Originally public by design (Admin_Portal.md: "if a judge has to log in
    to see accuracy numbers, they will not see them"). Locked behind
    require_role("admin") at the project owner's explicit request — every
    /api/admin/* route is now consistently admin-gated. If judge/reviewer
    access without an account matters later, that's a real product decision
    to revisit deliberately, not something to silently half-do.
    """
    report = _load_published_report()
    if report is None:
        return {"status": "not_generated", "message": _NOT_GENERATED_MESSAGE, "systems": None}
    return {"status": "ok", **report}


@router.get("/api/admin/quality")
def get_quality(_admin: dict = Depends(require_role("admin"))) -> dict[str, Any]:
    """reasoning_quality + cross_reference only, pulled from the same published report (one source of truth).

    Also admin-gated now — see get_evidence()'s docstring for why.
    """
    report = _load_published_report()
    if report is None:
        return {
            "status": "not_generated",
            "message": _NOT_GENERATED_MESSAGE,
            "reasoning_quality": None,
            "cross_reference": None,
        }
    return {
        "status": "ok",
        "reasoning_quality": report.get("reasoning_quality"),
        "cross_reference": report.get("cross_reference"),
    }


@router.get("/api/admin/config")
def get_config(_admin: dict = Depends(require_role("admin"))) -> dict[str, Any]:
    """Live model/prompt/threshold config plus the current git short SHA. Read-only, no editing endpoint.

    Also admin-gated now — see get_evidence()'s docstring for why.
    """
    return {
        "models": _STATIC_CONFIG.get("models", {}),
        "prompt_versions": _STATIC_CONFIG.get("prompt_versions", {}),
        "thresholds": _STATIC_CONFIG.get("thresholds", {}),
        "git_sha": _GIT_SHA,
    }


# ---------------------------------------------------------------------------
# Page serving — path-validated, not per-file routes (see module docstring)
#
# Scales to future pages/assets (Phase 2's dsr.html, audit.html, usage.js,
# etc.) with zero new route code: dropping a new .html file into
# web/admin_portal/ or a new .js file into web/admin_portal/js/ makes it
# servable immediately. Safety comes from resolving the real path and
# checking containment, not from a filename allow-list.
# ---------------------------------------------------------------------------

def _resolve_within(base: Path, *parts: str) -> Path | None:
    """Join parts onto base and return the resolved path only if it's still
    inside base (blocks .., absolute-path segments, and symlink escapes)."""
    candidate = base.joinpath(*parts).resolve()
    if not candidate.is_relative_to(base):
        return None
    return candidate


@router.get("/admin_portal", include_in_schema=False)
@router.get("/admin_portal/", include_in_schema=False)
def admin_portal_index() -> FileResponse:
    return _serve_html("index.html")


@router.get("/admin_portal/{page_name}.html", include_in_schema=False)
def admin_portal_page(page_name: str) -> FileResponse:
    return _serve_html(f"{page_name}.html")


@router.get("/admin_portal/js/{filename}", include_in_schema=False)
def admin_portal_js(filename: str) -> FileResponse:
    path = _resolve_within(_WEB_ADMIN_JS_DIR, filename)
    if path is None or not path.is_file() or path.suffix != ".js":
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path, media_type="text/javascript", headers={"Cache-Control": "no-store"})


def _serve_html(filename: str) -> FileResponse:
    path = _resolve_within(_WEB_ADMIN_DIR, filename)
    if path is None or not path.is_file() or path.suffix != ".html":
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path, media_type="text/html", headers={"Cache-Control": "no-store"})
