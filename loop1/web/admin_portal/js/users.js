/*
 * users.js — renders /api/admin/users/{summary,timeseries,engagement,retention}
 * into users.html.
 *
 * Two distinct fallback mechanisms, deliberately not conflated:
 *   1. fetchWithMockFallback()'s existing "sample data" path — fires on a
 *      failed/unauthenticated request, same as every other admin page.
 *   2. This page's own "demo data" per-section note — fires when a request
 *      SUCCEEDS but data_sufficient is false (too little real data to chart
 *      meaningfully). Renders the same AdminPortalMock.users* fixture, but
 *      with different wording and re-evaluated fresh on every poll — no
 *      manual toggle, no persisted "this section is in demo mode" state.
 */
(function () {
  const { fetchWithMockFallback, renderAuthRequired, fmtPct, el } = AdminPortal;

  const state = { days: 30 };
  const RANGE_OPTIONS = [7, 30, 90, 180];
  const seriesVisibility = {}; // legend-toggle state, keyed by series id, shared across chart re-renders

  // ---------------------------------------------------------------------
  // Demo-data note — visually distinct from the existing sample-banner:
  // different text, its own id/class, same amber tier.
  // ---------------------------------------------------------------------
  function renderDemoNote(containerId, show) {
    const root = document.getElementById(containerId);
    if (!root) return;
    root.innerHTML = "";
    if (!show) return;
    root.appendChild(
      el("span", { class: "demo-data-note" }, "Demo data — shown until usage is high enough to chart")
    );
  }

  // A section is either fully real or fully demo for a given render, never
  // blended — this is the one place that decision gets made, off the REAL
  // response's own data_sufficient flag (never the mock fixture's, which
  // always says true — it has to, it's fabricated to look plausible).
  function pickDisplayData(realData, mockData) {
    return realData.data_sufficient ? realData : mockData;
  }

  // A whole section can be "data_sufficient" (enough overall activity to
  // chart) while one specific series within it is genuinely all-zero — e.g.
  // plenty of real patient/session activity but zero cancellations so far.
  // That's not a bug, just an empty chart, but an empty chart reads as
  // "broken" — so these two series get their own, narrower demo fallback,
  // independent of the section-level data_sufficient flag.
  function seriesAllZero(rows, keys) {
    return rows.every((r) => keys.every((k) => !r[k]));
  }

  // ---------------------------------------------------------------------
  // Range tabs — same role="tablist"/"tab" + arrow-key pattern as
  // architecture-validation.js.
  // ---------------------------------------------------------------------
  function renderRangeTabs() {
    const root = document.getElementById("range-tabs");
    root.innerHTML = "";
    const tabs = RANGE_OPTIONS.map((d) =>
      el("button", {
        type: "button",
        role: "tab",
        "aria-selected": String(d === state.days),
        tabindex: d === state.days ? "0" : "-1",
      }, `${d}D`)
    );
    tabs.forEach((tab, i) => {
      tab.addEventListener("click", () => {
        state.days = RANGE_OPTIONS[i];
        renderRangeTabs();
        loadRangeDependent();
      });
      tab.addEventListener("keydown", (e) => {
        if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
        e.preventDefault();
        const dir = e.key === "ArrowRight" ? 1 : -1;
        const next = (i + dir + tabs.length) % tabs.length;
        tabs[next].focus();
        state.days = RANGE_OPTIONS[next];
        renderRangeTabs();
        loadRangeDependent();
      });
      root.appendChild(tab);
    });
  }

  // ---------------------------------------------------------------------
  // 1. KPI tiles
  // ---------------------------------------------------------------------
  function renderKpis(summary) {
    const root = document.getElementById("kpi-grid");
    root.innerHTML = "";
    const tiles = [
      [summary.total_patients, "Total Patients"],
      [summary.total_doctors, "Total Doctors"],
      [summary.new_patients_7d, "New Patients (7d)"],
      [summary.active_patients_30d, "Active Patients (30d)"],
      [summary.dormant_patients_30d, "Dormant Patients (30d)"],
      [fmtPct(summary.repeat_usage_rate_pct / 100, 1), "Repeat-Usage Rate"],
      [`${summary.avg_session_minutes} min`, "Avg. Session Length"],
      [fmtPct(summary.abandonment_rate_pct / 100, 1), "Abandonment Rate"],
      [fmtPct(summary.cancellation_rate_pct / 100, 1), "Cancellation Rate (30d)"],
    ];
    for (const [value, label] of tiles) {
      root.appendChild(
        el("div", { class: "stat-tile" }, [
          el("div", { class: "stat-tile-value" }, String(value)),
          el("div", { class: "stat-tile-label" }, label),
        ])
      );
    }
  }

  // ---------------------------------------------------------------------
  // Catmull-Rom -> cubic Bezier, tension 1/6 — same conversion used by the
  // Overview page's Cost & Usage chart, so every chart in the portal reads
  // as one system rather than two different visual languages.
  // ---------------------------------------------------------------------
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

  function formatChartDate(dateStr) {
    const d = new Date(dateStr);
    if (isNaN(d.getTime())) return dateStr;
    // timeZone: "UTC" — the backend sends bare "YYYY-MM-DD" strings, parsed
    // as UTC midnight; formatting in the viewer's local zone instead would
    // show the wrong day for anyone west of UTC.
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
  }

  // ---------------------------------------------------------------------
  // Chart primitive — smooth curves, gradient area fill, gridlines, sparse
  // date labels, and a custom hover tooltip, same design as the Overview
  // page's Cost & Usage chart. Legend swatches toggle series visibility
  // (persisted in seriesVisibility across re-renders/range changes so a
  // hidden series stays hidden on toggle).
  // ---------------------------------------------------------------------
  let chartIdCounter = 0;

  function renderLineChart(container, dates, seriesDefs) {
    container.innerHTML = "";
    const width = 560, height = 190;
    const padTop = 14, padBottom = 26, padLeft = 10, padRight = 10;
    const plotH = height - padTop - padBottom;
    const stepX = dates.length > 1 ? (width - padLeft - padRight) / (dates.length - 1) : 0;
    const baseY = padTop + plotH;
    const chartInstanceId = ++chartIdCounter;

    const svgNS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("class", "users-chart");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", seriesDefs.map((s) => s.label).join(", ") + ", " + dates.length + " days");

    const defs = document.createElementNS(svgNS, "defs");
    svg.appendChild(defs);

    for (let g = 1; g <= 3; g++) {
      const y = padTop + (plotH / 4) * g;
      const gridLine = document.createElementNS(svgNS, "line");
      gridLine.setAttribute("x1", padLeft); gridLine.setAttribute("x2", width - padRight);
      gridLine.setAttribute("y1", y.toFixed(2)); gridLine.setAttribute("y2", y.toFixed(2));
      gridLine.setAttribute("class", "chart-grid");
      svg.appendChild(gridLine);
    }
    const baseline = document.createElementNS(svgNS, "line");
    baseline.setAttribute("x1", padLeft); baseline.setAttribute("x2", width - padRight);
    baseline.setAttribute("y1", baseY.toFixed(2)); baseline.setAttribute("y2", baseY.toFixed(2));
    baseline.setAttribute("class", "chart-baseline");
    svg.appendChild(baseline);

    const labelIdx = dates.length > 2
      ? [0, Math.floor((dates.length - 1) / 2), dates.length - 1]
      : dates.map((_, i) => i);
    for (const i of labelIdx) {
      const label = document.createElementNS(svgNS, "text");
      label.setAttribute("x", (padLeft + i * stepX).toFixed(2));
      label.setAttribute("y", (height - 8).toFixed(2));
      label.setAttribute("text-anchor", i === 0 ? "start" : i === dates.length - 1 ? "end" : "middle");
      label.setAttribute("class", "chart-axis-label");
      label.textContent = formatChartDate(dates[i]);
      svg.appendChild(label);
    }

    const dots = [];

    for (const s of seriesDefs) {
      if (seriesVisibility[s.id] === undefined) seriesVisibility[s.id] = true;
      const visible = seriesVisibility[s.id];
      const max = Math.max(1, ...s.values);
      const points = s.values.map((v, i) => [padLeft + i * stepX, baseY - (v / max) * plotH]);
      const linePath = smoothPath(points);
      const gradId = `users-grad-${chartInstanceId}-${s.id}`;

      const grad = document.createElementNS(svgNS, "linearGradient");
      grad.setAttribute("id", gradId);
      grad.setAttribute("x1", "0"); grad.setAttribute("y1", "0");
      grad.setAttribute("x2", "0"); grad.setAttribute("y2", "1");
      const stop1 = document.createElementNS(svgNS, "stop");
      stop1.setAttribute("offset", "0%"); stop1.setAttribute("stop-color", `var(${s.colorVar})`); stop1.setAttribute("stop-opacity", "0.30");
      const stop2 = document.createElementNS(svgNS, "stop");
      stop2.setAttribute("offset", "100%"); stop2.setAttribute("stop-color", `var(${s.colorVar})`); stop2.setAttribute("stop-opacity", "0");
      grad.appendChild(stop1); grad.appendChild(stop2);
      defs.appendChild(grad);

      if (points.length > 1) {
        const area = document.createElementNS(svgNS, "path");
        area.setAttribute("d", `${linePath} L ${points[points.length - 1][0].toFixed(2)},${baseY.toFixed(2)} L ${points[0][0].toFixed(2)},${baseY.toFixed(2)} Z`);
        area.setAttribute("class", "chart-area");
        area.style.fill = `url(#${gradId})`;
        area.style.display = visible ? "" : "none";
        svg.appendChild(area);
      }

      const lineEl = document.createElementNS(svgNS, "path");
      lineEl.setAttribute("d", linePath);
      lineEl.setAttribute("class", "chart-line");
      lineEl.style.stroke = `var(${s.colorVar})`;
      lineEl.style.display = visible ? "" : "none";
      svg.appendChild(lineEl);

      points.forEach(([x, y], i) => {
        const circle = document.createElementNS(svgNS, "circle");
        circle.setAttribute("cx", x.toFixed(2));
        circle.setAttribute("cy", y.toFixed(2));
        circle.setAttribute("r", "2.25");
        circle.setAttribute("class", "chart-dot");
        circle.style.fill = `var(${s.colorVar})`;
        circle.style.display = visible ? "" : "none";
        svg.appendChild(circle);
        if (visible) dots.push({ el: circle, date: dates[i], label: s.label, value: s.values[i], colorVar: s.colorVar });
      });
    }

    const chartWrap = el("div", { class: "users-chart-wrap" });
    chartWrap.appendChild(svg);
    const tooltip = el("div", { class: "users-chart-tooltip" });
    chartWrap.appendChild(tooltip);
    wireTooltip(chartWrap, tooltip, dots);

    container.appendChild(chartWrap);
    container.appendChild(renderChartLegend(seriesDefs, () => renderLineChart(container, dates, seriesDefs)));
  }

  // Custom-positioned tooltip (measured off the real rendered dot/bar via
  // getBoundingClientRect, so it's correct regardless of viewBox scaling)
  // instead of the native browser <title> one — same approach as the
  // Overview page's Cost & Usage chart.
  function wireTooltip(chartWrap, tooltip, points) {
    function show(pointEl, entries) {
      const wrapRect = chartWrap.getBoundingClientRect();
      const elRect = pointEl.getBoundingClientRect();
      tooltip.style.left = `${elRect.left + elRect.width / 2 - wrapRect.left}px`;
      tooltip.style.top = `${elRect.top - wrapRect.top}px`;
      tooltip.innerHTML = "";
      tooltip.appendChild(el("div", { class: "tt-date" }, formatChartDate(entries[0].date)));
      for (const entry of entries) {
        tooltip.appendChild(el("div", { class: "tt-row" }, [
          el("span", { class: "tt-dot", style: `background:var(${entry.colorVar})` }),
          `${entry.label}: ${entry.value}`,
        ]));
      }
      tooltip.classList.add("visible");
    }
    function hide() { tooltip.classList.remove("visible"); }

    for (const p of points) {
      p.el.addEventListener("mouseenter", () => show(p.el, [p]));
      p.el.addEventListener("mouseleave", hide);
    }
  }

  function renderChartLegend(seriesDefs, onToggle) {
    return el("div", { class: "chart-legend" }, seriesDefs.map((s) => {
      const visible = seriesVisibility[s.id] !== false;
      const item = el("span", { class: `chart-legend-item${visible ? "" : " off"}` }, [
        el("span", { class: "chart-legend-swatch", style: `background:var(${s.colorVar})` }),
        s.label,
      ]);
      item.tabIndex = 0;
      item.setAttribute("role", "button");
      item.setAttribute("aria-pressed", String(visible));
      const toggle = () => {
        seriesVisibility[s.id] = !visible;
        onToggle();
      };
      item.addEventListener("click", toggle);
      item.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
      return item;
    }));
  }

  function renderStackedBarChart(container, dates, seriesDefs) {
    container.innerHTML = "";
    const width = 560, height = 190;
    const padTop = 14, padBottom = 26, padLeft = 10, padRight = 10;
    const plotH = height - padTop - padBottom;
    const baseY = padTop + plotH;
    const n = dates.length;
    const stepX = n > 1 ? (width - padLeft - padRight) / n : (width - padLeft - padRight);
    const barW = Math.max(1, stepX - 1.5);
    for (const s of seriesDefs) {
      if (seriesVisibility[s.id] === undefined) seriesVisibility[s.id] = true;
    }
    const visibleSeries = seriesDefs.filter((s) => seriesVisibility[s.id]);
    const maxTotal = Math.max(1, ...dates.map((_, i) => visibleSeries.reduce((sum, s) => sum + s.values[i], 0)));

    const svgNS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("class", "users-chart");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "Cancellations by category, " + n + " days");

    for (let g = 1; g <= 3; g++) {
      const y = padTop + (plotH / 4) * g;
      const gridLine = document.createElementNS(svgNS, "line");
      gridLine.setAttribute("x1", padLeft); gridLine.setAttribute("x2", width - padRight);
      gridLine.setAttribute("y1", y.toFixed(2)); gridLine.setAttribute("y2", y.toFixed(2));
      gridLine.setAttribute("class", "chart-grid");
      svg.appendChild(gridLine);
    }
    const baseline = document.createElementNS(svgNS, "line");
    baseline.setAttribute("x1", padLeft); baseline.setAttribute("x2", width - padRight);
    baseline.setAttribute("y1", baseY.toFixed(2)); baseline.setAttribute("y2", baseY.toFixed(2));
    baseline.setAttribute("class", "chart-baseline");
    svg.appendChild(baseline);

    const labelIdx = n > 2 ? [0, Math.floor((n - 1) / 2), n - 1] : dates.map((_, i) => i);
    for (const i of labelIdx) {
      const label = document.createElementNS(svgNS, "text");
      label.setAttribute("x", (padLeft + i * stepX + barW / 2).toFixed(2));
      label.setAttribute("y", (height - 8).toFixed(2));
      label.setAttribute("text-anchor", i === 0 ? "start" : i === n - 1 ? "end" : "middle");
      label.setAttribute("class", "chart-axis-label");
      label.textContent = formatChartDate(dates[i]);
      svg.appendChild(label);
    }

    const barsByDay = [];
    for (let i = 0; i < n; i++) {
      let yCursor = baseY;
      const x = padLeft + i * stepX;
      const dayEntries = [];
      for (const s of visibleSeries) {
        const v = s.values[i];
        if (!v) continue;
        const h = (v / maxTotal) * plotH;
        const rect = document.createElementNS(svgNS, "rect");
        rect.setAttribute("x", x.toFixed(2));
        rect.setAttribute("y", (yCursor - h).toFixed(2));
        rect.setAttribute("width", barW.toFixed(2));
        rect.setAttribute("height", h.toFixed(2));
        rect.setAttribute("rx", "1.5");
        rect.setAttribute("class", "chart-bar");
        rect.style.fill = `var(${s.colorVar})`;
        svg.appendChild(rect);
        yCursor -= h;
        dayEntries.push({ el: rect, date: dates[i], label: s.label, value: v, colorVar: s.colorVar });
      }
      barsByDay.push(...dayEntries);
    }

    const chartWrap = el("div", { class: "users-chart-wrap" });
    chartWrap.appendChild(svg);
    const tooltip = el("div", { class: "users-chart-tooltip" });
    chartWrap.appendChild(tooltip);
    wireTooltip(chartWrap, tooltip, barsByDay);

    container.appendChild(chartWrap);
    container.appendChild(renderChartLegend(seriesDefs, () => renderStackedBarChart(container, dates, seriesDefs)));
  }

  // ---------------------------------------------------------------------
  // Range-dependent sections: timeseries (growth+cancellations) and
  // engagement. Re-fetched together on every range-tab change.
  // ---------------------------------------------------------------------
  async function loadRangeDependent() {
    const [summaryResult, timeseriesResult, engagementResult] = await Promise.all([
      fetchWithMockFallback("/api/admin/users/summary", AdminPortalMock.usersSummary),
      fetchWithMockFallback(`/api/admin/users/timeseries?days=${state.days}`, AdminPortalMock.usersTimeseries),
      fetchWithMockFallback(`/api/admin/users/engagement?days=${state.days}`, AdminPortalMock.usersEngagement),
    ]);

    if (timeseriesResult.authRequired) {
      renderAuthRequired(document.getElementById("chart-patients"), { what: "growth & activity" });
      renderAuthRequired(document.getElementById("chart-doctors"), { what: "growth & activity" });
      renderAuthRequired(document.getElementById("chart-cancellations"), { what: "cancellations" });
    } else {
      renderDemoNote("growth-demo-note", !timeseriesResult.data.data_sufficient);
      const ts = pickDisplayData(timeseriesResult.data, AdminPortalMock.usersTimeseries);
      const dates = ts.series.map((r) => r.date);

      renderLineChart(document.getElementById("chart-patients"), dates, [
        { id: "new_patients", label: "New patients", colorVar: "--teal-primary", values: ts.series.map((r) => r.new_patients) },
        { id: "active_patients", label: "Active patients", colorVar: "--blue-secondary", values: ts.series.map((r) => r.active_patients) },
      ]);

      // Doctor roster = running cumulative total, anchored to the real
      // total_doctors from /summary (the timeseries only has per-day NEW
      // doctor counts — a roster is a running total, not a daily delta).
      // Same real-vs-demo pick as the timeseries itself, off /summary's own
      // data_sufficient — the two endpoints can disagree, each stays
      // independently real or demo.
      const summaryForRoster = summaryResult.authRequired
        ? AdminPortalMock.usersSummary
        : pickDisplayData(summaryResult.data, AdminPortalMock.usersSummary);
      const totalDoctorsNow = summaryForRoster.total_doctors;
      const newDoctorsSeries = ts.series.map((r) => r.new_doctors);
      const roster = new Array(newDoctorsSeries.length);
      let running = totalDoctorsNow;
      for (let i = newDoctorsSeries.length - 1; i >= 0; i--) {
        roster[i] = running;
        running -= newDoctorsSeries[i];
      }
      renderLineChart(document.getElementById("chart-doctors"), dates, [
        { id: "doctor_roster", label: "Doctor roster", colorVar: "--purple-accent", values: roster },
        { id: "active_doctors", label: "Active doctors", colorVar: "--teal-primary", values: ts.series.map((r) => r.active_doctors) },
      ]);

      const cancellationKeys = ["cancellations_patient", "cancellations_patient_reschedule", "cancellations_doctor"];
      const cancellationsSparse = seriesAllZero(ts.series, cancellationKeys);
      const cancellationsRows = cancellationsSparse ? AdminPortalMock.usersTimeseries.series : ts.series;
      renderDemoNote("cancellations-demo-note", cancellationsSparse);
      renderStackedBarChart(document.getElementById("chart-cancellations"), cancellationsRows.map((r) => r.date), [
        { id: "cancellations_patient", label: "Patient-cancelled", colorVar: "--teal-primary", values: cancellationsRows.map((r) => r.cancellations_patient) },
        { id: "cancellations_patient_reschedule", label: "Patient reschedule", colorVar: "--purple-accent", values: cancellationsRows.map((r) => r.cancellations_patient_reschedule) },
        { id: "cancellations_doctor", label: "Doctor-cancelled", colorVar: "--blue-secondary", values: cancellationsRows.map((r) => r.cancellations_doctor) },
      ]);
    }

    if (engagementResult.authRequired) {
      renderAuthRequired(document.getElementById("chart-engagement"), { what: "engagement" });
      document.querySelector("#termination-table tbody").innerHTML = "";
    } else {
      const eng = pickDisplayData(engagementResult.data, AdminPortalMock.usersEngagement);
      const engagementSparse = !engagementResult.data.data_sufficient
        ? false // already demo via pickDisplayData above — don't double-flag
        : seriesAllZero(eng.series, ["avg_session_minutes"]);
      renderDemoNote("engagement-demo-note", !engagementResult.data.data_sufficient || engagementSparse);
      const engagementRows = engagementSparse ? AdminPortalMock.usersEngagement.series : eng.series;
      renderLineChart(document.getElementById("chart-engagement"), engagementRows.map((r) => r.date), [
        { id: "avg_session_minutes", label: "Avg. session minutes", colorVar: "--teal-primary", values: engagementRows.map((r) => r.avg_session_minutes) },
      ]);
      renderTerminationTable(eng.termination_breakdown);
    }
  }

  function renderTerminationTable(breakdown) {
    const tbody = document.querySelector("#termination-table tbody");
    tbody.innerHTML = "";
    const total = Object.values(breakdown).reduce((a, b) => a + b, 0);
    const rowLabels = {
      max_turns: "max_turns",
      safety_stop: "safety_stop",
      confidence_threshold: "confidence_threshold",
      user_quit: "user_quit — abandonment",
    };
    for (const key of ["max_turns", "safety_stop", "confidence_threshold", "user_quit"]) {
      const count = breakdown[key] || 0;
      const pct = total ? (count / total) * 100 : 0;
      tbody.appendChild(el("tr", {}, [
        el("td", {}, rowLabels[key]),
        el("td", { class: "num mono" }, String(count)),
        el("td", { class: "num mono" }, `${pct.toFixed(1)}%`),
      ]));
    }
  }

  // ---------------------------------------------------------------------
  // Snapshot sections: KPIs (from /summary) and stuck-sessions tile — not
  // range-dependent, loaded once and on a slower poll, independent of the
  // range-tab re-fetches above.
  // ---------------------------------------------------------------------
  async function loadSnapshot() {
    const result = await fetchWithMockFallback("/api/admin/users/summary", AdminPortalMock.usersSummary);
    if (result.authRequired) {
      renderAuthRequired(document.getElementById("kpi-grid"), { what: "user metrics" });
      return;
    }
    renderDemoNote("kpi-demo-note", !result.data.data_sufficient);
    const summary = pickDisplayData(result.data, AdminPortalMock.usersSummary);
    renderKpis(summary);

    const tile = document.getElementById("stuck-sessions-tile");
    tile.innerHTML = "";
    if (summary.stuck_sessions > 0) {
      tile.appendChild(
        el("div", { class: "warning-tile" }, [
          el("span", { class: "wt-value" }, String(summary.stuck_sessions)),
          el("span", { class: "wt-label" }, "session(s) stuck open more than 24h — not cleaned up, or a patient got stuck mid-interview"),
        ])
      );
    }
  }

  // ---------------------------------------------------------------------
  // Retention — its own weeks= param, no range toggle (spec: a table, not
  // a chart, doesn't share the days-based range above).
  // ---------------------------------------------------------------------
  async function loadRetention() {
    const result = await fetchWithMockFallback("/api/admin/users/retention?weeks=12", AdminPortalMock.usersRetention);
    const tbody = document.querySelector("#retention-table tbody");
    if (result.authRequired) {
      renderAuthRequired(document.getElementById("retention-section"), { what: "retention cohorts" });
      return;
    }
    renderDemoNote("retention-demo-note", !result.data.data_sufficient);
    const data = pickDisplayData(result.data, AdminPortalMock.usersRetention);
    tbody.innerHTML = "";
    if (!data.cohorts.length) {
      tbody.appendChild(el("tr", {}, el("td", { class: "muted", colspan: "4" }, "No cohorts in range.")));
      return;
    }
    for (const c of data.cohorts) {
      tbody.appendChild(el("tr", {}, [
        el("td", {}, c.cohort_week),
        el("td", { class: "num mono" }, String(c.cohort_size)),
        el("td", { class: "num mono" }, String(c.returned_within_30d)),
        el("td", { class: "num mono" }, `${c.retention_pct}%`),
      ]));
    }
  }

  renderRangeTabs();
  loadSnapshot();
  loadRangeDependent();
  loadRetention();

  const POLL_MS = 30000;
  setInterval(() => {
    if (document.visibilityState !== "hidden") {
      loadSnapshot();
      loadRangeDependent();
    }
  }, POLL_MS);
})();
