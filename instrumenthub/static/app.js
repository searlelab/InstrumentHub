"use strict";

/* Confirmation prompts */
document.querySelectorAll("[data-confirm]").forEach((element) =>
  element.addEventListener("submit", (event) => {
    if (!window.confirm(element.dataset.confirm)) event.preventDefault();
  })
);

/* Administration: keep creation controls in place while expanding their editor below. */
document.querySelectorAll("[data-admin-disclosure-toggle]").forEach((button) => {
  const panel = document.getElementById(button.getAttribute("aria-controls"));
  if (!panel) return;
  button.addEventListener("click", () => {
    const expanded = button.getAttribute("aria-expanded") === "true";
    button.setAttribute("aria-expanded", String(!expanded));
    panel.hidden = expanded;
  });
});

/* Hold countdown */
const hold = document.querySelector("[data-hold-expires]");
if (hold) {
  const output = hold.querySelector("[data-countdown]");
  const tick = () => {
    const seconds = Math.max(0, Math.floor((Date.parse(hold.dataset.holdExpires) - Date.now()) / 1000));
    output.textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")} remaining`;
  };
  tick();
  window.setInterval(tick, 1000);
}

/* Calendar: instrument-scoped drag selection, shared by individual and overview views. */
document.querySelectorAll("[data-selection-calendar]").forEach((calendar) => {
  const bar = calendar.parentElement.querySelector("[data-select-bar]");
  const summary = bar && bar.querySelector("[data-select-summary]");
  const instrumentField = bar && bar.querySelector("[data-select-instrument]");
  const startField = bar && bar.querySelector("[data-select-start]");
  const countField = bar && bar.querySelector("[data-select-count]");
  const slots = () => Array.from(calendar.querySelectorAll("[data-start]"));
  let anchor = null;
  let dragging = false;

  const clearSelection = () => {
    slots().forEach((node) => node.classList.remove("selected"));
    if (bar) bar.classList.remove("visible");
    anchor = null;
  };

  // A run may only stay on the instrument where the selection began.
  const runBetween = (from, to) => {
    if (!from || !to || from.dataset.instrument !== to.dataset.instrument) return [];
    const instrumentSlots = slots().filter((node) => node.dataset.instrument === from.dataset.instrument);
    let a = instrumentSlots.indexOf(from);
    let b = instrumentSlots.indexOf(to);
    if (a < 0 || b < 0) return [];
    if (a > b) [a, b] = [b, a];
    const span = instrumentSlots.slice(a, b + 1);
    if (span.some((node) => node.dataset.status !== "available")) return [];
    if (span.some((node, index) => index > 0 && span[index - 1].dataset.end > node.dataset.start)) return [];
    return span;
  };

  const paint = (focus) => {
    const run = runBetween(anchor, focus);
    slots().forEach((node) => node.classList.remove("selected"));
    if (!run.length || !bar) return;
    run.forEach((node) => node.classList.add("selected"));
    instrumentField.value = run[0].dataset.instrument;
    startField.value = run[0].dataset.start;
    countField.value = run.length;
    summary.textContent = `${run.length} open block${run.length > 1 ? "s" : ""} selected`;
    bar.classList.add("visible");
  };

  calendar.addEventListener("pointerdown", (event) => {
    const slot = event.target.closest("[data-select-slot]");
    if (!slot || slot.dataset.status !== "available") return;
    if (event.target.closest("a")) return; // retain the individual calendar's keyboard fallback link
    event.preventDefault();
    dragging = true;
    calendar.classList.add("dragging");
    anchor = slot;
    paint(slot);
  });

  calendar.addEventListener("pointerover", (event) => {
    if (!dragging) return;
    const slot = event.target.closest("[data-start]");
    if (slot) paint(slot);
  });

  const endDrag = () => {
    dragging = false;
    calendar.classList.remove("dragging");
  };
  window.addEventListener("pointerup", endDrag);
  window.addEventListener("pointercancel", endDrag);

  calendar.addEventListener("click", (event) => {
    if (event.target.closest("a")) return;
    const slot = event.target.closest("[data-select-slot]");
    if (slot && slot.dataset.status === "available" && !slot.classList.contains("selected")) {
      anchor = slot;
      paint(slot);
    }
  });

  if (bar) bar.querySelector("[data-select-clear]").addEventListener("click", clearSelection);
});

/* Individual calendar: refresh availability unless a selection is in progress. */
const calendar = document.querySelector(".calendar[data-instrument][data-month]");
if (calendar) {
  const bar = calendar.parentElement.querySelector("[data-select-bar]");
  const slots = () => Array.from(calendar.querySelectorAll(".slot[data-start]"));

  /* Live availability refresh — skip while a selection is in progress. */
  window.setInterval(async () => {
    if (bar && bar.classList.contains("visible")) return;
    const response = await fetch(
      `/api/calendar?instrument_id=${encodeURIComponent(calendar.dataset.instrument)}&month=${encodeURIComponent(calendar.dataset.month)}`
    );
    if (!response.ok) return;
    const payload = await response.json();
    const current = new Map(
      slots().map((node) => [node.dataset.start, node.dataset.status])
    );
    if (payload.slots.some((slot) => current.get(slot.start) !== slot.status)) window.location.reload();
  }, 15000);
}

/* All-instruments overview: refresh only when an instrument/block status changes. */
const overview = document.querySelector("[data-calendar-overview][data-month]");
if (overview) {
  const overviewSlots = () => Array.from(overview.querySelectorAll("[data-overview-slot]"));
  window.setInterval(async () => {
    const response = await fetch(`/api/calendar?view=all&month=${encodeURIComponent(overview.dataset.month)}`);
    if (!response.ok) return;
    const payload = await response.json();
    const current = new Map(overviewSlots().map((node) => [`${node.dataset.instrument}:${node.dataset.start}`, node.dataset.status]));
    if (payload.slots.length !== current.size || payload.slots.some((slot) => current.get(`${slot.instrument_id}:${slot.start}`) !== slot.status)) window.location.reload();
  }, 15000);
}

/* Booking wizard: dependent references, quick create, and live rule preview. */
const wizard = document.querySelector("[data-booking-wizard]");
if (wizard) {
  const field = (name) => wizard.elements.namedItem(name);
  const preview = document.querySelector("[data-rule-preview]");
  const piField = field("pi_id");
  const projectField = field("project_id");
  const grantField = field("grant_id");
  const reviewToggle = wizard.querySelector("[data-review-toggle]");
  const reviewNotes = wizard.querySelector("[data-review-notes]");
  const optionalDetails = wizard.querySelector(".booking-optional");
  const submitButton = wizard.querySelector("[data-booking-submit]");

  const optionIsAvailable = (option, dataName, selectedId) =>
    !option.value || String(option.dataset[dataName] || "").split(",").includes(String(selectedId));

  const filterReferences = () => {
    const pi = piField.value;
    Array.from(projectField.options).forEach((option) => {
      option.hidden = !optionIsAvailable(option, "piId", pi);
      option.disabled = option.hidden;
    });
    if (projectField.selectedOptions[0]?.disabled) projectField.value = "";
    const project = projectField.value;
    Array.from(grantField.options).forEach((option) => {
      option.hidden = !optionIsAvailable(option, "projectIds", project);
      option.disabled = option.hidden;
    });
    if (grantField.selectedOptions[0]?.disabled) grantField.value = "";
    const empty = wizard.querySelector("[data-project-empty]");
    if (empty) empty.hidden = !pi || Array.from(projectField.options).some((option) => option.value && !option.disabled);
  };

  const syncReviewRequest = () => {
    const requested = reviewToggle.checked;
    reviewNotes.required = requested;
    wizard.querySelector("[data-notes-optional]").textContent = requested ? "required for review" : "optional";
    wizard.querySelector("[data-review-hint]").textContent = requested
      ? "Explain the exception or special circumstance so an administrator can decide."
      : "Visible to administrators reviewing the schedule.";
    submitButton.textContent = requested ? "Submit for administrator review" : "Confirm booking";
    if (requested) optionalDetails.open = true;
  };

  const renderRulePreview = (data) => {
    preview.replaceChildren();
    data.results.forEach((result) => {
      const item = document.createElement("div");
      item.className = `rule-result ${result.severity.replace("_", "-")}`;
      const marker = document.createElement("span");
      marker.className = "rule-marker";
      marker.setAttribute("aria-hidden", "true");
      marker.textContent = result.severity === "allowed" ? "✓" : result.severity === "blocked" ? "!" : "↗";
      const body = document.createElement("div");
      const title = document.createElement("strong");
      title.textContent = result.severity === "allowed" ? "Ready to confirm" : result.severity === "blocked" ? "Needs attention" : "Administrator review required";
      const message = document.createElement("p");
      message.textContent = result.message;
      body.append(title, message);
      item.append(marker, body);
      preview.append(item);
    });
    data.allotment_balances.forEach((balance) => {
      const item = document.createElement("p");
      item.className = "allotment-balance";
      item.textContent = `${balance.scope_type === "pi" ? "PI" : "Project"} time: ${balance.remaining_duration} available now; ${balance.remaining_after_request_duration} after this booking (${balance.pending_duration} pending).`;
      preview.append(item);
    });
    submitButton.textContent = data.results.some((result) => result.severity === "approval_required") || reviewToggle.checked
      ? "Submit for administrator review"
      : "Confirm booking";
  };

  let timer;
  const refresh = async () => {
    filterReferences();
    const pi = piField.value;
    const project = projectField.value;
    if (!pi || !project) return;
    const response = await fetch("/api/rules/preview", {method: "POST", headers: {"Content-Type": "application/json", "X-CSRF-Token": wizard.dataset.csrfToken}, body: JSON.stringify({hold_id: Number(wizard.dataset.holdId), pi_id: Number(pi), project_id: Number(project), override_required: field("override_required").checked})});
    if (!response.ok) return;
    const data = await response.json();
    renderRulePreview(data);
  };
  wizard.addEventListener("input", () => { window.clearTimeout(timer); timer = window.setTimeout(refresh, 150); });
  wizard.addEventListener("change", refresh);
  reviewToggle.addEventListener("change", syncReviewRequest);
  optionalDetails.addEventListener("toggle", () => {
    if (reviewToggle.checked && !optionalDetails.open) optionalDetails.open = true;
  });
  wizard.addEventListener("invalid", (event) => {
    const details = event.target.closest("details");
    if (details) details.open = true;
  }, true);

  wizard.querySelectorAll("[data-quick-create] input").forEach((input) => {
    input.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      input.closest("[data-quick-create]").querySelector("[data-create-reference]").click();
    });
  });

  wizard.querySelectorAll("[data-create-reference]").forEach((button) => {
    button.addEventListener("click", async () => {
      const type = button.dataset.createReference;
      const container = button.closest("[data-quick-create]");
      const status = container.querySelector("[data-create-status]");
      const values = type === "pi"
        ? {name: field("quick_pi_name").value, email: field("quick_pi_email").value}
        : type === "project"
          ? {pi_id: piField.value, code: field("quick_project_code").value, name: field("quick_project_name").value}
          : {project_id: projectField.value, number: field("quick_grant_number").value, name: field("quick_grant_name").value};
      if ((type === "project" && !values.pi_id) || (type === "grant" && !values.project_id)) {
        status.textContent = `Choose a ${type === "project" ? "PI" : "project"} first.`;
        status.className = "form-status error-text";
        return;
      }
      button.disabled = true;
      status.textContent = "Creating…";
      status.className = "form-status";
      try {
        const response = await fetch(`/api/booking-references/${type}`, {method: "POST", headers: {"Content-Type": "application/json", "X-CSRF-Token": wizard.dataset.csrfToken}, body: JSON.stringify(values)});
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || "Could not create this item.");
        const option = document.createElement("option");
        option.value = data.id;
        if (type === "pi") {
          option.textContent = data.name;
          piField.add(option); piField.value = String(data.id);
        } else if (type === "project") {
          option.textContent = `${data.code} — ${data.name}`;
          option.dataset.piId = data.pi_id;
          projectField.add(option); projectField.value = String(data.id);
        } else {
          option.textContent = `${data.number} — ${data.name}`;
          option.dataset.projectIds = data.project_id;
          grantField.add(option); grantField.value = String(data.id);
        }
        container.querySelectorAll("input").forEach((input) => { input.value = ""; });
        container.open = false;
        filterReferences();
        await refresh();
      } catch (error) {
        status.textContent = error.message;
        status.className = "form-status error-text";
      } finally {
        button.disabled = false;
      }
    });
  });

  filterReferences();
  syncReviewRequest();
  refresh();
}
