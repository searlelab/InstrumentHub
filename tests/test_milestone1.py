from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import zoneinfo
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError

from instrumenthub.db import Database, _apply_migration, run_migrations
from instrumenthub.__main__ import bootstrap_admin
from instrumenthub.web import create_app
from instrumenthub.models import AuditEvent, Instrument, SessionRecord, User
from instrumenthub.scheduling import slots_for_date
from instrumenthub.security import utc_text
from instrumenthub.services import audit, complete_setup, create_user
from tests.conftest import login


def test_migrations_are_idempotent_and_integrity_is_ok(settings):
    run_migrations(settings.database_path)
    run_migrations(settings.database_path)
    with sqlite3.connect(settings.database_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 5


def test_concurrent_first_run_migrations_share_one_fresh_database(tmp_path, monkeypatch):
    import instrumenthub.db as db_module

    database_path = tmp_path / "concurrent.sqlite3"
    starters_ready = Barrier(2)
    original = db_module._apply_migration

    def synchronized_first_migration(connection, path):
        if path.name == "001_initial.sql":
            starters_ready.wait()
        return original(connection, path)

    monkeypatch.setattr(db_module, "_apply_migration", synchronized_first_migration)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run_migrations(database_path), range(2)))

    assert results == [None, None]
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 5


def test_utc_text_has_fixed_sortable_microsecond_precision():
    earlier = utc_text(datetime(2026, 1, 1, 0, 0, 0, 1, tzinfo=UTC))
    later = utc_text(datetime(2026, 1, 1, 0, 0, 0, 2, tzinfo=UTC))
    assert earlier == "2026-01-01T00:00:00.000001Z"
    assert earlier < later


def test_setup_service_requires_an_active_administrator(app):
    with app.state.database.session() as db:
        member = db.scalar(select(User).where(User.username == "member"))
        with pytest.raises(ValueError, match="active administrator"):
            complete_setup(db, timezone="America/Chicago", instruments=[{"name": "One", "blocks": "Day,08:00,16:00"}, {"name": "Two", "blocks": "Day,08:00,16:00"}], hold_minutes=15, tentative_expiration_days=None, actor_user_id=member.id)


def test_user_creation_requires_an_active_admin_after_bootstrap(app):
    with app.state.database.session() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        with pytest.raises(ValueError, match="active administrator"):
            create_user(db, username="unauthorized", password="correct-horse-battery", display_name="Unauthorized", initials="UN", role="admin")
        admin.active = False
        with pytest.raises(ValueError, match="active administrator"):
            create_user(db, username="inactive-admin", password="correct-horse-battery", display_name="Inactive", initials="IA", role="user", actor_user_id=admin.id)


def test_failed_migration_rolls_back_schema_and_migration_record(tmp_path):
    migration = tmp_path / "004_broken.sql"
    migration.write_text("CREATE TABLE migration_should_rollback (id INTEGER);\nTHIS IS NOT SQL;", encoding="utf-8")
    with sqlite3.connect(tmp_path / "migration.sqlite3") as connection:
        connection.execute("CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
        with pytest.raises(sqlite3.OperationalError):
            _apply_migration(connection, migration)
        assert connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'migration_should_rollback'").fetchone() is None
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 0


def test_database_rejects_reservation_range_overlaps_in_both_orientations(settings):
    run_migrations(settings.database_path)
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute("INSERT INTO reserved_slots(id,calendar_entry_id,instrument_id,slot_start,slot_end,nominal_minutes,requested_slot_start) VALUES (1,1,1,'2026-01-01T10:00:00.000000Z','2026-01-01T12:00:00.000000Z',120,'2026-01-01T10:00:00.000000Z')")
        connection.commit()
        for slot_id, start, end in (
            (2, "2026-01-01T11:00:00.000000Z", "2026-01-01T13:00:00.000000Z"),
            (3, "2026-01-01T09:00:00.000000Z", "2026-01-01T11:00:00.000000Z"),
        ):
            with pytest.raises(sqlite3.IntegrityError, match="range overlap"):
                connection.execute("INSERT INTO reserved_slots(id,calendar_entry_id,instrument_id,slot_start,slot_end,nominal_minutes,requested_slot_start) VALUES (?,?,?,?,?,?,?)", (slot_id, slot_id, 1, start, end, 120, start))
            connection.rollback()
        connection.execute("INSERT INTO reserved_slots(id,calendar_entry_id,instrument_id,slot_start,slot_end,nominal_minutes,requested_slot_start) VALUES (4,4,1,'2026-01-01T12:00:00.000000Z','2026-01-01T14:00:00.000000Z',120,'2026-01-01T12:00:00.000000Z')")
        with pytest.raises(sqlite3.IntegrityError, match="range overlap"):
            connection.execute("UPDATE reserved_slots SET slot_start='2026-01-01T11:00:00.000000Z' WHERE id=4")


def test_range_guard_migration_rejects_preexisting_overlaps(tmp_path):
    database_path = tmp_path / "overlapping.sqlite3"
    migration_dir = Path(__file__).parents[1] / "instrumenthub" / "migrations"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
        for name in ("001_initial.sql", "002_wizard_state.sql", "003_user_projects.sql"):
            _apply_migration(connection, migration_dir / name)
        connection.execute("INSERT INTO reserved_slots(id,calendar_entry_id,instrument_id,slot_start,slot_end,nominal_minutes,requested_slot_start) VALUES (1,1,1,'2026-01-01T10:00:00.000000Z','2026-01-01T12:00:00.000000Z',120,'2026-01-01T10:00:00.000000Z')")
        connection.execute("INSERT INTO reserved_slots(id,calendar_entry_id,instrument_id,slot_start,slot_end,nominal_minutes,requested_slot_start) VALUES (2,2,1,'2026-01-01T11:00:00.000000Z','2026-01-01T13:00:00.000000Z',120,'2026-01-01T11:00:00.000000Z')")
        connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        run_migrations(database_path)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 3


def test_setup_rejects_overlapping_blocks(client):
    csrf = login(client)
    response = client.post("/setup", data={
        "csrf_token": csrf, "timezone": "America/Chicago", "instrument_1_name": "A",
        "instrument_1_blocks": "Long,16:00,08:00\nOverlap,00:00,07:00", "instrument_2_name": "B",
        "instrument_2_blocks": "Day,08:00,16:00", "hold_minutes": "15", "tentative_expiration_days": "",
    })
    assert response.status_code == 400
    assert "overlap" in response.text.lower()


def test_setup_and_scheduling_use_bundled_tzdata_without_system_zoneinfo(client, app):
    csrf = login(client)
    original_tzpath = zoneinfo.TZPATH
    ZoneInfo.clear_cache()
    zoneinfo.reset_tzpath(to=())
    try:
        response = client.post("/setup", data={
            "csrf_token": csrf, "timezone": "America/Chicago", "instrument_1_name": "A",
            "instrument_1_blocks": "Day,08:00,16:00", "instrument_2_name": "B",
            "instrument_2_blocks": "Day,08:00,16:00", "hold_minutes": "15", "tentative_expiration_days": "",
        }, follow_redirects=False)
        assert response.status_code == 303
        with app.state.database.session() as db:
            assert slots_for_date(db, 1, date.today(), "America/Chicago")
    finally:
        ZoneInfo.clear_cache()
        zoneinfo.reset_tzpath(to=original_tzpath)


def test_authentication_setup_gate_and_exactly_two_instruments(client):
    assert client.get("/").status_code == 200  # redirected to login and followed
    csrf = login(client)
    assert client.get("/", follow_redirects=False).headers["location"] == "/setup"
    bad = client.post("/setup", data={
        "csrf_token": csrf, "timezone": "America/Chicago", "instrument_1_name": "A",
        "instrument_1_blocks": "Day,08:00,16:00", "instrument_2_name": "A",
        "instrument_2_blocks": "Day,08:00,16:00", "hold_minutes": "15", "tentative_expiration_days": "",
    })
    assert bad.status_code == 400


def test_setup_explains_training_and_tentative_expiry(client):
    login(client)
    page = client.get("/setup")
    assert "current training record" in page.text
    assert "automatically cancel unapproved requests" in page.text


def test_setup_persists_and_cannot_repeat(client, configured, app):
    with app.state.database.session() as db:
        assert db.scalar(select(func.count()).select_from(Instrument)) == 2
    assert client.get("/setup", follow_redirects=False).headers["location"] == "/admin"


def test_user_cannot_access_admin(client):
    login(client, "member", "correct-horse-member!")
    response = client.get("/admin")
    assert response.status_code == 403


def test_csrf_is_required(client, admin_csrf):
    response = client.post("/admin/pis", data={"name": "PI One"})
    assert response.status_code in {403, 422}


def test_admin_reference_data_rejects_missing_references(client, configured):
    project = client.post("/admin/projects", data={"csrf_token": configured, "pi_id": "999", "code": "BAD", "name": "Bad"})
    allotment = client.post("/admin/allotments", data={"csrf_token": configured, "scope_type": "pi", "scope_id": "999", "period_start": "2026-01-01", "period_end": "2026-01-31", "minutes_allowed": "60"})
    assert project.status_code == 400
    assert allotment.status_code == 400


def test_password_change_revokes_all_sessions(client, app):
    csrf = login(client, "member", "correct-horse-member!")
    second = client.__class__(app)
    second.__enter__()
    try:
        login(second, "member", "correct-horse-member!")
        response = client.post("/profile/password", data={"csrf_token": csrf, "current_password": "correct-horse-member!", "new_password": "a-new-secure-password"}, follow_redirects=False)
        assert response.status_code == 303
        assert second.get("/profile", follow_redirects=False).status_code == 303
    finally:
        second.__exit__(None, None, None)


def test_login_throttling(client):
    for _ in range(5):
        assert client.post("/login", data={"username": "member", "password": "wrong"}).status_code == 400
    assert client.post("/login", data={"username": "member", "password": "wrong"}).status_code == 429


def test_login_prunes_attempt_history_older_than_thirty_days(client, app):
    with app.state.database.session() as db:
        db.execute(text("INSERT INTO login_attempts(username,remote_addr,attempted_at,succeeded) VALUES ('old-probe','test',:old,0),('recent-probe','test',:recent,0)"), {"old": utc_text(datetime.now(UTC) - timedelta(days=31)), "recent": utc_text(datetime.now(UTC) - timedelta(days=29))})
    assert client.post("/login", data={"username": "admin", "password": "correct-horse-battery"}, follow_redirects=False).status_code == 303
    with app.state.database.session() as db:
        assert db.execute(text("SELECT COUNT(*) FROM login_attempts WHERE username='old-probe'")).scalar_one() == 0
        assert db.execute(text("SELECT COUNT(*) FROM login_attempts WHERE username='recent-probe'")).scalar_one() == 1


def test_login_prunes_old_expired_and_revoked_sessions(client, app):
    now = datetime.now(UTC)
    with app.state.database.session() as db:
        user_id = db.scalar(select(User.id).where(User.username == "member"))
        common = {"user_id": user_id, "csrf_token": "probe", "created_at": utc_text(now - timedelta(days=20)), "last_seen_at": utc_text(now - timedelta(days=20)), "wizard_json": "{}"}
        db.add_all([
            SessionRecord(token_hash="expired-old", expires_at=utc_text(now - timedelta(days=8)), **common),
            SessionRecord(token_hash="expired-recent", expires_at=utc_text(now - timedelta(days=6)), **common),
            SessionRecord(token_hash="revoked-old", expires_at=utc_text(now + timedelta(days=1)), revoked_at=utc_text(now - timedelta(days=8)), **common),
        ])
    assert client.post("/login", data={"username": "admin", "password": "correct-horse-battery"}, follow_redirects=False).status_code == 303
    with app.state.database.session() as db:
        hashes = set(db.scalars(select(SessionRecord.token_hash).where(SessionRecord.token_hash.like("%-old") | SessionRecord.token_hash.like("%-recent"))).all())
    assert hashes == {"expired-recent"}


@pytest.mark.parametrize(
    ("path", "data", "expected_status", "message"),
    [
        ("/admin/users", {"username": "bad user", "password": "correct-horse-battery", "display_name": "Bad", "initials": "B", "role": "user"}, 400, "Username must be"),
        ("/admin/users", {"username": "short-password", "password": "short", "display_name": "Short", "initials": "S", "role": "user"}, 400, "at least 12 characters"),
        ("/admin/users", {"username": "admin", "password": "correct-horse-battery", "display_name": "Duplicate", "initials": "D", "role": "admin"}, 409, "already exists"),
    ],
)
def test_admin_user_validation_returns_actionable_http_errors(client, configured, path, data, expected_status, message):
    response = client.post(path, data={"csrf_token": configured, **data})
    assert response.status_code == expected_status
    assert message in response.text


def test_short_password_change_returns_bad_request(client):
    csrf = login(client, "member", "correct-horse-member!")
    response = client.post("/profile/password", data={"csrf_token": csrf, "current_password": "correct-horse-member!", "new_password": "short"})
    assert response.status_code == 400
    assert "at least 12 characters" in response.text


def test_audit_sanitizes_secrets(app):
    with app.state.database.session() as db:
        audit(db, "test", "test", "1", before={"password": "nope", "nested": {"token": "nope"}}, after={"safe": "yes"})
    with app.state.database.session() as db:
        event = db.scalar(select(AuditEvent).where(AuditEvent.action == "test"))
        assert "nope" not in event.before_json
        assert "[REDACTED]" in event.before_json


def test_ready_endpoint(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").json() == {"status": "ready"}


def test_safe_error_page_survives_a_session_commit_failure(client, configured, app, monkeypatch):
    original_session = app.state.database.session

    @contextmanager
    def failing_session():
        with original_session() as db:
            yield db
            raise OperationalError("COMMIT", {}, sqlite3.OperationalError("database is locked"))

    monkeypatch.setattr(app.state.database, "session", failing_session)
    response = client.get("/calendar", headers={"X-Request-ID": "commit-failure-test"})
    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "commit-failure-test"
    assert "An unexpected error occurred" in response.text
    assert "Request commit-failure-test" in response.text


def test_bootstrap_account_authenticates_against_the_same_instance(settings, monkeypatch):
    answers = iter(["bootstrap", "Bootstrap Administrator", "BA"])
    passwords = iter(["bootstrap-password", "bootstrap-password"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr("instrumenthub.__main__.getpass.getpass", lambda _prompt: next(passwords))
    assert bootstrap_admin(settings) == 0
    with TestClient(create_app(settings)) as local_client:
        response = local_client.post("/login", data={"username": "bootstrap", "password": "bootstrap-password"}, follow_redirects=False)
    assert response.status_code == 303


def test_bootstrap_retries_a_short_password(settings, monkeypatch):
    answers = iter(["bootstrap", "Bootstrap Administrator", "BA"])
    passwords = iter(["too-short", "too-short", "bootstrap-password", "bootstrap-password"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr("instrumenthub.__main__.getpass.getpass", lambda _prompt: next(passwords))
    assert bootstrap_admin(settings) == 0
    with Database(settings).session() as db:
        assert db.scalar(select(User.username)) == "bootstrap"
