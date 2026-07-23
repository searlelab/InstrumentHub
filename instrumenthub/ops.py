from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import Settings
from .db import run_migrations


def verify_database(path: Path) -> None:
    if not path.is_file():
        raise ValueError(f"Database file does not exist: {path}")
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise ValueError(f"Database integrity check failed: {result[0] if result else 'no result'}")
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"Database is corrupt or unreadable: {path}") from exc


def create_backup(settings: Settings, *, destination: Path | None = None, prune: bool = True) -> Path:
    settings.ensure_directories()
    verify_database(settings.database_path)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = destination or settings.backup_dir / f"instrumenthub-{stamp}.sqlite3"
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ValueError(f"Backup destination already exists: {destination}")
    with closing(sqlite3.connect(settings.database_path)) as source, closing(sqlite3.connect(destination)) as target:
        source.backup(target)
    os.chmod(destination, 0o600)
    verify_database(destination)
    if prune:
        cutoff = datetime.now(UTC) - timedelta(days=settings.backup_retention_days)
        candidates = [*settings.backup_dir.glob("instrumenthub-*.sqlite3"), *settings.backup_dir.glob("pre-restore-*.sqlite3")]
        for candidate in candidates:
            if candidate.resolve() != destination and datetime.fromtimestamp(candidate.stat().st_mtime, UTC) < cutoff:
                candidate.unlink()
    return destination


def _quarantine_corrupt_database(settings: Settings) -> None:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    for sequence in range(1000):
        suffix = "" if sequence == 0 else f"-{sequence}"
        quarantine = settings.backup_dir / f"corrupt-{stamp}{suffix}"
        try:
            quarantine.mkdir()
        except FileExistsError:
            continue
        break
    else:
        raise ValueError("Could not allocate a corrupt database quarantine directory.")
    for suffix in ("", "-wal", "-shm"):
        live = Path(f"{settings.database_path}{suffix}")
        if live.exists():
            os.replace(live, quarantine / f"{settings.database_path.name}{suffix}")


def restore_backup(settings: Settings, source: Path) -> Path | None:
    settings.ensure_directories()
    source = source.resolve()
    verify_database(source)
    safety = None
    if settings.database_path.exists():
        try:
            verify_database(settings.database_path)
        except ValueError:
            _quarantine_corrupt_database(settings)
        else:
            safety = create_backup(settings, destination=settings.backup_dir / f"pre-restore-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.sqlite3", prune=False)
    with closing(sqlite3.connect(source)) as incoming, closing(sqlite3.connect(settings.database_path)) as target:
        incoming.backup(target)
    run_migrations(settings.database_path)
    verify_database(settings.database_path)
    return safety
