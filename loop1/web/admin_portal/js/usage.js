/*
 * usage.js — renders /api/admin/usage into index.html's Cost & Usage card.
 *
 * No chart.js: Phase 1's evidence.js/quality.js never introduced a charting
 * library (their data didn't need one) — adding chart.js here would be a
 * NEW dependency, not matching an existing one, so the 14-day trend is a
 * small hand-rolled inline SVG instead, consistent with Phase 1's
 * zero-external-dependency approach.
 */
(function () {
  const { fetchWithMockFallback, renderAuthRequired, fmtUsd, el } = AdminPortal;

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

  function renderChart(container, daily) {
    const width = 560, height = 110, pad = 16;
    const maxSessions = Math.max(1, ...daily.map((d) => d.sessions));
    const stepX = daily.length > 1 ? (width - pad * 2) / (daily.length - 1) : 0;

    const svgNS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("class", "usage-chart");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "Sessions per day, last " + daily.length + " days");

    const points = daily
      .map((d, i) => {
        const x = pad + i * stepX;
        const y = height - pad - (d.sessions / maxSessions) * (height - pad * 2);
        return `${x},${y}`;
      })
      .join(" ");

    const polyline = document.createElementNS(svgNS, "polyline");
    polyline.setAttribute("points", points);
    polyline.setAttribute("fill", "none");
    polyline.setAttribute("stroke", "#4da3ff");
    polyline.setAttribute("stroke-width", "2");
    svg.appendChild(polyline);

    daily.forEach((d, i) => {
      const x = pad + i * stepX;
      const y = height - pad - (d.sessions / maxSessions) * (height - pad * 2);
      const circle = document.createElementNS(svgNS, "circle");
      circle.setAttribute("cx", x);
      circle.setAttribute("cy", y);
      circle.setAttribute("r", "2.5");
      circle.setAttribute("fill", "#4da3ff");
      const title = document.createElementNS(svgNS, "title");
      title.textContent = `${d.date}: ${d.sessions} sessions, ${d.turns} turns`;
      circle.appendChild(title);
      svg.appendChild(circle);
    });

    container.appendChild(svg);
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

  async function render() {
    const container = document.getElementById("usage-content");
    if (!container) return;

    const result = await fetchWithMockFallback("/api/admin/usage", AdminPortalMock.usage);
    if (result.authRequired) {
      renderAuthRequired(container, { what: "cost & usage" });
      return;
    }

    const banner = document.getElementById("usage-sample-banner");
    const data = result.data;

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
  }

  render();
})();
