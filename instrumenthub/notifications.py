from __future__ import annotations

import smtplib
from datetime import timedelta
from email.message import EmailMessage

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .config import Settings
from .models import Notification, NotificationDelivery, User
from .security import utc_text, utcnow

MAX_DELIVERY_ATTEMPTS = 10
NEVER_RETRY_AT = "9999-12-31T23:59:59.999999Z"


def enqueue_notification(
    db: Session, *, recipient_user_id: int, event_key: str, title: str, body: str, link: str = ""
) -> Notification:
    """Persist an inbox item and one delivery job; duplicate domain events are harmless."""
    created_at = utc_text()
    db.execute(text("INSERT OR IGNORE INTO notifications(recipient_user_id, event_key, title, body, link, created_at) VALUES (:recipient_user_id, :event_key, :title, :body, :link, :created_at)"), {"recipient_user_id": recipient_user_id, "event_key": event_key, "title": title.strip(), "body": body.strip(), "link": link, "created_at": created_at})
    notice = db.scalar(select(Notification).where(Notification.recipient_user_id == recipient_user_id, Notification.event_key == event_key))
    if not notice:
        raise ValueError("Notification could not be created.")
    db.execute(text("INSERT OR IGNORE INTO notification_deliveries(notification_id, state, attempts, next_attempt_at, last_error) VALUES (:notification_id, 'pending', 0, :next_attempt_at, '')"), {"notification_id": notice.id, "next_attempt_at": created_at})
    return notice


def unread_count(db: Session, user_id: int) -> int:
    return len(db.scalars(select(Notification.id).where(Notification.recipient_user_id == user_id, Notification.read_at.is_(None))).all())


def mark_read(db: Session, *, notification_id: int, user_id: int) -> None:
    notice = db.get(Notification, notification_id)
    if not notice or notice.recipient_user_id != user_id:
        raise ValueError("Notification not found.")
    if not notice.read_at:
        notice.read_at = utc_text()


def _retry_at(attempts: int) -> str:
    return utc_text(utcnow() + timedelta(minutes=min(60, 2 ** min(attempts, 6))))


def deliver_pending(db: Session, settings: Settings, *, limit: int = 100) -> dict[str, int]:
    """Attempt a bounded batch without letting terminal rows starve later delivery."""
    if not 1 <= limit <= 500:
        raise ValueError("Notification delivery limit must be from 1 through 500.")
    result = {"attempted": 0, "sent": 0, "deferred": 0, "failed": 0}
    if not settings.smtp_host or not settings.smtp_from:
        return result
    rows = db.execute(
        select(NotificationDelivery, Notification, User)
        .join(Notification, Notification.id == NotificationDelivery.notification_id)
        .join(User, User.id == Notification.recipient_user_id)
        .where(NotificationDelivery.state.in_(["pending", "failed"]), NotificationDelivery.next_attempt_at <= utc_text())
        .order_by(NotificationDelivery.id)
        .limit(500)
    ).all()
    for delivery, notice, recipient in rows:
        if result["attempted"] >= limit:
            break
        if not recipient.email:
            delivery.state, delivery.attempts, delivery.last_error, delivery.next_attempt_at = "failed", delivery.attempts + 1, "Recipient has no email address.", NEVER_RETRY_AT
            result["deferred"] += 1
            db.commit()
            continue
        result["attempted"] += 1
        delivery.attempts += 1
        try:
            message = EmailMessage()
            message["Subject"] = notice.title
            message["From"] = settings.smtp_from
            message["To"] = recipient.email
            message.set_content(f"{notice.body}\n\nOpen InstrumentHub: {notice.link or '/inbox'}")
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as server:
                if settings.smtp_starttls:
                    server.starttls()
                if settings.smtp_username:
                    server.login(settings.smtp_username, settings.smtp_password)
                server.send_message(message)
            delivery.state, delivery.sent_at, delivery.last_error = "sent", utc_text(), ""
            result["sent"] += 1
        except (OSError, smtplib.SMTPException) as exc:
            delivery.state, delivery.last_error = "failed", str(exc)[:500]
            delivery.next_attempt_at = NEVER_RETRY_AT if delivery.attempts >= MAX_DELIVERY_ATTEMPTS else _retry_at(delivery.attempts)
            result["failed"] += 1
        db.commit()
    return result
