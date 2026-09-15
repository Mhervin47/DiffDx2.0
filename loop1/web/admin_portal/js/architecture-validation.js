/*
 * architecture-validation.js — renders the MEDDxAgent (arXiv:2502.19175v2)
 * benchmark data into architecture-validation.html.
 *
 * Fully static: every number here is transcribed verbatim from the paper
 * (Tables 1, 2, 3, 8 and §5) — there is no /api/admin/* endpoint behind
 * this page, no live data, no PHI. Unlike every other admin_portal page,
 * nothing here needs fetchWithMockFallback()'s auth/sample-data handling,
 * because there is nothing to authenticate against or fall back from.
 *
 * Chart pattern follows evidence.js's renderPctBarChart precedent (hand-
 * rolled inline SVG, no charting library, all color from CSS custom
 * properties) but generalized to an arbitrary max value, since Avg Rank
 * (0-11) and GTPA@1 (0-1) need different scales.
 */
(function () {
  const { el } = AdminPortal;

  // ---------------------------------------------------------------------
  // Static content — transcribed verbatim from the prompt spec / paper.
  // ---------------------------------------------------------------------

  const KPI_TILES = [
    { value: "0.18 → 0.86", label: "GTPA@1, n=0 → iter=3 (n=15)" },
    { value: "+376%", label: "Relative GTPA@1 gain, n=0 → iter=3" },
    { value: "1.2–1.7x", label: "Fixed-pipeline speed vs. dynamic orchestration" },
    { value: "7.33 → 1.29", label: "Avg. rank of correct diagnosis, n=0 → iter=3" },
    { value: "3", label: "Datasets benchmarked (DDxPlus / iCraft-MD / RareBench)" },
    { value: "3", label: "LLMs benchmarked (GPT-4o / Llama3.1-70B / Llama3.1-8B)" },
  ];

  const CORRESPONDENCE = [
    ["History Taking Simulator", "Loop 1 — Doctor LLM (actor)", "Multi-turn interview, no full profile upfront"],
    ["Knowledge Retrieval Agent", "Loop 1 — FAISS + MMR exemplar retriever", "Retrieval-grounded reasoning per turn"],
    ["Diagnosis Strategy Agent", "Live differential + Loop 2 — Critic LLM", "Ranked, iteratively re-scored differential"],
    ["DDxDriver (fixed iteration mode)", "Loop 1 → Critic → Router, fixed sequence", "Fixed pipeline, not dynamic agent-choice orchestration"],
  ];

  const FULL_PROFILE = [
    ["Zero-shot", 0.69, 0.68, 0.46],
    ["Zero-shot + CoT", 0.71, 0.68, 0.47],
    ["Few-shot (dynamic, CoT)", 0.97, 0.64, 0.82],
  ];

  const COMPOSITION = [
    ["DDxPlus", "Respiratory", "1.3M", "49", "CC-BY"],
    ["iCraft-MD", "Skin", "140", "394", "MIT"],
    ["RareBench", "Rare disease", "2,185", "421", "Apache-2.0"],
  ];

  const FIXED_DYNAMIC = [
    ["Fixed (our pattern)", "Higher", "1.2–1.7x faster"],
    ["Dynamic agent-choice", "Lower", "Baseline"],
  ];

  // Format: [GTPA@1, AvgRank] for baselines, [GTPA@1, AvgRank, ΔProgress] for iterN.
  const DATA = {
    "GPT-4o": {
      "DDxPlus":    { KR_n0: [0.18,7.33], DS_n0: [0.27,6.01], KR_n5: [0.52,3.32], DS_n5: [0.72,2.14],
                      iter1: [0.74,1.91,0.00], iter2: [0.78,1.56,0.32], iter3: [0.86,1.29,0.32] },
      "iCraft-MD":  { KR_n0: [0.15,8.27], DS_n0: [0.18,7.87], KR_n5: [0.49,5.36], DS_n5: [0.40,5.55],
                      iter1: [0.52,4.93,0.00], iter2: [0.54,4.71,0.26], iter3: [0.54,4.80,0.17] },
      "RareBench":  { KR_n0: [0.07,9.07], DS_n0: [0.11,8.38], KR_n5: [0.40,5.27], DS_n5: [0.50,4.94],
                      iter1: [0.51,4.37,0.00], iter2: [0.56,4.10,0.13], iter3: [0.50,4.09,0.16] },
    },
    "Llama3.1-70B": {
      "DDxPlus":    { KR_n0: [0.19,7.58], DS_n0: [0.17,7.28], KR_n5: [0.39,5.03], DS_n5: [0.50,2.89],
                      iter1: [0.61,2.91,0.00], iter2: [0.71,2.20,0.41], iter3: [0.68,2.30,0.17] },
      "iCraft-MD":  { KR_n0: [0.13,8.19], DS_n0: [0.11,8.74], KR_n5: [0.34,6.86], DS_n5: [0.24,7.33],
                      iter1: [0.29,7.05,0.00], iter2: [0.37,6.26,0.07], iter3: [0.42,6.31,0.26] },
      "RareBench":  { KR_n0: [0.09,9.13], DS_n0: [0.20,6.81], KR_n5: [0.29,5.86], DS_n5: [0.23,5.77],
                      iter1: [0.39,5.05,0.00], iter2: [0.48,4.48,0.75], iter3: [0.48,4.30,0.44] },
    },
    "Llama3.1-8B": {
      "DDxPlus":    { KR_n0: [0.20,7.49], DS_n0: [0.16,8.45], KR_n5: [0.21,7.42], DS_n5: [0.23,5.77],
                      iter1: [0.34,5.25,0.00], iter2: [0.56,3.59,1.73], iter3: [0.58,3.10,1.23] },
      "iCraft-MD":  { KR_n0: [0.11,8.86], DS_n0: [0.03,10.37], KR_n5: [0.09,9.48], DS_n5: [0.03,10.08],
                      iter1: [0.11,9.38,0.00], iter2: [0.14,9.22,0.22], iter3: [0.12,9.07,0.17] },
      "RareBench":  { KR_n0: [0.11,8.58], DS_n0: [0.04,8.52], KR_n5: [0.04,9.69], DS_n5: [0.06,8.64],
                      iter1: [0.08,8.47,0.00], iter2: [0.09,8.11,0.44], iter3: [0.07,8.56,0.38] },
    },
  };

  const MODELS = ["GPT-4o", "Llama3.1-70B", "Llama3.1-8B"];
  const DATASETS = ["DDxPlus", "iCraft-MD", "RareBench"];
  const ROW_ORDER = ["KR_n0", "DS_n0", "KR_n5", "DS_n5", "iter1", "iter2", "iter3"];
  const ROW_LABELS = {
    KR_n0: "KR (n=0)", DS_n0: "DS (n=0)", KR_n5: "KR (n=5)", DS_n5: "DS (n=5)",
    iter1: "MEDDx (iter=1, n=5)", iter2: "MEDDx (iter=2, n=10)", iter3: "MEDDx (iter=3, n=15)",
  };

  const state = { model: "GPT-4o", dataset: "DDxPlus" };

  // ---------------------------------------------------------------------
  // Static section renderers
  // ---------------------------------------------------------------------

  function renderKpis() {
    const root = document.getElementById("kpi-grid");
    for (const t of KPI_TILES) {
      root.appendChild(
        el("div", { class: "stat-tile" }, [
          el("div", { class: "stat-tile-value" }, t.value),
          el("div", { class: "stat-tile-label" }, t.label),
        ])
      );
    }
  }

  function renderCorrespondence() {
    const tbody = document.getElementById("correspondence-tbody");
    for (const [meddx, diffdx, mechanism] of CORRESPONDENCE) {
      tbody.appendChild(el("tr", {}, [
        el("td", {}, meddx),
        el("td", {}, diffdx),
        el("td", {}, mechanism),
      ]));
    }
  }

  function renderFullProfile() {
    const tbody = document.getElementById("fullprofile-tbody");
    for (const [setting, ddxplus, icraft, rarebench] of FULL_PROFILE) {
      tbody.appendChild(el("tr", {}, [
        el("td", {}, setting),
        el("td", { class: "num mono" }, ddxplus.toFixed(2)),
        el("td", { class: "num mono" }, icraft.toFixed(2)),
        el("td", { class: "num mono" }, rarebench.toFixed(2)),
      ]));
    }
  }

  function renderComposition() {
    const tbody = document.getElementById("composition-tbody");
    for (const [dataset, domain, cases, diseases, license] of COMPOSITION) {
      tbody.appendChild(el("tr", {}, [
        el("td", {}, dataset),
        el("td", {}, domain),
        el("td", { class: "num mono" }, cases),
        el("td", { class: "num mono" }, diseases),
        el("td", {}, license),
      ]));
    }
  }

  function renderFixedDynamic() {
    const tbody = document.getElementById("fixeddynamic-tbody");
    for (const [mode, accuracy, speed] of FIXED_DYNAMIC) {
      tbody.appendChild(el("tr", {}, [
        el("td", {}, mode),
        el("td", {}, accuracy),
        el("td", { class: "mono" }, speed),
      ]));
    }
  }

  // ---------------------------------------------------------------------
  // Tabs — role="tablist"/"tab", arrow-key navigation, one tablist for
  // model and one for dataset (independent axes of the same toggle).
  // ---------------------------------------------------------------------

  function renderTabs(containerId, options, selected, onSelect) {
    const root = document.getElementById(containerId);
    root.innerHTML = "";
    const tabs = options.map((opt) =>
      el("button", {
        type: "button",
        role: "tab",
        "aria-selected": String(opt === selected),
        tabindex: opt === selected ? "0" : "-1",
      }, opt)
    );
    tabs.forEach((tab, i) => {
      tab.addEventListener("click", () => onSelect(options[i]));
      tab.addEventListener("keydown", (e) => {
        if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
        e.preventDefault();
        const dir = e.key === "ArrowRight" ? 1 : -1;
        const next = (i + dir + tabs.length) % tabs.length;
        tabs[next].focus();
        onSelect(options[next]);
      });
      root.appendChild(tab);
    });
  }

  // ---------------------------------------------------------------------
  // Bar chart — generalized version of evidence.js's renderPctBarChart:
  // takes an explicit max instead of assuming a 0-1 fraction, since Avg
  // Rank (0-11) and GTPA@1 (0-1) need different scales on the same page.
  // ---------------------------------------------------------------------

  function renderBarChart(root, entries, { max, decimals, ariaLabel }) {
    root.innerHTML = "";
    const svgNS = "http://www.w3.org/2000/svg";
    const width = 520, height = 170, pad = 22, barW = 44, gap = 12;
    const totalBarsW = entries.length * barW + (entries.length - 1) * gap;
    const startX = pad;

    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("viewBox", `0 0 ${Math.max(width, startX + totalBarsW + pad)} ${height}`);
    svg.setAttribute("class", "av-chart");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", ariaLabel);

    const axisY = height - pad - 12;
    const axis = document.createElementNS(svgNS, "line");
    axis.setAttribute("x1", startX - 6); axis.setAttribute("x2", startX + totalBarsW + 6);
    axis.setAttribute("y1", axisY); axis.setAttribute("y2", axisY);
    axis.setAttribute("class", "baseline");
    svg.appendChild(axis);

    entries.forEach((entry, i) => {
      const x = startX + i * (barW + gap);
      const maxH = axisY - 16;
      const frac = entry.value == null ? 0 : Math.max(0, Math.min(1, entry.value / max));
      const barH = entry.value == null ? 0 : Math.max(2, frac * maxH);
      const y = axisY - barH;

      const rect = document.createElementNS(svgNS, "rect");
      rect.setAttribute("x", x); rect.setAttribute("y", y);
      rect.setAttribute("width", barW); rect.setAttribute("height", Math.max(barH, 0.1));
      rect.setAttribute("rx", 2);
      rect.setAttribute("class", "bar");
      rect.style.fill = entry.isIter ? "var(--teal-primary)" : "var(--text-dim)";
      rect.setAttribute("opacity", entry.value == null ? 0.2 : (entry.isIter ? 1 : 0.6));
      const title = document.createElementNS(svgNS, "title");
      title.textContent = `${entry.label}: ${entry.value == null ? "no data" : entry.value.toFixed(decimals)}`;
      rect.appendChild(title);
      svg.appendChild(rect);

      if (entry.value != null) {
        const label = document.createElementNS(svgNS, "text");
        label.setAttribute("x", x + barW / 2);
        label.setAttribute("y", y - 4);
        label.setAttribute("text-anchor", "middle");
        label.setAttribute("class", "bar-label");
        label.textContent = entry.value.toFixed(decimals);
        svg.appendChild(label);
      }

      const axisLabel = document.createElementNS(svgNS, "text");
      axisLabel.setAttribute("x", x + barW / 2);
      axisLabel.setAttribute("y", axisY + 14);
      axisLabel.setAttribute("text-anchor", "middle");
      axisLabel.setAttribute("class", "axis-label");
      axisLabel.textContent = entry.shortLabel;
      svg.appendChild(axisLabel);
    });

    root.appendChild(svg);
  }

  // ---------------------------------------------------------------------
  // Explorer — table + two charts for the current model/dataset selection.
  // ---------------------------------------------------------------------

  function renderExplorer() {
    const rows = DATA[state.model][state.dataset];

    const tbody = document.getElementById("explorer-tbody");
    tbody.innerHTML = "";
    for (const key of ROW_ORDER) {
      const v = rows[key];
      const [gtpa, rank, progress] = v;
      tbody.appendChild(el("tr", {}, [
        el("td", {}, ROW_LABELS[key]),
        el("td", { class: "num mono" }, gtpa.toFixed(2)),
        el("td", { class: "num mono" }, rank.toFixed(2)),
        el("td", { class: "num mono" }, progress == null ? "—" : `${progress >= 0 ? "+" : ""}${progress.toFixed(2)}`),
      ]));
    }

    const gtpaEntries = ROW_ORDER.map((key) => ({
      label: ROW_LABELS[key],
      shortLabel: key.replace("_n", " n=").replace("iter", "it"),
      value: rows[key][0],
      isIter: key.startsWith("iter"),
    }));
    const rankEntries = ROW_ORDER.map((key) => ({
      label: ROW_LABELS[key],
      shortLabel: key.replace("_n", " n=").replace("iter", "it"),
      value: rows[key][1],
      isIter: key.startsWith("iter"),
    }));

    renderBarChart(document.getElementById("chart-gtpa"), gtpaEntries, {
      max: 1, decimals: 2,
      ariaLabel: `GTPA@1 by setting, ${state.model}, ${state.dataset}`,
    });
    renderBarChart(document.getElementById("chart-rank"), rankEntries, {
      max: 11, decimals: 2,
      ariaLabel: `Average rank by setting, ${state.model}, ${state.dataset}`,
    });
  }

  function setModel(m) { state.model = m; renderTabs("model-tabs", MODELS, state.model, setModel); renderExplorer(); }
  function setDataset(d) { state.dataset = d; renderTabs("dataset-tabs", DATASETS, state.dataset, setDataset); renderExplorer(); }

  renderKpis();
  renderCorrespondence();
  renderFullProfile();
  renderComposition();
  renderFixedDynamic();
  renderTabs("model-tabs", MODELS, state.model, setModel);
  renderTabs("dataset-tabs", DATASETS, state.dataset, setDataset);
  renderExplorer();
})();
