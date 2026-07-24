from __future__ import annotations

import calendar as calendar_module
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import Booking, BlockDefinition, CalendarEntry, Hold, Instrument, Maintenance, ReservedSlot, User
from .security import parse_utc, utc_text, utcnow
from .services import audit, get_setting, require_active_admin


@dataclass(frozen=True)
class Slot:
    start: datetime
    end: datetime
    nominal_minutes: int
    name: str

    @property
    def start_text(self) -> str:
        return utc_text(self.start.astimezone(UTC))

    @property
    def end_text(self) -> str:
        return utc_text(self.end.astimezone(UTC))


def _clock(value: str) -> time:
    hour, minute = (int(piece) for piece in value.split(":"))
    return time(hour, minute)


def _local_boundary(local_date: date, clock: time, zone: ZoneInfo) -> datetime:
    value = datetime.combine(local_date, clock, zone)
    if value.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != value.replace(tzinfo=None):
        raise ValueError(f"The local block boundary {local_date.isoformat()} {clock:%H:%M} does not exist in {zone.key}.")
    return value


def validate_block_boundaries(blocks: list[tuple[str, str, str]], *, timezone: str, effective_from: date) -> None:
    """Reject recurring block boundaries that fall in a spring-forward gap."""
    zone = ZoneInfo(timezone)
    for offset in range(366 * 10):
        local_date = effective_from + timedelta(days=offset)
        for _name, start_text, end_text in blocks:
            start_clock, end_clock = _clock(start_text), _clock(end_text)
            _local_boundary(local_date, start_clock, zone)
            end_date = local_date + timedelta(days=1) if end_clock <= start_clock else local_date
            _local_boundary(end_date, end_clock, zone)


def slots_from_blocks(blocks: list[tuple[str, str, str]], local_date: date, timezone: str) -> list[Slot]:
    zone = ZoneInfo(timezone)
    slots: list[Slot] = []
    for name, start_text, end_text in sorted(blocks, key=lambda item: _clock(item[1])):
        start_clock, end_clock = _clock(start_text), _clock(end_text)
        start = _local_boundary(local_date, start_clock, zone)
        end_date = local_date + timedelta(days=1) if end_clock <= start_clock else local_date
        end = _local_boundary(end_date, end_clock, zone)
        nominal = int((datetime.combine(end_date, end_clock) - datetime.combine(local_date, start_clock)).total_seconds() // 60)
        slots.append(Slot(start, end, nominal, name))
    return slots


def slots_for_date(db: Session, instrument_id: int, local_date: date, timezone: str) -> list[Slot]:
    definitions = db.scalars(select(BlockDefinition).where(
        BlockDefinition.instrument_id == instrument_id,
        BlockDefinition.effective_from <= local_date.isoformat(),
        (BlockDefinition.effective_to.is_(None)) | (BlockDefinition.effective_to >= local_date.isoformat()),
    ).order_by(BlockDefinition.sort_order)).all()
    return slots_from_blocks([(item.name, item.start_time, item.end_time) for item in definitions], local_date, timezone)


def slots_for_range(db: Session, instrument_id: int, start_date: date, end_date: date, timezone: str) -> list[Slot]:
    if end_date < start_date or (end_date - start_date).days > 400:
        raise ValueError("Invalid scheduling date range.")
    output: list[Slot] = []
    current = start_date
    while current <= end_date:
        output.extend(slots_for_date(db, instrument_id, current, timezone))
        current += timedelta(days=1)
    return output


def calendar_slots_for_date(db: Session, instrument_id: int, local_date: date, timezone: str) -> list[Slot]:
    """Return schedulable blocks, or the initial block pattern for historic display only."""
    slots = slots_for_date(db, instrument_id, local_date, timezone)
    if slots:
        return slots
    first_effective = db.scalar(
        select(BlockDefinition.effective_from)
        .where(BlockDefinition.instrument_id == instrument_id)
        .order_by(BlockDefinition.effective_from)
        .limit(1)
    )
    if not first_effective or local_date >= date.fromisoformat(first_effective):
        return []
    definitions = db.scalars(
        select(BlockDefinition)
        .where(BlockDefinition.instrument_id == instrument_id, BlockDefinition.effective_from == first_effective)
        .order_by(BlockDefinition.sort_order)
    ).all()
    return slots_from_blocks([(item.name, item.start_time, item.end_time) for item in definitions], local_date, timezone)


def calendar_slots_for_range(db: Session, instrument_id: int, start_date: date, end_date: date, timezone: str) -> list[Slot]:
    if end_date < start_date or (end_date - start_date).days > 400:
        raise ValueError("Invalid scheduling date range.")
    output: list[Slot] = []
    current = start_date
    while current <= end_date:
        output.extend(calendar_slots_for_date(db, instrument_id, current, timezone))
        current += timedelta(days=1)
    return output


def month_days(year: int, month: int) -> list[date]:
    return list(calendar_module.Calendar(firstweekday=0).itermonthdates(year, month))


def prune_expired_holds(db: Session) -> int:
    db.flush()
    result = db.execute(
        text("""
            DELETE FROM calendar_entries
            WHERE id IN (
                SELECT calendar_entry_id
                FROM holds
                WHERE expires_at <= :now
            )
        """),
        {"now": utc_text()},
    )
    return result.rowcount


def prune_expired_tentatives(db: Session) -> int:
    configured = get_setting(db, "tentative_expiration_days", "") or ""
    if not configured:
        return 0
    try:
        days = int(configured)
    except ValueError:
        return 0
    cutoff = utcnow() - timedelta(days=days)
    entries = db.scalars(select(CalendarEntry).where(CalendarEntry.entry_type == "booking", CalendarEntry.state == "tentative")).all()
    expired = [entry for entry in entries if parse_utc(entry.created_at) <= cutoff]
    for entry in expired:
        booking = db.scalar(select(Booking).where(Booking.calendar_entry_id == entry.id))
        for row in db.scalars(select(ReservedSlot).where(ReservedSlot.calendar_entry_id == entry.id)).all():
            db.delete(row)
        entry.state = "canceled"; entry.updated_at = utc_text()
        audit(db, "expired", "booking", booking.id if booking else entry.id, reason="Tentative approval window expired", before={"state": "tentative"}, after={"state": "canceled"})
    return len(expired)


def reservation_overlap(
    db: Session, *, instrument_id: int, slots: list[Slot],
    exclude_calendar_entry_id: int | None = None, exclude_slot_ids: set[int] | None = None,
) -> bool:
    if not slots:
        return False
    query = select(ReservedSlot.id).where(
        ReservedSlot.instrument_id == instrument_id,
        or_(*[
            and_(ReservedSlot.slot_start < slot.end_text, ReservedSlot.slot_end > slot.start_text)
            for slot in slots
        ]),
    )
    if exclude_calendar_entry_id is not None:
        query = query.where(ReservedSlot.calendar_entry_id != exclude_calendar_entry_id)
    if exclude_slot_ids:
        query = query.where(~ReservedSlot.id.in_(exclude_slot_ids))
    return db.scalar(query.limit(1)) is not None


def reserve_slots(db: Session, entry: CalendarEntry, slots: list[Slot]) -> None:
    if reservation_overlap(db, instrument_id=entry.instrument_id, slots=slots, exclude_calendar_entry_id=entry.id):
        raise ValueError("One or more selected instrument blocks are no longer available.")
    for slot in slots:
        db.add(ReservedSlot(calendar_entry_id=entry.id, instrument_id=entry.instrument_id, slot_start=slot.start_text, slot_end=slot.end_text, nominal_minutes=slot.nominal_minutes, requested_slot_start=slot.start_text))
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError("One or more selected instrument blocks are no longer available.") from exc


def calendar_data(db: Session, instrument_id: int, year: int, month: int, timezone: str, *, prune: bool = True) -> list[dict[str, object]]:
    if prune:
        prune_expired_holds(db)
        prune_expired_tentatives(db)
    days = month_days(year, month)
    slots = calendar_slots_for_range(db, instrument_id, days[0], days[-1], timezone)
    query = select(ReservedSlot, CalendarEntry, Booking, User).join(CalendarEntry, CalendarEntry.id == ReservedSlot.calendar_entry_id).outerjoin(Booking, Booking.calendar_entry_id == CalendarEntry.id).outerjoin(User, User.id == Booking.user_id).where(ReservedSlot.instrument_id == instrument_id)
    if slots:
        query = query.where(ReservedSlot.slot_start >= slots[0].start_text, ReservedSlot.slot_start <= slots[-1].start_text)
    by_start = {row.slot_start: (row, entry, booking, handler) for row, entry, booking, handler in db.execute(query).all()}
    output: list[dict[str, object]] = []
    zone = ZoneInfo(timezone)
    for slot in slots:
        occupied = by_start.get(slot.start_text)
        item: dict[str, object] = {"date": slot.start.astimezone(zone).date().isoformat(), "name": slot.name, "start": slot.start_text, "end": slot.end_text, "nominal_minutes": slot.nominal_minutes, "status": "available", "title": "Available", "entry_id": None}
        if slot.start.astimezone(UTC) <= utcnow():
            item.update(status="past", title="Past")
        if occupied:
            _reserved, entry, _booking, handler = occupied
            item.update(status="held" if entry.entry_type == "hold" else entry.state, title=entry.title, entry_id=entry.id, entry_type=entry.entry_type, handler_initials=handler.initials if handler else "", handler_name=handler.display_name if handler else "")
        output.append(item)
    return output


def create_maintenance(db: Session, *, instrument_id: int, start_date: date, end_date: date, kind: str, reason: str, actor_user_id: int, request_id: str = "") -> CalendarEntry:
    require_active_admin(db, actor_user_id)
    prune_expired_holds(db)
    if kind not in {"routine", "emergency", "outage"} or not reason.strip():
        raise ValueError("Maintenance type and reason are required.")
    instrument = db.get(Instrument, instrument_id)
    if not instrument or not instrument.active:
        raise ValueError("Instrument is not active.")
    slots = slots_for_range(db, instrument_id, start_date, end_date, get_setting(db, "timezone", "UTC") or "UTC")
    if not slots:
        raise ValueError("No configured blocks exist in that date range.")
    now = utc_text(); entry_type = "outage" if kind == "outage" else "maintenance"
    entry = CalendarEntry(instrument_id=instrument_id, entry_type=entry_type, state=entry_type, title="Instrument outage" if kind == "outage" else "Maintenance", created_by=actor_user_id, created_at=now, updated_at=now)
    db.add(entry); db.flush()
    db.add(Maintenance(calendar_entry_id=entry.id, kind=kind, known_duration=True, reason=reason.strip()))
    reserve_slots(db, entry, slots)
    audit(db, "created", entry_type, entry.id, actor_user_id=actor_user_id, reason=reason, after={"start": slots[0].start_text, "end": slots[-1].end_text}, request_id=request_id)
    return entry
