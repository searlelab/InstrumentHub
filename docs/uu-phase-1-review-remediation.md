# InstrumentHub Phase 1 Code-Review Remediation

**Canonical plan document:** `docs/uu-phase-1-review-remediation.md`

## Intent

Resolve both P1 findings and every verified P2 from the two independent reviews. Include small adjacent correctness fixes where they share the affected paths, while preserving the existing architecture, offline deployment, and Phase 1 feature boundaries.

## Current understanding

- The working tree is clean and the reviewed defects remain present.
- The current gate reports **125 passed, 1 failed, 1 skipped**. The failure is environmental: the local `.venv` lacks the already-declared and pinned `tzdata` dependency.
- Credential recovery, hold replacement, idle-session behavior, lifecycle rescheduling, outage recovery, error responses, and backup-retention wiring require source changes.
- No schema change is required for this scope; SQLite’s existing serialized hold writes can enforce one live hold per user and instrument.

## Desired direction and scope

- Provide authenticated administrator password resets for any account and a guarded, interactive break-glass CLI for recovering an existing administrator.
- Permit at most one live hold per user per instrument. A successful new acquisition supersedes the old hold atomically; administrators can inspect and release live holds.
- Make idle expiry reflect user activity rather than the calendar’s background polling.
- Preserve explicit booking lifecycle transitions and prevent outage-paused bookings from leaving the resumable outage state.
- Correct unknown-outage recovery, unhandled-error responses, retention configuration, misleading operational text, wizard cleanup, configured hold-duration messages, and the two reviewed accidental 500 paths.
- Defer the broader P3 refactor and hardening items: moving all reference mutations behind service authorization, database audit-event triggers, login CSRF/timing/rehash changes, and a schedule-draft discard workflow.

## Implementation approach

1. **Canonical plan and dependency gate**
   - Before implementation changes, write this settled plan verbatim to `docs/uu-phase-1-review-remediation.md` and leave it unchanged throughout the work loop.
   - Synchronize the development environment from `.[dev]` so the declared `tzdata` dependency is installed; do not alter dependency declarations solely to accommodate the stale local environment.

2. **Credential recovery**
   - Add a service-owned password-reset operation that requires an active administrator, validates the new password through the existing Argon2id helper, revokes every target session, and records a sanitized audit event with the administrator’s reason.
   - Add `POST /admin/users/{user_id}/password` and an administrator form for resetting any user’s password. A self-reset revokes the current session and returns the administrator to login; resetting another user preserves the administrator’s session.
   - Add `python -m instrumenthub reset-admin-password USERNAME --confirm-reset`. It must identify the configured database, refuse missing or non-administrator targets, prompt twice without accepting a password argument, reactivate a deactivated administrator, revoke all sessions, and record an actorless break-glass audit event.
   - Document normal resets and the break-glass procedure, including OS/database access requirements and use of the same `INSTRUMENTHUB_INSTANCE` as the service.

3. **Hold lifecycle**
   - During `acquire_hold`, prune expired holds and atomically release any existing live hold owned by the same user on the same instrument before reserving the new selection. Audit the superseded hold and rely on `BEGIN IMMEDIATE` to prevent concurrent acquisitions from exceeding the cap.
   - Preserve independent holds on the two different instruments. Failed replacement attempts must roll back and retain the prior valid hold.
   - Keep token validation for booking conversion and member-facing hold status/release. Add an administrator-only release service and route that require a reason but not the member’s token.
   - Show current live holds, owners, instruments, expiry, and release controls on Schedule operations.
   - Clear saved wizard data after successful hold-to-booking conversion and use the configured hold duration when re-rendering acquisition errors.

4. **Sessions and security/error behavior**
   - Extend session resolution with an explicit “refresh idle activity” choice. The calendar polling endpoint authenticates and enforces expiry without advancing `last_seen_at`; ordinary page navigation and interactive requests continue to refresh it.
   - Catch unhandled application exceptions inside the request-context middleware so generated 500 responses receive the request ID, CSP, `nosniff`, referrer policy, cache policy, and structured access/error logs.
   - Preserve sanitized public error pages and existing HTTP-exception behavior.

5. **Booking and outage lifecycle**
   - Restrict time rescheduling to `tentative` and `confirmed` bookings. Active or paused bookings must first move through an explicit, audited legal transition; metadata-only administrator corrections remain allowed.
   - While an instrument has an unresolved outage, reject `paused → active` and `paused → confirmed` for bookings queued by that outage. Cancellation remains available. Return an error naming the affected booking and explaining that the outage must be resumed or recovered.
   - Make resume reconciliation identify the specific blocking entry and state rather than reporting a generic stale preview.
   - During emergency unknown-outage recovery, cancel the associated outage calendar entry, remove its reserved slots, and audit the cleanup so no indefinite red occupancy remains.
   - Convert the empty configured-block case in outage shifting into a stable domain conflict and return missing booking transitions as 409 responses instead of dereference failures.

6. **Deployment and small operational corrections**
   - Add an installation-time backup-retention parameter, pass it explicitly into the scheduled `backup.ps1` action, and have that script set `INSTRUMENTHUB_BACKUP_RETENTION_DAYS`.
   - Update recovery documentation to describe the scheduled-task wiring accurately.
   - Rename the operations-page backup list so it does not claim displayed filenames have been verified at render time.

## Validation approach

- **Authentication:** test authorized and unauthorized web resets, password validation, session revocation, self-reset logout, guarded CLI refusal, interactive CLI success, deactivated-administrator recovery, audit redaction, and restart persistence.
- **Sessions/errors:** simulate an unattended calendar tab beyond idle expiry, confirm polling does not refresh activity, confirm ordinary navigation does, and force an unhandled route exception to verify all security headers and structured logs.
- **Holds:** test atomic same-instrument replacement, rollback on failed replacement, one hold per user/instrument under concurrent acquisition, simultaneous holds on different instruments, administrator release, member token enforcement, slot release, configured duration text, and wizard-state clearing.
- **Lifecycle/outages:** test active and paused reschedule rejection, explicit legal transitions, blocked outage-paused transitions, successful resume afterward, entry-specific reconciliation errors, removal of emergency-recovery outage occupancy, missing block definitions, and missing booking IDs.
- **Deployment:** add deterministic asset checks for retention propagation and documentation consistency.
- Run the affected milestone suites first, then the complete Pytest suite. Expected final local result is all runnable tests passing with only the prepared-offline-wheelhouse smoke test skipped when its fixture is unavailable.
- Re-read the complete diff for unrelated changes and manually exercise password recovery, hold replacement/release, idle logout, and unknown-outage recovery.

## Risks, decisions, and reviewer focus

- The break-glass command deliberately treats local database access as privileged operational authority; it resets only an existing administrator and cannot create arbitrary accounts.
- The hold cap is one per user per instrument, allowing at most two simultaneous holds in the stated two-instrument deployment.
- A failed replacement preserves the old hold through transaction rollback.
- No migration is planned. Review concurrency behavior carefully because the hold invariant is enforced by serialized service writes rather than a new database constraint.
- Scope defaults to the recommended P1-and-P2 remediation because no alternative scope selection was returned. Lower-priority architectural and login-hardening observations remain documented but excluded from this work loop.
- Do not stage, commit, or push.
