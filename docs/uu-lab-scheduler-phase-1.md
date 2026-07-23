# InstrumentHub Phase 1 Lab Scheduler

**Canonical plan document:** `docs/uu-lab-scheduler-phase-1.md`

## Intent

Build the complete five-milestone Phase 1 intranet scheduler for two mass spectrometers. The application will provide authenticated, conflict-safe fixed-block booking; configurable scheduling rules; administrator approvals and downtime management; auditability; and a tested Windows 11 deployment.

Each milestone is an acceptance gate: run the complete accumulated Pytest suite and review the milestone manually before beginning the next milestone. Do not implement deferred reporting, notifications, workstation scheduling, optimization, or external integrations.

## Current understanding

- The repository currently contains only baseline Git files and an untracked user-stories document; there is no application architecture to preserve.
- Use Python 3.12, FastAPI, Uvicorn, Jinja templates, local JavaScript/CSS, SQLite, numbered SQL migrations, and Pytest.
- Production runs on one Windows 11 intranet computer; development occurs on macOS. Runtime operation must not depend on Internet services.
- The first administrator is created through an interactive bootstrap command. Booking remains disabled until that administrator completes an authenticated setup flow for timezone, two instruments, fixed blocks, hold/approval policies, and training enforcement.
- Assume Caddy terminates HTTPS with a lab-issued certificate for the stable internal hostname. If no lab certificate is available, deployment is blocked pending an explicit security decision; do not silently fall back to HTTP.

## Desired direction and scope

- Users can view both instrument calendars, manage their own profile/project/grant associations, book for themselves, and edit or cancel eligible future reservations.
- Administrators manage accounts and reference data, approve exceptions, adjust allotments, operate downtime workflows, and make audited schedule changes.
- Store temporary holds separately from bookings while presenting them as the `held` calendar state. Booking states are `tentative`, `confirmed`, `active`, `paused`, `completed`, `canceled`, and `rejected`.
- Tentative reservations occupy calendar slots. Their configurable expiration defaults to disabled; if enabled, expiration transitions them to `canceled` with a system audit reason.
- Notifications and cancellation alerts remain deferred. Persist contact, audit, approval, and schedule-change data in forms that can support notifications later.
- Preserve the existing user-stories document and all unrelated working-tree changes. Never stage, commit, or push.

## Implementation approach

1. **Canonical plan and application foundation**
   - Before implementation files, write this settled plan verbatim to `docs/uu-lab-scheduler-phase-1.md`.
   - Create an installable `instrumenthub` package with an application factory, typed environment settings, domain/service/data-access layers, Jinja templates, local static assets, CLI commands, and separate instance paths for the database, logs, backups, and secrets.
   - Use SQLAlchemy 2 for database access and a small migration runner over ordered, immutable `migrations/*.sql` files recorded in `schema_migrations`.
   - Configure SQLite foreign keys, WAL mode, busy timeout, transactional writes, and integrity checks. Use UTC storage for instants and the configured IANA timezone for calendar display and recurring block generation.
   - Model users/roles, sessions, PIs, instruments, versioned block definitions, projects, grants, project-grant associations, instrument training, allotments, calendar entries, reserved slots, bookings, holds, maintenance/outages, approval decisions, schedule-change drafts, and immutable audit events.
   - Represent every occupied instrument slot through one canonical reservation table with a database uniqueness constraint on instrument and slot start. A typed calendar-entry parent identifies whether the owner is a hold, booking, maintenance event, or outage.
   - Version block definitions with future effective dates so configuration changes never reinterpret historical bookings. Permit non-overlapping blocks, including blocks crossing midnight; do not require them to cover the full day.

2. **Milestone 1 — authentication and reference data**
   - Add an interactive `python -m instrumenthub bootstrap-admin` command that reads the initial password without exposing it in command history.
   - Use Argon2id password hashing, opaque server-side sessions, CSRF protection, secure/HttpOnly/SameSite cookies, password-change session revocation, configurable idle and absolute expiry, login throttling, and authorization checks in service methods as well as routes.
   - Require first-run setup before user access: local timezone, exactly two active instruments, each instrument’s block definitions, default 15-minute hold duration, optional tentative-expiration policy, and per-instrument training enforcement.
   - Provide administrator pages for users, roles, PIs, instruments, training, projects, grants, project-grant links, and date-bounded PI/project allotments. Missing allotments mean unlimited rather than zero.
   - Store allotments in nominal scheduled minutes. Confirmed, active, paused, and completed bookings consume allotment; tentative usage is displayed separately as pending. Canceled and rejected bookings do not consume it.
   - Add immutable audit events containing actor, action, entity, timestamp, request identifier, reason, and sanitized before/after data. Never log passwords, hashes, session tokens, or CSRF secrets.

3. **Milestone 2 — calendars and availability**
   - Add authenticated month calendars with an instrument switcher, previous/next navigation, accessible fixed-block cells, and responsive layouts.
   - Expose a small same-origin JSON interface for visible-month availability and hold refresh; keep all mutation endpoints session-authenticated and CSRF-protected.
   - Render confirmed/tentative/active/paused states, holds, maintenance, outages, and availability distinctly using labels and patterns in addition to color. Maintenance remains red; a user-selected accent color may decorate bookings without replacing state indication.
   - Show handler initials and basic booking information on the calendar. Authenticated members may open booking details and see current-user contact information; anonymous access is never allowed.
   - Allow administrators to create routine maintenance and unavailable periods, with the same slot uniqueness protection as bookings.

4. **Milestone 3 — booking and rule evaluation**
   - Acquire a hold transactionally when a user selects consecutive open blocks. Hold dates are immutable within that wizard; changing time requires releasing and reacquiring. Show a countdown, release abandoned holds, and prune expired holds before every availability or write operation.
   - Build a resumable wizard for instrument, authenticated user, PI, project, optional applicable grant, experiment code/name, requested blocks, expected duration, sample category, optional description, and administrator notes.
   - Support either direct expected duration or estimation from injection count and gradient length. Estimate sample time plus two 20-minute QC injections for each started eight hours of sample time, show the calculation, and warn when reserved time is shorter without inventing an automatic optimization policy.
   - Show relevant project consecutive-time limits and PI/project allotment balances as soon as enough fields are selected.
   - Implement a deterministic rule service returning ordered results with stable codes, severity, and plain-language explanation:
     - **Blocked:** overlapping occupancy, instrument unavailable, inactive user, missing required data, missing enforced training, or an expired/lost hold.
     - **Approval required:** nominal duration over 10,080 minutes, project consecutive-time limit exceeded, PI/project allotment exceeded, or explicit administrator-override requirement.
     - **Allowed:** no blocking or approval-required result.
   - In one SQLite `BEGIN IMMEDIATE` transaction, revalidate the hold, instrument availability, account/training state, and rules; then replace the hold with a confirmed or tentative booking and its slots. The unique slot constraint is the final double-booking safeguard.
   - On conflict, preserve non-time wizard input in the user’s session and return them to calendar selection with a clear explanation.
   - Use Post/Redirect/Get for HTML forms. The internal JSON contract is limited to calendar availability, hold acquisition/release/status, and rule previews; it is not a supported public integration API.

5. **Milestone 4 — approvals, edits, and downtime**
   - Permit owners to edit or cancel future tentative/confirmed bookings. Time edits reacquire slots and rerun all rules; approval-required edits move the booking to tentative. Active, paused, completed, rejected, historical, or administratively altered records require an administrator.
   - Reevaluate availability and rules when approving. Require a reason when an administrator overrides an approval condition, changes another user’s booking, rejects a request, pauses a booking, or modifies historical data.
   - Make booking lifecycle transitions explicit and audited. Starting, pausing, resuming, and completing experiments are administrator actions; do not infer actual experimental status solely from wall-clock time.
   - For a known-duration outage, create a non-mutating preview that pauses an affected active booking, inserts downtime, and translates the active booking’s remaining slots plus every later nonterminal booking and maintenance entry by the outage’s fixed-block span. Preserve durations, relative order, and existing inter-entry gaps.
   - For an unknown-duration outage, mark the instrument unavailable, remove current scheduled slots from affected future entries, preserve their original requested times and deterministic queue order, and set affected bookings to `paused`. When a restart is entered, preview a translated schedule based on the final outage span before changing live slots.
   - Apply an approved schedule preview in one transaction after rechecking for conflicts. Restore future bookings to `confirmed`; leave actual experiment start/resume as an explicit administrator action. Record old/current/requested times and the complete change reason in audit history.
   - Provide administrator approval, schedule-preview, outage, and audit-history pages. Do not send emails or other notifications in Phase 1.

6. **Milestone 5 — operational readiness**
   - Add structured rotating local logs with request identifiers, safe error pages, startup migration checks, health/readiness endpoints, and administrator-visible operational status without exposing secrets.
   - Add `backup`, `verify-backup`, and guarded `restore` CLI commands using SQLite’s online backup API. Supply a daily Windows Task Scheduler definition, configurable retention with a 30-day default, integrity verification, and a documented restore rehearsal.
   - Deploy Uvicorn as a single application worker managed by WinSW. Run Caddy as a separate WinSW service for the stable hostname, HTTPS termination, security headers, and reverse proxying to loopback-only Uvicorn.
   - Provide PowerShell install, upgrade, backup, restore, and uninstall runbooks. Package pinned Python dependencies, Caddy, and WinSW for offline installation; keep instance data outside the application release directory.
   - Verify service-account filesystem permissions restrict the database, secrets, logs, certificates, and backups. Document certificate renewal as an IT-owned prerequisite.
   - Add concise administrator and user guides covering setup, booking, approvals, downtime, backups, and recovery.

## Validation approach

- **After every milestone:** run the full accumulated Pytest suite, migration-from-empty tests, downgrade-free upgrade tests, and a working-tree review for unintended scope. Do not start the next milestone until the suite passes.
- **Milestone 1:** test bootstrap lockout, password/session/CSRF behavior, role boundaries, inactive users, setup completeness, CRUD validation, allotment accounting, audit redaction, and restart persistence.
- **Milestone 2:** test month boundaries, overnight blocks, timezone/DST display, block-definition versioning, calendar state rendering, authenticated contact visibility, and maintenance/outage conflicts.
- **Milestone 3:** test every rule outcome and explanation, hold expiry/release, lost holds, tentative occupancy, duration estimation, preserved form data, atomic conversion, and simultaneous competing submissions against a real temporary SQLite database; exactly one competitor must succeed.
- **Milestone 4:** test the state-transition matrix, owner/admin edit authorization, approval rechecks, required override reasons, known/unknown outage previews, duration/order preservation, preview conflicts, rollback on failure, and audit reconstruction.
- **Milestone 5:** test clean install and migration, restart persistence, backup during reads/writes, integrity verification, restore into a disposable instance, log redaction, health behavior, and offline startup.
- Perform manual acceptance on macOS during development and on the target Windows 11 host using current Edge and Chrome. Include two authenticated browsers competing for slots, service restart, certificate validation, outage preview/approval, backup restoration, keyboard-only calendar use, and all Phase 1 acceptance criteria.

## Risks, decisions, and reviewer focus

- SQLite is appropriate for the stated 20-user/single-host deployment only with short transactions, one Uvicorn worker, WAL mode, busy timeout, and concurrency regression tests. Reassess the database if deployment becomes multi-host or write volume grows materially.
- The lab must supply actual instrument names, block times, policies, users, training records, allotments, hostname, service account, and HTTPS certificate; these are deployment data, not source defaults.
- Review the canonical slot uniqueness model, authorization at service boundaries, outage reconstruction, audit completeness, local-time/DST handling, and backup restoration independently.
- The duration estimator is advisory. Sample-compatibility enforcement, automatic placement, maintenance recommendations, reports, notifications, waitlists, PI administration, and external calendars remain explicitly deferred.
