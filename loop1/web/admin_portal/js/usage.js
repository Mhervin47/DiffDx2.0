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
  const { fetchWithMockFallback, renderAuthRequired, renderCoverageNote, fmtUsd, currencySymbol, convertAmount, getCurrency, el } = AdminPortal;

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

  // Catmull-Rom -> cubic Bezier, tension 1/6 (the standard conversion) —
  // turns a jagged point-to-point polyline into a smooth curve without
  // overshooting past the actual data points, unlike a naive spline.
  function smoothPath(points) {
    if (points.length < 2) return "";
    let d = `M ${points[0][0].toFixed(2)},${points[0][1].toFixed(2)}`;
    for (let i = 0; i < points.length - 1; i++) {
      const p0 = points[i === 0 ? i : i - 1];
      const p1 = points[i];
      const p2 = points[i + 1];
      const p3 = points[i + 2 < points.length ? i + 2 : i + 1];
      const c1x = p1[0] + (p2[0] - p0[0]) / 6;
      const c1y = p1[1] + (p2[1] - p0[1]) / 6;
      const c2x = p2[0] - (p3[0] - p1[0]) / 6;
      const c2y = p2[1] - (p3[1] - p1[1]) / 6;
      d += ` C ${c1x.toFixed(2)},${c1y.toFixed(2)} ${c2x.toFixed(2)},${c2y.toFixed(2)} ${p2[0].toFixed(2)},${p2[1].toFixed(2)}`;
    }
    return d;
  }

  // Two series (sessions, cost) sharing one x-axis but each scaled to its
  // own max — a shared y-axis would flatten whichever series has the
  // smaller range into a near-flat line. Smoothed curves + a soft gradient
  // fill under each line, light gridlines, sparse date labels, and a
  // custom-positioned hover tooltip (not the native browser one) — same
  // data as before, styled like an actual analytics dashboard instead of a
  // bare polyline plot.
  function renderChart(container, daily) {
    const width = 560, height = 190;
    const padTop = 14, padBottom = 26, padLeft = 10, padRight = 10;
    const plotH = height - padTop - padBottom;
    const maxSessions = Math.max(1, ...daily.map((d) => d.sessions));
    const maxCost = Math.max(0.000001, ...daily.map((d) => d.cost_usd_estimated || 0));
    const stepX = daily.length > 1 ? (width - padLeft - padRight) / (daily.length - 1) : 0;
    const baseY = padTop + plotH;

    const svgNS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("class", "usage-chart");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "Sessions and cost per day, last " + daily.length + " days");

    const defs = document.createElementNS(svgNS, "defs");
    svg.appendChild(defs);

    function xAt(i) { return padLeft + i * stepX; }
    function yAt(valueFn, max, d) { return baseY - (valueFn(d) / max) * plotH; }

    // Gridlines — three light horizontal reference lines, never the axis
    // labels themselves (this chart is a trend indicator, not a precision
    // instrument; exact values live in the tooltip).
    for (let g = 1; g <= 3; g++) {
      const y = padTop + (plotH / 4) * g;
      const gridLine = document.createElementNS(svgNS, "line");
      gridLine.setAttribute("x1", padLeft); gridLine.setAttribute("x2", width - padRight);
      gridLine.setAttribute("y1", y.toFixed(2)); gridLine.setAttribute("y2", y.toFixed(2));
      gridLine.setAttribute("class", "usage-chart-grid");
      svg.appendChild(gridLine);
    }
    const baseline = document.createElementNS(svgNS, "line");
    baseline.setAttribute("x1", padLeft); baseline.setAttribute("x2", width - padRight);
    baseline.setAttribute("y1", baseY.toFixed(2)); baseline.setAttribute("y2", baseY.toFixed(2));
    baseline.setAttribute("class", "usage-chart-baseline");
    svg.appendChild(baseline);

    // Sparse date labels — first, middle, last only; showing all 14 would
    // overlap at this width.
    const labelIdx = daily.length > 2
      ? [0, Math.floor((daily.length - 1) / 2), daily.length - 1]
      : daily.map((_, i) => i);
    for (const i of labelIdx) {
      const label = document.createElementNS(svgNS, "text");
      label.setAttribute("x", xAt(i).toFixed(2));
      label.setAttribute("y", (height - 8).toFixed(2));
      label.setAttribute("text-anchor", i === 0 ? "start" : i === daily.length - 1 ? "end" : "middle");
      label.setAttribute("class", "usage-chart-axis-label");
      const dateObj = new Date(daily[i].date);
      // timeZone: "UTC" matters here — a bare "YYYY-MM-DD" string (what the
      // backend sends) is parsed as UTC midnight by `new Date(...)`, but
      // toLocaleDateString formats in the viewer's local zone by default;
      // without pinning it to UTC too, anyone west of UTC sees the day
      // before the real date.
      label.textContent = isNaN(dateObj.getTime())
        ? daily[i].date
        : dateObj.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
      svg.appendChild(label);
    }

    const dots = []; // collected across both series for the tooltip wiring pass below

    function series(valueFn, max, cls, colorVar, gradId) {
      const pts = daily.map((d, i) => [xAt(i), yAt(valueFn, max, d)]);
      const linePath = smoothPath(pts);

      const grad = document.createElementNS(svgNS, "linearGradient");
      grad.setAttribute("id", gradId);
      grad.setAttribute("x1", "0"); grad.setAttribute("y1", "0");
      grad.setAttribute("x2", "0"); grad.setAttribute("y2", "1");
      const stop1 = document.createElementNS(svgNS, "stop");
      stop1.setAttribute("offset", "0%"); stop1.setAttribute("stop-color", `var(${colorVar})`); stop1.setAttribute("stop-opacity", "0.32");
      const stop2 = document.createElementNS(svgNS, "stop");
      stop2.setAttribute("offset", "100%"); stop2.setAttribute("stop-color", `var(${colorVar})`); stop2.setAttribute("stop-opacity", "0");
      grad.appendChild(stop1); grad.appendChild(stop2);
      defs.appendChild(grad);

      if (pts.length > 1) {
        const area = document.createElementNS(svgNS, "path");
        area.setAttribute("d", `${linePath} L ${pts[pts.length - 1][0].toFixed(2)},${baseY.toFixed(2)} L ${pts[0][0].toFixed(2)},${baseY.toFixed(2)} Z`);
        area.setAttribute("class", "usage-chart-area");
        area.style.fill = `url(#${gradId})`;
        svg.appendChild(area);
      }

      const linePathEl = document.createElementNS(svgNS, "path");
      linePathEl.setAttribute("d", linePath);
      linePathEl.setAttribute("class", `usage-chart-line ${cls}`);
      linePathEl.style.stroke = `var(${colorVar})`;
      svg.appendChild(linePathEl);

      daily.forEach((d, i) => {
        const [x, y] = pts[i];
        const circle = document.createElementNS(svgNS, "circle");
        circle.setAttribute("cx", x.toFixed(2));
        circle.setAttribute("cy", y.toFixed(2));
        circle.setAttribute("r", "2.5");
        circle.setAttribute("class", `usage-chart-dot ${cls}`);
        circle.style.fill = `var(${colorVar})`;
        svg.appendChild(circle);
        dots.push({ el: circle, d, colorVar });
      });
    }

    series((d) => d.sessions, maxSessions, "usage-chart-sessions", "--teal-primary", "usage-grad-sessions");
    series((d) => d.cost_usd_estimated || 0, maxCost, "usage-chart-cost", "--purple-accent", "usage-grad-cost");

    const chartWrap = el("div", { class: "usage-chart-wrap" });
    chartWrap.appendChild(svg);
    const tooltip = el("div", { class: "usage-chart-tooltip" });
    chartWrap.appendChild(tooltip);

    // Custom-positioned tooltip instead of the native browser <title> one —
    // measured off the real rendered dot position (getBoundingClientRect),
    // so it's correct regardless of how the viewBox scales to the actual
    // on-page width.
    function showTooltip(dotEl, d) {
      const wrapRect = chartWrap.getBoundingClientRect();
      const dotRect = dotEl.getBoundingClientRect();
      tooltip.style.left = `${dotRect.left + dotRect.width / 2 - wrapRect.left}px`;
      tooltip.style.top = `${dotRect.top - wrapRect.top}px`;
      tooltip.innerHTML = "";
      const dateObj = new Date(d.date);
      // Same UTC-pin as the axis labels above — d.date is a bare
      // "YYYY-MM-DD" string, parsed as UTC midnight.
      const dateLabel = isNaN(dateObj.getTime())
        ? d.date
        : dateObj.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" });
      tooltip.appendChild(el("div", { class: "tt-date" }, dateLabel));
      tooltip.appendChild(el("div", { class: "tt-row" }, [
        el("span", { class: "tt-dot", style: "background:var(--teal-primary)" }),
        `${d.sessions} session${d.sessions === 1 ? "" : "s"} · ${d.turns} turn${d.turns === 1 ? "" : "s"}`,
      ]));
      tooltip.appendChild(el("div", { class: "tt-row" }, [
        el("span", { class: "tt-dot", style: "background:var(--purple-accent)" }),
        `${currencySymbol()}${convertAmount(d.cost_usd_estimated || 0).toFixed(6)}`,
      ]));
      tooltip.classList.add("visible");
    }
    function hideTooltip() { tooltip.classList.remove("visible"); }

    for (const { el: dotEl, d } of dots) {
      dotEl.addEventListener("mouseenter", () => showTooltip(dotEl, d));
      dotEl.addEventListener("mouseleave", hideTooltip);
    }

    container.appendChild(chartWrap);
    container.appendChild(
      el("div", { class: "usage-chart-legend" }, [
        el("span", { class: "usage-legend-item" }, [
          el("span", { class: "usage-legend-swatch usage-chart-sessions" }),
          "Sessions",
        ]),
        el("span", { class: "usage-legend-item" }, [
          el("span", { class: "usage-legend-swatch usage-chart-cost" }),
          `Cost (${getCurrency()})`,
        ]),
      ])
    );
  }

  function renderProjection(container, data) {
    // Raw USD throughout — converted once, at display time, below. Summing
    // already-converted figures would double-convert the infra constant.
    const inferenceCost = (data.cost_per_session_usd_estimated || 0) * PROJECTED_MONTHLY_SESSIONS;
    const total = inferenceCost + PROJECTED_INFRA_USD_PER_MONTH;
    const sym = currencySymbol();
    container.appendChild(
      el(
        "p",
        { class: "muted usage-projection" },
        `Projection, not a measurement: at ${PROJECTED_MONTHLY_SESSIONS.toLocaleString()} sessions/month — ` +
          `${sym}${convertAmount(inferenceCost).toFixed(2)} inference + ${sym}${convertAmount(PROJECTED_INFRA_USD_PER_MONTH).toFixed(2)} infrastructure ` +
          `= ${sym}${convertAmount(total).toFixed(2)} total.`
      )
    );
  }

  function renderVoiceUsage(voice) {
    const container = document.getElementById("voice-usage-content");
    if (!container) return;

    if (!voice || voice.status === "no_data") {
      container.innerHTML = "";
      container.appendChild(el("p", { class: "muted" }, (voice && voice.message) || "No voice usage data."));
      if (voice) renderCoverageNote(container, voice.coverage_note);
      return;
    }

    const cards = [
      ["Requests", voice.requests_total, voice.requests_failed ? `${voice.requests_failed} failed` : null],
      ["Characters", voice.chars_total != null ? voice.chars_total.toLocaleString() : "—"],
      ["Cost (est.)", fmtUsd(voice.cost_usd_estimated)],
    ];
    const row = el("div", { class: "usage-stat-row" });
    for (const [label, value, sub] of cards) {
      row.appendChild(
        el("div", { class: "usage-stat-card" }, [
          el("div", { class: "usage-stat-value" }, String(value)),
          el("div", { class: "usage-stat-label" }, label),
          sub ? el("div", { class: "usage-stat-label" }, sub) : null,
        ])
      );
    }
    container.innerHTML = "";
    container.appendChild(row);

    if (voice.by_operation && voice.by_operation.length) {
      const opText = voice.by_operation
        .map((o) => `${o.operation} — ${o.count} req, ${o.chars.toLocaleString()} chars, ${fmtUsd(o.cost_usd_estimated)}`)
        .join(" · ");
      container.appendChild(el("p", { class: "muted" }, `By operation: ${opText}`));
    }
    if (voice.by_language && voice.by_language.length) {
      const langText = voice.by_language
        .map((l) => `${l.language} (${l.count})`)
        .join(" · ");
      container.appendChild(el("p", { class: "muted" }, `By language: ${langText}`));
    }
    renderCoverageNote(container, voice.coverage_note);
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
      const voiceContainer = document.getElementById("voice-usage-content");
      if (voiceContainer) renderAuthRequired(voiceContainer, { what: "voice usage" });
      return;
    }

    const banner = document.getElementById("usage-sample-banner");
    const data = result.data;

    lastUpdatedAt = Date.now();

    if (data.status === "no_data") {
      if (banner) banner.hidden = true;
      container.innerHTML = "";
      container.appendChild(el("p", { class: "muted" }, data.message));
      renderVoiceUsage(data.voice_usage);
      return;
    }

    if (banner) banner.hidden = !result.usingMock;
    container.innerHTML = "";
    renderStatCards(container, data);
    renderChart(container, data.daily);
    renderProjection(container, data);
    renderVoiceUsage(data.voice_usage);
    const voiceBanner = document.getElementById("voice-usage-sample-banner");
    if (voiceBanner) voiceBanner.hidden = !result.usingMock;
    renderCoverageNote(container, data.coverage_note, { extraNotes: data.notes });
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
