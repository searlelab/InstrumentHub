from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .models import Allotment, Booking, CalendarEntry, Grant, Instrument, PI, Project, ReservedSlot, User
from .security import parse_utc


def csv_safe(value: object) -> object:
    if isinstance(value, str) and value[:1] in {"=", "+", "-", "@"}:
        return f"'{value}"
    return value


def usage_report(db: Session, *, start: date, end: date, grant_id: int | None = None, pi_id: int | None = None, project_id: int | None = None, user_id: int | None = None, instrument_id: int | None = None, state: str = "") -> tuple[list[dict[str, object]], dict[str, int], list[dict[str, object]], list[dict[str, object]]]:
    query = select(ReservedSlot, CalendarEntry, Booking, User, PI, Project, Grant, Instrument).join(CalendarEntry, CalendarEntry.id == ReservedSlot.calendar_entry_id).outerjoin(Booking, Booking.calendar_entry_id == CalendarEntry.id).outerjoin(User, User.id == Booking.user_id).outerjoin(PI, PI.id == Booking.pi_id).outerjoin(Project, Project.id == Booking.project_id).outerjoin(Grant, Grant.id == Booking.grant_id).join(Instrument, Instrument.id == CalendarEntry.instrument_id).where(ReservedSlot.slot_start >= start.isoformat(), ReservedSlot.slot_start < (end + timedelta(days=1)).isoformat())
    if grant_id: query = query.where(Booking.grant_id == grant_id)
    if pi_id: query = query.where(Booking.pi_id == pi_id)
    if project_id: query = query.where(Booking.project_id == project_id)
    if user_id: query = query.where(Booking.user_id == user_id)
    if instrument_id: query = query.where(CalendarEntry.instrument_id == instrument_id)
    if state: query = query.where(CalendarEntry.state == state)
    rows: list[dict[str, object]] = []
    totals: Counter[str] = Counter()
    per_user: Counter[tuple[str, str]] = Counter()
    consecutive: defaultdict[tuple[str, str], list[tuple[object, object, int]]] = defaultdict(list)
    for slot, entry, booking, user, pi, project, grant, instrument in db.execute(query.order_by(ReservedSlot.slot_start, ReservedSlot.id)):
        minutes = slot.nominal_minutes
        totals[entry.state] += minutes
        if user:
            key = (user.display_name, instrument.name)
            per_user[key] += minutes
            consecutive[key].append((parse_utc(slot.slot_start), parse_utc(slot.slot_end), minutes))
        rows.append({"start": slot.slot_start, "end": slot.slot_end, "state": entry.state, "entry_type": entry.entry_type, "minutes": minutes, "minutes_source": "reserved", "instrument": instrument.name, "user": user.display_name if user else "", "pi": pi.name if pi else "", "project": project.code if project else "", "grant": grant.number if grant else "", "experiment": booking.experiment_code if booking else entry.title})
    terminal = select(CalendarEntry, Booking, User, PI, Project, Grant, Instrument).join(Booking, Booking.calendar_entry_id == CalendarEntry.id).join(User, User.id == Booking.user_id).join(PI, PI.id == Booking.pi_id).join(Project, Project.id == Booking.project_id).outerjoin(Grant, Grant.id == Booking.grant_id).join(Instrument, Instrument.id == CalendarEntry.instrument_id).where(CalendarEntry.state.in_(["canceled", "rejected"]), Booking.requested_start >= start.isoformat(), Booking.requested_start < (end + timedelta(days=1)).isoformat())
    if grant_id: terminal = terminal.where(Booking.grant_id == grant_id)
    if pi_id: terminal = terminal.where(Booking.pi_id == pi_id)
    if project_id: terminal = terminal.where(Booking.project_id == project_id)
    if user_id: terminal = terminal.where(Booking.user_id == user_id)
    if instrument_id: terminal = terminal.where(CalendarEntry.instrument_id == instrument_id)
    if state and state not in {"canceled", "rejected"}:
        terminal = terminal.where(text("0"))
    elif state:
        terminal = terminal.where(CalendarEntry.state == state)
    for entry, booking, user, pi, project, grant, instrument in db.execute(terminal.order_by(Booking.requested_start, Booking.id)):
        minutes = booking.reserved_minutes
        source = "reserved" if minutes else "snapshot unavailable"
        totals[entry.state] += minutes
        per_user[(user.display_name, instrument.name)] += minutes
        rows.append({"start": booking.requested_start, "end": booking.requested_end, "state": entry.state, "entry_type": entry.entry_type, "minutes": minutes, "minutes_source": source, "instrument": instrument.name, "user": user.display_name, "pi": pi.name, "project": project.code, "grant": grant.number if grant else "", "experiment": booking.experiment_code})
    usage = []
    for (name, instrument), minutes in sorted(per_user.items()):
        longest = current = 0
        previous_end = None
        for slot_start, slot_end, slot_minutes in sorted(consecutive[(name, instrument)]):
            if previous_end == slot_start:
                current += slot_minutes
            else:
                current = slot_minutes
            longest = max(longest, current)
            previous_end = slot_end
        usage.append({"user": name, "instrument": instrument, "reserved_minutes": minutes, "longest_consecutive_minutes": longest})

    allotments: list[dict[str, object]] = []
    allotment_query = select(Allotment).where(Allotment.period_start <= end.isoformat(), Allotment.period_end >= start.isoformat())
    if instrument_id:
        allotment_query = allotment_query.where((Allotment.instrument_id.is_(None)) | (Allotment.instrument_id == instrument_id))
    for allotment in db.scalars(allotment_query.order_by(Allotment.period_start, Allotment.id)):
        allotment_slots = select(ReservedSlot, CalendarEntry, Booking).join(CalendarEntry, CalendarEntry.id == ReservedSlot.calendar_entry_id).join(Booking, Booking.calendar_entry_id == CalendarEntry.id).where(
            ReservedSlot.slot_start >= max(start.isoformat(), allotment.period_start),
            ReservedSlot.slot_start < (min(end, date.fromisoformat(allotment.period_end)) + timedelta(days=1)).isoformat(),
            CalendarEntry.state.in_(["confirmed", "active", "paused", "completed", "tentative"]),
        )
        if allotment.instrument_id:
            allotment_slots = allotment_slots.where(CalendarEntry.instrument_id == allotment.instrument_id)
        if allotment.scope_type == "pi":
            allotment_slots = allotment_slots.where(Booking.pi_id == allotment.scope_id)
            scope = db.get(PI, allotment.scope_id)
            scope_name = scope.name if scope else f"PI {allotment.scope_id}"
        elif allotment.scope_type == "project":
            allotment_slots = allotment_slots.where(Booking.project_id == allotment.scope_id)
            scope = db.get(Project, allotment.scope_id)
            scope_name = scope.code if scope else f"Project {allotment.scope_id}"
        else:
            continue
        used = pending = 0
        for slot, entry, _booking in db.execute(allotment_slots):
            if entry.state == "tentative":
                pending += slot.nominal_minutes
            else:
                used += slot.nominal_minutes
        allotments.append({"scope": f"{allotment.scope_type}: {scope_name}", "period_start": allotment.period_start, "period_end": allotment.period_end, "minutes_allowed": allotment.minutes_allowed, "used_minutes": used, "pending_minutes": pending, "remaining_minutes": allotment.minutes_allowed - used - pending})
    return rows, dict(sorted(totals.items())), usage, allotments
