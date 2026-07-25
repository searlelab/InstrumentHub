from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import (
    ApprovalDecision, Booking, CalendarEntry, Instrument, Maintenance, PI, Project,
    ReservedSlot, ScheduleChangeDraft, User,
)
from .rules import booking_reference_requires_review, evaluate_booking, overall_result
from .scheduling import Slot, prune_expired_holds, reservation_overlap, reserve_slots, slots_for_range
from .security import parse_utc, utc_text
from .services import audit, get_setting, require_active_admin
from .notifications import enqueue_notification
from .booking import configured_slots

TERMINAL_STATES = {"completed", "canceled", "rejected"}
_UNSET = object()
TRANSITIONS = {
    "tentative": {"rejected", "canceled"},
    "confirmed": {"active", "paused", "canceled"},
    "active": {"paused", "completed"},
    "paused": {"active", "confirmed", "canceled"},
}


def _serialize_booking_write(db: Session) -> None:
    db.flush()
    if not db.in_transaction():
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    else:
        db.execute(text("UPDATE app_settings SET value=value WHERE 0"))


def _booking_parts(db: Session, booking_id: int):
    booking = db.scalar(select(Booking).where(Booking.id == booking_id).execution_options(populate_existing=True))
    if not booking:
        raise ValueError("Booking not found.")
    entry = db.scalar(select(CalendarEntry).where(CalendarEntry.id == booking.calendar_entry_id).execution_options(populate_existing=True))
    rows = db.scalars(select(ReservedSlot).where(ReservedSlot.calendar_entry_id == entry.id).order_by(ReservedSlot.slot_start)).all()
    return booking, entry, rows


def owner_can_manage_booking(booking: Booking, entry: CalendarEntry, rows: list[ReservedSlot], actor_id: int) -> bool:
    schedule_changed = bool(rows and (rows[0].slot_start != booking.requested_start or rows[-1].slot_end != booking.requested_end))
    return bool(
        booking.user_id == actor_id and entry.state in {"tentative", "confirmed"} and rows
        and parse_utc(rows[0].slot_start) > datetime.now(UTC)
        and not booking.administratively_altered and not schedule_changed
    )


def transition_booking(db: Session, *, booking_id: int, new_state: str, admin_id: int, reason: str, request_id: str = "") -> int:
    _serialize_booking_write(db)
    admin = require_active_admin(db, admin_id)
    if not reason.strip():
        raise ValueError("A reason is required for lifecycle changes.")
    booking, entry, _rows = _booking_parts(db, booking_id)
    if new_state not in TRANSITIONS.get(entry.state, set()):
        raise ValueError(f"Cannot transition a {entry.state} booking to {new_state}.")
    instrument = db.get(Instrument, entry.instrument_id)
    if booking.queue_order is not None and instrument and instrument.unavailable and new_state in {"active", "confirmed"}:
        raise ValueError(
            f"Booking {booking.id} ({entry.title}) is paused by an unresolved outage. "
            "Resume or recover the instrument outage before changing this booking's lifecycle state."
        )
    if new_state == "canceled":
        cancel_booking(db, booking_id=booking_id, actor=admin, reason=reason, request_id=request_id)
        return entry.id
    if new_state == "rejected":
        decide_booking(db, booking_id=booking_id, decision="rejected", admin_id=admin_id, reason=reason, request_id=request_id)
        return entry.id
    old = entry.state; entry.state = new_state; entry.updated_at = utc_text()
    audit(db, "state_changed", "booking", booking.id, actor_user_id=admin_id, reason=reason, before={"state": old}, after={"state": new_state}, request_id=request_id)
    return entry.id


def decide_booking(db: Session, *, booking_id: int, decision: str, admin_id: int, reason: str, request_id: str = "") -> None:
    _serialize_booking_write(db)
    require_active_admin(db, admin_id)
    if decision not in {"approved", "rejected"} or not reason.strip():
        raise ValueError("A decision and reason are required.")
    booking, entry, rows = _booking_parts(db, booking_id)
    if entry.state != "tentative":
        raise ValueError("Only tentative bookings can be decided.")
    if decision == "approved":
        slot_values = [Slot(parse_utc(row.slot_start), parse_utc(row.slot_end), row.nominal_minutes, "Reserved block") for row in rows]
        results = evaluate_booking(db, user=db.get(User, booking.user_id), instrument=db.get(Instrument, entry.instrument_id), pi=db.get(PI, booking.pi_id), project=db.get(Project, booking.project_id), slots=slot_values, override_required=booking.override_required, exclude_calendar_entry_id=entry.id)
        if overall_result(results) == "blocked":
            raise ValueError("The booking is now blocked and cannot be approved.")
        entry.state = "confirmed"
        for entity_type, entity_id in (("pi", booking.pi_id), ("project", booking.project_id)):
            if booking_reference_requires_review(db, entity_type, entity_id):
                audit(db, "validated", entity_type, entity_id, actor_user_id=admin_id, reason=reason, after={"booking_id": booking.id}, request_id=request_id)
    else:
        entry.state = "rejected"
        booking.reserved_minutes = sum(row.nominal_minutes for row in rows)
        for row in rows:
            db.delete(row)
    entry.updated_at = utc_text()
    db.add(ApprovalDecision(booking_id=booking.id, administrator_id=admin_id, decision=decision, reason=reason.strip(), decided_at=utc_text(), approval_round=booking.approval_round))
    audit(db, decision, "booking", booking.id, actor_user_id=admin_id, reason=reason, after={"state": entry.state}, request_id=request_id)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError("This approval round has already received a final decision.") from exc
    enqueue_notification(db, recipient_user_id=booking.user_id, event_key=f"booking:{booking.id}:decision:{booking.approval_round}", title="Booking approved" if decision == "approved" else "Booking rejected", body=f"{entry.title} was {decision}. {reason}", link=f"/entries/{entry.id}")


def cancel_booking(db: Session, *, booking_id: int, actor: User, reason: str = "User cancellation", request_id: str = "") -> None:
    actor_id = actor.id
    _serialize_booking_write(db)
    actor = db.get(User, actor_id)
    if not actor or not actor.active:
        raise ValueError("An active user is required.")
    booking, entry, rows = _booking_parts(db, booking_id)
    if entry.state not in {"tentative", "confirmed", "paused"}:
        raise ValueError(f"A {entry.state} booking cannot be canceled.")
    if actor.role != "admin":
        if not owner_can_manage_booking(booking, entry, rows, actor.id):
            raise ValueError("This booking is not eligible for owner cancellation.")
    elif not reason.strip():
        raise ValueError("An administrator reason is required.")
    old = entry.state; entry.state = "canceled"; entry.updated_at = utc_text(); booking.reserved_minutes = sum(row.nominal_minutes for row in rows)
    for row in rows:
        db.delete(row)
    audit(db, "canceled", "booking", booking.id, actor_user_id=actor.id, reason=reason, before={"state": old}, after={"state": "canceled"}, request_id=request_id)
    enqueue_notification(db, recipient_user_id=booking.user_id, event_key=f"booking:{booking.id}:canceled:{entry.updated_at}", title="Booking canceled", body=f"{entry.title} was canceled. {reason}", link=f"/entries/{entry.id}")


def edit_booking(
    db: Session, *, booking_id: int, actor: User, experiment_code: str, expected_minutes: int | None,
    sample_category: str, description: str, admin_notes: str, column_setup: str | object = _UNSET,
    libraries_present: bool | object = _UNSET, gradient_minutes: int | None | object = _UNSET, new_start: str | None = None,
    block_count: int | None = None, reason: str = "", request_id: str = "",
) -> None:
    actor_id = actor.id
    _serialize_booking_write(db)
    prune_expired_holds(db)
    actor = db.get(User, actor_id)
    if not actor or not actor.active:
        raise ValueError("An active user is required.")
    booking, entry, rows = _booking_parts(db, booking_id)
    owner_edit = actor.role != "admin"
    if owner_edit and not owner_can_manage_booking(booking, entry, rows, actor.id):
        raise ValueError("This booking is not eligible for owner editing.")
    if not owner_edit and not reason.strip():
        raise ValueError("An administrator reason is required.")
    duration_omitted = expected_minutes is None
    if expected_minutes is None:
        expected_minutes = booking.expected_minutes
    if not experiment_code.strip() or expected_minutes <= 0 or not sample_category.strip() or (gradient_minutes is not _UNSET and gradient_minutes is not None and gradient_minutes <= 0):
        raise ValueError("Experiment code and sample category are required.")
    if new_start and entry.state not in {"tentative", "confirmed"}:
        article = "An" if entry.state == "active" else "A"
        raise ValueError(
            f"{article} {entry.state} booking cannot be rescheduled. Use explicit lifecycle transitions before changing its reserved time."
        )
    old_state = entry.state
    current_count = len(rows)
    target_count = current_count if block_count is None else block_count
    if new_start and (target_count < 1 or target_count > 100):
        raise ValueError("A booking may select between 1 and 100 consecutive blocks.")
    if not new_start and target_count != current_count:
        raise ValueError("Choose a new block start when changing the number of reserved blocks.")
    before = {"experiment_code": booking.experiment_code, "expected_minutes": booking.expected_minutes, "sample_category": booking.sample_category, "column_setup": booking.column_setup, "libraries_present": booking.libraries_present, "gradient_minutes": booking.gradient_minutes, "state": entry.state, "start": rows[0].slot_start if rows else None, "end": rows[-1].slot_end if rows else None, "block_count": current_count}
    updated_rows = rows
    if new_start:
        slots = configured_slots(db, entry.instrument_id, new_start, target_count)
        if owner_edit and slots[0].start.astimezone(UTC) <= datetime.now(UTC):
            raise ValueError("Owners cannot move a booking into the past.")
        if reservation_overlap(db, instrument_id=entry.instrument_id, slots=slots, exclude_calendar_entry_id=entry.id):
            raise ValueError("One or more selected instrument blocks are no longer available.")
        for row in rows:
            db.delete(row)
        db.flush(); reserve_slots(db, entry, slots)
        results = evaluate_booking(db, user=db.get(User, booking.user_id), instrument=db.get(Instrument, entry.instrument_id), pi=db.get(PI, booking.pi_id), project=db.get(Project, booking.project_id), slots=slots, override_required=booking.override_required, exclude_calendar_entry_id=entry.id)
        if overall_result(results) == "blocked":
            raise ValueError("The edited schedule is blocked by current policy.")
        entry.state = "tentative" if overall_result(results) == "approval_required" else "confirmed"
        if entry.state == "tentative" and old_state != "tentative" and db.scalar(select(ApprovalDecision.id).where(ApprovalDecision.booking_id == booking.id, ApprovalDecision.approval_round == booking.approval_round).limit(1)):
            booking.approval_round += 1
        booking.requested_start, booking.requested_end = slots[0].start_text, slots[-1].end_text
        if not owner_edit:
            booking.administratively_altered = True
        updated_rows = slots
        if duration_omitted:
            expected_minutes = sum(slot.nominal_minutes for slot in slots)
    booking.experiment_code, booking.expected_minutes = experiment_code.strip(), expected_minutes
    booking.sample_category, booking.description = sample_category.strip(), description.strip()
    if column_setup is not _UNSET:
        booking.column_setup = column_setup.strip()
    if libraries_present is not _UNSET:
        booking.libraries_present = libraries_present
    if gradient_minutes is not _UNSET:
        booking.gradient_minutes = gradient_minutes
    if not owner_edit:
        booking.admin_notes = admin_notes.strip()
    entry.title, entry.updated_at = booking.experiment_code, utc_text()
    audit(db, "updated", "booking", booking.id, actor_user_id=actor.id, reason=reason or "Owner edit", before=before, after={"experiment_code": booking.experiment_code, "expected_minutes": expected_minutes, "sample_category": booking.sample_category, "column_setup": booking.column_setup, "libraries_present": booking.libraries_present, "gradient_minutes": booking.gradient_minutes, "state": entry.state, "start": updated_rows[0].start_text if new_start else rows[0].slot_start if rows else None, "end": updated_rows[-1].end_text if new_start else rows[-1].slot_end if rows else None, "block_count": len(updated_rows)}, request_id=request_id)


def _affected_slots(db: Session, instrument_id: int, start_text: str) -> list[ReservedSlot]:
    return db.scalars(select(ReservedSlot).join(CalendarEntry, CalendarEntry.id == ReservedSlot.calendar_entry_id).where(
        ReservedSlot.instrument_id == instrument_id, ReservedSlot.slot_end > start_text,
        CalendarEntry.entry_type.in_(["booking", "maintenance"]),
        ~CalendarEntry.state.in_(TERMINAL_STATES),
    ).order_by(ReservedSlot.slot_start, ReservedSlot.id)).all()


def _configured_slots_from(db: Session, *, instrument_id: int, start_date: date, required_count: int, timezone: str) -> list[Slot]:
    """Return enough configured blocks without imposing a horizon on valid bookings."""
    slots: list[Slot] = []
    current = start_date
    for _ in range(10):
        page_end = current + timedelta(days=400)
        page = slots_for_range(db, instrument_id, current, page_end, timezone)
        slots.extend(page)
        if len(slots) >= required_count:
            return slots
        current = page_end + timedelta(days=1)
    raise ValueError("There are not enough configured blocks within the next ten years.")


def _shift_by_block_span(db: Session, *, instrument_id: int, outage_slots: list[Slot], affected: list[ReservedSlot], timezone: str) -> list[dict[str, object]]:
    """Map occupied slots onto the same number of subsequent configured blocks."""
    if not affected:
        return []
    zone = ZoneInfo(timezone)
    first_date = outage_slots[0].start.astimezone(zone).date()
    last_date = max(parse_utc(row.slot_start).astimezone(zone).date() for row in affected)
    existing = slots_for_range(db, instrument_id, first_date, min(last_date, first_date + timedelta(days=400)), timezone)
    if not existing:
        raise ValueError("The outage start date has no configured instrument blocks to shift.")
    while existing[-1].start.astimezone(zone).date() < last_date:
        page_start = existing[-1].start.astimezone(zone).date() + timedelta(days=1)
        page = slots_for_range(db, instrument_id, page_start, min(last_date, page_start + timedelta(days=400)), timezone)
        if not page:
            raise ValueError("The affected schedule crosses a period with no configured instrument blocks to shift.")
        existing.extend(page)
    existing_indexes = {slot.start_text: index for index, slot in enumerate(existing)}
    required_count = max(existing_indexes.get(row.slot_start, -1) for row in affected) + len(outage_slots) + 1
    candidates = _configured_slots_from(db, instrument_id=instrument_id, start_date=first_date, required_count=required_count, timezone=timezone)
    by_start = {slot.start_text: index for index, slot in enumerate(candidates)}
    span = len(outage_slots)
    items: list[dict[str, object]] = []
    for row in affected:
        try:
            target = candidates[by_start[row.slot_start] + span]
        except (KeyError, IndexError) as exc:
            raise ValueError("An affected schedule entry is not aligned to configured instrument blocks.") from exc
        if target.nominal_minutes != row.nominal_minutes:
            raise ValueError("The changed block definition cannot preserve an affected entry's duration. Adjust the block version or outage dates and regenerate the preview.")
        items.append({
            "slot_id": row.id, "entry_id": row.calendar_entry_id,
            "old_start": row.slot_start, "old_end": row.slot_end,
            "new_start": target.start_text, "new_end": target.end_text,
            "source_nominal_minutes": row.nominal_minutes,
            "target_nominal_minutes": target.nominal_minutes,
            "nominal_minutes": row.nominal_minutes,
        })
    return items


def preview_outage(db: Session, *, instrument_id: int, start_date: date, end_date: date | None, reason: str, admin_id: int, request_id: str = "") -> ScheduleChangeDraft:
    require_active_admin(db, admin_id)
    if not reason.strip():
        raise ValueError("An outage reason is required.")
    prune_expired_holds(db)
    instrument = db.get(Instrument, instrument_id)
    if not instrument:
        raise ValueError("Instrument not found.")
    if end_date is None and instrument.unavailable:
        raise ValueError("An unknown outage is already unresolved for this instrument.")
    timezone = get_setting(db, "timezone", "UTC") or "UTC"
    outage_slots = slots_for_range(db, instrument_id, start_date, end_date or start_date, timezone)
    terminal_starts = db.scalars(select(ReservedSlot.slot_start).join(CalendarEntry, CalendarEntry.id == ReservedSlot.calendar_entry_id).where(
        ReservedSlot.instrument_id == instrument_id,
        ReservedSlot.slot_start.in_([slot.start_text for slot in outage_slots]),
        CalendarEntry.state.in_(TERMINAL_STATES),
    )).all()
    if terminal_starts:
        outage_slots = [slot for slot in outage_slots if slot.start_text > max(terminal_starts)]
    if not outage_slots:
        raise ValueError("The outage has no remaining configured slots after completed work.")
    affected = _affected_slots(db, instrument_id, outage_slots[0].start_text)
    items = _shift_by_block_span(db, instrument_id=instrument_id, outage_slots=outage_slots, affected=affected, timezone=timezone) if end_date else [
        {"slot_id": row.id, "entry_id": row.calendar_entry_id, "old_start": row.slot_start, "old_end": row.slot_end, "new_start": None, "new_end": None, "source_nominal_minutes": row.nominal_minutes, "target_nominal_minutes": None, "nominal_minutes": row.nominal_minutes}
        for row in affected
    ]
    moving_ids = {item["slot_id"] for item in items}
    if reservation_overlap(db, instrument_id=instrument_id, slots=outage_slots, exclude_slot_ids=moving_ids):
        raise ValueError("The outage period conflicts with an existing fixed calendar entry.")
    if end_date:
        shifted_slots = [Slot(parse_utc(item["new_start"]), parse_utc(item["new_end"]), item["nominal_minutes"], "Shifted block") for item in items]
        if reservation_overlap(db, instrument_id=instrument_id, slots=shifted_slots, exclude_slot_ids=moving_ids):
            raise ValueError("The proposed shifted schedule conflicts with a fixed calendar entry.")
    payload = {"instrument_id": instrument_id, "outage_slots": [{"start": s.start_text, "end": s.end_text, "nominal_minutes": s.nominal_minutes} for s in outage_slots], "items": items}
    draft = ScheduleChangeDraft(instrument_id=instrument_id, kind="known_outage" if end_date else "unknown_outage", status="draft", reason=reason.strip(), payload_json=json.dumps(payload, sort_keys=True), created_by=admin_id, created_at=utc_text())
    db.add(draft); db.flush()
    audit(db, "previewed", "schedule_change", draft.id, actor_user_id=admin_id, reason=reason, after={"kind": draft.kind, "affected_slots": len(items)}, request_id=request_id)
    return draft


def _unresolved_unknown_outages(db: Session, instrument_id: int) -> list[ScheduleChangeDraft]:
    return db.scalars(select(ScheduleChangeDraft).where(
        ScheduleChangeDraft.instrument_id == instrument_id,
        ScheduleChangeDraft.kind == "unknown_outage",
        ScheduleChangeDraft.status == "applied",
    ).order_by(ScheduleChangeDraft.applied_at, ScheduleChangeDraft.id)).all()


def _source_rows_for_draft(db: Session, *, instrument_id: int, items: list[dict[str, object]]) -> dict[int, ReservedSlot]:
    source_ids = {int(item["slot_id"]) for item in items}
    rows = {row.id: row for row in db.scalars(select(ReservedSlot).where(ReservedSlot.id.in_(source_ids))).all()} if source_ids else {}
    for item in items:
        row = rows.get(int(item["slot_id"]))
        if not row or row.calendar_entry_id != item["entry_id"] or row.instrument_id != instrument_id or row.slot_start != item["old_start"] or row.slot_end != item["old_end"] or row.nominal_minutes != item.get("source_nominal_minutes", item["nominal_minutes"]):
            raise ValueError("The schedule preview is stale because an affected entry changed. Regenerate the preview.")
    return rows


def _reconcile_resume_items(db: Session, *, instrument_id: int, items: list[dict[str, object]]) -> list[dict[str, object]]:
    reconciled = []
    for item in items:
        entry = db.get(CalendarEntry, item["entry_id"])
        if not entry or entry.state in TERMINAL_STATES:
            booking = db.scalar(select(Booking).where(Booking.calendar_entry_id == item["entry_id"]))
            if booking:
                booking.queue_order = None; booking.pre_pause_state = None
            continue
        expected_state = "paused" if entry.entry_type == "booking" else "maintenance"
        has_slots = db.scalar(select(ReservedSlot.id).where(ReservedSlot.calendar_entry_id == entry.id, ReservedSlot.slot_start >= item["old_start"]).limit(1))
        if entry.instrument_id != instrument_id or entry.state != expected_state or has_slots:
            raise ValueError(
                f"Schedule entry {entry.id} ({entry.title}) is in state '{entry.state}' or has regained reserved slots; "
                f"the unresolved outage requires state '{expected_state}' with no slots. Correct that entry before regenerating the resume preview."
            )
        reconciled.append(item)
    return reconciled


def preview_resume(db: Session, *, instrument_id: int, restart_date: date, admin_id: int, reason: str, request_id: str = "") -> ScheduleChangeDraft:
    require_active_admin(db, admin_id)
    if not reason.strip():
        raise ValueError("A resume reason is required.")
    instrument = db.get(Instrument, instrument_id)
    if not instrument or not instrument.unavailable:
        raise ValueError("This instrument does not have an unresolved outage to resume.")
    sources = _unresolved_unknown_outages(db, instrument_id)
    if not sources:
        raise ValueError("No unresolved outage exists for this instrument.")
    timezone = get_setting(db, "timezone", "UTC") or "UTC"
    restart_slots = slots_for_range(db, instrument_id, restart_date, restart_date, timezone)
    if not restart_slots:
        raise ValueError("The restart date has no configured instrument blocks.")
    restart = restart_slots[0].start
    # Reconstruct by configured block position, rather than a wall-clock delta,
    # so partial-day definitions and daylight-saving transitions stay aligned.
    zone = ZoneInfo(timezone)
    items = []
    seen_slot_ids: set[int] = set()
    for source in sources:
        original = json.loads(source.payload_json)
        origin = parse_utc(original["outage_slots"][0]["start"])
        old_items = _reconcile_resume_items(db, instrument_id=instrument_id, items=original["items"])
        old_last = max((parse_utc(item["old_start"]) for item in old_items), default=origin)
        old_start_date = origin.astimezone(zone).date()
        old_end_date = old_last.astimezone(zone).date()
        old_candidates = _configured_slots_from(db, instrument_id=instrument_id, start_date=old_start_date, required_count=1, timezone=timezone)
        while old_candidates[-1].start.astimezone(zone).date() < old_end_date:
            old_candidates = _configured_slots_from(db, instrument_id=instrument_id, start_date=old_start_date, required_count=len(old_candidates) + 1, timezone=timezone)
        old_indexes = {slot.start_text: index for index, slot in enumerate(old_candidates)}
        origin_index = old_indexes.get(utc_text(origin))
        if origin_index is None:
            raise ValueError("The original outage is not aligned to configured instrument blocks.")
        max_offset = max((old_indexes.get(item["old_start"], -1) - origin_index for item in old_items), default=0)
        new_candidates = _configured_slots_from(db, instrument_id=instrument_id, start_date=restart_date, required_count=max_offset + 1, timezone=timezone)
        for item in old_items:
            if item["slot_id"] in seen_slot_ids:
                continue
            old_index = old_indexes.get(item["old_start"])
            if old_index is None:
                raise ValueError("An affected schedule entry is not aligned to configured instrument blocks.")
            target = new_candidates[old_index - origin_index]
            source_minutes = item.get("source_nominal_minutes", item["nominal_minutes"])
            if target.nominal_minutes != source_minutes:
                raise ValueError("The changed block definition cannot preserve an affected entry's duration. Adjust the block version or restart date and regenerate the preview.")
            items.append({**item, "new_start": target.start_text, "new_end": target.end_text, "source_nominal_minutes": source_minutes, "target_nominal_minutes": target.nominal_minutes, "nominal_minutes": source_minutes})
            seen_slot_ids.add(item["slot_id"])
    payload = {"instrument_id": instrument_id, "source_draft_ids": [item.id for item in sources], "restart_at": utc_text(restart.astimezone(UTC)), "items": items}
    proposed = [Slot(parse_utc(item["new_start"]), parse_utc(item["new_end"]), item["nominal_minutes"], "Reconstructed block") for item in items]
    if reservation_overlap(db, instrument_id=instrument_id, slots=proposed):
        raise ValueError("The reconstructed schedule conflicts with an existing calendar entry.")
    draft = ScheduleChangeDraft(instrument_id=instrument_id, kind="resume", status="draft", reason=reason.strip(), payload_json=json.dumps(payload, sort_keys=True), created_by=admin_id, created_at=utc_text())
    db.add(draft); db.flush()
    audit(db, "previewed", "schedule_change", draft.id, actor_user_id=admin_id, reason=reason, after={"kind": "resume", "affected_slots": len(items)}, request_id=request_id)
    return draft


def recover_unknown_outage(db: Session, *, instrument_id: int, admin_id: int, reason: str, request_id: str = "") -> int:
    """Last-resort recovery for an unreconstructable outage; affected bookings are canceled."""
    _serialize_booking_write(db)
    require_active_admin(db, admin_id)
    if not reason.strip():
        raise ValueError("A recovery reason is required.")
    instrument = db.get(Instrument, instrument_id)
    sources = _unresolved_unknown_outages(db, instrument_id)
    if not instrument or not instrument.unavailable or not sources:
        raise ValueError("This instrument does not have an unresolved outage to recover.")
    entry_ids = {item["entry_id"] for source in sources for item in json.loads(source.payload_json)["items"]}
    canceled = 0
    for entry_id in entry_ids:
        entry = db.get(CalendarEntry, entry_id)
        if entry and entry.entry_type == "booking" and entry.state == "paused":
            entry.state = "canceled"; entry.updated_at = utc_text(); canceled += 1
            booking = db.scalar(select(Booking).where(Booking.calendar_entry_id == entry_id))
            if booking:
                booking.queue_order = None; booking.pre_pause_state = None
            audit(db, "canceled", "booking", booking.id if booking else entry_id, actor_user_id=admin_id, reason=reason, before={"state": "paused"}, after={"state": "canceled"}, request_id=request_id)
        elif entry and entry.entry_type == "maintenance" and entry.state not in TERMINAL_STATES:
            old_state = entry.state; entry.state = "canceled"; entry.updated_at = utc_text()
            audit(db, "canceled", "maintenance", entry_id, actor_user_id=admin_id, reason=reason, before={"state": old_state}, after={"state": "canceled"}, request_id=request_id)
    for source in sources:
        source.status = "discarded"
        audit(db, "recovered", "schedule_change", source.id, actor_user_id=admin_id, reason=reason, after={"canceled_bookings": canceled}, request_id=request_id)
    outage_entries = db.scalars(
        select(CalendarEntry)
        .join(Maintenance, Maintenance.calendar_entry_id == CalendarEntry.id)
        .where(
            CalendarEntry.instrument_id == instrument_id,
            CalendarEntry.entry_type == "outage",
            CalendarEntry.state == "outage",
            Maintenance.kind == "outage",
            Maintenance.known_duration.is_(False),
            Maintenance.restart_at.is_(None),
        )
        .order_by(CalendarEntry.id)
    ).all()
    for outage_entry in outage_entries:
        slot_count = len(db.scalars(select(ReservedSlot).where(ReservedSlot.calendar_entry_id == outage_entry.id)).all())
        outage_entry.state = "canceled"
        outage_entry.updated_at = utc_text()
        db.execute(delete(ReservedSlot).where(ReservedSlot.calendar_entry_id == outage_entry.id))
        audit(
            db,
            "canceled",
            "outage",
            outage_entry.id,
            actor_user_id=admin_id,
            reason=reason,
            before={"state": "outage", "reserved_slots": slot_count},
            after={"state": "canceled", "reserved_slots": 0},
            request_id=request_id,
        )
    instrument.unavailable = False
    return canceled


def apply_schedule_draft(db: Session, *, draft_id: int, admin_id: int, request_id: str = "") -> None:
    if not db.in_transaction():
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    require_active_admin(db, admin_id)
    prune_expired_holds(db)
    draft = db.get(ScheduleChangeDraft, draft_id)
    if not draft or draft.status != "draft":
        raise ValueError("Schedule preview is missing or already applied.")
    payload = json.loads(draft.payload_json); instrument = db.get(Instrument, draft.instrument_id)
    if draft.kind == "resume":
        payload["items"] = _reconcile_resume_items(db, instrument_id=draft.instrument_id, items=payload["items"])
    audit_items = []
    for item in payload["items"]:
        booking = db.scalar(select(Booking).where(Booking.calendar_entry_id == item["entry_id"]))
        audit_items.append({
            "entry_id": item["entry_id"], "old_start": item["old_start"], "old_end": item["old_end"],
            "new_start": item["new_start"], "new_end": item["new_end"], "nominal_minutes": item["nominal_minutes"],
            "requested_start": booking.requested_start if booking else None,
            "requested_end": booking.requested_end if booking else None,
        })
    def notify_affected(kind: str, title: str) -> None:
        for entry_id in sorted({item["entry_id"] for item in payload["items"]}):
            booking = db.scalar(select(Booking).where(Booking.calendar_entry_id == entry_id))
            if booking:
                enqueue_notification(db, recipient_user_id=booking.user_id, event_key=f"schedule:{draft.id}:{kind}:{booking.id}", title=title, body=f"{db.get(CalendarEntry, entry_id).title}: {draft.reason}", link=f"/entries/{entry_id}")
    if draft.kind == "known_outage":
        moving_ids = {item["slot_id"] for item in payload["items"]}
        moving_rows = _source_rows_for_draft(db, instrument_id=draft.instrument_id, items=payload["items"])
        protected_slots = [Slot(parse_utc(item["new_start"]), parse_utc(item["new_end"]), item["nominal_minutes"], "Shifted block") for item in payload["items"]]
        protected_slots.extend(Slot(parse_utc(item["start"]), parse_utc(item["end"]), item["nominal_minutes"], "Outage") for item in payload["outage_slots"])
        if reservation_overlap(db, instrument_id=draft.instrument_id, slots=protected_slots, exclude_slot_ids=moving_ids):
            raise ValueError("The approved schedule preview now conflicts with a protected instrument block.")
        for item in sorted(payload["items"], key=lambda value: value["old_start"], reverse=True):
            row = moving_rows[item["slot_id"]]
            row.slot_start, row.slot_end = item["new_start"], item["new_end"]
            row.nominal_minutes = item["nominal_minutes"]
            db.flush()
        for entry_id in {item["entry_id"] for item in payload["items"]}:
            affected_entry = db.get(CalendarEntry, entry_id)
            if affected_entry and affected_entry.entry_type == "booking":
                booking = db.scalar(select(Booking).where(Booking.calendar_entry_id == entry_id))
                booking.administratively_altered = True
                if affected_entry.state == "active":
                    booking.pre_pause_state = "active"; affected_entry.state = "paused"
    elif draft.kind == "unknown_outage":
        if instrument.unavailable or _unresolved_unknown_outages(db, draft.instrument_id):
            raise ValueError("An unknown outage is already unresolved for this instrument.")
        source_rows = _source_rows_for_draft(db, instrument_id=draft.instrument_id, items=payload["items"])
        entry_ids = set()
        for item in payload["items"]:
            row = source_rows[int(item["slot_id"])]
            entry_ids.add(row.calendar_entry_id); db.delete(row)
        ordered_entries = sorted(
            entry_ids,
            key=lambda entry_id: (
                db.scalar(select(Booking.requested_start).where(Booking.calendar_entry_id == entry_id)) or "",
                entry_id,
            ),
        )
        for queue_order, entry_id in enumerate(ordered_entries, start=1):
            entry = db.get(CalendarEntry, entry_id)
            if entry and entry.entry_type == "booking":
                booking = db.scalar(select(Booking).where(Booking.calendar_entry_id == entry_id)); booking.pre_pause_state = entry.state; booking.queue_order = queue_order; entry.state = "paused"
        instrument.unavailable = True
        db.flush()
    else:
        source_ids = payload.get("source_draft_ids", [])
        sources = [db.get(ScheduleChangeDraft, source_id) for source_id in source_ids]
        if not instrument.unavailable or not sources or any(not source or source.kind != "unknown_outage" or source.status != "applied" for source in sources):
            raise ValueError("The source outage is no longer eligible for resumption.")
        proposed_slots = [Slot(parse_utc(item["new_start"]), parse_utc(item["new_end"]), item["nominal_minutes"], "Reconstructed block") for item in payload["items"]]
        if reservation_overlap(db, instrument_id=draft.instrument_id, slots=proposed_slots):
            raise ValueError("The schedule preview is stale because the reconstructed blocks are now occupied. Regenerate the preview.")
        for item in payload["items"]:
            entry_id = item["entry_id"]
            entry = db.get(CalendarEntry, entry_id)
            if not entry or entry.state in TERMINAL_STATES:
                raise ValueError("The schedule preview is stale because an affected entry changed. Regenerate the preview.")
        for item in payload["items"]:
            entry_id = item["entry_id"]
            db.add(ReservedSlot(calendar_entry_id=entry_id, instrument_id=draft.instrument_id, slot_start=item["new_start"], slot_end=item["new_end"], nominal_minutes=item["nominal_minutes"], requested_slot_start=item["old_start"]))
        for entry_id in {item["entry_id"] for item in payload["items"]}:
            entry = db.get(CalendarEntry, entry_id)
            if entry and entry.entry_type == "booking":
                booking = db.scalar(select(Booking).where(Booking.calendar_entry_id == entry_id))
                entry.state = booking.pre_pause_state if booking and booking.pre_pause_state in {"tentative", "confirmed"} else "confirmed"
                booking.queue_order = None; booking.administratively_altered = True
        instrument.unavailable = False
        for source in sources:
            source.status = "discarded"
        maintenance = db.scalar(select(Maintenance).join(CalendarEntry, CalendarEntry.id == Maintenance.calendar_entry_id).where(CalendarEntry.instrument_id == draft.instrument_id, Maintenance.kind == "outage", Maintenance.known_duration.is_(False)).order_by(Maintenance.id.desc()))
        if maintenance: maintenance.restart_at = payload["restart_at"]
        draft.status = "applied"; draft.applied_at = utc_text(); audit(db, "applied", "schedule_change", draft.id, actor_user_id=admin_id, reason=draft.reason, after={"kind": draft.kind, "items": audit_items}, request_id=request_id); notify_affected("resume", "Instrument schedule resumed"); return
    now = utc_text(); entry = CalendarEntry(instrument_id=draft.instrument_id, entry_type="outage", state="outage", title="Instrument outage", created_by=admin_id, created_at=now, updated_at=now)
    db.add(entry); db.flush(); db.add(Maintenance(calendar_entry_id=entry.id, kind="outage", known_duration=draft.kind == "known_outage", reason=draft.reason))
    outage_slots = [Slot(parse_utc(item["start"]), parse_utc(item["end"]), item["nominal_minutes"], "Outage") for item in payload["outage_slots"]]
    reserve_slots(db, entry, outage_slots)
    draft.status = "applied"; draft.applied_at = utc_text()
    audit(db, "applied", "schedule_change", draft.id, actor_user_id=admin_id, reason=draft.reason, after={"kind": draft.kind, "outage_slots": payload["outage_slots"], "items": audit_items}, request_id=request_id)
    notify_affected(draft.kind, "Instrument schedule changed")
