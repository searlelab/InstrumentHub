from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from instrumenthub.models import Allotment, PI, Project
from instrumenthub.time_display import accounting_hours_value, format_accounting_duration, parse_accounting_hours


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [
        (0, "0 hours"),
        (360, "6 hours"),
        (1440, "1 day"),
        (1800, "1 day 6 hours"),
        (510, "8.5 hours"),
        (-1440, "-1 day"),
    ],
)
def test_accounting_duration_formatter_uses_hours_and_days(minutes, expected):
    assert format_accounting_duration(minutes) == expected


def test_accounting_hours_parser_rounds_to_existing_minute_storage():
    assert parse_accounting_hours("6") == 360
    assert parse_accounting_hours("8.5") == 510
    assert accounting_hours_value(900) == "15"
    with pytest.raises(ValueError, match="positive number of hours"):
        parse_accounting_hours("0", label="Allotment")


def test_admin_accounting_inputs_accept_hours_and_render_hours_days(client, configured, app):
    with app.state.database.session() as db:
        pi = PI(name="Hours PI", email="", active=True)
        db.add(pi)
        db.flush()
        project = Project(pi_id=pi.id, code="HOURS", name="Hours project", max_consecutive_minutes=None, active=True)
        db.add(project)
        db.flush()
        project_id = project.id

    response = client.post(
        "/admin/allotments",
        data={
            "csrf_token": configured,
            "scope_type": "project",
            "scope_id": str(project_id),
            "period_start": date.today().isoformat(),
            "period_end": date.today().isoformat(),
            "hours_allowed": "30",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with app.state.database.session() as db:
        allotment = db.scalar(select(Allotment).where(Allotment.scope_id == project_id))
        assert allotment.minutes_allowed == 1800

    page = client.get("/admin?tab=allotments")
    assert 'name="hours_allowed"' in page.text
    assert "1 day 6 hours" in page.text
    assert "Minutes" not in page.text
    projects_page = client.get("/admin?tab=projects")
    assert "Max consecutive hours" in projects_page.text

    reports = client.get(f"/admin/reports?start={date.today()}&end={date.today()}")
    assert "Reserved time" in reports.text
    assert "Longest consecutive time" in reports.text
    assert "minutes" not in reports.text.lower()
    csv = client.get(f"/admin/reports.csv?start={date.today()}&end={date.today()}")
    assert csv.status_code == 200
    assert "duration,duration_source" in csv.text
    assert "minutes" not in csv.text.lower()
