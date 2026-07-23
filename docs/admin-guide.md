# InstrumentHub administrator guide

Create the first administrator with `python -m instrumenthub bootstrap-admin`, run with `INSTRUMENTHUB_INSTANCE` set to the same instance the service uses (`C:\ProgramData\InstrumentHub` on Windows) so the account lands in the live database, then sign in over HTTPS and complete initial setup. Enter exactly two instruments, their fixed block definitions, timezone, hold duration, optional tentative expiry, and training policy before opening booking to members.

Use Administration to maintain individual users, PIs, projects, grants, training, and date-bounded allotments. Training records show whether they are current or revoked; record renewed training or revoke a current record with a reason as appropriate. An absent allotment means unlimited. Use Schedule operations to approve or reject tentative requests, inspect audit history, and preview outages.

An authenticated administrator can reset any account's password from Administration. A reset requires a reason and revokes every session belonging to that account. If the current administrator resets their own password, they must sign in again.

If no administrator can sign in, an operator with authorized local access to the application database can run `python -m instrumenthub reset-admin-password USERNAME --confirm-reset`. Set `INSTRUMENTHUB_INSTANCE` to the same instance used by the service (`C:\ProgramData\InstrumentHub` by default) and verify the database path printed by the command before entering the new password. The command only recovers an existing administrator, reactivates that account if necessary, revokes its prior sessions, and records a break-glass audit event. Restrict this procedure to authorized Windows administrators or service operators with database access.

Outage previews do not change the live calendar. Review the affected schedule before applying. A known-duration outage shifts affected remaining slots. An unresolved outage pauses affected bookings and removes promised dates; enter a restart date and apply the reconstruction only when operations can resume. Do not manually resume an outage-paused booking while the instrument remains unavailable. Every decision, override, and state change requires a reason.

The Maintenance page manages recurring maintenance templates and their due tasks. Create a template with an interval, expected duration, optional assignee, and first due date. Complete or skip each generated task with a reason, or block its selected calendar range as routine maintenance. The Usage reports page provides filtered on-screen totals, reservation drill-down, member-use comparisons, and CSV export for grant and allotment review.

Notifications always appear in the authenticated in-app inbox. When lab IT configures the SMTP environment variables, the local notification-delivery task retries pending email without delaying bookings, decisions, cancellations, or outage recovery.
