# Backup and recovery

The daily task runs `python -m instrumenthub backup`. Each online SQLite backup is integrity-checked and retained for 30 days by default. Monitor task exit status and periodically copy backups to lab-managed protected storage.

Set `INSTRUMENTHUB_BACKUP_RETENTION_DAYS` for the InstrumentHub service and scheduled backup task to change retention. It accepts an integer number of days and defaults to `30`. Use a positive integer in production; `0` expires every prior managed backup immediately, while a negative value also expires all prior managed backups. The backup created by the current command is never removed by that command.

To rehearse recovery, use `verify-backup` against a copied backup, restore it into a disposable instance directory, start one application worker, and verify authentication, calendar, booking details, and audit history.

For production recovery, stop the InstrumentHub service and run `restore.ps1 -Backup <path>`. Restore verifies the source, creates a pre-restore safety backup when the live database is healthy, replaces the database through SQLite's backup API, applies forward migrations, verifies integrity, and restarts the service. If the live database is missing, restore proceeds without a safety copy. If it is corrupt, restore quarantines the database and its WAL/SHM sidecars in the backup directory before proceeding. Never restore while the application service is running.
