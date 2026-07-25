# Faster, clearer instrument booking

**Canonical plan document:** `docs/uu-faster-clearer-instrument-booking.md` will be created after plan approval.

## Intent

Redesign booking for expert speed and beginner clarity while preserving holds, rule enforcement, audit history, and existing bookings.

## Current understanding

- Booking currently presents every field at once.
- Expected minutes—or injection count plus gradient—is mandatory even though the reserved blocks already define duration.
- PI, project, and grant records can only be created from Administration.
- PI/project/grant choices are not progressively filtered.
- Sample category is unexplained free text. Its operational purpose is sequencing experiments to manage contamination and carryover risk.
- “Administrator override” does not explain when to use it or that it creates a tentative request requiring review.
- Rule feedback is an unstructured text string.

## Desired direction and scope

- Present a compact reservation summary and a short primary form: PI, project, optional grant, experiment name, and sample category.
- Filter projects by PI and grants by project.
- Add inline, progressively disclosed “Create PI,” “Create project,” and “Create grant” actions without leaving or losing the held reservation.
- Make these quick-create actions available to authenticated bookers, audit every creation, automatically associate a newly created project with its creator, and link newly created grants to the selected project.
- Remove injection count and expected minutes from the booking UI. Continue accepting them server-side for compatibility; when omitted, store the reserved block duration as the planned duration.
- Keep gradient, column setup, libraries, description, and scheduler notes optional inside a collapsed “Run details” section.
- Explain sample category as information administrators use to sequence experiments and reduce carryover risk. Offer clear examples while retaining flexible text for lab-specific categories.
- Replace “Request administrator override” with a clearly explained “Request administrator review” option. Explain qualifying situations, warn that submission will remain tentative, reveal a required justification when selected, and show automatic rule exceptions without requiring the user to diagnose policy themselves.
- Render rule checks as readable status cards with plain-language outcomes and the expected submission result.
- Add accessible hover/focus help for unfamiliar fields, plus persistent short hints so touch and keyboard users receive the same guidance.
- Apply the same optional-duration behavior and terminology to booking edits and details.
- Preserve existing database schema and historical data.

## Implementation approach

1. Create the canonical plan document.
2. Add focused booking-reference creation services and JSON endpoints with validation, duplicate protection, relationships, authorization, and auditing.
3. Update booking submission/edit handling so duration inputs are optional and reserved duration is the deterministic fallback.
4. Rebuild the booking form structure and related detail/edit wording.
5. Add dependent PI/project/grant controls, inline creation, optional-section behavior, override justification, and structured rule feedback in JavaScript.
6. Add responsive booking-specific styling and accessible tooltips using the existing visual system.
7. Update the user guide and focused regression coverage.

## Validation approach

- Test duration-free booking and compatibility with legacy duration inputs.
- Test quick creation, duplicate rejection, project membership, grant linking, auditing, and authorization.
- Test PI/project/grant filtering and saved-form recovery.
- Test override justification and tentative outcomes.
- Run targeted booking tests, then the complete Pytest suite.
- Render and manually exercise the booking flow at desktop and mobile widths.

## Risks, decisions, and reviewer focus

The material policy decision is allowing any authenticated booker to quick-create PI/project/grant records. This best satisfies the requested in-booking workflow, but it broadens reference-data creation beyond administrators. All such changes will be audited and duplicate-protected.

Please approve this plan, or tell me if quick creation should remain administrator-only.
