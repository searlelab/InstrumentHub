# GitHub-style administration settings dashboard

**Canonical plan document:** `docs/uu-admin-sidebar-tabs.md`

## Intent

Transform Administration into a polished, GitHub-settings-inspired dashboard with persistent sidebar navigation, clear ownership-based sections, compact cards, consistent iconography, and responsive behavior. Preserve existing permissions, workflows, audit events, routes, and data rules.

## Design direction

- Use a light settings-dashboard shell with white content surfaces, subtle borders, restrained shadows, and the existing evergreen/tide palette.
- Add a two-column layout:
  - Sticky left sidebar with grouped navigation.
  - Main content column with page heading, description, primary action, and focused management content.
- Sidebar groups:
  - **Manage:** Users, Instruments, Projects & grants, Training, Allotments.
  - **Operations:** Approvals & outages, Maintenance, Usage reports, System status.
- Use URL-addressable tabs such as `/admin?tab=users`.
- Render only the selected management panel.
- On small screens, convert the sidebar to a horizontally scrollable or stacked navigation bar above the content.

## Unified icon assets

Consolidate all Phosphor SVGs into one flat directory:

`instrumenthub/static/icons/`

Move the existing calendar assets out of `instrumenthub/static/images/calendar/` into this directory, alongside the new administration icons.

Use icons such as:

- `users.svg`
- `test-tube.svg`
- `folder-simple.svg`
- `graduation-cap.svg`
- `clock.svg`
- `calendar-check.svg`
- `wrench.svg`
- `chart-line.svg`
- `gear.svg`
- `plus.svg`
- `arrow-right.svg`
- `caret-right.svg`
- `check.svg`
- `x.svg`

Include the Phosphor MIT license in the asset directory or the repository’s third-party notices. Update all template, CSS, test, and documentation references from `/static/images/calendar/...` to `/static/icons/...`, then remove the old calendar-icon directory.

## Management restructuring

- **Users:** user table, add-user disclosure, role changes, activation, and password resets.
- **Instruments:** instrument creation, future block versions, activation, and training policy.
- **Projects & grants:** PI/project/grant creation, active/inactive status, and project-grant links.
- **Training:** record/revoke training and view training records.
- **Allotments:** create, edit, end, and review allotments.

Remove “Manage reference data” completely. Every control should be presented under the entity it modifies.

Each tab will use the same visual rhythm:

1. Page title and short explanation.
2. Primary action or expandable creation form.
3. Current records in a bordered table/card list.
4. Secondary policy or relationship controls below.

## Implementation approach

1. Write this exact plan to `docs/uu-admin-sidebar-tabs.md` before implementation changes.
2. Update `instrumenthub/templates/admin.html` with the sidebar shell, tab panels, contextual headings, and icon references.
3. Update `instrumenthub/web.py` to:
   - Validate supported `tab` values.
   - Default missing or invalid tabs to Users.
   - Preserve tab context in admin form redirects.
   - Keep existing POST endpoints and authorization/audit logic compatible.
4. Update `instrumenthub/static/app.css` with:
   - Sidebar/content layout.
   - Active navigation treatment.
   - Settings-style cards and section headers.
   - Balanced responsive form grids.
   - Consistent control widths, top alignment, table overflow, and mobile behavior.
   - Shared sizing/color rules for icons.
5. Remove obsolete JavaScript that dynamically rewrites generic reference-data form actions.
6. Move existing calendar SVGs and add selected Phosphor assets under `instrumenthub/static/icons/`.
7. Update calendar templates and related documentation/tests to use the unified icon path.
8. Update `docs/admin-guide.md` and `docs/uu-calendar-icon-overview-selection.md` with the new navigation and asset locations.

## Validation approach

- Add tests for:
  - Default and explicit admin tabs.
  - Invalid-tab fallback.
  - Active navigation state and accessible labels.
  - Absence of “Manage reference data”.
  - Correct placement of management controls.
  - POST redirects preserving the originating tab.
  - Existing calendar assets loading from `/static/icons/...`.
  - Existing mutation, authorization, validation, and audit behavior.
- Run the complete Pytest suite.
- Manually inspect desktop and mobile layouts, keyboard navigation, focus states, table overflow, collapsed creation forms, and the Training/Allotments balance.
- Verify all icon references resolve and no references to `/static/images/calendar/` remain.

## Assumptions and reviewer focus

- Training and Allotments remain separate tabs.
- `/admin` remains the canonical route; tabs use a query parameter.
- The old calendar asset path is an internal static path and will be replaced rather than retained as a duplicate.
- No database migration or permission change is needed.
- Reviewers should verify every existing admin action remains discoverable and that no audit-sensitive behavior changes.
