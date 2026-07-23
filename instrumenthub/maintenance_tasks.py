from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .models import CalendarEntry, MaintenanceTask, MaintenanceTemplate, ReservedSlot, User
from .notifications import enqueue_notification
from .security import utc_text
from .services import audit, require_active_admin
from .scheduling import create_maintenance

MAX_TASKS_PER_GENERATION = 366


def _serialize_task_write(db: Session) -> None:
    db.flush()
    if not db.in_transaction():
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    else:
        db.execute(text("UPDATE app_settings SET value=value WHERE 0"))


def generate_due_tasks(db: Session, *, through: date | None = None) -> list[MaintenanceTask]:
    through = through or date.today()
    _serialize_task_write(db)
    created: list[MaintenanceTask] = []
    templates = db.scalars(select(MaintenanceTemplate).where(MaintenanceTemplate.active.is_(True)).order_by(MaintenanceTemplate.id)).all()
    for template in templates:
        due = date.fromisoformat(template.next_due_date)
        while due <= through and len(created) < MAX_TASKS_PER_GENERATION:
            inserted = db.execute(text("INSERT OR IGNORE INTO maintenance_tasks(template_id, due_date, assignee_user_id, state, created_at) VALUES (:template_id, :due_date, :assignee_user_id, 'open', :created_at)"), {"template_id": template.id, "due_date": due.isoformat(), "assignee_user_id": template.assignee_user_id, "created_at": utc_text()})
            task = db.scalar(select(MaintenanceTask).where(MaintenanceTask.template_id == template.id, MaintenanceTask.due_date == due.isoformat()))
            if not task:
                raise ValueError("Maintenance task could not be created.")
            if inserted.rowcount:
                created.append(task)
            if inserted.rowcount and task.assignee_user_id:
                enqueue_notification(db, recipient_user_id=task.assignee_user_id, event_key=f"maintenance-task:{task.id}:assigned", title="Maintenance task due", body=f"{template.title} is due on {task.due_date}.", link="/admin/schedule")
            due += timedelta(days=template.interval_days)
        template.next_due_date = due.isoformat()
        if len(created) >= MAX_TASKS_PER_GENERATION:
            break
    for task, template in db.execute(select(MaintenanceTask, MaintenanceTemplate).join(MaintenanceTemplate, MaintenanceTemplate.id == MaintenanceTask.template_id).where(MaintenanceTemplate.active.is_(True), MaintenanceTask.state == "open", MaintenanceTask.due_date < date.today().isoformat(), MaintenanceTask.assignee_user_id.is_not(None)).order_by(MaintenanceTask.id)):
        enqueue_notification(db, recipient_user_id=task.assignee_user_id, event_key=f"maintenance-task:{task.id}:overdue", title="Maintenance task overdue", body=f"{template.title} was due on {task.due_date}.", link="/admin/schedule")
    return created


def create_template(
    db: Session, *, instrument_id: int, title: str, interval_days: int, expected_minutes: int,
    assignee_user_id: int | None, first_due_date: date, admin_id: int, request_id: str = ""
) -> MaintenanceTemplate:
    require_active_admin(db, admin_id)
    if not title.strip() or not 1 <= interval_days <= 3650 or expected_minutes <= 0:
        raise ValueError("A title, valid interval, and positive expected duration are required.")
    if assignee_user_id and not db.get(User, assignee_user_id):
        raise ValueError("Assignee not found.")
    template = MaintenanceTemplate(instrument_id=instrument_id, title=title.strip(), interval_days=interval_days, expected_minutes=expected_minutes, assignee_user_id=assignee_user_id, active=True, next_due_date=first_due_date.isoformat(), created_by=admin_id, created_at=utc_text())
    db.add(template); db.flush()
    audit(db, "created", "maintenance_template", template.id, actor_user_id=admin_id, after={"title": template.title}, request_id=request_id)
    return template


def complete_task(db: Session, *, task_id: int, state: str, reason: str, admin_id: int, request_id: str = "") -> MaintenanceTask:
    require_active_admin(db, admin_id)
    task = db.get(MaintenanceTask, task_id)
    if not task or task.state != "open":
        raise ValueError("Open maintenance task not found.")
    if state not in {"completed", "skipped"} or not reason.strip():
        raise ValueError("A completion or skip reason is required.")
    task.state, task.completed_at, task.completion_reason = state, utc_text(), reason.strip()
    if state == "skipped" and task.calendar_entry_id:
        entry = db.get(CalendarEntry, task.calendar_entry_id)
        if entry and entry.state == "maintenance":
            for slot in db.scalars(select(ReservedSlot).where(ReservedSlot.calendar_entry_id == entry.id)).all():
                db.delete(slot)
            entry.state, entry.updated_at = "canceled", utc_text()
            audit(db, "canceled", "maintenance", entry.id, actor_user_id=admin_id, reason=reason, before={"state": "maintenance"}, after={"state": "canceled", "maintenance_task_id": task.id}, request_id=request_id)
    audit(db, state, "maintenance_task", task.id, actor_user_id=admin_id, reason=reason, request_id=request_id)
    db.flush()
    return task


def assign_task(db: Session, *, task_id: int, assignee_user_id: int | None, reason: str, admin_id: int, request_id: str = "") -> MaintenanceTask:
    require_active_admin(db, admin_id)
    task = db.get(MaintenanceTask, task_id)
    if not task or task.state != "open" or not reason.strip():
        raise ValueError("An open maintenance task and assignment reason are required.")
    assignee = db.get(User, assignee_user_id) if assignee_user_id else None
    if assignee_user_id and (not assignee or not assignee.active):
        raise ValueError("Assignee not found or inactive.")
    previous = task.assignee_user_id
    task.assignee_user_id = assignee_user_id
    assignment = audit(db, "assigned", "maintenance_task", task.id, actor_user_id=admin_id, reason=reason, before={"assignee_user_id": previous}, after={"assignee_user_id": assignee_user_id}, request_id=request_id)
    db.flush()
    if assignee:
        template = db.get(MaintenanceTemplate, task.template_id)
        enqueue_notification(db, recipient_user_id=assignee.id, event_key=f"maintenance-task:{task.id}:assigned:{assignment.id}", title="Maintenance task assigned", body=f"{template.title if template else 'Maintenance task'} is due on {task.due_date}.", link="/admin/maintenance-tasks")
    db.flush()
    return task


def set_template_active(db: Session, *, template_id: int, active: bool, reason: str, admin_id: int, request_id: str = "") -> MaintenanceTemplate:
    require_active_admin(db, admin_id)
    template = db.get(MaintenanceTemplate, template_id)
    if not template or not reason.strip():
        raise ValueError("A maintenance template and reason are required.")
    previous = template.active
    template.active = active
    if active and not previous:
        due = date.fromisoformat(template.next_due_date)
        today = date.today()
        if due < today:
            skipped_intervals = (today - due).days // template.interval_days
            due += timedelta(days=skipped_intervals * template.interval_days)
            if due < today:
                due += timedelta(days=template.interval_days)
            template.next_due_date = due.isoformat()
    audit(db, "activated" if active else "deactivated", "maintenance_template", template.id, actor_user_id=admin_id, reason=reason, before={"active": previous}, after={"active": active}, request_id=request_id)
    db.flush()
    return template


def schedule_task(
    db: Session, *, task_id: int, start_date: date, end_date: date, reason: str, admin_id: int, request_id: str = ""
) -> MaintenanceTask:
    require_active_admin(db, admin_id)
    task = db.get(MaintenanceTask, task_id)
    if not task or task.state != "open" or task.calendar_entry_id:
        raise ValueError("Open unscheduled maintenance task not found.")
    template = db.get(MaintenanceTemplate, task.template_id)
    if not template or end_date < start_date or not reason.strip():
        raise ValueError("A valid maintenance date range and reason are required.")
    entry = create_maintenance(db, instrument_id=template.instrument_id, start_date=start_date, end_date=end_date, kind="routine", reason=reason, actor_user_id=admin_id, request_id=request_id)
    task.calendar_entry_id = entry.id
    audit(db, "scheduled", "maintenance_task", task.id, actor_user_id=admin_id, reason=reason, after={"calendar_entry_id": entry.id}, request_id=request_id)
    return task
