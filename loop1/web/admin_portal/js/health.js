/* health.js — renders /api/admin/health/detail status pills into index.html. */
(function () {
  const { fetchWithMockFallback, renderAuthRequired, el } = AdminPortal;

  const POLL_MS = 30000;
  let pollTimer = null;
  let lastCheckedAt = null;   // Date parsed from the server's checked_at
  let lastFetchedAt = null;   // client-side Date.now() when this resolved
  let updatedAgoTimer = null;
  let selectedPillName = null; // which pill's detail is showing, survives a poll refresh

  const STATUS_META = {
    ok: { label: "ok", cls: "pill-ok" },
    down: { label: "down", cls: "pill-down" },
    not_configured: { label: "not configured", cls: "pill-neutral" },
    degraded: { label: "degraded", cls: "pill-degraded" },
    unknown: { label: "unknown", cls: "pill-neutral" },
  };

  function detailText(name, block) {
    if (!block) return `${name}: no data.`;
    const parts = [block.detail || `Status: ${block.status}.`];
    if (block.latency_ms != null) parts.push(`Latency: ${block.latency_ms.toFixed(1)}ms.`);
    return parts.join(" ");
  }

  function renderDetailPanel(container, name, block) {
    let panel = document.getElementById("health-detail-panel");
    if (!panel) {
      panel = el("div", { id: "health-detail-panel", class: "health-detail-panel" });
      container.appendChild(panel);
    }
    panel.innerHTML = "";
    panel.appendChild(el("span", { class: "health-detail-name" }, `${name}:`));
    panel.appendChild(document.createTextNode(detailText(name, block)));
  }

  function renderPill(container, name, block) {
    const meta = STATUS_META[block && block.status] || STATUS_META.unknown;
    const latency = block && block.latency_ms != null ? ` (${block.latency_ms.toFixed(0)}ms)` : "";
    const isSelected = selectedPillName === name;
    const attrs = {
      type: "button",
      class: `health-pill ${meta.cls}${isSelected ? " health-pill-selected" : ""}`,
      "aria-expanded": isSelected ? "true" : "false",
    };
    // Only set title when there's real detail text — el()'s setAttribute
    // would otherwise stringify an `undefined` value into a literal
    // "undefined" tooltip (e.g. the API pill, which has no .detail).
    if (block && block.detail) attrs.title = block.detail;
    const pill = el("button", attrs, [
      el("span", { class: "health-pill-name" }, name),
      el("span", { class: "health-pill-status" }, `${meta.label}${latency}`),
    ]);
    pill.addEventListener("click", () => {
      selectedPillName = selectedPillName === name ? null : name;
      renderPills(container, container._lastData);
    });
    return pill;
  }

  // Rebuilds the pill row + (if a pill is selected) the detail panel below
  // it, from the last-fetched data — split out from poll() so a click can
  // re-render without a network round-trip.
  function renderPills(container, data) {
    container._lastData = data;
    container.innerHTML = "";
    const row = el("div", { class: "health-pill-row" });
    const blocks = {
      API: { status: data.api },
      Postgres: data.postgres,
      Redis: data.redis,
      OpenRouter: data.openrouter,
    };
    for (const [name, block] of Object.entries(blocks)) {
      row.appendChild(renderPill(container, name, block));
    }
    container.appendChild(row);
    if (selectedPillName && blocks[selectedPillName]) {
      renderDetailPanel(container, selectedPillName, blocks[selectedPillName]);
    }
  }

  function renderUnavailable(container) {
    container.innerHTML = "";
    container.appendChild(el("p", { class: "muted" }, "Status unavailable — could not reach the health endpoint."));
  }

  function renderUpdatedAgo() {
    const labelSpan = document.getElementById("health-updated-ago");
    if (!labelSpan || !lastFetchedAt) return;
    const secs = Math.round((Date.now() - lastFetchedAt) / 1000);
    const agoText = secs < 5 ? "updated just now" : `updated ${secs}s ago`;
    labelSpan.textContent = lastCheckedAt ? `${agoText} (checked ${lastCheckedAt.toLocaleTimeString()})` : agoText;
  }

  async function poll() {
    const container = document.getElementById("health-pills");
    if (!container) return;

    const result = await fetchWithMockFallback("/api/admin/health/detail", AdminPortalMock.healthDetail);

    if (result.authRequired) {
      renderAuthRequired(container, { what: "system health" });
      return;
    }

    const data = result.data;
    if (!data || !data.checked_at) {
      renderUnavailable(container);
      return;
    }

    lastCheckedAt = new Date(data.checked_at);
    lastFetchedAt = Date.now();

    container.innerHTML = "";
    const banner = document.getElementById("health-sample-banner");
    if (banner) banner.hidden = !result.usingMock;

    renderPills(container, data);
    container.appendChild(
      el("p", { class: "muted live-status-row" }, [
        el("span", { class: "live-pulse-dot" }),
        el("span", { id: "health-updated-ago" }, "updated just now"),
      ])
    );
    renderUpdatedAgo();
  }

  function schedule() {
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
    if (document.visibilityState === "hidden") {
      // Back off entirely while the tab isn't visible — visibilitychange
      // below restarts polling the moment it's shown again.
      return;
    }
    poll();
    pollTimer = setTimeout(schedule, POLL_MS);
  }

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && pollTimer === null) {
      schedule();
    }
  });

  schedule();
  updatedAgoTimer = setInterval(renderUpdatedAgo, 1000);
})();
