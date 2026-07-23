from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.orm import Session

from .config import Settings
from .models import SessionRecord, User

PASSWORD_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)


def utcnow() -> datetime:
    return datetime.now(UTC)


def utc_text(value: datetime | None = None) -> str:
    return (value or utcnow()).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Password must contain at least 12 characters.")
    return PASSWORD_HASHER.hash(password)


def verify_password(encoded: str, password: str) -> bool:
    try:
        return PASSWORD_HASHER.verify(encoded, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def create_session(db: Session, user: User, settings: Settings) -> tuple[str, SessionRecord]:
    token = secrets.token_urlsafe(32)
    now = utcnow()
    record = SessionRecord(
        token_hash=hash_token(token),
        user_id=user.id,
        csrf_token=secrets.token_urlsafe(32),
        created_at=utc_text(now),
        last_seen_at=utc_text(now),
        expires_at=utc_text(now + timedelta(hours=settings.session_absolute_hours)),
    )
    db.add(record)
    db.flush()
    return token, record


def resolve_session(db: Session, token: str | None, settings: Settings) -> tuple[User, SessionRecord] | None:
    if not token:
        return None
    record = db.scalar(select(SessionRecord).where(SessionRecord.token_hash == hash_token(token)))
    if not record or record.revoked_at:
        return None
    now = utcnow()
    if parse_utc(record.expires_at) <= now or parse_utc(record.last_seen_at) + timedelta(minutes=settings.session_idle_minutes) <= now:
        record.revoked_at = utc_text(now)
        return None
    user = db.get(User, record.user_id)
    if not user or not user.active:
        record.revoked_at = utc_text(now)
        return None
    record.last_seen_at = utc_text(now)
    return user, record


def revoke_user_sessions(db: Session, user_id: int) -> None:
    db.execute(update(SessionRecord).where(SessionRecord.user_id == user_id, SessionRecord.revoked_at.is_(None)).values(revoked_at=utc_text()))


def cleanup_sessions(db: Session) -> None:
    cutoff = utc_text(utcnow() - timedelta(days=7))
    db.execute(delete(SessionRecord).where(or_(SessionRecord.expires_at < cutoff, SessionRecord.revoked_at < cutoff)))


def cleanup_login_attempts(db: Session) -> None:
    cutoff = utc_text(utcnow() - timedelta(days=30))
    db.execute(text("DELETE FROM login_attempts WHERE attempted_at < :cutoff"), {"cutoff": cutoff})
