from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select

from instrumenthub.booking import acquire_hold
from instrumenthub.models import Instrument, User
from instrumenthub.security import utc_text, utcnow
from tests.test_milestone4 import make_booking
from instrumenthub.services import replace_block_definitions
from instrumenthub.scheduling import calendar_data, create_maintenance, month_days, slots_for_date
from tests.conftest import login


def test_month_grid_includes_complete_weeks():
    days = month_days(2026, 8)
    assert len(days) in {35, 42}
    assert days[0].weekday() == 0
    assert days[-1].weekday() == 6


def test_overnight_slots_preserve_nominal_duration_across_dst(app, configured):
    with app.state.database.session() as db:
        instrument = db.scalar(select(Instrument).order_by(Instrument.id))
        slots = slots_for_date(db, instrument.id, date(2026, 11, 1), "America/Chicago")
        overnight = next(slot for slot in slots if slot.name == "Overnight")
        assert overnight.nominal_minutes == 12 * 60
        assert overnight.end.date() == overnight.start.date() + timedelta(days=1)


def test_calendar_displays_initial_blocks_as_past_before_instrument_setup(app, configured):
    previous_month = (date.today().replace(day=1) - timedelta(days=1))
    with app.state.database.session() as db:
        instrument = db.scalar(select(Instrument).order_by(Instrument.id))
        slots = calendar_data(db, instrument.id, previous_month.year, previous_month.month, "America/Chicago")
    assert slots
    assert all(slot["status"] == "past" for slot in slots)


def test_calendar_requires_authentication(client, configured):
    client.post("/logout", data={"csrf_token": configured})
    response = client.get("/api/calendar?instrument_id=1&month=2026-08", follow_redirects=False)
    assert response.status_code == 303


def test_calendar_api_requires_an_individual_instrument(client, configured):
    response = client.get("/api/calendar?month=2026-08")
    assert response.status_code == 400


def test_calendar_defaults_to_all_instruments_overview(client, configured):
    response = client.get("/calendar?month=2026-08")
    assert response.status_code == 200
    assert "Orbitrap A" in response.text
    assert "Orbitrap B" in response.text
    assert "Morning" in response.text
    assert "Afternoon" in response.text
    assert "Overnight" in response.text
    assert "data-calendar-overview" in response.text
    assert "data-selection-calendar" in response.text


def test_calendar_remembers_the_last_selected_instrument(client, configured):
    month = (date.today() + timedelta(days=3)).strftime("%Y-%m")
    selected = client.get(f"/calendar?instrument_id=2&month={month}")
    assert selected.status_code == 200
    assert 'data-instrument="2"' in selected.text
    remembered = client.get(f"/calendar?month={month}")
    assert remembered.status_code == 200
    assert 'data-instrument="2"' in remembered.text
    all_instruments = client.get(f"/calendar?view=all&month={month}")
    assert "data-calendar-overview" in all_instruments.text
    default = client.get(f"/calendar?month={month}")
    assert "data-calendar-overview" in default.text


def test_maintenance_occupies_slots_and_conflicts(client, configured, app):
    target = date.today() + timedelta(days=2)
    data = {"csrf_token": configured, "instrument_id": "1", "start_date": target.isoformat(), "end_date": target.isoformat(), "kind": "routine", "reason": "Calibration"}
    first = client.post("/admin/maintenance", data=data, follow_redirects=False)
    assert first.status_code == 303
    second = client.post("/admin/maintenance", data=data, follow_redirects=False)
    assert second.status_code == 409
    api = client.get(f"/api/calendar?instrument_id=1&month={target.strftime('%Y-%m')}").json()
    occupied = [slot for slot in api["slots"] if slot["date"] == target.isoformat()]
    assert occupied and all(slot["status"] == "maintenance" for slot in occupied)


def test_maintenance_prunes_expired_holds_before_reserving(app, configured):
    target = date.today() + timedelta(days=3)
    with app.state.database.session() as db:
        member = db.scalar(select(User).where(User.username == "member"))
        admin = db.scalar(select(User).where(User.username == "admin"))
        slot = slots_for_date(db, 1, target, "America/Chicago")[0]
        hold, _token, _slots = acquire_hold(db, user=member, instrument_id=1, start_text=slot.start_text, count=1)
        hold.expires_at = utc_text(utcnow() - timedelta(minutes=1))
        entry = create_maintenance(db, instrument_id=1, start_date=target, end_date=target, kind="routine", reason="Calibration", actor_user_id=admin.id)
        assert entry.id


def test_authenticated_member_can_view_booking_contact_later(client, configured):
    # The detail route itself is protected; Milestone 3 supplies booking rows.
    assert client.get("/entries/9999").status_code == 404
    client.post("/logout", data={"csrf_token": configured})
    login(client, "member", "correct-horse-member!")
    assert client.get("/entries/9999").status_code == 404


def test_calendar_includes_booking_handler_initials(client, app, configured):
    _booking_id, _entry_id, start = make_booking(app, days=28)
    month = start[:7]
    page = client.get(f"/calendar?instrument_id=2&month={month}")
    assert "OPS-28 · LM" in page.text
    slots = client.get(f"/api/calendar?instrument_id=2&month={month}").json()["slots"]
    assert next(slot for slot in slots if slot["title"] == "OPS-28")["handler_initials"] == "LM"


def test_overview_links_confirmed_entries_but_not_holds(client, app, configured):
    _booking_id, entry_id, start = make_booking(app, days=14)
    with app.state.database.session() as db:
        member = db.scalar(select(User).where(User.username == "member"))
        slot = slots_for_date(db, 1, date.today() + timedelta(days=15), "America/Chicago")[0]
        hold, _token, _slots = acquire_hold(db, user=member, instrument_id=1, start_text=slot.start_text, count=1)
        hold_entry_id = hold.calendar_entry_id
    page = client.get(f"/calendar?view=all&month={start[:7]}")
    assert f'href="/entries/{entry_id}"' in page.text
    assert "overview-slot confirmed" in page.text
    assert 'title="Orbitrap B —' in page.text
    assert "OPS-14 (Confirmed), Lab Member" in page.text
    assert "LM</span>" not in page.text
    assert f'href="/entries/{hold_entry_id}"' not in page.text
    assert "overview-slot held" in page.text


def test_calendar_icon_assets_and_overview_selection_markup(client, configured):
    page = client.get("/calendar?view=all&month=2026-08")
    assert page.status_code == 200
    assert "data-selection-calendar" in page.text
    assert "data-select-instrument" in page.text
    template = (__import__("pathlib").Path(__file__).parents[1] / "instrumenthub" / "templates" / "calendar.html").read_text(encoding="utf-8")
    assert "sun-horizon.svg" in template
    assert "sun-dim.svg" in template
    assert "cloud-moon.svg" in template
    assert "selection-plus.svg" in page.text
    for name in ("selection-plus.svg", "info.svg", "sun-horizon.svg", "sun-dim.svg", "cloud-moon.svg"):
        response = client.get(f"/static/images/calendar/{name}")
        assert response.status_code == 200
    assert client.get("/static/microscope.svg").status_code == 200


def test_overview_orders_standard_blocks_and_uses_the_correct_time_icons(client, configured):
    page = client.get(f"/calendar?view=all&month={(date.today() + timedelta(days=3)).strftime('%Y-%m')}")
    assert page.status_code == 200
    html = page.text
    assert html.index('alt="Morning"') < html.index('alt="Afternoon"') < html.index('alt="Overnight"')
    afternoon = html.index('alt="Afternoon"')
    overnight = html.index('alt="Overnight"')
    assert "sun-dim.svg" in html[max(0, afternoon - 180):afternoon]
    assert "cloud-moon.svg" in html[max(0, overnight - 180):overnight]


def test_individual_calendar_shows_time_icons_before_block_names(client, configured):
    page = client.get(f"/calendar?instrument_id=1&month={(date.today() + timedelta(days=3)).strftime('%Y-%m')}")
    assert page.status_code == 200
    assert 'class="slot-time-icon morning"' in page.text
    assert 'class="slot-time-icon afternoon"' in page.text
    assert 'class="slot-time-icon night"' in page.text
    assert page.text.index('class="slot-time-icon morning"') < page.text.index("Morning</strong>")


def test_layout_reserves_scrollbar_space_and_aligns_header_to_calendar_view():
    css = (__import__("pathlib").Path(__file__).parents[1] / "instrumenthub" / "static" / "app.css").read_text(encoding="utf-8")
    template = (__import__("pathlib").Path(__file__).parents[1] / "instrumenthub" / "templates" / "base.html").read_text(encoding="utf-8")
    assert "scrollbar-gutter: stable" in css
    assert ".header-content" in css and "max-width: 1220px" in css
    assert '<header><div class="header-content">' in template


def test_block_and_maintenance_services_require_an_active_administrator(app, configured):
    with app.state.database.session() as db:
        member = db.scalar(select(User).where(User.username == "member"))
        with __import__("pytest").raises(ValueError, match="active administrator"):
            replace_block_definitions(db, instrument_id=1, effective_from=date.today() + timedelta(days=30), blocks_text="Day,09:00,17:00", actor_user_id=member.id)
        with __import__("pytest").raises(ValueError, match="active administrator"):
            create_maintenance(db, instrument_id=1, start_date=date.today() + timedelta(days=2), end_date=date.today() + timedelta(days=2), kind="routine", reason="Unauthorized", actor_user_id=member.id)


def test_future_block_version_does_not_reinterpret_current_schedule(app, configured):
    future = date.today() + timedelta(days=30)
    with app.state.database.session() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        replace_block_definitions(db, instrument_id=1, effective_from=future, blocks_text="Day,09:00,17:00", actor_user_id=admin.id)
    with app.state.database.session() as db:
        current = slots_for_date(db, 1, date.today() + timedelta(days=1), "America/Chicago")
        revised = slots_for_date(db, 1, future, "America/Chicago")
        assert current[0].name == "Morning"
        assert [slot.name for slot in revised] == ["Day"]


def test_block_version_rejects_overlap_across_an_overnight_cutover(app, configured):
    future = date.today() + timedelta(days=30)
    with app.state.database.session() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        with __import__("pytest").raises(ValueError, match="effective-date boundary"):
            replace_block_definitions(db, instrument_id=1, effective_from=future, blocks_text="Early,00:00,12:00", actor_user_id=admin.id)


def test_calendar_api_rejects_inactive_instruments_like_the_html_page(app, client, configured):
    with app.state.database.session() as db:
        db.get(Instrument, 1).active = False
    month = (date.today() + timedelta(days=3)).strftime("%Y-%m")
    assert client.get(f"/calendar?instrument_id=1&month={month}").status_code == 404
    assert client.get(f"/api/calendar?instrument_id=1&month={month}").status_code == 404


def test_block_version_rejects_a_cutover_that_would_orphan_booking_slots(app, configured):
    _booking_id, _entry_id, _start = make_booking(app, days=40)
    with app.state.database.session() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        with __import__("pytest").raises(ValueError, match="scheduled reservation"):
            replace_block_definitions(db, instrument_id=2, effective_from=date.today() + timedelta(days=30), blocks_text="Day,09:00,17:00", actor_user_id=admin.id)
