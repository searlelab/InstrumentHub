from __future__ import annotations

import json
import re
from datetime import UTC, date, timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Thread, current_thread
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import event, func, select

import instrumenthub.booking as booking_module
from instrumenthub.booking import acquire_hold, configured_slots, release_hold, release_hold_as_admin, submit_booking
from instrumenthub.management import decide_booking
from instrumenthub.models import Allotment, AppSetting, AuditEvent, Booking, CalendarEntry, Grant, Hold, Instrument, InstrumentTraining, PI, Project, ProjectGrant, ReservedSlot, SessionRecord, User, UserProject
from instrumenthub.rules import allotment_balances, estimate_duration_minutes, evaluate_booking, overall_result
from instrumenthub.scheduling import calendar_data, prune_expired_holds, slots_for_date
from instrumenthub.security import utc_text, utcnow
from instrumenthub.services import replace_block_definitions
from instrumenthub.services import parse_blocks
from tests.conftest import login


def seed_booking_references(app):
    with app.state.database.session() as db:
        pi = PI(name="Dr. Example", email="pi@example.test", active=True)
        db.add(pi); db.flush()
        project = Project(pi_id=pi.id, code="P-001", name="Proteomics", max_consecutive_minutes=None, active=True)
        db.add(project); db.flush()
        member = db.scalar(select(User).where(User.username == "member"))
        db.add(UserProject(user_id=member.id, project_id=project.id))
        return pi.id, project.id


def future_slot(app, instrument_id=2, days=3):
    with app.state.database.session() as db:
        return slots_for_date(db, instrument_id, date.today() + timedelta(days=days), "America/Chicago")[0]


def test_three_block_setup_is_stored_and_selected_in_calendar_order(app, client):
    csrf = login(client)
    response = client.post(
        "/setup",
        data={
            "csrf_token": csrf,
            "timezone": "America/Chicago",
            "instrument_name": "Orbitrap A",
            "instrument_blocks": "Morning,06:00,12:00\nAfternoon,12:00,18:00\nOvernight,18:00,06:00",
            "hold_minutes": "15",
            "tentative_expiration_days": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert [block[0] for block in parse_blocks("Morning,06:00,12:00\nAfternoon,12:00,18:00\nOvernight,18:00,06:00")] == ["Morning", "Afternoon", "Overnight"]
    with app.state.database.session() as db:
        target = date.today() + timedelta(days=7)
        slots = slots_for_date(db, 1, target, "America/Chicago")
        assert [slot.name for slot in slots] == ["Morning", "Afternoon", "Overnight"]
        assert [slot.start.astimezone(ZoneInfo("America/Chicago")).hour for slot in slots] == [6, 12, 18]
        assert [slot.nominal_minutes for slot in slots] == [360, 360, 720]
        admin = db.scalar(select(User).where(User.username == "admin"))
        hold, _token, selected = acquire_hold(db, user=admin, instrument_id=1, start_text=slots[0].start_text, count=3)
        assert hold.id
        assert all(slot.start > utcnow() for slot in selected)
        assert all(previous.end_text == current.start_text for previous, current in zip(selected, selected[1:]))
        assert selected[0].start.astimezone(ZoneInfo("America/Chicago")).hour == 6
        assert selected[-1].end.astimezone(ZoneInfo("America/Chicago")).hour == 6


def test_setup_rejects_block_boundaries_in_a_spring_forward_gap(app, client):
    csrf = login(client)
    response = client.post(
        "/setup",
        data={
            "csrf_token": csrf,
            "timezone": "America/Chicago",
            "instrument_name": "Orbitrap A",
            "instrument_blocks": "Gap,02:30,04:00",
            "hold_minutes": "15",
            "tentative_expiration_days": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "does not exist" in response.text


def test_duration_estimator_adds_qc_per_started_eight_hours():
    assert estimate_duration_minutes(8, 60) == 520
    assert estimate_duration_minutes(9, 60) == 620


def test_rule_engine_blocks_training_and_orders_results(app, configured):
    pi_id, project_id = seed_booking_references(app)
    with app.state.database.session() as db:
        member = db.scalar(select(User).where(User.username == "member"))
        instrument = db.get(Instrument, 1)
        pi, project = db.get(PI, pi_id), db.get(Project, project_id)
        slot = slots_for_date(db, 1, date.today() + timedelta(days=3), "America/Chicago")[0]
        results = evaluate_booking(db, user=member, instrument=instrument, pi=pi, project=project, slots=[slot])
        assert results[0].code == "training_missing"
        assert overall_result(results) == "blocked"


def test_admin_can_revoke_training_and_immediately_block_booking(client, configured, app):
    pi_id, project_id = seed_booking_references(app)
    recorded = client.post("/admin/training", data={"csrf_token": configured, "user_id": 2, "instrument_id": 1}, follow_redirects=False)
    assert recorded.status_code == 303
    revoked = client.post("/admin/training/revoke", data={"csrf_token": configured, "user_id": 2, "instrument_id": 1, "reason": "Training withdrawn"}, follow_redirects=False)
    assert revoked.status_code == 303
    admin = client.get("/admin")
    assert "Training withdrawn" not in admin.text
    assert "Revoked" in admin.text
    with app.state.database.session() as db:
        training = db.get(InstrumentTraining, {"user_id": 2, "instrument_id": 1})
        assert training and training.revoked_at
        assert db.scalar(select(AuditEvent).where(AuditEvent.action == "revoked", AuditEvent.entity_type == "training")).reason == "Training withdrawn"
        member = db.scalar(select(User).where(User.username == "member"))
        slot = slots_for_date(db, 1, date.today() + timedelta(days=3), "America/Chicago")[0]
        results = evaluate_booking(db, user=member, instrument=db.get(Instrument, 1), pi=db.get(PI, pi_id), project=db.get(Project, project_id), slots=[slot])
        assert [result.code for result in results] == ["training_missing"]


def test_rule_engine_reports_occupancy_and_lost_hold(app, configured):
    pi_id, project_id = seed_booking_references(app)
    slot = future_slot(app, days=4)
    with app.state.database.session() as db:
        member = db.scalar(select(User).where(User.username == "member"))
        acquire_hold(db, user=member, instrument_id=2, start_text=slot.start_text, count=1)
    with app.state.database.session() as db:
        member = db.scalar(select(User).where(User.username == "member"))
        results = evaluate_booking(db, user=member, instrument=db.get(Instrument, 2), pi=db.get(PI, pi_id), project=db.get(Project, project_id), slots=[slot], hold_valid=False)
        assert [result.code for result in results] == ["hold_lost", "overlapping_occupancy"]


@pytest.mark.parametrize("orientation", ["existing_starts_first", "candidate_starts_first"])
def test_hold_acquisition_rejects_range_overlap_with_a_different_start(app, configured, orientation):
    slot = future_slot(app, days=8)
    with app.state.database.session() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        entry = CalendarEntry(instrument_id=2, entry_type="maintenance", state="maintenance", title="Range conflict", created_by=admin.id, created_at=utc_text(), updated_at=utc_text())
        db.add(entry); db.flush()
        if orientation == "existing_starts_first":
            start, end = slot.start - timedelta(hours=1), slot.start + timedelta(hours=1)
        else:
            start, end = slot.end - timedelta(hours=1), slot.end + timedelta(hours=1)
        start_text, end_text = utc_text(start.astimezone(UTC)), utc_text(end.astimezone(UTC))
        db.add(ReservedSlot(calendar_entry_id=entry.id, instrument_id=2, slot_start=start_text, slot_end=end_text, nominal_minutes=120, requested_slot_start=start_text))
    with app.state.database.session() as db:
        member = db.scalar(select(User).where(User.username == "member"))
        with pytest.raises(ValueError, match="no longer available"):
            acquire_hold(db, user=member, instrument_id=2, start_text=slot.start_text, count=1)


def test_allotment_excess_requires_approval(app, configured):
    pi_id, project_id = seed_booking_references(app)
    slot = future_slot(app)
    with app.state.database.session() as db:
        db.add(Allotment(scope_type="project", scope_id=project_id, instrument_id=2, period_start=slot.start_text[:10], period_end=slot.start_text[:10], minutes_allowed=10))
    with app.state.database.session() as db:
        results = evaluate_booking(db, user=db.scalar(select(User).where(User.username == "member")), instrument=db.get(Instrument, 2), pi=db.get(PI, pi_id), project=db.get(Project, project_id), slots=[slot])
        assert [r.code for r in results] == ["project_allotment_exceeded"]
        assert overall_result(results) == "approval_required"


def test_allotment_applies_to_the_local_portion_of_a_straddling_booking(app, configured):
    pi_id, project_id = seed_booking_references(app)
    target = date.today() + timedelta(days=12)
    with app.state.database.session() as db:
        slots = slots_for_date(db, 2, target, "America/Chicago")
        selected = configured_slots(db, 2, slots[1].start_text, 2)
        db.add(Allotment(scope_type="project", scope_id=project_id, instrument_id=2, period_start=target.isoformat(), period_end=target.isoformat(), minutes_allowed=10))
    with app.state.database.session() as db:
        results = evaluate_booking(db, user=db.scalar(select(User).where(User.username == "member")), instrument=db.get(Instrument, 2), pi=db.get(PI, pi_id), project=db.get(Project, project_id), slots=selected)
        assert [result.code for result in results] == ["project_allotment_exceeded"]


def test_hold_to_confirmed_booking_and_contact_detail(client, configured, app):
    pi_id, project_id = seed_booking_references(app)
    slot = future_slot(app)
    client.post("/logout", data={"csrf_token": configured})
    csrf = login(client, "member", "correct-horse-member!")
    held = client.post("/book/hold", data={"csrf_token": csrf, "instrument_id": "2", "slot_start": slot.start_text, "count": "1"}, follow_redirects=False)
    assert held.status_code == 303
    hold_id = int(held.headers["location"].split("/")[-1])
    with app.state.database.session() as db:
        session = db.scalar(select(SessionRecord).where(SessionRecord.user_id == 2, SessionRecord.revoked_at.is_(None)))
        session.wizard_json = json.dumps({"experiment_code": "stale experiment"})
    result = client.post("/book/submit", data={"csrf_token": csrf, "hold_id": hold_id, "instrument_id": "2", "pi_id": pi_id, "project_id": project_id, "experiment_code": "EXP-1", "sample_category": "cells"}, follow_redirects=False)
    assert result.status_code == 303
    detail = client.get(result.headers["location"])
    assert "EXP-1" in detail.text
    assert "member@example.test" in detail.text
    assert "Reserved duration" in detail.text
    assert "6 hours nominal time" in detail.text
    with app.state.database.session() as db:
        entry = db.scalar(select(CalendarEntry).where(CalendarEntry.title == "EXP-1"))
        assert entry.state == "confirmed"
        booking = db.scalar(select(Booking).where(Booking.calendar_entry_id == entry.id))
        assert booking.expected_minutes == booking.reserved_minutes == 360
        session = db.scalar(select(SessionRecord).where(SessionRecord.user_id == 2, SessionRecord.revoked_at.is_(None)))
        assert json.loads(session.wizard_json) == {}


def test_new_hold_atomically_supersedes_same_user_hold_on_same_instrument(app, configured):
    target = date.today() + timedelta(days=31)
    with app.state.database.session() as db:
        user = db.scalar(select(User).where(User.username == "member"))
        slots = slots_for_date(db, 2, target, "America/Chicago")
        first, _first_token, _ = acquire_hold(db, user=user, instrument_id=2, start_text=slots[0].start_text, count=1)
        first_id, first_entry_id, first_start = first.id, first.calendar_entry_id, slots[0].start_text
    with app.state.database.session() as db:
        user = db.scalar(select(User).where(User.username == "member"))
        second, _second_token, _ = acquire_hold(db, user=user, instrument_id=2, start_text=slots[1].start_text, count=1)
        second_id = second.id
    with app.state.database.session() as db:
        assert db.get(Hold, second_id) is not None
        assert db.scalar(select(func.count()).select_from(Hold).where(Hold.user_id == 2)) == 1
        assert db.scalar(select(ReservedSlot.id).where(ReservedSlot.slot_start == first_start)) is None
        assert db.scalar(select(ReservedSlot.id).where(ReservedSlot.slot_start == slots[1].start_text)) is not None
        assert db.scalar(select(AuditEvent.id).where(AuditEvent.action == "superseded", AuditEvent.entity_id == str(first_id)))


def test_failed_hold_replacement_rolls_back_and_preserves_prior_hold(app, configured):
    target = date.today() + timedelta(days=32)
    with app.state.database.session() as db:
        member = db.scalar(select(User).where(User.username == "member"))
        admin = db.scalar(select(User).where(User.username == "admin"))
        slots = slots_for_date(db, 2, target, "America/Chicago")
        original, _token, _ = acquire_hold(db, user=member, instrument_id=2, start_text=slots[0].start_text, count=1)
        blocked, _blocked_token, _ = acquire_hold(db, user=admin, instrument_id=2, start_text=slots[1].start_text, count=1)
        original_id, blocked_id = original.id, blocked.id
    with pytest.raises(ValueError, match="no longer available"):
        with app.state.database.session() as db:
            member = db.scalar(select(User).where(User.username == "member"))
            acquire_hold(db, user=member, instrument_id=2, start_text=slots[1].start_text, count=1)
    with app.state.database.session() as db:
        assert db.get(Hold, original_id) is not None and db.get(Hold, blocked_id) is not None


def test_user_can_hold_once_on_each_instrument(app, configured):
    with app.state.database.session() as db:
        user = db.scalar(select(User).where(User.username == "member"))
        first_slot = slots_for_date(db, 1, date.today() + timedelta(days=33), "America/Chicago")[0]
        second_slot = slots_for_date(db, 2, date.today() + timedelta(days=33), "America/Chicago")[0]
        acquire_hold(db, user=user, instrument_id=1, start_text=first_slot.start_text, count=1)
        acquire_hold(db, user=user, instrument_id=2, start_text=second_slot.start_text, count=1)
    with app.state.database.session() as db:
        assert db.scalar(select(func.count()).select_from(Hold).where(Hold.user_id == 2)) == 2


def test_concurrent_same_user_holds_leave_one_live_hold(app, configured):
    with app.state.database.session() as db:
        slots = slots_for_date(db, 2, date.today() + timedelta(days=34), "America/Chicago")
        member_id = db.scalar(select(User.id).where(User.username == "member"))
    barrier = Barrier(2)

    def acquire(slot):
        barrier.wait()
        with app.state.database.session() as db:
            assert not db.in_transaction()
            user = User(id=member_id)
            hold, _token, _ = acquire_hold(db, user=user, instrument_id=2, start_text=slot.start_text, count=1)
            return hold.id

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(acquire, slots[:2]))
    assert len(ids) == 2
    with app.state.database.session() as db:
        assert db.scalar(select(func.count()).select_from(Hold).where(Hold.user_id == 2)) == 1


@pytest.mark.parametrize("release_kind", ["admin", "member"])
def test_release_racing_with_submission_cannot_delete_confirmed_booking(app, configured, monkeypatch, release_kind):
    pi_id, project_id = seed_booking_references(app)
    slot = future_slot(app, days=37)
    with app.state.database.session() as db:
        member = db.scalar(select(User).where(User.username == "member"))
        hold, token, _ = acquire_hold(db, user=member, instrument_id=2, start_text=slot.start_text, count=1)
        hold_id, entry_id, member_id = hold.id, hold.calendar_entry_id, member.id

    submit_locked = Event()
    allow_submit = Event()
    release_attempting_lock = Event()
    original_resolve_hold = booking_module.resolve_hold
    original_serialize = booking_module._serialize_hold_write

    def gated_resolve_hold(*args, **kwargs):
        if current_thread().name == "submit-booking":
            submit_locked.set()
            assert allow_submit.wait(5)
        return original_resolve_hold(*args, **kwargs)

    def observed_serialize(db):
        if current_thread().name == "release-hold":
            release_attempting_lock.set()
        return original_serialize(db)

    monkeypatch.setattr(booking_module, "resolve_hold", gated_resolve_hold)
    monkeypatch.setattr(booking_module, "_serialize_hold_write", observed_serialize)
    outcomes = {}

    def submit():
        try:
            with app.state.database.session() as db:
                booking, _results = submit_booking(
                    db,
                    hold_id=hold_id,
                    token=token,
                    user=User(id=member_id),
                    pi_id=pi_id,
                    project_id=project_id,
                    grant_id=None,
                    experiment_code="RACE-SAFE",
                    expected_minutes=300,
                    injection_count=None,
                    gradient_minutes=None,
                    sample_category="test",
                )
                outcomes["booking_id"] = booking.id
        except Exception as exc:
            outcomes["submit_error"] = exc

    def release():
        try:
            with app.state.database.session() as db:
                if release_kind == "admin":
                    release_hold_as_admin(db, hold_id=hold_id, admin_id=1, reason="Concurrent release")
                else:
                    release_hold(db, hold_id=hold_id, tokens=[token], user_id=member_id)
                outcomes["released"] = True
        except Exception as exc:
            outcomes["release_error"] = exc

    submit_thread = Thread(target=submit, name="submit-booking")
    release_thread = Thread(target=release, name="release-hold")
    submit_thread.start()
    assert submit_locked.wait(5)
    release_thread.start()
    assert release_attempting_lock.wait(5)
    allow_submit.set()
    submit_thread.join(10); release_thread.join(10)
    assert not submit_thread.is_alive() and not release_thread.is_alive()
    assert "submit_error" not in outcomes and outcomes.get("booking_id")
    assert isinstance(outcomes.get("release_error"), ValueError)
    expected_error = "not found or already expired" if release_kind == "admin" else "missing, expired"
    assert expected_error in str(outcomes["release_error"])
    assert "released" not in outcomes
    with app.state.database.session() as db:
        booking = db.scalar(select(Booking).where(Booking.calendar_entry_id == entry_id))
        entry = db.get(CalendarEntry, entry_id)
        rows = db.scalars(select(ReservedSlot).where(ReservedSlot.calendar_entry_id == entry_id)).all()
        assert booking and entry and entry.entry_type == "booking" and entry.state == "confirmed"
        assert len(rows) == 1 and db.get(Hold, hold_id) is None
        assert not db.scalar(select(AuditEvent.id).where(AuditEvent.action == "released", AuditEvent.entity_type == "hold", AuditEvent.entity_id == str(hold_id)))


def test_calendar_prune_racing_with_submission_cannot_delete_confirmed_booking(app, configured, monkeypatch):
    pi_id, project_id = seed_booking_references(app)
    slot = future_slot(app, days=38)
    with app.state.database.session() as db:
        member = db.scalar(select(User).where(User.username == "member"))
        hold, token, _ = acquire_hold(db, user=member, instrument_id=2, start_text=slot.start_text, count=1)
        hold_id, entry_id, member_id = hold.id, hold.calendar_entry_id, member.id
        live_at = utcnow()
        hold.expires_at = utc_text(live_at + timedelta(minutes=1))

    clock = {"now": live_at}
    monkeypatch.setattr("instrumenthub.security.utcnow", lambda: clock["now"])
    submit_validated = Event()
    allow_submit = Event()
    prune_delete_started = Event()
    original_evaluate = booking_module.evaluate_booking

    def gated_evaluate(*args, **kwargs):
        if current_thread().name == "submit-booking":
            submit_validated.set()
            assert allow_submit.wait(5)
        return original_evaluate(*args, **kwargs)

    def observe_prune_delete(_conn, _cursor, statement, _parameters, _context, _executemany):
        if current_thread().name == "calendar-prune" and statement.lstrip().upper().startswith("DELETE FROM CALENDAR_ENTRIES"):
            prune_delete_started.set()

    monkeypatch.setattr(booking_module, "evaluate_booking", gated_evaluate)
    event.listen(app.state.database.engine, "before_cursor_execute", observe_prune_delete)
    outcomes = {}

    def submit():
        try:
            with app.state.database.session() as db:
                booking, _results = submit_booking(
                    db,
                    hold_id=hold_id,
                    token=token,
                    user=User(id=member_id),
                    pi_id=pi_id,
                    project_id=project_id,
                    grant_id=None,
                    experiment_code="PRUNE-RACE-SAFE",
                    expected_minutes=300,
                    injection_count=None,
                    gradient_minutes=None,
                    sample_category="test",
                )
                outcomes["booking_id"] = booking.id
        except Exception as exc:
            outcomes["submit_error"] = exc

    def render_calendar():
        try:
            with app.state.database.session() as db:
                local_date = slot.start.astimezone(ZoneInfo("America/Chicago")).date()
                calendar_data(db, 2, local_date.year, local_date.month, "America/Chicago")
                outcomes["calendar_ok"] = True
        except Exception as exc:
            outcomes["calendar_error"] = exc

    submit_thread = Thread(target=submit, name="submit-booking")
    calendar_thread = Thread(target=render_calendar, name="calendar-prune")
    try:
        submit_thread.start()
        assert submit_validated.wait(5)
        clock["now"] = live_at + timedelta(minutes=2)
        calendar_thread.start()
        assert prune_delete_started.wait(5)
        allow_submit.set()
        submit_thread.join(10); calendar_thread.join(10)
    finally:
        allow_submit.set()
        submit_thread.join(10); calendar_thread.join(10)
        event.remove(app.state.database.engine, "before_cursor_execute", observe_prune_delete)

    assert not submit_thread.is_alive() and not calendar_thread.is_alive()
    assert "submit_error" not in outcomes and outcomes.get("booking_id")
    assert "calendar_error" not in outcomes and outcomes.get("calendar_ok")
    with app.state.database.session() as db:
        booking = db.scalar(select(Booking).where(Booking.calendar_entry_id == entry_id))
        entry_row = db.get(CalendarEntry, entry_id)
        rows = db.scalars(select(ReservedSlot).where(ReservedSlot.calendar_entry_id == entry_id)).all()
        assert booking and entry_row and entry_row.entry_type == "booking" and entry_row.state == "confirmed"
        assert len(rows) == 1 and db.get(Hold, hold_id) is None


def test_admin_can_view_and_release_member_hold(client, configured, app):
    with app.state.database.session() as db:
        member = db.scalar(select(User).where(User.username == "member"))
        slot = slots_for_date(db, 2, date.today() + timedelta(days=35), "America/Chicago")[0]
        hold, _token, _ = acquire_hold(db, user=member, instrument_id=2, start_text=slot.start_text, count=1)
        hold_id, entry_id = hold.id, hold.calendar_entry_id
    page = client.get("/admin/schedule")
    assert "Live booking holds" in page.text and "Lab Member" in page.text
    response = client.post(f"/admin/holds/{hold_id}/release", data={"csrf_token": configured, "reason": "User abandoned wizard"}, follow_redirects=False)
    assert response.status_code == 303
    with app.state.database.session() as db:
        assert db.get(Hold, hold_id) is None and db.get(CalendarEntry, entry_id) is None
        event = db.scalar(select(AuditEvent).where(AuditEvent.action == "released", AuditEvent.entity_id == str(hold_id)))
        assert event.actor_user_id == 1 and event.reason == "User abandoned wizard"


def test_hold_error_uses_configured_duration(client, configured, app):
    with app.state.database.session() as db:
        db.get(AppSetting, "hold_minutes").value = "7"
        member = db.scalar(select(User).where(User.username == "member"))
        slot = slots_for_date(db, 2, date.today() + timedelta(days=36), "America/Chicago")[0]
        acquire_hold(db, user=member, instrument_id=2, start_text=slot.start_text, count=1)
    response = client.post("/book/hold", data={"csrf_token": configured, "instrument_id": "2", "slot_start": slot.start_text, "count": "1"})
    assert response.status_code == 409
    assert "held for 7 minutes" in response.text


def test_booking_wizard_has_live_rules_reference_filters_and_allotment_balances(client, configured, app):
    pi_id, project_id = seed_booking_references(app)
    slot = future_slot(app, days=25)
    with app.state.database.session() as db:
        db.add(Allotment(scope_type="project", scope_id=project_id, instrument_id=2, period_start=slot.start_text[:10], period_end=slot.start_text[:10], minutes_allowed=1000))
    client.post("/logout", data={"csrf_token": configured})
    csrf = login(client, "member", "correct-horse-member!")
    held = client.post("/book/hold", data={"csrf_token": csrf, "instrument_id": "2", "slot_start": slot.start_text, "count": "1"}, follow_redirects=False)
    hold_id = int(held.headers["location"].split("/")[-1])
    form = client.get(held.headers["location"])
    assert "data-booking-wizard" in form.text
    assert "data-rule-preview" in form.text
    assert 'data-pi-id="' in form.text
    assert 'name="expected_minutes"' not in form.text
    assert 'name="injection_count"' not in form.text
    assert "Your selected blocks define the booking duration" in form.text
    assert "reduce contamination or carryover" in form.text
    script = client.get("/static/app.js").text
    assert 'wizard.addEventListener("invalid"' in script
    assert 'optionalDetails.addEventListener("toggle"' in script
    assert client.get("/api/duration-estimate?injection_count=8&gradient_minutes=60").json()["minutes"] == 520
    preview = client.post("/api/rules/preview", json={"hold_id": hold_id, "pi_id": pi_id, "project_id": project_id}, headers={"X-CSRF-Token": csrf})
    assert preview.status_code == 200
    assert preview.json()["allotment_balances"][0]["remaining_minutes"] == 1000
    assert preview.json()["allotment_balances"][0]["remaining_duration"] == "16.67 hours"


def test_member_can_quick_create_audited_booking_references(client, configured, app):
    client.post("/logout", data={"csrf_token": configured})
    csrf = login(client, "member", "correct-horse-member!")
    headers = {"X-CSRF-Token": csrf}
    pi_response = client.post("/api/booking-references/pi", json={"name": "Dr. Quick", "email": "quick@example.test"}, headers=headers)
    assert pi_response.status_code == 201
    pi_id = pi_response.json()["id"]
    project_response = client.post("/api/booking-references/project", json={"pi_id": pi_id, "code": "QUICK-1", "name": "Quick project"}, headers=headers)
    assert project_response.status_code == 201
    project_id = project_response.json()["id"]
    grant_response = client.post("/api/booking-references/grant", json={"project_id": project_id, "number": "R01-QUICK", "name": "Quick grant"}, headers=headers)
    assert grant_response.status_code == 201
    grant_id = grant_response.json()["id"]
    with app.state.database.session() as db:
        assert db.get(UserProject, {"user_id": 2, "project_id": project_id})
        assert db.get(ProjectGrant, {"project_id": project_id, "grant_id": grant_id})
        events = db.scalars(select(AuditEvent).where(AuditEvent.actor_user_id == 2, AuditEvent.action == "created").order_by(AuditEvent.id)).all()
        assert [event.entity_type for event in events[-3:]] == ["pi", "project", "grant"]
    slot = future_slot(app, days=38)
    held = client.post("/book/hold", data={"csrf_token": csrf, "instrument_id": "2", "slot_start": slot.start_text, "count": "1"}, follow_redirects=False)
    hold_id = int(held.headers["location"].split("/")[-1])
    preview = client.post("/api/rules/preview", json={"hold_id": hold_id, "pi_id": pi_id, "project_id": project_id}, headers=headers)
    assert "booking_reference_review" in [result["code"] for result in preview.json()["results"]]
    submitted = client.post("/book/submit", data={"csrf_token": csrf, "hold_id": hold_id, "instrument_id": "2", "pi_id": pi_id, "project_id": project_id, "grant_id": grant_id, "experiment_code": "QUICK-REVIEW", "sample_category": "cells"}, follow_redirects=False)
    assert submitted.status_code == 303
    with app.state.database.session() as db:
        entry = db.scalar(select(CalendarEntry).where(CalendarEntry.title == "QUICK-REVIEW"))
        booking = db.scalar(select(Booking).where(Booking.calendar_entry_id == entry.id))
        assert entry.state == "tentative"
        decide_booking(db, booking_id=booking.id, decision="approved", admin_id=1, reason="Reference data verified")
        validated = db.scalars(select(AuditEvent).where(AuditEvent.action == "validated").order_by(AuditEvent.id)).all()
        assert [(event.entity_type, event.entity_id) for event in validated[-2:]] == [("pi", str(pi_id)), ("project", str(project_id))]
    next_slot = future_slot(app, days=40)
    next_hold = client.post("/book/hold", data={"csrf_token": csrf, "instrument_id": "2", "slot_start": next_slot.start_text, "count": "1"}, follow_redirects=False)
    next_hold_id = int(next_hold.headers["location"].split("/")[-1])
    confirmed = client.post("/book/submit", data={"csrf_token": csrf, "hold_id": next_hold_id, "instrument_id": "2", "pi_id": pi_id, "project_id": project_id, "experiment_code": "QUICK-VALIDATED", "sample_category": "cells"}, follow_redirects=False)
    assert confirmed.status_code == 303
    with app.state.database.session() as db:
        assert db.scalar(select(CalendarEntry).where(CalendarEntry.title == "QUICK-VALIDATED")).state == "confirmed"


def test_quick_create_rejects_duplicates_and_review_request_needs_context(client, configured, app):
    pi_id, project_id = seed_booking_references(app)
    slot = future_slot(app, days=39)
    client.post("/logout", data={"csrf_token": configured})
    csrf = login(client, "member", "correct-horse-member!")
    duplicate = client.post("/api/booking-references/pi", json={"name": "dr. example"}, headers={"X-CSRF-Token": csrf})
    assert duplicate.status_code == 400
    held = client.post("/book/hold", data={"csrf_token": csrf, "instrument_id": "2", "slot_start": slot.start_text, "count": "1"}, follow_redirects=False)
    hold_id = int(held.headers["location"].split("/")[-1])
    result = client.post("/book/submit", data={"csrf_token": csrf, "hold_id": hold_id, "instrument_id": "2", "pi_id": pi_id, "project_id": project_id, "experiment_code": "REVIEW", "sample_category": "single-cell", "override_required": "yes"}, follow_redirects=False)
    assert result.status_code == 303
    assert "Tell+the+scheduler+why+administrator+review+is+needed" in result.headers["location"]
    submitted = client.post("/book/submit", data={"csrf_token": csrf, "hold_id": hold_id, "instrument_id": "2", "pi_id": pi_id, "project_id": project_id, "experiment_code": "REVIEW", "sample_category": "single-cell", "override_required": "yes", "admin_notes": "Additional time is needed for a critical cohort."}, follow_redirects=False)
    assert submitted.status_code == 303
    with app.state.database.session() as db:
        entry = db.scalar(select(CalendarEntry).where(CalendarEntry.title == "REVIEW"))
        assert entry.state == "tentative"


def test_lost_hold_returns_to_calendar_and_retains_wizard_fields(client, configured, app):
    pi_id, project_id = seed_booking_references(app)
    slot = future_slot(app, days=26)
    client.post("/logout", data={"csrf_token": configured})
    csrf = login(client, "member", "correct-horse-member!")
    held = client.post("/book/hold", data={"csrf_token": csrf, "instrument_id": "2", "slot_start": slot.start_text, "count": "1"}, follow_redirects=False)
    hold_id = int(held.headers["location"].split("/")[-1])
    form = client.get(held.headers["location"])
    instrument_id = re.search(r'name="instrument_id" value="(\d+)"', form.text).group(1)
    assert instrument_id == "2"
    with app.state.database.session() as db:
        db.get(Hold, hold_id).expires_at = utc_text(utcnow() - timedelta(minutes=1))
        assert prune_expired_holds(db) == 1
    result = client.post("/book/submit", data={"csrf_token": csrf, "hold_id": hold_id, "instrument_id": instrument_id, "pi_id": pi_id, "project_id": project_id, "experiment_code": "Recover me", "expected_minutes": "300", "injection_count": "8", "gradient_minutes": "60", "sample_category": "cells", "description": "retain details", "admin_notes": "retain notes"}, follow_redirects=False)
    assert result.status_code == 303
    assert result.headers["location"].startswith("/calendar?instrument_id=2")
    assert "Choose+available+blocks+to+continue" in result.headers["location"]
    calendar = client.get(result.headers["location"])
    assert "Choose available blocks to continue." in calendar.text
    with app.state.database.session() as db:
        session = db.scalar(select(SessionRecord).where(SessionRecord.user_id == 2))
        saved = json.loads(session.wizard_json)
        assert saved["experiment_code"] == "Recover me"
        assert saved["injection_count"] == "8"
        assert saved["description"] == "retain details"


def test_expired_hold_submission_returns_lost_hold_instead_of_index_error(app, configured):
    pi_id, project_id = seed_booking_references(app)
    slot = future_slot(app, days=29)
    with app.state.database.session() as db:
        user = db.scalar(select(User).where(User.username == "member"))
        hold, token, _slots = acquire_hold(db, user=user, instrument_id=2, start_text=slot.start_text, count=1)
        hold.expires_at = utc_text(utcnow() - timedelta(minutes=1))
        with __import__("pytest").raises(ValueError, match="hold is missing, expired"):
            submit_booking(db, hold_id=hold.id, token=token, user=user, pi_id=pi_id, project_id=project_id, grant_id=None, experiment_code="EXPIRED", expected_minutes=100, injection_count=None, gradient_minutes=None, sample_category="test")


def test_booking_submission_reloads_and_rejects_a_stale_inactive_user(app, configured):
    pi_id, project_id = seed_booking_references(app)
    slot = future_slot(app, days=30)
    with app.state.database.session() as db:
        user = db.scalar(select(User).where(User.username == "member"))
        hold, token, _slots = acquire_hold(db, user=user, instrument_id=2, start_text=slot.start_text, count=1)
        stale_user = User(id=user.id, username=user.username, display_name=user.display_name, initials=user.initials, password_hash=user.password_hash, role="user", active=True, created_at=user.created_at)
        user.active = False
        with __import__("pytest").raises(ValueError, match="account is inactive"):
            submit_booking(db, hold_id=hold.id, token=token, user=stale_user, pi_id=pi_id, project_id=project_id, grant_id=None, experiment_code="STALE", expected_minutes=100, injection_count=None, gradient_minutes=None, sample_category="test")


def test_ending_allotments_preserves_dates_or_cancels_future_records(client, configured, app):
    _pi_id, project_id = seed_booking_references(app)
    today = date.today()
    with app.state.database.session() as db:
        current = Allotment(scope_type="project", scope_id=project_id, instrument_id=None, period_start=(today - timedelta(days=2)).isoformat(), period_end=(today + timedelta(days=2)).isoformat(), minutes_allowed=100)
        historical = Allotment(scope_type="project", scope_id=project_id, instrument_id=None, period_start=(today - timedelta(days=8)).isoformat(), period_end=(today - timedelta(days=4)).isoformat(), minutes_allowed=100)
        future = Allotment(scope_type="project", scope_id=project_id, instrument_id=None, period_start=(today + timedelta(days=10)).isoformat(), period_end=(today + timedelta(days=20)).isoformat(), minutes_allowed=100)
        db.add_all([current, historical, future]); db.flush()
        current_id, historical_id, future_id = current.id, historical.id, future.id
    for allotment_id in (current_id, historical_id, future_id):
        response = client.post(f"/admin/allotments/{allotment_id}", data={"csrf_token": configured, "action": "end", "period_start": today.isoformat(), "period_end": today.isoformat(), "minutes_allowed": "100", "reason": "Allocation ended"}, follow_redirects=False)
        assert response.status_code == 303
    with app.state.database.session() as db:
        assert db.get(Allotment, current_id).period_end == today.isoformat()
        assert db.get(Allotment, historical_id).period_end == (today - timedelta(days=4)).isoformat()
        assert db.get(Allotment, future_id) is None


def test_allotment_balance_reports_tentative_minutes_as_pending(app, configured):
    pi_id, project_id = seed_booking_references(app)
    slot = future_slot(app, days=27)
    with app.state.database.session() as db:
        db.add(Allotment(scope_type="project", scope_id=project_id, instrument_id=2, period_start=slot.start_text[:10], period_end=slot.start_text[:10], minutes_allowed=1000))
        user = db.scalar(select(User).where(User.username == "member"))
        hold, token, slots = acquire_hold(db, user=user, instrument_id=2, start_text=slot.start_text, count=1)
        submit_booking(db, hold_id=hold.id, token=token, user=user, pi_id=pi_id, project_id=project_id, grant_id=None, experiment_code="PENDING", expected_minutes=120, injection_count=None, gradient_minutes=None, sample_category="test", override_required=True)
    with app.state.database.session() as db:
        balances = allotment_balances(db, pi=db.get(PI, pi_id), project=db.get(Project, project_id), instrument=db.get(Instrument, 2), slots=slots)
        assert balances[0]["consumed_minutes"] == 0
        assert balances[0]["pending_minutes"] == slots[0].nominal_minutes


def test_admin_can_correct_roles_reference_links_allotments_and_instrument_policy(client, configured, app):
    pi_id, project_id = seed_booking_references(app)
    with app.state.database.session() as db:
        grant = Grant(pi_id=pi_id, number="G-MANAGE", name="Manage me", active=True)
        db.add(grant); db.flush()
        db.add(ProjectGrant(project_id=project_id, grant_id=grant.id))
        allotment = Allotment(scope_type="project", scope_id=project_id, instrument_id=2, period_start=date.today().isoformat(), period_end=(date.today() + timedelta(days=30)).isoformat(), minutes_allowed=100)
        db.add(allotment); db.flush()
        grant_id, allotment_id = grant.id, allotment.id
    assert client.post("/admin/users/2/role", data={"csrf_token": configured, "role": "admin", "reason": "Coverage rotation"}, follow_redirects=False).status_code == 303
    assert client.post(f"/admin/references/project/{project_id}/status", data={"csrf_token": configured, "active": "false", "reason": "Project closed"}, follow_redirects=False).status_code == 303
    assert client.post("/admin/project-grants/unlink", data={"csrf_token": configured, "project_id": project_id, "grant_id": grant_id, "reason": "Grant ended"}, follow_redirects=False).status_code == 303
    assert client.post(f"/admin/allotments/{allotment_id}", data={"csrf_token": configured, "action": "update", "period_start": date.today().isoformat(), "period_end": (date.today() + timedelta(days=15)).isoformat(), "minutes_allowed": "240", "reason": "Corrected allocation"}, follow_redirects=False).status_code == 303
    assert client.post("/admin/instruments/2/policy", data={"csrf_token": configured, "active": "false", "training_required": "true", "reason": "Service interval"}, follow_redirects=False).status_code == 303
    with app.state.database.session() as db:
        assert db.get(User, 2).role == "admin"
        assert not db.get(Project, project_id).active
        assert not db.get(ProjectGrant, {"project_id": project_id, "grant_id": grant_id})
        assert db.get(Allotment, allotment_id).minutes_allowed == 240
        assert not db.get(Instrument, 2).active and db.get(Instrument, 2).training_required
        assert db.scalar(select(AuditEvent).where(AuditEvent.action == "unlinked", AuditEvent.entity_type == "project_grant")).reason == "Grant ended"


def test_held_blocks_are_revalidated_before_booking_after_a_block_change(app, configured):
    pi_id, project_id = seed_booking_references(app)
    slot = future_slot(app, days=45)
    with app.state.database.session() as db:
        member = db.scalar(select(User).where(User.username == "member"))
        hold, token, _ = acquire_hold(db, user=member, instrument_id=2, start_text=slot.start_text, count=1)
        hold_id = hold.id
        admin = db.scalar(select(User).where(User.username == "admin"))
        replace_block_definitions(db, instrument_id=2, effective_from=date.today() + timedelta(days=30), blocks_text="Day,09:00,17:00", actor_user_id=admin.id)
    with app.state.database.session() as db:
        member = db.scalar(select(User).where(User.username == "member"))
        with __import__("pytest").raises(ValueError, match="held blocks no longer match"):
            submit_booking(db, hold_id=hold_id, token=token, user=member, pi_id=pi_id, project_id=project_id, grant_id=None, experiment_code="GRID", expected_minutes=300, injection_count=None, gradient_minutes=None, sample_category="test")
        assert db.get(CalendarEntry, hold.calendar_entry_id).entry_type == "hold"


def test_hold_json_status_and_release_share_the_hold_cookie(client, configured, app):
    slot = future_slot(app, days=6)
    created = client.post(
        "/api/holds",
        json={"instrument_id": 2, "slot_start": slot.start_text, "count": 1},
        headers={"X-CSRF-Token": configured},
    )
    assert created.status_code == 201
    assert "Path=/" in created.headers["set-cookie"]
    hold_id = created.json()["id"]
    assert client.get(f"/api/holds/{hold_id}").status_code == 200
    released = client.delete(f"/api/holds/{hold_id}", headers={"X-CSRF-Token": configured})
    assert released.status_code == 204
    assert released.content == b""
    assert client.get(f"/api/holds/{hold_id}").status_code == 404


def test_holds_on_both_instruments_are_independent(client, configured, app):
    first, second = future_slot(app, instrument_id=1, days=7), future_slot(app, instrument_id=2, days=7)
    csrf = login(client, "member", "correct-horse-member!")
    one = client.post("/book/hold", data={"csrf_token": csrf, "instrument_id": "1", "slot_start": first.start_text, "count": "1"}, follow_redirects=False)
    two = client.post("/book/hold", data={"csrf_token": csrf, "instrument_id": "2", "slot_start": second.start_text, "count": "1"}, follow_redirects=False)
    assert one.status_code == 303 and two.status_code == 303
    first_id = int(one.headers["location"].split("/")[-1])
    second_id = int(two.headers["location"].split("/")[-1])
    assert client.get(f"/book/{first_id}").status_code == 200
    assert client.get(f"/book/{second_id}").status_code == 200


def test_hold_can_span_a_month_boundary(app, configured):
    first_of_next = (date.today().replace(day=1) + timedelta(days=45)).replace(day=1)
    month_end = first_of_next - timedelta(days=1)
    with app.state.database.session() as db:
        member = db.scalar(select(User).where(User.username == "member"))
        closing = slots_for_date(db, 2, month_end, "America/Chicago")[-1]
        _hold, _token, slots = acquire_hold(db, user=member, instrument_id=2, start_text=closing.start_text, count=2)
    zone = ZoneInfo("America/Chicago")
    assert slots[0].start.astimezone(zone).date() == month_end
    assert slots[-1].start.astimezone(zone).date() == first_of_next


def test_tentative_booking_still_occupies_slot(app, configured):
    pi_id, project_id = seed_booking_references(app)
    slot = future_slot(app, days=5)
    with app.state.database.session() as db:
        user = db.scalar(select(User).where(User.username == "member"))
        hold, token, _ = acquire_hold(db, user=user, instrument_id=2, start_text=slot.start_text, count=1)
        hold_id = hold.id
    with app.state.database.session() as db:
        user = db.scalar(select(User).where(User.username == "member"))
    with app.state.database.session() as db:
        booking, results = submit_booking(db, hold_id=hold_id, token=token, user=user, pi_id=pi_id, project_id=project_id, grant_id=None, experiment_code="TENT-1", expected_minutes=300, injection_count=None, gradient_minutes=None, sample_category="tissue", override_required=True)
        assert overall_result(results) == "approval_required"
        assert db.get(CalendarEntry, booking.calendar_entry_id).state == "tentative"
    with app.state.database.session() as db:
        other = db.scalar(select(User).where(User.username == "admin"))
        try:
            acquire_hold(db, user=other, instrument_id=2, start_text=slot.start_text, count=1)
        except ValueError as exc:
            assert "no longer available" in str(exc)
        else:
            raise AssertionError("Tentative booking did not protect its slot")


def test_first_hold_wins_for_competing_users(app, configured):
    slot = future_slot(app, days=7)
    with app.state.database.session() as db:
        first = db.scalar(select(User).where(User.username == "member"))
    with app.state.database.session() as db:
        acquire_hold(db, user=first, instrument_id=2, start_text=slot.start_text, count=1)
    with app.state.database.session() as db:
        second = db.scalar(select(User).where(User.username == "admin"))
    with app.state.database.session() as db:
        try:
            acquire_hold(db, user=second, instrument_id=2, start_text=slot.start_text, count=1)
        except ValueError:
            pass
        else:
            raise AssertionError("Competing hold unexpectedly succeeded")


def test_simultaneous_holds_have_exactly_one_winner(app, configured):
    slot = future_slot(app, days=9)
    with app.state.database.session() as db:
        users = [db.scalar(select(User).where(User.username == name)) for name in ("member", "admin")]

    def compete(user):
        try:
            with app.state.database.session() as db:
                acquire_hold(db, user=user, instrument_id=2, start_text=slot.start_text, count=1)
            return "won"
        except ValueError:
            return "lost"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(compete, users))
    assert sorted(outcomes) == ["lost", "won"]


def test_configured_tentative_expiration_cancels_and_releases(app, configured):
    pi_id, project_id = seed_booking_references(app)
    slot = future_slot(app, days=13)
    with app.state.database.session() as db:
        user = db.scalar(select(User).where(User.username == "member"))
        hold, token, _ = acquire_hold(db, user=user, instrument_id=2, start_text=slot.start_text, count=1); hold_id = hold.id
    with app.state.database.session() as db:
        user = db.scalar(select(User).where(User.username == "member"))
    with app.state.database.session() as db:
        booking, _ = submit_booking(db, hold_id=hold_id, token=token, user=user, pi_id=pi_id, project_id=project_id, grant_id=None, experiment_code="EXPIRE", expected_minutes=100, injection_count=None, gradient_minutes=None, sample_category="test", override_required=True)
        entry = db.get(CalendarEntry, booking.calendar_entry_id)
        entry.created_at = utc_text(utcnow() - timedelta(days=3))
        db.get(AppSetting, "tentative_expiration_days").value = "1"
        entry_id = entry.id
    with app.state.database.session() as db:
        calendar_data(db, 2, date.today().year, date.today().month, "America/Chicago")
    with app.state.database.session() as db:
        assert db.get(CalendarEntry, entry_id).state == "canceled"
        assert not db.scalars(select(ReservedSlot).where(ReservedSlot.calendar_entry_id == entry_id)).all()
