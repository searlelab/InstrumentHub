from __future__ import annotations

from datetime import date, timedelta
from dataclasses import replace
import json

from sqlalchemy import text

from instrumenthub.maintenance_tasks import assign_task, complete_task, create_template, generate_due_tasks, schedule_task, set_template_active
from instrumenthub.models import Allotment, Booking, CalendarEntry, Grant, MaintenanceTask, Notification, PI, Project, ReservedSlot, ScheduleChangeDraft, User
from instrumenthub.notifications import deliver_pending, enqueue_notification, mark_read
from instrumenthub.reporting import csv_safe, usage_report
from instrumenthub.management import edit_booking


def test_notification_is_deduplicated_readable_and_deferred_without_smtp(app, settings):
    with app.state.database.session() as db:
        first = enqueue_notification(db, recipient_user_id=2, event_key="booking:1:created", title="Booking confirmed", body="Your booking is confirmed.", link="/entries/1")
        second = enqueue_notification(db, recipient_user_id=2, event_key="booking:1:created", title="Different", body="Ignored")
        assert first.id == second.id
        assert deliver_pending(db, settings) == {"attempted": 0, "sent": 0, "deferred": 0, "failed": 0}
        mark_read(db, notification_id=first.id, user_id=2)
        assert db.get(Notification, first.id).read_at


def test_delivery_skips_unmailable_rows_and_sends_later_recipient(app, settings, monkeypatch):
    class FakeSMTP:
        sent: list[str] = []
        def __init__(self, *_args, **_kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def starttls(self): pass
        def login(self, *_args): pass
        def send_message(self, message): self.sent.append(message["To"])

    monkeypatch.setattr("instrumenthub.notifications.smtplib.SMTP", FakeSMTP)
    smtp_settings = replace(settings, smtp_host="smtp.test", smtp_from="scheduler@test")
    with app.state.database.session() as db:
        for index in range(5):
            enqueue_notification(db, recipient_user_id=1, event_key=f"missing-email:{index}", title="Missing", body="No email")
        enqueue_notification(db, recipient_user_id=2, event_key="deliverable", title="Deliver", body="Send")
        result = deliver_pending(db, smtp_settings, limit=1)
        assert result == {"attempted": 1, "sent": 1, "deferred": 5, "failed": 0}
        assert FakeSMTP.sent == ["member@example.test"]


def test_maintenance_template_generates_one_deterministic_due_task(app, configured):
    with app.state.database.session() as db:
        template = create_template(db, instrument_id=1, title="Calibrate", interval_days=7, expected_minutes=120, assignee_user_id=2, first_due_date=date.today(), admin_id=1)
        generated = generate_due_tasks(db, through=date.today())
        assert [task.template_id for task in generated] == [template.id]
        assert db.get(MaintenanceTask, generated[0].id).due_date == date.today().isoformat()
        assert db.scalar(text("SELECT COUNT(*) FROM notifications WHERE recipient_user_id=2")) == 1


def test_usage_report_handles_empty_filters_and_calendar_shows_no_current_user(client, configured, app):
    with app.state.database.session() as db:
        rows, totals, usage, allotments = usage_report(db, start=date.today(), end=date.today())
    assert rows == [] and totals == {} and usage == [] and allotments == []
    page = client.get("/calendar")
    assert page.status_code == 200
    assert "Hover a block for its instrument" in page.text


def test_canceled_usage_and_csv_link_keep_the_selected_grant(client, configured, app):
    with app.state.database.session() as db:
        pi = PI(name="Reporting PI", email="", active=True); db.add(pi); db.flush()
        project = Project(pi_id=pi.id, code="REPORT", name="Reporting", max_consecutive_minutes=None, active=True); db.add(project)
        grant = Grant(pi_id=pi.id, number="R01-REPORT", name="Reporting grant", active=True); db.add(grant)
        entry = CalendarEntry(instrument_id=1, entry_type="booking", state="canceled", title="Canceled", created_by=1, created_at="2026-01-01T00:00:00.000000Z", updated_at="2026-01-01T00:00:00.000000Z"); db.add(entry); db.flush()
        db.add(Booking(calendar_entry_id=entry.id, user_id=2, pi_id=pi.id, project_id=project.id, grant_id=grant.id, experiment_code="CANCEL", expected_minutes=100, sample_category="cells", description="", admin_notes="", override_required=False, requested_start=date.today().isoformat(), requested_end=date.today().isoformat(), reserved_minutes=120))
        grant_id = grant.id
        rows, totals, _usage, _allotments = usage_report(db, start=date.today(), end=date.today(), grant_id=grant_id)
        assert rows[0]["state"] == "canceled" and totals == {"canceled": 120}
    page = client.get(f"/admin/reports?start={date.today()}&end={date.today()}&grant_id={grant_id}")
    assert f"grant_id={grant_id}" in page.text
    csv = client.get(f"/admin/reports.csv?start={date.today()}&end={date.today()}&grant_id={grant_id}")
    assert "duration,duration_source" in csv.text
    assert "2 hours" in csv.text
    assert "minutes" not in csv.text.lower()


def test_historical_canceled_booking_reports_snapshot_unavailable(app, configured):
    with app.state.database.session() as db:
        pi = PI(name="Historical PI", email="", active=True); db.add(pi); db.flush()
        project = Project(pi_id=pi.id, code="HISTORY", name="Historical", max_consecutive_minutes=None, active=True); db.add(project)
        entry = CalendarEntry(instrument_id=1, entry_type="booking", state="canceled", title="Old canceled", created_by=1, created_at="2026-01-01T00:00:00.000000Z", updated_at="2026-01-01T00:00:00.000000Z"); db.add(entry); db.flush()
        db.add(Booking(calendar_entry_id=entry.id, user_id=2, pi_id=pi.id, project_id=project.id, grant_id=None, experiment_code="OLD", expected_minutes=480, sample_category="cells", description="", admin_notes="", override_required=False, requested_start=date.today().isoformat(), requested_end=date.today().isoformat(), reserved_minutes=0))
        rows, totals, _usage, _allotments = usage_report(db, start=date.today(), end=date.today())
        historical = next(row for row in rows if row["experiment"] == "OLD")
        assert historical["minutes"] == 0 and historical["minutes_source"] == "snapshot unavailable"
        assert totals["canceled"] == 0


def test_csv_formula_cells_are_escaped():
    assert csv_safe("=cmd|' /C calc'!A0") == "'=cmd|' /C calc'!A0"
    assert csv_safe("ordinary text") == "ordinary text"


def test_schedule_preview_keeps_review_context_and_confirmation(client, configured, app):
    with app.state.database.session() as db:
        db.add(ScheduleChangeDraft(instrument_id=1, kind="resume", status="draft", reason="Service restored", payload_json=json.dumps({"restart_at": "2026-08-01", "outage_slots": [{"start": "2026-07-31T13:00:00.000000Z", "end": "2026-07-31T21:00:00.000000Z"}], "items": []}), created_by=1, created_at="2026-07-01T00:00:00.000000Z", applied_at=None))
    page = client.get("/admin/schedule")
    assert page.status_code == 200
    assert "Restart: 2026-08-01" in page.text
    assert "Outage blocks:" in page.text
    assert 'data-confirm="Apply this reviewed schedule preview?"' in page.text


def test_maintenance_task_can_block_the_calendar(app, configured):
    with app.state.database.session() as db:
        template = create_template(db, instrument_id=1, title="Weekly clean", interval_days=7, expected_minutes=60, assignee_user_id=None, first_due_date=date.today(), admin_id=1)
        task = generate_due_tasks(db, through=date.today())[0]
        scheduled = schedule_task(db, task_id=task.id, start_date=date.today(), end_date=date.today(), reason="Scheduled maintenance", admin_id=1)
        assert scheduled.calendar_entry_id is not None


def test_skipping_scheduled_maintenance_releases_its_calendar_block(app, configured):
    with app.state.database.session() as db:
        template = create_template(db, instrument_id=1, title="Skip clean", interval_days=7, expected_minutes=60, assignee_user_id=None, first_due_date=date.today(), admin_id=1)
        task = generate_due_tasks(db, through=date.today())[0]
        task = schedule_task(db, task_id=task.id, start_date=date.today(), end_date=date.today(), reason="Hold time", admin_id=1)
        entry_id = task.calendar_entry_id
        complete_task(db, task_id=task.id, state="skipped", reason="Service not needed", admin_id=1)
        assert db.get(CalendarEntry, entry_id).state == "canceled"
        assert db.scalar(text("SELECT COUNT(*) FROM reserved_slots WHERE calendar_entry_id=:entry_id"), {"entry_id": entry_id}) == 0


def test_maintenance_assignment_and_template_activation_are_audited(app, configured):
    with app.state.database.session() as db:
        template = create_template(db, instrument_id=1, title="Assign clean", interval_days=7, expected_minutes=60, assignee_user_id=None, first_due_date=date.today(), admin_id=1)
        task = generate_due_tasks(db, through=date.today())[0]
        assign_task(db, task_id=task.id, assignee_user_id=2, reason="Operations handoff", admin_id=1)
        set_template_active(db, template_id=template.id, active=False, reason="Paused for service", admin_id=1)
        assert db.get(MaintenanceTask, task.id).assignee_user_id == 2
        assert not db.get(type(template), template.id).active
        assert db.scalar(text("SELECT COUNT(*) FROM notifications WHERE recipient_user_id=2 AND event_key LIKE :event_key"), {"event_key": f"maintenance-task:{task.id}:assigned:%"}) == 1
        assert db.scalar(text("SELECT COUNT(*) FROM audit_events WHERE entity_type='maintenance_template' AND action='deactivated'")) == 1


def test_reactivating_a_dormant_template_skips_missed_intervals(app, configured):
    with app.state.database.session() as db:
        first_due = date.today() - timedelta(days=365)
        template = create_template(db, instrument_id=1, title="Dormant clean", interval_days=1, expected_minutes=60, assignee_user_id=2, first_due_date=first_due, admin_id=1)
        set_template_active(db, template_id=template.id, active=False, reason="Instrument mothballed", admin_id=1)
        assert generate_due_tasks(db, through=date.today()) == []
        set_template_active(db, template_id=template.id, active=True, reason="Instrument returned", admin_id=1)
        generated = generate_due_tasks(db, through=date.today())
        assert len(generated) == 1
        assert generated[0].due_date == date.today().isoformat()
        assert db.scalar(text("SELECT COUNT(*) FROM notifications WHERE recipient_user_id=2")) == 1


def test_inactive_templates_do_not_send_overdue_notices_and_reassignments_notify_again(app, configured):
    with app.state.database.session() as db:
        template = create_template(db, instrument_id=1, title="Paused clean", interval_days=7, expected_minutes=60, assignee_user_id=2, first_due_date=date.today() - timedelta(days=1), admin_id=1)
        task = generate_due_tasks(db, through=date.today())[0]
        initial_notices = db.scalar(text("SELECT COUNT(*) FROM notifications WHERE recipient_user_id=2"))
        set_template_active(db, template_id=template.id, active=False, reason="Paused", admin_id=1)
        generate_due_tasks(db, through=date.today())
        assert db.scalar(text("SELECT COUNT(*) FROM notifications WHERE recipient_user_id=2")) == initial_notices
        assign_task(db, task_id=task.id, assignee_user_id=None, reason="Temporary handoff", admin_id=1)
        assign_task(db, task_id=task.id, assignee_user_id=2, reason="Returned to owner", admin_id=1)
        assert db.scalar(text("SELECT COUNT(*) FROM notifications WHERE recipient_user_id=2 AND event_key LIKE :event_key"), {"event_key": f"maintenance-task:{task.id}:assigned:%"}) == 1


def test_report_shows_allotment_context_and_longest_consecutive_usage(app, configured):
    with app.state.database.session() as db:
        pi = PI(name="Allotment PI", email="", active=True); db.add(pi); db.flush()
        project = Project(pi_id=pi.id, code="ALLOT", name="Allotment", max_consecutive_minutes=None, active=True); db.add(project)
        entry = CalendarEntry(instrument_id=1, entry_type="booking", state="confirmed", title="Consecutive", created_by=1, created_at="2026-01-01T00:00:00.000000Z", updated_at="2026-01-01T00:00:00.000000Z"); db.add(entry); db.flush()
        db.add(Booking(calendar_entry_id=entry.id, user_id=2, pi_id=pi.id, project_id=project.id, grant_id=None, experiment_code="ALLOT", expected_minutes=240, sample_category="cells", description="", admin_notes="", override_required=False, requested_start=f"{date.today()}T08:00:00.000000Z", requested_end=f"{date.today()}T12:00:00.000000Z", reserved_minutes=240))
        db.add_all([
            ReservedSlot(calendar_entry_id=entry.id, instrument_id=1, slot_start=f"{date.today()}T08:00:00.000000Z", slot_end=f"{date.today()}T10:00:00.000000Z", nominal_minutes=120, requested_slot_start=f"{date.today()}T08:00:00.000000Z"),
            ReservedSlot(calendar_entry_id=entry.id, instrument_id=1, slot_start=f"{date.today()}T10:00:00.000000Z", slot_end=f"{date.today()}T12:00:00.000000Z", nominal_minutes=120, requested_slot_start=f"{date.today()}T10:00:00.000000Z"),
        ])
        db.add(Allotment(scope_type="project", scope_id=project.id, instrument_id=1, period_start=date.today().isoformat(), period_end=date.today().isoformat(), minutes_allowed=600))
        _rows, _totals, usage, allotments = usage_report(db, start=date.today(), end=date.today())
        item = next(item for item in usage if item["user"] == "Lab Member")
        assert item["longest_consecutive_minutes"] == 240
        assert allotments == [{"scope": "project: ALLOT", "period_start": date.today().isoformat(), "period_end": date.today().isoformat(), "minutes_allowed": 600, "used_minutes": 240, "pending_minutes": 0, "remaining_minutes": 360}]


def test_booking_edit_persists_optional_operational_metadata(app, configured):
    with app.state.database.session() as db:
        pi = PI(name="Metadata PI", email="", active=True); db.add(pi); db.flush()
        project = Project(pi_id=pi.id, code="META", name="Metadata", max_consecutive_minutes=None, active=True); db.add(project)
        entry = CalendarEntry(instrument_id=1, entry_type="booking", state="confirmed", title="Metadata", created_by=1, created_at="2026-01-01T00:00:00.000000Z", updated_at="2026-01-01T00:00:00.000000Z"); db.add(entry); db.flush()
        booking = Booking(calendar_entry_id=entry.id, user_id=2, pi_id=pi.id, project_id=project.id, grant_id=None, experiment_code="META", expected_minutes=60, sample_category="cells", description="", admin_notes="", override_required=False, requested_start=date.today().isoformat(), requested_end=date.today().isoformat(), reserved_minutes=0)
        db.add(booking); db.flush()
        edit_booking(db, booking_id=booking.id, actor=db.get(User, 1), experiment_code="META", expected_minutes=60, sample_category="cells", description="", admin_notes="", column_setup="C18 25 cm", libraries_present=True, gradient_minutes=90, reason="Correct setup")
        updated = db.get(Booking, booking.id)
        assert (updated.column_setup, updated.libraries_present, updated.gradient_minutes) == ("C18 25 cm", True, 90)
        edit_booking(db, booking_id=booking.id, actor=db.get(User, 1), experiment_code="META-2", expected_minutes=60, sample_category="cells", description="", admin_notes="", reason="Rename only")
        assert (updated.column_setup, updated.libraries_present, updated.gradient_minutes) == ("C18 25 cm", True, 90)
