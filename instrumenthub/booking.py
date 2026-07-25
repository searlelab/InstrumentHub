from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import Booking, CalendarEntry, Grant, Hold, Instrument, PI, Project, ProjectGrant, ReservedSlot, SessionRecord, User, UserProject
from .rules import RuleResult, estimate_duration_minutes, evaluate_booking, overall_result
from .scheduling import Slot, prune_expired_holds, prune_expired_tentatives, reservation_overlap, reserve_slots, slots_for_range
from .security import hash_token, parse_utc, utc_text, utcnow
from .services import audit, get_setting, require_active_admin
from .notifications import enqueue_notification


def create_booking_pi(db: Session, *, user: User, name: str, email: str = "", request_id: str = "") -> PI:
    user = db.get(User, user.id)
    normalized_name = name.strip()
    if not user or not user.active:
        raise ValueError("An active user is required.")
    if not normalized_name:
        raise ValueError("PI name is required.")
    if db.scalar(select(PI.id).where(func.lower(PI.name) == normalized_name.lower())):
        raise ValueError("A PI with that name already exists. Select the existing PI instead.")
    pi = PI(name=normalized_name, email=email.strip(), active=True)
    db.add(pi)
    try:
        db.flush()
    except IntegrityError as exc:
        raise ValueError("A PI with that name already exists. Select the existing PI instead.") from exc
    audit(db, "created", "pi", pi.id, actor_user_id=user.id, after={"name": pi.name, "source": "booking", "requires_review": user.role != "admin"}, request_id=request_id)
    return pi


def create_booking_project(
    db: Session, *, user: User, pi_id: int, code: str, name: str, request_id: str = "",
) -> Project:
    user = db.get(User, user.id)
    pi = db.get(PI, pi_id)
    normalized_code, normalized_name = code.strip(), name.strip()
    if not user or not user.active:
        raise ValueError("An active user is required.")
    if not pi or not pi.active:
        raise ValueError("Select an active PI before creating a project.")
    if not normalized_code or not normalized_name:
        raise ValueError("Project code and name are required.")
    if db.scalar(select(Project.id).where(func.lower(Project.code) == normalized_code.lower())):
        raise ValueError("A project with that code already exists. Select the existing project instead.")
    project = Project(pi_id=pi.id, code=normalized_code, name=normalized_name, max_consecutive_minutes=None, active=True)
    db.add(project)
    try:
        db.flush()
    except IntegrityError as exc:
        raise ValueError("A project with that code already exists. Select the existing project instead.") from exc
    db.add(UserProject(user_id=user.id, project_id=project.id))
    audit(db, "created", "project", project.id, actor_user_id=user.id, after={"code": project.code, "pi_id": pi.id, "source": "booking", "requires_review": user.role != "admin"}, request_id=request_id)
    return project


def create_booking_grant(
    db: Session, *, user: User, project_id: int, number: str, name: str, request_id: str = "",
) -> Grant:
    user = db.get(User, user.id)
    project = db.get(Project, project_id)
    normalized_number, normalized_name = number.strip(), name.strip()
    if not user or not user.active:
        raise ValueError("An active user is required.")
    if not project or not project.active:
        raise ValueError("Select an active project before creating a grant.")
    if user.role != "admin" and not db.get(UserProject, {"user_id": user.id, "project_id": project.id}):
        raise ValueError("Add the selected project to your profile before creating a grant for it.")
    if not normalized_number or not normalized_name:
        raise ValueError("Grant number and name are required.")
    if db.scalar(select(Grant.id).where(func.lower(Grant.number) == normalized_number.lower())):
        raise ValueError("A grant with that number already exists. Select the existing grant instead.")
    grant = Grant(pi_id=project.pi_id, number=normalized_number, name=normalized_name, active=True)
    db.add(grant)
    try:
        db.flush()
    except IntegrityError as exc:
        raise ValueError("A grant with that number already exists. Select the existing grant instead.") from exc
    db.add(ProjectGrant(project_id=project.id, grant_id=grant.id))
    audit(db, "created", "grant", grant.id, actor_user_id=user.id, after={"number": grant.number, "project_id": project.id, "source": "booking"}, request_id=request_id)
    return grant


def configured_slots(db: Session, instrument_id: int, start_text: str, count: int) -> list[Slot]:
    if count < 1 or count > 100:
        raise ValueError("A booking may select between 1 and 100 consecutive blocks.")
    start = parse_utc(start_text)
    timezone = get_setting(db, "timezone", "UTC") or "UTC"
    candidates = slots_for_range(db, instrument_id, start.date() - timedelta(days=1), start.date() + timedelta(days=60), timezone)
    try:
        index = next(i for i, slot in enumerate(candidates) if slot.start_text == utc_text(start))
    except StopIteration as exc:
        raise ValueError("The selected start is not a configured instrument block.") from exc
    selected = candidates[index:index + count]
    if len(selected) != count:
        raise ValueError("The requested blocks extend beyond configured availability.")
    if any(previous.end_text > current.start_text for previous, current in zip(selected, selected[1:])):
        raise ValueError("The requested blocks are not consecutive in the instrument schedule.")
    return selected


def _serialize_hold_write(db: Session) -> None:
    db.flush()
    if not db.in_transaction():
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    else:
        db.execute(text("UPDATE app_settings SET value=value WHERE 0"))


def acquire_hold(db: Session, *, user: User, instrument_id: int, start_text: str, count: int, request_id: str = "") -> tuple[Hold, str, list[Slot]]:
    _serialize_hold_write(db)
    prune_expired_holds(db)
    prune_expired_tentatives(db)
    instrument = db.get(Instrument, instrument_id)
    if not instrument or not instrument.active or instrument.unavailable:
        raise ValueError("The instrument is unavailable.")
    slots = configured_slots(db, instrument_id, start_text, count)
    now = utcnow(); token = secrets.token_urlsafe(32)
    if any(slot.start.astimezone(UTC) <= now for slot in slots):
        raise ValueError("All selected booking blocks must be in the future.")
    existing_holds = db.execute(
        select(Hold, CalendarEntry)
        .join(CalendarEntry, CalendarEntry.id == Hold.calendar_entry_id)
        .where(Hold.user_id == user.id, CalendarEntry.instrument_id == instrument_id)
        .order_by(Hold.id)
    ).all()
    for existing_hold, existing_entry in existing_holds:
        audit(
            db,
            "superseded",
            "hold",
            existing_hold.id,
            actor_user_id=user.id,
            after={"replacement_instrument_id": instrument_id},
            request_id=request_id,
        )
        db.delete(existing_entry)
    if existing_holds:
        db.flush()
        for existing_hold, existing_entry in existing_holds:
            if existing_hold in db:
                db.expunge(existing_hold)
            if existing_entry in db:
                db.expunge(existing_entry)
    if reservation_overlap(db, instrument_id=instrument_id, slots=slots):
        raise ValueError("One or more selected instrument blocks are no longer available.")
    entry = CalendarEntry(instrument_id=instrument_id, entry_type="hold", state="held", title="Temporarily held", created_by=user.id, created_at=utc_text(now), updated_at=utc_text(now))
    db.add(entry); db.flush()
    hold_minutes = int(get_setting(db, "hold_minutes", "15") or "15")
    hold = Hold(calendar_entry_id=entry.id, user_id=user.id, token_hash=hash_token(token), expires_at=utc_text(now + timedelta(minutes=hold_minutes)))
    db.add(hold); reserve_slots(db, entry, slots)
    audit(db, "created", "hold", hold.id, actor_user_id=user.id, after={"expires_at": hold.expires_at, "blocks": count}, request_id=request_id)
    return hold, token, slots


def resolve_hold(db: Session, *, hold_id: int, token: str, user_id: int) -> tuple[Hold, CalendarEntry, list[ReservedSlot]]:
    prune_expired_holds(db)
    db.flush()
    hold = db.scalar(select(Hold).where(Hold.id == hold_id))
    if not hold or hold.expires_at <= utc_text() or hold.user_id != user_id or not secrets.compare_digest(hold.token_hash, hash_token(token)):
        raise ValueError("The hold is missing, expired, or belongs to another user.")
    entry = db.get(CalendarEntry, hold.calendar_entry_id)
    if not entry:
        raise ValueError("The hold is missing, expired, or belongs to another user.")
    slots = db.scalars(select(ReservedSlot).where(ReservedSlot.calendar_entry_id == entry.id).order_by(ReservedSlot.slot_start)).all()
    if not slots:
        raise ValueError("The hold is missing, expired, or belongs to another user.")
    return hold, entry, slots


def slot_values(rows: list[ReservedSlot]) -> list[Slot]:
    return [Slot(parse_utc(row.slot_start), parse_utc(row.slot_end), row.nominal_minutes, "Reserved block") for row in rows]


def submit_booking(
    db: Session, *, hold_id: int, token: str, user: User, pi_id: int, project_id: int,
    grant_id: int | None, experiment_code: str, expected_minutes: int | None,
    injection_count: int | None, gradient_minutes: int | None, sample_category: str,
    description: str = "", admin_notes: str = "", override_required: bool = False,
    request_id: str = "", expected_instrument_id: int | None = None, column_setup: str = "",
    libraries_present: bool = False, gradient_minutes_value: int | None = None,
) -> tuple[Booking, list[RuleResult]]:
    _serialize_hold_write(db)
    user = db.get(User, user.id)
    if not user or not user.active:
        raise ValueError("Your account is inactive or no longer available.")
    hold, entry, rows = resolve_hold(db, hold_id=hold_id, token=token, user_id=user.id)
    if expected_instrument_id is not None and entry.instrument_id != expected_instrument_id:
        raise ValueError("The selected instrument does not match this hold.")
    pi, project, instrument = db.get(PI, pi_id), db.get(Project, project_id), db.get(Instrument, entry.instrument_id)
    if user.role != "admin" and not db.get(UserProject, {"user_id": user.id, "project_id": project_id}):
        raise ValueError("Add the selected project to your profile before booking it.")
    if grant_id:
        grant = db.get(Grant, grant_id)
        link = db.get(ProjectGrant, {"project_id": project_id, "grant_id": grant_id})
        if not grant or not link:
            raise ValueError("The selected grant is not linked to the project.")
    if expected_minutes is None:
        expected_minutes = estimate_duration_minutes(injection_count, gradient_minutes) if injection_count is not None and gradient_minutes is not None else sum(row.nominal_minutes for row in rows)
    if expected_minutes <= 0 or not experiment_code.strip() or not sample_category.strip():
        raise ValueError("Experiment code and sample category are required; any supplied duration must be positive.")
    slots = slot_values(rows)
    try:
        current_slots = configured_slots(db, entry.instrument_id, rows[0].slot_start, len(rows))
    except ValueError as exc:
        raise ValueError("The held blocks no longer match the current instrument schedule. Release the hold and choose available blocks.") from exc
    if [slot.start_text for slot in current_slots] != [slot.start_text for slot in slots]:
        raise ValueError("The held blocks no longer match the current instrument schedule. Release the hold and choose available blocks.")
    results = evaluate_booking(db, user=user, instrument=instrument, pi=pi, project=project, slots=slots, override_required=override_required, exclude_calendar_entry_id=entry.id, hold_valid=True)
    outcome = overall_result(results)
    if outcome == "blocked":
        raise ValueError(" ".join(result.message for result in results if result.severity == "blocked"))
    state = "tentative" if outcome == "approval_required" else "confirmed"
    entry.entry_type = "booking"; entry.state = state; entry.title = experiment_code.strip(); entry.updated_at = utc_text()
    booking = Booking(calendar_entry_id=entry.id, user_id=user.id, pi_id=pi_id, project_id=project_id, grant_id=grant_id, experiment_code=experiment_code.strip(), expected_minutes=expected_minutes, sample_category=sample_category.strip(), description=description.strip(), admin_notes=admin_notes.strip(), override_required=override_required, requested_start=rows[0].slot_start, requested_end=rows[-1].slot_end, column_setup=column_setup.strip(), libraries_present=libraries_present, gradient_minutes=gradient_minutes_value, reserved_minutes=sum(row.nominal_minutes for row in rows))
    db.add(booking); db.delete(hold); db.flush()
    audit(db, "created", "booking", booking.id, actor_user_id=user.id, after={"state": state, "rules": [result.as_dict() for result in results]}, request_id=request_id)
    enqueue_notification(db, recipient_user_id=user.id, event_key=f"booking:{booking.id}:created", title="Booking confirmed" if state == "confirmed" else "Booking pending approval", body=f"{booking.experiment_code} is {state}.", link=f"/entries/{entry.id}")
    return booking, results


def save_wizard_state(db: Session, session_token: str | None, values: dict[str, object]) -> None:
    if not session_token:
        return
    record = db.scalar(select(SessionRecord).where(SessionRecord.token_hash == hash_token(session_token)))
    if record:
        record.wizard_json = json.dumps(values, sort_keys=True)


def clear_wizard_state(db: Session, session_token: str | None) -> None:
    if not session_token:
        return
    record = db.scalar(select(SessionRecord).where(SessionRecord.token_hash == hash_token(session_token)))
    if record:
        record.wizard_json = "{}"


def release_hold_as_admin(
    db: Session,
    *,
    hold_id: int,
    admin_id: int,
    reason: str,
    request_id: str = "",
) -> None:
    _serialize_hold_write(db)
    require_active_admin(db, admin_id)
    if not reason.strip():
        raise ValueError("A hold-release reason is required.")
    prune_expired_holds(db)
    hold = db.get(Hold, hold_id)
    if not hold:
        raise ValueError("Hold not found or already expired.")
    entry = db.get(CalendarEntry, hold.calendar_entry_id)
    if not entry:
        raise ValueError("Hold not found or already expired.")
    audit(
        db,
        "released",
        "hold",
        hold.id,
        actor_user_id=admin_id,
        reason=reason,
        after={"owner_user_id": hold.user_id, "instrument_id": entry.instrument_id},
        request_id=request_id,
    )
    db.delete(entry)


def release_hold(
    db: Session,
    *,
    hold_id: int,
    tokens: list[str],
    user_id: int,
    request_id: str = "",
) -> int:
    _serialize_hold_write(db)
    prune_expired_holds(db)
    hold = db.get(Hold, hold_id)
    if not hold or hold.user_id != user_id:
        raise ValueError("The hold is missing, expired, or belongs to another user.")
    token = next((value for value in tokens if secrets.compare_digest(hold.token_hash, hash_token(value))), None)
    if not token:
        raise ValueError("The hold is missing, expired, or belongs to another user.")
    hold, entry, _slots = resolve_hold(db, hold_id=hold_id, token=token, user_id=user_id)
    instrument_id = entry.instrument_id
    audit(db, "released", "hold", hold.id, actor_user_id=user_id, request_id=request_id)
    db.delete(entry)
    return instrument_id
