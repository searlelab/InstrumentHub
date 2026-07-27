from __future__ import annotations


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
    assert "Booking block schedules" in instruments.text
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
