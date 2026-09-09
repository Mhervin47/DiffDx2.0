/* quality.js — renders /api/admin/quality (or mock.js sample data) into the page. */
(async function () {
  const { fetchWithMockFallback, fmtPct, el } = AdminPortal;

  const mockReport = AdminPortalMock.evidenceReport;
  const mockQuality = {
    status: "ok",
    reasoning_quality: mockReport.reasoning_quality,
    cross_reference: mockReport.cross_reference,
  };
  const { data, usingMock } = await fetchWithMockFallback("/api/admin/quality", mockQuality);

  document.getElementById("sample-banner").hidden = !usingMock;

  if (!data.reasoning_quality) {
    document.getElementById("content").innerHTML =
      '<p class="muted">No critique data found — run <code>python -m loop2.runners.phase7_eval</code> (without --skip-critique), then regenerate and publish the report.</p>';
    return;
  }

  renderScores(data.reasoning_quality);
  renderCalibration(data.reasoning_quality);
  renderWeaknesses(data.reasoning_quality);
  renderCrossReference(data.cross_reference || []);

  function renderScores(rq) {
    const table = document.getElementById("scores-table");
    table.appendChild(el("thead", {}, el("tr", {}, [el("th", {}, "Score"), el("th", {}, "Mean"), el("th", {}, "Median")])));
    const rows = [
      ["Question quality", rq.mean_question_quality, rq.median_question_quality],
      ["Differential quality", rq.mean_differential_quality, rq.median_differential_quality],
      ["Reasoning quality", rq.mean_reasoning_quality, rq.median_reasoning_quality],
    ];
    const tbody = el("tbody");
    for (const [label, mean, median] of rows) {
      tbody.appendChild(el("tr", {}, [
        el("td", { class: "metric-label" }, label),
        el("td", {}, mean === null ? "—" : mean.toFixed(3)),
        el("td", {}, median === null ? "—" : median.toFixed(3)),
      ]));
    }
    table.appendChild(tbody);
    document.getElementById("turn-summary").textContent =
      `${rq.total_turns} turns critiqued across ${rq.total_sessions} sessions.`;
  }

  function renderCalibration(rq) {
    const container = document.getElementById("calibration-section");
    const dist = rq.confidence_calibration_distribution || {};
    const total = Object.values(dist).reduce((a, b) => a + b, 0);
    if (total === 0) {
      container.appendChild(el("p", { class: "muted" }, "No calibration data."));
      return;
    }
    const ul = el("ul", { class: "bar-list" });
    for (const [label, count] of Object.entries(dist)) {
      const pct = count / total;
      const li = el("li");
      li.appendChild(el("span", { class: "bar-label" }, `${label} — ${count} (${fmtPct(pct, 0)})`));
      const bar = el("div", { class: "bar-track" }, el("div", { class: `bar-fill bar-${label}`, style: `width:${(pct * 100).toFixed(1)}%` }));
      li.appendChild(bar);
      ul.appendChild(li);
    }
    container.appendChild(ul);
  }

  function renderWeaknesses(rq) {
    const container = document.getElementById("weakness-section");
    const counts = rq.weakness_category_counts || {};
    const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
    if (!entries.length) {
      container.appendChild(el("p", { class: "muted" }, "No weakness categories recorded."));
      return;
    }
    const ul = el("ul", { class: "plain-list" });
    for (const [cat, count] of entries) {
      ul.appendChild(el("li", {}, `${cat.replace(/_/g, " ")}: ${count}`));
    }
    container.appendChild(ul);

    if (rq.top_weakness_texts && rq.top_weakness_texts.length) {
      container.appendChild(el("h3", { class: "sub-heading" }, "Most common recorded weaknesses"));
      const ul2 = el("ul", { class: "plain-list" });
      for (const item of rq.top_weakness_texts) {
        ul2.appendChild(el("li", {}, `(${item.count}×) ${item.weakness}`));
      }
      container.appendChild(ul2);
    }
  }

  function renderCrossReference(rows) {
    const table = document.getElementById("cross-ref-table");
    if (!rows.length) {
      document.getElementById("cross-ref-section").appendChild(
        el("p", { class: "muted" }, "No cross-referenced failures (or no critique data).")
      );
      return;
    }
    table.appendChild(el("thead", {}, el("tr", {}, [
      el("th", {}, "Patient"),
      el("th", {}, "Mean Q Quality"),
      el("th", {}, "Low-Quality Turns"),
      el("th", {}, "Correct?"),
      el("th", {}, "In Exemplar Pool"),
      el("th", {}, "Weakness Categories"),
    ])));
    const tbody = el("tbody");
    // Rendered in the exact order the API returns — already sorted by
    // cross_reference_failures() (wrong diagnosis first, then by mean
    // question quality ascending). Do not re-sort here.
    for (const row of rows) {
      tbody.appendChild(el("tr", {}, [
        el("td", {}, row.patient_id),
        el("td", {}, row.mean_question_quality === null ? "—" : row.mean_question_quality.toFixed(2)),
        el("td", {}, (row.low_quality_turns || []).join(", ") || "—"),
        el("td", {}, row.leading_diagnosis_correct ? "✓" : "✗"),
        el("td", {}, row.in_exemplar_pool ? "yes" : "no"),
        el("td", {}, [...new Set(row.weakness_categories || [])].join(", ") || "—"),
      ]));
    }
    table.appendChild(tbody);
  }
})();
