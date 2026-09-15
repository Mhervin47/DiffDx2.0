/*
 * audit.js — filterable table over /api/admin/audit, with ?entry=<uuid>
 * deep-link support.
 *
 * Item 9: polls every 30s (matching health.js/usage.js's own cadence and
 * visibility-backoff pattern) — audit_log_entries reads are cheap DB
 * queries, not LLM calls, so this is safe to poll. Respects whatever
 * filter is currently active rather than resetting it, and the deep-link
 * scroll/highlight only fires on the very first load, not on every poll.
 */
(function () {
  const { fetchWithMockFallback, renderAuthRequired, el } = AdminPortal;

  const state = { actor: "", from: "", to: "", offset: 0, limit: 50 };
  const POLL_MS = 30000;
  let pollTimer = null;
  let deepLinkApplied = false;
  let lastUpdatedAt = null;

  function buildQuery() {
    const params = new URLSearchParams();
    if (state.actor) params.set("actor", state.actor);
    if (state.from) params.set("from", state.from);
    if (state.to) params.set("to", state.to);
    params.set("limit", String(state.limit));
    params.set("offset", String(state.offset));
    return params.toString();
  }

  // Date on one line, time on the next — a raw ISO string ("2026-01-01T12:03:00")
  // crammed into one cell was hard to scan across a whole table of rows.
  function renderTimestampCell(ts) {
    if (!ts) return el("td", {}, "—");
    const d = new Date(ts);
    if (isNaN(d.getTime())) return el("td", {}, ts);
    const dateStr = d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
    const timeStr = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    return el("td", {}, [
      el("div", { class: "ts-date" }, dateStr),
      el("div", { class: "ts-time" }, timeStr),
    ]);
  }

  function renderRow(item) {
    const tr = el("tr", { id: `audit-row-${item.id}` });
    tr.appendChild(renderTimestampCell(item.ts));
    tr.appendChild(el("td", {}, `${item.actor_name} (${item.actor_role})`));
    tr.appendChild(el("td", {}, item.action));
    tr.appendChild(el("td", {}, item.resource_type || "—"));
    tr.appendChild(el("td", {}, item.resource_id || "—"));
    tr.appendChild(el("td", {}, item.ip || "—"));
    return tr;
  }

  function applyDeepLink() {
    if (deepLinkApplied) return;
    const params = new URLSearchParams(window.location.search);
    const entry = params.get("entry");
    if (!entry) return;
    const row = document.getElementById(`audit-row-${entry}`);
    if (row) {
      row.scrollIntoView({ behavior: "smooth", block: "center" });
      row.classList.add("highlighted");
      deepLinkApplied = true;
    }
  }

  function renderUpdatedAgo() {
    const label = document.getElementById("audit-updated-ago");
    if (!label || !lastUpdatedAt) return;
    const secs = Math.round((Date.now() - lastUpdatedAt) / 1000);
    label.textContent = secs < 5 ? "updated just now" : `updated ${secs}s ago`;
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

    lastUpdatedAt = Date.now();
    renderUpdatedAgo();
  }

  document.getElementById("audit-filter-form").addEventListener("submit", (e) => {
    e.preventDefault();
    state.actor = document.getElementById("f-actor").value.trim();
    state.from = document.getElementById("f-from").value;
    state.to = document.getElementById("f-to").value;
    state.offset = 0;
    load();
  });

  document.getElementById("audit-filter-reset").addEventListener("click", () => {
    document.getElementById("audit-filter-form").reset();
    Object.assign(state, { actor: "", from: "", to: "", offset: 0 });
    load();
  });

  function schedule() {
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
    if (document.visibilityState === "hidden") {
      return;
    }
    load();
    pollTimer = setTimeout(schedule, POLL_MS);
  }

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && pollTimer === null) {
      schedule();
    }
  });

  // Growth section (total doctors/users + 14-day trend) — mock only, no
  // fetch: there's no real endpoint for this yet (see mock.js's
  // platformGrowth entry). Two series sharing one x-axis but each scaled
  // to its own max, same approach as index.html's usage.js Cost & Usage
  // chart — a shared y-axis would flatten "doctors" (tens) next to "users"
  // (hundreds) into a near-flat line.
  function renderGrowthChart(daily) {
    const width = 560, height = 110, pad = 16;
    const maxDoctors = Math.max(1, ...daily.map((d) => d.doctors));
    const maxUsers = Math.max(1, ...daily.map((d) => d.users));
    const stepX = daily.length > 1 ? (width - pad * 2) / (daily.length - 1) : 0;

    const svgNS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("class", "growth-chart");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "Total doctors and users, last " + daily.length + " days");

    function points(valueFn, max) {
      return daily
        .map((d, i) => {
          const x = pad + i * stepX;
          const y = height - pad - (valueFn(d) / max) * (height - pad * 2);
          return `${x},${y}`;
        })
        .join(" ");
    }

    function line(valueFn, max, cls, colorVar) {
      const polyline = document.createElementNS(svgNS, "polyline");
      polyline.setAttribute("points", points(valueFn, max));
      polyline.setAttribute("fill", "none");
      polyline.setAttribute("class", cls);
      polyline.style.stroke = `var(${colorVar})`;
      svg.appendChild(polyline);

      daily.forEach((d, i) => {
        const x = pad + i * stepX;
        const y = height - pad - (valueFn(d) / max) * (height - pad * 2);
        const circle = document.createElementNS(svgNS, "circle");
        circle.setAttribute("cx", x);
        circle.setAttribute("cy", y);
        circle.setAttribute("r", "2.5");
        circle.setAttribute("class", cls);
        circle.style.fill = `var(${colorVar})`;
        const title = document.createElementNS(svgNS, "title");
        title.textContent = `${d.date}: ${d.doctors} doctors, ${d.users} users`;
        circle.appendChild(title);
        svg.appendChild(circle);
      });
    }

    line((d) => d.doctors, maxDoctors, "growth-chart-doctors", "--teal-primary");
    line((d) => d.users, maxUsers, "growth-chart-users", "--purple-accent");
    return svg;
  }

  function renderGrowth() {
    const data = AdminPortalMock.platformGrowth;
    const docEl = document.getElementById("growth-total-doctors");
    const userEl = document.getElementById("growth-total-users");
    if (docEl) docEl.textContent = data.total_doctors.toLocaleString();
    if (userEl) userEl.textContent = data.total_users.toLocaleString();

    const wrap = document.getElementById("growth-chart-wrap");
    if (!wrap) return;
    wrap.innerHTML = "";
    wrap.appendChild(renderGrowthChart(data.daily));
    wrap.appendChild(
      el("div", { class: "growth-chart-legend" }, [
        el("span", { class: "growth-legend-item" }, [
          el("span", { class: "growth-legend-swatch growth-chart-doctors" }),
          "Doctors",
        ]),
        el("span", { class: "growth-legend-item" }, [
          el("span", { class: "growth-legend-swatch growth-chart-users" }),
          "Users",
        ]),
      ])
    );
  }

  renderGrowth();
  schedule();
  setInterval(renderUpdatedAgo, 1000);
})();
