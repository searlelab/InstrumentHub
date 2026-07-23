# InstrumentHub user-story coverage report

**Assessment date:** 2026-07-23  
**Scope:** current repository, including application code, templates, migrations, documentation, and automated tests. This is an implementation assessment, not a review of the aspirational expansion plan in `uu-user-story-coverage-expansion.md`.

## Rating guide

| Rating | Meaning |
| --- | --- |
| Covered | The material workflow is implemented, persisted, access-controlled, and has automated evidence. Minor UX refinements may remain. |
| Partial | Important parts work, but one or more explicit acceptance needs are absent or materially weaker. |
| Missing | No first-class implementation was found. |

## Executive summary

InstrumentHub has a strong, tested core for two-instrument, block-based scheduling: authenticated calendars, transactional temporary holds, conflict prevention, rule evaluation, approval exceptions, maintenance blocks, outages, audit history, notifications, and administrator reports. The largest gaps are the deliberately deferred resource/queue features and the PI-facing analytics experience.

| Rating | Stories |
| --- | --- |
| Covered | 1 of 17: first booking with policy guidance; 5: confirmed-booking feedback; 6: simultaneous booking conflict protection. |
| Partial | 12 of 17: current-user contact; calendar visibility; fast expert intake; time estimation; two-instrument support; emergency-maintenance displacement; routine maintenance; emergency maintenance; unresolved outage; RPPR reporting; monopolization investigation; PI allotment visibility. |
| Missing | 2 of 17: laboratory-computer-only scheduling; laboratory-computer co-use while running/after running. |

The report treats the four dash-prefixed items in the source as distinct stories, yielding 17 stories total.

## Per-story coverage

### 1. New lab member booking their first experiment — Covered

**Implemented:** The held-time wizard previews PI/project rules as selections change, including training, availability, one-week, project-consecutive, and PI/project allotment outcomes. It displays remaining and pending allotment time, carries wizard values forward when a hold is lost, and converts an over-limit request to an administrator-reviewed tentative booking rather than confirming it. Server-side evaluation repeats the checks at submission.

**Evidence:** `instrumenthub/rules.py`, `instrumenthub/static/app.js`, `instrumenthub/booking.py`; `tests/test_milestone3.py::test_booking_wizard_has_live_duration_rules_and_allotment_balances`, `::test_lost_hold_returns_to_calendar_and_retains_wizard_fields`, and allotment/rule tests.

**Improve:** Make policy messages more user-oriented (for example, name the affected PI/project and display hours/days as well as minutes). Also make the consequence of a tentative request explicit before submission; the current confirmation path uses the same held-time flow as an immediate confirmation.

### 2. Contact the current instrument user during an interruption — Partial

**Implemented:** Authenticated users see a current-user panel on each instrument calendar with name, email, and phone, computed from an active/confirmed booking that spans the current time. Booking details also expose handler contact information, and the no-active-user state is explicit.

**Evidence:** `instrumenthub/web.py` calendar query, `instrumenthub/templates/calendar.html`, `instrumenthub/templates/entry_detail.html`; `tests/test_milestone2.py::test_authenticated_member_can_view_booking_contact_later` and `tests/test_user_story_expansion.py::test_usage_report_handles_empty_filters_and_calendar_shows_no_current_user`.

**Gaps / improve:** The requested hover interaction is not present; it is a calendar-level panel instead. Contact data is visible to every authenticated member, so the lab should explicitly approve that privacy model and add a role/privacy setting if needed. Add a focused test for the active-booking boundary and contact changes when a profile is edited.

### 3. See who is coming up in the schedule — Partial

**Implemented:** Members have month calendars for any active instrument and a compact, read-only all-instruments overview with booking-handler initials, patterned maintenance/outage/tentative/held states, and safe entry links. Individual calendar blocks use a right-aligned expand triangle for details, keeping block content to two lines. Owner/admin-only editing, transactional conflict protection, and 15-second availability refresh remain in place.

**Evidence:** `instrumenthub/templates/calendar.html`, `instrumenthub/static/app.js`, `instrumenthub/scheduling.py`; `tests/test_milestone2.py::test_calendar_renders_both_instruments_and_local_blocks`, `::test_calendar_includes_booking_handler_initials`, `::test_overview_links_confirmed_entries_but_not_holds`, and `tests/test_milestone3.py::test_simultaneous_holds_have_exactly_one_winner`.

**Gaps / improve:** There are state colors, but no personalized member color. The application authenticates edits with sessions and CSRF protection rather than a separate code prompt, which is a reasonable security control but differs from the wording. Cancellation notifies the booking owner, not interested lab members who want to claim newly free time; there is no watchlist, broadcast, or waitlist. Add calendar subscription/availability notifications and an accessible member-color scheme.

### 4. Experienced member schedules a new experiment quickly — Partial

**Implemented:** Drag/click selection and a booking wizard collect title, PI/project/grant, expected duration or injections plus gradient, sample category, description, column setup, library presence, and administrator notes. Intakes persist with their creation timestamp. Exceptions become tentative and can be approved/rejected by an administrator; owners and admins can move or extend eligible bookings. The estimator uses injection count × gradient plus 40 minutes of QC per started 8 hours.

**Evidence:** `instrumenthub/templates/booking_form.html`, `instrumenthub/booking.py`, `instrumenthub/rules.py`, `instrumenthub/management.py`; `tests/test_milestone3.py::test_duration_estimator_adds_qc_per_started_eight_hours`, and `tests/test_milestone4.py::test_moving_a_multiblock_booking_preserves_its_size_and_audits_the_interval`.

**Gaps / improve:** There is no earliest-available finder or scheduling recommendation. Compliant requests are immediately confirmed, so the stated weekly human review queue applies only to exception requests. Maintenance is not automatically inserted between experiments, and sample category is captured but not used to prevent incompatible sequencing. The chosen calendar blocks are not derived from estimated duration; the UI only warns when the estimate exceeds held time. Implement an availability search/recommendation service and make duration-to-block capacity validation explicit.

### 5. Know when a successfully scheduled experiment is in the schedule — Covered

**Implemented:** A successful submission redirects to a persisted entry-detail page with status, scheduled span, project, grant, handler contact, and requested operational details. The calendar reads the same durable reserved-slot records, and failures return actionable errors without silently creating a booking.

**Evidence:** `instrumenthub/booking.py`, `instrumenthub/templates/entry_detail.html`, `instrumenthub/scheduling.py`; `tests/test_milestone3.py::test_hold_to_confirmed_booking_and_contact_detail`.

**Improve:** Redirecting to the entry is clear, but the story explicitly asks to see the calendar immediately. Add a confirmation banner and a "View on calendar" link/date focus, plus an end-to-end test that reloads the calendar after submission.

### 6. Two members schedule the same time — Covered

**Implemented:** Selecting blocks creates a database-backed temporary hold that occupies the same reservation ranges as a booking, has an expiry, and is displayed as held. Creation is serialized and range overlap is enforced both in application logic and the database. A competing request receives an availability error; the calendar refreshes availability every 15 seconds.

**Evidence:** `instrumenthub/booking.py`, `instrumenthub/scheduling.py`, migrations `004_reservation_range_guards.sql` and `005_booking_mutation_guards.sql`; `tests/test_milestone3.py::test_first_hold_wins_for_competing_users`, `::test_simultaneous_holds_have_exactly_one_winner`, and hold/submission race tests.

**Improve:** Make the expiry/release behavior more visible before a user starts the form and add a user-facing countdown on the calendar hold itself. The 15-second poll is good but is not a push refresh.

### 7. Experiment displaced by emergency maintenance — Partial

**Implemented:** Administrators can create a non-mutating outage preview, review a mapping of old to proposed slots, apply it atomically, preserve booking duration/order through block-aligned shifts, mark impacted active bookings paused, and notify affected booking owners through the inbox/optional SMTP.

**Evidence:** `instrumenthub/management.py`, `instrumenthub/templates/admin_schedule.html`, `instrumenthub/notifications.py`; `tests/test_milestone4.py::test_known_outage_preview_is_nonmutating_then_shifts`, `::test_outage_preview_renders_slot_mapping_and_application_audits_it`, and DST/long-horizon outage tests.

**Gaps / improve:** Notifications say that the schedule changed but do not communicate a tailored status, relative order, required action, or promised next update. There is no acknowledgement workflow. Add structured displacement fields and per-user before/after times in the notification/detail page.

### 8. How much time to schedule — Partial

**Implemented:** Members can enter expected minutes or an injection count and gradient. The application provides a live estimate with the stated QC rule and warns if expected/estimated duration exceeds held capacity.

**Evidence:** `instrumenthub/rules.py::estimate_duration_minutes`, `instrumenthub/static/app.js`; `tests/test_milestone3.py::test_duration_estimator_adds_qc_per_started_eight_hours`.

**Gaps / improve:** The estimate is not translated into a recommended number of blocks or an earliest feasible span, and a warning does not prevent undersized reservations. Add capacity matching and an "adjust my held blocks" action.

### 9. Reserve the laboratory computer separately from an active experiment — Missing

**Finding:** There is no resource model, workstation calendar, workstation booking type, or separate availability policy. The repository scope explicitly lists workstation scheduling as deferred in `README.md`.

**Recommended next slice:** Add generic bookable resources first, then introduce a `workstation` resource type with independent slots, permissions, and reporting.

### 10. Use the laboratory computer while running or immediately after samples — Missing

**Finding:** No implementation links a computer reservation to an instrument booking, enforces a co-use/after-run entitlement, or provides post-run handoff time.

**Recommended next slice:** Build on the resource model above with an optional booking association and policies for concurrent and grace-period workstation use.

### 11. Two instruments in the preceding stories — Partial

**Implemented:** Setup requires one instrument, and only after setup can administrators add further instruments one at a time using the configured lab timezone. Users can choose an individual instrument or the read-only all-instruments overview; holds are independent per instrument, and reports/maintenance policies filter by instrument.

**Evidence:** `instrumenthub/services.py` setup and creation service, `instrumenthub/templates/calendar.html`, `instrumenthub/booking.py`; `tests/test_milestone1.py::test_authentication_setup_gate_and_single_instrument`, `::test_instrument_creation_is_unavailable_until_setup`, `::test_admin_can_add_instrument_and_calendar_overview_uses_active_instruments`, and `tests/test_milestone3.py::test_holds_on_both_instruments_are_independent`.

**Gaps / improve:** There is no preferred-instrument field, cross-instrument availability search, queue, fallback preference, or automatic placement. Add instrument preferences to the intake and a recommendation view before considering automatic scheduling.

### 12. Administrator schedules routine maintenance — Partial

**Implemented:** Administrators can create recurring maintenance templates with interval, expected duration, optional assignee, activate/deactivate them, generate deterministic due tasks, assign, complete/skip, audit changes, and turn a task into a conflict-protected calendar block. Due/overdue tasks appear in schedule operations and assignees receive durable notifications.

**Evidence:** `instrumenthub/maintenance_tasks.py`, `instrumenthub/templates/maintenance_tasks.html`, migration `006_user_story_coverage.sql`; tests in `tests/test_user_story_expansion.py` for recurrence, assignment, scheduling, skipping, and auditing.

**Gaps / improve:** Only administrators can mark work complete; the story calls for users to do so. Due tasks are generated through application operations rather than a dedicated recurring background job, and no scheduling recommendation is made. Provide assignee completion with reason/audit, automated daily task generation, and a suggested conflict-free slot.

### 13. Administrator schedules emergency maintenance — Partial

**Implemented:** Administrators enter outage duration and reason, preview a shifted calendar, approve the exact mapping, preserve routine maintenance and booking slot durations when the block layout permits, apply repeatedly, and notify affected users only when a draft is applied (not while merely previewing).

**Evidence:** `instrumenthub/management.py::preview_outage` and `::apply_schedule_draft`; `tests/test_milestone4.py` outage preview, stale-preview, partial-day, and repeated-resume coverage.

**Gaps / improve:** The required staged notification policy (next-week users immediately; later users after conclusion) is not implemented: all affected booking owners are notified on apply. The UI also does not show an administrator a notification recipient/timing preview. Add a notification plan to the draft and test its deduplication/timing.

### 14. Administrator pauses an unresolved outage — Partial

**Implemented:** An unknown-duration outage removes future reserved slots while retaining bookings, their order, requested details, and pre-pause state; it marks the instrument unavailable, suppresses repeated notifications by notifying only on applied changes, allows a restart-date reconstruction preview, and applies only after review. A recovery route exists for irrecoverable cases.

**Evidence:** `instrumenthub/management.py::preview_resume`, `::apply_schedule_draft`, and `::recover_unknown_outage`; `tests/test_milestone4.py::test_unknown_outage_pauses_and_resume_reconstructs`, `::test_only_one_unknown_outage_can_be_applied`, and recovery tests.

**Gaps / improve:** Slotless paused future bookings are not rendered as ordered pending items on the member calendar, so the calendar does not fully communicate the promised pending queue. Add an explicit "pending after outage" panel ordered by `queue_order`, including the most recent update and next update expectation.

### 15. PI builds a grant-specific NIH RPPR report — Partial

**Implemented:** Administrator-only reports filter by grant, PI, project, user, instrument, date range, and state. They provide state totals, allotment context, member usage, reservation drill-down, and CSV export with formula-injection protection. Canceled/rejected historic bookings distinguish missing historical reserved-minute snapshots.

**Evidence:** `instrumenthub/reporting.py`, `instrumenthub/templates/reports.html`, `instrumenthub/web.py` report routes; `tests/test_user_story_expansion.py::test_canceled_usage_and_csv_link_keep_the_selected_grant`, `::test_historical_canceled_booking_reports_snapshot_unavailable`, and `::test_csv_formula_cells_are_escaped`.

**Gaps / improve:** There is no PI role or self-service report access, visual chart, report notes field, or completed-vs-reserved interpretation beyond status totals. Add a PI-scoped authorization model, report visualizations, notes/description columns, and a documented RPPR export format.

### 16. PI investigates instrument monopolization — Partial

**Implemented:** The report can filter by member/instrument/window and shows reserved minutes, longest consecutive minutes, reservation drill-down, and allotment context. Schedule changes and booking actions are auditable.

**Evidence:** `instrumenthub/reporting.py`, `instrumenthub/templates/reports.html`, `instrumenthub/services.py` audit service; `tests/test_user_story_expansion.py::test_report_shows_allotment_context_and_longest_consecutive_usage`.

**Gaps / improve:** It does not calculate number of reservations, average reservation length, capacity share, other-user comparison, prime-time use, extensions, or automated fairness flags. Add a dedicated fairness report with these measures and explicit thresholds; retain the drill-down for explainability.

### 17. PI views lab usage and remaining allotment — Partial

**Implemented:** Allotment calculations distinguish used and tentative minutes and report remaining capacity. Reports can be filtered to PI/project/grant/user and show per-user use and reservation records. A time-limit request becomes tentative for administrator review with the relevant booking context.

**Evidence:** `instrumenthub/rules.py::allotment_balances`, `instrumenthub/reporting.py`, `instrumenthub/templates/reports.html`; `tests/test_milestone3.py::test_allotment_balance_reports_tentative_minutes_as_pending` and `tests/test_user_story_expansion.py::test_report_shows_allotment_context_and_longest_consecutive_usage`.

**Gaps / improve:** The story's PI self-service experience is absent; reports are administrator-only. There is also no consolidated PI dashboard showing project/grant rollups and an approval decision view. Add PI-scoped accounts/dashboard or consciously re-scope the story to administrators acting for PIs.

## Recommended delivery priorities

1. **Resource and entitlement model:** implement workstation scheduling and linked post-run/computing access (stories 9–10).
2. **Scheduling intelligence:** earliest-availability search, duration-to-block matching, maintenance-gap suggestions, preferred instruments, and sample-compatibility rules (stories 4, 8, 11).
3. **Operational communication:** cancellation availability subscriptions, structured emergency-displacement notices, pending outage queue, and staged notification plans (stories 3, 7, 13–14).
4. **PI experience and analytics:** PI-scoped access, RPPR-ready export/charting, and a fairness dashboard with capacity-share/comparison metrics (stories 15–17).
5. **Calendar polish:** member colors, current-user hover affordance, and explicit post-submit calendar confirmation (stories 2, 3, 5).

## Verification performed

The test collection contains 171 tests. Focused runs completed successfully for 49 foundational/calendar tests, 104 booking/outage/coverage-expansion tests, and 17 operational tests; one optional offline-wheelhouse smoke test was skipped because its environment variable was not configured. The suite emitted only the existing FastAPI `TestClient` deprecation warning.
