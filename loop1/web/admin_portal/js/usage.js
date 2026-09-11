/*
 * usage.js — renders /api/admin/usage into index.html's Cost & Usage card.
 *
 * No chart.js: Phase 1's evidence.js/quality.js never introduced a charting
 * library (their data didn't need one) — adding chart.js here would be a
 * NEW dependency, not matching an existing one, so the 14-day trend is a
 * small hand-rolled inline SVG instead, consistent with Phase 1's
 * zero-external-dependency approach.
 *
 * Polls every 30s (matching health.js's own cadence and visibility-backoff
 * pattern exactly) — sessions/usage rows are cheap DB reads, not LLM calls,
 * so this is safe to poll.
 */
(function () {
  const { fetchWithMockFallback, renderAuthRequired, fmtUsd, el } = AdminPortal;

  const POLL_MS = 30000;
  let pollTimer = null;
  let lastUpdatedAt = null;
  let updatedAgoTimer = null;

  // Named constant, not a magic number inline — a placeholder AWS/hosting
  // estimate. Update with a real figure before citing this projection
  // externally; it is labelled as a projection everywhere it renders.
  const PROJECTED_INFRA_USD_PER_MONTH = 40;
  const PROJECTED_MONTHLY_SESSIONS = 10000;

  function renderStatCards(container, data) {
    const cards = [
      ["Cost / session (est.)", fmtUsd(data.cost_per_session_usd_estimated)],
      ["Tokens / session (mean)", data.mean_tokens_per_session != null ? Math.round(data.mean_tokens_per_session).toLocaleString() : "—"],
      ["p95 LLM latency", data.p95_llm_latency_ms != null ? `${Math.round(data.p95_llm_latency_ms)} ms` : "—"],
      ["Sessions today", data.sessions_today],
    ];
    const row = el("div", { class: "usage-stat-row" });
    for (const [label, value] of cards) {
      row.appendChild(
        el("div", { class: "usage-stat-card" }, [
          el("div", { class: "usage-stat-value" }, String(value)),
          el("div", { class: "usage-stat-label" }, label),
        ])
      );
    }
    container.appendChild(row);
    container.appendChild(
      el(
        "p",
        { class: "muted" },
        `n = ${data.counts.sessions_in_window} sessions, ${data.counts.usage_records} usage records in the last ${data.window_days} days.`
      )
    );
  }

  // Two series (sessions, cost) sharing one x-axis but each scaled to its
  // own max — a shared y-axis would flatten whichever series has the
  // smaller range into a near-flat line.
  function renderChart(container, daily) {
    const width = 560, height = 130, pad = 16;
    const maxSessions = Math.max(1, ...daily.map((d) => d.sessions));
    const maxCost = Math.max(0.000001, ...daily.map((d) => d.cost_usd_estimated || 0));
    const stepX = daily.length > 1 ? (width - pad * 2) / (daily.length - 1) : 0;

    const svgNS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("class", "usage-chart");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "Sessions and cost per day, last " + daily.length + " days");

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
        title.textContent = `${d.date}: ${d.sessions} sessions, ${d.turns} turns, $${(d.cost_usd_estimated || 0).toFixed(6)}`;
        circle.appendChild(title);
        svg.appendChild(circle);
      });
    }

    line((d) => d.sessions, maxSessions, "usage-chart-sessions", "--teal-primary");
    line((d) => d.cost_usd_estimated || 0, maxCost, "usage-chart-cost", "--purple-accent");

    container.appendChild(svg);
    container.appendChild(
      el("div", { class: "usage-chart-legend" }, [
        el("span", { class: "usage-legend-item" }, [
          el("span", { class: "usage-legend-swatch usage-chart-sessions" }),
          "Sessions",
        ]),
        el("span", { class: "usage-legend-item" }, [
          el("span", { class: "usage-legend-swatch usage-chart-cost" }),
          "Cost (USD)",
        ]),
      ])
    );
  }

  function renderProjection(container, data) {
    const inferenceCost = (data.cost_per_session_usd_estimated || 0) * PROJECTED_MONTHLY_SESSIONS;
    const total = inferenceCost + PROJECTED_INFRA_USD_PER_MONTH;
    container.appendChild(
      el(
        "p",
        { class: "muted usage-projection" },
        `Projection, not a measurement: at ${PROJECTED_MONTHLY_SESSIONS.toLocaleString()} sessions/month — ` +
          `$${inferenceCost.toFixed(2)} inference + $${PROJECTED_INFRA_USD_PER_MONTH.toFixed(2)} infrastructure ` +
          `= $${total.toFixed(2)} total.`
      )
    );
  }

  function renderUpdatedAgo() {
    const label = document.getElementById("usage-updated-ago");
    if (!label || !lastUpdatedAt) return;
    const secs = Math.round((Date.now() - lastUpdatedAt) / 1000);
    label.textContent = secs < 5 ? "updated just now" : `updated ${secs}s ago`;
  }

  async function poll() {
    const container = document.getElementById("usage-content");
    if (!container) return;

    const result = await fetchWithMockFallback("/api/admin/usage", AdminPortalMock.usage);
    if (result.authRequired) {
      renderAuthRequired(container, { what: "cost & usage" });
      return;
    }

    const banner = document.getElementById("usage-sample-banner");
    const data = result.data;

    lastUpdatedAt = Date.now();

    if (data.status === "no_data") {
      if (banner) banner.hidden = true;
      container.innerHTML = "";
      container.appendChild(el("p", { class: "muted" }, data.message));
      return;
    }

    if (banner) banner.hidden = !result.usingMock;
    container.innerHTML = "";
    renderStatCards(container, data);
    renderChart(container, data.daily);
    renderProjection(container, data);
    container.appendChild(el("p", { class: "usage-coverage-note" }, data.coverage_note));
    for (const note of data.notes || []) {
      container.appendChild(el("p", { class: "muted" }, note));
    }
    container.appendChild(
      el("p", { class: "muted live-status-row" }, [
        el("span", { class: "live-pulse-dot" }),
        el("span", { id: "usage-updated-ago" }, "updated just now"),
      ])
    );
  }

  function schedule() {
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
    if (document.visibilityState === "hidden") {
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
