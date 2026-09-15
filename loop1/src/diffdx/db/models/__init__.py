"""Import every model module so each class registers on Base.metadata.

Alembic's autogenerate and Base.metadata.create_all() only see tables whose
model classes have actually been imported somewhere — this module is that
somewhere. Import diffdx.db.models (this package) before touching either.
"""

from diffdx.db.models.audit import AuditLogEntry
from diffdx.db.models.clinical import (
    AppointmentIntake,
    DiagnosticSession,
    Prescription,
    PrescriptionHistoryBatch,
    Referral,
    RefillRequest,
    SecondOpinion,
    SessionTurn,
    SuggestedTest,
    TreatmentPlanItem,
)
from diffdx.db.models.dsr import DsrErasureRequest
from diffdx.db.models.files import UploadedFile
from diffdx.db.models.messaging import Message, MessageThread
from diffdx.db.models.reports import MessageReport
from diffdx.db.models.scheduling import (
    Appointment,
    BlockedDate,
    DoctorSlot,
    RescheduleProposal,
    Waitlist,
)
from diffdx.db.models.usage import LlmUsageEvent, SarvamUsageEvent
from diffdx.db.models.user import Dependent, Doctor, Patient, RefreshToken, User
from diffdx.db.models.verification import EmailOtp

__all__ = [
    "User",
    "EmailOtp",
    "Patient",
    "Doctor",
    "Dependent",
    "RefreshToken",
    "Appointment",
    "DoctorSlot",
    "BlockedDate",
    "Waitlist",
    "RescheduleProposal",
    "DiagnosticSession",
    "SessionTurn",
    "SuggestedTest",
    "Prescription",
    "PrescriptionHistoryBatch",
    "Referral",
    "RefillRequest",
    "AppointmentIntake",
    "SecondOpinion",
    "TreatmentPlanItem",
    "MessageThread",
    "Message",
    "UploadedFile",
    "AuditLogEntry",
    "LlmUsageEvent",
    "SarvamUsageEvent",
    "DsrErasureRequest",
    "MessageReport",
]
