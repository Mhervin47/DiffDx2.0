/*
 * quality.js — renders /api/admin/quality (or mock.js sample data) into the
 * page.
 *
 * /api/admin/quality (and /api/admin/evidence, fetched here too for the
 * safety summary and static badge) now require require_role("admin") —
 * project owner's explicit decision. Unauthenticated visitors get the same
 * "sign in as admin" prompt every other admin_portal page uses.
 *
 * Design-pass rewrite (evidence_quality_redesign_prompt.md): grouped by the
 * question a viewer has, one hero stat per section, same visual family as
 * the redesigned evidence.html. All existing empty-states/fallback copy
 * preserved verbatim.
 */
(async function () {
  const { fetchWithMockFallback, fetchJSON, renderAuthRequired, fmtPct, el } = AdminPortal;

  const mockReport = AdminPortalMock.evidenceReport;
  const mockQuality = {
    status: "ok",
    reasoning_quality: mockReport.reasoning_quality,
    cross_reference: mockReport.cross_reference,
  };
  const result = await fetchWithMockFallback("/api/admin/quality", mockQuality);

  if (result.authRequired) {
    document.getElementById("sample-banner").hidden = true;
    renderAuthRequired(document.getElementById("content"), { what: "AI quality scores" });
    return;
  }

  const { data, usingMock } = result;

  document.getElementById("sample-banner").hidden = !usingMock;

  renderStaticBadge();

  if (!data.reasoning_quality) {
    document.getElementById("content").innerHTML =
      '<p class="muted">No critique data found — run <code>python -m loop2.runners.phase7_eval</code> (without --skip-critique), then regenerate and publish the report.</p>';
    return;
  }

  renderSafetySummary();
  renderReasoningSection(data.reasoning_quality);
  renderCalibration(data.reasoning_quality);
  renderWeaknesses(data.reasoning_quality);
  renderCrossReference(data.cross_reference || []);

  // Item 8 — this page is also a static, point-in-time offline benchmark
  // (same underlying report as evidence.html), not something that updates
  // on its own; no polling here for the same reason.
  function renderStaticBadge() {
    const root = document.getElementById("static-badge-root");
    if (!root) return;
    // /api/admin/quality doesn't carry eval_set_size (it only ever returns
    // reasoning_quality/cross_reference — see admin_portal.py's
    // get_quality()) so this fetches the evidence endpoint, same as the
    // safety-summary sentence below, rather than guessing the number.
    fetchJSON("/api/admin/evidence").then((evidence) => {
      // We already know from the main /api/admin/quality fetch above that
      // this viewer IS an admin (the authRequired branch already returned
      // early otherwise) — so a 401 here would be a genuinely inconsistent
      // auth state, not the normal "not signed in" case. Treat it as
      // "not available" rather than silently falling back to sample data.
      if (evidence && evidence.status === "auth_required") {
        root.appendChild(el("span", { class: "static-badge" }, "Offline benchmark — not live"));
        return;
      }
      const n = evidence && evidence.status === "ok" ? evidence.eval_set_size : (usingMock ? mockReport.eval_set_size : null);
      root.appendChild(
        el("span", { class: "static-badge" }, n != null ? `Offline benchmark — n=${n} — not live` : "Offline benchmark — not live")
      );
    });
  }

  // Item 4 — plain-English translation of the safety-recall delta, now
  // promoted to a lead takeaway under the header rather than a plain muted
  // paragraph — it already was exactly the "one-sentence claim" the
  // redesign wants, just needed visual weight. This data does not exist in
  // /api/admin/quality's own response (confirmed by reading admin_portal.py's
  // get_quality(), which only returns reasoning_quality/cross_reference) —
  // it lives in /api/admin/evidence's safety block, so this fetches that
  // endpoint too (it's already public, zero backend change) purely to read
  // report.safety.
  async function renderSafetySummary() {
    const root = document.getElementById("safety-summary-root");
    if (!root) return;
    let evidence = await fetchJSON("/api/admin/evidence");
    // Same reasoning as renderStaticBadge() above: reaching this function
    // already proves this viewer is an admin, so a 401 here is a stale/
    // expired-token edge case, not the normal unauthenticated path — don't
    // paper over it with sample data.
    if (evidence && evidence.status === "auth_required") {
      root.appendChild(el("p", { class: "lead-takeaway" }, "Safety-recall comparison unavailable — your session may have expired; try refreshing."));
      return;
    }
    let usingMockEvidence = false;
    if (!evidence || evidence.status !== "ok") {
      evidence = mockReport;
      usingMockEvidence = true;
    }
    const recall = evidence.safety && evidence.safety.red_flag_top3_recall;
    if (!recall || !recall.actor_critic) {
      root.appendChild(el("p", { class: "lead-takeaway" }, "Safety-recall comparison not available yet."));
      return;
    }
    const ac = recall.actor_critic;
    const baseline = recall.baseline_mode_a || recall.baseline_mode_b;
    if (!baseline || ac.total == null) {
      root.appendChild(el("p", { class: "lead-takeaway" }, "Not enough red-flag cases in this eval set for a comparison."));
      return;
    }
    const caughtMore = ac.correct - baseline.correct;
    const sentence =
      caughtMore > 0
        ? `Actor-critic caught ${caughtMore} more high-acuity case${caughtMore === 1 ? "" : "s"} than the baseline, out of ${ac.total} test patients with a red-flag feature.`
        : caughtMore < 0
        ? `Actor-critic caught ${Math.abs(caughtMore)} fewer high-acuity case${Math.abs(caughtMore) === 1 ? "" : "s"} than the baseline, out of ${ac.total} test patients with a red-flag feature.`
        : `Actor-critic and the baseline caught the same number of high-acuity cases (${ac.correct} of ${ac.total} test patients with a red-flag feature).`;
    const p = el("p", { class: usingMockEvidence ? "lead-takeaway safety-summary-sample" : "lead-takeaway" }, sentence);
    if (usingMockEvidence) p.title = "Sample data — evidence report not yet generated.";
    root.appendChild(p);
  }

  // ---------------------------------------------------------------------
  // "How good is the reasoning?" — hero = mean_reasoning_quality, shown as
  // a /100 score rather than a raw 0-1 decimal or a bare percentage: this
  // is a critic-assigned quality rating, not a proportion-correct like
  // evidence.html's accuracy figures, and a "score out of 100" framing
  // keeps it visually distinct from that page's percentages.
  // ---------------------------------------------------------------------
  function renderReasoningSection(rq) {
    const heroVal = document.getElementById("reasoning-hero-value");
    const heroSuffix = document.getElementById("reasoning-hero-suffix");
    if (rq.mean_reasoning_quality != null) {
      heroVal.childNodes[0].textContent = String(Math.round(rq.mean_reasoning_quality * 100));
      heroSuffix.textContent = " / 100";
    }

    document.getElementById("reasoning-takeaway").textContent =
      `${rq.total_turns} turns critiqued across ${rq.total_sessions} sessions.`;

    const subStats = document.getElementById("reasoning-sub-stats");
    const rows = [
      ["Question quality", rq.mean_question_quality, rq.median_question_quality],
      ["Differential quality", rq.mean_differential_quality, rq.median_differential_quality],
    ];
    for (const [label, mean, median] of rows) {
      const value = mean == null ? "—" : String(Math.round(mean * 100));
      subStats.appendChild(
        el("div", { class: "sub-stat" }, [
          el("div", { class: "sub-stat-value" }, [
            value,
            median != null ? el("span", { class: "sub-stat-median" }, `(median ${Math.round(median * 100)})`) : null,
          ]),
          el("div", { class: "sub-stat-label" }, `${label} / 100`),
        ])
      );
    }
  }

  function renderCalibration(rq) {
    const container = document.getElementById("calibration-section");
    const dist = rq.confidence_calibration_distribution || {};
    const total = Object.values(dist).reduce((a, b) => a + b, 0);
    if (total === 0) {
      container.appendChild(el("p", { class: "muted" }, "No calibration data."));
      document.getElementById("calibration-takeaway").textContent = "No calibration data available.";
      return;
    }
    const wellCount = dist["well-calibrated"] || 0;
    document.getElementById("calibration-takeaway").textContent =
      `${fmtPct(wellCount / total, 0)} of critiqued turns were well-calibrated.`;

    const ul = el("ul", { class: "bar-list" });
    // Well-calibrated first and visually dominant (taller track, bolder
    // label) — it's the section's headline figure; over/under stay in the
    // same list but quieter, matching "vary by importance."
    const order = ["well-calibrated", "overconfident", "underconfident"];
    for (const label of order) {
      if (!(label in dist)) continue;
      const count = dist[label];
      const pct = count / total;
      const li = el("li", { class: label === "well-calibrated" ? "bar-dominant" : "" });
      li.appendChild(el("span", { class: "bar-label" }, `${label} — ${count} (${fmtPct(pct, 0)})`));
      const bar = el("div", { class: "bar-track" }, el("div", { class: `bar-fill bar-${label}`, style: `width:${(pct * 100).toFixed(1)}%` }));
      li.appendChild(bar);
      ul.appendChild(li);
    }
    container.appendChild(ul);
  }

  // ---------------------------------------------------------------------
  // "What does it get wrong?" — categorical, so no single hero number;
  // converted from a plain <ul> into a horizontal bar-list matching the
  // calibration bars' visual language, most-common category dominant.
  // ---------------------------------------------------------------------
  function renderWeaknesses(rq) {
    const container = document.getElementById("weakness-section");
    const counts = rq.weakness_category_counts || {};
    const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
    if (!entries.length) {
      container.appendChild(el("p", { class: "muted" }, "No weakness categories recorded."));
      document.getElementById("weakness-takeaway").textContent = "No weakness categories recorded.";
      return;
    }
    const total = entries.reduce((sum, [, c]) => sum + c, 0);
    const [topCat, topCount] = entries[0];
    document.getElementById("weakness-takeaway").textContent =
      `${topCat.replace(/_/g, " ")} was the most common weakness (${topCount} occurrence${topCount === 1 ? "" : "s"}).`;

    const ul = el("ul", { class: "bar-list" });
    entries.forEach(([cat, count], i) => {
      const pct = count / total;
      const li = el("li");
      li.appendChild(el("span", { class: "bar-label" }, `${cat.replace(/_/g, " ")} — ${count}`));
      const bar = el("div", { class: "bar-track" }, el("div", { class: `bar-fill ${i === 0 ? "bar-weakness-top" : "bar-weakness"}`, style: `width:${(pct * 100).toFixed(1)}%` }));
      li.appendChild(bar);
      ul.appendChild(li);
    });
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
    const takeaway = document.getElementById("cross-ref-takeaway");
    if (!rows.length) {
      document.getElementById("cross-ref-section").appendChild(
        el("p", { class: "muted" }, "No cross-referenced failures (or no critique data).")
      );
      takeaway.textContent = "No sessions combined low question quality with an incorrect diagnosis.";
      return;
    }
    takeaway.textContent = `${rows.length} session${rows.length === 1 ? "" : "s"} combined low question quality with an incorrect top diagnosis.`;
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
