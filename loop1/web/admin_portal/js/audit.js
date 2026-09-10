/* audit.js — filterable table over /api/admin/audit, with ?entry=<uuid> deep-link support. */
(function () {
  const { fetchWithMockFallback, renderAuthRequired, el } = AdminPortal;

  const state = { actor: "", action: "", resource_type: "", from: "", to: "", offset: 0, limit: 50 };

  function buildQuery() {
    const params = new URLSearchParams();
    if (state.actor) params.set("actor", state.actor);
    if (state.action) params.set("action", state.action);
    if (state.resource_type) params.set("resource_type", state.resource_type);
    if (state.from) params.set("from", state.from);
    if (state.to) params.set("to", state.to);
    params.set("limit", String(state.limit));
    params.set("offset", String(state.offset));
    return params.toString();
  }

  function renderRow(item) {
    const tr = el("tr", { id: `audit-row-${item.id}` });
    tr.appendChild(el("td", {}, item.ts || "—"));
    tr.appendChild(el("td", {}, `${item.actor_name} (${item.actor_role})`));
    tr.appendChild(el("td", {}, item.action));
    tr.appendChild(el("td", {}, item.resource_type || "—"));
    tr.appendChild(el("td", {}, item.resource_id || "—"));
    tr.appendChild(el("td", {}, item.ip || "—"));
    return tr;
  }

  function applyDeepLink() {
    const params = new URLSearchParams(window.location.search);
    const entry = params.get("entry");
    if (!entry) return;
    const row = document.getElementById(`audit-row-${entry}`);
    if (row) {
      row.scrollIntoView({ behavior: "smooth", block: "center" });
      row.classList.add("highlighted");
    }
  }

  async function load() {
    const wrap = document.getElementById("audit-table-wrap");
    const result = await fetchWithMockFallback(`/api/admin/audit?${buildQuery()}`, AdminPortalMock.audit);

    if (result.authRequired) {
      renderAuthRequired(wrap, { what: "the audit log" });
      return;
    }

    const banner = document.getElementById("audit-sample-banner");
    if (banner) banner.hidden = !result.usingMock;

    const data = result.data;
    const tbody = document.querySelector("#audit-table tbody");
    tbody.innerHTML = "";
    if (!data.items || !data.items.length) {
      tbody.appendChild(el("tr", {}, el("td", { class: "muted", colspan: "6" }, "No audit entries match these filters.")));
    } else {
      for (const item of data.items) tbody.appendChild(renderRow(item));
    }
    document.getElementById("audit-total").textContent = `${data.total} total`;
    applyDeepLink();
  }

  document.getElementById("audit-filter-form").addEventListener("submit", (e) => {
    e.preventDefault();
    state.actor = document.getElementById("f-actor").value.trim();
    state.action = document.getElementById("f-action").value.trim();
    state.resource_type = document.getElementById("f-resource-type").value.trim();
    state.from = document.getElementById("f-from").value;
    state.to = document.getElementById("f-to").value;
    state.offset = 0;
    load();
  });

  document.getElementById("audit-filter-reset").addEventListener("click", () => {
    document.getElementById("audit-filter-form").reset();
    Object.assign(state, { actor: "", action: "", resource_type: "", from: "", to: "", offset: 0 });
    load();
  });

  load();
})();
