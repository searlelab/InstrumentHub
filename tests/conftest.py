from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from instrumenthub.config import Settings
from instrumenthub.db import Database
from instrumenthub.services import create_user
from instrumenthub.web import create_app


@pytest.fixture
def settings(tmp_path):
    return Settings(
        instance_dir=tmp_path,
        database_path=tmp_path / "test.sqlite3",
        log_dir=tmp_path / "logs",
        backup_dir=tmp_path / "backups",
        secure_cookies=False,
    )


@pytest.fixture
def app(settings):
    app = create_app(settings)
    with app.state.database.session() as db:
        admin = create_user(db, username="admin", password="correct-horse-battery", display_name="Admin User", initials="AU", role="admin")
        create_user(db, username="member", password="correct-horse-member!", display_name="Lab Member", initials="LM", role="user", email="member@example.test", phone="555-0100", actor_user_id=admin.id)
    return app


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def login(client: TestClient, username="admin", password="correct-horse-battery") -> str:
    response = client.post("/login", data={"username": username, "password": password}, follow_redirects=False)
    assert response.status_code == 303
    page = client.get("/setup" if username == "admin" else "/profile")
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match
    return match.group(1)


@pytest.fixture
def admin_csrf(client):
    return login(client)


@pytest.fixture
def configured(client, admin_csrf):
    response = client.post(
        "/setup",
        data={
            "csrf_token": admin_csrf,
            "timezone": "America/Chicago",
            "instrument_name": "Orbitrap A",
            "instrument_blocks": "Morning,06:00,12:00\nAfternoon,12:00,18:00\nOvernight,18:00,06:00",
            "instrument_training": "yes",
            "hold_minutes": "15",
            "tentative_expiration_days": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    response = client.post(
        "/admin/instruments",
        data={
            "csrf_token": admin_csrf,
            "name": "Orbitrap B",
            "blocks": "Morning,06:00,12:00\nAfternoon,12:00,18:00\nOvernight,18:00,06:00",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return admin_csrf
