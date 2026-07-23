from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from threading import Barrier

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from instrumenthub.booking import acquire_hold, submit_booking
from instrumenthub.management import apply_schedule_draft, cancel_booking, decide_booking, edit_booking, preview_outage, preview_resume, recover_unknown_outage, transition_booking
from instrumenthub.models import Allotment, ApprovalDecision, AuditEvent, BlockDefinition, Booking, CalendarEntry, Hold, Instrument, Maintenance, PI, Project, ReservedSlot, ScheduleChangeDraft, User, UserProject
from instrumenthub.scheduling import create_maintenance, slots_for_date
from instrumenthub.security import utc_text


def make_booking(app, *, days: int, tentative: bool = False, count: int = 1):
    with app.state.database.session() as db:
        pi = db.scalar(select(PI).where(PI.name == "Operations PI"))
        if not pi:
            pi = PI(name="Operations PI", email="", active=True); db.add(pi); db.flush()
            project = Project(pi_id=pi.id, code="OPS", name="Operations Project", active=True); db.add(project); db.flush()
            member = db.scalar(select(User).where(User.username == "member")); db.add(UserProject(user_id=member.id, project_id=project.id))
        else:
            project = db.scalar(select(Project).where(Project.pi_id == pi.id))
        user = db.scalar(select(User).where(User.username == "member"))
        slot = slots_for_date(db, 2, date.today() + timedelta(days=days), "America/Chicago")[0]
        hold, token, _ = acquire_hold(db, user=user, instrument_id=2, start_text=slot.start_text, count=count)
        hold_id, pi_id, project_id = hold.id, pi.id, project.id
    with app.state.database.session() as db:
        user = db.scalar(select(User).where(User.username == "member"))
    with app.state.database.session() as db:
        booking, _ = submit_booking(db, hold_id=hold_id, token=token, user=user, pi_id=pi_id, project_id=project_id, grant_id=None, experiment_code=f"OPS-{days}", expected_minutes=300, injection_count=None, gradient_minutes=None, sample_category="test", override_required=tentative)
        return booking.id, booking.calendar_entry_id, slot.start_text


def admin_user(app):
    with app.state.database.session() as db:
        return db.scalar(select(User).where(User.username == "admin"))


def member_user(app):
    with app.state.database.session() as db:
        return db.scalar(select(User).where(User.username == "member"))


def test_tentative_approval_and_lifecycle_are_audited(app, configured):
    booking_id, entry_id, _ = make_booking(app, days=10, tentative=True)
    admin = admin_user(app)
    with app.state.database.session() as db:
        decide_booking(db, booking_id=booking_id, decision="approved", admin_id=admin.id, reason="Capacity exception accepted")
        assert db.get(CalendarEntry, entry_id).state == "confirmed"
    with app.state.database.session() as db:
        transition_booking(db, booking_id=booking_id, new_state="active", admin_id=admin.id, reason="Run started")
        transition_booking(db, booking_id=booking_id, new_state="completed", admin_id=admin.id, reason="Run completed")
        assert db.get(CalendarEntry, entry_id).state == "completed"
        assert db.scalar(select(ApprovalDecision).where(ApprovalDecision.booking_id == booking_id)).reason == "Capacity exception accepted"


def test_invalid_lifecycle_transition_is_rejected(app, configured):
    booking_id, _entry_id, _ = make_booking(app, days=11)
    admin = admin_user(app)
    with app.state.database.session() as db:
        with pytest.raises(ValueError, match="Cannot transition"):
            transition_booking(db, booking_id=booking_id, new_state="completed", admin_id=admin.id, reason="Invalid shortcut")


@pytest.mark.parametrize("state", ["active", "paused"])
def test_active_and_paused_bookings_require_explicit_transition_before_reschedule(app, configured, state):
    booking_id, entry_id, _ = make_booking(app, days=62)
    with app.state.database.session() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        entry = db.get(CalendarEntry, entry_id)
        entry.state = state
        target = slots_for_date(db, 2, date.today() + timedelta(days=63), "America/Chicago")[0]
        with pytest.raises(ValueError, match="explicit lifecycle transitions"):
            edit_booking(
                db,
                booking_id=booking_id,
                actor=admin,
                experiment_code="NO-IMPLICIT-DEMOTION",
                expected_minutes=300,
                sample_category="test",
                description="",
                admin_notes="",
                new_start=target.start_text,
                reason="Attempted move",
            )
        assert entry.state == state


def test_outage_paused_booking_cannot_resume_before_instrument_recovery(app, configured):
    booking_id, entry_id, _ = make_booking(app, days=64)
    outage_date = date.today() + timedelta(days=64)
    with app.state.database.session() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        outage = preview_outage(db, instrument_id=2, start_date=outage_date, end_date=None, reason="Fault", admin_id=admin.id)
        apply_schedule_draft(db, draft_id=outage.id, admin_id=admin.id)
    with app.state.database.session() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        with pytest.raises(ValueError, match=f"Booking {booking_id}.*unresolved outage"):
            transition_booking(db, booking_id=booking_id, new_state="active", admin_id=admin.id, reason="Premature resume")
        assert db.get(CalendarEntry, entry_id).state == "paused"
        resume = preview_resume(db, instrument_id=2, restart_date=outage_date + timedelta(days=2), admin_id=admin.id, reason="Repaired")
        assert resume.kind == "resume"


def test_resume_reconciliation_names_the_booking_with_an_invalid_state(app, configured):
    booking_id, entry_id, _ = make_booking(app, days=65)
    outage_date = date.today() + timedelta(days=65)
    with app.state.database.session() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        outage = preview_outage(db, instrument_id=2, start_date=outage_date, end_date=None, reason="Fault", admin_id=admin.id)
        apply_schedule_draft(db, draft_id=outage.id, admin_id=admin.id)
    with app.state.database.session() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        entry = db.get(CalendarEntry, entry_id)
        entry.state = "active"
        with pytest.raises(ValueError, match=f"Schedule entry {entry_id}.*OPS-65.*state 'active'"):
            preview_resume(db, instrument_id=2, restart_date=outage_date + timedelta(days=2), admin_id=admin.id, reason="Repaired")
        assert db.get(Booking, booking_id).queue_order is not None


@pytest.mark.parametrize("initial_state", ["tentative", "confirmed", "paused"])
def test_every_lifecycle_cancellation_path_releases_reserved_slots(app, configured, initial_state):
    booking_id, entry_id, _ = make_booking(app, days=11)
    with app.state.database.session() as db:
        entry = db.get(CalendarEntry, entry_id)
        entry.state = initial_state
        admin = db.scalar(select(User).where(User.username == "admin"))
        transition_booking(db, booking_id=booking_id, new_state="canceled", admin_id=admin.id, reason="Lifecycle cancellation")
        assert db.get(CalendarEntry, entry_id).state == "canceled"
        assert not db.scalars(select(ReservedSlot).where(ReservedSlot.calendar_entry_id == entry_id)).all()


def test_tentative_lifecycle_rejection_releases_reserved_slots(app, configured):
    booking_id, entry_id, _ = make_booking(app, days=11, tentative=True)
    with app.state.database.session() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        transition_booking(db, booking_id=booking_id, new_state="rejected", admin_id=admin.id, reason="Request declined")
        assert db.get(CalendarEntry, entry_id).state == "rejected"
        assert not db.scalars(select(ReservedSlot).where(ReservedSlot.calendar_entry_id == entry_id)).all()
        assert db.scalar(select(ApprovalDecision).where(ApprovalDecision.booking_id == booking_id)).decision == "rejected"


def test_tentative_booking_cannot_bypass_the_approval_decision(app, configured):
    booking_id, _entry_id, _ = make_booking(app, days=12, tentative=True)
    with app.state.database.session() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        with pytest.raises(ValueError, match="Cannot transition"):
            transition_booking(db, booking_id=booking_id, new_state="confirmed", admin_id=admin.id, reason="Bypass approval")


def test_privileged_schedule_services_require_an_active_administrator(app, configured):
    booking_id, _entry_id, _start = make_booking(app, days=13)
    with app.state.database.session() as db:
        member = db.scalar(select(User).where(User.username == "member"))
        with pytest.raises(ValueError, match="active administrator"):
            transition_booking(db, booking_id=booking_id, new_state="active", admin_id=member.id, reason="Unauthorized")
        with pytest.raises(ValueError, match="active administrator"):
            decide_booking(db, booking_id=booking_id, decision="approved", admin_id=member.id, reason="Unauthorized")
        with pytest.raises(ValueError, match="active administrator"):
            preview_outage(db, instrument_id=2, start_date=date.today() + timedelta(days=13), end_date=None, reason="Unauthorized", admin_id=member.id)
        with pytest.raises(ValueError, match="active administrator"):
            preview_resume(db, instrument_id=2, restart_date=date.today() + timedelta(days=13), admin_id=member.id, reason="Unauthorized")
        with pytest.raises(ValueError, match="active administrator"):
            recover_unknown_outage(db, instrument_id=2, admin_id=member.id, reason="Unauthorized")
        with pytest.raises(ValueError, match="active administrator"):
            apply_schedule_draft(db, draft_id=9999, admin_id=member.id)


def test_owner_can_cancel_future_booking_and_slots_are_released(app, configured):
    booking_id, entry_id, _ = make_booking(app, days=12)
    member = member_user(app)
    with app.state.database.session() as db:
        cancel_booking(db, booking_id=booking_id, actor=member)
        assert db.get(CalendarEntry, entry_id).state == "canceled"
        assert not db.scalars(select(ReservedSlot).where(ReservedSlot.calendar_entry_id == entry_id)).all()


def test_admin_cancellation_requires_a_reason(app, configured):
    booking_id, _entry_id, _ = make_booking(app, days=13)
    with app.state.database.session() as db:
        with pytest.raises(ValueError, match="administrator reason"):
            cancel_booking(db, booking_id=booking_id, actor=db.scalar(select(User).where(User.username == "admin")), reason="")


def test_inactive_or_demoted_administrators_cannot_cancel_or_edit(app, configured):
    booking_id, entry_id, original_start = make_booking(app, days=19)
    with app.state.database.session() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        admin.active = False
        with pytest.raises(ValueError, match="active user"):
            cancel_booking(db, booking_id=booking_id, actor=admin, reason="Unauthorized")
        assert db.get(CalendarEntry, entry_id).state == "confirmed"
    with app.state.database.session() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        admin.active, admin.role = True, "user"
        stale_admin = User(id=admin.id, role="admin", active=True)
        with pytest.raises(ValueError, match="not eligible"):
            edit_booking(db, booking_id=booking_id, actor=stale_admin, experiment_code="Unauthorized", expected_minutes=300, sample_category="test", description="", admin_notes="", new_start=original_start, reason="Unauthorized")
        assert db.get(Booking, booking_id).experiment_code == "OPS-19"


def test_owner_edit_cannot_change_to_past_or_overwrite_admin_notes(app, configured):
    booking_id, entry_id, original_start = make_booking(app, days=15)
    with app.state.database.session() as db:
        booking = db.get(Booking, booking_id); booking.admin_notes = "Do not replace"
        member = db.scalar(select(User).where(User.username == "member"))
        for definition in db.scalars(select(BlockDefinition).where(BlockDefinition.instrument_id == 2)):
            definition.effective_from = (date.today() - timedelta(days=2)).isoformat()
        past = slots_for_date(db, 2, date.today() - timedelta(days=1), "America/Chicago")[0]
        with pytest.raises(ValueError, match="past"):
            edit_booking(db, booking_id=booking_id, actor=member, experiment_code="Changed", expected_minutes=300, sample_category="test", description="", admin_notes="attacker text", new_start=past.start_text)
    with app.state.database.session() as db:
        assert db.get(Booking, booking_id).admin_notes == "Do not replace"
        assert db.scalar(select(ReservedSlot.slot_start).where(ReservedSlot.calendar_entry_id == entry_id)) == original_start


def test_time_edit_does_not_double_count_its_own_allotment_usage(app, configured):
    booking_id, entry_id, original_start = make_booking(app, days=16)
    with app.state.database.session() as db:
        booking = db.get(Booking, booking_id)
        db.add(Allotment(scope_type="project", scope_id=booking.project_id, instrument_id=2, period_start=original_start[:10], period_end=original_start[:10], minutes_allowed=720))
        target = next(slot for slot in slots_for_date(db, 2, date.today() + timedelta(days=16), "America/Chicago") if slot.start_text != original_start)
        member = db.scalar(select(User).where(User.username == "member"))
        edit_booking(db, booking_id=booking_id, actor=member, experiment_code="OPS-16", expected_minutes=300, sample_category="test", description="", admin_notes="", new_start=target.start_text)
    with app.state.database.session() as db:
        assert db.get(CalendarEntry, entry_id).state == "confirmed"


def test_time_edit_prunes_an_expired_hold_from_the_target_slot(app, configured):
    booking_id, entry_id, _ = make_booking(app, days=18)
    member = member_user(app)
    with app.state.database.session() as db:
        target = slots_for_date(db, 2, date.today() + timedelta(days=19), "America/Chicago")[0]
        hold, _token, _slots = acquire_hold(db, user=db.get(User, member.id), instrument_id=2, start_text=target.start_text, count=1)
        hold.expires_at = "2000-01-01T00:00:00.000000Z"
        hold_id = hold.id
        hold_entry_id = hold.calendar_entry_id
    with app.state.database.session() as db:
        edit_booking(db, booking_id=booking_id, actor=member, experiment_code="OPS-18", expected_minutes=300, sample_category="test", description="", admin_notes="", new_start=target.start_text)
    with app.state.database.session() as db:
        assert db.get(Hold, hold_id) is None
        assert db.get(CalendarEntry, hold_entry_id) is None
        assert db.scalar(select(ReservedSlot.slot_start).where(ReservedSlot.calendar_entry_id == entry_id)) == target.start_text


def test_moving_a_multiblock_booking_preserves_its_size_and_audits_the_interval(app, client, configured):
    booking_id, entry_id, original_start = make_booking(app, days=41, count=3)
    with app.state.database.session() as db:
        original_rows = db.scalars(select(ReservedSlot).where(ReservedSlot.calendar_entry_id == entry_id).order_by(ReservedSlot.slot_start)).all()
        original_end = original_rows[-1].slot_end
        target = slots_for_date(db, 2, date.today() + timedelta(days=45), "America/Chicago")[0]
    page = client.get(f"/bookings/{booking_id}/edit")
    assert page.status_code == 200
    assert 'name="block_count" min="1" max="100" value="3"' in page.text
    response = client.post(f"/bookings/{booking_id}/edit", data={
        "csrf_token": configured, "experiment_code": "OPS-41", "expected_minutes": "300",
        "sample_category": "test", "description": "", "admin_notes": "", "new_start": target.start_text,
        "reason": "Move intact",
    }, follow_redirects=False)
    assert response.status_code == 303
    with app.state.database.session() as db:
        rows = db.scalars(select(ReservedSlot).where(ReservedSlot.calendar_entry_id == entry_id).order_by(ReservedSlot.slot_start)).all()
        booking = db.get(Booking, booking_id)
        event = db.scalar(select(AuditEvent).where(AuditEvent.action == "updated", AuditEvent.entity_type == "booking", AuditEvent.entity_id == str(booking_id)).order_by(AuditEvent.id.desc()))
        before, after = json.loads(event.before_json), json.loads(event.after_json)
        assert len(rows) == 3
        assert booking.requested_start == rows[0].slot_start and booking.requested_end == rows[-1].slot_end
        assert {key: before[key] for key in ("start", "end", "block_count")} == {"start": original_start, "end": original_end, "block_count": 3}
        assert {key: after[key] for key in ("start", "end", "block_count")} == {"start": rows[0].slot_start, "end": rows[-1].slot_end, "block_count": 3}


def test_terminal_bookings_cannot_be_canceled_or_rescheduled_by_admin(app, configured):
    completed_id, completed_entry_id, _ = make_booking(app, days=46)
    canceled_id, canceled_entry_id, canceled_start = make_booking(app, days=47)
    admin = admin_user(app)
    with app.state.database.session() as db:
        db.get(CalendarEntry, completed_entry_id).state = "completed"
        cancel_booking(db, booking_id=canceled_id, actor=admin, reason="Withdrawn")
    with app.state.database.session() as db:
        target = slots_for_date(db, 2, date.today() + timedelta(days=48), "America/Chicago")[0]
        with pytest.raises(ValueError, match="completed booking cannot be canceled"):
            cancel_booking(db, booking_id=completed_id, actor=admin, reason="Too late")
        with pytest.raises(ValueError, match="canceled booking cannot be rescheduled"):
            edit_booking(db, booking_id=canceled_id, actor=admin, experiment_code="OPS-47", expected_minutes=300, sample_category="test", description="", admin_notes="", new_start=target.start_text, reason="Invalid move")
        assert not db.scalars(select(ReservedSlot).where(ReservedSlot.calendar_entry_id == canceled_entry_id)).all()


@pytest.mark.parametrize("terminal_state", ["canceled", "rejected"])
def test_admin_can_correct_terminal_booking_metadata_without_schedule_controls(app, client, configured, terminal_state):
    booking_id, entry_id, _ = make_booking(app, days=54, tentative=terminal_state == "rejected")
    admin = admin_user(app)
    with app.state.database.session() as db:
        if terminal_state == "canceled":
            cancel_booking(db, booking_id=booking_id, actor=admin, reason="Withdrawn")
        else:
            decide_booking(db, booking_id=booking_id, decision="rejected", admin_id=admin.id, reason="Declined")
    page = client.get(f"/bookings/{booking_id}/edit")
    assert page.status_code == 200
    assert 'name="new_start"' not in page.text
    assert 'name="block_count"' not in page.text
    response = client.post(f"/bookings/{booking_id}/edit", data={
        "csrf_token": configured, "experiment_code": f"CORRECTED-{terminal_state}", "expected_minutes": "300",
        "sample_category": "test", "description": "Corrected metadata", "admin_notes": "Historical correction",
        "reason": "Correct terminal metadata",
    }, follow_redirects=False)
    assert response.status_code == 303
    with app.state.database.session() as db:
        booking = db.get(Booking, booking_id)
        assert booking.experiment_code == f"CORRECTED-{terminal_state}"
        assert db.get(CalendarEntry, entry_id).state == terminal_state
        assert not db.scalars(select(ReservedSlot).where(ReservedSlot.calendar_entry_id == entry_id)).all()
        event = db.scalar(select(AuditEvent).where(AuditEvent.action == "updated", AuditEvent.entity_type == "booking", AuditEvent.entity_id == str(booking_id)).order_by(AuditEvent.id.desc()))
        after = json.loads(event.after_json)
        assert after["block_count"] == 0 and after["start"] is None and after["end"] is None


def test_owner_cannot_edit_after_a_known_outage_alters_the_schedule(app, configured):
    booking_id, _entry_id, _ = make_booking(app, days=49)
    admin = admin_user(app)
    with app.state.database.session() as db:
        draft = preview_outage(db, instrument_id=2, start_date=date.today() + timedelta(days=49), end_date=date.today() + timedelta(days=49), reason="Service", admin_id=admin.id)
        draft_id = draft.id
    with app.state.database.session() as db:
        apply_schedule_draft(db, draft_id=draft_id, admin_id=admin.id)
    with app.state.database.session() as db:
        member = db.scalar(select(User).where(User.username == "member"))
        with pytest.raises(ValueError, match="not eligible for owner editing"):
            edit_booking(db, booking_id=booking_id, actor=member, experiment_code="OWNER-EDIT", expected_minutes=300, sample_category="test", description="", admin_notes="")
        assert db.get(Booking, booking_id).administratively_altered


def test_concurrent_approval_and_rejection_produce_one_final_decision(app, configured):
    booking_id, entry_id, _ = make_booking(app, days=50, tentative=True)
    start = Barrier(2)

    def decide(decision):
        start.wait()
        try:
            with app.state.database.session() as db:
                admin_id = db.scalar(select(User.id).where(User.username == "admin"))
                decide_booking(db, booking_id=booking_id, decision=decision, admin_id=admin_id, reason=f"Concurrent {decision}")
            return "ok"
        except ValueError:
            return "rejected-as-stale"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(decide, ("approved", "rejected")))
    assert sorted(results) == ["ok", "rejected-as-stale"]
    with app.state.database.session() as db:
        assert db.scalar(select(func.count()).select_from(ApprovalDecision).where(ApprovalDecision.booking_id == booking_id)) == 1
        assert db.get(CalendarEntry, entry_id).state in {"confirmed", "rejected"}


def test_database_rejects_two_final_decisions_for_one_approval_round(app, configured):
    booking_id, _entry_id, _ = make_booking(app, days=51, tentative=True)
    with app.state.database.session() as db:
        admin_id = db.scalar(select(User.id).where(User.username == "admin"))
        db.add_all([
            ApprovalDecision(booking_id=booking_id, administrator_id=admin_id, decision="approved", reason="First", decided_at=utc_text(), approval_round=1),
            ApprovalDecision(booking_id=booking_id, administrator_id=admin_id, decision="rejected", reason="Second", decided_at=utc_text(), approval_round=1),
        ])
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()


def test_a_material_reschedule_opens_a_new_approval_round(app, configured):
    booking_id, entry_id, _ = make_booking(app, days=52, tentative=True)
    admin = admin_user(app)
    with app.state.database.session() as db:
        decide_booking(db, booking_id=booking_id, decision="approved", admin_id=admin.id, reason="Initial approval")
    with app.state.database.session() as db:
        target = slots_for_date(db, 2, date.today() + timedelta(days=53), "America/Chicago")[0]
        admin = db.get(User, admin.id)
        edit_booking(db, booking_id=booking_id, actor=admin, experiment_code="OPS-52", expected_minutes=300, sample_category="test", description="", admin_notes="", new_start=target.start_text, reason="Material move")
        assert db.get(CalendarEntry, entry_id).state == "tentative"
        assert db.get(Booking, booking_id).approval_round == 2
    with app.state.database.session() as db:
        decide_booking(db, booking_id=booking_id, decision="approved", admin_id=admin.id, reason="Reapproval")
    with app.state.database.session() as db:
        rounds = db.scalars(select(ApprovalDecision.approval_round).where(ApprovalDecision.booking_id == booking_id).order_by(ApprovalDecision.approval_round)).all()
        assert rounds == [1, 2]


def test_unknown_outage_recovery_cancels_slotless_maintenance(app, configured):
    admin = admin_user(app)
    maintenance_date = date.today() + timedelta(days=55)
    with app.state.database.session() as db:
        unrelated = CalendarEntry(instrument_id=1, entry_type="maintenance", state="canceled", title="Historical entry", created_by=admin.id, created_at=utc_text(), updated_at=utc_text())
        db.add(unrelated); db.flush()
        maintenance = create_maintenance(db, instrument_id=2, start_date=maintenance_date, end_date=maintenance_date, kind="routine", reason="Planned service", actor_user_id=admin.id)
        maintenance_entry_id = maintenance.id
        maintenance_row_id = db.scalar(select(Maintenance.id).where(Maintenance.calendar_entry_id == maintenance_entry_id))
        assert maintenance_row_id != maintenance_entry_id
        outage = preview_outage(db, instrument_id=2, start_date=maintenance_date - timedelta(days=1), end_date=None, reason="Unknown fault", admin_id=admin.id)
        outage_id = outage.id
    with app.state.database.session() as db:
        apply_schedule_draft(db, draft_id=outage_id, admin_id=admin.id)
        assert db.get(CalendarEntry, maintenance_entry_id).state == "maintenance"
        assert not db.scalars(select(ReservedSlot).where(ReservedSlot.calendar_entry_id == maintenance_entry_id)).all()
    with app.state.database.session() as db:
        recover_unknown_outage(db, instrument_id=2, admin_id=admin.id, reason="Cannot reconstruct")
    with app.state.database.session() as db:
        assert db.get(CalendarEntry, maintenance_entry_id).state == "canceled"
        assert db.scalar(select(AuditEvent.id).where(AuditEvent.action == "canceled", AuditEvent.entity_type == "maintenance", AuditEvent.entity_id == str(maintenance_entry_id)))
        assert not db.scalar(select(AuditEvent.id).where(AuditEvent.action == "canceled", AuditEvent.entity_type == "maintenance", AuditEvent.entity_id == str(maintenance_row_id)))


def test_calendar_drag_hint_is_visible_for_keyboard_focus():
    css = (__import__("pathlib").Path(__file__).parents[1] / "instrumenthub" / "static" / "app.css").read_text(encoding="utf-8")
    assert ".slot.available:focus-within .drag-hint" in css
    assert ".drag-hint:focus-visible" in css


def test_known_outage_preview_is_nonmutating_then_shifts(app, configured):
    booking_id, entry_id, original_start = make_booking(app, days=14)
    admin = admin_user(app); outage_date = date.today() + timedelta(days=14)
    with app.state.database.session() as db:
        draft = preview_outage(db, instrument_id=2, start_date=outage_date, end_date=outage_date, reason="Pump replacement", admin_id=admin.id)
        draft_id = draft.id
        assert db.scalar(select(ReservedSlot.slot_start).where(ReservedSlot.calendar_entry_id == entry_id)) == original_start
    with app.state.database.session() as db:
        apply_schedule_draft(db, draft_id=draft_id, admin_id=admin.id)
    with app.state.database.session() as db:
        shifted = db.scalar(select(ReservedSlot.slot_start).where(ReservedSlot.calendar_entry_id == entry_id))
        assert shifted != original_start
        assert db.get(Booking, booking_id)


def test_outage_preview_renders_slot_mapping_and_application_audits_it(app, client, configured):
    booking_id, entry_id, original_start = make_booking(app, days=17)
    outage_date = date.today() + timedelta(days=17)
    with app.state.database.session() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        booking = db.get(Booking, booking_id)
        draft = preview_outage(db, instrument_id=2, start_date=outage_date, end_date=outage_date, reason="Pump replacement", admin_id=admin.id)
        draft_id = draft.id
        requested_start = booking.requested_start

    page = client.get("/admin/schedule")
    assert page.status_code == 200
    assert "OPS-17" in page.text
    assert original_start in page.text
    assert "Proposed block" in page.text

    with app.state.database.session() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        apply_schedule_draft(db, draft_id=draft_id, admin_id=admin.id)
    with app.state.database.session() as db:
        event = db.scalar(select(AuditEvent).where(AuditEvent.action == "applied", AuditEvent.entity_type == "schedule_change", AuditEvent.entity_id == draft_id))
        recorded = json.loads(event.after_json)
        item = next(value for value in recorded["items"] if value["entry_id"] == entry_id)
        assert item["old_start"] == original_start
        assert item["requested_start"] == requested_start
        assert item["new_start"] != original_start


def test_known_outage_apply_rechecks_intervening_conflicts(app, configured):
    _booking_id, entry_id, original_start = make_booking(app, days=20)
    with app.state.database.session() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        draft = preview_outage(db, instrument_id=2, start_date=date.today() + timedelta(days=20), end_date=date.today() + timedelta(days=20), reason="Pump replacement", admin_id=admin.id)
        payload = json.loads(draft.payload_json)
        target = next(item for item in payload["items"] if item["entry_id"] == entry_id)
        conflict = CalendarEntry(instrument_id=2, entry_type="maintenance", state="maintenance", title="Intervening maintenance", created_by=admin.id, created_at=utc_text(), updated_at=utc_text())
        db.add(conflict); db.flush()
        db.add(ReservedSlot(calendar_entry_id=conflict.id, instrument_id=2, slot_start=target["new_start"], slot_end=target["new_end"], nominal_minutes=target["nominal_minutes"], requested_slot_start=target["new_start"]))
        draft_id = draft.id
    with app.state.database.session() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        with pytest.raises(ValueError, match="now conflicts"):
            apply_schedule_draft(db, draft_id=draft_id, admin_id=admin.id)
        assert db.scalar(select(ReservedSlot.slot_start).where(ReservedSlot.calendar_entry_id == entry_id)) == original_start


def test_known_outage_apply_rejects_a_preview_staled_by_cancellation(app, configured):
    booking_id, _entry_id, _ = make_booking(app, days=21)
    with app.state.database.session() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        draft = preview_outage(db, instrument_id=2, start_date=date.today() + timedelta(days=21), end_date=date.today() + timedelta(days=21), reason="Pump replacement", admin_id=admin.id)
        cancel_booking(db, booking_id=booking_id, actor=admin, reason="Experiment withdrawn")
        with pytest.raises(ValueError, match="Regenerate the preview"):
            apply_schedule_draft(db, draft_id=draft.id, admin_id=admin.id)
        assert db.get(ScheduleChangeDraft, draft.id).status == "draft"


@pytest.mark.parametrize("end_date", [date.today() + timedelta(days=23), None])
def test_outage_apply_rejects_reused_slot_ids_after_cancellation(app, configured, end_date):
    booking_id, _entry_id, _ = make_booking(app, days=23)
    with app.state.database.session() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        draft = preview_outage(db, instrument_id=2, start_date=date.today() + timedelta(days=23), end_date=end_date, reason="Pump replacement", admin_id=admin.id)
        source_slot_id = json.loads(draft.payload_json)["items"][0]["slot_id"]
        cancel_booking(db, booking_id=booking_id, actor=admin, reason="Experiment withdrawn")
    replacement_id, replacement_entry_id, replacement_start = make_booking(app, days=26)
    with app.state.database.session() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        replacement_slot = db.scalar(select(ReservedSlot).where(ReservedSlot.calendar_entry_id == replacement_entry_id))
        assert replacement_slot.id == source_slot_id
        with pytest.raises(ValueError, match="Regenerate the preview"):
            apply_schedule_draft(db, draft_id=draft.id, admin_id=admin.id)
        assert db.get(ScheduleChangeDraft, draft.id).status == "draft"
        assert db.scalar(select(ReservedSlot.slot_start).where(ReservedSlot.calendar_entry_id == replacement_entry_id)) == replacement_start
        assert db.get(Booking, replacement_id)


def test_resume_apply_rechecks_intervening_occupancy(app, configured):
    _booking_id, _entry_id, _ = make_booking(app, days=22)
    outage_date = date.today() + timedelta(days=22)
    with app.state.database.session() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        outage = preview_outage(db, instrument_id=2, start_date=outage_date, end_date=None, reason="Fault", admin_id=admin.id)
        apply_schedule_draft(db, draft_id=outage.id, admin_id=admin.id)
        resume = preview_resume(db, instrument_id=2, restart_date=outage_date + timedelta(days=4), admin_id=admin.id, reason="Repaired")
        item = json.loads(resume.payload_json)["items"][0]
        conflict = CalendarEntry(instrument_id=2, entry_type="maintenance", state="maintenance", title="Intervening maintenance", created_by=admin.id, created_at=utc_text(), updated_at=utc_text())
        db.add(conflict); db.flush()
        db.add(ReservedSlot(calendar_entry_id=conflict.id, instrument_id=2, slot_start=item["new_start"], slot_end=item["new_end"], nominal_minutes=item["nominal_minutes"], requested_slot_start=item["new_start"]))
        with pytest.raises(ValueError, match="reconstructed blocks are now occupied"):
            apply_schedule_draft(db, draft_id=resume.id, admin_id=admin.id)
        assert db.get(ScheduleChangeDraft, resume.id).status == "draft"


@pytest.mark.parametrize("cancel_after_preview", [False, True])
def test_resume_omits_a_booking_canceled_during_an_unknown_outage(app, configured, cancel_after_preview):
    canceled_booking_id, canceled_entry_id, _ = make_booking(app, days=30)
    remaining_booking_id, remaining_entry_id, _ = make_booking(app, days=31)
    outage_date = date.today() + timedelta(days=29)
    admin = admin_user(app)
    with app.state.database.session() as db:
        outage = preview_outage(db, instrument_id=2, start_date=outage_date, end_date=None, reason="Instrument fault", admin_id=admin.id)
        outage_id = outage.id
    with app.state.database.session() as db:
        apply_schedule_draft(db, draft_id=outage_id, admin_id=admin.id)
    if not cancel_after_preview:
        with app.state.database.session() as db:
            cancel_booking(db, booking_id=canceled_booking_id, actor=admin, reason="Experiment withdrawn")
    with app.state.database.session() as db:
        resume = preview_resume(db, instrument_id=2, restart_date=outage_date + timedelta(days=5), admin_id=admin.id, reason="Instrument repaired")
        resume_id = resume.id
        if not cancel_after_preview:
            assert canceled_entry_id not in {item["entry_id"] for item in json.loads(resume.payload_json)["items"]}
    if cancel_after_preview:
        with app.state.database.session() as db:
            cancel_booking(db, booking_id=canceled_booking_id, actor=admin, reason="Experiment withdrawn")
    with app.state.database.session() as db:
        apply_schedule_draft(db, draft_id=resume_id, admin_id=admin.id)
    with app.state.database.session() as db:
        canceled = db.get(Booking, canceled_booking_id)
        assert db.get(CalendarEntry, canceled_entry_id).state == "canceled"
        assert canceled.queue_order is None and canceled.pre_pause_state is None
        assert db.get(CalendarEntry, remaining_entry_id).state == "confirmed"
        assert db.scalars(select(ReservedSlot).where(ReservedSlot.calendar_entry_id == remaining_entry_id)).all()
        assert db.get(Booking, remaining_booking_id).queue_order is None
        assert not db.get(Instrument, 2).unavailable


@pytest.mark.parametrize("expired_hold_after_preview", [False, True])
def test_known_outage_prunes_expired_holds_during_preview_and_apply(app, configured, expired_hold_after_preview):
    target_date = date.today() + timedelta(days=33)
    admin = admin_user(app)
    if expired_hold_after_preview:
        with app.state.database.session() as db:
            draft = preview_outage(db, instrument_id=2, start_date=target_date, end_date=target_date, reason="Service", admin_id=admin.id)
            draft_id = draft.id
    with app.state.database.session() as db:
        target = slots_for_date(db, 2, target_date, "America/Chicago")[0]
        hold, _token, _slots = acquire_hold(db, user=db.get(User, admin.id), instrument_id=2, start_text=target.start_text, count=1)
        hold.expires_at = "2000-01-01T00:00:00.000000Z"
        hold_id = hold.id
    if not expired_hold_after_preview:
        with app.state.database.session() as db:
            draft = preview_outage(db, instrument_id=2, start_date=target_date, end_date=target_date, reason="Service", admin_id=admin.id)
            draft_id = draft.id
    with app.state.database.session() as db:
        apply_schedule_draft(db, draft_id=draft_id, admin_id=admin.id)
    with app.state.database.session() as db:
        assert db.get(Hold, hold_id) is None
        assert db.get(ScheduleChangeDraft, draft_id).status == "applied"


def test_resume_preview_requires_a_reason_at_service_and_route_boundaries(app, client, configured):
    with app.state.database.session() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        with pytest.raises(ValueError, match="resume reason"):
            preview_resume(db, instrument_id=2, restart_date=date.today() + timedelta(days=5), admin_id=admin.id, reason="   ")
    response = client.post("/admin/outages/resume-preview", data={"csrf_token": configured, "instrument_id": "2", "restart_date": (date.today() + timedelta(days=5)).isoformat(), "reason": "   "})
    assert response.status_code == 409
    assert "resume reason" in response.text


def test_resume_preserves_slots_that_precede_a_partially_affected_booking(app, configured):
    _booking_id, entry_id, _ = make_booking(app, days=24, count=3)
    outage_date = date.today() + timedelta(days=25)
    with app.state.database.session() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        before = db.scalars(select(ReservedSlot).where(ReservedSlot.calendar_entry_id == entry_id).order_by(ReservedSlot.slot_start)).all()
        preserved_start = before[1].slot_start
        outage = preview_outage(db, instrument_id=2, start_date=outage_date, end_date=None, reason="Fault", admin_id=admin.id)
        assert [item["old_start"] for item in json.loads(outage.payload_json)["items"]] == [before[2].slot_start]
        apply_schedule_draft(db, draft_id=outage.id, admin_id=admin.id)
        assert [row.slot_start for row in db.scalars(select(ReservedSlot).where(ReservedSlot.calendar_entry_id == entry_id))] == [before[0].slot_start, preserved_start]
        resume = preview_resume(db, instrument_id=2, restart_date=outage_date + timedelta(days=4), admin_id=admin.id, reason="Repaired")
        resume_id = resume.id
    with app.state.database.session() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        apply_schedule_draft(db, draft_id=resume_id, admin_id=admin.id)
        starts = [row.slot_start for row in db.scalars(select(ReservedSlot).where(ReservedSlot.calendar_entry_id == entry_id).order_by(ReservedSlot.slot_start))]
        assert preserved_start in starts
        assert len(starts) == 3


def test_schedule_apply_acquires_immediate_transaction_before_admin_lookup(app, configured, monkeypatch):
    import instrumenthub.management as management

    seen_transaction_states = []
    original = management.require_active_admin

    def recording_require_active_admin(db, admin_id):
        seen_transaction_states.append(db.in_transaction())
        return original(db, admin_id)

    monkeypatch.setattr(management, "require_active_admin", recording_require_active_admin)
    with app.state.database.session() as db:
        with pytest.raises(ValueError, match="missing"):
            management.apply_schedule_draft(db, draft_id=9999, admin_id=1)
    assert seen_transaction_states == [True]


def test_unknown_outage_recovery_acquires_immediate_transaction_before_admin_lookup(app, configured, monkeypatch):
    import instrumenthub.management as management

    seen_transaction_states = []
    original = management.require_active_admin

    def recording_require_active_admin(db, admin_id):
        seen_transaction_states.append(db.in_transaction())
        return original(db, admin_id)

    monkeypatch.setattr(management, "require_active_admin", recording_require_active_admin)
    with app.state.database.session() as db:
        with pytest.raises(ValueError, match="unresolved outage"):
            management.recover_unknown_outage(db, instrument_id=2, admin_id=1, reason="Probe")
    assert seen_transaction_states == [True]


def test_outage_starts_after_completed_work_on_its_first_date(app, configured):
    _booking_id, entry_id, _ = make_booking(app, days=27)
    outage_date = date.today() + timedelta(days=27)
    with app.state.database.session() as db:
        entry = db.get(CalendarEntry, entry_id)
        entry.state = "completed"
        admin = db.scalar(select(User).where(User.username == "admin"))
        draft = preview_outage(db, instrument_id=2, start_date=outage_date, end_date=outage_date + timedelta(days=1), reason="Failed after run", admin_id=admin.id)
        payload = json.loads(draft.payload_json)
        completed_start = db.scalar(select(ReservedSlot.slot_start).where(ReservedSlot.calendar_entry_id == entry_id))
        assert all(item["start"] > completed_start for item in payload["outage_slots"])


def test_unknown_outage_pauses_and_resume_reconstructs(app, configured):
    booking_id, entry_id, _ = make_booking(app, days=18)
    admin = admin_user(app); outage_date = date.today() + timedelta(days=18)
    with app.state.database.session() as db:
        draft = preview_outage(db, instrument_id=2, start_date=outage_date, end_date=None, reason="Awaiting service engineer", admin_id=admin.id); draft_id = draft.id
    with app.state.database.session() as db:
        apply_schedule_draft(db, draft_id=draft_id, admin_id=admin.id)
    with app.state.database.session() as db:
        assert db.get(CalendarEntry, entry_id).state == "paused"
        assert db.get(Instrument, 2).unavailable
        assert not db.scalars(select(ReservedSlot).where(ReservedSlot.calendar_entry_id == entry_id)).all()
    with app.state.database.session() as db:
        resume = preview_resume(db, instrument_id=2, restart_date=outage_date + timedelta(days=5), admin_id=admin.id, reason="Engineer cleared instrument"); resume_id = resume.id
    with app.state.database.session() as db:
        apply_schedule_draft(db, draft_id=resume_id, admin_id=admin.id)
    with app.state.database.session() as db:
        assert db.get(CalendarEntry, entry_id).state == "confirmed"
        assert not db.get(Instrument, 2).unavailable
        assert db.scalars(select(ReservedSlot).where(ReservedSlot.calendar_entry_id == entry_id)).all()
        assert db.get(Booking, booking_id).queue_order is None
        with pytest.raises(ValueError, match="unresolved outage"):
            preview_resume(db, instrument_id=2, restart_date=outage_date + timedelta(days=6), admin_id=admin.id, reason="Duplicate resume")


def test_only_one_unknown_outage_can_be_applied(app, configured):
    _booking_id, _entry_id, _ = make_booking(app, days=55)
    admin = admin_user(app); outage_date = date.today() + timedelta(days=5)
    with app.state.database.session() as db:
        first = preview_outage(db, instrument_id=2, start_date=outage_date, end_date=None, reason="Fault A", admin_id=admin.id)
        second = preview_outage(db, instrument_id=2, start_date=outage_date + timedelta(days=1), end_date=None, reason="Fault B", admin_id=admin.id)
        first_id, second_id = first.id, second.id
    with app.state.database.session() as db:
        apply_schedule_draft(db, draft_id=first_id, admin_id=admin.id)
    with app.state.database.session() as db:
        with pytest.raises(ValueError, match="already unresolved"):
            apply_schedule_draft(db, draft_id=second_id, admin_id=admin.id)
        with pytest.raises(ValueError, match="already unresolved"):
            preview_outage(db, instrument_id=2, start_date=outage_date + timedelta(days=2), end_date=None, reason="Fault C", admin_id=admin.id)


def test_resume_reconstructs_all_legacy_unresolved_outage_drafts(app, configured):
    _booking_id, booking_entry_id, booking_start = make_booking(app, days=60)
    with app.state.database.session() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        booking_entry = db.get(CalendarEntry, booking_entry_id)
        for row in db.scalars(select(ReservedSlot).where(ReservedSlot.calendar_entry_id == booking_entry_id)):
            db.delete(row)
        booking_entry.state = "paused"
        maintenance_entry = CalendarEntry(instrument_id=2, entry_type="maintenance", state="maintenance", title="Legacy maintenance", created_by=admin.id, created_at=utc_text(), updated_at=utc_text())
        db.add(maintenance_entry); db.flush()
        outage_a = slots_for_date(db, 2, date.today() + timedelta(days=5), "America/Chicago")[0]
        outage_b = slots_for_date(db, 2, date.today() + timedelta(days=6), "America/Chicago")[0]
        maintenance_slot = slots_for_date(db, 2, date.today() + timedelta(days=70), "America/Chicago")[0]
        for index, (outage_slot, item) in enumerate(((outage_a, {"slot_id": 1001, "entry_id": booking_entry_id, "old_start": booking_start, "old_end": booking_start, "nominal_minutes": 720}), (outage_b, {"slot_id": 1002, "entry_id": maintenance_entry.id, "old_start": maintenance_slot.start_text, "old_end": maintenance_slot.end_text, "nominal_minutes": maintenance_slot.nominal_minutes})), start=1):
            db.add(ScheduleChangeDraft(instrument_id=2, kind="unknown_outage", status="applied", reason=f"legacy {index}", payload_json=json.dumps({"outage_slots": [{"start": outage_slot.start_text, "end": outage_slot.end_text, "nominal_minutes": outage_slot.nominal_minutes}], "items": [item]}), created_by=admin.id, created_at=utc_text(), applied_at=utc_text()))
        db.get(Instrument, 2).unavailable = True
    with app.state.database.session() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        resume = preview_resume(db, instrument_id=2, restart_date=date.today() + timedelta(days=10), admin_id=admin.id, reason="Legacy recovery")
        items = json.loads(resume.payload_json)["items"]
        assert {item["entry_id"] for item in items} == {booking_entry_id, maintenance_entry.id}


def test_block_aligned_outage_shift_handles_partial_day_blocks(app, configured):
    with app.state.database.session() as db:
        for definition in db.scalars(select(BlockDefinition).where(BlockDefinition.instrument_id == 2)):
            db.delete(definition)
        db.flush()
        today = date.today().isoformat()
        db.add_all([
            BlockDefinition(instrument_id=2, name="AM", start_time="08:00", end_time="12:00", effective_from=today, sort_order=0),
            BlockDefinition(instrument_id=2, name="PM", start_time="13:00", end_time="17:00", effective_from=today, sort_order=1),
        ])
        pi = PI(name="Partial PI", email="", active=True); db.add(pi); db.flush()
        project = Project(pi_id=pi.id, code="PARTIAL", name="Partial", active=True); db.add(project); db.flush()
        user = db.scalar(select(User).where(User.username == "member")); db.add(UserProject(user_id=user.id, project_id=project.id))
        target_day = date.today() + timedelta(days=25)
        pm = slots_for_date(db, 2, target_day, "America/Chicago")[1]
        hold, token, _ = acquire_hold(db, user=user, instrument_id=2, start_text=pm.start_text, count=1)
        hold_id = hold.id
    with app.state.database.session() as db:
        user = db.scalar(select(User).where(User.username == "member"))
        booking, _ = submit_booking(db, hold_id=hold_id, token=token, user=user, pi_id=pi.id, project_id=project.id, grant_id=None, experiment_code="PARTIAL", expected_minutes=240, injection_count=None, gradient_minutes=None, sample_category="test")
        admin = db.scalar(select(User).where(User.username == "admin"))
        draft = preview_outage(db, instrument_id=2, start_date=target_day, end_date=target_day, reason="Partial blocks", admin_id=admin.id)
        item = next(item for item in __import__("json").loads(draft.payload_json)["items"] if item["entry_id"] == booking.calendar_entry_id)
        assert item["new_start"] == slots_for_date(db, 2, target_day + timedelta(days=1), "America/Chicago")[1].start_text
        assert item["nominal_minutes"] == 240
        assert item["source_nominal_minutes"] == item["target_nominal_minutes"] == 240


def test_known_outage_rejects_a_block_cutover_that_cannot_preserve_duration(app, configured):
    cutover = date.today() + timedelta(days=35)
    _booking_id, _entry_id, _start = make_booking(app, days=34)
    with app.state.database.session() as db:
        for definition in db.scalars(select(BlockDefinition).where(BlockDefinition.instrument_id == 2)):
            definition.effective_to = (cutover - timedelta(days=1)).isoformat()
        db.add(BlockDefinition(instrument_id=2, name="Short day", start_time="09:00", end_time="17:00", effective_from=cutover.isoformat(), sort_order=0))
        admin = db.scalar(select(User).where(User.username == "admin"))
        with pytest.raises(ValueError, match="cannot preserve an affected entry's duration"):
            preview_outage(db, instrument_id=2, start_date=cutover - timedelta(days=1), end_date=cutover - timedelta(days=1), reason="Cutover outage", admin_id=admin.id)


def test_partial_day_blocks_allow_sequential_multiblock_holds(app, configured):
    with app.state.database.session() as db:
        for definition in db.scalars(select(BlockDefinition).where(BlockDefinition.instrument_id == 2)):
            db.delete(definition)
        db.flush()
        today = date.today().isoformat()
        db.add_all([
            BlockDefinition(instrument_id=2, name="AM", start_time="08:00", end_time="12:00", effective_from=today, sort_order=0),
            BlockDefinition(instrument_id=2, name="PM", start_time="13:00", end_time="17:00", effective_from=today, sort_order=1),
        ])
        target = date.today() + timedelta(days=26)
        first = slots_for_date(db, 2, target, "America/Chicago")[0]
        user = db.scalar(select(User).where(User.username == "member"))
        _hold, _token, selected = acquire_hold(db, user=user, instrument_id=2, start_text=first.start_text, count=2)
        assert len(selected) == 2
        assert selected[0].end_text < selected[1].start_text


def test_block_aligned_outage_shift_handles_spring_dst(app, configured):
    target_day = date(2027, 3, 10)
    with app.state.database.session() as db:
        pi = PI(name="DST PI", email="", active=True); db.add(pi); db.flush()
        project = Project(pi_id=pi.id, code="DST", name="DST", active=True); db.add(project); db.flush()
        user = db.scalar(select(User).where(User.username == "member")); db.add(UserProject(user_id=user.id, project_id=project.id))
        slot = slots_for_date(db, 2, target_day, "America/Chicago")[0]
        hold, token, _ = acquire_hold(db, user=user, instrument_id=2, start_text=slot.start_text, count=1)
        hold_id = hold.id
    with app.state.database.session() as db:
        user = db.scalar(select(User).where(User.username == "member"))
        booking, _ = submit_booking(db, hold_id=hold_id, token=token, user=user, pi_id=pi.id, project_id=project.id, grant_id=None, experiment_code="DST", expected_minutes=720, injection_count=None, gradient_minutes=None, sample_category="test")
        admin = db.scalar(select(User).where(User.username == "admin"))
        draft = preview_outage(db, instrument_id=2, start_date=date(2027, 3, 1), end_date=date(2027, 3, 7), reason="DST alignment", admin_id=admin.id)
        item = next(item for item in __import__("json").loads(draft.payload_json)["items"] if item["entry_id"] == booking.calendar_entry_id)
        assert item["new_start"] == slots_for_date(db, 2, target_day + timedelta(days=7), "America/Chicago")[0].start_text


def test_unresolved_outage_blocks_block_changes_and_has_audited_recovery(app, configured):
    booking_id, entry_id, _ = make_booking(app, days=60)
    outage_date = date.today() + timedelta(days=5)
    with app.state.database.session() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        outage = preview_outage(db, instrument_id=2, start_date=outage_date, end_date=None, reason="Investigation", admin_id=admin.id)
        outage_id = outage.id
    with app.state.database.session() as db:
        apply_schedule_draft(db, draft_id=outage_id, admin_id=admin.id)
        outage_entry = db.scalar(select(CalendarEntry).join(Maintenance, Maintenance.calendar_entry_id == CalendarEntry.id).where(CalendarEntry.entry_type == "outage", Maintenance.known_duration.is_(False)))
        outage_entry_id = outage_entry.id
        assert db.scalar(select(func.count()).select_from(ReservedSlot).where(ReservedSlot.calendar_entry_id == outage_entry_id)) > 0
    with app.state.database.session() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        from instrumenthub.services import replace_block_definitions
        with pytest.raises(ValueError, match="unknown outage"):
            replace_block_definitions(db, instrument_id=2, effective_from=date.today() + timedelta(days=10), blocks_text="Day,09:00,17:00", actor_user_id=admin.id)
        assert recover_unknown_outage(db, instrument_id=2, admin_id=admin.id, reason="Configuration cannot be reconciled") == 1
    with app.state.database.session() as db:
        assert not db.get(Instrument, 2).unavailable
        assert db.get(CalendarEntry, entry_id).state == "canceled"
        booking = db.get(Booking, booking_id)
        assert booking and booking.queue_order is None and booking.pre_pause_state is None
        assert db.get(CalendarEntry, outage_entry_id).state == "canceled"
        assert db.scalar(select(func.count()).select_from(ReservedSlot).where(ReservedSlot.calendar_entry_id == outage_entry_id)) == 0
        assert db.scalar(select(AuditEvent.id).where(AuditEvent.action == "canceled", AuditEvent.entity_type == "outage", AuditEvent.entity_id == str(outage_entry_id)))


def test_missing_booking_transition_returns_conflict(client, configured):
    response = client.post(
        "/admin/bookings/999999/transition",
        data={"csrf_token": configured, "new_state": "active", "reason": "Missing record"},
    )
    assert response.status_code == 409
    assert "Booking not found" in response.text


def test_known_outage_handles_a_booking_more_than_120_days_ahead(app, configured):
    _booking_id, _entry_id, _start = make_booking(app, days=300)
    outage_date = date.today() + timedelta(days=300)
    with app.state.database.session() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        draft = preview_outage(db, instrument_id=2, start_date=outage_date, end_date=outage_date, reason="Long horizon", admin_id=admin.id)
        assert __import__("json").loads(draft.payload_json)["items"]
