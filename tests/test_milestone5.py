from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
import subprocess
import sys

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version
from sqlalchemy import select

from instrumenthub.models import User
from instrumenthub.ops import _quarantine_corrupt_database, create_backup, restore_backup, verify_database


def test_online_backup_verify_and_restore(app, configured, settings):
    with app.state.database.session() as db:
        member = db.scalar(select(User).where(User.username == "member"))
        member.email = "before@example.test"
    backup = create_backup(settings)
    verify_database(backup)
    with app.state.database.session() as db:
        member = db.scalar(select(User).where(User.username == "member"))
        member.email = "after@example.test"
    safety = restore_backup(settings, backup)
    assert safety.exists()
    with app.state.database.session() as db:
        assert db.scalar(select(User).where(User.username == "member")).email == "before@example.test"


def test_restore_rebuilds_a_missing_target_without_a_safety_backup(app, configured, settings, tmp_path):
    source = create_backup(settings)
    target = replace(settings, instance_dir=tmp_path / "missing", database_path=tmp_path / "missing" / "instrumenthub.sqlite3", log_dir=tmp_path / "missing" / "logs", backup_dir=tmp_path / "missing" / "backups")
    safety = restore_backup(target, source)
    assert safety is None
    verify_database(target.database_path)


def test_restore_quarantines_a_corrupt_target_and_its_sidecars(app, configured, settings, tmp_path):
    source = create_backup(settings)
    target = replace(settings, instance_dir=tmp_path / "corrupt", database_path=tmp_path / "corrupt" / "instrumenthub.sqlite3", log_dir=tmp_path / "corrupt" / "logs", backup_dir=tmp_path / "corrupt" / "backups")
    target.ensure_directories()
    target.database_path.write_bytes(b"not sqlite")
    Path(f"{target.database_path}-wal").write_bytes(b"wal")
    Path(f"{target.database_path}-shm").write_bytes(b"shm")
    safety = restore_backup(target, source)
    assert safety is None
    verify_database(target.database_path)
    quarantines = list(target.backup_dir.glob("corrupt-*"))
    assert len(quarantines) == 1
    assert len(list(quarantines[0].iterdir())) == 3


def test_corrupt_database_quarantine_uses_a_fresh_directory_for_all_sidecars(settings, tmp_path, monkeypatch):
    import instrumenthub.ops as ops

    target = replace(settings, instance_dir=tmp_path / "collision", database_path=tmp_path / "collision" / "instrumenthub.sqlite3", log_dir=tmp_path / "collision" / "logs", backup_dir=tmp_path / "collision" / "backups")
    target.ensure_directories()
    for suffix in ("", "-wal", "-shm"):
        Path(f"{target.database_path}{suffix}").write_bytes(b"corrupt")

    class FrozenDateTime:
        @classmethod
        def now(cls, _timezone):
            return datetime(2026, 1, 2, tzinfo=UTC)

    (target.backup_dir / "corrupt-20260102T000000Z").mkdir()
    monkeypatch.setattr(ops, "datetime", FrozenDateTime)
    _quarantine_corrupt_database(target)
    quarantine = target.backup_dir / "corrupt-20260102T000000Z-1"
    assert sorted(path.name for path in quarantine.iterdir()) == ["instrumenthub.sqlite3", "instrumenthub.sqlite3-shm", "instrumenthub.sqlite3-wal"]
    assert not target.database_path.exists()
    assert not Path(f"{target.database_path}-wal").exists()
    assert not Path(f"{target.database_path}-shm").exists()


def test_verify_backup_cli_reports_an_expected_error_without_traceback(tmp_path):
    missing = tmp_path / "missing.sqlite3"
    result = subprocess.run([sys.executable, "-m", "instrumenthub", "verify-backup", str(missing)], capture_output=True, text=True)
    assert result.returncode == 1
    assert "Database file does not exist" in result.stderr
    assert "Traceback" not in result.stderr


def test_request_log_is_structured_and_omits_query_secrets(client, app, settings):
    client.get("/login?token=do-not-log")
    for handler in __import__("logging").getLogger("instrumenthub").handlers:
        handler.flush()
    lines = (settings.log_dir / "instrumenthub.jsonl").read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    assert any(record.get("path") == "/login" for record in records)
    assert "do-not-log" not in "\n".join(lines)


def test_admin_operational_status_hides_paths(client, configured, settings):
    response = client.get("/admin/operations")
    assert response.status_code == 200
    assert "Database integrity" in response.text
    assert str(settings.database_path) not in response.text


def test_public_readiness_uses_only_cheap_database_queries(client, monkeypatch):
    import instrumenthub.web as web

    statements = []
    original = web.text
    monkeypatch.setattr(web, "text", lambda statement: statements.append(statement) or original(statement))
    response = client.get("/ready")
    assert response.status_code == 200
    assert "SELECT 1" in statements
    assert all("integrity_check" not in statement for statement in statements)


def test_windows_offline_deployment_assets_exist():
    root = Path(__file__).parents[1]
    required = [
        root / "requirements.lock", root / "deploy/windows/Caddyfile.example",
        root / "deploy/windows/instrumenthub-service.xml", root / "deploy/windows/caddy-service.xml",
        root / "deploy/windows/install.ps1", root / "deploy/windows/upgrade.ps1",
        root / "deploy/windows/prepare-offline-bundle.ps1",
        root / "deploy/windows/backup.ps1", root / "deploy/windows/restore.ps1",
        root / "deploy/windows/uninstall.ps1", root / "deploy/windows/common.ps1", root / "docs/user-guide.md",
        root / "docs/admin-guide.md", root / "docs/windows-deployment.md", root / "docs/backup-recovery.md",
    ]
    assert all(path.is_file() and path.stat().st_size > 0 for path in required)
    caddy = (root / "deploy/windows/Caddyfile.example").read_text()
    service = (root / "deploy/windows/instrumenthub-service.xml").read_text()
    assert "tls " in caddy and "127.0.0.1:8000" in caddy
    assert "--host 127.0.0.1" in service and "workers" not in service
    # Operational commands must target the same instance the service uses, not a
    # relative "instance" directory tied to the caller's working directory.
    for script in ("backup.ps1", "restore.ps1", "upgrade.ps1"):
        assert "INSTRUMENTHUB_INSTANCE" in (root / "deploy/windows" / script).read_text()
    install = (root / "deploy/windows/install.ps1").read_text()
    assert "backup.ps1" in install and "-m instrumenthub backup" not in install
    assert '"NT AUTHORITY\\LocalService"' in install
    assert "NT AUTHORITY\\NetworkService" in install and "DOMAIN\\gMSA$" in install
    assert '$AclServiceAccount = if ($ServiceAccount -eq "LocalSystem") { "NT AUTHORITY\\SYSTEM" } else { $ServiceAccount }' in install
    assert 'if ($ServiceAccount -ne "LocalSystem")' in install
    assert "Set-RestrictedAcl" in install and "SetAccessRuleProtection($true, $false)" in install
    assert "RemoveAccessRuleSpecific" in install
    assert '@{ Identity=$AclServiceAccount; Rights="Modify" }' in install
    assert '@{ Identity=$AclServiceAccount; Rights="ReadAndExecute" }' in install
    assert 'Set-RestrictedAcl "$DataDirectory\\backups" $backupRules' in install
    assert 'Identity="NT AUTHORITY\\SYSTEM"; Rights="FullControl"' in install
    assert "__DATA_DIRECTORY__" in service
    caddy_service = (root / "deploy/windows/caddy-service.xml").read_text()
    caddyfile = (root / "deploy/windows/Caddyfile.example").read_text()
    assert "__DATA_DIRECTORY__" in caddy_service and "__DATA_DIRECTORY__" in caddyfile
    assert '&quot;__DATA_DIRECTORY__\\Caddyfile&quot;' in caddy_service
    assert '"__DATA_DIRECTORY__/certs/scheduler.crt"' in caddyfile
    assert '$content = $content.Replace("__DATA_DIRECTORY__", $escapedDataDirectory)' in install
    assert '.Replace("__DATA_DIRECTORY__", $DataDirectory.Replace("\\", "/"))' in install
    assert 'Get-ChildItem "$ReleaseDirectory\\wheelhouse\\instrumenthub-*.whl"' in install
    assert "Release wheelhouse must contain exactly one InstrumentHub wheel." in install
    assert "--force-reinstall" in install and "--no-deps" in install
    assert "instrumenthub==" not in install
    prepare = (root / "deploy/windows/prepare-offline-bundle.ps1").read_text()
    assert "setuptools>=" not in prepare and '"--no-deps"' in prepare and '"--only-binary=:all:"' in prepare
    assert '"--no-deps"' in install
    assert "ServiceAccount" in install and "serviceaccount" in service
    upgrade = (root / "deploy/windows/upgrade.ps1").read_text()
    assert "InstrumentHubCaddy.exe" in upgrade and '"$InstallDirectory\\bin\\caddy.exe" "$InstallDirectory\\caddy.exe"' in upgrade
    assert upgrade.index("try {") < upgrade.index('"$InstallDirectory\\InstrumentHub.exe" @("stop")')
    assert upgrade.index("try {") < upgrade.index('"$InstallDirectory\\InstrumentHubCaddy.exe" @("stop")')
    assert "$hubStopped" in upgrade and "$caddyStopped" in upgrade
    assert "--force-reinstall" in upgrade and "--no-deps" in upgrade and "instrumenthub==" not in upgrade
    assert "INSTRUMENTHUB_INSTANCE" in (root / "docs/windows-deployment.md").read_text()
    deployment = (root / "docs/windows-deployment.md").read_text()
    assert '"LAB\\InstrumentHubSvc$"' in deployment
    # Native exit codes are not terminating errors in PowerShell, so each script must
    # assert them explicitly rather than relying on $ErrorActionPreference.
    common = (root / "deploy/windows/common.ps1").read_text()
    assert "LASTEXITCODE" in common and "throw" in common
    for script in ("backup.ps1", "restore.ps1", "upgrade.ps1", "install.ps1", "prepare-offline-bundle.ps1"):
        body = (root / "deploy/windows" / script).read_text()
        assert "common.ps1" in body and "Invoke-Native" in body, script
    # A failed restore or upgrade must not be masked by the service restart in finally.
    for script in ("restore.ps1", "upgrade.ps1"):
        assert "exit 1" in (root / "deploy/windows" / script).read_text(), script


def test_windows_python312_runtime_dependency_closure_is_pinned():
    root = Path(__file__).parents[1]
    locked = {}
    for line in (root / "requirements.lock").read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        requirement = Requirement(line)
        pins = list(requirement.specifier)
        assert len(pins) == 1 and pins[0].operator == "=="
        locked[canonicalize_name(requirement.name)] = Version(pins[0].version)

    # Runtime requirements from the pinned wheel metadata. Markers are evaluated
    # for the production target rather than the host running this test.
    runtime_requirements = [
        "argon2-cffi-bindings",
        "cffi>=1.0.1",
        "pycparser",
        "starlette>=0.46.0",
        "pydantic>=2.9.0",
        "typing-extensions>=4.8.0",
        "typing-inspection>=0.4.2",
        "annotated-doc>=0.0.2",
        "anyio>=3.6.2,<5",
        "idna>=2.8",
        "MarkupSafe>=2.0",
        "annotated-types>=0.6.0",
        "pydantic-core==2.46.4",
        "greenlet!=0.4.17; python_version < '3.14' and platform_machine == 'AMD64'",
        "colorama; platform_system == 'Windows'",
        "click>=7.0",
        "h11>=0.8",
    ]
    target = default_environment()
    target.update({
        "implementation_name": "cpython", "os_name": "nt", "platform_machine": "AMD64",
        "platform_python_implementation": "CPython", "platform_system": "Windows",
        "python_full_version": "3.12.0", "python_version": "3.12", "sys_platform": "win32",
    })
    for value in runtime_requirements:
        requirement = Requirement(value)
        if requirement.marker and not requirement.marker.evaluate(target):
            continue
        name = canonicalize_name(requirement.name)
        assert name in locked, f"Missing Windows runtime dependency: {requirement.name}"
        assert locked[name] in requirement.specifier, f"Locked {requirement.name} does not satisfy {requirement.specifier}"


def test_prepared_wheelhouse_installs_without_network(tmp_path):
    wheelhouse = os.environ.get("INSTRUMENTHUB_OFFLINE_WHEELHOUSE")
    if not wheelhouse:
        __import__("pytest").skip("Set INSTRUMENTHUB_OFFLINE_WHEELHOUSE to run the prepared-bundle smoke test.")
    wheelhouse_path = Path(wheelhouse)
    assert any(wheelhouse_path.glob("instrumenthub-*.whl"))
    venv = tmp_path / "offline-venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    root = Path(__file__).parents[1]
    subprocess.run([str(python), "-m", "pip", "install", "--no-index", "--no-deps", "--only-binary=:all:", "--find-links", str(wheelhouse_path), "-r", str(root / "requirements.lock")], check=True)
    subprocess.run([str(python), "-m", "pip", "install", "--no-index", "--no-deps", "--only-binary=:all:", "--find-links", str(wheelhouse_path), "instrumenthub==0.1.0"], check=True)
    subprocess.run([str(python), "-m", "pip", "check"], check=True)
    subprocess.run([str(python), "-c", "import instrumenthub; print(instrumenthub.__name__)"], check=True)
