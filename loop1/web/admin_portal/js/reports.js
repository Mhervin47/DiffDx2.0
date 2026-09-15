/*
 * reports.js — filterable table over /api/admin/message-reports, with
 * "Mark Reviewed" / "Dismiss" row actions (PATCH). Same admin-console
 * pattern as dsr.js's pending-requests panel: a review queue an admin
 * works from, not an automated moderation system.
 */
(function () {
  const { fetchWithMockFallback, renderAuthRequired, el } = AdminPortal;

  const state = { status: "open" };
  let pendingAction = null; // { id, status } for the row-action modal

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
      reviewBtn.addEventListener("click", () => openActionModal(item.id, "reviewed"));
      const dismissBtn = el("button", { class: "report-action-btn dismiss" }, "Dismiss");
      dismissBtn.addEventListener("click", () => openActionModal(item.id, "dismissed"));
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

  // Review/dismiss note modal — replaces the native prompt() dialog. One
  // shared modal for both actions; its title/prompt/button text and color
  // swap based on which action opened it (pendingAction.status).
  function openActionModal(id, status) {
    pendingAction = { id, status };
    const modal = document.getElementById("report-action-modal");
    const confirmBtn = document.getElementById("report-action-confirm-btn");
    if (status === "reviewed") {
      document.getElementById("report-action-modal-title").textContent = "Mark reviewed";
      document.getElementById("report-action-modal-prompt").textContent = "Note for this review (optional):";
      confirmBtn.textContent = "Mark Reviewed";
      confirmBtn.className = "report-modal-confirm-btn review";
    } else {
      document.getElementById("report-action-modal-title").textContent = "Dismiss this report";
      document.getElementById("report-action-modal-prompt").textContent = "Reason for dismissing (optional):";
      confirmBtn.textContent = "Dismiss";
      confirmBtn.className = "report-modal-confirm-btn dismiss";
    }
    document.getElementById("report-action-note").value = "";
    modal.hidden = false;
    document.getElementById("report-action-note").focus();
  }

  function closeActionModal() {
    pendingAction = null;
    document.getElementById("report-action-modal").hidden = true;
  }

  async function confirmAction() {
    if (!pendingAction) return;
    const { id, status } = pendingAction;
    const admin_note = document.getElementById("report-action-note").value;
    const confirmBtn = document.getElementById("report-action-confirm-btn");
    confirmBtn.disabled = true;
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
      closeActionModal();
      await load();
    } catch (err) {
      alert(`Update failed: ${err.message}`);
    } finally {
      confirmBtn.disabled = false;
    }
  }

  document.getElementById("report-action-cancel-btn").addEventListener("click", closeActionModal);
  document.getElementById("report-action-confirm-btn").addEventListener("click", confirmAction);
  document.getElementById("report-action-modal").addEventListener("click", (e) => {
    if (e.target.id === "report-action-modal") closeActionModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !document.getElementById("report-action-modal").hidden) closeActionModal();
  });

  document.getElementById("reports-filter-form").addEventListener("submit", (e) => {
    e.preventDefault();
    state.status = document.getElementById("f-status").value;
    load();
  });

  load();
})();
