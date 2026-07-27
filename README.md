# InstrumentHub

**InstrumentHub is a secure, intranet-first scheduling application for shared mass spectrometers.** It gives lab members a clear calendar and guided booking flow, while giving administrators the controls needed to manage training, approvals, downtime, audit history, and recovery.

Designed for a small lab deployment, InstrumentHub runs as a single FastAPI application with SQLite and has no runtime dependency on external services.

## What it does

- Shows authenticated, fixed-block calendars for one or more instruments, including a compact all-instruments overview.
- Prevents double booking with transactional holds and database-backed slot uniqueness.
- Guides members through booking details, duration estimates, projects, grants, and rule checks.
- Applies configurable training, allotment, consecutive-time, and approval policies.
- Supports tentative approvals, booking edits/cancellations, recurring maintenance tasks, and planned or unplanned outages.
- Provides an authenticated inbox with optional SMTP delivery; email is disabled unless a lab-managed relay is configured.
- Provides administrator management for people, reference data, training, allotments, maintenance, schedule operations, and filtered usage reports with CSV export.
- Keeps immutable audit history and uses secure password hashing, server-side sessions, CSRF protection, and login throttling.
- Includes health/readiness endpoints, rotating local logs, verified SQLite backups, and guarded restores.

## Technology

- Python 3.12+
- FastAPI and Uvicorn
- Jinja templates and local static assets
- SQLAlchemy 2 and SQLite
- Argon2id password hashing
- Pytest

## Quick start (development)

Prerequisite: Python 3.12 or later.

```bash
git clone <repository-url>
cd InstrumentHub
python -m venv .venv
source .venv/bin/activate                 # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

For local HTTP development, explicitly disable secure-only cookies and use a disposable instance directory:

```bash
export INSTRUMENTHUB_INSTANCE="$PWD/.instance"
export INSTRUMENTHUB_SECURE_COOKIES=false
python -m instrumenthub bootstrap-admin
python -m instrumenthub serve
```

Open <http://127.0.0.1:8000>, sign in with the first administrator, and complete the setup flow. Setup establishes the lab timezone, one initial instrument class and instrument, the shared calendar structure, hold policy, tentative-expiry policy, and training requirements. Administrators can add further instruments to the existing class individually later.

On Windows PowerShell, use:

```powershell
$env:INSTRUMENTHUB_INSTANCE = "$PWD\.instance"
$env:INSTRUMENTHUB_SECURE_COOKIES = "false"
python -m instrumenthub bootstrap-admin
python -m instrumenthub serve
```

> Do not set `INSTRUMENTHUB_SECURE_COOKIES=false` in production. Production access is intended to be HTTPS-only.

## Common commands

```bash
# Start the local application (one Uvicorn worker)
python -m instrumenthub serve --host 127.0.0.1 --port 8000

# Confirm which instance and database the CLI is using
python -m instrumenthub status

# Run the automated tests
pytest

# Create and verify an online SQLite backup
python -m instrumenthub backup
python -m instrumenthub verify-backup path/to/backup.sqlite3

# Restore a verified backup only after stopping the application service
python -m instrumenthub restore path/to/backup.sqlite3 --confirm-restore

# Emergency recovery for an existing administrator (authorized operators only)
python -m instrumenthub reset-admin-password USERNAME --confirm-reset

# Deliver a bounded batch of queued notification emails (SMTP is optional)
python -m instrumenthub deliver-notifications --limit 100
```

The default instance directory is `./instance`. It contains the SQLite database, logs, and backups; set `INSTRUMENTHUB_INSTANCE` before every operational command to ensure it targets the intended deployment.

## Deployment

InstrumentHub is intended to run on one Windows 11 intranet host:

1. Package the approved dependencies and service binaries on a connected staging computer with `deploy/windows/prepare-offline-bundle.ps1`.
2. Install the offline bundle, Uvicorn service, and Caddy reverse proxy with `deploy/windows/install.ps1`.
3. Use a stable internal DNS name and a lab-issued TLS certificate; Caddy terminates HTTPS and Uvicorn remains bound to loopback.
4. Create the initial administrator against the live instance, then complete setup through the HTTPS hostname.
5. Confirm the daily backup task and the five-minute notification-delivery task, then perform the documented restore rehearsal before launch.

See the [Windows deployment guide](docs/windows-deployment.md) for the full procedure. The deployment scripts support a restricted service identity and keep live data outside the application release directory.

## Operations and documentation

| Topic | Document |
| --- | --- |
| Member booking workflow | [User guide](docs/user-guide.md) |
| Administrator setup, approvals, maintenance, and reports | [Administrator guide](docs/admin-guide.md) |
| Backup retention and restore rehearsal | [Backup and recovery](docs/backup-recovery.md) |
| Windows installation and service configuration | [Windows deployment](docs/windows-deployment.md) |
| Phase 1 scope and acceptance criteria | [Project plan](docs/uu-lab-scheduler-phase-1.md) |

Operational endpoints are available locally at `/health` and `/ready`. The readiness endpoint verifies database availability and migrations; it returns `503` when the application is not ready.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `INSTRUMENTHUB_INSTANCE` | `instance` | Base directory for application data. |
| `INSTRUMENTHUB_DATABASE` | `<instance>/instrumenthub.sqlite3` | SQLite database path. |
| `INSTRUMENTHUB_LOG_DIR` | `<instance>/logs` | Structured log directory. |
| `INSTRUMENTHUB_BACKUP_DIR` | `<instance>/backups` | Backup directory. |
| `INSTRUMENTHUB_SECURE_COOKIES` | `true` | Require HTTPS-only session cookies. |
| `INSTRUMENTHUB_SESSION_IDLE_MINUTES` | `480` | Idle session timeout. |
| `INSTRUMENTHUB_SESSION_ABSOLUTE_HOURS` | `24` | Maximum session lifetime. |
| `INSTRUMENTHUB_BACKUP_RETENTION_DAYS` | `30` | Retention period, from 1 to 3650 days. |
| `INSTRUMENTHUB_SMTP_HOST` | empty | Optional lab-managed SMTP relay hostname; email delivery is disabled when unset. |
| `INSTRUMENTHUB_SMTP_PORT` | `587` | SMTP relay port. |
| `INSTRUMENTHUB_SMTP_USERNAME` | empty | Optional SMTP username. |
| `INSTRUMENTHUB_SMTP_PASSWORD` | empty | Optional SMTP password; configure outside source control and deployment XML. |
| `INSTRUMENTHUB_SMTP_FROM` | empty | Sender address for notification emails. |
| `INSTRUMENTHUB_SMTP_STARTTLS` | `true` | Use STARTTLS for SMTP delivery. |

## Scope

InstrumentHub focuses on reliable local scheduling for shared instruments, with local notifications, recurring maintenance operations, and administrator reporting. External calendar integrations, waitlists, automatic placement, preferred-instrument queues, workstation scheduling, sample-compatibility enforcement, and a PI self-service portal remain out of scope.

## License

Licensed under the [Apache License 2.0](LICENSE).
