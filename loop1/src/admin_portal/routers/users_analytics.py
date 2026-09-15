"""
Users analytics — admin portal. Acquisition, activity, engagement, and
retention for patients and doctors, kept structurally separate throughout
because they are not comparable populations in this data model:

- Patients self-register. Doctors are always seeded (scripts/seed_doctors.py),
  never self-registered — a spike in doctor rows is an ops roster action, not
  organic growth. Never chart the two as if they mean the same thing.
- There is no account deactivation, no soft-delete, no last_login_at, and no
  subscription/plan concept anywhere in this codebase (diffdx.db.models.user.User
  has none of these). "Active"/"dormant" here is derived entirely from
  AuditLogEntry login events; there is no other source for it.
- The only real "leaving" signals that exist are (a) inactivity and (b) DSR
  erasure — but erasure is a rare, explicit, privacy-driven hard delete, not
  a churn event, and is never counted or charted as churn anywhere below.
  Everything here says "dormant"/"at risk"/"inactive," never "churn."
- DiagnosticSession.termination_reason == "user_quit" is the real
  session-abandonment signal — but it is only reachable from the offline/CLI
  driver (loop1/session.py), never from the live web session driver
  (web/api_session.py, which only ever writes max_turns/safety_stop/
  confidence_threshold). On a live-traffic-only deployment, user_quit (and
  therefore abandonment_rate_pct) will genuinely read 0 — that is not a bug
  in this endpoint, it is an accurate reflection of what the live path can
  produce. See sop.html#users.
- Appointment.cancelled_by has three real values: "patient" and
  "patient_reschedule" (written in routers/appointments4.py), and "doctor"
  (written by DELETE /api/doctor/appointments/{appt_id} in
  routers/appointments.py — a doctor cancelling their own schedule). There is
  deliberately no admin-initiated cancellation path: admins have no right to
  cancel a patient's appointment in this product, so no "admin" value is ever
  written and none should be charted.

Naive vs aware datetimes, handled explicitly rather than mixed:
  User.created_at, AuditLogEntry.created_at, Appointment.booked_at — naive.
  DiagnosticSession.started_at/ended_at, Appointment.cancelled_at — aware
  (DateTime(timezone=True)). Every window boundary below is computed twice
  (a naive-UTC version and an aware-UTC version) and each query uses
  whichever matches the column it's filtering — never a naive boundary
  against an aware column or vice versa. This is the same "compare like
  with like" discipline audit.py's _parse_boundary already applies, just
  needed in both directions here instead of one.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from admin_portal._bucketing import build_daily_series, to_date
from diffdx.db.engine import get_sessionmaker
from diffdx.db.models.audit import AuditLogEntry
from diffdx.db.models.clinical import DiagnosticSession
from diffdx.db.models.scheduling import Appointment
from diffdx.db.models.user import User
from diffdx.dependencies import require_role

router = APIRouter(tags=["admin_portal"])

_SUMMARY_SUFFICIENT_MIN = 5
_ENGAGEMENT_SUFFICIENT_MIN = 5
_RETENTION_COHORT_SUFFICIENT_MIN = 3

_TERMINATION_REASONS = ("max_turns", "safety_stop", "confidence_threshold", "user_quit")


def _now() -> tuple[datetime, datetime]:
    """(aware_utc, naive_utc) — both anchored to the same instant, for
    callers that need to filter both aware and naive columns from one call."""
    aware = datetime.now(timezone.utc)
    return aware, aware.replace(tzinfo=None)


@router.get("/api/admin/users/summary")
def get_users_summary(_admin: dict = Depends(require_role("admin"))) -> dict[str, Any]:
    now_aware, now_naive = _now()
    win7_naive = now_naive - timedelta(days=7)
    win30_naive = now_naive - timedelta(days=30)
    win30_aware = now_aware - timedelta(days=30)
    stuck_cutoff_aware = now_aware - timedelta(hours=24)

    with get_sessionmaker()() as db:
        total_patients = db.execute(
            select(func.count()).select_from(User).where(User.role == "patient")
        ).scalar_one()
        total_doctors = db.execute(
            select(func.count()).select_from(User).where(User.role == "doctor")
        ).scalar_one()

        new_patients_7d = db.execute(
            select(func.count()).select_from(User)
            .where(User.role == "patient", User.created_at >= win7_naive)
        ).scalar_one()
        new_patients_30d = db.execute(
            select(func.count()).select_from(User)
            .where(User.role == "patient", User.created_at >= win30_naive)
        ).scalar_one()

        def _active_patients(window_start_naive: datetime) -> int:
            login_ids = (
                select(AuditLogEntry.actor_user_id)
                .where(
                    AuditLogEntry.action == "login",
                    AuditLogEntry.created_at >= window_start_naive,
                    AuditLogEntry.actor_user_id.is_not(None),
                )
                .distinct()
            )
            return db.execute(
                select(func.count()).select_from(User)
                .where(User.role == "patient", User.id.in_(login_ids))
            ).scalar_one()

        active_patients_7d = _active_patients(win7_naive)
        active_patients_30d = _active_patients(win30_naive)

        active_ids_30d = (
            select(AuditLogEntry.actor_user_id)
            .where(
                AuditLogEntry.action == "login",
                AuditLogEntry.created_at >= win30_naive,
                AuditLogEntry.actor_user_id.is_not(None),
            )
            .distinct()
        )
        dormant_patients_30d = db.execute(
            select(func.count()).select_from(User)
            .where(
                User.role == "patient",
                User.created_at < win30_naive,
                User.id.notin_(active_ids_30d),
            )
        ).scalar_one()

        # repeat_usage_rate_pct: of patients with >=1 DiagnosticSession or
        # Appointment ever (combined count across both), the % with >=2.
        session_patient_ids = select(DiagnosticSession.patient_id).where(
            DiagnosticSession.patient_id.is_not(None)
        )
        appt_patient_ids = select(Appointment.patient_id)
        combined = session_patient_ids.union_all(appt_patient_ids).subquery()
        per_patient_counts = (
            select(combined.c.patient_id, func.count().label("n"))
            .group_by(combined.c.patient_id)
            .subquery()
        )
        total_with_activity = db.execute(
            select(func.count()).select_from(per_patient_counts)
        ).scalar_one()
        with_repeat = db.execute(
            select(func.count()).select_from(per_patient_counts)
            .where(per_patient_counts.c.n >= 2)
        ).scalar_one()
        repeat_usage_rate_pct = (
            round(with_repeat / total_with_activity * 100, 1) if total_with_activity else 0.0
        )

        # avg_session_minutes / avg_turns_per_session — all-time snapshot,
        # ended_at IS NOT NULL only (an in-progress session has no duration
        # to average in, and averaging null-as-zero would silently drag the
        # mean down every time a session is simply still open).
        ended = db.execute(
            select(DiagnosticSession.started_at, DiagnosticSession.ended_at, DiagnosticSession.total_turns)
            .where(DiagnosticSession.ended_at.is_not(None))
        ).all()
        durations_min = [
            (ended_at - started_at).total_seconds() / 60.0
            for started_at, ended_at, _turns in ended
            if started_at is not None and ended_at is not None
        ]
        avg_session_minutes = round(sum(durations_min) / len(durations_min), 1) if durations_min else 0.0
        turn_counts = [t for _s, _e, t in ended if t is not None]
        avg_turns_per_session = round(sum(turn_counts) / len(turn_counts), 1) if turn_counts else 0.0

        # abandonment_rate_pct: user_quit / all sessions with a
        # termination_reason set, last 30d (by started_at — the reason
        # describes how the session ended, but we window by when it started,
        # matching how a "sessions from the last 30 days" cohort reads).
        term_reasons_30d = db.execute(
            select(DiagnosticSession.termination_reason)
            .where(
                DiagnosticSession.termination_reason.is_not(None),
                DiagnosticSession.started_at >= win30_aware,
            )
        ).scalars().all()
        total_term = len(term_reasons_30d)
        user_quit_count = sum(1 for r in term_reasons_30d if r == "user_quit")
        abandonment_rate_pct = round(user_quit_count / total_term * 100, 1) if total_term else 0.0

        # stuck_sessions: current snapshot, not windowed.
        stuck_sessions = db.execute(
            select(func.count()).select_from(DiagnosticSession)
            .where(DiagnosticSession.ended_at.is_(None), DiagnosticSession.started_at < stuck_cutoff_aware)
        ).scalar_one()

        # cancellation_rate_pct: cancelled / booked, last 30d, by booked_at (naive).
        total_booked_30d = db.execute(
            select(func.count()).select_from(Appointment).where(Appointment.booked_at >= win30_naive)
        ).scalar_one()
        cancelled_30d = db.execute(
            select(func.count()).select_from(Appointment)
            .where(Appointment.booked_at >= win30_naive, Appointment.status == "cancelled")
        ).scalar_one()
        cancellation_rate_pct = (
            round(cancelled_30d / total_booked_30d * 100, 1) if total_booked_30d else 0.0
        )

    return {
        "status": "ok",
        "data_sufficient": (total_patients + total_doctors) >= _SUMMARY_SUFFICIENT_MIN,
        "total_patients": total_patients,
        "total_doctors": total_doctors,
        "new_patients_7d": new_patients_7d,
        "new_patients_30d": new_patients_30d,
        "active_patients_7d": active_patients_7d,
        "active_patients_30d": active_patients_30d,
        "dormant_patients_30d": dormant_patients_30d,
        "repeat_usage_rate_pct": repeat_usage_rate_pct,
        "avg_session_minutes": avg_session_minutes,
        "avg_turns_per_session": avg_turns_per_session,
        "abandonment_rate_pct": abandonment_rate_pct,
        "stuck_sessions": stuck_sessions,
        "cancellation_rate_pct": cancellation_rate_pct,
    }


@router.get("/api/admin/users/timeseries")
def get_users_timeseries(
    days: int = Query(default=30, ge=1, le=180),
    _admin: dict = Depends(require_role("admin")),
) -> dict[str, Any]:
    now_aware, now_naive = _now()
    start_naive = (now_naive - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    start_aware = start_naive.replace(tzinfo=timezone.utc)
    start_date = start_naive.date()
    end_date = now_naive.date()

    with get_sessionmaker()() as db:
        def _by_day_new_users(role: str) -> dict[date, int]:
            rows = db.execute(
                select(func.date(User.created_at), func.count())
                .select_from(User)
                .where(User.role == role, User.created_at >= start_naive)
                .group_by(func.date(User.created_at))
            ).all()
            return {to_date(d): c for d, c in rows}

        def _by_day_active(role: str) -> dict[date, int]:
            rows = db.execute(
                select(func.date(AuditLogEntry.created_at), func.count(func.distinct(AuditLogEntry.actor_user_id)))
                .select_from(AuditLogEntry)
                .join(User, User.id == AuditLogEntry.actor_user_id)
                .where(
                    AuditLogEntry.action == "login",
                    AuditLogEntry.created_at >= start_naive,
                    User.role == role,
                )
                .group_by(func.date(AuditLogEntry.created_at))
            ).all()
            return {to_date(d): c for d, c in rows}

        def _by_day_cancellations(cancelled_by_value: str) -> dict[date, int]:
            rows = db.execute(
                select(func.date(Appointment.cancelled_at), func.count())
                .select_from(Appointment)
                .where(
                    Appointment.status == "cancelled",
                    Appointment.cancelled_at.is_not(None),
                    Appointment.cancelled_at >= start_aware,
                    Appointment.cancelled_by == cancelled_by_value,
                )
                .group_by(func.date(Appointment.cancelled_at))
            ).all()
            return {to_date(d): c for d, c in rows}

        new_patients = _by_day_new_users("patient")
        new_doctors = _by_day_new_users("doctor")
        active_patients = _by_day_active("patient")
        active_doctors = _by_day_active("doctor")
        # Three real cancelled_by values — see module docstring. No
        # admin-cancellation series: admins have no right to cancel, so
        # "admin" is never written and never charted.
        cancellations_patient = _by_day_cancellations("patient")
        cancellations_patient_reschedule = _by_day_cancellations("patient_reschedule")
        cancellations_doctor = _by_day_cancellations("doctor")

        total_patients = db.execute(
            select(func.count()).select_from(User).where(User.role == "patient")
        ).scalar_one()
        total_doctors = db.execute(
            select(func.count()).select_from(User).where(User.role == "doctor")
        ).scalar_one()

    series = build_daily_series(
        start_date, end_date,
        new_patients=new_patients,
        new_doctors=new_doctors,
        active_patients=active_patients,
        active_doctors=active_doctors,
        cancellations_patient=cancellations_patient,
        cancellations_patient_reschedule=cancellations_patient_reschedule,
        cancellations_doctor=cancellations_doctor,
    )

    return {
        "status": "ok",
        "days": days,
        "data_sufficient": (total_patients + total_doctors) >= _SUMMARY_SUFFICIENT_MIN,
        "series": series,
    }


@router.get("/api/admin/users/engagement")
def get_users_engagement(
    days: int = Query(default=30, ge=1, le=180),
    _admin: dict = Depends(require_role("admin")),
) -> dict[str, Any]:
    now_aware, _now_naive = _now()
    start_aware = (now_aware - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)

    with get_sessionmaker()() as db:
        ended_in_window = db.execute(
            select(DiagnosticSession.started_at, DiagnosticSession.ended_at, DiagnosticSession.total_turns)
            .where(
                DiagnosticSession.ended_at.is_not(None),
                DiagnosticSession.started_at >= start_aware,
            )
        ).all()

        term_reasons = db.execute(
            select(DiagnosticSession.termination_reason)
            .where(
                DiagnosticSession.termination_reason.is_not(None),
                DiagnosticSession.started_at >= start_aware,
            )
        ).scalars().all()

    minutes_by_day: dict[date, list[float]] = {}
    turns_by_day: dict[date, list[int]] = {}
    for started_at, ended_at, total_turns in ended_in_window:
        if started_at is None or ended_at is None:
            continue
        d = started_at.date()
        minutes_by_day.setdefault(d, []).append((ended_at - started_at).total_seconds() / 60.0)
        if total_turns is not None:
            turns_by_day.setdefault(d, []).append(total_turns)

    avg_minutes_by_day = {d: round(sum(v) / len(v), 1) for d, v in minutes_by_day.items()}
    avg_turns_by_day = {d: round(sum(v) / len(v), 1) for d, v in turns_by_day.items()}

    series = build_daily_series(
        start_aware.date(), now_aware.date(),
        avg_session_minutes=avg_minutes_by_day,
        avg_turns_per_session=avg_turns_by_day,
    )

    breakdown = {reason: 0 for reason in _TERMINATION_REASONS}
    for reason in term_reasons:
        if reason in breakdown:
            breakdown[reason] += 1

    return {
        "status": "ok",
        "days": days,
        "data_sufficient": len(ended_in_window) >= _ENGAGEMENT_SUFFICIENT_MIN,
        "series": series,
        "termination_breakdown": breakdown,
    }


@router.get("/api/admin/users/retention")
def get_users_retention(
    weeks: int = Query(default=12, ge=1, le=52),
    _admin: dict = Depends(require_role("admin")),
) -> dict[str, Any]:
    _now_aware, now_naive = _now()
    cutoff_naive = now_naive - timedelta(weeks=weeks)

    with get_sessionmaker()() as db:
        patients = db.execute(
            select(User.id, User.created_at).where(User.role == "patient")
        ).all()

        session_rows = db.execute(
            select(DiagnosticSession.patient_id, DiagnosticSession.started_at)
            .where(DiagnosticSession.patient_id.is_not(None), DiagnosticSession.started_at.is_not(None))
        ).all()
        appt_rows = db.execute(
            select(Appointment.patient_id, Appointment.booked_at)
        ).all()

    # Weekly cohorts keyed by the Monday of each patient's signup week.
    cohorts: dict[date, list[uuid.UUID]] = {}
    for user_id, created_at in patients:
        if created_at < cutoff_naive:
            continue
        week_start = created_at.date() - timedelta(days=created_at.weekday())
        cohorts.setdefault(week_start, []).append(user_id)

    # Combined activity timestamps per patient, both sources normalized to
    # naive UTC — DiagnosticSession.started_at is aware, Appointment.booked_at
    # is already naive.
    activity_by_patient: dict[uuid.UUID, list[datetime]] = {}
    for patient_id, started_at in session_rows:
        ts = started_at.astimezone(timezone.utc).replace(tzinfo=None) if started_at.tzinfo else started_at
        activity_by_patient.setdefault(patient_id, []).append(ts)
    for patient_id, booked_at in appt_rows:
        activity_by_patient.setdefault(patient_id, []).append(booked_at)

    cohort_out: list[dict[str, Any]] = []
    for week_start in sorted(cohorts.keys()):
        member_ids = cohorts[week_start]
        cohort_size = len(member_ids)
        returned = 0
        for patient_id in member_ids:
            times = sorted(activity_by_patient.get(patient_id, []))
            if len(times) < 2:
                continue
            first = times[0]
            if any(first < t <= first + timedelta(days=30) for t in times[1:]):
                returned += 1
        retention_pct = round(returned / cohort_size * 100, 1) if cohort_size else 0.0
        cohort_out.append({
            "cohort_week": week_start.isoformat(),
            "cohort_size": cohort_size,
            "returned_within_30d": returned,
            "retention_pct": retention_pct,
        })

    data_sufficient = any(c["cohort_size"] >= _RETENTION_COHORT_SUFFICIENT_MIN for c in cohort_out)

    return {
        "status": "ok",
        "weeks": weeks,
        "data_sufficient": data_sufficient,
        "cohorts": cohort_out,
    }
