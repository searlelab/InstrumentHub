from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select

from instrumenthub.models import CalendarEntry, ClassBlockDefinition, Instrument, InstrumentClass, ReservedSlot, User
from instrumenthub.scheduling import slots_for_date
from instrumenthub.security import utc_text


def test_admin_dashboard_defaults_to_users_and_has_grouped_navigation(client, configured):
    page = client.get("/admin")

    assert page.status_code == 200
    assert "<h1>Users</h1>" in page.text
    assert 'href="/admin?tab=users"' in page.text
    assert 'href="/admin?tab=instruments"' in page.text
    assert 'aria-current="page"' in page.text
    assert "Manage reference data" not in page.text
    assert "<h1>Projects &amp; grants</h1>" not in page.text


def test_admin_dashboard_renders_only_the_selected_management_panel(client, configured):
    expected = {
        "users": "<h1>Users</h1>",
        "instruments": "<h1>Instruments</h1>",
        "projects": "<h1>Projects &amp; grants</h1>",
        "training": "<h1>Training</h1>",
        "allotments": "<h1>Allotments</h1>",
    }

    for tab, heading in expected.items():
        page = client.get(f"/admin?tab={tab}")
        assert page.status_code == 200
        assert heading in page.text
        assert page.text.count("<h1>") == 1
        assert f'/admin?tab={tab}' in page.text


def test_admin_dashboard_invalid_tab_falls_back_to_users(client, configured):
    page = client.get("/admin?tab=unknown")

    assert page.status_code == 200
    assert "<h1>Users</h1>" in page.text
    assert "<h1>Allotments</h1>" not in page.text


def test_admin_creation_controls_are_collapsed_and_use_balanced_widths(client, configured):
    users = client.get("/admin?tab=users")
    instruments = client.get("/admin?tab=instruments")

    assert 'data-admin-disclosure-toggle aria-expanded="false" aria-controls="admin-add-user"' in users.text
    assert 'disclosure-icon-minus' in users.text
    assert 'class="admin-form-grid"' in users.text
    assert 'data-admin-disclosure-toggle aria-expanded="false" aria-controls="admin-add-instrument"' in instruments.text
    assert 'disclosure-icon-plus' in instruments.text
    assert 'disclosure-icon-minus' in instruments.text
    assert "Access and training policy" in instruments.text
    assert "Calendar Structure" in instruments.text
    assert "Initial booking blocks" not in instruments.text
    assert "Future calendar blocks" in instruments.text
    assert "Create future version" not in instruments.text


def test_operations_backup_button_creates_backup_and_redirects(client, configured, settings):
    page = client.get("/admin/operations")

    assert page.status_code == 200
    assert 'action="/admin/operations/backup"' in page.text
    response = client.post(
        "/admin/operations/backup",
        data={"csrf_token": configured},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/operations?message=Backup+created"
    assert list(settings.backup_dir.glob("instrumenthub-*.sqlite3"))


def test_admin_mutation_redirect_preserves_tab_context(client, configured):
    response = client.post(
        "/admin/pis?tab=projects",
        data={"csrf_token": configured, "name": "Redirect PI", "email": ""},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin?tab=projects&message=PI+created"


def test_calendar_icons_are_served_from_the_shared_icon_directory(client, configured):
    for name in ("sun-horizon.svg", "sun-dim.svg", "cloud-moon.svg", "info.svg", "selection-plus.svg", "minus.svg"):
        response = client.get(f"/static/icons/{name}")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/svg+xml")


def test_instruments_share_the_seeded_class_and_new_instruments_inherit_its_schedule(client, configured, app):
    csrf = client.get("/admin?tab=instruments").text.split('name="csrf_token" value="')[1].split('"')[0]
    response = client.post("/admin/instruments?tab=instruments", data={"csrf_token": csrf, "name": "Inherited schedule"}, follow_redirects=False)

    assert response.status_code == 303
    with app.state.database.session() as db:
        instrument_class = db.scalar(select(InstrumentClass).where(InstrumentClass.name == "Mass Spectrometer"))
        instruments = db.scalars(select(Instrument).order_by(Instrument.id)).all()
        assert instrument_class
        assert all(item.instrument_class_id == instrument_class.id for item in instruments)
        blocks = db.scalars(select(ClassBlockDefinition).where(ClassBlockDefinition.instrument_class_id == instrument_class.id).order_by(ClassBlockDefinition.sort_order)).all()
        assert [(item.name, item.start_time, item.end_time) for item in blocks] == [
            ("Morning", "06:00", "12:00"), ("Afternoon", "12:00", "18:00"), ("Overnight", "18:00", "06:00")
        ]


def test_calendar_structure_future_change_applies_to_every_instrument(client, configured, app):
    future = date.today() + timedelta(days=30)
    response = client.post(
        "/admin/instrument-classes/1/schedule?tab=instruments",
        data={
            "csrf_token": configured,
            "effective_from": future.isoformat(),
            "blocks": "Early,06:00,12:00\nLate,12:00,18:00\nOvernight,18:00,06:00",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    with app.state.database.session() as db:
        for instrument_id in (1, 2):
            assert [slot.name for slot in slots_for_date(db, instrument_id, future, "America/Chicago")] == ["Early", "Late", "Overnight"]
            assert [slot.name for slot in slots_for_date(db, instrument_id, future - timedelta(days=1), "America/Chicago")] == ["Morning", "Afternoon", "Overnight"]


def test_legacy_instrument_schedule_route_remains_instrument_scoped(client, configured, app):
    future = date.today() + timedelta(days=30)
    response = client.post(
        "/admin/instruments/1/blocks?tab=instruments",
        data={"csrf_token": configured, "effective_from": future.isoformat(), "blocks": "Day,09:00,17:00"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    with app.state.database.session() as db:
        assert [slot.name for slot in slots_for_date(db, 1, future, "America/Chicago")] == ["Day"]
        assert [slot.name for slot in slots_for_date(db, 2, future, "America/Chicago")] == ["Morning", "Afternoon", "Overnight"]


def test_calendar_structure_rejects_reservations_on_any_class_instrument(client, configured, app):
    future = date.today() + timedelta(days=30)
    effective_from = date.today() + timedelta(days=10)
    with app.state.database.session() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        entry = CalendarEntry(instrument_id=2, entry_type="booking", state="confirmed", title="Reserved", created_by=admin.id, created_at=utc_text(), updated_at=utc_text())
        db.add(entry)
        db.flush()
        slot = slots_for_date(db, 2, future, "America/Chicago")[0]
        db.add(ReservedSlot(calendar_entry_id=entry.id, instrument_id=2, slot_start=slot.start_text, slot_end=slot.end_text, nominal_minutes=slot.nominal_minutes, requested_slot_start=slot.start_text))

    response = client.post(
        "/admin/instrument-classes/1/schedule?tab=instruments",
        data={"csrf_token": configured, "effective_from": effective_from.isoformat(), "blocks": "Early,06:00,12:00\nLate,12:00,18:00\nOvernight,18:00,06:00"},
    )

    assert response.status_code == 400
    assert "scheduled reservation" in response.text


def test_operations_pages_share_the_admin_shell_and_active_sidebar(client, configured):
    pages = {
        "/admin/schedule": "schedule",
        "/admin/maintenance-tasks": "maintenance",
        "/admin/reports": "reports",
        "/admin/operations": "operations",
    }

    for path, active_tab in pages.items():
        page = client.get(path)
        assert page.status_code == 200
        assert "Administration" in page.text
        assert f'aria-current="page"' in page.text
        assert f'class="admin-nav-link active" href="' in page.text
        assert active_tab in page.text

    calendar = client.get("/calendar")
    assert 'href="/admin/maintenance-tasks"' not in calendar.text
