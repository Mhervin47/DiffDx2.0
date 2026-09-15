/*
 * dsr.js — Data Subject Request console: search, inventory, export, and a
 * gated erase (feature-flagged server-side; dry-run by default here too).
 */
(function () {
  const { fetchWithMockFallback, renderAuthRequired, el } = AdminPortal;

  let selectedSubject = null;
  let denyRequestId = null;

  // ── Pending Requests panel ──────────────────────────────────────────
  // A thin front door: no "approve" here — approving a request IS an
  // admin running the search -> inventory -> erase flow below on that
  // subject, unchanged. This panel only lists requests and denies them.
  function renderPendingRequests(items) {
    const list = document.getElementById("dsr-pending-list");
    list.innerHTML = "";
    if (!items.length) {
      list.appendChild(el("p", { class: "muted" }, "No pending requests."));
      return;
    }
    for (const item of items) {
      const info = el("div", { class: "dsr-pending-info" }, [
        el("div", {}, [el("strong", {}, item.name), el("span", { class: "muted" }, ` — ${item.email}`)]),
        el("div", { class: "muted dsr-pending-reason" }, item.reason ? `"${item.reason}"` : "(no reason given)"),
        el("div", { class: "muted" }, `requested ${item.requested_at || "—"}`),
      ]);
      info.addEventListener("click", () => {
        document.getElementById("dsr-search-input").value = item.email;
        search();
        window.scrollTo({ top: document.querySelector(".card:nth-of-type(2)")?.offsetTop ?? 0, behavior: "smooth" });
      });
      const denyBtn = el("button", { class: "dsr-deny-btn" }, "Deny");
      denyBtn.addEventListener("click", () => openDenyModal(item.id));
      list.appendChild(el("div", { class: "dsr-pending-row" }, [info, denyBtn]));
    }
  }

  async function loadPendingRequests() {
    const result = await fetchWithMockFallback("/api/admin/dsr-requests?status=pending", AdminPortalMock.dsrRequestsList);
    if (result.authRequired) {
      renderAuthRequired(document.getElementById("dsr-pending-list"), { what: "pending requests" });
      return;
    }
    renderPendingRequests(result.data.items || []);
  }

  // Deny-reason modal — replaces the native prompt() dialog. Opens on a
  // "Deny" click, stores which request it's for in denyRequestId, and the
  // actual API call happens from the modal's own confirm button below.
  function openDenyModal(requestId) {
    denyRequestId = requestId;
    const modal = document.getElementById("dsr-deny-modal");
    const textarea = document.getElementById("dsr-deny-reason");
    textarea.value = "";
    modal.hidden = false;
    textarea.focus();
  }

  function closeDenyModal() {
    denyRequestId = null;
    document.getElementById("dsr-deny-modal").hidden = true;
  }

  async function confirmDeny() {
    if (!denyRequestId) return;
    const requestId = denyRequestId;
    const note = document.getElementById("dsr-deny-reason").value;
    const confirmBtn = document.getElementById("dsr-deny-confirm-btn");
    confirmBtn.disabled = true;
    try {
      const token = typeof getAuthToken === "function" ? getAuthToken() : null;
      const res = await fetch(`/api/admin/dsr-requests/${requestId}/deny`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
        body: JSON.stringify({ note }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        alert(`Deny failed: ${data.detail || res.status}`);
        return;
      }
      closeDenyModal();
      await loadPendingRequests();
    } catch (err) {
      alert(`Deny failed: ${err.message}`);
    } finally {
      confirmBtn.disabled = false;
    }
  }

  document.getElementById("dsr-deny-cancel-btn").addEventListener("click", closeDenyModal);
  document.getElementById("dsr-deny-confirm-btn").addEventListener("click", confirmDeny);
  document.getElementById("dsr-deny-modal").addEventListener("click", (e) => {
    if (e.target.id === "dsr-deny-modal") closeDenyModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !document.getElementById("dsr-deny-modal").hidden) closeDenyModal();
  });

  function renderSearchResults(items) {
    const list = document.getElementById("dsr-results");
    list.innerHTML = "";
    if (!items.length) {
      list.appendChild(el("p", { class: "muted" }, "No matching subjects."));
      return;
    }
    for (const item of items) {
      const row = el("div", { class: "dsr-result-row" }, [
        el("div", {}, [el("strong", {}, item.name), el("span", { class: "muted" }, ` — ${item.email}`)]),
        el("div", { class: "muted" }, `${item.session_count} session(s) · registered ${item.created_at || "—"}`),
      ]);
      row.addEventListener("click", () => selectSubject(item.user_id));
      list.appendChild(row);
    }
  }

  async function search() {
    const q = document.getElementById("dsr-search-input").value.trim();
    const list = document.getElementById("dsr-results");
    const result = await fetchWithMockFallback(`/api/admin/subjects?q=${encodeURIComponent(q)}`, AdminPortalMock.subjectsSearch);
    if (result.authRequired) {
      renderAuthRequired(list, { what: "subject search" });
      return;
    }
    const banner = document.getElementById("dsr-sample-banner");
    if (banner) banner.hidden = !result.usingMock;
    renderSearchResults(result.data.items || []);
  }

  async function selectSubject(userId) {
    selectedSubject = userId;
    document.getElementById("dsr-detail").hidden = false;
    document.getElementById("dsr-receipt").hidden = true;
    const invContainer = document.getElementById("dsr-inventory");
    invContainer.innerHTML = "<p class=\"muted\">Loading…</p>";

    const result = await fetchWithMockFallback(`/api/admin/subjects/${userId}/inventory`, AdminPortalMock.subjectInventory);
    if (result.authRequired) {
      renderAuthRequired(invContainer, { what: "subject inventory" });
      return;
    }
    const data = result.data;
    document.getElementById("dsr-subject-name").textContent = `${data.name} <${data.email}>`;

    const table = el("table", { class: "dsr-inventory-table" });
    table.appendChild(
      el("thead", {}, el("tr", {}, [el("th", {}, "Category"), el("th", {}, "Location"), el("th", {}, "Count"), el("th", {}, "")]))
    );
    const tbody = el("tbody");
    for (const cat of data.categories) {
      tbody.appendChild(
        el("tr", {}, [
          el("td", {}, cat.label),
          el("td", { class: "muted" }, cat.tables.join(", ") + (cat.detail ? ` — ${cat.detail}` : "")),
          el("td", {}, String(cat.record_count)),
          el("td", {}, cat.retained ? el("span", { class: "dsr-retained-badge" }, "RETAINED") : ""),
        ])
      );
    }
    table.appendChild(tbody);
    invContainer.innerHTML = "";
    invContainer.appendChild(table);
    invContainer.appendChild(
      el(
        "p",
        { class: "muted dsr-retained-note" },
        "The audit trail is never erased — it is pseudonymised instead. An erasable audit log is not an audit log."
      )
    );

    document.getElementById("dsr-confirm-email").value = "";
    document.getElementById("dsr-erase-btn").disabled = true;
  }

  function renderReceipt(data) {
    const box = document.getElementById("dsr-receipt");
    box.hidden = false;
    box.innerHTML = "";
    box.appendChild(el("h3", {}, data.dry_run ? "Dry run — nothing was deleted" : "Erasure complete"));
    const counts = data.dry_run ? data.would_erase : data.erased;
    const list = el("ul", { class: "dsr-receipt-list" });
    for (const [label, count] of Object.entries(counts || {})) {
      list.appendChild(el("li", {}, `${label}: ${count}`));
    }
    box.appendChild(list);
    box.appendChild(el("p", { class: "muted" }, `Retained: audit log (${JSON.stringify(data.retained)})`));
    if (data.warnings && data.warnings.length) {
      box.appendChild(el("p", { class: "dsr-warning" }, `Warnings: ${data.warnings.join("; ")}`));
    }
    if (!data.dry_run && data.audit_entry_id) {
      box.appendChild(
        el("p", {}, [
          "Audit entry: ",
          el("a", { href: `audit.html?entry=${data.audit_entry_id}`, class: "auth-required-link" }, data.audit_entry_id),
        ])
      );
      box.appendChild(el("p", { class: "muted" }, `Erased at: ${data.erased_at}`));
    }
  }

  document.getElementById("dsr-search-btn").addEventListener("click", search);
  document.getElementById("dsr-search-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") search();
  });

  document.getElementById("dsr-export-btn").addEventListener("click", () => {
    if (!selectedSubject) return;
    // Deliberately not a plain <a download> — the export needs the
    // Authorization header, which a bare link can't attach. Fetch it and
    // open the JSON in a new tab instead.
    (async () => {
      try {
        const token = typeof getAuthToken === "function" ? getAuthToken() : null;
        const res = await fetch(`/api/admin/subjects/${selectedSubject}/export`, {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        });
        const data = await res.json();
        const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
        window.open(URL.createObjectURL(blob), "_blank");
      } catch (err) {
        alert(`Export failed: ${err.message}`);
      }
    })();
  });

  document.getElementById("dsr-confirm-email").addEventListener("input", (e) => {
    document.getElementById("dsr-erase-btn").disabled = e.target.value.trim().length === 0;
  });

  document.getElementById("dsr-erase-btn").addEventListener("click", async () => {
    if (!selectedSubject) return;
    const confirmEmail = document.getElementById("dsr-confirm-email").value.trim();
    const dryRun = document.getElementById("dsr-dry-run-toggle").checked;
    const receiptBox = document.getElementById("dsr-receipt");
    receiptBox.hidden = false;
    receiptBox.innerHTML = "<p class=\"muted\">Working…</p>";

    try {
      const token = typeof getAuthToken === "function" ? getAuthToken() : null;
      const res = await fetch(`/api/admin/subjects/${selectedSubject}?dry_run=${dryRun}`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
        body: JSON.stringify({ confirm_email: confirmEmail }),
      });
      const data = await res.json();
      if (!res.ok) {
        receiptBox.innerHTML = "";
        receiptBox.appendChild(el("p", { class: "dsr-error" }, data.detail || `Request failed (HTTP ${res.status}).`));
        return;
      }
      renderReceipt(data);
    } catch (err) {
      receiptBox.innerHTML = "";
      receiptBox.appendChild(el("p", { class: "dsr-error" }, `Request failed: ${err.message}`));
    }
  });

  loadPendingRequests();
})();
