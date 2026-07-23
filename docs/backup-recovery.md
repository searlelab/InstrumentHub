# Backup and recovery

The daily task runs `python -m instrumenthub backup`. Each online SQLite backup is integrity-checked and retained for 30 days by default. Monitor task exit status and periodically copy backups to lab-managed protected storage.

Pass `-BackupRetentionDays` to `install.ps1` to configure the daily scheduled backup task; it defaults to `30` and accepts values from 1 through 3650 days. The task passes that value to `backup.ps1`, which sets `INSTRUMENTHUB_BACKUP_RETENTION_DAYS` for the backup command. For an authorized manual backup, either pass `-RetentionDays` to `backup.ps1` or set `INSTRUMENTHUB_BACKUP_RETENTION_DAYS` before running the CLI directly; the same 1-through-3650 validation applies to the environment variable, and invalid values stop the command before any backup is created or pruned. Application and CLI settings are validated together, so an invalid retention environment value also prevents the service and non-backup CLI commands from starting until the value is corrected. The backup created by the current command is never removed by that command.

To rehearse recovery, use `verify-backup` against a copied backup, restore it into a disposable instance directory, start one application worker, and verify authentication, calendar, booking details, and audit history.

For production recovery, stop the InstrumentHub service and run `restore.ps1 -Backup <path>`. Restore verifies the source, creates a pre-restore safety backup when the live database is healthy, replaces the database through SQLite's backup API, applies forward migrations, verifies integrity, and restarts the service. If the live database is missing, restore proceeds without a safety copy. If it is corrupt, restore quarantines the database and its WAL/SHM sidecars in the backup directory before proceeding. Never restore while the application service is running.

Notification delivery is independent of backup and restore. The Windows installation schedules `deliver-notifications` every five minutes; it sends only queued records and retries failed SMTP delivery, so restore and backup operations never depend on SMTP availability.
