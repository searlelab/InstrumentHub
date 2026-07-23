from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import AppSetting, AuditEvent, BlockDefinition, Booking, CalendarEntry, Instrument, ReservedSlot, User
from .security import hash_password, parse_utc, utc_text

SENSITIVE_KEYS = {"password", "password_hash", "token", "token_hash", "csrf", "csrf_token", "secret"}
USERNAME_RE = re.compile(r"^[a-zA-Z0-9._-]{3,64}$")


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: "[REDACTED]" if key.lower() in SENSITIVE_KEYS else sanitize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    return value


def audit(
    db: Session,
    action: str,
    entity_type: str,
    entity_id: int | str | None,
    *,
    actor_user_id: int | None = None,
    reason: str = "",
    before: Any = None,
    after: Any = None,
    request_id: str = "",
) -> None:
    db.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id is not None else None,
            reason=reason,
            before_json=json.dumps(sanitize(before or {}), sort_keys=True, separators=(",", ":")),
            after_json=json.dumps(sanitize(after or {}), sort_keys=True, separators=(",", ":")),
            request_id=request_id,
            created_at=utc_text(),
        )
    )


def require_active_admin(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if not user or not user.active or user.role != "admin":
        raise ValueError("An active administrator is required.")
    return user


def create_user(
    db: Session,
    *,
    username: str,
    password: str,
    display_name: str,
    initials: str,
    role: str = "user",
    email: str = "",
    phone: str = "",
    actor_user_id: int | None = None,
    request_id: str = "",
) -> User:
    username = username.strip()
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("Username must be 3-64 letters, numbers, dots, dashes, or underscores.")
    if role not in {"user", "admin"}:
        raise ValueError("Invalid role.")
    existing_users = db.scalar(select(func.count()).select_from(User)) or 0
    if existing_users:
        if actor_user_id is None:
            raise ValueError("An active administrator is required to create users.")
        require_active_admin(db, actor_user_id)
    elif actor_user_id is not None or role != "admin":
        raise ValueError("The first account must be created as an administrator by bootstrap.")
    if db.scalar(select(func.count()).select_from(User).where(func.lower(User.username) == username.lower())):
        raise ValueError("Username already exists.")
    user = User(
        username=username,
        password_hash=hash_password(password),
        display_name=display_name.strip(),
        initials=initials.strip().upper()[:6],
        email=email.strip(),
        phone=phone.strip(),
        role=role,
        active=True,
        created_at=utc_text(),
    )
    if not user.display_name or not user.initials:
        raise ValueError("Display name and initials are required.")
    db.add(user)
    db.flush()
    audit(db, "created", "user", user.id, actor_user_id=actor_user_id, after={"username": username, "role": role}, request_id=request_id)
    return user


def get_setting(db: Session, key: str, default: str | None = None) -> str | None:
    setting = db.get(AppSetting, key)
    return setting.value if setting else default


def set_setting(db: Session, key: str, value: str) -> None:
    setting = db.get(AppSetting, key)
    if setting:
        setting.value = value
    else:
        db.add(AppSetting(key=key, value=value))


def setup_complete(db: Session) -> bool:
    return get_setting(db, "setup_complete") == "true"


def parse_blocks(text: str) -> list[tuple[str, str, str]]:
    blocks: list[tuple[str, str, str]] = []
    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        parts = [part.strip() for part in raw_line.split(",")]
        if len(parts) != 3 or not re.fullmatch(r"\d{2}:\d{2}", parts[1]) or not re.fullmatch(r"\d{2}:\d{2}", parts[2]):
            raise ValueError("Each block line must be Name,HH:MM,HH:MM.")
        for clock in parts[1:]:
            hour, minute = (int(value) for value in clock.split(":"))
            if hour > 23 or minute > 59:
                raise ValueError("Block times must be valid 24-hour clock values.")
        if parts[1] == parts[2]:
            raise ValueError("A block cannot have zero duration.")
        blocks.append((parts[0], parts[1], parts[2]))
    if not blocks:
        raise ValueError("At least one block is required for each instrument.")
    if len({name.lower() for name, _start, _end in blocks}) != len(blocks):
        raise ValueError("Block names must be distinct within an instrument version.")
    segments: list[tuple[int, int, str]] = []
    for name, start, end in blocks:
        start_minute = int(start[:2]) * 60 + int(start[3:])
        end_minute = int(end[:2]) * 60 + int(end[3:])
        if end_minute <= start_minute:
            segments.extend([(start_minute, 1440, name), (0, end_minute, name)])
        else:
            segments.append((start_minute, end_minute, name))
    segments.sort()
    for previous, current in zip(segments, segments[1:]):
        if current[0] < previous[1]:
            raise ValueError(f"Blocks {previous[2]} and {current[2]} overlap.")
    # Block order is a scheduling invariant, not a data-entry convention.
    # Keep the stored sort order aligned with each block's local start time.
    return sorted(blocks, key=lambda block: (int(block[1][:2]), int(block[1][3:])))


def replace_block_definitions(db: Session, *, instrument_id: int, effective_from: date, blocks_text: str, actor_user_id: int, request_id: str = "") -> None:
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    require_active_admin(db, actor_user_id)
    if effective_from <= date.today():
        raise ValueError("A revised block definition must begin on a future date.")
    instrument = db.get(Instrument, instrument_id)
    if not instrument:
        raise ValueError("Instrument not found.")
    if instrument.unavailable:
        raise ValueError("Cannot revise blocks while an unknown outage is unresolved. Resume it or use the outage recovery action first.")
    blocks = parse_blocks(blocks_text)
    timezone = get_setting(db, "timezone", "UTC") or "UTC"
    from .scheduling import slots_for_date, slots_from_blocks, validate_block_boundaries
    validate_block_boundaries(blocks, timezone=timezone, effective_from=effective_from)
    outgoing = slots_for_date(db, instrument_id, effective_from - timedelta(days=1), timezone)
    incoming = slots_from_blocks(blocks, effective_from, timezone)
    if any(previous.start < current.end and current.start < previous.end for previous in outgoing for current in incoming):
        raise ValueError("The revised block definition overlaps the outgoing version across its effective-date boundary.")
    future_slots = db.scalars(select(ReservedSlot).join(CalendarEntry, CalendarEntry.id == ReservedSlot.calendar_entry_id).where(
        ReservedSlot.instrument_id == instrument_id,
        CalendarEntry.entry_type.in_(["booking", "maintenance", "outage"]),
        ~CalendarEntry.state.in_(["completed", "canceled", "rejected"]),
    )).all()
    blocking = [slot for slot in future_slots if parse_utc(slot.slot_start).astimezone(ZoneInfo(timezone)).date() >= effective_from]
    if blocking:
        last_blocked = max(parse_utc(slot.slot_start).astimezone(ZoneInfo(timezone)).date() for slot in blocking)
        raise ValueError(f"Cannot revise blocks before {last_blocked + timedelta(days=1):%Y-%m-%d}; {len(blocking)} scheduled reservation block(s) would need reconciliation.")
    latest_effective = db.scalar(select(func.max(BlockDefinition.effective_from)).where(BlockDefinition.instrument_id == instrument_id))
    if latest_effective and effective_from.isoformat() <= latest_effective:
        raise ValueError("A revised block version must begin after every existing version.")
    prior = db.scalars(select(BlockDefinition).where(BlockDefinition.instrument_id == instrument_id, BlockDefinition.effective_to.is_(None))).all()
    cutoff = (effective_from - timedelta(days=1)).isoformat()
    for definition in prior:
        definition.effective_to = cutoff
    for index, (name, start, end) in enumerate(blocks):
        db.add(BlockDefinition(instrument_id=instrument_id, name=name, start_time=start, end_time=end, effective_from=effective_from.isoformat(), sort_order=index))
    audit(db, "versioned", "block_definition", instrument_id, actor_user_id=actor_user_id, after={"effective_from": effective_from.isoformat(), "blocks": [item[0] for item in blocks]}, request_id=request_id)


def complete_setup(
    db: Session,
    *,
    timezone: str,
    instruments: list[dict[str, Any]],
    hold_minutes: int,
    tentative_expiration_days: int | None,
    actor_user_id: int,
    request_id: str = "",
) -> None:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    require_active_admin(db, actor_user_id)
    if setup_complete(db):
        raise ValueError("Initial setup is already complete.")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("Unknown IANA timezone.") from exc
    if len(instruments) != 2 or len({item["name"].strip().lower() for item in instruments}) != 2:
        raise ValueError("Exactly two instruments with distinct names are required.")
    if not 5 <= hold_minutes <= 60:
        raise ValueError("Hold duration must be between 5 and 60 minutes.")
    effective = date.today().isoformat()
    for instrument_data in instruments:
        blocks = parse_blocks(instrument_data["blocks"])
        from .scheduling import validate_block_boundaries
        validate_block_boundaries(blocks, timezone=timezone, effective_from=date.today())
        instrument = Instrument(
            name=instrument_data["name"].strip(),
            active=True,
            training_required=bool(instrument_data.get("training_required")),
            unavailable=False,
        )
        db.add(instrument)
        db.flush()
        for index, (name, start, end) in enumerate(blocks):
            db.add(BlockDefinition(instrument_id=instrument.id, name=name, start_time=start, end_time=end, effective_from=effective, sort_order=index))
    set_setting(db, "timezone", timezone)
    set_setting(db, "hold_minutes", str(hold_minutes))
    set_setting(db, "tentative_expiration_days", "" if tentative_expiration_days is None else str(tentative_expiration_days))
    set_setting(db, "setup_complete", "true")
    audit(db, "completed", "setup", None, actor_user_id=actor_user_id, after={"timezone": timezone, "instruments": [i["name"] for i in instruments]}, request_id=request_id)
