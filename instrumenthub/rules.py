from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Allotment, AuditEvent, Booking, CalendarEntry, Instrument, InstrumentTraining, PI, Project, ReservedSlot, User
from .scheduling import Slot, reservation_overlap
from .security import parse_utc
from .services import get_setting

Severity = Literal["allowed", "approval_required", "blocked"]


@dataclass(frozen=True)
class RuleResult:
    code: str
    severity: Severity
    message: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def booking_reference_requires_review(db: Session, entity_type: str, entity_id: int) -> bool:
    events = db.scalars(select(AuditEvent).where(
        AuditEvent.entity_type == entity_type,
        AuditEvent.entity_id == str(entity_id),
        AuditEvent.action.in_(["created", "validated"]),
    ).order_by(AuditEvent.id)).all()
    created_for_booking = any(
        event.action == "created"
        and json.loads(event.after_json or "{}").get("requires_review") is True
        for event in events
    )
    return created_for_booking and not any(event.action == "validated" for event in events)


def estimate_duration_minutes(injection_count: int, gradient_minutes: int) -> int:
    if injection_count <= 0 or gradient_minutes <= 0:
        raise ValueError("Injection count and gradient length must be positive.")
    sample_minutes = injection_count * gradient_minutes
    return sample_minutes + math.ceil(sample_minutes / 480) * 40


def _allotment_usage(db: Session, allotment: Allotment, *, timezone: str, states: tuple[str, ...] = ("confirmed", "active", "paused", "completed"), exclude_calendar_entry_id: int | None = None) -> int:
    query = select(ReservedSlot).join(CalendarEntry, CalendarEntry.id == ReservedSlot.calendar_entry_id).join(Booking, Booking.calendar_entry_id == CalendarEntry.id).where(
        CalendarEntry.state.in_(states),
    )
    if allotment.scope_type == "pi":
        query = query.where(Booking.pi_id == allotment.scope_id)
    else:
        query = query.where(Booking.project_id == allotment.scope_id)
    if allotment.instrument_id:
        query = query.where(CalendarEntry.instrument_id == allotment.instrument_id)
    if exclude_calendar_entry_id is not None:
        query = query.where(CalendarEntry.id != exclude_calendar_entry_id)
    zone = ZoneInfo(timezone)
    return sum(
        row.nominal_minutes
        for row in db.scalars(query)
        if allotment.period_start <= parse_utc(row.slot_start).astimezone(zone).date().isoformat() <= allotment.period_end
    )


def allotment_balances(
    db: Session, *, pi: PI, project: Project, instrument: Instrument, slots: list[Slot],
    exclude_calendar_entry_id: int | None = None,
) -> list[dict[str, int | str]]:
    """Return the current and projected balance for applicable allotments."""
    if not slots:
        return []
    zone = ZoneInfo(get_setting(db, "timezone", "UTC") or "UTC")
    slot_dates = [slot.start.astimezone(zone).date().isoformat() for slot in slots]
    first_date, last_date = min(slot_dates), max(slot_dates)
    balances = []
    allotments = db.scalars(select(Allotment).where(
        Allotment.period_start <= last_date, Allotment.period_end >= first_date,
        (Allotment.instrument_id.is_(None)) | (Allotment.instrument_id == instrument.id),
    ).order_by(Allotment.scope_type, Allotment.id)).all()
    for allotment in allotments:
        matches = (allotment.scope_type == "pi" and allotment.scope_id == pi.id) or (allotment.scope_type == "project" and allotment.scope_id == project.id)
        if not matches:
            continue
        used = _allotment_usage(db, allotment, timezone=zone.key, exclude_calendar_entry_id=exclude_calendar_entry_id)
        pending = _allotment_usage(db, allotment, timezone=zone.key, states=("tentative",), exclude_calendar_entry_id=exclude_calendar_entry_id)
        requested = sum(slot.nominal_minutes for slot, slot_date in zip(slots, slot_dates) if allotment.period_start <= slot_date <= allotment.period_end)
        balances.append({"scope_type": allotment.scope_type, "period_start": allotment.period_start, "period_end": allotment.period_end, "consumed_minutes": used, "pending_minutes": pending, "remaining_minutes": allotment.minutes_allowed - used, "remaining_after_request_minutes": allotment.minutes_allowed - used - requested})
    return balances


def evaluate_booking(
    db: Session, *, user: User | None, instrument: Instrument | None, pi: PI | None,
    project: Project | None, slots: list[Slot], override_required: bool = False,
    exclude_calendar_entry_id: int | None = None, hold_valid: bool | None = None,
) -> list[RuleResult]:
    results: list[RuleResult] = []
    required = [(user, "user"), (instrument, "instrument"), (pi, "PI"), (project, "project")]
    missing = [label for value, label in required if value is None]
    if missing or not slots:
        results.append(RuleResult("required_information_missing", "blocked", f"Required information is missing: {', '.join(missing or ['time blocks'])}."))
        return results
    if not user.active:
        results.append(RuleResult("user_inactive", "blocked", "The user account is inactive."))
    if not instrument.active or instrument.unavailable:
        results.append(RuleResult("instrument_unavailable", "blocked", "The instrument is unavailable."))
    if project.pi_id != pi.id or not project.active or not pi.active:
        results.append(RuleResult("project_pi_invalid", "blocked", "The selected PI and project are not an active valid pairing."))
    if instrument.training_required:
        trained = db.get(InstrumentTraining, {"user_id": user.id, "instrument_id": instrument.id})
        if not trained or trained.revoked_at:
            results.append(RuleResult("training_missing", "blocked", "Required instrument training is missing."))
    if hold_valid is False:
        results.append(RuleResult("hold_lost", "blocked", "The temporary hold has expired or is no longer valid."))
    if reservation_overlap(db, instrument_id=instrument.id, slots=slots, exclude_calendar_entry_id=exclude_calendar_entry_id):
        results.append(RuleResult("overlapping_occupancy", "blocked", "One or more selected instrument blocks are already protected or booked."))
    total_minutes = sum(slot.nominal_minutes for slot in slots)
    if total_minutes > 10080:
        results.append(RuleResult("over_one_week", "approval_required", "The request exceeds one week of scheduled time."))
    if project.max_consecutive_minutes and total_minutes > project.max_consecutive_minutes:
        results.append(RuleResult("project_consecutive_limit", "approval_required", "The request exceeds the project's consecutive-time limit."))
    zone = ZoneInfo(get_setting(db, "timezone", "UTC") or "UTC")
    slot_dates = [slot.start.astimezone(zone).date().isoformat() for slot in slots]
    first_date, last_date = min(slot_dates), max(slot_dates)
    allotments = db.scalars(select(Allotment).where(
        Allotment.period_start <= last_date, Allotment.period_end >= first_date,
        (Allotment.instrument_id.is_(None)) | (Allotment.instrument_id == instrument.id),
    ).order_by(Allotment.scope_type, Allotment.id)).all()
    for allotment in allotments:
        matches = (allotment.scope_type == "pi" and allotment.scope_id == pi.id) or (allotment.scope_type == "project" and allotment.scope_id == project.id)
        requested_minutes = sum(slot.nominal_minutes for slot, slot_date in zip(slots, slot_dates) if allotment.period_start <= slot_date <= allotment.period_end)
        if matches and _allotment_usage(db, allotment, timezone=zone.key, exclude_calendar_entry_id=exclude_calendar_entry_id) + requested_minutes > allotment.minutes_allowed:
            results.append(RuleResult(f"{allotment.scope_type}_allotment_exceeded", "approval_required", f"This request exceeds the active {allotment.scope_type} allotment."))
    if override_required:
        results.append(RuleResult("administrator_override", "approval_required", "An administrator override is required."))
    pending_references = [
        label for label, item in (("PI", pi), ("project", project))
        if booking_reference_requires_review(db, label.lower(), item.id)
    ]
    if pending_references:
        results.append(RuleResult(
            "booking_reference_review",
            "approval_required",
            f"The newly created {' and '.join(pending_references)} must be validated by an administrator.",
        ))
    if not results:
        results.append(RuleResult("allowed", "allowed", "The request satisfies all scheduling rules."))
    order = {"blocked": 0, "approval_required": 1, "allowed": 2}
    return sorted(results, key=lambda result: (order[result.severity], result.code))


def overall_result(results: list[RuleResult]) -> Severity:
    if any(item.severity == "blocked" for item in results):
        return "blocked"
    if any(item.severity == "approval_required" for item in results):
        return "approval_required"
    return "allowed"
