from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    instance_dir: Path
    database_path: Path
    log_dir: Path
    backup_dir: Path
    secure_cookies: bool = True
    session_idle_minutes: int = 480
    session_absolute_hours: int = 24
    backup_retention_days: int = 30

    @classmethod
    def from_env(cls) -> "Settings":
        instance = Path(os.environ.get("INSTRUMENTHUB_INSTANCE", "instance")).resolve()
        return cls(
            instance_dir=instance,
            database_path=Path(os.environ.get("INSTRUMENTHUB_DATABASE", instance / "instrumenthub.sqlite3")).resolve(),
            log_dir=Path(os.environ.get("INSTRUMENTHUB_LOG_DIR", instance / "logs")).resolve(),
            backup_dir=Path(os.environ.get("INSTRUMENTHUB_BACKUP_DIR", instance / "backups")).resolve(),
            secure_cookies=os.environ.get("INSTRUMENTHUB_SECURE_COOKIES", "true").lower() not in {"0", "false", "no"},
            session_idle_minutes=int(os.environ.get("INSTRUMENTHUB_SESSION_IDLE_MINUTES", "480")),
            session_absolute_hours=int(os.environ.get("INSTRUMENTHUB_SESSION_ABSOLUTE_HOURS", "24")),
            backup_retention_days=int(os.environ.get("INSTRUMENTHUB_BACKUP_RETENTION_DAYS", "30")),
        )

    def ensure_directories(self) -> None:
        self.instance_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)

