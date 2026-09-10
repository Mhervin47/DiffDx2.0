/* health.js — renders /api/admin/health/detail status pills into index.html. */
(function () {
  const { fetchWithMockFallback, renderAuthRequired, el } = AdminPortal;

  const POLL_MS = 30000;
  let pollTimer = null;

  const STATUS_META = {
    ok: { label: "ok", cls: "pill-ok" },
    down: { label: "down", cls: "pill-down" },
    not_configured: { label: "not configured", cls: "pill-neutral" },
    degraded: { label: "degraded", cls: "pill-degraded" },
    unknown: { label: "unknown", cls: "pill-neutral" },
  };

  function renderPill(name, block) {
    const meta = STATUS_META[block && block.status] || STATUS_META.unknown;
    const latency = block && block.latency_ms != null ? ` (${block.latency_ms.toFixed(0)}ms)` : "";
    const pill = el("div", { class: `health-pill ${meta.cls}` }, [
      el("span", { class: "health-pill-name" }, name),
      el("span", { class: "health-pill-status" }, `${meta.label}${latency}`),
    ]);
    if (block && block.detail) pill.title = block.detail;
    return pill;
  }

  function renderUnavailable(container) {
    container.innerHTML = "";
    container.appendChild(el("p", { class: "muted" }, "Status unavailable — could not reach the health endpoint."));
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

    container.innerHTML = "";
    const banner = document.getElementById("health-sample-banner");
    if (banner) banner.hidden = !result.usingMock;

    container.appendChild(
      el("div", { class: "health-pill-row" }, [
        renderPill("API", { status: data.api }),
        renderPill("Postgres", data.postgres),
        renderPill("Redis", data.redis),
        renderPill("Groq", data.groq),
      ])
    );
    container.appendChild(el("p", { class: "muted health-checked-at" }, `Checked: ${data.checked_at}`));
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
})();
