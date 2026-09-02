"""Import every model module so each class registers on Base.metadata.

Alembic's autogenerate and Base.metadata.create_all() only see tables whose
model classes have actually been imported somewhere — this module is that
somewhere. Import diffdx.db.models (this package) before touching either.
"""

from diffdx.db.models.audit import AuditLogEntry
from diffdx.db.models.clinical import (
    DiagnosticSession,
    Prescription,
    Referral,
    SecondOpinion,
    SessionTurn,
    SuggestedTest,
    TreatmentPlanItem,
)
from diffdx.db.models.files import UploadedFile
from diffdx.db.models.messaging import Message, MessageThread
from diffdx.db.models.scheduling import Appointment, BlockedDate, DoctorSlot, Waitlist
from diffdx.db.models.user import Dependent, Doctor, Patient, RefreshToken, User

__all__ = [
    "User",
    "Patient",
    "Doctor",
    "Dependent",
    "RefreshToken",
    "Appointment",
    "DoctorSlot",
    "BlockedDate",
    "Waitlist",
    "DiagnosticSession",
    "SessionTurn",
    "SuggestedTest",
    "Prescription",
    "Referral",
    "SecondOpinion",
    "TreatmentPlanItem",
    "MessageThread",
    "Message",
    "UploadedFile",
    "AuditLogEntry",
]
