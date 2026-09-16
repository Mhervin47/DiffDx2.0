"""Request schemas for /api/auth/* — moved verbatim from web/api.py as part
of Task 4's router split. Field-for-field identical; no behavior change."""
from __future__ import annotations

from pydantic import BaseModel


class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


class VerifyOtpRequest(BaseModel):
    email: str
    code: str


class ResendOtpRequest(BaseModel):
    email: str


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    email: str
    code: str
    new_password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class ProfileUpdateRequest(BaseModel):
    name: str | None = None
    mobile: str | None = None
    age: int | None = None
    blood_type: str | None = None
    gender: str | None = None
    address: str | None = None
    emergency_contact_name: str | None = None
    emergency_contact_phone: str | None = None
    allergies: str | None = None
    chronic_conditions: str | None = None


class DependentRequest(BaseModel):
    name: str
    relationship: str  # child | parent | spouse | sibling | other
    age: int | None = None
    gender: str | None = None
    blood_type: str | None = None
    allergies: str | None = None
    chronic_conditions: str | None = None
