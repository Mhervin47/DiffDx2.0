"""Request schemas for the appointments/doctor/patient domain — moved
verbatim from web/api.py as part of Task 4's router split. Each was
verified used by exactly one route before moving; field-for-field
identical to the originals."""
from __future__ import annotations

from pydantic import BaseModel


class NotesRequest(BaseModel):
    notes: str


class StatusRequest(BaseModel):
    status: str  # "upcoming" | "seen" | "no_show"


class TestOrderItem(BaseModel):
    id: str
    test: str
    category: str       # blood | imaging | other
    priority: str       # routine | urgent | stat
    notes: str = ""
    ordered_at: str


class TestOrdersRequest(BaseModel):
    test_orders: list[TestOrderItem]


class ReferralRequest(BaseModel):
    specialty: str
    to_doctor: str = ""   # empty = any available specialist
    urgency: str = "Routine"
    notes: str = ""
    internal_note: str = ""   # doctor-only, never returned to patient endpoints
    referred_at: str = ""


class PlanItem(BaseModel):
    id: str
    text: str
    approved: bool = True
    source: str = "ai"   # "ai" | "doctor"


class ApprovedPlanRequest(BaseModel):
    plan: list[PlanItem]


class RescheduleRequest(BaseModel):
    slot: str


class SlotsRequest(BaseModel):
    slots: list[str]


class TestResultItem(BaseModel):
    test_id: str        # matches an existing test order id
    result: str
    status: str = "pending"   # pending | normal | abnormal | critical
    recorded_at: str


class TestResultsRequest(BaseModel):
    test_results: list[TestResultItem]


class PrescriptionItem(BaseModel):
    id: str
    drug: str
    dose: str
    route: str = "oral"
    frequency: str
    duration: str
    meal_timing: str = ""
    notes: str = ""
    prescribed_at: str


class PrescriptionsRequest(BaseModel):
    prescriptions: list[PrescriptionItem]
    force_new_batch: bool = False


class FollowUpRequest(BaseModel):
    slot: str
    notes: str = ""


class DoctorSummaryRequest(BaseModel):
    summary: str


class WaitlistRequest(BaseModel):
    doctor_id: str
    doctor_name: str
    specialty: str
    note: str = ""


class BlockDateRequest(BaseModel):
    date: str  # "YYYY-MM-DD"
    reason: str = ""


class TagsRequest(BaseModel):
    tags: list[str]


class SecondOpinionRequest(BaseModel):
    to_doctor_id: str
    to_doctor_name: str
    note: str = ""


class SecondOpinionResponseRequest(BaseModel):
    response: str


class ProposeRescheduleRequest(BaseModel):
    proposed_slot: str
    reason: str = ""


class RescheduleResponseRequest(BaseModel):
    action: str  # 'accept' or 'decline'
