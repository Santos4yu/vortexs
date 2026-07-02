/* =========================================================
   Vortex Prop Research — client-side renderer
   No backend, no database, no odds API. Reads a single JSON
   file and lets you search + render one full prop breakdown
   at a time (mirrors the /prediction command output).
   Saved props persist locally via localStorage only.
   ========================================================= */

/**
 * Single source of truth for where prop research data comes from.
 * Swap this one line later (e.g. a KV store URL or API endpoint)
 * and nothing else in this file needs to change, as long as the
 * response shape matches { props: [...] }.
 */
const DATA_SOURCE = "predictions.json";

const SAVED_KEY = "vortex_saved_prop_ids";
const AVATAR_HUES = [168, 262, 24, 200, 330, 48, 140, 300];

const state = {
  props: [],
  activeIndex: -1,
  savedIds: loadSaved(),
  parlaySelection: new Set(),
  currentTab: "research",
};

const els = {};

init();

async function init() {
  cacheEls();

  try {
    const res = await fetch(DATA_SOURCE, { cache: "no-store" });
    if (!res.ok) throw new Error(`Failed to load ${DATA_SOURCE}: ${res.status}`);
    const data = await res.json();
    state.props = data.props || [];
  } catch (err) {
    console.error(err);
    els.emptyState.textContent = `Couldn't load predictions.json — ${err.message}`;
    return;
  }

  wireTabs();
  wireSearch();
  wireLinePicker();
  renderBrowseChips();
  wireSavedToolbar();
  renderSavedGrid();
  updateSavedCount();
  updateParlayBar();
}

function cacheEls() {
  els.searchInput = document.getElementById("search-input");
  els.searchResults = document.getElementById("search-results");
  els.reportWrap = document.getElementById("report-wrap");
  els.emptyState = document.getElementById("empty-state");
  els.browseChips = document.getElementById("browse-chips");

  els.playerProfile = document.getElementById("player-profile");
  els.profileAvatar = document.getElementById("profile-avatar");
  els.profileName = document.getElementById("profile-name");
  els.profileSub = document.getElementById("profile-sub");
  els.profileStats = document.getElementById("profile-stats");

  els.linePicker = document.getElementById("line-picker");
  els.sideToggle = document.getElementById("side-toggle");
  els.lineNumber = document.getElementById("line-number");
  els.lineSlider = document.getElementById("line-slider");
  els.lineStepDown = document.getElementById("line-step-down");
  els.lineStepUp = document.getElementById("line-step-up");
  els.lineNoData = document.getElementById("line-no-data");

  els.tabs = document.getElementById("tabs");
  els.tabIndicator = document.getElementById("tab-indicator");
  els.panelResearch = document.getElementById("panel-research");
  els.panelSaved = document.getElementById("panel-saved");
  els.savedCount = document.getElementById("saved-count");

  els.savedGrid = document.getElementById("saved-grid");
  els.savedEmpty = document.getElementById("saved-empty");
  els.clearSavedBtn = document.getElementById("clear-saved-btn");

  els.parlayBar = document.getElementById("parlay-bar");
  els.parlaySelectedCount = document.getElementById("parlay-selected-count");
  els.parlayClearBtn = document.getElementById("parlay-clear-btn");
  els.parlayCompareBtn = document.getElementById("parlay-compare-btn");
  els.parlayView = document.getElementById("parlay-view");

  els.toastStack = document.getElementById("toast-stack");
}

/* ---------- Tabs ---------- */

function wireTabs() {
  els.tabs.querySelectorAll(".tab-btn").forEach((btn, i) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab, btn));
  });
  // position indicator under the initially-active tab once layout settles
  requestAnimationFrame(() => moveIndicator(els.tabs.querySelector(".tab-btn.active")));
  window.addEventListener("resize", () => moveIndicator(els.tabs.querySelector(".tab-btn.active")));
}

function switchTab(tab, btn) {
  state.currentTab = tab;
  els.tabs.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b === btn));
  moveIndicator(btn);

  els.panelResearch.hidden = tab !== "research";
  els.panelSaved.hidden = tab !== "saved";
  els.parlayBar.hidden = tab !== "saved" || state.parlaySelection.size === 0;

  if (tab === "saved") renderSavedGrid();
}

function moveIndicator(btn) {
  if (!btn) return;
  const tabsRect = els.tabs.getBoundingClientRect();
  const btnRect = btn.getBoundingClientRect();
  els.tabIndicator.style.width = `${btnRect.width}px`;
  els.tabIndicator.style.transform = `translateX(${btnRect.left - tabsRect.left - 4}px)`;
}

/* ---------- Avatars ---------- */

function hashString(str) {
  let hash = 0;
  for (let i = 0; i < str.length; i++) {
    hash = (hash << 5) - hash + str.charCodeAt(i);
    hash |= 0;
  }
  return Math.abs(hash);
}

function initialsFor(name) {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0].toUpperCase())
    .join("");
}

function avatarHtml(playerOrProp, size = "") {
  const player = typeof playerOrProp === "string" ? playerOrProp : playerOrProp.player;
  const headshot = typeof playerOrProp === "object" ? playerOrProp.headshot : null;
  const hash = hashString(player);
  const hue = AVATAR_HUES[hash % AVATAR_HUES.length];
  const hue2 = (hue + 40) % 360;
  const initials = initialsFor(player);
  const sizeClass = size ? ` avatar-${size}` : "";
  // Initials render underneath; if the photo loads it covers them, if it
  // fails onerror removes the img and the initials fallback shows through.
  const img = headshot
    ? `<img src="${escapeHtml(headshot)}" alt="" loading="lazy" onerror="this.remove()">`
    : "";
  return `<div class="avatar${sizeClass}" style="background:linear-gradient(135deg, hsl(${hue} 80% 55%), hsl(${hue2} 80% 45%))">${escapeHtml(initials)}${img}</div>`;
}

/* ---------- Toasts ---------- */

function showToast(message, variant = "default") {
  const toast = document.createElement("div");
  toast.className = `toast${variant === "warn" ? " toast-warn" : ""}`;
  toast.textContent = message;
  els.toastStack.appendChild(toast);
  setTimeout(() => {
    toast.classList.add("leaving");
    toast.addEventListener("animationend", () => toast.remove(), { once: true });
  }, 2200);
}

/* ---------- Saved props (localStorage) ---------- */

function loadSaved() {
  try {
    const raw = localStorage.getItem(SAVED_KEY);
    return new Set(raw ? JSON.parse(raw) : []);
  } catch {
    return new Set();
  }
}

function persistSaved() {
  localStorage.setItem(SAVED_KEY, JSON.stringify([...state.savedIds]));
}

function isSaved(id) {
  return state.savedIds.has(id);
}

function toggleSave(prop, btnEl) {
  if (isSaved(prop.id)) {
    state.savedIds.delete(prop.id);
    showToast(`Removed ${prop.player} from saved props`, "warn");
  } else {
    state.savedIds.add(prop.id);
    showToast(`Saved ${prop.player} — ${prop.side} ${prop.line} ${prop.betType}`);
  }
  persistSaved();
  updateSavedCount();
  if (btnEl) {
    syncSaveButton(btnEl, prop.id);
    btnEl.classList.remove("pop");
    void btnEl.offsetWidth; // restart animation
    btnEl.classList.add("pop");
  }
  if (state.currentTab === "saved") renderSavedGrid();
}

function syncSaveButton(btnEl, id) {
  const saved = isSaved(id);
  btnEl.classList.toggle("saved", saved);
  btnEl.querySelector(".save-btn-label").textContent = saved ? "Saved" : "Save";
}

function updateSavedCount() {
  els.savedCount.textContent = state.savedIds.size;
}

/* ---------- Search ---------- */

function wireSearch() {
  els.searchInput.addEventListener("input", onSearchInput);
  els.searchInput.addEventListener("focus", onSearchInput);
  els.searchInput.addEventListener("keydown", onSearchKeydown);
  // Capture phase so the dropdown always closes before any other click
  // handler runs — prevents it lingering over content below it.
  document.addEventListener(
    "click",
    (e) => {
      if (!e.target.closest(".search-results") && !e.target.closest("#search-input")) {
        hideResults();
      }
    },
    true
  );
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && document.activeElement !== els.searchInput) {
      e.preventDefault();
      els.searchInput.focus();
    }
  });
}

/** Group all props by player so the dropdown/chips show one row per player. */
function groupByPlayer(props) {
  const map = new Map();
  props.forEach((p) => {
    if (!map.has(p.player)) map.set(p.player, []);
    map.get(p.player).push(p);
  });
  return [...map.entries()];
}

function matchPlayers(query) {
  const q = query.trim().toLowerCase();
  const groups = groupByPlayer(state.props);
  if (!q) return groups.slice(0, 8);
  return groups
    .filter(([player, props]) =>
      [player, props[0].team, props[0].sport].filter(Boolean).some((f) => f.toLowerCase().includes(q))
    )
    .slice(0, 8);
}

function onSearchInput() {
  renderResults(matchPlayers(els.searchInput.value));
}

function onSearchKeydown(e) {
  const items = els.searchResults.querySelectorAll(".search-result-item");
  if (!items.length) return;

  if (e.key === "ArrowDown") {
    e.preventDefault();
    state.activeIndex = Math.min(state.activeIndex + 1, items.length - 1);
    highlightActive(items);
  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    state.activeIndex = Math.max(state.activeIndex - 1, 0);
    highlightActive(items);
  } else if (e.key === "Enter") {
    e.preventDefault();
    const idx = state.activeIndex >= 0 ? state.activeIndex : 0;
    items[idx]?.dispatchEvent(new Event("mousedown"));
  } else if (e.key === "Escape") {
    hideResults();
    els.searchInput.blur();
  }
}

function highlightActive(items) {
  items.forEach((item, i) => item.classList.toggle("active", i === state.activeIndex));
}

function renderResults(groups) {
  state.activeIndex = -1;
  els.searchResults.innerHTML = "";

  if (groups.length === 0) {
    hideResults();
    return;
  }

  groups.forEach(([player, props], i) => {
    const first = props[0];
    const li = document.createElement("li");
    li.className = "search-result-item";
    li.style.animationDelay = `${i * 30}ms`;
    li.innerHTML = `
      ${avatarHtml(first, "sm")}
      <span class="sr-main">
        <span class="sr-player">${escapeHtml(player)}${first.team ? " (" + escapeHtml(first.team) + ")" : ""}</span>
        <span class="sr-pick">${props.length} prop${props.length > 1 ? "s" : ""} available</span>
      </span>
      <span class="sr-sport">${escapeHtml(first.sport)}</span>
    `;
    li.addEventListener("mousedown", () => {
      els.searchInput.value = "";
      hideResults();
      selectPlayer(player);
    });
    els.searchResults.appendChild(li);
  });

  els.searchResults.hidden = false;
}

function hideResults() {
  els.searchResults.hidden = true;
}

function renderBrowseChips() {
  els.browseChips.innerHTML = "";
  groupByPlayer(state.props).forEach(([player]) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "browse-chip";
    chip.textContent = player;
    chip.addEventListener("click", () => selectPlayer(player));
    els.browseChips.appendChild(chip);
  });
}

/* ---------- Player profile: stat buttons + slide/type-in line picker ---------- */

const cmd = { player: null, stat: null, line: null, side: null };

function wireLinePicker() {
  els.sideToggle.querySelectorAll(".side-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.disabled) return;
      els.sideToggle.querySelectorAll(".side-btn").forEach((b) => b.classList.toggle("active", b === btn));
      cmd.side = btn.dataset.side;
      applyLineSelection();
    });
  });

  els.lineSlider.addEventListener("input", () => {
    setLineValue(Number(els.lineSlider.value));
  });
  els.lineNumber.addEventListener("change", () => {
    setLineValue(Number(els.lineNumber.value));
  });
  els.lineStepDown.addEventListener("click", () => setLineValue(cmd.line - 0.5));
  els.lineStepUp.addEventListener("click", () => setLineValue(cmd.line + 0.5));
}

function selectPlayer(player) {
  hideResults();
  cmd.player = player;
  cmd.stat = null;
  cmd.line = null;
  cmd.side = null;

  const first = propsForPlayer()[0];
  els.profileAvatar.innerHTML = avatarHtml(first, "lg");
  els.profileName.textContent = `${player}${first.team ? " (" + first.team + ")" : ""}`;
  els.profileSub.textContent = `${first.sport} · tap a stat to dial in a line`;

  const stats = [...new Set(propsForPlayer().map((p) => p.betType))];
  els.profileStats.innerHTML = "";
  stats.forEach((stat, i) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "profile-stat-btn";
    btn.style.animationDelay = `${i * 50}ms`;
    btn.textContent = stat;
    btn.addEventListener("click", () => selectStat(stat, btn));
    els.profileStats.appendChild(btn);
  });

  els.linePicker.hidden = true;
  els.playerProfile.hidden = false;
  clearReport();

  if (stats.length === 1) {
    selectStat(stats[0], els.profileStats.querySelector(".profile-stat-btn"));
  }

  els.playerProfile.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function selectStat(stat, btnEl) {
  cmd.stat = stat;
  els.profileStats.querySelectorAll(".profile-stat-btn").forEach((b) => b.classList.toggle("active", b === btnEl));

  const matches = propsForPlayer().filter((p) => p.betType === stat);
  const lines = matches.map((p) => p.line);
  const defaultProp = matches[0];
  const availableSides = new Set(matches.map((p) => p.side));

  // Disable a side entirely when this stat has zero data for it — avoids
  // sending someone into a "no data" dead end they couldn't have predicted.
  els.sideToggle.querySelectorAll(".side-btn").forEach((b) => {
    const hasData = availableSides.has(b.dataset.side);
    b.disabled = !hasData;
    b.title = hasData ? "" : `No ${b.dataset.side} data for ${stat}`;
  });

  // Slider spans a little past the known lines so there's room to explore.
  const min = Math.max(0, Math.min(...lines) - 1.5);
  const max = Math.max(...lines) + 1.5;
  els.lineSlider.min = String(min);
  els.lineSlider.max = String(max);
  els.lineNumber.min = String(min);
  els.lineNumber.max = String(max);

  cmd.side = defaultProp.side;
  els.sideToggle.querySelectorAll(".side-btn").forEach((b) => b.classList.toggle("active", b.dataset.side === cmd.side));

  els.linePicker.hidden = false;
  setLineValue(defaultProp.line);
}

function setLineValue(value) {
  const min = Number(els.lineSlider.min);
  const max = Number(els.lineSlider.max);
  const snapped = Math.round(Math.max(min, Math.min(max, value)) * 2) / 2; // snap to 0.5
  cmd.line = snapped;

  els.lineSlider.value = String(snapped);
  els.lineNumber.value = String(snapped);
  const pct = max > min ? ((snapped - min) / (max - min)) * 100 : 0;
  els.lineSlider.style.setProperty("--fill", `${pct}%`);

  applyLineSelection();
}

function applyLineSelection() {
  const match = propsForPlayer().find(
    (p) => p.betType === cmd.stat && Math.abs(p.line - cmd.line) < 0.01 && p.side === cmd.side
  );

  if (match) {
    els.lineNoData.hidden = true;
    renderReport(match);
    return;
  }

  clearReport();
  const nearest = propsForPlayer()
    .filter((p) => p.betType === cmd.stat)
    .sort((a, b) => Math.abs(a.line - cmd.line) - Math.abs(b.line - cmd.line))[0];

  els.lineNoData.hidden = false;
  els.lineNoData.innerHTML = `No computed analysis for ${escapeHtml(cmd.side)} ${cmd.line} yet.`;
  if (nearest) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = `Jump to ${nearest.side} ${nearest.line} (Score ${nearest.score})`;
    btn.addEventListener("click", () => {
      cmd.side = nearest.side;
      els.sideToggle.querySelectorAll(".side-btn").forEach((b) => b.classList.toggle("active", b.dataset.side === cmd.side));
      setLineValue(nearest.line);
    });
    els.lineNoData.appendChild(btn);
  }
}

function propsForPlayer() {
  return state.props.filter((p) => p.player === cmd.player);
}

function clearReport() {
  els.reportWrap.querySelector(".report")?.remove();
  els.emptyState.hidden = false;
}

/* ---------- Report rendering (Research tab) ---------- */

function renderReport(p) {
  hideResults();
  els.emptyState.hidden = true;
  els.reportWrap.querySelector(".report")?.remove();

  const node = buildReportNode(p);
  els.reportWrap.appendChild(node);

  const saveBtn = node.querySelector(".save-btn");
  syncSaveButton(saveBtn, p.id);
  saveBtn.addEventListener("click", () => toggleSave(p, saveBtn));

  const revealBlocks = node.querySelectorAll("[data-reveal]");
  revealBlocks.forEach((block, i) => {
    setTimeout(() => block.classList.add("in"), 120 + i * 90);
  });

  requestAnimationFrame(() => {
    countUpScoreNum(node, p.score);
    fillHitRateBars(node, p.hitRates || {});
    fillSparkline(node, p.last5 || []);
  });

  if (state.currentTab === "research") {
    node.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

/**
 * Rebuilds the player profile + line picker for an exact prop, used when
 * reopening a saved prop or a parlay leg from the Saved tab.
 */
function openExactProp(p) {
  selectPlayer(p.player);
  const statBtn = [...els.profileStats.querySelectorAll(".profile-stat-btn")].find(
    (b) => b.textContent === p.betType
  );
  selectStat(p.betType, statBtn);
  cmd.side = p.side;
  els.sideToggle.querySelectorAll(".side-btn").forEach((b) => b.classList.toggle("active", b.dataset.side === p.side));
  setLineValue(p.line);
}

function buildReportNode(p) {
  const template = document.getElementById("report-template");
  const node = template.content.firstElementChild.cloneNode(true);

  node.querySelector(".rt-avatar-slot").innerHTML = avatarHtml(p, "lg");

  fillHeader(node, p);
  fillWhyItHits(node, p);
  fillSplitFactor(node, p);
  fillMatchup(node, p);
  fillNarrative(node, p);
  fillPerformance(node, p);
  fillVsMatchup(node, p);
  fillEnvRisk(node, p);
  fillModelConfirm(node, p);

  return node;
}

function fillHeader(node, p) {
  node.querySelector(".rt-sport").textContent = p.sport || "";
  node.querySelector(".rt-title").textContent = `${p.player} — ${p.side} ${p.line} ${p.betType}`;
  node.querySelector(".rt-sub").textContent =
    `${p.team ? p.team + " · " : ""}${p.location || ""}${p.estHitRate != null ? " · est. " + p.estHitRate + "% hit rate" : ""}`;

  node.querySelector(".score-tier-icon").textContent = p.tierIcon || "";

  node.querySelector(".verdict-pill").textContent = p.verdict || "";
  node.querySelector(".verdict-detail").textContent = p.verdictDetail || "";

  node.querySelector(".unit-value").textContent = p.unitSize || "—";
}

function fillWhyItHits(node, p) {
  const list = node.querySelector(".why-list");
  (p.whyItHits || []).forEach((line) => {
    const li = document.createElement("li");
    li.textContent = line;
    list.appendChild(li);
  });
}

function fillSplitFactor(node, p) {
  const split = p.split || {};
  node.querySelector(".road-avg").textContent = split.roadAvg != null ? `${split.roadAvg}` : "—";
  node.querySelector(".road-note").textContent = split.roadOverRate != null ? `${split.roadOverRate}% over rate` : "";
  node.querySelector(".home-avg").textContent = split.homeAvg != null ? `${split.homeAvg}` : "—";
  node.querySelector(".home-note").textContent = split.homeOverRate != null ? `${split.homeOverRate}% over rate` : "";
  node.querySelector(".split-callout").textContent = split.callout || "";
  node.querySelector(".volume-note").textContent = split.volume || "";
}

function fillMatchup(node, p) {
  const m = p.matchup || {};
  node.querySelector(".matchup-opp").textContent = m.opponent ? `Opponent: ${m.opponent}` : "";
  node.querySelector(".matchup-pitcher").textContent = m.pitcher || "";

  const bvpEl = node.querySelector(".matchup-bvp");
  if (m.bvp) {
    bvpEl.innerHTML = `<b>BvP</b> ${escapeHtml(m.bvp)}${m.bvpNote ? "<br>" + escapeHtml(m.bvpNote) : ""}`;
    bvpEl.hidden = false;
  } else {
    bvpEl.hidden = true;
  }

  node.querySelector(".matchup-leash").textContent = m.leash || "";
  node.querySelector(".matchup-handedness").textContent = m.handedness || "";
}

function fillNarrative(node, p) {
  node.querySelector(".narrative-text").textContent = p.narrative || "";
}

function fillPerformance(node, p) {
  node.querySelector(".perf-season").textContent = p.seasonLine || "";
}

function fillVsMatchup(node, p) {
  const vs = p.vsMatchup || {};

  const h2hEl = node.querySelector(".vs-h2h");
  if (vs.h2h) {
    h2hEl.innerHTML = `<b>H2H</b> ${escapeHtml(vs.h2h)}${vs.h2hNote ? "<br>" + escapeHtml(vs.h2hNote) : ""}`;
    h2hEl.hidden = false;
  } else {
    h2hEl.hidden = true;
  }

  node.querySelector(".vs-career").textContent = vs.career || "";
  node.querySelector(".vs-season").textContent = vs.season || "";
}

function fillEnvRisk(node, p) {
  node.querySelector(".env-text").textContent = p.environment || "";
  node.querySelector(".env-wind").textContent = p.wind || "";
  const list = node.querySelector(".risk-list");
  (p.risk || []).forEach((line) => {
    const li = document.createElement("li");
    li.textContent = line;
    list.appendChild(li);
  });
}

function fillModelConfirm(node, p) {
  node.querySelector(".model-confirm-pill").textContent = "Model Confirms";
  node.querySelector(".model-confirm-detail").textContent = p.modelConfirm || "";
  node.querySelector(".report-timestamp").textContent = formatDate(p.date);
}

/* ---------- Animated fills ---------- */

function countUpScoreNum(node, score) {
  const el = node.querySelector(".score-num");
  const target = Number(score) || 0;
  const start = performance.now();
  const duration = 900;
  function tick(now) {
    const t = Math.min(1, (now - start) / duration);
    const eased = 1 - Math.pow(1 - t, 3);
    el.textContent = Math.round(target * eased);
    if (t < 1) requestAnimationFrame(tick);
    else el.textContent = target;
  }
  requestAnimationFrame(tick);
}

function fillHitRateBars(node, rates) {
  const order = ["l5", "l10", "l20"];
  const rows = node.querySelectorAll(".hr-row");
  rows.forEach((row, i) => {
    const key = order[i];
    const val = Number(rates[key]) || 0;
    const fill = row.querySelector(".hr-fill");
    const pctLabel = row.querySelector(".hr-pct");
    pctLabel.textContent = `${val}%`;
    requestAnimationFrame(() => {
      fill.style.width = `${val}%`;
    });
  });
}

function fillSparkline(node, values) {
  const holder = node.querySelector("#sparkline-holder");
  holder.innerHTML = "";
  if (!values.length) return;

  const max = Math.max(...values, 1);
  values.forEach((v) => {
    const bar = document.createElement("div");
    bar.className = "spark-bar";
    const valEl = document.createElement("span");
    valEl.className = "spark-val";
    valEl.textContent = v;
    bar.appendChild(valEl);
    holder.appendChild(bar);
    const pct = Math.max(6, (v / max) * 100);
    requestAnimationFrame(() => {
      bar.style.height = `${pct}%`;
    });
  });
}

/* ---------- Saved tab ---------- */

function wireSavedToolbar() {
  els.clearSavedBtn.addEventListener("click", () => {
    if (state.savedIds.size === 0) return;
    if (!confirm("Clear all saved props? This can't be undone.")) return;
    state.savedIds.clear();
    state.parlaySelection.clear();
    persistSaved();
    updateSavedCount();
    renderSavedGrid();
    updateParlayBar();
    hideParlayView();
    showToast("Cleared all saved props", "warn");
  });

  els.parlayClearBtn.addEventListener("click", () => {
    state.parlaySelection.clear();
    renderSavedGrid();
    updateParlayBar();
    hideParlayView();
  });

  els.parlayCompareBtn.addEventListener("click", () => {
    if (state.parlaySelection.size < 2) return;
    renderParlayView();
  });
}

function getSavedProps() {
  return state.props.filter((p) => state.savedIds.has(p.id));
}

function renderSavedGrid() {
  const saved = getSavedProps();
  els.savedGrid.innerHTML = "";
  els.savedEmpty.hidden = saved.length > 0;
  els.clearSavedBtn.style.visibility = saved.length ? "visible" : "hidden";

  const template = document.getElementById("saved-card-template");

  saved.forEach((p, i) => {
    const node = template.content.firstElementChild.cloneNode(true);
    node.style.animationDelay = `${i * 45}ms`;
    node.classList.toggle("selected", state.parlaySelection.has(p.id));

    node.querySelector(".avatar-slot").innerHTML = avatarHtml(p);
    node.querySelector(".saved-player").textContent = `${p.player}${p.team ? " (" + p.team + ")" : ""}`;
    node.querySelector(".saved-pick").textContent = `${p.side} ${p.line} ${p.betType}`;
    node.querySelector(".saved-score").textContent = `${p.tierIcon || ""} ${p.score ?? "—"}`;
    node.querySelector(".saved-sport-tag").textContent = p.sport || "";

    const checkbox = node.querySelector(".saved-checkbox");
    checkbox.checked = state.parlaySelection.has(p.id);
    checkbox.addEventListener("click", (e) => e.stopPropagation());
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) state.parlaySelection.add(p.id);
      else state.parlaySelection.delete(p.id);
      node.classList.toggle("selected", checkbox.checked);
      updateParlayBar();
    });

    const removeBtn = node.querySelector(".saved-remove");
    removeBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      node.classList.add("removing");
      state.savedIds.delete(p.id);
      state.parlaySelection.delete(p.id);
      persistSaved();
      updateSavedCount();
      updateParlayBar();
      node.addEventListener(
        "animationend",
        () => {
          node.remove();
          if (getSavedProps().length === 0) els.savedEmpty.hidden = false;
        },
        { once: true }
      );
    });

    node.addEventListener("click", () => {
      switchTab("research", document.querySelector('.tab-btn[data-tab="research"]'));
      openExactProp(p);
    });

    els.savedGrid.appendChild(node);
  });
}

function updateParlayBar() {
  const count = state.parlaySelection.size;
  els.parlayBar.hidden = state.currentTab !== "saved" || count === 0;
  els.parlaySelectedCount.textContent = `${count} selected`;
  els.parlayCompareBtn.disabled = count < 2;
  if (count < 2) hideParlayView();
}

function hideParlayView() {
  els.parlayView.hidden = true;
  els.parlayView.innerHTML = "";
}

function renderParlayView() {
  const legs = state.props.filter((p) => state.parlaySelection.has(p.id));
  if (legs.length < 2) return;

  const combinedHitRate = legs.reduce((acc, p) => acc * ((Number(p.estHitRate) || 0) / 100), 1) * 100;
  const avgScore = legs.reduce((acc, p) => acc + (Number(p.score) || 0), 0) / legs.length;

  els.parlayView.innerHTML = `
    <div class="parlay-summary">
      <span class="parlay-summary-value" id="parlay-combined-value">0%</span>
      <span class="parlay-summary-label">Estimated combined hit rate · ${legs.length}-leg parlay</span>
      <div class="parlay-summary-sub">Avg individual score: ${avgScore.toFixed(1)} · Legs are treated as independent — actual correlation may differ. Expand a leg for its matchup detail.</div>
    </div>
  `;

  legs.forEach((p, i) => {
    const m = p.matchup || {};
    const matchupLines = [
      m.opponent ? `<b>vs ${escapeHtml(m.opponent)}</b>` : "",
      m.pitcher ? escapeHtml(m.pitcher) : "",
      m.bvp ? `BvP: ${escapeHtml(m.bvp)}` : "",
      m.handedness ? escapeHtml(m.handedness) : "",
      p.environment ? escapeHtml(p.environment) : "",
    ].filter(Boolean);

    const leg = document.createElement("div");
    leg.className = "parlay-leg";
    leg.style.animationDelay = `${120 + i * 90}ms`;
    leg.innerHTML = `
      <div class="parlay-leg-head">
        ${avatarHtml(p, "sm")}
        <div class="parlay-leg-text">
          <span class="parlay-leg-player">${escapeHtml(p.player)}</span>
          <span class="parlay-leg-pick">${escapeHtml(p.side)} ${escapeHtml(String(p.line))} ${escapeHtml(p.betType)} · est. ${p.estHitRate ?? "—"}%</span>
        </div>
        <span class="parlay-leg-score">${p.tierIcon || ""} ${p.score ?? "—"}</span>
        <svg class="parlay-leg-caret" width="10" height="6" viewBox="0 0 10 6"><path d="M1 1l4 4 4-4" stroke="currentColor" stroke-width="1.5" fill="none"/></svg>
      </div>
      <div class="parlay-leg-detail">
        <div class="parlay-leg-detail-inner">
          ${matchupLines.length ? `<p class="parlay-leg-matchup">${matchupLines.join("<br>")}</p>` : ""}
          <ul class="bullet-list">
            ${(p.whyItHits || []).slice(0, 4).map((line) => `<li>${escapeHtml(line)}</li>`).join("")}
          </ul>
        </div>
      </div>
    `;
    leg.querySelector(".parlay-leg-head").addEventListener("click", () => {
      leg.classList.toggle("expanded");
    });
    els.parlayView.appendChild(leg);
  });

  els.parlayView.hidden = false;
  els.parlayView.scrollIntoView({ behavior: "smooth", block: "nearest" });

  requestAnimationFrame(() => countUpEl("parlay-combined-value", combinedHitRate, { decimals: 1, suffix: "%" }));
}

function countUpEl(id, target, { decimals = 0, duration = 1000, suffix = "" } = {}) {
  const el = document.getElementById(id);
  if (!el) return;
  const start = performance.now();
  function tick(now) {
    const t = Math.min(1, (now - start) / duration);
    const eased = 1 - Math.pow(1 - t, 3);
    const value = target * eased;
    el.textContent = `${value.toFixed(decimals)}${suffix}`;
    if (t < 1) requestAnimationFrame(tick);
    else el.textContent = `${target.toFixed(decimals)}${suffix}`;
  }
  requestAnimationFrame(tick);
}

/* ---------- Utils ---------- */

function formatDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { month: "long", day: "numeric", year: "numeric" });
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = String(str ?? "");
  return div.innerHTML;
}
