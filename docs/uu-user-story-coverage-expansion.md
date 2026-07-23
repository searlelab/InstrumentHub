# User-story coverage expansion

**Canonical plan document:** `docs/uu-user-story-coverage-expansion.md`

## Summary

Expand coverage through an operations-and-PI release while preserving the current immediate-confirmation model for compliant requests. Add durable in-app and SMTP-backed notifications, recurring maintenance task management, administrator-facing PI/grant reporting, and targeted calendar/booking improvements. Workstation scheduling, preferred-instrument queues, automatic placement, and sample-compatibility enforcement remain deferred.

## Key changes

- First write this plan verbatim to `docs/uu-user-story-coverage-expansion.md`; do not alter it during implementation. Preserve the existing untracked `README.md`, and do not stage, commit, or push.
- Add an additive migration for notification, notification-delivery, maintenance-template, and maintenance-task records; add optional booking metadata for column setup, library presence, and stored gradient details. Existing bookings must remain valid with safe defaults.
- Create a notification service that persists an in-app notice before attempting SMTP delivery. Trigger notices only after committed booking decisions, cancellations, outage schedule application, outage recovery, and maintenance-task assignment/overdue events—never for previews. Deduplicate by event/entity/recipient, support read state, and retain delivery attempts.
- Add SMTP configuration through environment settings, disabled by default. Add a bounded `deliver-notifications` CLI command for retrying pending messages; wire it into the Windows deployment as a local scheduled task. SMTP failure must not roll back a scheduling operation: the inbox notice remains available and email is retried.
- Extend authenticated member UI with an inbox, unread count, and booking/outage status details. Add a current-instrument-user panel based on current local time, exposing the active handler’s contact details or an explicit no-active-user state. Expand booking details to show project, grant, duration, column setup, gradient, library status, and sample category.
- Add optional wizard and edit-form fields for column setup, library presence, and gradient; retain existing duration estimation and immediate confirmation for rule-compliant bookings. Exception, override, and policy-limit requests remain tentative for administrator decision.
- Add recurring maintenance templates with instrument, task name, interval, expected duration, optional assignee, and active status. Generate deterministic due tasks, let administrators assign/complete/skip them with audit reasons, and allow a generated task to create a normal maintenance calendar block. Surface overdue and upcoming tasks in schedule operations; do not auto-place or recommend schedule slots.
- Add administrator-only reporting routes and UI for grant, PI, project, user, instrument, date range, and booking-state filters. Provide an on-screen usage summary, reservation drill-down, and CSV export. Include reserved, completed, tentative, canceled, maintenance, and outage totals; support allotment context and member-usage/consecutive-booking indicators for monopolization investigations.
- Update the administrator guide, user guide, backup/deployment documentation, and Windows scripts for notification delivery, maintenance tasks, report exports, and the new operational scheduled task.

## Test plan

- Add migration-from-empty and upgrade tests for all new tables/columns.
- Test notification authorization, recipient selection, deduplication, inbox read state, SMTP-disabled behavior, failed-delivery retry, and the guarantee that previews do not notify.
- Test current-user contact resolution across active, upcoming, completed, and no-booking states; verify detail-page metadata visibility and owner/admin edit constraints.
- Test maintenance recurrence generation, deterministic due ordering, assignment, completion/skip audit events, maintenance-block conflicts, and overdue status.
- Test report authorization, filter combinations, category totals, CSV output, drill-down consistency, empty results, and exclusion of unrelated grants.
- Run targeted new tests, all milestone suites, and the complete Pytest suite. Manually exercise SMTP failure/retry, outage notifications, recurring maintenance completion, and a grant-report CSV against a disposable instance.

## Assumptions

- Administrators remain the only reporting, approval, and maintenance-management role; no PI login role is added.
- Authenticated lab members may view active-handler email and phone, matching the existing member-facing booking-detail policy.
- Compliant bookings continue to confirm immediately; only existing exception/override conditions require review.
- SMTP is a lab-managed relay configured externally; credentials are never stored in the database or deployment XML.
- Deferred: workstation/computer resources, preferred-instrument queues, automated scheduling recommendations, sample-compatibility enforcement, external calendars, and a general PI self-service portal.
