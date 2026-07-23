# Extensible instruments and all-calendar overview

## Intent

Remove the two-instrument setup constraint, allow administrators to add one fully configured instrument at a time after setup, and add a compact all-instruments calendar view. Existing per-instrument booking, holds, maintenance, and reporting behavior remains intact.

## Current understanding

- The schema and core scheduling services are already instrument-scoped and support any number of records.
- Only initial setup and its form handler enforce exactly two instruments.
- The calendar is single-instrument and renders every block as a separate vertical card.
- No migration is needed: `instruments` and `block_definitions` already support the intended model.

## Implementation approach

- Create the canonical plan document, then refactor initial setup to collect and validate exactly one instrument with its name, training requirement, and initial block definitions.
- Extract the shared “create instrument plus initial blocks” service logic from setup. It will validate block definitions against the configured timezone, rely on the existing case-insensitive unique name constraint, create the instrument active, and write an audit event.
- Add an administrator-only single-instrument creation form to Admin. It uses the shared service and immediately makes the new instrument available to calendar switching, booking, training, maintenance, allotments, and reports.
- Update calendar routing and template navigation with an **All instruments** toggle. The existing individual view remains the only booking-enabled view.
- Implement the all-instruments monthly overview by collecting the active instruments in deterministic name order and grouping each day’s slots into one compact inline row per instrument. Each block is a small status-colored segment with its block name and handler initials; occupied entries retain their detail links. The overview has no drag-to-hold controls and directs users to an individual instrument view to book.
- Add an all-instruments calendar API response and polling behavior so the overview refreshes when any displayed slot status changes, matching the existing 15-second individual-calendar availability update.
- Add responsive styling so an instrument’s block segments remain inline and wrap at most naturally to a second line before expanding the day card; preserve the current detailed three-card layout on individual calendars.
- Update the README, administrator guide, user guide, and coverage documentation to describe one-instrument setup, administrator-added instruments, and the summary calendar. Replace stale “exactly two” wording.

## Test plan

- Verify initial setup succeeds with one instrument, persists its block definitions, rejects invalid/duplicate names or invalid block layouts, and remains locked until that single instrument is configured.
- Verify an administrator can add a second and later instrument; non-admin and CSRF-invalid requests are rejected; the new instrument appears in calendar, booking, and existing instrument-filter controls.
- Preserve regression coverage that holds remain independent per instrument and that bookings, maintenance, and reports remain correctly scoped.
- Verify the all-instruments calendar shows every active instrument, groups each instrument’s blocks into compact daily rows, renders states/entry details correctly, excludes inactive instruments, and exposes no hold-selection controls.
- Run the focused milestone and expansion suites, then the complete `pytest` suite.

## Assumptions

- “All instruments” is a calendar toggle, not the default landing view.
- The all-instruments view includes active instruments only and is read-only; booking starts from an individual instrument calendar.
- Existing two-instrument deployments remain valid without data migration and automatically gain the overview and future-add capability.
