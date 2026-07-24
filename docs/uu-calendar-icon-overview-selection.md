# Calendar iconography and overview selection

**Canonical plan document:** `docs/uu-calendar-icon-overview-selection.md`

## Intent

Make calendar blocks visually compact and consistent: replace text affordances with the supplied icons, render the all-instruments view as connected, instrument-colored tracks with status-colored bookings, and enable per-instrument drag selection directly from that overview.

## Current understanding

- The individual calendar already supports contiguous open-slot selection and submits a normal hold request.
- The overview currently renders named instrument rows, textual Morning/Evening/Overnight blocks, handler initials, and no selection controls.
- Status is provided by `calendar_data`; the overview API already refreshes status changes every 15 seconds.
- The downloaded SVGs are available and will be added under `instrumenthub/static/images/calendar/`.

## Desired direction and scope

- Use `selection-plus.svg` for available individual-calendar selection affordance and `info.svg` for booked-entry details.
- In the overview, use the supplied sun/moon icons instead of block-name text; retain meaningful accessible labels and native hover details containing instrument, time block, booking title/status, and handler identity where applicable.
- Render a connected per-instrument accent rail through each overview day row; open slots use that instrument’s accent, while booked/held/maintenance/outage states keep the existing shared status palette.
- Preserve entry-detail links for non-held occupied slots. No schema, migration, authorization, or booking-rule changes.

## Implementation approach

- First write this plan unchanged to `docs/uu-calendar-icon-overview-selection.md`.
- Copy the five supplied SVGs to `instrumenthub/static/images/calendar/`, and update the calendar template/CSS to use them as compact, keyboard-accessible controls instead of text arrows and slot labels.
- Refactor calendar selection JavaScript into a shared, instrument-scoped controller usable by both layouts. In the overview, anchor a drag to its initial instrument row, select only that instrument’s contiguous available slots, and submit the selected instrument ID, first slot, and block count through the existing `/book/hold` flow.
- Add an overview selection bar equivalent to the individual calendar’s bar; clear selection on request and prevent selection gestures from hijacking occupied-entry links.
- Enrich overview slot markup/data with deterministic instrument accent assignment and tooltip text. Cycle a documented palette for instruments beyond the existing named accent set; hide repetitive instrument labels from day cells while preserving them for assistive technology and hover details.
- Update focused HTML/CSS/JS regression coverage and the user guide to describe direct overview selection and the revised icon/status semantics.

## Validation approach

- Verify each supplied SVG is served from the static asset path and replaces its intended calendar text control.
- Verify the overview contains accessible time-of-day icons, instrument/status/user hover metadata, connected accent rails, status-colored occupied blocks, and no handler initials rendered as visible block content.
- Verify overview mouse/pointer drag selects only contiguous available slots for the initially chosen instrument, produces the correct hidden hold form values, ignores bookings, and preserves individual-calendar selection behavior.
- Run the focused calendar tests, then the complete `pytest` suite; manually inspect desktop and narrow responsive layouts.

## Risks, decisions, and reviewer focus

- Chosen direction: connected instrument-color tracks across overview day cells.
- Native `title` hover text is the tooltip mechanism, paired with explicit `aria-label`s for keyboard and screen-reader access.
- Existing booking-status colors remain authoritative; only availability is instrument-accent colored.
- Review pointer-event behavior carefully so navigation to a booked entry and selection of an open overview slot remain unambiguous.
