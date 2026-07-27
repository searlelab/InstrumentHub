from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String, unique=True)
    display_name: Mapped[str] = mapped_column(String)
    initials: Mapped[str] = mapped_column(String)
    email: Mapped[str] = mapped_column(String, default="")
    phone: Mapped[str] = mapped_column(String, default="")
    accent_color: Mapped[str] = mapped_column(String, default="#2563eb")
    last_calendar_instrument_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    role: Mapped[str] = mapped_column(String, default="user")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    password_hash: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String)


class SessionRecord(Base):
    __tablename__ = "sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String, unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    csrf_token: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String)
    last_seen_at: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str] = mapped_column(String)
    revoked_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    wizard_json: Mapped[str] = mapped_column(Text, default="{}")


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class PI(Base):
    __tablename__ = "pis"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    email: Mapped[str] = mapped_column(String, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class InstrumentClass(Base):
    __tablename__ = "instrument_classes"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)


class Instrument(Base):
    __tablename__ = "instruments"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    instrument_class_id: Mapped[Optional[int]] = mapped_column(ForeignKey("instrument_classes.id"), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    training_required: Mapped[bool] = mapped_column(Boolean, default=False)
    unavailable: Mapped[bool] = mapped_column(Boolean, default=False)


class BlockDefinition(Base):
    __tablename__ = "block_definitions"
    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    name: Mapped[str] = mapped_column(String)
    start_time: Mapped[str] = mapped_column(String)
    end_time: Mapped[str] = mapped_column(String)
    effective_from: Mapped[str] = mapped_column(String)
    effective_to: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer)


class ClassBlockDefinition(Base):
    __tablename__ = "class_block_definitions"
    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_class_id: Mapped[int] = mapped_column(ForeignKey("instrument_classes.id"))
    name: Mapped[str] = mapped_column(String)
    start_time: Mapped[str] = mapped_column(String)
    end_time: Mapped[str] = mapped_column(String)
    effective_from: Mapped[str] = mapped_column(String)
    effective_to: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    pi_id: Mapped[int] = mapped_column(ForeignKey("pis.id"))
    code: Mapped[str] = mapped_column(String, unique=True)
    name: Mapped[str] = mapped_column(String)
    max_consecutive_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Grant(Base):
    __tablename__ = "grants"
    id: Mapped[int] = mapped_column(primary_key=True)
    pi_id: Mapped[int] = mapped_column(ForeignKey("pis.id"))
    number: Mapped[str] = mapped_column(String, unique=True)
    name: Mapped[str] = mapped_column(String)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ProjectGrant(Base):
    __tablename__ = "project_grants"
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    grant_id: Mapped[int] = mapped_column(ForeignKey("grants.id"), primary_key=True)


class UserProject(Base):
    __tablename__ = "user_projects"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), primary_key=True)


class InstrumentTraining(Base):
    __tablename__ = "instrument_training"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"), primary_key=True)
    trained_at: Mapped[str] = mapped_column(String)
    revoked_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class Allotment(Base):
    __tablename__ = "allotments"
    id: Mapped[int] = mapped_column(primary_key=True)
    scope_type: Mapped[str] = mapped_column(String)
    scope_id: Mapped[int] = mapped_column(Integer)
    instrument_id: Mapped[Optional[int]] = mapped_column(ForeignKey("instruments.id"), nullable=True)
    period_start: Mapped[str] = mapped_column(String)
    period_end: Mapped[str] = mapped_column(String)
    minutes_allowed: Mapped[int] = mapped_column(Integer)


class CalendarEntry(Base):
    __tablename__ = "calendar_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    entry_type: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)


class ReservedSlot(Base):
    __tablename__ = "reserved_slots"
    id: Mapped[int] = mapped_column(primary_key=True)
    calendar_entry_id: Mapped[int] = mapped_column(ForeignKey("calendar_entries.id", ondelete="CASCADE"))
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    slot_start: Mapped[str] = mapped_column(String)
    slot_end: Mapped[str] = mapped_column(String)
    nominal_minutes: Mapped[int] = mapped_column(Integer)
    requested_slot_start: Mapped[str] = mapped_column(String)


class Booking(Base):
    __tablename__ = "bookings"
    id: Mapped[int] = mapped_column(primary_key=True)
    calendar_entry_id: Mapped[int] = mapped_column(ForeignKey("calendar_entries.id"), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    pi_id: Mapped[int] = mapped_column(ForeignKey("pis.id"))
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    grant_id: Mapped[Optional[int]] = mapped_column(ForeignKey("grants.id"), nullable=True)
    experiment_code: Mapped[str] = mapped_column(String)
    expected_minutes: Mapped[int] = mapped_column(Integer)
    sample_category: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text, default="")
    admin_notes: Mapped[str] = mapped_column(Text, default="")
    override_required: Mapped[bool] = mapped_column(Boolean, default=False)
    requested_start: Mapped[str] = mapped_column(String)
    requested_end: Mapped[str] = mapped_column(String)
    queue_order: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pre_pause_state: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    administratively_altered: Mapped[bool] = mapped_column(Boolean, default=False)
    approval_round: Mapped[int] = mapped_column(Integer, default=1)
    column_setup: Mapped[str] = mapped_column(Text, default="")
    libraries_present: Mapped[bool] = mapped_column(Boolean, default=False)
    gradient_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    reserved_minutes: Mapped[int] = mapped_column(Integer, default=0)


class Hold(Base):
    __tablename__ = "holds"
    id: Mapped[int] = mapped_column(primary_key=True)
    calendar_entry_id: Mapped[int] = mapped_column(ForeignKey("calendar_entries.id"), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    token_hash: Mapped[str] = mapped_column(String, unique=True)
    expires_at: Mapped[str] = mapped_column(String)


class Maintenance(Base):
    __tablename__ = "maintenance"
    id: Mapped[int] = mapped_column(primary_key=True)
    calendar_entry_id: Mapped[int] = mapped_column(ForeignKey("calendar_entries.id"), unique=True)
    kind: Mapped[str] = mapped_column(String)
    known_duration: Mapped[bool] = mapped_column(Boolean, default=True)
    reason: Mapped[str] = mapped_column(Text)
    restart_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class ApprovalDecision(Base):
    __tablename__ = "approval_decisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"))
    administrator_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    decision: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(Text)
    decided_at: Mapped[str] = mapped_column(String)
    approval_round: Mapped[int] = mapped_column(Integer, default=1)


class ScheduleChangeDraft(Base):
    __tablename__ = "schedule_change_drafts"
    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    kind: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[str] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[str] = mapped_column(String)
    applied_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String)
    entity_type: Mapped[str] = mapped_column(String)
    entity_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    before_json: Mapped[str] = mapped_column(Text, default="{}")
    after_json: Mapped[str] = mapped_column(Text, default="{}")
    request_id: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[str] = mapped_column(String)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    recipient_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    event_key: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    body: Mapped[str] = mapped_column(Text)
    link: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[str] = mapped_column(String)
    read_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"
    id: Mapped[int] = mapped_column(primary_key=True)
    notification_id: Mapped[int] = mapped_column(ForeignKey("notifications.id"), unique=True)
    state: Mapped[str] = mapped_column(String, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[str] = mapped_column(String)
    last_error: Mapped[str] = mapped_column(Text, default="")
    sent_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class MaintenanceTemplate(Base):
    __tablename__ = "maintenance_templates"
    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    title: Mapped[str] = mapped_column(String)
    interval_days: Mapped[int] = mapped_column(Integer)
    expected_minutes: Mapped[int] = mapped_column(Integer)
    assignee_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    next_due_date: Mapped[str] = mapped_column(String)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[str] = mapped_column(String)


class MaintenanceTask(Base):
    __tablename__ = "maintenance_tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("maintenance_templates.id"))
    due_date: Mapped[str] = mapped_column(String)
    assignee_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    state: Mapped[str] = mapped_column(String, default="open")
    completed_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    completion_reason: Mapped[str] = mapped_column(Text, default="")
    calendar_entry_id: Mapped[Optional[int]] = mapped_column(ForeignKey("calendar_entries.id"), nullable=True, unique=True)
    created_at: Mapped[str] = mapped_column(String)
