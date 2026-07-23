# InstrumentHub administrator guide

Create the first administrator with `python -m instrumenthub bootstrap-admin`, run with `INSTRUMENTHUB_INSTANCE` set to the same instance the service uses (`C:\ProgramData\InstrumentHub` on Windows) so the account lands in the live database, then sign in over HTTPS and complete initial setup. Enter exactly two instruments, their fixed block definitions, timezone, hold duration, optional tentative expiry, and training policy before opening booking to members.

Use Administration to maintain individual users, PIs, projects, grants, training, and date-bounded allotments. Training records show whether they are current or revoked; record renewed training or revoke a current record with a reason as appropriate. An absent allotment means unlimited. Use Schedule operations to approve or reject tentative requests, inspect audit history, and preview outages.

Outage previews do not change the live calendar. Review the affected schedule before applying. A known-duration outage shifts affected remaining slots. An unresolved outage pauses affected bookings and removes promised dates; enter a restart date and apply the reconstruction only when operations can resume. Every decision, override, and state change requires a reason.
