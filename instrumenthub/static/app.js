"use strict";

/* Confirmation prompts */
document.querySelectorAll("[data-confirm]").forEach((element) =>
  element.addEventListener("submit", (event) => {
    if (!window.confirm(element.dataset.confirm)) event.preventDefault();
  })
);

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

/* Calendar: drag-to-select consecutive open blocks, plus live availability refresh */
const calendar = document.querySelector(".calendar[data-instrument][data-month]");
if (calendar) {
  const bar = document.getElementById("select-bar");
  const summary = bar && bar.querySelector("[data-select-summary]");
  const startField = bar && bar.querySelector("[data-select-start]");
  const countField = bar && bar.querySelector("[data-select-count]");
  const slots = () => Array.from(calendar.querySelectorAll(".slot[data-start]"));
  const openSlots = () => slots().filter((node) => node.dataset.status === "available");

  let anchor = null;
  let dragging = false;

  const clearSelection = () => {
    slots().forEach((node) => node.classList.remove("selected"));
    if (bar) bar.classList.remove("visible");
    anchor = null;
  };

  // Contiguous run of open blocks between anchor and focus; empty if any gap is not open.
  const runBetween = (from, to) => {
    const open = openSlots();
    let a = open.indexOf(from);
    let b = open.indexOf(to);
    if (a < 0 || b < 0) return [];
    if (a > b) [a, b] = [b, a];
    // Require the selected blocks to be adjacent in the full block sequence (no booked gap).
    const all = slots();
    const startIdx = all.indexOf(open[a]);
    const endIdx = all.indexOf(open[b]);
    const span = all.slice(startIdx, endIdx + 1);
    if (span.some((node) => node.dataset.status !== "available")) return [];
    if (span.some((node, index) => index > 0 && span[index - 1].dataset.end > node.dataset.start)) return [];
    return span;
  };

  const paint = (focus) => {
    const run = runBetween(anchor, focus);
    slots().forEach((node) => node.classList.remove("selected"));
    if (!run.length || !bar) return;
    run.forEach((node) => node.classList.add("selected"));
    startField.value = run[0].dataset.start;
    countField.value = run.length;
    summary.textContent = `${run.length} open block${run.length > 1 ? "s" : ""} selected`;
    bar.classList.add("visible");
  };

  calendar.addEventListener("pointerdown", (event) => {
    const slot = event.target.closest(".slot");
    if (!slot || slot.dataset.status !== "available") return;
    if (event.target.closest("a")) return; // allow the keyboard/click fallback link
    event.preventDefault();
    dragging = true;
    calendar.classList.add("dragging");
    anchor = slot;
    paint(slot);
  });

  calendar.addEventListener("pointerover", (event) => {
    if (!dragging) return;
    const slot = event.target.closest(".slot");
    if (slot) paint(slot);
  });

  const endDrag = () => {
    dragging = false;
    calendar.classList.remove("dragging");
  };
  window.addEventListener("pointerup", endDrag);
  window.addEventListener("pointercancel", endDrag);

  // Single click (no drag) selects one block.
  calendar.addEventListener("click", (event) => {
    const link = event.target.closest("a");
    if (link) return;
    const slot = event.target.closest(".slot");
    if (slot && slot.dataset.status === "available" && !slot.classList.contains("selected")) {
      anchor = slot;
      paint(slot);
    }
  });

if (bar) {
    bar.querySelector("[data-select-clear]").addEventListener("click", clearSelection);
  }

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

/* Booking wizard: live duration estimate and rule preview. */
const wizard = document.querySelector("[data-booking-wizard]");
if (wizard) {
  const field = (name) => wizard.elements.namedItem(name);
  const preview = document.querySelector("[data-rule-preview]");
  const estimate = document.querySelector("[data-duration-estimate]");
  const warning = document.querySelector("[data-reservation-warning]");
  const showReservationWarning = (minutes, label) => {
    const reserved = Number(wizard.dataset.reservedMinutes);
    warning.hidden = !minutes || minutes <= reserved;
    if (!warning.hidden) warning.textContent = `${label} exceeds the ${reserved} reserved nominal minutes.`;
  };
  let timer;
  const refresh = async () => {
    const pi = field("pi_id").value;
    const project = field("project_id").value;
    const injections = field("injection_count").value;
    const gradient = field("gradient_minutes").value;
    const expected = Number(field("expected_minutes").value);
    if (expected) showReservationWarning(expected, "Expected duration");
    if (injections && gradient) {
      const response = await fetch(`/api/duration-estimate?injection_count=${encodeURIComponent(injections)}&gradient_minutes=${encodeURIComponent(gradient)}`);
      if (response.ok) {
        const data = await response.json();
        estimate.textContent = `Estimated duration: ${data.minutes} minutes (includes QC injections).`;
        if (!expected) showReservationWarning(data.minutes, "Estimated duration");
      }
    }
    if (!pi || !project) return;
    const response = await fetch("/api/rules/preview", {method: "POST", headers: {"Content-Type": "application/json", "X-CSRF-Token": wizard.dataset.csrfToken}, body: JSON.stringify({hold_id: Number(wizard.dataset.holdId), pi_id: Number(pi), project_id: Number(project), override_required: field("override_required").checked})});
    if (!response.ok) return;
    const data = await response.json();
    preview.textContent = data.results.map((result) => `${result.severity}: ${result.message}`).join(" ");
    if (data.project_consecutive_limit) preview.textContent += ` Project consecutive-time limit: ${data.project_consecutive_limit} minutes.`;
    if (data.allotment_balances.length) preview.textContent += " " + data.allotment_balances.map((balance) => `${balance.scope_type} allotment: ${balance.remaining_minutes} minutes remaining, ${balance.pending_minutes} pending (${balance.remaining_after_request_minutes} after this request).`).join(" ");
  };
  wizard.addEventListener("input", () => { window.clearTimeout(timer); timer = window.setTimeout(refresh, 150); });
  wizard.addEventListener("change", refresh);
  refresh();
}

document.querySelectorAll("[data-management-form]").forEach((form) => {
  form.addEventListener("submit", () => {
    const user = form.elements.namedItem("user_id");
    const referenceType = form.elements.namedItem("reference_type");
    const reference = form.elements.namedItem("reference_id");
    const instrument = form.elements.namedItem("instrument_id");
    if (user) form.action = `/admin/users/${encodeURIComponent(user.value)}/role`;
    if (referenceType && reference) form.action = `/admin/references/${encodeURIComponent(referenceType.value)}/${encodeURIComponent(reference.value)}/status`;
    if (instrument) form.action = `/admin/instruments/${encodeURIComponent(instrument.value)}/policy`;
  });
});
