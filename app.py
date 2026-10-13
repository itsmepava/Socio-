from __future__ import annotations

import os
import hmac
import secrets
import base64
import ipaddress
import csv
import io
import hashlib
import smtplib
from html import escape
from email.message import EmailMessage
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
import json
import re
from urllib.parse import urlsplit

import click
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from flask import Flask, Response, abort, flash, g, redirect, render_template, request, send_from_directory, session, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import CheckConstraint, Index, UniqueConstraint, func, inspect, or_, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from PIL import Image, ImageOps, UnidentifiedImageError
import qrcode

db = SQLAlchemy()
csrf = CSRFProtect()
limiter = Limiter(key_func=get_remote_address, default_limits=[])
password_hasher = PasswordHasher()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def valid_email(value: str) -> bool:
    if len(value) > 254 or " " in value or value.count("@") != 1:
        return False
    local, domain = value.rsplit("@", 1)
    return bool(local and domain and "." in domain and not domain.startswith(".") and not domain.endswith("."))


def safe_internal_path(candidate: str | None) -> str | None:
    if not candidate or not candidate.startswith("/") or candidate.startswith("//"):
        return None
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc or candidate.startswith("/\\"):
        return None
    return candidate


def is_local_development_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    if hostname.casefold() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return address.is_loopback or address.is_private


def qr_png_data_uri(value: str) -> str:
    image_buffer = io.BytesIO()
    qrcode.make(value).save(image_buffer, format="PNG")
    encoded = base64.b64encode(image_buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def user_display_name(user: User | None) -> str:
    if not user:
        return ""
    if user.full_name and user.full_name.strip():
        return user.full_name.strip()
    local_name = user.email.split("@", 1)[0]
    parts = local_name.replace(".", " ").replace("_", " ").replace("-", " ").split()
    return " ".join(part.capitalize() for part in parts) or "Socio member"


def can_view_organization_updates(organization_id: int) -> bool:
    user = getattr(g, "user", None)
    if not user:
        return False
    if user.role == "platform_admin":
        return True
    if user.role == "organization_admin":
        return user.organization_id == organization_id
    return Membership.query.filter_by(user_id=user.id, organization_id=organization_id).first() is not None


def normalize_uploaded_image(raw: bytes, max_side: int, max_output_bytes: int) -> bytes:
    """Decode and re-encode supported raster images, discarding source metadata."""
    with Image.open(io.BytesIO(raw)) as source:
        if source.format not in ("PNG", "JPEG", "WEBP"):
            raise ValueError("Choose a PNG, JPEG, or WebP image.")
        if source.width < 1 or source.height < 1 or source.width * source.height > 20_000_000:
            raise ValueError("The image dimensions are too large. Choose an image under 20 megapixels.")
        source.verify()
    with Image.open(io.BytesIO(raw)) as source:
        image = ImageOps.exif_transpose(source)
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
        normalized = io.BytesIO()
        image.save(normalized, format="PNG", optimize=True)
    result = normalized.getvalue()
    if len(result) > max_output_bytes:
        raise ValueError("The processed image is still too large. Choose a simpler or smaller image.")
    return result


def spreadsheet_safe(value: object) -> str:
    text_value = "" if value is None else str(value)
    if text_value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text_value
    return text_value


class Organization(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(254), nullable=False, unique=True, index=True)
    status = db.Column(db.String(16), nullable=False, default="Pending")
    membership_approval_required = db.Column(db.Boolean, nullable=False, default=True, server_default=text("1"))
    recommendation_required = db.Column(db.Boolean, nullable=False, default=False, server_default=text("0"))
    application_prompt = db.Column(db.String(500), nullable=False, default="", server_default="")
    onboarding_form = db.Column(db.Text, nullable=False, default="[]", server_default="[]")
    logo_filename = db.Column(db.String(80), nullable=True)
    loyalty_reward_enabled = db.Column(db.Boolean, nullable=False, default=False, server_default=text("0"))
    loyalty_reward_after_days = db.Column(db.Integer, nullable=False, default=30, server_default=text("30"))
    loyalty_reward_days = db.Column(db.Integer, nullable=False, default=30, server_default=text("30"))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (CheckConstraint("status in ('Pending','Approved','Suspended')"),)
    users = db.relationship("User", back_populates="organization")
    plans = db.relationship("Plan", back_populates="organization")
    memberships = db.relationship("Membership", back_populates="organization")
    offers = db.relationship("OrganizationOffer", back_populates="organization")
    events = db.relationship("OrganizationEvent", back_populates="organization")


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(254), nullable=False, index=True)
    full_name = db.Column(db.String(120), nullable=True)
    gender = db.Column(db.String(40), nullable=True)
    phone_number = db.Column(db.String(32), nullable=True)
    profile_image = db.Column(db.LargeBinary, nullable=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(24), nullable=False)
    organization_id = db.Column(db.Integer, db.ForeignKey("organization.id"), nullable=True, index=True)
    active = db.Column(db.Boolean, nullable=False, default=True)
    email_verified = db.Column(db.Boolean, nullable=False, default=True)
    # JSON list of server-enforced admin permissions. None keeps legacy admins
    # fully privileged; new staff accounts always receive an explicit scope.
    admin_permissions = db.Column(db.Text, nullable=True)
    admin_title = db.Column(db.String(60), nullable=True)
    session_nonce = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (
        CheckConstraint("role in ('platform_admin','organization_admin','member')"),
        CheckConstraint("(role = 'platform_admin' and organization_id is null) or (role = 'organization_admin' and organization_id is not null) or role = 'member'"),
        Index("uq_platform_admin_email", "email", unique=True,
              sqlite_where=text("role = 'platform_admin'"), postgresql_where=text("role = 'platform_admin'")),
        Index("uq_organization_account_email", "organization_id", "email", unique=True,
              sqlite_where=text("organization_id is not null"), postgresql_where=text("organization_id is not null")),
    )
    organization = db.relationship("Organization", back_populates="users")


class Plan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organization.id"), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    price_minor = db.Column(db.Integer, nullable=False)
    billing_period = db.Column(db.String(12), nullable=False)
    benefits = db.Column(db.String(2000), nullable=False, default="")
    archived = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (
        CheckConstraint("price_minor >= 0"),
        CheckConstraint("billing_period in ('monthly','yearly')"),
    )
    organization = db.relationship("Organization", back_populates="plans")
    offers = db.relationship("OrganizationOffer", back_populates="plan")


class OrganizationOffer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organization.id"), nullable=False, index=True)
    plan_id = db.Column(db.Integer, db.ForeignKey("plan.id"), nullable=True, index=True)
    title = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(500), nullable=False)
    offer_type = db.Column(db.String(24), nullable=False, default="Extra benefit")
    start_day = db.Column(db.Integer, nullable=False, default=0)
    end_day = db.Column(db.Integer, nullable=True)
    archived = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (
        CheckConstraint("offer_type in ('Welcome bonus','Extra benefit','Limited time','Other')"),
        CheckConstraint("start_day >= 0"),
        CheckConstraint("end_day is null or end_day >= start_day"),
    )
    organization = db.relationship("Organization", back_populates="offers")
    plan = db.relationship("Plan", back_populates="offers")


class OrganizationEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organization.id"), nullable=False, index=True)
    title = db.Column(db.String(120), nullable=False)
    description = db.Column(db.String(1000), nullable=False)
    venue = db.Column(db.String(200), nullable=False, default="")
    event_date = db.Column(db.Date, nullable=False, index=True)
    poster_filename = db.Column(db.String(80), nullable=True)
    archived = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    organization = db.relationship("Organization", back_populates="events")


class Membership(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organization.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    plan_id = db.Column(db.Integer, db.ForeignKey("plan.id"), nullable=False)
    full_name = db.Column(db.String(120), nullable=False)
    status = db.Column(db.String(16), nullable=False, default="Pending")
    start_day = db.Column(db.Integer, nullable=True)
    end_day = db.Column(db.Integer, nullable=True)
    cancel_at_period_end = db.Column(db.Boolean, nullable=False, default=False)
    next_plan_id = db.Column(db.Integer, db.ForeignKey("plan.id"), nullable=True)
    last_renewal_due_day = db.Column(db.Integer, nullable=True)
    loyalty_rewarded_day = db.Column(db.Integer, nullable=True)
    loyalty_reward_days = db.Column(db.Integer, nullable=False, default=0, server_default=text("0"))
    onboarding_responses = db.Column(db.Text, nullable=False, default="{}", server_default="{}")
    consent_accepted_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", name="uq_membership_organization_user"),
        CheckConstraint("status in ('Pending','Active')"),
        CheckConstraint("start_day is null or start_day >= 0"),
        CheckConstraint("end_day is null or end_day >= 0"),
    )
    organization = db.relationship("Organization", back_populates="memberships")
    user = db.relationship("User")
    plan = db.relationship("Plan", foreign_keys=[plan_id])
    next_plan = db.relationship("Plan", foreign_keys=[next_plan_id])
    payments = db.relationship("SimulatedPayment", back_populates="membership", order_by="SimulatedPayment.id.desc()")


class MembershipApplication(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    membership_id = db.Column(db.Integer, db.ForeignKey("membership.id"), nullable=False, unique=True, index=True)
    status = db.Column(db.String(16), nullable=False, default="Pending")
    recommendation_text = db.Column(db.Text, nullable=False, default="")
    review_note = db.Column(db.String(500), nullable=False, default="")
    submitted_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    reviewed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    reviewed_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    __table_args__ = (CheckConstraint("status in ('Pending','Approved','Declined')"),)
    membership = db.relationship("Membership")
    reviewed_by = db.relationship("User")


class SimulatedPayment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organization.id"), nullable=False, index=True)
    membership_id = db.Column(db.Integer, db.ForeignKey("membership.id"), nullable=False, index=True)
    plan_id = db.Column(db.Integer, db.ForeignKey("plan.id"), nullable=False)
    amount_minor = db.Column(db.Integer, nullable=False)
    kind = db.Column(db.String(16), nullable=False)
    channel = db.Column(db.String(12), nullable=False, default="simulated")
    status = db.Column(db.String(16), nullable=False, default="Pending")
    created_day = db.Column(db.Integer, nullable=False)
    confirmed_day = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (
        CheckConstraint("amount_minor >= 0"),
        CheckConstraint("kind in ('join','upgrade','renewal')"),
        CheckConstraint("status in ('Pending','Paid','Failed','Abandoned')"),
        CheckConstraint("channel in ('simulated','offline')"),
    )
    organization = db.relationship("Organization")
    membership = db.relationship("Membership", back_populates="payments")
    plan = db.relationship("Plan")
    refund_request = db.relationship("SimulatedRefund", back_populates="payment", uselist=False)


class MemberBillingProof(db.Model):
    """Member-submitted proof of a club payment or expense for organization review."""
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organization.id"), nullable=False, index=True)
    membership_id = db.Column(db.Integer, db.ForeignKey("membership.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    record_type = db.Column(db.String(24), nullable=False)
    description = db.Column(db.String(160), nullable=False)
    amount_minor = db.Column(db.Integer, nullable=False)
    expense_date = db.Column(db.Date, nullable=False)
    member_note = db.Column(db.String(500), nullable=False, default="")
    evidence_png = db.Column(db.LargeBinary, nullable=False)
    status = db.Column(db.String(16), nullable=False, default="Submitted")
    organization_note = db.Column(db.String(500), nullable=False, default="")
    reviewed_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    submitted_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    reviewed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    __table_args__ = (
        CheckConstraint("record_type in ('Membership fee','Club expense','Other')"),
        CheckConstraint("amount_minor > 0"),
        CheckConstraint("status in ('Submitted','Accepted','Declined')"),
    )
    organization = db.relationship("Organization")
    membership = db.relationship("Membership")
    user = db.relationship("User", foreign_keys=[user_id])
    reviewed_by = db.relationship("User", foreign_keys=[reviewed_by_user_id])


class SimulationState(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    current_day = db.Column(db.Integer, nullable=False, default=0)


class ActivityLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    actor_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    action = db.Column(db.String(80), nullable=False)
    object_type = db.Column(db.String(40), nullable=False)
    object_id = db.Column(db.Integer, nullable=False)
    details = db.Column(db.String(500), nullable=False, default="")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)


class SimulatedRefund(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    payment_id = db.Column(db.Integer, db.ForeignKey("simulated_payment.id"), nullable=False, unique=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organization.id"), nullable=False, index=True)
    amount_minor = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.String(500), nullable=False)
    status = db.Column(db.String(16), nullable=False, default="Requested")
    requested_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    processed_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    created_day = db.Column(db.Integer, nullable=False)
    processed_day = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (
        CheckConstraint("amount_minor > 0"),
        CheckConstraint("status in ('Requested','Approved','Declined')"),
    )
    payment = db.relationship("SimulatedPayment", back_populates="refund_request")
    organization = db.relationship("Organization")
    requested_by = db.relationship("User", foreign_keys=[requested_by_user_id])
    processed_by = db.relationship("User", foreign_keys=[processed_by_user_id])


class MemberInvitation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organization.id"), nullable=False, index=True)
    plan_id = db.Column(db.Integer, db.ForeignKey("plan.id"), nullable=False)
    full_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(254), nullable=False, index=True)
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    status = db.Column(db.String(16), nullable=False, default="Pending")
    created_day = db.Column(db.Integer, nullable=False)
    expires_day = db.Column(db.Integer, nullable=False)
    claimed_day = db.Column(db.Integer, nullable=True)
    claimed_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    email_sent_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (CheckConstraint("status in ('Pending','Claimed','Revoked')"),)
    organization = db.relationship("Organization")
    plan = db.relationship("Plan")
    claimed_user = db.relationship("User", foreign_keys=[claimed_user_id])
    created_by = db.relationship("User", foreign_keys=[created_by_user_id])


class MemberNotification(db.Model):
    """An organization update addressed to one member or several members."""
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organization.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    title = db.Column(db.String(120), nullable=False)
    body = db.Column(db.String(2000), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    read_at = db.Column(db.DateTime(timezone=True), nullable=True)
    organization = db.relationship("Organization")
    user = db.relationship("User", foreign_keys=[user_id])
    created_by = db.relationship("User", foreign_keys=[created_by_user_id])


class MemberPlanChange(db.Model):
    """A member's request to move to another club-defined membership tier."""
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organization.id"), nullable=False, index=True)
    membership_id = db.Column(db.Integer, db.ForeignKey("membership.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    from_plan_id = db.Column(db.Integer, db.ForeignKey("plan.id"), nullable=False)
    to_plan_id = db.Column(db.Integer, db.ForeignKey("plan.id"), nullable=False)
    status = db.Column(db.String(16), nullable=False, default="Requested", index=True)
    member_note = db.Column(db.String(500), nullable=False, default="")
    organization_note = db.Column(db.String(500), nullable=False, default="")
    requested_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    reviewed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    reviewed_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    __table_args__ = (CheckConstraint("status in ('Requested','Approved','Declined')"),)
    organization = db.relationship("Organization")
    membership = db.relationship("Membership")
    user = db.relationship("User", foreign_keys=[user_id])
    from_plan = db.relationship("Plan", foreign_keys=[from_plan_id])
    to_plan = db.relationship("Plan", foreign_keys=[to_plan_id])
    reviewed_by = db.relationship("User", foreign_keys=[reviewed_by_user_id])


class MemberQrPass(db.Model):
    """Short-lived single-use pass presented by a member to organization staff."""
    id = db.Column(db.Integer, primary_key=True)
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    membership_id = db.Column(db.Integer, db.ForeignKey("membership.id"), nullable=False, index=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organization.id"), nullable=False, index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    used_at = db.Column(db.DateTime(timezone=True), nullable=True)
    revoked_at = db.Column(db.DateTime(timezone=True), nullable=True)
    membership = db.relationship("Membership")
    organization = db.relationship("Organization")


class OrganizationQrCheckin(db.Model):
    """Short-lived club code that lets an existing member check themselves in."""
    id = db.Column(db.Integer, primary_key=True)
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organization.id"), nullable=False, index=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    revoked_at = db.Column(db.DateTime(timezone=True), nullable=True)
    organization = db.relationship("Organization")
    created_by = db.relationship("User")


class MembershipAttendance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organization.id"), nullable=False, index=True)
    membership_id = db.Column(db.Integer, db.ForeignKey("membership.id"), nullable=False, index=True)
    member_pass_id = db.Column(db.Integer, db.ForeignKey("member_qr_pass.id"), nullable=True, unique=True)
    organization_code_id = db.Column(db.Integer, db.ForeignKey("organization_qr_checkin.id"), nullable=True)
    checked_in_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    checked_in_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    method = db.Column(db.String(16), nullable=False)
    __table_args__ = (
        CheckConstraint("method in ('staff_scan','member_scan')"),
        CheckConstraint("(member_pass_id is not null and organization_code_id is null) or "
                        "(member_pass_id is null and organization_code_id is not null)"),
        UniqueConstraint("membership_id", "organization_code_id", name="uq_attendance_member_club_code"),
    )
    organization = db.relationship("Organization")
    membership = db.relationship("Membership")
    checked_in_by = db.relationship("User")
    member_pass = db.relationship("MemberQrPass")
    organization_code = db.relationship("OrganizationQrCheckin")


class EmailVerificationToken(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    used_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    sent_at = db.Column(db.DateTime(timezone=True), nullable=True)
    user = db.relationship("User")


class StaffInvitation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(254), nullable=False, index=True)
    full_name = db.Column(db.String(120), nullable=False)
    role = db.Column(db.String(24), nullable=False)
    organization_id = db.Column(db.Integer, db.ForeignKey("organization.id"), nullable=True, index=True)
    admin_permissions = db.Column(db.Text, nullable=False)
    admin_title = db.Column(db.String(60), nullable=False)
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    claimed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    revoked_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (
        CheckConstraint("role in ('platform_admin','organization_admin')"),
        CheckConstraint("(role = 'platform_admin' and organization_id is null) or (role = 'organization_admin' and organization_id is not null)"),
    )
    organization = db.relationship("Organization")
    created_by = db.relationship("User")


def record_activity(action: str, object_type: str, object_id: int, details: str = "") -> None:
    db.session.add(ActivityLog(actor_user_id=getattr(g, "user", None).id if getattr(g, "user", None) else None,
                               action=action, object_type=object_type, object_id=object_id,
                               details=details[:500]))


def simulated_day() -> int:
    state = db.session.get(SimulationState, 1)
    if not state:
        state = SimulationState(id=1, current_day=0)
        db.session.add(state)
        db.session.commit()
    return state.current_day


def period_days(plan: Plan) -> int:
    return 365 if plan.billing_period == "yearly" else 30


def membership_status(membership: Membership) -> str:
    if membership.status == "Pending":
        return "Pending"
    if membership.end_day is not None and simulated_day() >= membership.end_day:
        return "Expired"
    if membership.cancel_at_period_end:
        return "Cancelled"
    return "Active"


def membership_access_active(membership: Membership) -> bool:
    """A scheduled cancellation keeps access until its current period actually ends."""
    return (membership.status == "Active" and
            (membership.end_day is None or simulated_day() < membership.end_day))


def grant_loyalty_rewards(organization: Organization, previous_day: int, current_day: int,
                          include_already_qualified: bool = False) -> int:
    """Give each continuously active member one configured, no-charge membership extension."""
    if not organization.loyalty_reward_enabled:
        return 0
    eligible = Membership.query.filter_by(organization_id=organization.id, status="Active").filter(
        Membership.cancel_at_period_end.is_(False),
        Membership.start_day.is_not(None), Membership.end_day.is_not(None),
        Membership.loyalty_rewarded_day.is_(None),
    ).all()
    rewarded = 0
    for membership in eligible:
        threshold_day = membership.start_day + organization.loyalty_reward_after_days
        reached_during_advance = previous_day < threshold_day <= current_day
        already_active_and_qualified = (include_already_qualified and threshold_day <= current_day
                                        and membership.end_day > current_day)
        if ((reached_during_advance and membership.end_day >= threshold_day)
                or already_active_and_qualified):
            membership.end_day += organization.loyalty_reward_days
            membership.loyalty_rewarded_day = current_day
            membership.loyalty_reward_days = organization.loyalty_reward_days
            record_activity("loyalty_membership_days_gifted", "membership", membership.id,
                            f"{organization.loyalty_reward_days} no-charge days after {organization.loyalty_reward_after_days} active days")
            rewarded += 1
    return rewarded


def current_organization_offers(organization_id: int) -> list[OrganizationOffer]:
    today = simulated_day()
    return OrganizationOffer.query.filter_by(organization_id=organization_id, archived=False).filter(
        OrganizationOffer.start_day <= today,
        or_(OrganizationOffer.end_day.is_(None), OrganizationOffer.end_day >= today)
    ).order_by(OrganizationOffer.id.desc()).all()


def establish_session(user: User, remember: bool = False) -> None:
    session.clear()
    # Use a browser-session cookie unless the person explicitly asks to stay signed in.
    session.permanent = bool(remember)
    user.session_nonce = secrets.token_urlsafe(32)
    db.session.commit()
    session["user_id"] = user.id
    session["session_nonce"] = user.session_nonce


def ensure_local_sqlite_schema() -> None:
    """Create demo tables and add non-destructive local billing columns."""
    if db.engine.dialect.name != "sqlite":
        return
    db.create_all()
    staff_invitation_columns = {column["name"] for column in inspect(db.engine).get_columns("staff_invitation")}
    if "revoked_at" not in staff_invitation_columns:
        with db.engine.begin() as connection:
            connection.execute(text("ALTER TABLE staff_invitation ADD COLUMN revoked_at DATETIME"))
    member_invitation_columns = {column["name"] for column in inspect(db.engine).get_columns("member_invitation")}
    if "email_sent_at" not in member_invitation_columns:
        with db.engine.begin() as connection:
            connection.execute(text("ALTER TABLE member_invitation ADD COLUMN email_sent_at DATETIME"))
    organization_columns = {column["name"] for column in inspect(db.engine).get_columns("organization")}
    with db.engine.begin() as connection:
        if "membership_approval_required" not in organization_columns:
            # Existing demo organizations keep their prior open-join behavior;
            # newly registered organizations use the model's approval-required default.
            connection.execute(text("ALTER TABLE organization ADD COLUMN membership_approval_required BOOLEAN NOT NULL DEFAULT 0"))
        if "recommendation_required" not in organization_columns:
            connection.execute(text("ALTER TABLE organization ADD COLUMN recommendation_required BOOLEAN NOT NULL DEFAULT 0"))
        if "application_prompt" not in organization_columns:
            connection.execute(text("ALTER TABLE organization ADD COLUMN application_prompt VARCHAR(500) NOT NULL DEFAULT ''"))
        if "onboarding_form" not in organization_columns:
            connection.execute(text("ALTER TABLE organization ADD COLUMN onboarding_form TEXT NOT NULL DEFAULT '[]'"))
        if "logo_filename" not in organization_columns:
            connection.execute(text("ALTER TABLE organization ADD COLUMN logo_filename VARCHAR(80)"))
        if "loyalty_reward_enabled" not in organization_columns:
            connection.execute(text("ALTER TABLE organization ADD COLUMN loyalty_reward_enabled BOOLEAN NOT NULL DEFAULT 0"))
        if "loyalty_reward_after_days" not in organization_columns:
            connection.execute(text("ALTER TABLE organization ADD COLUMN loyalty_reward_after_days INTEGER NOT NULL DEFAULT 30"))
        if "loyalty_reward_days" not in organization_columns:
            connection.execute(text("ALTER TABLE organization ADD COLUMN loyalty_reward_days INTEGER NOT NULL DEFAULT 30"))
    user_columns = {column["name"] for column in inspect(db.engine).get_columns("user")}
    with db.engine.begin() as connection:
        if "full_name" not in user_columns:
            connection.execute(text("ALTER TABLE user ADD COLUMN full_name VARCHAR(120)"))
        if "gender" not in user_columns:
            connection.execute(text("ALTER TABLE user ADD COLUMN gender VARCHAR(40)"))
        if "phone_number" not in user_columns:
            connection.execute(text("ALTER TABLE user ADD COLUMN phone_number VARCHAR(32)"))
        if "profile_image" not in user_columns:
            connection.execute(text("ALTER TABLE user ADD COLUMN profile_image BLOB"))
        if "email_verified" not in user_columns:
            # Existing local demo accounts predate confirmation and remain usable.
            connection.execute(text("ALTER TABLE user ADD COLUMN email_verified BOOLEAN NOT NULL DEFAULT 1"))
        if "admin_permissions" not in user_columns:
            connection.execute(text("ALTER TABLE user ADD COLUMN admin_permissions TEXT"))
        if "admin_title" not in user_columns:
            connection.execute(text("ALTER TABLE user ADD COLUMN admin_title VARCHAR(60)"))
        # Older demo databases allowed only one organization administrator.
        # Scoped staff roles require additional organization-admin accounts.
        connection.execute(text("DROP INDEX IF EXISTS uq_one_organization_admin"))
    user_sql = next((row[0] for row in db.session.execute(text(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='user'")).all()), "") or ""
    if "role != 'platform_admin' and organization_id is not null" in user_sql:
        # Rebuild the user table so existing SQLite demo databases accept
        # member accounts that are not tied to one organization.
        raw = db.engine.raw_connection()
        try:
            cursor = raw.cursor()
            cursor.execute("PRAGMA foreign_keys=OFF")
            cursor.execute("""CREATE TABLE user_new (
                id INTEGER NOT NULL PRIMARY KEY,
                email VARCHAR(254) NOT NULL,
                full_name VARCHAR(120),
                gender VARCHAR(40),
                phone_number VARCHAR(32),
                profile_image BLOB,
                password_hash VARCHAR(255) NOT NULL,
                role VARCHAR(24) NOT NULL,
                organization_id INTEGER REFERENCES organization(id),
                active BOOLEAN NOT NULL,
                email_verified BOOLEAN NOT NULL,
                admin_permissions TEXT,
                admin_title VARCHAR(60),
                session_nonce VARCHAR(64),
                created_at DATETIME NOT NULL,
                CONSTRAINT ck_user_role CHECK (role in ('platform_admin','organization_admin','member')),
                CONSTRAINT ck_user_role_organization CHECK (
                    (role = 'platform_admin' and organization_id is null) or
                    (role = 'organization_admin' and organization_id is not null) or role = 'member')
            )""")
            cursor.execute("""INSERT INTO user_new
                (id,email,full_name,gender,phone_number,profile_image,password_hash,role,organization_id,active,email_verified,
                 admin_permissions,admin_title,session_nonce,created_at)
                SELECT id,email,full_name,gender,phone_number,profile_image,password_hash,role,organization_id,active,email_verified,
                       admin_permissions,admin_title,session_nonce,created_at FROM user""")
            cursor.execute("DROP TABLE user")
            cursor.execute("ALTER TABLE user_new RENAME TO user")
            cursor.execute("CREATE INDEX ix_user_email ON user (email)")
            cursor.execute("CREATE INDEX ix_user_organization_id ON user (organization_id)")
            cursor.execute("CREATE UNIQUE INDEX uq_platform_admin_email ON user (email) WHERE role = 'platform_admin'")
            cursor.execute("CREATE UNIQUE INDEX uq_organization_account_email ON user (organization_id, email) WHERE organization_id IS NOT NULL")
            raw.commit()
            cursor.execute("PRAGMA foreign_keys=ON")
        except Exception:
            raw.rollback()
            raise
        finally:
            raw.close()
    verification_columns = {column["name"] for column in inspect(db.engine).get_columns("email_verification_token")}
    if "sent_at" not in verification_columns:
        with db.engine.begin() as connection:
            connection.execute(text("ALTER TABLE email_verification_token ADD COLUMN sent_at DATETIME"))
    columns = {column["name"] for column in inspect(db.engine).get_columns("membership")}
    with db.engine.begin() as connection:
        if "next_plan_id" not in columns:
            connection.execute(text("ALTER TABLE membership ADD COLUMN next_plan_id INTEGER REFERENCES plan(id)"))
        if "last_renewal_due_day" not in columns:
            connection.execute(text("ALTER TABLE membership ADD COLUMN last_renewal_due_day INTEGER"))
        if "loyalty_rewarded_day" not in columns:
            connection.execute(text("ALTER TABLE membership ADD COLUMN loyalty_rewarded_day INTEGER"))
        if "loyalty_reward_days" not in columns:
            connection.execute(text("ALTER TABLE membership ADD COLUMN loyalty_reward_days INTEGER NOT NULL DEFAULT 0"))
        if "onboarding_responses" not in columns:
            connection.execute(text("ALTER TABLE membership ADD COLUMN onboarding_responses TEXT NOT NULL DEFAULT '{}'"))
        if "consent_accepted_at" not in columns:
            connection.execute(text("ALTER TABLE membership ADD COLUMN consent_accepted_at DATETIME"))
    payment_columns = {column["name"] for column in inspect(db.engine).get_columns("simulated_payment")}
    if "channel" not in payment_columns:
        with db.engine.begin() as connection:
            connection.execute(text("ALTER TABLE simulated_payment ADD COLUMN channel VARCHAR(12) NOT NULL DEFAULT 'simulated'"))
    membership_uniques = {constraint.get("name") for constraint in inspect(db.engine).get_unique_constraints("membership")}
    if "uq_membership_organization_user" not in membership_uniques:
        # Older demos enforced one membership per member account. Rebuild only
        # this table; dependent payment rows keep referencing the same table name.
        raw = db.engine.raw_connection()
        try:
            cursor = raw.cursor()
            cursor.execute("PRAGMA foreign_keys=OFF")
            cursor.execute("""CREATE TABLE membership_new (
                id INTEGER NOT NULL PRIMARY KEY,
                organization_id INTEGER NOT NULL REFERENCES organization(id),
                user_id INTEGER NOT NULL REFERENCES user(id),
                plan_id INTEGER NOT NULL REFERENCES plan(id),
                full_name VARCHAR(120) NOT NULL,
                status VARCHAR(16) NOT NULL DEFAULT 'Pending',
                start_day INTEGER,
                end_day INTEGER,
                cancel_at_period_end BOOLEAN NOT NULL DEFAULT 0,
                next_plan_id INTEGER REFERENCES plan(id),
                last_renewal_due_day INTEGER,
                loyalty_rewarded_day INTEGER,
                loyalty_reward_days INTEGER NOT NULL DEFAULT 0,
                onboarding_responses TEXT NOT NULL DEFAULT '{}',
                consent_accepted_at DATETIME,
                created_at DATETIME NOT NULL,
                CONSTRAINT uq_membership_organization_user UNIQUE (organization_id, user_id),
                CONSTRAINT ck_membership_status CHECK (status in ('Pending','Active')),
                CONSTRAINT ck_membership_start_day CHECK (start_day is null or start_day >= 0),
                CONSTRAINT ck_membership_end_day CHECK (end_day is null or end_day >= 0)
            )""")
            cursor.execute("""INSERT INTO membership_new
                (id, organization_id, user_id, plan_id, full_name, status, start_day, end_day,
                 cancel_at_period_end, next_plan_id, last_renewal_due_day, loyalty_rewarded_day,
                 loyalty_reward_days, onboarding_responses, consent_accepted_at, created_at)
                SELECT id, organization_id, user_id, plan_id, full_name, status, start_day, end_day,
                   cancel_at_period_end, next_plan_id, last_renewal_due_day, loyalty_rewarded_day,
                   loyalty_reward_days, onboarding_responses, consent_accepted_at, created_at FROM membership""")
            cursor.execute("DROP TABLE membership")
            cursor.execute("ALTER TABLE membership_new RENAME TO membership")
            cursor.execute("CREATE INDEX ix_membership_organization_id ON membership (organization_id)")
            cursor.execute("CREATE INDEX ix_membership_user_id ON membership (user_id)")
            raw.commit()
            cursor.execute("PRAGMA foreign_keys=ON")
        except Exception:
            raw.rollback()
            raise
        finally:
            raw.close()


def create_simulated_payment(membership: Membership, plan: Plan, kind: str, amount_minor: int) -> SimulatedPayment:
    pending = SimulatedPayment.query.filter_by(membership_id=membership.id, status="Pending").first()
    if pending:
        return pending
    payment = SimulatedPayment(organization_id=membership.organization_id, membership_id=membership.id,
                               plan_id=plan.id, amount_minor=amount_minor, kind=kind,
                               created_day=simulated_day())
    db.session.add(payment)
    db.session.flush()
    record_activity("simulated_payment_created", "payment", payment.id,
                    f"{kind}; LKR {amount_minor / 100:.2f}; simulated only")
    db.session.commit()
    return payment


def settle_simulated_payment(payment: SimulatedPayment) -> None:
    """Apply a simulated bank confirmation once; repeated confirmations are harmless."""
    if payment.status != "Pending":
        record_activity("simulated_payment_duplicate_ignored", "payment", payment.id,
                        f"Existing status: {payment.status}")
        db.session.commit()
        return
    membership = db.session.get(Membership, payment.membership_id)
    payment.status = "Paid"
    payment.confirmed_day = simulated_day()
    if payment.kind in ("join", "renewal"):
        plan = db.session.get(Plan, payment.plan_id)
        membership.plan_id = plan.id
        membership.status = "Active"
        if payment.kind == "join" or membership.start_day is None:
            membership.start_day = simulated_day()
        membership.end_day = simulated_day() + period_days(plan)
        membership.cancel_at_period_end = False
        if payment.kind == "renewal":
            grant_loyalty_rewards(membership.organization, simulated_day(), simulated_day(),
                                  include_already_qualified=True)
    elif payment.kind == "upgrade":
        membership.plan_id = payment.plan_id
    action = "offline_payment_recorded" if payment.channel == "offline" else "simulated_payment_confirmed"
    details = f"{payment.kind}; offline payment recorded by organization" if payment.channel == "offline" else f"{payment.kind}; simulated confirmation"
    record_activity(action, "payment", payment.id, details)
    db.session.commit()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not getattr(g, "user", None):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


ADMIN_ENDPOINT_PERMISSIONS = {
    "admin_organizations": "dashboard",
    "admin_reconciliation_export": "reports",
    "admin_change_organization": "organizations",
    "admin_confirm_payment": "payments",
    "admin_process_refund": "refunds",
    "admin_advance_simulation": "simulation",
    "admin_reset_simulation": "simulation",
    "platform_team": "team",
    "admin_email_setup": "team",
    "admin_send_test_email": "team",
    "organization_loyalty_settings": "plans",
    "organization_create_event": "events",
    "organization_archive_event": "events",
    "organization_upload_logo": "branding",
    "organization_remove_logo": "branding",
    "organization_application_settings": "applications",
    "organization_review_application": "applications",
    "organization_request_refund": "finance",
    "organization_export_members": "members",
    "organization_member_import_template": "members",
    "organization_import_members": "members",
    "organization_revoke_invitation": "members",
    "organization_resend_member_invitation": "members",
    "organization_forms": "members",
    "organization_send_notification": "members",
    "organization_members": "members",
    "organization_attendance": "members",
    "organization_add_member": "members",
    "organization_review_member_plan_change": "members",
    "organization_review_billing_proof": "members",
    "organization_record_offline_payment": "finance",
    "archive_plan": "plans",
    "organization_create_offer": "offers",
    "organization_archive_offer": "offers",
    "organization_team": "team",
    "member_notifications": "member",
    "member_notification_read": "member",
    "member_request_plan_change": "member",
}


def admin_permission_set(user: User) -> set[str]:
    if not user.admin_permissions:
        return {"*"}  # Existing owner accounts retain their current access.
    try:
        values = json.loads(user.admin_permissions)
        return set(values) if isinstance(values, list) else set()
    except (TypeError, ValueError):
        return set()


def has_admin_permission(user: User, permission: str) -> bool:
    permissions = admin_permission_set(user)
    return "*" in permissions or permission in permissions


def role_required(*roles):
    def decorate(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not getattr(g, "user", None):
                endpoint = "member_sign_in" if roles == ("member",) else "login"
                return redirect(url_for(endpoint, next=request.path))
            if g.user.role not in roles:
                abort(403)
            if g.user.role in ("platform_admin", "organization_admin"):
                permission = ADMIN_ENDPOINT_PERMISSIONS.get(request.endpoint)
                if request.endpoint == "organization_plans":
                    permission = "dashboard" if request.method == "GET" else "plans"
                if permission and not has_admin_permission(g.user, permission):
                    abort(403)
                if (request.endpoint == "organization_plans" and request.method == "GET"
                        and not has_admin_permission(g.user, "dashboard")):
                    abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorate


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY"),
        SQLALCHEMY_DATABASE_URI=os.environ.get("DATABASE_URL", "sqlite:///membership.db"),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "0") == "1",  # Local HTTP only by default.
        PERMANENT_SESSION_LIFETIME=timedelta(days=14),
        RATELIMIT_STORAGE_URI="memory://",
        MAX_CONTENT_LENGTH=6 * 1024 * 1024,
        MAIL_SERVER=os.environ.get("MAIL_SERVER", ""),
        MAIL_PORT=int(os.environ.get("MAIL_PORT", "587")),
        MAIL_USERNAME=os.environ.get("MAIL_USERNAME", ""),
        MAIL_PASSWORD=os.environ.get("MAIL_PASSWORD", ""),
        MAIL_FROM=os.environ.get("MAIL_FROM", ""),
        MAIL_USE_TLS=os.environ.get("MAIL_USE_TLS", "1") == "1",
        MAIL_USE_SSL=os.environ.get("MAIL_USE_SSL", "0") == "1",
        PUBLIC_BASE_URL=os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:5000"),
        DEV_EMAIL_PREVIEW=os.environ.get("DEV_EMAIL_PREVIEW", "0") == "1",
        SHOW_PLATFORM_ADMIN_LINK=os.environ.get("SHOW_PLATFORM_ADMIN_LINK", "0") == "1",
    )
    if test_config:
        app.config.update(test_config)
    app.config.setdefault("ORGANIZATION_LOGO_FOLDER", os.environ.get(
        "ORGANIZATION_LOGO_FOLDER", os.path.join(app.instance_path, "organization-logos")))
    os.makedirs(app.config["ORGANIZATION_LOGO_FOLDER"], exist_ok=True)
    if not app.config.get("SECRET_KEY"):
        raise RuntimeError("Set SECRET_KEY in the environment before starting the app.")
    public_url = urlsplit(app.config.get("PUBLIC_BASE_URL", ""))
    if public_url.scheme not in ("http", "https") or not public_url.hostname or public_url.username or public_url.password:
        raise RuntimeError("PUBLIC_BASE_URL must be an absolute HTTP or HTTPS URL without credentials.")
    if app.config.get("DEV_EMAIL_PREVIEW"):
        if public_url.scheme != "http" or not is_local_development_host(public_url.hostname):
            raise RuntimeError("DEV_EMAIL_PREVIEW is only allowed with localhost or a private-network HTTP PUBLIC_BASE_URL.")

    db.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)
    with app.app_context():
        ensure_local_sqlite_schema()

    def issue_email_verification(user: User) -> bool:
        if user.email_verified:
            return True
        smtp_configured = all(app.config.get(key) for key in ("MAIL_SERVER", "MAIL_FROM"))
        if not smtp_configured and not app.config.get("DEV_EMAIL_PREVIEW"):
            return False
        previous_tokens = EmailVerificationToken.query.filter_by(user_id=user.id).order_by(
            EmailVerificationToken.created_at.desc()).all()
        last_successful = next((token for token in previous_tokens if token.sent_at), None)
        if last_successful:
            last_sent = last_successful.sent_at
            if last_sent.tzinfo is None:
                last_sent = last_sent.replace(tzinfo=timezone.utc)
            if utcnow() - last_sent < timedelta(seconds=30):
                return False
        raw_token = secrets.token_urlsafe(32)
        verification = EmailVerificationToken(
            user_id=user.id, token_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
            expires_at=utcnow() + timedelta(hours=24))
        db.session.add(verification)
        db.session.commit()
        path = url_for("confirm_email", token=raw_token)
        base_url = app.config["PUBLIC_BASE_URL"].rstrip("/")
        confirmation_url = f"{base_url}{path}"
        message = EmailMessage()
        message["Subject"] = "Confirm your Socio email address"
        message["From"] = app.config["MAIL_FROM"]
        message["To"] = user.email
        message["Reply-To"] = app.config["MAIL_FROM"]
        message["X-Entity-Ref-ID"] = secrets.token_hex(12)
        greeting = user.full_name or "there"
        message.set_content(f"Hi {greeting},\n\nConfirm your email address for Socio by opening this link:\n"
                            f"{confirmation_url}\n\nThis link expires in 24 hours. If you did not create this account, you can ignore this message.")
        message.add_alternative(
            f"<html><body style='margin:0;padding:24px;background:#f7f5f2;font-family:Arial,sans-serif;color:#171513'>"
            f"<table role='presentation' style='max-width:560px;margin:auto;background:#fffefa;border:1px solid #e8e2db;border-radius:14px'>"
            f"<tr><td style='padding:30px'><p style='font-size:13px;font-weight:bold;color:#f36a35'>SOCIO</p>"
            f"<h1 style='font-size:23px'>Confirm your email</h1><p>Hi {escape(user.full_name or 'there')},</p>"
            f"<p>Confirm your email address to finish creating your Socio account.</p>"
            f"<p style='margin:26px 0'><a href='{escape(confirmation_url, quote=True)}' style='display:inline-block;padding:13px 20px;border-radius:8px;background:#f36a35;color:#171513;text-decoration:none;font-weight:bold'>Confirm email address</a></p>"
            f"<p style='font-size:13px;color:#6f6963'>This link expires in 24 hours. If you did not create this account, you can ignore this message.</p></td></tr></table></body></html>",
            subtype="html")
        if not smtp_configured:
            app.logger.warning("LOCAL EMAIL PREVIEW for %s — copy this link into your browser:\n%s",
                               user.email, confirmation_url)
            verification.sent_at = utcnow()
            for previous in previous_tokens:
                previous.used_at = utcnow()
            db.session.commit()
            return True
        try:
            server = app.config["MAIL_SERVER"]
            port = int(app.config.get("MAIL_PORT", 587))
            if app.config.get("MAIL_USE_SSL"):
                smtp = smtplib.SMTP_SSL(server, port, timeout=15)
            else:
                smtp = smtplib.SMTP(server, port, timeout=15)
            with smtp:
                if app.config.get("MAIL_USE_TLS") and not app.config.get("MAIL_USE_SSL"):
                    smtp.starttls()
                username = app.config.get("MAIL_USERNAME")
                password = app.config.get("MAIL_PASSWORD")
                if username:
                    smtp.login(username, password or "")
                smtp.send_message(message)
            verification.sent_at = utcnow()
            for previous in previous_tokens:
                previous.used_at = utcnow()
            db.session.commit()
            return True
        except (OSError, smtplib.SMTPException, ValueError):
            app.logger.exception("SMTP delivery of a Socio confirmation failed for %s", user.email)
            verification.used_at = utcnow()
            db.session.commit()
            return False

    def verification_expired(verification: EmailVerificationToken) -> bool:
        expires_at = verification.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return expires_at <= utcnow()

    def email_preview_mode() -> bool:
        return bool(app.config.get("DEV_EMAIL_PREVIEW") and not all(
            app.config.get(key) for key in ("MAIL_SERVER", "MAIL_FROM")))

    def deliver_member_update_email(user: User, organization: Organization,
                                    title: str, body: str) -> bool:
        if not all(app.config.get(key) for key in ("MAIL_SERVER", "MAIL_FROM")):
            return False
        message = EmailMessage()
        message["Subject"] = f"{organization.name}: {title}"
        message["From"] = app.config["MAIL_FROM"]
        message["To"] = user.email
        message.set_content(f"{title}\n\n{body}\n\nOpen Socio to view your organization updates.")
        message.add_alternative(
            "<html><body style='font-family:Arial,sans-serif;color:#171513'>"
            f"<p><strong>{escape(organization.name)}</strong></p>"
            f"<h2>{escape(title)}</h2><p>{escape(body).replace(chr(10), '<br>')}</p>"
            "<p>Sign in to Socio to view your organization updates.</p></body></html>",
            subtype="html")
        try:
            server_name = app.config["MAIL_SERVER"]
            port = int(app.config.get("MAIL_PORT", 587))
            smtp = (smtplib.SMTP_SSL(server_name, port, timeout=15) if app.config.get("MAIL_USE_SSL")
                    else smtplib.SMTP(server_name, port, timeout=15))
            with smtp:
                if app.config.get("MAIL_USE_TLS") and not app.config.get("MAIL_USE_SSL"):
                    smtp.starttls()
                if app.config.get("MAIL_USERNAME"):
                    smtp.login(app.config["MAIL_USERNAME"], app.config.get("MAIL_PASSWORD") or "")
                smtp.send_message(message)
            return True
        except (OSError, smtplib.SMTPException, ValueError):
            app.logger.exception("Socio organization update email failed for %s", user.email)
            return False

    def deliver_member_invitation(invitation: MemberInvitation, raw_token: str) -> bool:
        smtp_configured = all(app.config.get(key) for key in ("MAIL_SERVER", "MAIL_FROM"))
        if not smtp_configured and not app.config.get("DEV_EMAIL_PREVIEW"):
            return False
        invite_url = (f"{app.config['PUBLIC_BASE_URL'].rstrip('/')}/invite/"
                      f"{raw_token}")
        organization_name = invitation.organization.name
        subject = f"You’ve been added to {organization_name} on Socio"
        message = EmailMessage()
        message["Subject"] = subject
        if smtp_configured:
            message["From"] = app.config["MAIL_FROM"]
        message["To"] = invitation.email
        message.set_content(
            f"Hi {invitation.full_name},\n\n{organization_name} has added you as a member. "
            f"Open this secure link to complete your Socio profile and onboarding form:\n{invite_url}\n\n"
            "Your organization has already recorded your membership. This link expires in 7 days. "
            "If you were not expecting this invitation, contact the organization."
        )
        message.add_alternative(
            "<html><body style='font-family:Arial,sans-serif;color:#171513'>"
            f"<p><strong>{escape(organization_name)}</strong> has added you as a member.</p>"
            "<p>Complete your Socio profile and the organization’s onboarding form to see your club updates.</p>"
            f"<p><a href='{escape(invite_url, quote=True)}'>Open your secure Socio invitation</a></p>"
            "<p>This link expires in 7 days. If you were not expecting this invitation, contact the organization.</p>"
            "</body></html>", subtype="html")
        if not smtp_configured:
            app.logger.warning("LOCAL MEMBER INVITATION for %s — copy this link into your browser:\n%s",
                               invitation.email, invite_url)
            return True
        try:
            server_name = app.config["MAIL_SERVER"]
            port = int(app.config.get("MAIL_PORT", 587))
            smtp = (smtplib.SMTP_SSL(server_name, port, timeout=15) if app.config.get("MAIL_USE_SSL")
                    else smtplib.SMTP(server_name, port, timeout=15))
            with smtp:
                if app.config.get("MAIL_USE_TLS") and not app.config.get("MAIL_USE_SSL"):
                    smtp.starttls()
                if app.config.get("MAIL_USERNAME"):
                    smtp.login(app.config["MAIL_USERNAME"], app.config.get("MAIL_PASSWORD") or "")
                smtp.send_message(message)
            return True
        except (OSError, smtplib.SMTPException, ValueError):
            app.logger.exception("Socio membership invitation email failed for %s", invitation.email)
            return False

    def queue_organization_update(organization: Organization, title: str, body: str,
                                  plan_id: int | None = None) -> list[User]:
        query = Membership.query.filter_by(organization_id=organization.id)
        if plan_id is not None:
            query = query.filter_by(plan_id=plan_id)
        recipients = []
        for membership in query.all():
            if not membership_access_active(membership):
                continue
            recipients.append(membership.user)
            db.session.add(MemberNotification(organization_id=organization.id,
                user_id=membership.user_id, created_by_user_id=g.user.id,
                title=title, body=body))
        return recipients

    def email_organization_update(organization: Organization, title: str,
                                 body: str, recipients: list[User]) -> int:
        return sum(1 for user in recipients if user.email_verified and
                   deliver_member_update_email(user, organization, title, body))

    def deliver_staff_invitation(invitation: StaffInvitation) -> bool:
        smtp_configured = all(app.config.get(key) for key in ("MAIL_SERVER", "MAIL_FROM"))
        if not smtp_configured and not app.config.get("DEV_EMAIL_PREVIEW"):
            return False
        raw_token = secrets.token_urlsafe(32)
        invitation.token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        db.session.add(invitation)
        db.session.flush()
        invite_url = f"{app.config['PUBLIC_BASE_URL'].rstrip('/')}{url_for('claim_staff_invitation', token=raw_token)}"
        message = EmailMessage()
        message["Subject"] = "You’re invited to manage Socio"
        message["From"] = app.config["MAIL_FROM"]
        message["To"] = invitation.email
        message.set_content(
            f"Hi {invitation.full_name},\n\nYou have been invited to Socio as {invitation.admin_title}. "
            f"Set your password and confirm access here: {invite_url}\n\n"
            "This one-time link expires in 24 hours. If you were not expecting it, ignore this email."
        )
        if not smtp_configured:
            app.logger.warning("LOCAL STAFF INVITATION for %s — copy this link into your browser:\n%s",
                               invitation.email, invite_url)
            return True
        try:
            server_name = app.config["MAIL_SERVER"]
            port = int(app.config.get("MAIL_PORT", 587))
            smtp = (smtplib.SMTP_SSL(server_name, port, timeout=15) if app.config.get("MAIL_USE_SSL")
                    else smtplib.SMTP(server_name, port, timeout=15))
            with smtp:
                if app.config.get("MAIL_USE_TLS") and not app.config.get("MAIL_USE_SSL"):
                    smtp.starttls()
                username = app.config.get("MAIL_USERNAME")
                if username:
                    smtp.login(username, app.config.get("MAIL_PASSWORD", ""))
                smtp.send_message(message)
            return True
        except (OSError, smtplib.SMTPException, ValueError):
            db.session.rollback()
            return False

    @app.before_request
    def load_user():
        g.user = db.session.get(User, session.get("user_id")) if session.get("user_id") else None
        if g.user and (not g.user.active or not g.user.session_nonce or not hmac.compare_digest(
                g.user.session_nonce, session.get("session_nonce", ""))):
            session.clear()
            g.user = None
        g.simulated_day = simulated_day()

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'self'")
        if getattr(g, "user", None):
            response.headers.setdefault("Cache-Control", "no-store")
        if app.config.get("SESSION_COOKIE_SECURE"):
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response

    @app.context_processor
    def template_helpers():
        return {"membership_status": membership_status,
                "membership_access_active": membership_access_active,
                "display_name": user_display_name,
                "admin_permission_set": admin_permission_set,
                "unread_notification_count": (MemberNotification.query.filter_by(
                    user_id=g.user.id, read_at=None).count()
                    if getattr(g, "user", None) and g.user.role == "member" else 0),
                "has_admin_permission": lambda target_or_permission, permission=None: bool(
                    (target_or_permission if permission is not None else getattr(g, "user", None))
                    and has_admin_permission(
                        target_or_permission if permission is not None else g.user,
                        permission if permission is not None else target_or_permission))}

    @app.get("/")
    def index():
        if g.user:
            return redirect(url_for("welcome"))
        return redirect(url_for("sign_in"))

    @app.get("/sign-in")
    def sign_in():
        if g.user:
            return redirect(url_for("welcome"))
        return render_template("sign_in.html")

    @app.get("/mobile-preview")
    def mobile_app_preview():
        platform = request.args.get("platform", "android").lower()
        if platform not in ("android", "ios"):
            platform = "android"
        tab = request.args.get("tab", "clubs").lower()
        if tab not in ("clubs", "updates", "passes", "profile"):
            tab = "clubs"
        member = g.user if getattr(g, "user", None) and g.user.role == "member" else None
        memberships = (Membership.query.filter_by(user_id=member.id).order_by(Membership.id.desc()).all()
                       if member else [])
        updates = (MemberNotification.query.filter_by(user_id=member.id).order_by(
            MemberNotification.created_at.desc()).limit(8).all() if member else [])
        sample_clubs = [
            {"name": "Colombo Chess Club", "tier": "Annual Member", "status": "Active",
             "detail": "Club night · Friday at 6:30 PM", "offer": "Member café offer"},
            {"name": "Lakeview Golf Club", "tier": "Weekend Member", "status": "Active",
             "detail": "Monthly medal · Sunday at 7:00 AM", "offer": "Practice range guest pass"},
        ]
        sample_updates = [
            {"organization": "Colombo Chess Club", "title": "Friday club night", "body": "Doors open at 6:00 PM. Bring a guest and join the rapid games.", "time": "Today · 9:15 AM"},
            {"organization": "Lakeview Golf Club", "title": "Monthly medal tee sheet", "body": "The Sunday tee sheet is ready. Contact the club office to confirm your time.", "time": "Yesterday"},
            {"organization": "Colombo Chess Club", "title": "Member café offer", "body": "Show your member pass at the café for this week’s club special.", "time": "2 days ago"},
        ]
        return render_template("mobile_preview.html", platform=platform, tab=tab,
            member=member, memberships=memberships, updates=updates,
            sample_clubs=sample_clubs, sample_updates=sample_updates)

    @app.get("/about")
    def about():
        return render_template("about.html")

    @app.route("/member/profile", methods=["GET", "POST"])
    @role_required("member")
    @limiter.limit("10 per hour", methods=["POST"])
    def member_profile():
        user = g.user
        if request.method == "POST":
            full_name = request.form.get("full_name", "").strip()
            gender = request.form.get("gender", "").strip()
            phone_number = request.form.get("phone_number", "").strip()
            allowed_genders = {"", "Woman", "Man", "Non-binary", "Prefer not to say", "Other"}
            photo = request.files.get("profile_image")
            remove_photo = request.form.get("remove_profile_image") == "yes"
            normalized_photo = None
            errors = []
            if not full_name or len(full_name) > 120:
                errors.append("Enter a name up to 120 characters.")
            if gender not in allowed_genders:
                errors.append("Choose one of the listed profile options.")
            if len(phone_number) > 32 or (phone_number and not re.fullmatch(r"[+0-9() .-]{7,32}", phone_number)):
                errors.append("Enter a valid phone number, or leave it blank.")
            if photo and photo.filename:
                raw_photo = photo.stream.read(3 * 1024 * 1024 + 1)
                if len(raw_photo) > 3 * 1024 * 1024:
                    errors.append("The profile picture must be 3 MB or smaller.")
                else:
                    try:
                        normalized_photo = normalize_uploaded_image(raw_photo, max_side=512,
                                                                    max_output_bytes=700 * 1024)
                    except (UnidentifiedImageError, OSError, ValueError):
                        errors.append("Choose a valid PNG, JPEG, or WebP picture under 20 megapixels.")
            if errors:
                for message in errors:
                    flash(message, "error")
            else:
                user.full_name = full_name
                user.gender = gender or None
                user.phone_number = phone_number or None
                if normalized_photo:
                    user.profile_image = normalized_photo
                elif remove_photo:
                    user.profile_image = None
                record_activity("member_profile_updated", "user", user.id,
                                "Profile details or picture updated by the member")
                db.session.commit()
                flash("Your profile was updated.", "success")
                return redirect(url_for("member_profile"))
        return render_template("member_profile.html", user=user)

    @app.get("/member/profile/picture")
    @role_required("member")
    def member_profile_picture():
        if not g.user.profile_image:
            abort(404)
        response = Response(g.user.profile_image, mimetype="image/png")
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Disposition"] = "inline; filename=socio-profile.png"
        return response

    @app.get("/healthz")
    def health_check():
        try:
            db.session.execute(text("SELECT 1"))
        except SQLAlchemyError:
            db.session.rollback()
            return Response("unhealthy\n", status=503, mimetype="text/plain")
        return Response("ok\n", mimetype="text/plain")

    @app.get("/service-worker.js")
    def service_worker():
        response = send_from_directory(app.static_folder, "service-worker.js",
                                       mimetype="application/javascript")
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Service-Worker-Allowed"] = "/"
        return response

    @app.get("/welcome")
    @login_required
    def welcome():
        memberships = Membership.query.filter_by(user_id=g.user.id).order_by(Membership.id.desc()).all() if g.user.role == "member" else []
        return render_template("welcome.html", membership=memberships[0] if memberships else None,
                               memberships=memberships)

    @app.route("/email/confirm/<token>", methods=["GET", "POST"])
    @limiter.limit("20 per hour")
    def confirm_email(token: str):
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        verification = EmailVerificationToken.query.filter_by(token_hash=token_hash).first()
        if request.method == "GET":
            valid = bool(verification and not verification.used_at and not verification_expired(verification))
            return render_template("confirm_email.html", valid=valid)
        if not verification or verification.used_at or verification_expired(verification):
            flash("That confirmation link is invalid or expired. Request a new one to continue.", "error")
            return redirect(url_for("resend_email_confirmation"))
        verification.used_at = utcnow()
        verification.user.email_verified = True
        db.session.commit()
        establish_session(verification.user)
        flash("Your email is confirmed. Welcome to Socio.", "success")
        return redirect(url_for("welcome"))

    @app.route("/email/resend", methods=["GET", "POST"])
    @limiter.limit("5 per hour")
    def resend_email_confirmation():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            users = User.query.filter_by(email=email, active=True, email_verified=False).all() if valid_email(email) else []
            for user in users:
                issue_email_verification(user)
            # Keep the resend response identical whether or not an account exists.
            if email_preview_mode():
                flash("If an unconfirmed Socio account uses that email, its local confirmation link is printed in the Flask server console.", "success")
            else:
                flash("If that email belongs to an unconfirmed Socio account, a fresh link was requested. Check your spam folder; if nothing arrives, ask the site administrator to check SMTP settings and the server log.", "success")
            return redirect(url_for("resend_email_confirmation"))
        return render_template("resend_email.html", preview=email_preview_mode())

    @app.get("/organizations/<int:organization_id>")
    def public_organization(organization_id: int):
        organization = db.session.get(Organization, organization_id)
        if not organization or organization.status != "Approved":
            abort(404)
        member_access = None
        if g.user and g.user.role == "member":
            member_access = Membership.query.filter_by(user_id=g.user.id,
                                                        organization_id=organization.id).first()
            if not member_access:
                abort(404)
        elif not (g.user and (g.user.role == "platform_admin" or
                              (g.user.role == "organization_admin" and
                               g.user.organization_id == organization.id))):
            abort(404)
        plans = Plan.query.filter_by(organization_id=organization.id, archived=False).order_by(Plan.price_minor).all()
        offers = [offer for offer in current_organization_offers(organization.id)
                  if not offer.plan_id or any(plan.id == offer.plan_id for plan in plans)]
        events = OrganizationEvent.query.filter_by(organization_id=organization.id, archived=False).filter(
            OrganizationEvent.event_date >= utcnow().date()).order_by(OrganizationEvent.event_date).all()
        return render_template("public_organization.html", organization=organization, plans=plans,
                               offers=offers, events=events, simulated_day=simulated_day(),
                               member_access=member_access)

    @app.get("/organizations/<int:organization_id>/offers/<int:offer_id>")
    def public_organization_offer(organization_id: int, offer_id: int):
        organization = db.session.get(Organization, organization_id)
        if not organization or organization.status != "Approved":
            abort(404)
        if not can_view_organization_updates(organization.id):
            abort(404)
        offer = OrganizationOffer.query.filter_by(
            id=offer_id, organization_id=organization.id, archived=False
        ).first_or_404()
        if g.user.role == "member":
            member_access = Membership.query.filter_by(user_id=g.user.id,
                organization_id=organization.id).first_or_404()
            if offer.plan_id and member_access.plan_id != offer.plan_id:
                abort(404)
        current_day = simulated_day()
        if (offer.start_day > current_day
                or (offer.end_day is not None and offer.end_day < current_day)
                or (offer.plan_id and (not offer.plan or offer.plan.archived))):
            abort(404)
        return render_template("offer_detail.html", organization=organization,
                               offer=offer, simulated_day=current_day)

    @app.get("/organizations/<int:organization_id>/events/<int:event_id>")
    def public_organization_event(organization_id: int, event_id: int):
        organization = db.session.get(Organization, organization_id)
        if not organization or organization.status != "Approved":
            abort(404)
        if not can_view_organization_updates(organization.id):
            abort(404)
        event = OrganizationEvent.query.filter_by(
            id=event_id, organization_id=organization.id, archived=False
        ).first_or_404()
        if event.event_date < utcnow().date():
            abort(404)
        plans = Plan.query.filter_by(organization_id=organization.id, archived=False).order_by(
            Plan.price_minor).all()
        return render_template("event_detail.html", organization=organization,
                               event=event, plans=plans)

    @app.get("/organizations/<int:organization_id>/logo")
    def organization_logo(organization_id: int):
        organization = db.session.get(Organization, organization_id)
        if not organization or not organization.logo_filename:
            abort(404)
        if (organization.status != "Approved" and not
                (g.user and g.user.role == "organization_admin"
                 and g.user.organization_id == organization.id)):
            abort(404)
        return send_from_directory(app.config["ORGANIZATION_LOGO_FOLDER"], organization.logo_filename,
                                   mimetype="image/png", max_age=3600, conditional=True)

    @app.get("/events/<int:event_id>/poster")
    def organization_event_poster(event_id: int):
        event = db.session.get(OrganizationEvent, event_id)
        if not event or not event.poster_filename:
            abort(404)
        organization = event.organization
        if not can_view_organization_updates(organization.id):
            abort(404)
        if (organization.status != "Approved" and not
                (g.user and g.user.role == "organization_admin"
                 and g.user.organization_id == organization.id)):
            abort(404)
        return send_from_directory(app.config["ORGANIZATION_LOGO_FOLDER"], event.poster_filename,
                                   mimetype="image/png", max_age=3600, conditional=True)

    @app.get("/members")
    def member_directory():
        if not g.user or g.user.role != "member":
            return redirect(url_for("member_sign_in"))
        search = request.args.get("q", "").strip()[:100]
        memberships = Membership.query.filter_by(user_id=g.user.id).order_by(Membership.id.desc()).all()
        organization_ids = [membership.organization_id for membership in memberships]
        organizations = Organization.query.filter(Organization.id.in_(organization_ids)).order_by(
            Organization.name).all() if organization_ids else []
        if search:
            needle = f"%{search}%"
            organizations = [org for org in organizations if needle.strip("%") in org.name.casefold()
                             or any(needle.strip("%") in membership.plan.name.casefold()
                                    for membership in memberships if membership.organization_id == org.id)]
        membership_by_org = {membership.organization_id: membership for membership in memberships}
        active_offers = OrganizationOffer.query.filter(OrganizationOffer.organization_id.in_(
            [org.id for org in organizations])).filter_by(archived=False).filter(
                OrganizationOffer.start_day <= simulated_day(),
                or_(OrganizationOffer.end_day.is_(None), OrganizationOffer.end_day >= simulated_day())
            ).order_by(OrganizationOffer.id.desc()).all() if organizations else []
        offers_by_org: dict[int, list[OrganizationOffer]] = {}
        for offer in active_offers:
            if offer.plan_id and offer.plan.archived:
                continue
            if offer.plan_id and membership_by_org.get(offer.organization_id, None) and (
                    membership_by_org[offer.organization_id].plan_id != offer.plan_id):
                continue
            offers_by_org.setdefault(offer.organization_id, []).append(offer)
        events_by_org: dict[int, list[OrganizationEvent]] = {}
        if organizations:
            upcoming_events = OrganizationEvent.query.filter(
                OrganizationEvent.organization_id.in_([org.id for org in organizations]),
                OrganizationEvent.archived.is_(False),
                OrganizationEvent.event_date >= utcnow().date(),
            ).order_by(OrganizationEvent.event_date).all()
            for event in upcoming_events:
                events_by_org.setdefault(event.organization_id, []).append(event)
        plans_by_org = {}
        for organization_id in organization_ids:
            plans_by_org[organization_id] = Plan.query.filter_by(
                organization_id=organization_id, archived=False).order_by(Plan.name).all()
        highest_plan_id_by_org = {
            organization_id: max(plans, key=lambda plan: plan.price_minor).id
            for organization_id, plans in plans_by_org.items() if plans
        }
        pending_upgrade_by_membership = {
            payment.membership_id: payment
            for payment in SimulatedPayment.query.join(
                Membership, SimulatedPayment.membership_id == Membership.id
            ).filter(Membership.user_id == g.user.id, SimulatedPayment.status == "Pending",
                     SimulatedPayment.kind == "upgrade").order_by(SimulatedPayment.id.desc()).all()
        }
        latest_plan_change_by_membership = {}
        for change in MemberPlanChange.query.filter_by(user_id=g.user.id).order_by(
                MemberPlanChange.requested_at.desc()).all():
            latest_plan_change_by_membership.setdefault(change.membership_id, change)
        return render_template("member_directory.html", organizations=organizations,
                               membership_by_org=membership_by_org, offers_by_org=offers_by_org,
                               events_by_org=events_by_org, plans_by_org=plans_by_org,
                               plan_change_by_membership=latest_plan_change_by_membership,
                               highest_plan_id_by_org=highest_plan_id_by_org,
                               pending_upgrade_by_membership=pending_upgrade_by_membership,
                               search=search)

    @app.route("/member/memberships/<int:membership_id>/billing", methods=["GET", "POST"])
    @role_required("member")
    @limiter.limit("12 per hour", methods=["POST"])
    def member_billing(membership_id: int):
        membership = Membership.query.filter_by(id=membership_id, user_id=g.user.id).first_or_404()
        proofs = MemberBillingProof.query.filter_by(membership_id=membership.id).order_by(
            MemberBillingProof.submitted_at.desc()).all()
        if request.method == "POST":
            record_type = request.form.get("record_type", "")
            description = request.form.get("description", "").strip()
            expense_date_raw = request.form.get("expense_date", "")
            member_note = request.form.get("member_note", "").strip()
            upload = request.files.get("evidence")
            try:
                amount = Decimal(request.form.get("amount", "").strip())
                if not amount.is_finite() or amount <= 0 or amount > Decimal("999999999.99"):
                    raise InvalidOperation
                amount_minor = int(amount.quantize(Decimal("0.01")) * 100)
            except (InvalidOperation, ValueError, OverflowError):
                amount_minor = 0
            try:
                expense_date = date.fromisoformat(expense_date_raw)
            except (ValueError, TypeError):
                expense_date = None
            errors = []
            if record_type not in ("Membership fee", "Club expense", "Other"):
                errors.append("Choose a valid record type.")
            if not description or len(description) > 160:
                errors.append("Add a short description up to 160 characters.")
            if amount_minor <= 0 or amount_minor > 99_999_999_999:
                errors.append("Enter a valid LKR amount greater than zero.")
            if not expense_date or expense_date > utcnow().date():
                errors.append("Choose a valid date that is not in the future.")
            if len(member_note) > 500:
                errors.append("Your note can be up to 500 characters.")
            evidence_png = None
            if not upload or not upload.filename:
                errors.append("Add a photo of the bill or receipt.")
            else:
                raw_evidence = upload.stream.read(3 * 1024 * 1024 + 1)
                if len(raw_evidence) > 3 * 1024 * 1024:
                    errors.append("The receipt photo must be 3 MB or smaller.")
                else:
                    try:
                        evidence_png = normalize_uploaded_image(raw_evidence, max_side=2200,
                                                                max_output_bytes=1200 * 1024)
                    except (UnidentifiedImageError, OSError, ValueError):
                        errors.append("Use a valid PNG, JPEG, or WebP receipt photo under 20 megapixels.")
            if errors:
                for message in errors:
                    flash(message, "error")
            else:
                proof = MemberBillingProof(organization_id=membership.organization_id,
                    membership_id=membership.id, user_id=g.user.id, record_type=record_type,
                    description=description, amount_minor=amount_minor, expense_date=expense_date,
                    member_note=member_note, evidence_png=evidence_png)
                db.session.add(proof)
                db.session.flush()
                record_activity("member_billing_proof_submitted", "billing_proof", proof.id,
                    f"{record_type}; LKR {amount_minor / 100:.2f}; {membership.organization.name}")
                db.session.commit()
                flash("Your bill or expense proof was submitted to the organization for review. Socio does not process or reimburse this payment.", "success")
                return redirect(url_for("member_billing", membership_id=membership.id))
        payments = SimulatedPayment.query.filter_by(membership_id=membership.id).order_by(
            SimulatedPayment.created_at.desc()).limit(100).all()
        return render_template("member_billing.html", membership=membership, proofs=proofs,
                               payments=payments,
                               today=utcnow().date().isoformat())

    @app.get("/billing-proof/<int:proof_id>/evidence")
    @login_required
    def member_billing_evidence(proof_id: int):
        proof = db.session.get(MemberBillingProof, proof_id)
        if not proof:
            abort(404)
        member_can_view = g.user.role == "member" and proof.user_id == g.user.id
        organization_can_view = (g.user.role == "organization_admin"
            and g.user.organization_id == proof.organization_id
            and has_admin_permission(g.user, "members"))
        if not (member_can_view or organization_can_view):
            abort(404)
        response = Response(proof.evidence_png, mimetype="image/png")
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Disposition"] = "inline; filename=socio-bill-proof.png"
        return response

    @app.get("/member/memberships/<int:membership_id>/card")
    @role_required("member")
    @limiter.limit("12 per hour")
    def member_qr_card(membership_id: int):
        membership = Membership.query.filter_by(id=membership_id, user_id=g.user.id).first_or_404()
        if not membership_access_active(membership):
            abort(403)
        now = utcnow()
        MemberQrPass.query.filter_by(membership_id=membership.id, used_at=None,
                                     revoked_at=None).update({"revoked_at": now},
                                                            synchronize_session=False)
        raw_token = secrets.token_urlsafe(32)
        qr_pass = MemberQrPass(token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
                               membership_id=membership.id,
                               organization_id=membership.organization_id,
                               expires_at=now + timedelta(minutes=3))
        db.session.add(qr_pass)
        db.session.commit()
        pass_path = url_for("organization_checkin_member", token=raw_token)
        qr_url = app.config["PUBLIC_BASE_URL"].rstrip("/") + pass_path
        return render_template("member_qr_card.html", membership=membership,
                               qr_image=qr_png_data_uri(qr_url), expires_at=qr_pass.expires_at)

    @app.route("/check-in/member/<token>", methods=["GET", "POST"])
    @limiter.limit("30 per hour")
    def organization_checkin_member(token: str):
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        qr_pass = MemberQrPass.query.filter_by(token_hash=token_hash).first()
        now = utcnow()
        expires_at = qr_pass.expires_at if qr_pass else now
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if not qr_pass or qr_pass.revoked_at or qr_pass.used_at or expires_at <= now:
            return render_template("checkin_expired.html"), 410
        if not g.user:
            return redirect(url_for("login", role="organization_admin", next=request.full_path))
        if g.user.role == "organization_admin":
            if g.user.organization_id != qr_pass.organization_id:
                abort(403)
            if not has_admin_permission(g.user, "members"):
                abort(403)
        elif g.user.role != "platform_admin":
            abort(403)
        membership = qr_pass.membership
        if not membership_access_active(membership):
            return render_template("checkin_inactive.html", membership=membership), 409
        if request.method == "POST":
            attendance = MembershipAttendance(organization_id=membership.organization_id,
                membership_id=membership.id, member_pass_id=qr_pass.id,
                checked_in_by_user_id=g.user.id, method="staff_scan")
            qr_pass.used_at = now
            db.session.add(attendance)
            record_activity("member_qr_checkin", "membership", membership.id,
                            f"Checked in by staff account {g.user.id}")
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash("This member pass has already been used.", "info")
                if g.user.role == "organization_admin":
                    return redirect(url_for("organization_attendance"))
                return redirect(url_for("admin_organizations"))
            flash(f"{membership.full_name} was checked in successfully.", "success")
            if g.user.role == "organization_admin":
                return redirect(url_for("organization_attendance"))
            return redirect(url_for("admin_organizations"))
        return render_template("checkin_member_confirm.html", membership=membership,
                               qr_pass=qr_pass)

    @app.route("/check-in/club/<token>", methods=["GET", "POST"])
    @limiter.limit("20 per hour")
    def member_self_checkin(token: str):
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        checkin_code = OrganizationQrCheckin.query.filter_by(token_hash=token_hash).first()
        now = utcnow()
        expires_at = checkin_code.expires_at if checkin_code else now
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if not checkin_code or checkin_code.revoked_at or expires_at <= now:
            return render_template("checkin_expired.html"), 410
        if not g.user:
            return redirect(url_for("member_sign_in", next=request.full_path))
        if g.user.role != "member":
            abort(403)
        membership = Membership.query.filter_by(user_id=g.user.id,
            organization_id=checkin_code.organization_id).first()
        if not membership or not membership_access_active(membership):
            return render_template("checkin_no_membership.html",
                                   organization=checkin_code.organization), 403
        existing = MembershipAttendance.query.filter_by(membership_id=membership.id,
            organization_code_id=checkin_code.id).first()
        if request.method == "POST":
            if existing:
                flash("You are already checked in for this club check-in code.", "info")
            else:
                attendance = MembershipAttendance(organization_id=membership.organization_id,
                    membership_id=membership.id, organization_code_id=checkin_code.id,
                    method="member_scan")
                db.session.add(attendance)
                record_activity("member_self_checkin", "membership", membership.id,
                                f"Organization {membership.organization_id}; QR code {checkin_code.id}")
                try:
                    db.session.commit()
                    flash(f"You’re checked in at {checkin_code.organization.name}.", "success")
                except IntegrityError:
                    db.session.rollback()
                    flash("You’re already checked in for this code.", "info")
            return redirect(url_for("member_directory"))
        return render_template("checkin_member_self.html", membership=membership,
                               checkin_code=checkin_code, existing=existing)

    @app.post("/member/memberships/<int:membership_id>/tier-change")
    @role_required("member")
    @limiter.limit("10 per hour")
    def member_request_plan_change(membership_id: int):
        membership = Membership.query.filter_by(id=membership_id, user_id=g.user.id).first_or_404()
        if membership_status(membership) != "Active":
            abort(403)
        try:
            target_plan_id = int(request.form.get("plan_id", "0"))
        except ValueError:
            target_plan_id = 0
        target_plan = Plan.query.filter_by(id=target_plan_id,
            organization_id=membership.organization_id, archived=False).first()
        if target_plan and target_plan.billing_period != membership.plan.billing_period:
            flash("Choose a tier with the same billing period. Contact your club if you want to change billing frequency.", "error")
            return redirect(url_for("member_directory"))
        pending = MemberPlanChange.query.filter_by(membership_id=membership.id,
            status="Requested").first()
        if not target_plan or target_plan.id == membership.plan_id:
            flash("Choose a different available membership tier.", "error")
        elif pending:
            flash("You already have a tier-change request awaiting the organization’s review.", "error")
        elif membership.next_plan_id:
            flash("Undo your scheduled tier change before requesting another one.", "error")
        else:
            note = request.form.get("member_note", "").strip()[:500]
            change = MemberPlanChange(organization_id=membership.organization_id,
                membership_id=membership.id, user_id=g.user.id,
                from_plan_id=membership.plan_id, to_plan_id=target_plan.id,
                member_note=note)
            db.session.add(change)
            record_activity("member_tier_change_requested", "membership", membership.id,
                            f"{membership.plan.name} to {target_plan.name}")
            db.session.commit()
            flash("Your request was sent to the organization. If an upgrade is approved, you’ll review a simulated prorated payment in Socio.", "success")
        return redirect(url_for("member_directory"))

    @app.route("/organizations/register", methods=["GET", "POST"])
    @limiter.limit("5 per hour")
    def register_organization():
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            org_email = request.form.get("organization_email", "").strip().lower()
            admin_email = request.form.get("email", "").strip().lower()
            admin_name = request.form.get("admin_name", "").strip()
            gender = request.form.get("gender", "").strip()
            password = request.form.get("password", "")
            password_confirmation = request.form.get("password_confirmation", "")
            if (not name or len(name) > 120 or not admin_name or len(admin_name) > 120
                    or gender not in ("", "Woman", "Man", "Non-binary", "Prefer not to say", "Other")
                    or not valid_email(org_email) or not valid_email(admin_email)):
                flash("Enter a valid organization name, administrator name, and email addresses.", "error")
            elif len(password) < 12 or len(password) > 128 or password != password_confirmation:
                flash("Use a matching password between 12 and 128 characters.", "error")
            elif Organization.query.filter_by(email=org_email).first():
                flash("That organization contact email is already registered.", "error")
            else:
                organization = Organization(name=name, email=org_email)
                db.session.add(organization)
                db.session.flush()
                user = User(email=admin_email, full_name=admin_name, gender=gender or None,
                            password_hash=password_hasher.hash(password),
                            role="organization_admin", organization_id=organization.id,
                            email_verified=False)
                db.session.add(user)
                db.session.flush()
                record_activity("organization_registered", "organization", organization.id)
                db.session.commit()
                sent = issue_email_verification(user)
                return render_template("email_pending.html", sent=sent, preview=email_preview_mode(), email=user.email)
        return render_template("register.html")

    @app.route("/login", methods=["GET", "POST"])
    @limiter.limit("5 per minute")
    def login():
        login_role = request.args.get("role", "")
        if login_role not in ("platform_admin", "organization_admin", "member"):
            login_role = ""
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            organization_email = request.form.get("organization_email", "").strip().lower()
            candidates = User.query.filter_by(email=email).all()
            if login_role:
                role_candidates = [u for u in candidates if u.role == login_role]
                if organization_email:
                    organization = Organization.query.filter_by(email=organization_email).first()
                    user = next((u for u in role_candidates if organization and
                                 (u.organization_id == organization.id or
                                  (u.role == "member" and Membership.query.filter_by(
                                      user_id=u.id, organization_id=organization.id).first()))), None)
                else:
                    user = role_candidates[0] if len(role_candidates) == 1 else None
            elif organization_email:
                organization = Organization.query.filter_by(email=organization_email).first()
                user = next((u for u in candidates if organization and u.organization_id == organization.id), None)
            else:
                user = next((u for u in candidates if u.role == "platform_admin"), None)
                tenant_candidates = [u for u in candidates if u.organization_id is not None]
                if user is None and len(tenant_candidates) == 1:
                    user = tenant_candidates[0]
            password = request.form.get("password", "")
            valid = False
            if user and user.active:
                try:
                    valid = password_hasher.verify(user.password_hash, password)
                    if password_hasher.check_needs_rehash(user.password_hash):
                        user.password_hash = password_hasher.hash(password)
                        db.session.commit()
                except VerifyMismatchError:
                    pass
            if not valid:
                flash("Email or password is incorrect.", "error")
            elif not user.email_verified:
                flash("Confirm your email before signing in. You can request another link below.", "error")
                return redirect(url_for("resend_email_confirmation"))
            elif (user.role == "organization_admin" and user.organization_id is not None
                  and user.organization.status != "Approved"):
                flash("This organization is not currently approved for sign-in.", "error")
            else:
                establish_session(user, remember=request.form.get("remember_me") == "yes")
                flash(f"Welcome back, {user_display_name(user)}.", "success")
                return redirect(safe_internal_path(request.args.get("next")) or url_for("welcome"))
        return render_template("login.html", login_role=login_role)

    @app.route("/member/sign-in", methods=["GET", "POST"])
    @limiter.limit("5 per minute")
    def member_sign_in():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            organization_email = request.form.get("organization_email", "").strip().lower()
            candidates = User.query.filter_by(email=email, role="member", active=True).all()
            if organization_email:
                organization = Organization.query.filter_by(email=organization_email, status="Approved").first()
                user = next((u for u in candidates if organization and
                             (u.organization_id == organization.id or Membership.query.filter_by(
                                 user_id=u.id, organization_id=organization.id).first())), None)
            else:
                user = candidates[0] if len(candidates) == 1 else None
            password = request.form.get("password", "")
            try:
                valid = bool(user and password_hasher.verify(user.password_hash, password))
            except VerifyMismatchError:
                valid = False
            if not valid:
                flash("Email or password is incorrect. If this email is linked to older separate organization accounts, include an organization contact email.", "error")
            elif not user.email_verified:
                flash("Confirm your email before signing in. You can request another link below.", "error")
                return redirect(url_for("resend_email_confirmation"))
            else:
                establish_session(user, remember=request.form.get("remember_me") == "yes")
                flash(f"Welcome back, {user_display_name(user)}.", "success")
                return redirect(safe_internal_path(request.args.get("next")) or url_for("welcome"))
        organizations = Organization.query.filter_by(status="Approved").order_by(Organization.name).all()
        return render_template("member_sign_in.html", organizations=organizations)

    @app.post("/logout")
    @login_required
    def logout():
        g.user.session_nonce = secrets.token_urlsafe(32)
        db.session.commit()
        session.clear()
        return redirect(url_for("index"))

    @app.route("/member/register", methods=["GET", "POST"])
    @limiter.limit("5 per hour")
    def member_register():
        if g.user:
            return redirect(url_for("welcome"))
        return render_template("member_register.html", organizations=[], invitation_only=True)

    platform_scopes = {
        "organization_reviewer": ("Organization reviewer", ["dashboard", "organizations"]),
        "finance_reviewer": ("Finance reviewer", ["dashboard", "payments", "refunds", "reports"]),
        "read_only": ("Read only", ["dashboard"]),
    }
    organization_scopes = {
        "events_manager": ("Events and offers manager", ["dashboard", "events", "offers", "branding"]),
        "membership_manager": ("Membership manager", ["dashboard", "members", "applications"]),
        "plans_manager": ("Plans manager", ["dashboard", "plans", "offers"]),
        "finance_manager": ("Finance manager", ["dashboard", "finance"]),
        "read_only": ("Read only", ["dashboard"]),
    }

    @app.route("/staff/invite/<token>", methods=["GET", "POST"])
    @limiter.limit("10 per hour")
    def claim_staff_invitation(token: str):
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        invitation = StaffInvitation.query.filter_by(token_hash=token_hash).first()
        invitation_expiry = (invitation.expires_at.replace(tzinfo=timezone.utc)
                             if invitation and invitation.expires_at.tzinfo is None
                             else invitation.expires_at if invitation else None)
        if (not invitation or invitation.claimed_at or invitation.revoked_at or invitation_expiry <= utcnow()
                or (invitation.organization_id and invitation.organization.status != "Approved")):
            abort(404)
        if request.method == "POST":
            password = request.form.get("password", "")
            confirmation = request.form.get("password_confirmation", "")
            if len(password) < 12 or len(password) > 128 or password != confirmation:
                flash("Use a matching password of 12–128 characters.", "error")
            else:
                existing = User.query.filter_by(email=invitation.email, role=invitation.role,
                                                organization_id=invitation.organization_id).first()
                if existing:
                    abort(409)
                user = User(email=invitation.email, full_name=invitation.full_name,
                            password_hash=password_hasher.hash(password), role=invitation.role,
                            organization_id=invitation.organization_id, email_verified=True,
                            admin_permissions=invitation.admin_permissions,
                            admin_title=invitation.admin_title)
                invitation.claimed_at = utcnow()
                db.session.add(user)
                record_activity("staff_invitation_claimed", "staff_invitation", invitation.id,
                                invitation.admin_title)
                try:
                    db.session.commit()
                except IntegrityError:
                    db.session.rollback()
                    flash("An account already exists for this email. Sign in to continue.", "error")
                else:
                    establish_session(user)
                    flash("Your staff account is ready.", "success")
                    return redirect(url_for("welcome"))
        return render_template("staff_invite_claim.html", invitation=invitation)

    @app.route("/admin/team", methods=["GET", "POST"])
    @role_required("platform_admin")
    @limiter.limit("10 per hour")
    def platform_team():
        if "*" not in admin_permission_set(g.user):
            abort(403)
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            full_name = request.form.get("full_name", "").strip()
            role_key = request.form.get("team_role", "")
            selected = platform_scopes.get(role_key)
            if not selected or not valid_email(email) or not full_name or len(full_name) > 120:
                flash("Enter a valid name, email, and team role.", "error")
            elif User.query.filter_by(email=email, role="platform_admin").first() or StaffInvitation.query.filter_by(
                    email=email, role="platform_admin", claimed_at=None, revoked_at=None).first():
                flash("That email already has a platform account or pending invitation.", "error")
            else:
                title, permissions = selected
                invitation = StaffInvitation(email=email, full_name=full_name, role="platform_admin",
                    organization_id=None, admin_permissions=json.dumps(permissions), admin_title=title,
                    token_hash="", expires_at=utcnow() + timedelta(hours=24), created_by_user_id=g.user.id)
                if deliver_staff_invitation(invitation):
                    db.session.commit()
                    flash("Team invitation sent. It expires in 24 hours.", "success")
                else:
                    db.session.rollback()
                    flash("Email could not be sent. Configure SMTP first; no invitation was saved.", "error")
            return redirect(url_for("platform_team"))
        members = User.query.filter_by(role="platform_admin").order_by(User.email).all()
        invitations = StaffInvitation.query.filter_by(role="platform_admin", claimed_at=None,
                                                       revoked_at=None).order_by(
            StaffInvitation.created_at.desc()).all()
        return render_template("admin_team.html", members=members, invitations=invitations,
                               roles=platform_scopes)

    @app.post("/admin/team/<int:user_id>/deactivate")
    @role_required("platform_admin")
    def platform_team_deactivate(user_id: int):
        if "*" not in admin_permission_set(g.user):
            abort(403)
        member = User.query.filter_by(id=user_id, role="platform_admin").first_or_404()
        if member.id == g.user.id:
            abort(400)
        member.active = False
        member.session_nonce = secrets.token_urlsafe(32)
        record_activity("platform_team_member_deactivated", "user", member.id)
        db.session.commit()
        flash("Platform team access has been disabled.", "success")
        return redirect(url_for("platform_team"))

    @app.post("/admin/team/<int:user_id>/role")
    @role_required("platform_admin")
    def platform_team_change_role(user_id: int):
        if "*" not in admin_permission_set(g.user):
            abort(403)
        member = User.query.filter_by(id=user_id, role="platform_admin").first_or_404()
        role = platform_scopes.get(request.form.get("team_role", ""))
        if not role:
            abort(400)
        if member.id == g.user.id:
            abort(400)
        member.admin_title = role[0]
        member.admin_permissions = json.dumps(role[1])
        record_activity("platform_team_role_changed", "user", member.id, role[0])
        db.session.commit()
        flash("Platform team role updated.", "success")
        return redirect(url_for("platform_team"))

    @app.post("/admin/team/invitations/<int:invitation_id>/revoke")
    @role_required("platform_admin")
    def platform_team_revoke_invitation(invitation_id: int):
        if "*" not in admin_permission_set(g.user):
            abort(403)
        invitation = StaffInvitation.query.filter_by(id=invitation_id, role="platform_admin",
                                                     claimed_at=None, revoked_at=None).first_or_404()
        invitation.revoked_at = utcnow()
        db.session.commit()
        flash("Platform team invitation revoked.", "success")
        return redirect(url_for("platform_team"))

    @app.route("/organization/team", methods=["GET", "POST"])
    @role_required("organization_admin")
    @limiter.limit("10 per hour")
    def organization_team():
        if "*" not in admin_permission_set(g.user):
            abort(403)
        organization_id = g.user.organization_id
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            full_name = request.form.get("full_name", "").strip()
            role_key = request.form.get("team_role", "")
            custom_title = request.form.get("workspace_title", "").strip()
            selected = organization_scopes.get(role_key)
            if (not selected or not valid_email(email) or not full_name or len(full_name) > 120
                    or len(custom_title) > 60):
                flash("Enter a valid name, email, team role, and workspace title (up to 60 characters).", "error")
            elif User.query.filter_by(email=email, organization_id=organization_id).first() or StaffInvitation.query.filter_by(
                    email=email, role="organization_admin", organization_id=organization_id,
                    claimed_at=None, revoked_at=None).first():
                flash("That email already has an account or pending invitation for this organization.", "error")
            else:
                role_title, permissions = selected
                title = custom_title or role_title
                invitation = StaffInvitation(email=email, full_name=full_name, role="organization_admin",
                    organization_id=organization_id, admin_permissions=json.dumps(permissions),
                    admin_title=title, token_hash="", expires_at=utcnow() + timedelta(hours=24),
                    created_by_user_id=g.user.id)
                if deliver_staff_invitation(invitation):
                    db.session.commit()
                    flash("Staff invitation sent. It expires in 24 hours.", "success")
                else:
                    db.session.rollback()
                    flash("Email could not be sent. Configure SMTP first; no invitation was saved.", "error")
            return redirect(url_for("organization_team"))
        members = User.query.filter_by(role="organization_admin", organization_id=organization_id).order_by(User.email).all()
        invitations = StaffInvitation.query.filter_by(role="organization_admin", organization_id=organization_id,
                                                     claimed_at=None, revoked_at=None).order_by(StaffInvitation.created_at.desc()).all()
        return render_template("organization_team.html", organization=g.user.organization,
                               members=members, invitations=invitations, roles=organization_scopes)

    @app.post("/organization/team/<int:user_id>/deactivate")
    @role_required("organization_admin")
    def organization_team_deactivate(user_id: int):
        if "*" not in admin_permission_set(g.user):
            abort(403)
        member = User.query.filter_by(id=user_id, role="organization_admin",
                                      organization_id=g.user.organization_id).first_or_404()
        if member.id == g.user.id:
            abort(400)
        member.active = False
        member.session_nonce = secrets.token_urlsafe(32)
        record_activity("organization_team_member_deactivated", "user", member.id,
                        f"Organization {g.user.organization_id}")
        db.session.commit()
        flash("Organization team access has been disabled.", "success")
        return redirect(url_for("organization_team"))

    @app.post("/organization/team/<int:user_id>/role")
    @role_required("organization_admin")
    def organization_team_change_role(user_id: int):
        if "*" not in admin_permission_set(g.user):
            abort(403)
        member = User.query.filter_by(id=user_id, role="organization_admin",
                                      organization_id=g.user.organization_id).first_or_404()
        role = organization_scopes.get(request.form.get("team_role", ""))
        if not role:
            abort(400)
        if member.id == g.user.id:
            abort(400)
        role_titles = {item[0] for item in organization_scopes.values()}
        if not member.admin_title or member.admin_title in role_titles:
            member.admin_title = role[0]
        member.admin_permissions = json.dumps(role[1])
        record_activity("organization_team_role_changed", "user", member.id,
                        f"Organization {g.user.organization_id}; {role[0]}")
        db.session.commit()
        flash("Organization staff role updated.", "success")
        return redirect(url_for("organization_team"))

    @app.post("/organization/team/invitations/<int:invitation_id>/revoke")
    @role_required("organization_admin")
    def organization_team_revoke_invitation(invitation_id: int):
        if "*" not in admin_permission_set(g.user):
            abort(403)
        invitation = StaffInvitation.query.filter_by(id=invitation_id, role="organization_admin",
            organization_id=g.user.organization_id, claimed_at=None, revoked_at=None).first_or_404()
        invitation.revoked_at = utcnow()
        db.session.commit()
        flash("Organization staff invitation revoked.", "success")
        return redirect(url_for("organization_team"))

    @app.get("/admin/email-setup")
    @role_required("platform_admin")
    def admin_email_setup():
        settings = {key: bool(app.config.get(key)) for key in
                    ("MAIL_SERVER", "MAIL_USERNAME", "MAIL_PASSWORD", "MAIL_FROM")}
        return render_template("admin_email_setup.html", settings=settings,
                               smtp_configured=all(settings.get(key) for key in
                                                   ("MAIL_SERVER", "MAIL_USERNAME", "MAIL_PASSWORD", "MAIL_FROM")),
                               preview_enabled=bool(app.config.get("DEV_EMAIL_PREVIEW")))

    @app.post("/admin/email-setup/test")
    @role_required("platform_admin")
    @limiter.limit("3 per hour")
    def admin_send_test_email():
        if not all(app.config.get(key) for key in ("MAIL_SERVER", "MAIL_FROM")):
            flash("SMTP is not configured yet. Add the settings to your local .env file and restart Socio.", "error")
            return redirect(url_for("admin_email_setup"))
        message = EmailMessage()
        message["Subject"] = "Socio email setup test"
        message["From"] = app.config["MAIL_FROM"]
        message["To"] = g.user.email
        message.set_content(
            f"Hi {g.user.full_name or 'there'},\n\n"
            "This is a test email from Socio. Your SMTP settings are working, and the app can send account messages."
        )
        try:
            server_name = app.config["MAIL_SERVER"]
            port = int(app.config.get("MAIL_PORT", 587))
            smtp = (smtplib.SMTP_SSL(server_name, port, timeout=15) if app.config.get("MAIL_USE_SSL")
                    else smtplib.SMTP(server_name, port, timeout=15))
            with smtp:
                if app.config.get("MAIL_USE_TLS") and not app.config.get("MAIL_USE_SSL"):
                    smtp.starttls()
                username = app.config.get("MAIL_USERNAME")
                if username:
                    smtp.login(username, app.config.get("MAIL_PASSWORD") or "")
                smtp.send_message(message)
        except (OSError, smtplib.SMTPException, ValueError):
            app.logger.exception("SMTP test email failed")
            flash("Socio could not send the test email. Check the SMTP settings in your local .env file.", "error")
        else:
            flash(f"Test email sent to {g.user.email}. Check your inbox and spam folder.", "success")
        return redirect(url_for("admin_email_setup"))

    @app.get("/admin/organizations")
    @role_required("platform_admin")
    def admin_organizations():
        organizations = Organization.query.order_by(Organization.created_at.desc()).all()
        member_counts = {org.id: Membership.query.filter_by(organization_id=org.id).count() for org in organizations}
        pending_payments = SimulatedPayment.query.filter_by(status="Pending").order_by(SimulatedPayment.id.desc()).all()
        payments = SimulatedPayment.query.order_by(SimulatedPayment.id.desc()).limit(50).all()
        pending_refunds = SimulatedRefund.query.filter_by(status="Requested").order_by(SimulatedRefund.id.desc()).all()
        activity = ActivityLog.query.order_by(ActivityLog.id.desc()).limit(20).all()
        paid_total_minor = db.session.query(func.coalesce(func.sum(SimulatedPayment.amount_minor), 0)).filter_by(status="Paid").scalar()
        refunded_total_minor = db.session.query(func.coalesce(func.sum(SimulatedRefund.amount_minor), 0)).filter_by(status="Approved").scalar()
        return render_template("admin_organizations.html", organizations=organizations,
                               member_counts=member_counts,
                               pending_payments=pending_payments, payments=payments,
                               pending_refunds=pending_refunds, activity=activity,
                               member_total=Membership.query.count(), payment_total=SimulatedPayment.query.count(),
                               paid_total_minor=paid_total_minor, refunded_total_minor=refunded_total_minor,
                               net_total_minor=paid_total_minor - refunded_total_minor,
                               simulated_day=simulated_day())

    @app.get("/admin/reconciliation.csv")
    @role_required("platform_admin")
    def admin_reconciliation_export():
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["payment_id", "organization", "member", "email", "plan", "type", "channel",
                         "payment_status", "gross_minor", "refund_status", "refund_minor", "net_minor",
                         "created_simulated_day", "confirmed_simulated_day"])
        payments = SimulatedPayment.query.order_by(SimulatedPayment.id.asc()).all()
        for payment in payments:
            refund = payment.refund_request
            paid = payment.status == "Paid"
            refunded = bool(refund and refund.status == "Approved")
            writer.writerow([payment.id, spreadsheet_safe(payment.organization.name),
                             spreadsheet_safe(payment.membership.full_name), spreadsheet_safe(payment.membership.user.email),
                             spreadsheet_safe(payment.plan.name), payment.kind, payment.channel, payment.status,
                             payment.amount_minor if paid else 0,
                             refund.status if refund else "", refund.amount_minor if refunded else 0,
                             (payment.amount_minor if paid else 0) - (refund.amount_minor if refunded else 0),
                             payment.created_day, payment.confirmed_day if payment.confirmed_day is not None else ""])
        response = Response("\ufeff" + output.getvalue(), mimetype="text/csv; charset=utf-8")
        response.headers["Content-Disposition"] = 'attachment; filename="socio-simulated-reconciliation.csv"'
        return response

    @app.get("/admin")
    @app.get("/platform-admin")
    def admin_entry():
        """Private entry point for the platform-admin workspace."""
        if g.user and g.user.role == "platform_admin":
            return redirect(url_for("admin_organizations"))
        return redirect(url_for("login", role="platform_admin"))

    @app.post("/admin/organizations/<int:organization_id>/<action>")
    @role_required("platform_admin")
    def admin_change_organization(organization_id: int, action: str):
        organization = db.session.get(Organization, organization_id)
        if not organization:
            abort(404)
        transitions = {"approve": "Approved", "suspend": "Suspended", "reinstate": "Approved"}
        new_status = transitions.get(action)
        if not new_status:
            abort(404)
        allowed_from = {"approve": "Pending", "suspend": "Approved", "reinstate": "Suspended"}
        if organization.status != allowed_from[action]:
            abort(409)
        old_status = organization.status
        organization.status = new_status
        record_activity(f"organization_{action}", "organization", organization.id,
                        f"{old_status} to {new_status}")
        db.session.commit()
        flash(f"{organization.name} is now {new_status.lower()}.", "success")
        return redirect(url_for("admin_organizations"))

    @app.post("/admin/payments/<int:payment_id>/confirm")
    @role_required("platform_admin")
    def admin_confirm_payment(payment_id: int):
        payment = db.session.get(SimulatedPayment, payment_id)
        if not payment:
            abort(404)
        settle_simulated_payment(payment)
        flash(f"Simulated payment #{payment.id} confirmation delivered.", "success")
        return redirect(url_for("admin_organizations"))

    @app.post("/admin/refunds/<int:refund_id>/<action>")
    @role_required("platform_admin")
    def admin_process_refund(refund_id: int, action: str):
        refund = db.session.get(SimulatedRefund, refund_id)
        if not refund:
            abort(404)
        if action not in ("approve", "decline"):
            abort(404)
        if refund.status != "Requested":
            abort(409)
        refund.status = "Approved" if action == "approve" else "Declined"
        refund.processed_by_user_id = g.user.id
        refund.processed_day = simulated_day()
        record_activity(f"simulated_refund_{action}d" if action == "decline" else "simulated_refund_approved",
                        "refund", refund.id,
                        f"LKR {refund.amount_minor / 100:.2f}; simulated only; payment #{refund.payment_id}")
        db.session.commit()
        flash(f"Simulated refund #{refund.id} {refund.status.lower()}. No money was moved.", "success")
        return redirect(url_for("admin_organizations"))

    @app.post("/admin/simulation/advance")
    @role_required("platform_admin")
    def admin_advance_simulation():
        try:
            days = int(request.form.get("days", "15"))
        except ValueError:
            abort(400)
        if days not in (7, 15):
            abort(400)
        state = db.session.get(SimulationState, 1)
        if not state:
            state = SimulationState(id=1, current_day=0)
            db.session.add(state)
        previous_day = state.current_day
        state.current_day += days
        renewal_count = 0
        downgrade_count = 0
        loyalty_reward_count = 0
        for organization in Organization.query.filter_by(loyalty_reward_enabled=True).all():
            loyalty_reward_count += grant_loyalty_rewards(organization, previous_day, state.current_day)
        for membership in Membership.query.filter_by(status="Active").all():
            if membership.end_day is None or membership.end_day > state.current_day:
                continue
            if membership.next_plan_id and not membership.cancel_at_period_end:
                membership.plan_id = membership.next_plan_id
                membership.next_plan_id = None
                downgrade_count += 1
                record_activity("membership_downgrade_applied", "membership", membership.id,
                                f"Plan changed at period end on simulated day {state.current_day}")
            if membership.cancel_at_period_end:
                continue
            if membership.last_renewal_due_day == membership.end_day:
                continue
            payment = create_simulated_payment(membership, membership.plan, "renewal", membership.plan.price_minor)
            if payment.kind == "renewal":
                membership.last_renewal_due_day = membership.end_day
                renewal_count += 1
        record_activity("simulation_advanced", "simulation", state.id,
                        f"Advanced {days} days to day {state.current_day}; {renewal_count} renewal(s) due; {downgrade_count} downgrade(s) applied; {loyalty_reward_count} loyalty gift(s) awarded")
        db.session.commit()
        flash(f"Simulation advanced {days} days to day {state.current_day}. {renewal_count} renewal payment(s) are due; {downgrade_count} scheduled downgrade(s) took effect; {loyalty_reward_count} loyalty gift(s) were awarded.", "success")
        return redirect(url_for("admin_organizations"))

    @app.post("/admin/simulation/reset")
    @role_required("platform_admin")
    def admin_reset_simulation():
        # Preserve platform administrator accounts while clearing demo-only data.
        if request.form.get("confirm_reset") != "yes":
            flash("Select the confirmation box before resetting demo data.", "error")
            return redirect(url_for("admin_organizations"))
        ActivityLog.query.delete(synchronize_session=False)
        MemberInvitation.query.delete(synchronize_session=False)
        MembershipApplication.query.delete(synchronize_session=False)
        OrganizationEvent.query.delete(synchronize_session=False)
        EmailVerificationToken.query.delete(synchronize_session=False)
        MembershipAttendance.query.delete(synchronize_session=False)
        MemberQrPass.query.delete(synchronize_session=False)
        OrganizationQrCheckin.query.delete(synchronize_session=False)
        MemberPlanChange.query.delete(synchronize_session=False)
        MemberNotification.query.delete(synchronize_session=False)
        OrganizationOffer.query.delete(synchronize_session=False)
        SimulatedRefund.query.delete(synchronize_session=False)
        SimulatedPayment.query.delete(synchronize_session=False)
        Membership.query.delete(synchronize_session=False)
        Plan.query.delete(synchronize_session=False)
        User.query.filter(User.role != "platform_admin").delete(synchronize_session=False)
        Organization.query.delete(synchronize_session=False)
        state = db.session.get(SimulationState, 1)
        if state:
            state.current_day = 0
        else:
            db.session.add(SimulationState(id=1, current_day=0))
        record_activity("simulation_reset", "simulation", 1,
                        "Demo data removed; platform administrator accounts preserved")
        db.session.commit()
        for filename in os.listdir(app.config["ORGANIZATION_LOGO_FOLDER"]):
            file_path = os.path.join(app.config["ORGANIZATION_LOGO_FOLDER"], filename)
            if os.path.isfile(file_path):
                os.remove(file_path)
        flash("Demo data and simulated time were reset. Platform admin accounts were kept.", "success")
        return redirect(url_for("admin_organizations"))

    @app.route("/organization/plans", methods=["GET", "POST"])
    @role_required("organization_admin")
    def organization_plans():
        organization = db.session.get(Organization, g.user.organization_id)
        if request.method == "POST":
            if organization.status != "Approved":
                abort(403)
            name = request.form.get("name", "").strip()
            benefits = request.form.get("benefits", "").strip()
            period = request.form.get("billing_period", "")
            price = request.form.get("price_minor", "")
            if not name or len(name) > 100 or len(benefits) > 2000 or period not in ("monthly", "yearly"):
                flash("Enter a valid plan name, billing period, and benefits.", "error")
            else:
                try:
                    price_minor = int(price)
                    if price_minor < 0 or price_minor > 2_000_000_000:
                        raise ValueError
                except (TypeError, ValueError):
                    flash("Price must be a non-negative whole number of LKR minor units.", "error")
                else:
                    plan = Plan(organization_id=organization.id, name=name, benefits=benefits,
                                billing_period=period, price_minor=price_minor)
                    db.session.add(plan)
                    db.session.commit()
                    flash("Plan created.", "success")
                    return redirect(url_for("organization_plans"))
        plans = Plan.query.filter_by(organization_id=organization.id).order_by(Plan.id.desc()).all()
        search = request.args.get("q", "").strip()[:100]
        member_query = Membership.query.filter_by(organization_id=organization.id)
        if search:
            needle = f"%{search}%"
            member_query = member_query.join(User, Membership.user_id == User.id).filter(
                or_(Membership.full_name.ilike(needle), User.email.ilike(needle)))
        memberships = member_query.order_by(Membership.id.desc()).all()
        offline_eligible = set()
        for membership in memberships:
            application = MembershipApplication.query.filter_by(membership_id=membership.id).first()
            if (organization.status == "Approved" and not membership.cancel_at_period_end
                    and membership_status(membership) in ("Pending", "Expired")
                    and not (application and application.status == "Pending")
                    and not SimulatedPayment.query.filter_by(membership_id=membership.id, status="Pending").first()):
                offline_eligible.add(membership.id)
        payments = SimulatedPayment.query.filter_by(organization_id=organization.id).order_by(SimulatedPayment.id.desc()).limit(50).all()
        invitations = MemberInvitation.query.filter_by(organization_id=organization.id).order_by(MemberInvitation.id.desc()).limit(100).all()
        refunds = SimulatedRefund.query.filter_by(organization_id=organization.id).order_by(SimulatedRefund.id.desc()).limit(50).all()
        gross_paid_minor = db.session.query(func.coalesce(func.sum(SimulatedPayment.amount_minor), 0)).filter_by(
            organization_id=organization.id, status="Paid").scalar()
        refund_total_minor = db.session.query(func.coalesce(func.sum(SimulatedRefund.amount_minor), 0)).filter_by(
            organization_id=organization.id, status="Approved").scalar()
        offers = OrganizationOffer.query.filter_by(organization_id=organization.id).order_by(
            OrganizationOffer.id.desc()).all()
        applications = (MembershipApplication.query.join(Membership).filter(
            Membership.organization_id == organization.id).order_by(
                MembershipApplication.status.asc(), MembershipApplication.submitted_at.desc()).limit(100).all())
        events = OrganizationEvent.query.filter_by(organization_id=organization.id).order_by(
            OrganizationEvent.event_date.desc()).limit(100).all()
        return render_template("plans.html", organization=organization, plans=plans,
                               memberships=memberships, payments=payments, simulated_day=simulated_day(),
                               invitations=invitations, offers=offers,
                               offer_types=("Welcome bonus", "Extra benefit", "Limited time", "Other"),
                               search=search, offline_eligible=offline_eligible, refunds=refunds,
                               applications=applications,
                               events=events,
                               today_date=utcnow().date().isoformat(),
                               gross_paid_minor=gross_paid_minor, refund_total_minor=refund_total_minor,
                               net_paid_minor=gross_paid_minor - refund_total_minor)

    @app.route("/organization/forms", methods=["GET", "POST"])
    @role_required("organization_admin")
    def organization_forms():
        organization = db.session.get(Organization, g.user.organization_id)
        try:
            fields = json.loads(organization.onboarding_form or "[]")
            if not isinstance(fields, list):
                fields = []
        except (TypeError, ValueError):
            fields = []
        if request.method == "POST":
            remove_id = request.form.get("remove_id", "")
            if remove_id:
                fields = [field for field in fields if field.get("id") != remove_id]
                organization.onboarding_form = json.dumps(fields)
                db.session.commit()
                flash("Form question removed. Existing member responses are retained.", "success")
                return redirect(url_for("organization_forms"))
            label = request.form.get("label", "").strip()
            kind = request.form.get("kind", "text")
            if not label or len(label) > 160 or kind not in ("text", "long_text", "checkbox"):
                flash("Add a question up to 160 characters and choose a valid answer type.", "error")
            elif len(fields) >= 15:
                flash("An onboarding form can have up to 15 custom questions.", "error")
            else:
                fields.append({"id": secrets.token_hex(8), "label": label,
                               "kind": kind, "required": request.form.get("required") == "yes"})
                organization.onboarding_form = json.dumps(fields)
                record_activity("onboarding_question_added", "organization", organization.id, label)
                db.session.commit()
                flash("Question added to the member welcome form.", "success")
                return redirect(url_for("organization_forms"))
        return render_template("organization_forms.html", organization=organization, fields=fields)

    @app.get("/organization/members")
    @role_required("organization_admin")
    def organization_members():
        organization = db.session.get(Organization, g.user.organization_id)
        search = request.args.get("q", "").strip()[:100]
        query = Membership.query.filter_by(organization_id=organization.id).join(
            User, Membership.user_id == User.id)
        if search:
            needle = f"%{search}%"
            query = query.filter(or_(Membership.full_name.ilike(needle), User.email.ilike(needle)))
        memberships = query.order_by(Membership.full_name.asc()).limit(500).all()
        plans = Plan.query.filter_by(organization_id=organization.id, archived=False).order_by(Plan.name).all()
        responses_by_membership = {}
        for membership in memberships:
            try:
                saved = json.loads(membership.onboarding_responses or "[]")
                responses_by_membership[membership.id] = saved if isinstance(saved, list) else []
            except (TypeError, ValueError):
                responses_by_membership[membership.id] = []
        plan_changes = MemberPlanChange.query.filter_by(organization_id=organization.id,
            status="Requested").order_by(MemberPlanChange.requested_at.asc()).all()
        billing_proofs = MemberBillingProof.query.filter_by(organization_id=organization.id).order_by(
            MemberBillingProof.submitted_at.desc()).limit(200).all()
        return render_template("organization_members.html", organization=organization,
                               memberships=memberships, plans=plans, search=search,
                               responses_by_membership=responses_by_membership,
                               plan_changes=plan_changes, billing_proofs=billing_proofs)

    @app.post("/organization/billing-proofs/<int:proof_id>/<action>")
    @role_required("organization_admin")
    def organization_review_billing_proof(proof_id: int, action: str):
        if action not in ("accept", "decline"):
            abort(404)
        proof = MemberBillingProof.query.filter_by(
            id=proof_id, organization_id=g.user.organization_id).first_or_404()
        if proof.status != "Submitted":
            flash("This bill or expense proof has already been reviewed.", "error")
            return redirect(url_for("organization_members"))
        note = request.form.get("organization_note", "").strip()
        if len(note) > 500:
            flash("The review note can be up to 500 characters.", "error")
            return redirect(url_for("organization_members"))
        proof.status = "Accepted" if action == "accept" else "Declined"
        proof.organization_note = note
        proof.reviewed_by_user_id = g.user.id
        proof.reviewed_at = utcnow()
        message = (f"{proof.organization.name} reviewed your {proof.record_type.lower()} proof "
                   f"for LKR {proof.amount_minor / 100:,.2f}: {proof.status.lower()}.")
        if note:
            message += f" Note: {note}"
        message += " This review does not move or reimburse money."
        db.session.add(MemberNotification(organization_id=proof.organization_id,
            user_id=proof.user_id, created_by_user_id=g.user.id,
            title="Bill or expense proof reviewed", body=message))
        record_activity(f"member_billing_proof_{action}d", "billing_proof", proof.id,
                        f"{proof.record_type}; LKR {proof.amount_minor / 100:.2f}")
        db.session.commit()
        flash("Proof review saved. Socio has not transferred or reimbursed money.", "success")
        return redirect(url_for("organization_members"))

    @app.get("/organization/attendance")
    @role_required("organization_admin")
    @limiter.limit("20 per hour")
    def organization_attendance():
        organization = db.session.get(Organization, g.user.organization_id)
        if organization.status != "Approved":
            abort(403)
        now = utcnow()
        OrganizationQrCheckin.query.filter_by(organization_id=organization.id,
            revoked_at=None).update({"revoked_at": now}, synchronize_session=False)
        raw_token = secrets.token_urlsafe(32)
        checkin_code = OrganizationQrCheckin(token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
            organization_id=organization.id, created_by_user_id=g.user.id,
            expires_at=now + timedelta(minutes=10))
        db.session.add(checkin_code)
        db.session.commit()
        checkin_path = url_for("member_self_checkin", token=raw_token)
        qr_url = app.config["PUBLIC_BASE_URL"].rstrip("/") + checkin_path
        attendance = MembershipAttendance.query.filter_by(organization_id=organization.id).order_by(
            MembershipAttendance.checked_in_at.desc()).limit(100).all()
        return render_template("organization_attendance.html", organization=organization,
            checkin_code=checkin_code, qr_image=qr_png_data_uri(qr_url), attendance=attendance)

    @app.post("/organization/member-plan-changes/<int:change_id>/<action>")
    @role_required("organization_admin")
    def organization_review_member_plan_change(change_id: int, action: str):
        change = MemberPlanChange.query.filter_by(id=change_id,
            organization_id=g.user.organization_id).first_or_404()
        if action not in ("approve", "decline"):
            abort(404)
        if change.status != "Requested":
            flash("This tier-change request has already been reviewed.", "error")
            return redirect(url_for("organization_members"))
        note = request.form.get("organization_note", "").strip()[:500]
        change.status = "Approved" if action == "approve" else "Declined"
        change.organization_note = note
        change.reviewed_at = utcnow()
        change.reviewed_by_user_id = g.user.id
        payment = None
        if action == "approve":
            target = Plan.query.filter_by(id=change.to_plan_id,
                organization_id=change.organization_id, archived=False).first()
            if not target:
                abort(409)
            current = change.membership.plan
            if target.billing_period != current.billing_period:
                db.session.rollback()
                flash("The billing period must stay the same for this tier change. Ask the member to submit a request using a matching billing period.", "error")
                return redirect(url_for("organization_members"))
            if target.price_minor > current.price_minor:
                pending = SimulatedPayment.query.filter_by(
                    membership_id=change.membership_id, status="Pending").first()
                if pending and (pending.kind != "upgrade" or pending.plan_id != target.id):
                    db.session.rollback()
                    flash("Resolve the member’s existing pending payment before approving this upgrade.", "error")
                    return redirect(url_for("organization_members"))
                days_left = max(0, (change.membership.end_day or simulated_day()) - simulated_day())
                amount = round((target.price_minor - current.price_minor) * days_left /
                               period_days(current))
                payment = create_simulated_payment(change.membership, target, "upgrade", amount)
                message_suffix = (f" Your simulated prorated payment of LKR {payment.amount_minor / 100:,.2f} "
                                  "is ready in Socio. No money will be collected.")
            elif target.price_minor < current.price_minor:
                change.membership.next_plan_id = target.id
                message_suffix = " The lower tier is scheduled to start after your current paid period."
            else:
                change.membership.plan_id = target.id
                message_suffix = " The tier has been updated."
        else:
            message_suffix = ""
        message = (f"Your request to change from {change.from_plan.name} to {change.to_plan.name} "
                   f"was {change.status.lower()} by {change.organization.name}.")
        message += message_suffix
        if note:
            message += f" Organization note: {note}"
        db.session.add(MemberNotification(organization_id=change.organization_id,
            user_id=change.user_id, created_by_user_id=g.user.id,
            title="Membership tier request update", body=message))
        record_activity(f"member_tier_change_{action}d", "membership", change.membership_id,
                        f"{change.from_plan.name} to {change.to_plan.name}")
        db.session.commit()
        emailed = (change.user.email_verified and deliver_member_update_email(
            change.user, change.organization, "Membership tier request update", message))
        flash(f"Request {change.status.lower()}. The member was notified" +
              (" by email and in Socio." if emailed else " in Socio."), "success")
        return redirect(url_for("organization_members"))

    @app.post("/organization/members/add")
    @role_required("organization_admin")
    @limiter.limit("30 per hour")
    def organization_add_member():
        organization = db.session.get(Organization, g.user.organization_id)
        name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        try:
            plan_id = int(request.form.get("plan_id", "0"))
        except ValueError:
            plan_id = 0
        plan = Plan.query.filter_by(id=plan_id, organization_id=organization.id, archived=False).first()
        if organization.status != "Approved":
            abort(403)
        if not name or len(name) > 120 or not valid_email(email) or not plan:
            flash("Enter the member’s name, valid email, and club plan.", "error")
        elif any(Membership.query.filter_by(user_id=user.id, organization_id=organization.id).first()
                 for user in User.query.filter_by(email=email, role="member").all()):
            flash("This person is already in the club member directory.", "error")
        elif MemberInvitation.query.filter_by(organization_id=organization.id, email=email,
                                               status="Pending").filter(
                MemberInvitation.expires_day > simulated_day()).first():
            flash("A current invitation already exists for this email.", "error")
        else:
            raw_token = secrets.token_urlsafe(32)
            invitation = MemberInvitation(organization_id=organization.id, plan_id=plan.id,
                full_name=name, email=email, token_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
                status="Pending", created_day=simulated_day(), expires_day=simulated_day() + 7,
                created_by_user_id=g.user.id)
            db.session.add(invitation)
            db.session.flush()
            if deliver_member_invitation(invitation, raw_token):
                invitation.email_sent_at = utcnow()
                record_activity("member_invited", "invitation", invitation.id,
                                f"{email}; club membership activation")
                db.session.commit()
                flash(f"{name} was added to the invitation list. Socio emailed the activation link to {email}.", "success")
            else:
                db.session.rollback()
                flash("Socio could not send this invitation. Check SMTP settings; no invite was saved.", "error")
        return redirect(url_for("organization_members"))

    @app.post("/organization/notifications")
    @role_required("organization_admin")
    @limiter.limit("20 per hour")
    def organization_send_notification():
        organization = db.session.get(Organization, g.user.organization_id)
        if organization.status != "Approved":
            abort(403)
        title = request.form.get("title", "").strip()
        body = request.form.get("body", "").strip()
        recipient = request.form.get("recipient", "").strip()
        if not title or len(title) > 120 or not body or len(body) > 2000:
            flash("Enter a title up to 120 characters and a message up to 2,000 characters.", "error")
            return redirect(url_for("organization_members"))
        member_query = Membership.query.filter_by(organization_id=organization.id).join(
            User, Membership.user_id == User.id)
        if recipient != "all":
            try:
                membership_id = int(recipient)
            except ValueError:
                abort(400)
            member_query = member_query.filter(Membership.id == membership_id)
        memberships = [membership for membership in member_query.all()
                       if membership_status(membership) == "Active"]
        if not memberships:
            flash("No members matched this notification.", "error")
            return redirect(url_for("organization_members"))
        notifications = []
        for membership in memberships:
            notification = MemberNotification(organization_id=organization.id,
                user_id=membership.user_id, created_by_user_id=g.user.id,
                title=title, body=body)
            db.session.add(notification)
            notifications.append((notification, membership.user))
        record_activity("member_notification_sent", "organization", organization.id,
                        f"{len(notifications)} member(s); {title}")
        db.session.commit()
        delivered = sum(1 for _, user in notifications
                        if user.email_verified and deliver_member_update_email(user, organization, title, body))
        flash(f"Update posted to {len(notifications)} member inbox(es). {delivered} email notification(s) delivered.", "success")
        return redirect(url_for("organization_members"))

    @app.get("/member/notifications")
    @role_required("member")
    def member_notifications():
        notifications = MemberNotification.query.filter_by(user_id=g.user.id).order_by(
            MemberNotification.created_at.desc()).limit(100).all()
        return render_template("member_notifications.html", notifications=notifications)

    @app.post("/member/notifications/<int:notification_id>/read")
    @role_required("member")
    def member_notification_read(notification_id: int):
        notification = MemberNotification.query.filter_by(id=notification_id, user_id=g.user.id).first_or_404()
        notification.read_at = notification.read_at or utcnow()
        db.session.commit()
        return redirect(url_for("member_notifications"))

    @app.post("/organization/loyalty-settings")
    @role_required("organization_admin")
    def organization_loyalty_settings():
        organization = db.session.get(Organization, g.user.organization_id)
        if organization.status != "Approved":
            abort(403)
        enabled = request.form.get("loyalty_reward_enabled") == "yes"
        try:
            after_days = int(request.form.get("loyalty_reward_after_days", "30"))
            reward_days = int(request.form.get("loyalty_reward_days", "30"))
        except (TypeError, ValueError):
            flash("Enter valid loyalty milestone and gift days.", "error")
            return redirect(url_for("organization_plans"))
        if after_days not in (30, 60, 90, 180, 365) or reward_days not in (7, 14, 30, 60):
            flash("Choose a listed active-support milestone and loyalty gift length.", "error")
            return redirect(url_for("organization_plans"))
        organization.loyalty_reward_enabled = enabled
        organization.loyalty_reward_after_days = after_days
        organization.loyalty_reward_days = reward_days
        rewarded = grant_loyalty_rewards(organization, simulated_day(), simulated_day(),
                                         include_already_qualified=True)
        record_activity("loyalty_reward_settings_changed", "organization", organization.id,
                        f"enabled={enabled}; after={after_days}; gift={reward_days} days; awarded={rewarded}")
        db.session.commit()
        message = "Loyalty gift settings saved."
        if rewarded:
            message += f" {rewarded} currently active member(s) received the configured gift."
        flash(message, "success")
        return redirect(url_for("organization_plans"))

    @app.post("/organization/events")
    @role_required("organization_admin")
    @limiter.limit("20 per hour")
    def organization_create_event():
        organization = db.session.get(Organization, g.user.organization_id)
        if organization.status != "Approved":
            abort(403)
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        venue = request.form.get("venue", "").strip()
        try:
            event_date = date.fromisoformat(request.form.get("event_date", ""))
        except ValueError:
            event_date = None
        if (not title or len(title) > 120 or not description or len(description) > 1000
                or len(venue) > 200 or event_date is None or event_date < utcnow().date()):
            flash("Enter an event name, future date, and valid event details.", "error")
            return redirect(url_for("organization_plans"))
        poster = request.files.get("poster_file")
        poster_filename = None
        if poster and poster.filename:
            raw = poster.stream.read(5_000_001)
            if len(raw) > 5_000_000:
                flash("Event posters must be no larger than 5 MB.", "error")
                return redirect(url_for("organization_plans"))
            try:
                normalized = normalize_uploaded_image(raw, max_side=1800, max_output_bytes=4_000_000)
            except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
                flash(str(exc) or "Choose a valid PNG, JPEG, or WebP event poster.", "error")
                return redirect(url_for("organization_plans"))
            poster_filename = f"{secrets.token_hex(20)}.png"
            with open(os.path.join(app.config["ORGANIZATION_LOGO_FOLDER"], poster_filename), "wb") as poster_file:
                poster_file.write(normalized)
        event = OrganizationEvent(organization_id=organization.id, title=title,
                                  description=description, venue=venue,
                                  event_date=event_date, poster_filename=poster_filename)
        db.session.add(event)
        db.session.flush()
        record_activity("organization_event_published", "organization_event", event.id,
                        f"{title}; {event_date.isoformat()}")
        event_url = url_for("public_organization_event", organization_id=organization.id,
                            event_id=event.id, _external=True)
        recipients = queue_organization_update(
            organization, f"New event: {title}",
            f"{event_date.strftime('%d %b %Y')} · {venue or 'Venue to be announced'}\n{description}\n\nView event: {event_url}")
        db.session.commit()
        delivered = email_organization_update(organization, f"New event: {title}",
            f"{event_date.strftime('%d %b %Y')} · {venue or 'Venue to be announced'}\n{description}\n\nView event: {event_url}", recipients)
        flash(f"Event published. {len(recipients)} active member inbox(es) notified; {delivered} email(s) sent.", "success")
        return redirect(url_for("organization_plans"))

    @app.post("/organization/events/<int:event_id>/archive")
    @role_required("organization_admin")
    def organization_archive_event(event_id: int):
        event = OrganizationEvent.query.filter_by(id=event_id,
                                                   organization_id=g.user.organization_id).first_or_404()
        event.archived = True
        record_activity("organization_event_archived", "organization_event", event.id, event.title)
        db.session.commit()
        flash("Event removed from member pages.", "success")
        return redirect(url_for("organization_plans"))

    @app.post("/organization/logo")
    @role_required("organization_admin")
    def organization_upload_logo():
        organization = db.session.get(Organization, g.user.organization_id)
        upload = request.files.get("logo_file")
        if not upload or not upload.filename:
            flash("Choose a PNG, JPEG, or WebP image to upload.", "error")
            return redirect(url_for("organization_plans"))
        raw = upload.stream.read(1_500_001)
        if len(raw) > 1_500_000:
            flash("The logo image must be no larger than 1.5 MB.", "error")
            return redirect(url_for("organization_plans"))
        try:
            normalized_bytes = normalize_uploaded_image(raw, max_side=1200, max_output_bytes=2_000_000)
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
            flash(str(exc) or "That image could not be opened. Choose a valid PNG, JPEG, or WebP logo.", "error")
            return redirect(url_for("organization_plans"))
        filename = f"{secrets.token_hex(20)}.png"
        with open(os.path.join(app.config["ORGANIZATION_LOGO_FOLDER"], filename), "wb") as image_file:
            image_file.write(normalized_bytes)
        previous_filename = organization.logo_filename
        organization.logo_filename = filename
        record_activity("organization_logo_updated", "organization", organization.id)
        db.session.commit()
        if previous_filename:
            try:
                os.remove(os.path.join(app.config["ORGANIZATION_LOGO_FOLDER"], previous_filename))
            except FileNotFoundError:
                pass
        flash("Organization logo updated. It now appears in the member gallery and public page.", "success")
        return redirect(url_for("organization_plans"))

    @app.post("/organization/logo/remove")
    @role_required("organization_admin")
    def organization_remove_logo():
        organization = db.session.get(Organization, g.user.organization_id)
        previous_filename = organization.logo_filename
        organization.logo_filename = None
        record_activity("organization_logo_removed", "organization", organization.id)
        db.session.commit()
        if previous_filename:
            try:
                os.remove(os.path.join(app.config["ORGANIZATION_LOGO_FOLDER"], previous_filename))
            except FileNotFoundError:
                pass
        flash("Organization logo removed.", "success")
        return redirect(url_for("organization_plans"))

    @app.post("/organization/application-settings")
    @role_required("organization_admin")
    def organization_application_settings():
        organization = db.session.get(Organization, g.user.organization_id)
        if organization.status != "Approved":
            abort(403)
        mode = request.form.get("membership_mode", "")
        if mode not in ("approval", "open"):
            abort(400)
        prompt = request.form.get("application_prompt", "").strip()
        if len(prompt) > 500:
            flash("Application instructions can be up to 500 characters.", "error")
            return redirect(url_for("organization_plans"))
        recommendation_required = request.form.get("recommendation_required") == "yes"
        organization.membership_approval_required = mode == "approval" or recommendation_required
        organization.recommendation_required = recommendation_required
        organization.application_prompt = prompt
        record_activity("membership_application_settings_changed", "organization", organization.id,
                        f"approval={organization.membership_approval_required}; recommendation={recommendation_required}")
        db.session.commit()
        flash("Membership application settings saved.", "success")
        return redirect(url_for("organization_plans"))

    @app.post("/organization/applications/<int:application_id>/<action>")
    @role_required("organization_admin")
    def organization_review_application(application_id: int, action: str):
        if action not in ("approve", "decline"):
            abort(404)
        application = (MembershipApplication.query.join(Membership).filter(
            MembershipApplication.id == application_id,
            Membership.organization_id == g.user.organization_id).first_or_404())
        if application.status != "Pending":
            abort(409)
        note = request.form.get("review_note", "").strip()
        if len(note) > 500:
            flash("The review note can be up to 500 characters.", "error")
            return redirect(url_for("organization_plans"))
        application.status = "Approved" if action == "approve" else "Declined"
        application.review_note = note
        application.reviewed_at = utcnow()
        application.reviewed_by_user_id = g.user.id
        if action == "approve":
            payment = create_simulated_payment(application.membership,
                                               application.membership.plan,
                                               "join", application.membership.plan.price_minor)
            record_activity("membership_application_approved", "membership_application", application.id,
                            f"Payment #{payment.id} is ready for member confirmation")
            db.session.commit()
            flash("Application approved. The member can now review and complete the simulated payment.", "success")
        else:
            record_activity("membership_application_declined", "membership_application", application.id, note)
            db.session.commit()
            flash("Application declined. The member can revise and resubmit it.", "info")
        return redirect(url_for("organization_plans"))

    @app.post("/organization/payments/<int:payment_id>/refund")
    @role_required("organization_admin")
    def organization_request_refund(payment_id: int):
        organization = db.session.get(Organization, g.user.organization_id)
        payment = SimulatedPayment.query.filter_by(id=payment_id, organization_id=organization.id).first_or_404()
        reason = request.form.get("reason", "").strip()
        if payment.status != "Paid" or payment.channel != "simulated" or payment.amount_minor <= 0:
            abort(400)
        if SimulatedRefund.query.filter_by(payment_id=payment.id).first():
            flash("A refund request already exists for this payment.", "error")
            return redirect(url_for("organization_plans"))
        if len(reason) < 8 or len(reason) > 500:
            flash("Add a short reason between 8 and 500 characters.", "error")
            return redirect(url_for("organization_plans"))
        refund = SimulatedRefund(payment_id=payment.id, organization_id=organization.id,
                                 amount_minor=payment.amount_minor, reason=reason,
                                 requested_by_user_id=g.user.id, created_day=simulated_day())
        db.session.add(refund)
        db.session.flush()
        record_activity("simulated_refund_requested", "refund", refund.id,
                        f"LKR {refund.amount_minor / 100:.2f}; payment #{payment.id}; simulated only")
        db.session.commit()
        flash("Full simulated refund requested for platform review. No money was moved.", "success")
        return redirect(url_for("organization_plans"))

    @app.get("/organization/members/export.csv")
    @role_required("organization_admin")
    def organization_export_members():
        organization = db.session.get(Organization, g.user.organization_id)
        search = request.args.get("q", "").strip()[:100]
        query = Membership.query.filter_by(organization_id=organization.id)
        if search:
            needle = f"%{search}%"
            query = query.join(User, Membership.user_id == User.id).filter(
                or_(Membership.full_name.ilike(needle), User.email.ilike(needle)))
        rows = query.order_by(Membership.id.desc()).all()
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["member_name", "email", "plan", "membership_status", "billing_period",
                         "start_day", "end_day", "scheduled_plan", "cancel_at_period_end"])
        for membership in rows:
            writer.writerow([spreadsheet_safe(membership.full_name), spreadsheet_safe(membership.user.email),
                             spreadsheet_safe(membership.plan.name), spreadsheet_safe(membership_status(membership)),
                             membership.plan.billing_period,
                             membership.start_day if membership.start_day is not None else "",
                             membership.end_day if membership.end_day is not None else "",
                             spreadsheet_safe(membership.next_plan.name if membership.next_plan else ""),
                             "yes" if membership.cancel_at_period_end else "no"])
        response = Response("\ufeff" + output.getvalue(), mimetype="text/csv; charset=utf-8")
        response.headers["Content-Disposition"] = f'attachment; filename="socio-members-{organization.id}.csv"'
        return response

    @app.get("/organization/members/import-template.csv")
    @role_required("organization_admin")
    def organization_member_import_template():
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["full_name", "email", "plan_name"])
        writer.writerow(["A member name", "member@example.com", "Community"])
        response = Response("\ufeff" + output.getvalue(), mimetype="text/csv; charset=utf-8")
        response.headers["Content-Disposition"] = 'attachment; filename="socio-member-import-template.csv"'
        return response

    @app.post("/organization/members/import")
    @role_required("organization_admin")
    @limiter.limit("10 per hour")
    def organization_import_members():
        organization = db.session.get(Organization, g.user.organization_id)
        if organization.status != "Approved":
            abort(403)
        upload = request.files.get("file")
        if not upload or not upload.filename:
            flash("Choose a CSV file to import.", "error")
            return redirect(url_for("organization_plans"))
        try:
            raw_data = upload.stream.read(2 * 1024 * 1024 + 1)
            text_data = raw_data.decode("utf-8-sig")
        except UnicodeDecodeError:
            flash("The CSV must use UTF-8 encoding.", "error")
            return redirect(url_for("organization_plans"))
        if len(raw_data) > 2 * 1024 * 1024:
            flash("The CSV is larger than the 2 MB limit.", "error")
            return redirect(url_for("organization_plans"))
        reader = csv.DictReader(io.StringIO(text_data))
        headers = {str(header or "").strip().lower() for header in (reader.fieldnames or [])}
        if not {"full_name", "email", "plan_name"}.issubset(headers):
            flash("Include these CSV columns: full_name, email, plan_name.", "error")
            return redirect(url_for("organization_plans"))
        rows = list(reader)
        if not rows or len(rows) > 500:
            flash("Import between 1 and 500 member rows at a time.", "error")
            return redirect(url_for("organization_plans"))
        plans_by_name = {}
        for plan in Plan.query.filter_by(organization_id=organization.id, archived=False).all():
            plans_by_name.setdefault(plan.name.strip().casefold(), []).append(plan)
        normalized, errors, seen = [], [], set()
        today = simulated_day()
        for line_number, raw in enumerate(rows, start=2):
            row = {str(key or "").strip().lower(): (value or "").strip()
                   for key, value in raw.items() if key is not None}
            name = row.get("full_name", "")
            email = row.get("email", "").lower()
            matches = plans_by_name.get(row.get("plan_name", "").casefold(), [])
            issue = None
            if not name or len(name) > 120:
                issue = "name is required (up to 120 characters)"
            elif not valid_email(email):
                issue = "email is invalid"
            elif email in seen:
                issue = "email appears more than once in this CSV"
            elif User.query.filter_by(email=email, organization_id=organization.id).first():
                issue = "an account already exists for this email"
            elif MemberInvitation.query.filter_by(organization_id=organization.id, email=email,
                                                   status="Pending").filter(
                    MemberInvitation.expires_day > today).first():
                issue = "an active invitation already exists for this email"
            elif len(matches) != 1:
                issue = "plan name must match one available plan exactly"
            if issue:
                errors.append(f"Row {line_number}: {issue}.")
            else:
                seen.add(email)
                normalized.append((name, email, matches[0]))
        if errors:
            flash("No invitations were created. " + " ".join(errors[:10]), "error")
            return redirect(url_for("organization_plans"))
        invite_links = []
        for name, email, plan in normalized:
            raw_token = secrets.token_urlsafe(32)
            invite = MemberInvitation(
                organization_id=organization.id, plan_id=plan.id, full_name=name, email=email,
                token_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(), status="Pending",
                created_day=today, expires_day=today + 7, created_by_user_id=g.user.id)
            db.session.add(invite)
            db.session.flush()
            sent = deliver_member_invitation(invite, raw_token)
            if sent:
                invite.email_sent_at = utcnow()
            invite_links.append({"name": name, "email": email, "plan": plan.name,
                                 "sent": sent})
        record_activity("member_invitations_imported", "organization", organization.id,
                        f"{len(invite_links)} email invitation(s); raw invitation tokens are not stored")
        db.session.commit()
        return render_template("invite_results.html", organization=organization, invite_links=invite_links)

    @app.route("/invite/<token>", methods=["GET", "POST"])
    @limiter.limit("20 per hour")
    def claim_member_invitation(token: str):
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        invite = MemberInvitation.query.filter_by(token_hash=token_hash).first()
        if (not invite or invite.status != "Pending" or simulated_day() >= invite.expires_day
                or invite.organization.status != "Approved" or invite.plan.archived):
            abort(404)
        try:
            onboarding_fields = json.loads(invite.organization.onboarding_form or "[]")
            if not isinstance(onboarding_fields, list):
                onboarding_fields = []
            onboarding_fields = [field for field in onboarding_fields
                                 if isinstance(field, dict) and field.get("kind") in
                                 ("text", "long_text", "checkbox") and field.get("id")]
        except (TypeError, ValueError):
            onboarding_fields = []
        existing_members = User.query.filter_by(email=invite.email, role="member").order_by(User.id).all()
        signed_member = bool(g.user and g.user.role == "member" and g.user.email == invite.email)
        if request.method == "POST":
            name = request.form.get("full_name", "").strip()
            gender = request.form.get("gender", "").strip()
            password = request.form.get("password", "")
            confirm = request.form.get("confirm_password", "")
            responses = {field.get("id", ""): request.form.get(f"custom_{field.get('id', '')}", "").strip()
                         for field in onboarding_fields if field.get("kind") != "checkbox"}
            responses.update({field.get("id", ""): ("yes" if request.form.get(
                f"custom_{field.get('id', '')}") == "yes" else "")
                for field in onboarding_fields if field.get("kind") == "checkbox"})
            saved_responses = [{"question": field.get("label", ""),
                                "answer": responses.get(field.get("id", ""), "")}
                               for field in onboarding_fields]
            allowed_genders = ("", "Woman", "Man", "Non-binary", "Prefer not to say", "Other")
            password_member = None
            if existing_members and not signed_member:
                for candidate in existing_members:
                    try:
                        if password_hasher.verify(candidate.password_hash, password):
                            password_member = candidate
                            break
                    except VerifyMismatchError:
                        continue
            if (not name or len(name) > 120 or gender not in allowed_genders
                    or request.form.get("consent") != "yes"
                    or any(field.get("required") and not responses.get(field.get("id", ""))
                           for field in onboarding_fields)):
                flash("Complete the required profile, consent, and organization form fields.", "error")
            elif existing_members and not signed_member and not password_member:
                flash("Enter the password for your existing Socio account to accept this invitation.", "error")
            elif not existing_members and (len(password) < 12 or len(password) > 128 or password != confirm):
                flash("Choose a password from 12 to 128 characters and enter it the same way twice.", "error")
            else:
                existing_org_user = User.query.filter_by(email=invite.email,
                                                         organization_id=invite.organization_id).first()
                if existing_org_user and existing_org_user.role != "member":
                    abort(409)
                if signed_member:
                    member = g.user
                elif existing_members:
                    member = next((candidate for candidate in existing_members
                                   if Membership.query.filter_by(user_id=candidate.id,
                                                                 organization_id=invite.organization_id).first()), None)
                    member = member or password_member
                    if member is None:
                        flash("This email has older separate member accounts. Sign in to the intended account, then open the invitation again.", "error")
                        return render_template("member_invite.html", invite=invite,
                                               onboarding_fields=onboarding_fields,
                                               existing_account=bool(existing_members),
                                               signed_member=signed_member)
                else:
                    member = User(email=invite.email, full_name=name, gender=gender or None,
                                  password_hash=password_hasher.hash(password), role="member",
                                  organization_id=invite.organization_id, email_verified=True)
                    db.session.add(member)
                    db.session.flush()
                existing_membership = Membership.query.filter_by(user_id=member.id,
                                                                   organization_id=invite.organization_id).first()
                if existing_membership:
                    abort(409)
                member.full_name = name or member.full_name
                if gender:
                    member.gender = gender
                # The invitation token was delivered to this exact mailbox, so claiming it verifies the address.
                member.email_verified = True
                membership = Membership(organization_id=invite.organization_id, user_id=member.id,
                                        plan_id=invite.plan_id, full_name=name, status="Active",
                                        start_day=simulated_day(),
                                        end_day=simulated_day() + period_days(invite.plan),
                                        onboarding_responses=json.dumps(saved_responses),
                                        consent_accepted_at=utcnow())
                db.session.add(membership)
                invite.status = "Claimed"
                invite.claimed_day = simulated_day()
                invite.claimed_user = member
                record_activity("member_invitation_claimed", "invitation", invite.id,
                                "Invitation claimed; member account created")
                try:
                    db.session.commit()
                except IntegrityError:
                    db.session.rollback()
                    abort(409)
                establish_session(member)
                flash(f"Welcome to Socio and {invite.organization.name}. Your club access is ready.", "success")
                return redirect(url_for("welcome"))
        return render_template("member_invite.html", invite=invite, onboarding_fields=onboarding_fields,
                               existing_account=bool(existing_members), signed_member=signed_member)

    @app.post("/organization/invitations/<int:invitation_id>/revoke")
    @role_required("organization_admin")
    def organization_revoke_invitation(invitation_id: int):
        invite = MemberInvitation.query.filter_by(id=invitation_id,
                                                  organization_id=g.user.organization_id).first_or_404()
        if invite.status != "Pending":
            abort(409)
        invite.status = "Revoked"
        record_activity("member_invitation_revoked", "invitation", invite.id)
        db.session.commit()
        flash("Invitation revoked.", "success")
        return redirect(url_for("organization_plans"))

    @app.post("/organization/invitations/<int:invitation_id>/resend")
    @role_required("organization_admin")
    @limiter.limit("10 per hour")
    def organization_resend_member_invitation(invitation_id: int):
        invitation = MemberInvitation.query.filter_by(
            id=invitation_id, organization_id=g.user.organization_id, status="Pending").first_or_404()
        raw_token = secrets.token_urlsafe(32)
        invitation.token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        invitation.created_day = simulated_day()
        invitation.expires_day = simulated_day() + 7
        invitation.email_sent_at = None
        if deliver_member_invitation(invitation, raw_token):
            invitation.email_sent_at = utcnow()
            record_activity("member_invitation_resent", "invitation", invitation.id)
            db.session.commit()
            flash(f"A fresh activation email was sent to {invitation.email}.", "success")
        else:
            db.session.rollback()
            flash("Socio could not send the invitation. Check SMTP settings and try again.", "error")
        return redirect(url_for("organization_plans"))

    @app.post("/organization/members/<int:membership_id>/record-offline-payment")
    @role_required("organization_admin")
    def organization_record_offline_payment(membership_id: int):
        organization = db.session.get(Organization, g.user.organization_id)
        membership = Membership.query.filter_by(id=membership_id, organization_id=organization.id).first_or_404()
        status = membership_status(membership)
        if organization.status != "Approved" or status not in ("Pending", "Expired"):
            abort(400)
        if status == "Pending" and organization.membership_approval_required:
            application = MembershipApplication.query.filter_by(membership_id=membership.id).first()
            if not application or application.status != "Approved":
                flash("Approve this membership application before recording a join payment.", "error")
                return redirect(url_for("organization_plans"))
        if membership.cancel_at_period_end:
            flash("This member has scheduled cancellation. Ask them to request renewal first.", "error")
            return redirect(url_for("organization_plans"))
        if SimulatedPayment.query.filter_by(membership_id=membership.id, status="Pending").first():
            flash("A payment is already awaiting confirmation. Resolve it before recording an offline payment.", "error")
            return redirect(url_for("organization_plans"))
        plan = membership.plan
        if plan.archived:
            flash("This membership plan is archived. Restore or choose an available plan before recording payment.", "error")
            return redirect(url_for("organization_plans"))
        kind = "join" if status == "Pending" else "renewal"
        payment = SimulatedPayment(organization_id=organization.id, membership_id=membership.id,
                                   plan_id=plan.id, amount_minor=plan.price_minor, kind=kind,
                                   channel="offline", status="Pending", created_day=simulated_day())
        db.session.add(payment)
        db.session.flush()
        settle_simulated_payment(payment)
        flash(f"Offline payment recorded in the demo for {membership.full_name}. No funds were verified or moved.", "success")
        return redirect(url_for("organization_plans"))

    @app.post("/organization/plans/<int:plan_id>/archive")
    @role_required("organization_admin")
    def archive_plan(plan_id: int):
        plan = Plan.query.filter_by(id=plan_id, organization_id=g.user.organization_id).first()
        if not plan:
            abort(404)
        plan.archived = True
        db.session.commit()
        flash("Plan archived.", "success")
        return redirect(url_for("organization_plans"))

    @app.post("/organization/offers")
    @role_required("organization_admin")
    def organization_create_offer():
        organization = db.session.get(Organization, g.user.organization_id)
        if organization.status != "Approved":
            abort(403)
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        offer_type = request.form.get("offer_type", "")
        allowed_types = ("Welcome bonus", "Extra benefit", "Limited time", "Other")
        try:
            start_day = int(request.form.get("start_day", simulated_day()))
            end_raw = request.form.get("end_day", "").strip()
            end_day = int(end_raw) if end_raw else None
            selected_plan_id = int(request.form.get("plan_id", "")) if request.form.get("plan_id") else None
        except (TypeError, ValueError):
            flash("Choose valid offer dates and plan.", "error")
            return redirect(url_for("organization_plans"))
        plan = (Plan.query.filter_by(id=selected_plan_id, organization_id=organization.id,
                                     archived=False).first() if selected_plan_id else None)
        if (not title or len(title) > 100 or not description or len(description) > 500
                or offer_type not in allowed_types or start_day < simulated_day()
                or (end_day is not None and end_day < start_day)
                or (selected_plan_id is not None and plan is None)):
            flash("Enter a title and description, choose an offer type and available plan, and use valid simulated dates.", "error")
            return redirect(url_for("organization_plans"))
        offer = OrganizationOffer(organization_id=organization.id, plan_id=plan.id if plan else None,
                                  title=title, description=description, offer_type=offer_type,
                                  start_day=start_day, end_day=end_day)
        db.session.add(offer)
        db.session.flush()
        record_activity("organization_offer_created", "offer", offer.id,
                        f"{offer_type}; no checkout discount applied")
        offer_url = url_for("public_organization_offer", organization_id=organization.id,
                            offer_id=offer.id, _external=True)
        offer_body = f"{description}\n\nView offer: {offer_url}"
        recipients = queue_organization_update(organization, f"New offer: {title}",
                                               offer_body, plan_id=plan.id if plan else None)
        db.session.commit()
        delivered = email_organization_update(organization, f"New offer: {title}", offer_body, recipients)
        flash(f"Offer published. {len(recipients)} eligible member inbox(es) notified; {delivered} email(s) sent.", "success")
        return redirect(url_for("organization_plans"))

    @app.post("/organization/offers/<int:offer_id>/archive")
    @role_required("organization_admin")
    def organization_archive_offer(offer_id: int):
        offer = OrganizationOffer.query.filter_by(id=offer_id,
                                                   organization_id=g.user.organization_id).first_or_404()
        offer.archived = True
        record_activity("organization_offer_archived", "offer", offer.id)
        db.session.commit()
        flash("Offer removed from member pages.", "success")
        return redirect(url_for("organization_plans"))

    @app.route("/join/<int:organization_id>", methods=["GET", "POST"])
    @limiter.limit("10 per minute")
    def join_organization(organization_id: int):
        organization = db.session.get(Organization, organization_id)
        if not organization or not organization.memberships:
            abort(404)
        flash("Club membership is managed by the organization. Ask its team to add you and send your Socio invitation.", "info")
        return redirect(url_for("member_sign_in"))

    @app.get("/member")
    @app.get("/member/<int:selected_membership_id>")
    @role_required("member")
    def member_home(selected_membership_id: int | None = None):
        if selected_membership_id is not None:
            if not Membership.query.filter_by(id=selected_membership_id, user_id=g.user.id).first():
                abort(404)
        return redirect(url_for("member_directory"))

    @app.post("/member/<int:membership_id>/retry")
    @role_required("member")
    def member_retry_payment(membership_id: int):
        membership = Membership.query.filter_by(id=membership_id, user_id=g.user.id).first_or_404()
        status = membership_status(membership)
        if status not in ("Pending", "Expired"):
            abort(400)
        if status == "Pending" and membership.organization.membership_approval_required:
            application = MembershipApplication.query.filter_by(membership_id=membership.id).first()
            if not application or application.status != "Approved":
                flash("This organization must approve your membership application before payment.", "info")
                return redirect(url_for("member_home", selected_membership_id=membership.id))
        plan = db.session.get(Plan, membership.plan_id)
        if plan.archived:
            flash("This plan is no longer available. Contact the organization before renewing.", "error")
            return redirect(url_for("member_home", selected_membership_id=membership.id))
        kind = "renewal" if status == "Expired" else "join"
        payment = create_simulated_payment(membership, plan, kind, plan.price_minor)
        return redirect(url_for("payment_review", payment_id=payment.id))

    @app.post("/member/<int:membership_id>/upgrade/<int:plan_id>")
    @role_required("member")
    def member_upgrade(membership_id: int, plan_id: int):
        membership = Membership.query.filter_by(id=membership_id, user_id=g.user.id).first_or_404()
        if membership_status(membership) != "Active":
            abort(400)
        target = Plan.query.filter_by(id=plan_id, organization_id=membership.organization_id, archived=False).first_or_404()
        current = membership.plan
        if target.price_minor <= current.price_minor or target.billing_period != current.billing_period:
            abort(400)
        days_left = max(0, membership.end_day - simulated_day())
        amount = round((target.price_minor - current.price_minor) * days_left / period_days(current))
        payment = create_simulated_payment(membership, target, "upgrade", amount)
        return redirect(url_for("payment_review", payment_id=payment.id))

    @app.post("/member/<int:membership_id>/cancel")
    @role_required("member")
    def member_cancel(membership_id: int):
        membership = Membership.query.filter_by(id=membership_id, user_id=g.user.id).first_or_404()
        if membership_status(membership) not in ("Active", "Cancelled"):
            abort(400)
        membership.cancel_at_period_end = not membership.cancel_at_period_end
        if membership.cancel_at_period_end:
            membership.next_plan_id = None
        record_activity("membership_cancellation_changed", "membership", membership.id,
                        "Cancellation at period end" if membership.cancel_at_period_end else "Cancellation undone")
        db.session.commit()
        flash("Your membership will remain active through the paid period." if membership.cancel_at_period_end
              else "Membership cancellation undone.", "success")
        return redirect(url_for("member_home", selected_membership_id=membership.id))

    @app.post("/member/<int:membership_id>/downgrade/<int:plan_id>")
    @role_required("member")
    def member_schedule_downgrade(membership_id: int, plan_id: int):
        membership = Membership.query.filter_by(id=membership_id, user_id=g.user.id).first_or_404()
        if membership_status(membership) != "Active" or membership.cancel_at_period_end:
            abort(400)
        target = Plan.query.filter_by(id=plan_id, organization_id=membership.organization_id,
                                     archived=False).first_or_404()
        if target.billing_period != membership.plan.billing_period or target.price_minor >= membership.plan.price_minor:
            abort(400)
        if membership.next_plan_id == target.id:
            membership.next_plan_id = None
            details = "Scheduled downgrade removed"
            message = "Scheduled plan change removed. Your current plan will continue."
        else:
            membership.next_plan_id = target.id
            details = f"Downgrade to plan {target.id} at period end"
            message = f"{target.name} is scheduled to start at the end of your current paid period."
        record_activity("membership_downgrade_scheduled", "membership", membership.id, details)
        db.session.commit()
        flash(message, "success")
        return redirect(url_for("member_home", selected_membership_id=membership.id))

    @app.route("/payments/<int:payment_id>/review", methods=["GET", "POST"])
    @role_required("member")
    def payment_review(payment_id: int):
        payment = db.session.get(SimulatedPayment, payment_id)
        if not payment or payment.membership.user_id != g.user.id:
            abort(404)
        if payment.status != "Pending":
            return redirect(url_for("member_home", selected_membership_id=payment.membership_id))

        membership = payment.membership
        if (payment.kind == "join" and membership.organization.membership_approval_required):
            application = MembershipApplication.query.filter_by(membership_id=membership.id).first()
            if not application or application.status != "Approved":
                flash("This organization must approve your application before payment.", "info")
                return redirect(url_for("member_home", selected_membership_id=membership.id))
        if request.method == "POST":
            if request.form.get("confirm_terms") != "yes":
                flash("Confirm the simulated payment and cancellation terms before continuing.", "error")
                return redirect(url_for("payment_review", payment_id=payment.id))
            # Only this server-rendered review can authorize the matching pending payment page.
            session["reviewed_payment_id"] = payment.id
            return redirect(url_for("simulated_bank", payment_id=payment.id))

        current_day = simulated_day()
        if payment.kind == "upgrade":
            starts_when = "Immediately after successful confirmation"
            ending = f"Existing paid period end, simulated day {membership.end_day}"
        else:
            starts_when = f"After successful confirmation, estimated simulated day {current_day}"
            ending = f"Estimated simulated day {current_day + period_days(payment.plan)}"
        return render_template("payment_review.html", payment=payment, membership=membership,
                               starts_when=starts_when, ending=ending)

    @app.get("/simulated-bank/<int:payment_id>")
    @role_required("member")
    def simulated_bank(payment_id: int):
        payment = db.session.get(SimulatedPayment, payment_id)
        if not payment or payment.membership.user_id != g.user.id:
            abort(404)
        if payment.status != "Pending":
            return redirect(url_for("member_home", selected_membership_id=payment.membership_id))
        if payment.kind == "join" and payment.organization.membership_approval_required:
            application = MembershipApplication.query.filter_by(membership_id=payment.membership_id).first()
            if not application or application.status != "Approved":
                session.pop("reviewed_payment_id", None)
                flash("This organization must approve your application before payment.", "info")
                return redirect(url_for("member_home", selected_membership_id=payment.membership_id))
        if session.get("reviewed_payment_id") != payment.id:
            return redirect(url_for("payment_review", payment_id=payment.id))
        return render_template("simulated_bank.html", payment=payment)

    @app.post("/simulated-bank/<int:payment_id>/<action>")
    @role_required("member")
    def simulated_bank_action(payment_id: int, action: str):
        payment = db.session.get(SimulatedPayment, payment_id)
        if not payment or payment.membership.user_id != g.user.id:
            abort(404)
        if payment.status != "Pending":
            return redirect(url_for("member_home", selected_membership_id=payment.membership_id))
        if payment.kind == "join" and payment.organization.membership_approval_required:
            application = MembershipApplication.query.filter_by(membership_id=payment.membership_id).first()
            if not application or application.status != "Approved":
                session.pop("reviewed_payment_id", None)
                flash("This organization must approve your application before payment.", "info")
                return redirect(url_for("member_home", selected_membership_id=payment.membership_id))
        if session.get("reviewed_payment_id") != payment.id:
            return redirect(url_for("payment_review", payment_id=payment.id))
        if action == "success":
            session.pop("reviewed_payment_id", None)
            settle_simulated_payment(payment)
            flash("Simulated payment succeeded. No money was collected.", "success")
            return redirect(url_for("member_home", selected_membership_id=payment.membership_id))
        if action == "duplicate":
            session.pop("reviewed_payment_id", None)
            settle_simulated_payment(payment)
            settle_simulated_payment(payment)
            flash("Simulated payment succeeded. Duplicate confirmation was safely ignored.", "success")
            return redirect(url_for("member_home", selected_membership_id=payment.membership_id))
        if action == "late":
            session.pop("reviewed_payment_id", None)
            record_activity("simulated_payment_late_confirmation", "payment", payment.id,
                            "Awaiting platform admin confirmation")
            db.session.commit()
            flash("Payment is awaiting delayed confirmation. An admin can deliver it from the platform dashboard.", "info")
            return redirect(url_for("member_home", selected_membership_id=payment.membership_id))
        if action in ("failure", "abandon"):
            session.pop("reviewed_payment_id", None)
            payment.status = "Failed" if action == "failure" else "Abandoned"
            record_activity(f"simulated_payment_{payment.status.lower()}", "payment", payment.id,
                            "Simulated bank outcome")
            db.session.commit()
            flash("The simulated payment did not complete. You can retry from your membership page.", "error")
            return redirect(url_for("member_home", selected_membership_id=payment.membership_id))
        abort(404)

    @app.cli.command("create-admin")
    def create_admin():
        """Create the first platform administrator interactively."""
        email = click.prompt("Platform administrator email").strip().lower()
        password = click.prompt("Password (12+ characters)", hide_input=True, confirmation_prompt=True)
        if not valid_email(email) or len(password) < 12 or len(password) > 128 or User.query.filter_by(email=email, role="platform_admin").first():
            raise click.ClickException("Password length is invalid or that email already exists.")
        db.session.add(User(email=email, password_hash=password_hasher.hash(password), role="platform_admin",
                            email_verified=True))
        db.session.commit()
        click.echo("Platform administrator created.")

    @app.cli.command("seed-demo")
    def seed_demo():
        """Create sample organization data and an interactively configured local admin."""
        public_url = urlsplit(app.config.get("PUBLIC_BASE_URL", ""))
        if db.engine.dialect.name != "sqlite" or public_url.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise click.ClickException("Sample data can only be created in a local SQLite database.")

        demo_email = "chess@example.com"
        existing = Organization.query.filter_by(email=demo_email).first()
        if existing:
            if existing.name != "Colombo Chess Club":
                raise click.ClickException("The demo contact email is already used by a different organization.")
            click.echo("Colombo Chess Club sample data already exists; nothing was changed.")
            return

        admin_email = click.prompt("Local demo organization administrator email").strip().lower()
        password = click.prompt("Password for the demo organization admin (12+ characters)",
                                hide_input=True, confirmation_prompt=True)
        if not valid_email(admin_email) or len(password) < 12 or len(password) > 128:
            raise click.ClickException("Enter a valid email and a password between 12 and 128 characters.")

        organization = Organization(name="Colombo Chess Club", email=demo_email, status="Approved")
        db.session.add(organization)
        db.session.flush()
        administrator = User(email=admin_email, full_name="Club Administrator",
                             password_hash=password_hasher.hash(password), role="organization_admin",
                             organization_id=organization.id, email_verified=True)
        community = Plan(organization_id=organization.id, name="Community", price_minor=150000,
                         billing_period="monthly",
                         benefits="Weekly club nights\nMember discussion group\nCasual tournament entry")
        supporter = Plan(organization_id=organization.id, name="Club Supporter", price_minor=300000,
                         billing_period="monthly",
                         benefits="Everything in Community\nPriority tournament registration\nOne guest pass each month")
        annual = Plan(organization_id=organization.id, name="Annual Player", price_minor=1500000,
                      billing_period="yearly",
                      benefits="Everything in Community\nAnnual club championship entry\nTwo guest passes")
        db.session.add_all([administrator, community, supporter, annual])
        db.session.flush()
        db.session.add_all([
            OrganizationOffer(organization_id=organization.id, plan_id=community.id,
                              title="First club night is on us",
                              description="Meet the players and join one weekly club night as our guest.",
                              offer_type="Welcome bonus", start_day=simulated_day()),
            OrganizationOffer(organization_id=organization.id, plan_id=None,
                              title="Bring a friend",
                              description="Invite a friend to your first casual tournament.",
                              offer_type="Extra benefit", start_day=simulated_day()),
        ])
        record_activity("local_demo_seeded", "organization", organization.id,
                        "Sample organization, plans, offers, and organization admin created")
        db.session.commit()
        click.echo("Local sample data is ready: Colombo Chess Club, three plans, two offers, and its organization admin.")

    @app.cli.command("init-db")
    def init_db():
        """Create local tables for the initial prototype."""
        ensure_local_sqlite_schema()
        click.echo("Database tables created. Use migrations for later schema changes.")

    return app
