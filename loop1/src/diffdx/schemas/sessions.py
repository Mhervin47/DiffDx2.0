"""Request schemas for /api/session/*, /api/cases/* — moved verbatim from
web/api.py as part of Task 4's router split. Field-for-field identical."""
from __future__ import annotations

from pydantic import BaseModel


class StartRequest(BaseModel):
    case_id: str
    session_language: str = "en-IN"


class SymptomIn(BaseModel):
    name: str
    onset: str = ""
    severity: str = ""
    notes: str = ""


class HistoryIn(BaseModel):
    medical: list[str] = []
    medications: list[str] = []
    allergies: list[str] = []
    family: list[str] = []
    social: list[str] = []


class StartCustomRequest(BaseModel):
    """Start a session from a patient-provided free-form profile."""
    age: int | None = None
    sex: str | None = None
    occupation: str | None = None
    bmi: float | None = None
    chief_complaint: str
    symptoms: list[SymptomIn] = []
    history: HistoryIn = HistoryIn()
    free_notes: str = ""
    session_language: str = "en-IN"


class TurnRequest(BaseModel):
    patient_answer: str


class BookRequest(BaseModel):
    doctor_id: str
    slot: str
