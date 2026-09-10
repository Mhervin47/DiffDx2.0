"""
Publish the working comparison report into the location the deployed app can
actually serve — admin portal, Phase 1.

loop1/data/admin_portal/comparison_report.json (written by compare_report.py)
is offline-only: it lives under data/, which the Dockerfile never copies into
the deployed image (same treatment as phase7_sessions/, phase7_critiques/,
etc — see the Dockerfile's own comment). loop1/web/admin_portal/data/ is a
subfolder of web/, which the Dockerfile's `COPY ./web` step DOES ship. The
router (admin_portal/routers/admin_portal.py) reads exclusively from the
published copy this script creates, never from the working copy.

This is a deliberate, explicit step — not run automatically by
compare_report.py — so skipping it is a visible choice, not a forgotten one.

Usage:
    python src/admin_portal/eval/publish_report.py
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

_log = logging.getLogger(__name__)

# src/admin_portal/eval/publish_report.py -> eval -> admin_portal -> src -> loop1
_LOOP1_ROOT = Path(__file__).resolve().parent.parent.parent.parent

_WORKING_COPY = _LOOP1_ROOT / "data" / "admin_portal" / "comparison_report.json"
_PUBLISHED_COPY = _LOOP1_ROOT / "web" / "admin_portal" / "data" / "comparison_report.json"


def publish() -> Path:
    if not _WORKING_COPY.exists():
        raise SystemExit(
            f"Working report not found at {_WORKING_COPY}.\n"
            "Run `python src/admin_portal/eval/compare_report.py` first to generate it."
        )
    _PUBLISHED_COPY.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_WORKING_COPY, _PUBLISHED_COPY)
    return _PUBLISHED_COPY


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    published = publish()
    _log.info("Published %s -> %s", _WORKING_COPY, published)
    _log.info("This is the only copy the router and a real deployment ever read.")


if __name__ == "__main__":
    main()
