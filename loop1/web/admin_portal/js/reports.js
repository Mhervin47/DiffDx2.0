/*
 * reports.js — filterable table over /api/admin/message-reports, with
 * "Mark Reviewed" / "Dismiss" row actions (PATCH). Same admin-console
 * pattern as dsr.js's pending-requests panel: a review queue an admin
 * works from, not an automated moderation system.
 */
(function () {
  const { fetchWithMockFallback, renderAuthRequired, el } = AdminPortal;

  const state = { status: "open" };

  const REASON_LABELS = {
    non_medical: "Not medical care",
    harassment: "Harassment",
    inappropriate_content: "Inappropriate content",
    spam: "Spam",
    other: "Other",
  };

  function fmtReason(reason) {
    return REASON_LABELS[reason] || reason;
  }

  function renderRow(item) {
    const tr = el("tr", { id: `report-row-${item.id}` });

    const reportedLabel = item.reported_doctor_id
      ? `${item.reported_name} (doctor)`
      : `${item.reported_name} (patient)`;
    tr.appendChild(el("td", {}, reportedLabel));
    tr.appendChild(el("td", {}, `${item.reporter_name} (${item.reporter_role})`));
    tr.appendChild(el("td", {}, fmtReason(item.reason)));
    tr.appendChild(el("td", { class: "report-details-text" }, item.details || "—"));
    tr.appendChild(el("td", {}, el("span", { class: `status-pill status-${item.status}` }, item.status)));
    tr.appendChild(el("td", {}, item.created_at || "—"));

    const actions = el("div", { class: "report-row-actions" });
    if (item.status === "open") {
      const reviewBtn = el("button", { class: "report-action-btn review" }, "Mark Reviewed");
      reviewBtn.addEventListener("click", () => updateReport(item.id, "reviewed"));
      const dismissBtn = el("button", { class: "report-action-btn dismiss" }, "Dismiss");
      dismissBtn.addEventListener("click", () => updateReport(item.id, "dismissed"));
      actions.appendChild(reviewBtn);
      actions.appendChild(dismissBtn);
    } else {
      actions.appendChild(el("span", { class: "muted" }, item.admin_note ? `Note: ${item.admin_note}` : "—"));
    }
    tr.appendChild(el("td", {}, actions));
    return tr;
  }

  async function load() {
    const wrap = document.getElementById("reports-table-wrap");
    const qs = `?status=${encodeURIComponent(state.status)}`;
    const result = await fetchWithMockFallback(`/api/admin/message-reports${qs}`, AdminPortalMock.messageReports);

    if (result.authRequired) {
      renderAuthRequired(wrap, { what: "message reports" });
      return;
    }

    const banner = document.getElementById("reports-sample-banner");
    if (banner) banner.hidden = !result.usingMock;

    const data = result.data;
    const tbody = document.querySelector("#reports-table tbody");
    tbody.innerHTML = "";
    if (!data.items || !data.items.length) {
      tbody.appendChild(el("tr", {}, el("td", { class: "muted", colspan: "7" }, "No reports match this filter.")));
    } else {
      for (const item of data.items) tbody.appendChild(renderRow(item));
    }
    document.getElementById("reports-total").textContent = `${data.total} total`;
  }

  async function updateReport(id, status) {
    const admin_note = prompt(status === "reviewed" ? "Note for this review (optional):" : "Reason for dismissing (optional):");
    if (admin_note === null) return; // cancelled
    try {
      const token = typeof getAuthToken === "function" ? getAuthToken() : null;
      const res = await fetch(`/api/admin/message-reports/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
        body: JSON.stringify({ status, admin_note: admin_note || null }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        alert(`Update failed: ${data.detail || res.status}`);
        return;
      }
      await load();
    } catch (err) {
      alert(`Update failed: ${err.message}`);
    }
  }

  document.getElementById("reports-filter-form").addEventListener("submit", (e) => {
    e.preventDefault();
    state.status = document.getElementById("f-status").value;
    load();
  });

  load();
})();
