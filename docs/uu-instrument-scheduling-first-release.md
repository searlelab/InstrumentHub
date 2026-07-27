# Class-aware calendar structure for the first release

## Summary

Introduce a small internal instrument-class model now, seeded with one class: **Mass Spectrometer**. The class will own the booking schedule, while users continue to see the existing instrument-focused calendar and booking experience.

Administrators will manage the class schedule through a high-level **Calendar Structure** section within Instruments. Schedule changes will require a future effective date and will apply to every instrument in that class. Adding new classes remains unavailable in the interface for now.

## Implementation changes

- Write this plan to `docs/uu-instrument-scheduling-first-release.md` before implementation.
- Add an internal instrument-class table and assign every instrument to a class.
- During initial setup/reset, allow the administrator to name the initial class. Existing data should be seeded as `Mass Spectrometer`.
- Move schedule ownership from individual instruments to their instrument class while preserving existing instrument-based calendar, booking, hold, maintenance, and outage routes.
- Migrate the current mass-spectrometer schedule into the seeded class structure without rewriting historical bookings or deleting existing schedule records.
- Remove per-instrument “Initial booking blocks” and future schedule editors from the Instruments interface.
- Add a **Calendar Structure** editor for the existing class:
  - Show the class name and current time blocks.
  - Allow editing block names and times.
  - Require a future effective date.
  - Apply the new structure to all instruments in the class.
  - Validate overlaps, overnight boundaries, DST behavior, and conflicts across every instrument in the class.
- Keep class creation, class deletion, and scheduling-method selection hidden until a later release.
- Preserve authorization, validation, audit events, and existing URL contracts wherever possible.

## Tests and acceptance criteria

- The migration creates the `Mass Spectrometer` class and assigns all current instruments to it.
- Setup/reset accepts the initial class name and defaults correctly.
- New instruments inherit the selected class schedule automatically.
- The Instruments UI contains no per-instrument block editor or “Initial booking blocks” field.
- Calendar Structure edits affect all instruments in the class only from the selected future date.
- Changes are rejected when any class instrument has a conflicting reservation or invalid schedule boundary.
- Existing bookings, holds, maintenance, outages, historical calendars, permissions, and audit events remain correct.
- Calendar and booking APIs continue accepting instrument identifiers while resolving schedules through the instrument’s class.
- The admin interface does not expose adding additional classes yet.
- Run the complete Pytest suite.

## Assumptions

- The current deployment contains only mass spectrometers.
- The initial standard remains Morning 06:00–12:00, Afternoon 12:00–18:00, and Overnight 18:00–06:00.
- Class-level schedule versioning replaces per-instrument schedule editing.
- Hourly and day-based methods remain future work.
