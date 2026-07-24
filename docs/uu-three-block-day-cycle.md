# Three-block day cycle

**Canonical plan document:** `docs/uu-three-block-day-cycle.md`

## Intent

Replace the current Morning/Evening/Overnight pattern with Morning (06:00–12:00), Afternoon (12:00–18:00), and Overnight (18:00–06:00). Render each calendar day in that exact order: Morning, Afternoon, Overnight.

## Current understanding

- Blocks are already sorted by local start time for scheduling and rendering, so the new 06:00/12:00/18:00 definitions naturally preserve both chronological display and consecutive-block selection.
- The overview currently maps only `Morning` to a morning icon; any non-Morning/non-Evening name is treated as night.
- `instance/instrumenthub.sqlite3` is an ignored local example database with legacy definitions and one test booking. It is safe to clear its schedule data.

## Desired direction and scope

- Use the new three-block pattern as the initial setup default and as the standard test fixture.
- Do not add a repository migration that could erase schedules in a future deployed instance.
- Perform a one-time, local-only reset of the checked-in workspace’s ignored example database: clear bookings, holds, maintenance/outage calendar entries, reservations, maintenance scheduling records, and schedule drafts; replace every instrument’s definitions with the three new blocks effective immediately. Preserve users, instruments, setup settings, and audit history.

## Implementation approach

1. Create this canonical plan document before implementation.
2. Update setup defaults, calendar icon classification, and user-facing wording from “Evening” to “Afternoon”; map Afternoon to the afternoon/sun icon and Overnight to the moon icon.
3. Update focused scheduler and calendar tests to assert:
   - definitions and slots are ordered Morning → Afternoon → Overnight;
   - times and nominal durations are 6h, 6h, and 12h;
   - a three-block hold is continuous from 06:00 through the following 06:00;
   - individual and overview calendars render blocks in the requested order with the correct accessible labels and icons.
4. Update the shared configured fixture to exercise the standard three-block pattern, while retaining dedicated partial/custom-block coverage.
5. Reset the local example database transactionally after stopping any running app process, then verify its new block rows and absence of scheduled entries.

## Validation approach

- Run the timing, calendar, booking-hold, and migration test modules, then the full `pytest` suite.
- Inspect the local database after reset for each instrument’s ordered definitions: Morning 06:00–12:00, Afternoon 12:00–18:00, Overnight 18:00–06:00.
- Manually load the all-instruments and single-instrument calendar views to confirm visual order, icon mapping, and cross-midnight selection behavior.

## Risks, decisions, and reviewer focus

- Clearing schedule data is intentionally destructive but explicitly limited to the ignored local example database; it must not be encoded as an automatic application migration.
- The overnight block belongs visually to the day it starts (18:00), even though it ends at 06:00 the next day; this makes the displayed sequence match reservation continuity.
- Review that legacy terminology is removed only where it describes the standard pattern, while generic/custom block support remains unchanged.
