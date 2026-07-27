# Convert instrument-time accounting to hours/days

**Canonical plan document:** `docs/uu-hours-days-accounting.md`

## Intent

Replace minute-based user-facing instrument accounting with clear hour/day values, reflecting the six-hour minimum booking block. Preserve minute-based database storage and internal scheduling calculations.

## Current understanding

Minute accounting appears in allotment forms, project limits, booking balance previews, reservation details, usage reports/CSV exports, and schedule previews. Hold timers, session settings, notification intervals, and scientific gradient lengths are operational or scientific durations and remain minute-based.

## Desired direction and scope

- Add a shared duration formatter:
  - `6 hours`, `12 hours`, `1 day`, `1 day 6 hours`.
  - Use decimal hours for legacy/non-whole-hour values.
- Change user-facing accounting inputs to decimal hours:
  - Allotment amounts.
  - Project maximum consecutive time.
  - Convert submitted hours to existing minute columns at the web boundary.
- Update booking summaries, entry details, live allotment balances, reports, schedule previews, and report CSV exports to use hours/days.
- Keep existing database columns, internal calculation thresholds, and raw minute API fields for compatibility; add formatted API fields consumed by the UI.
- Keep hold durations, session/notification settings, gradient lengths, and duration-estimator internals in minutes.
- Update current documentation and focused tests to describe the new accounting units.

## Implementation approach

1. Create the canonical plan document before implementation.
2. Add a reusable formatting/parsing helper for accounting durations and decimal-hour form values.
3. Update web routes and templates for allotments and project limits, including validation messages and audit payload presentation.
4. Add formatted accounting values to rule-preview/report contexts and update JavaScript, reports, CSV output, booking details, and schedule previews.
5. Update documentation references that describe allotment or usage accounting.
6. Add focused regression coverage for formatting, hour-to-minute conversion, API/UI rendering, reports, CSV output, and preservation of excluded minute-based operational fields.

## Validation approach

- Test six-hour, day, mixed day/hour, zero, and legacy fractional-hour formatting.
- Verify hour inputs persist equivalent minute values and reject invalid/nonpositive values.
- Verify reports and CSV contain hours/days and no user-facing accounting labels containing “minutes”.
- Verify live booking balances and reservation details use the formatted values.
- Retain tests confirming hold timers and gradient lengths intentionally remain minute-based.
- Run focused accounting tests followed by the complete `pytest` suite.

## Risks, decisions, and reviewer focus

- No database migration is planned; existing minute data remains authoritative.
- Decimal-hour inputs are converted to whole minutes, preserving the current storage model.
- CSV column names for accounting durations will intentionally change from minute-oriented fields to duration-oriented fields.
- Review rounding of legacy values and confirm that no minute-based operational/scientific field was accidentally converted.
