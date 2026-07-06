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

/**
 * Live-compute endpoint — used whenever a searched player/stat/line/side
 * isn't already in the static DATA_SOURCE. Same one-line-swap contract:
 * change this and nothing else needs to change, as long as the response
 * matches a single prop object (same shape as one entry in props[]).
 */
const API_SOURCE = "/api/prediction";
const API_PLAYERS_SOURCE = "/api/players";

const SAVED_KEY = "vortex_saved_prop_ids";
const AVATAR_HUES = [168, 262, 24, 200, 330, 48, 140, 300];

// Just names, not data -- every lookup still goes through the live API.
const SUGGESTED_PLAYERS = ["Shohei Ohtani", "Freddie Freeman", "Aaron Judge"];

// Standard MLB batter prop types, always offered for a batter even with no
// static entry — the live API can compute any of these on demand.
// "Strikeouts" here is the BATTER'S OWN strikeouts (as a hitter) -- a
// completely different prop_type from "Strikeouts (Pitcher)" below.
const BATTER_STATS = [
  "Hits+Runs+RBIs", "Hits", "Total Bases", "Home Runs",
  "RBIs", "Runs Scored", "Strikeouts", "Walks", "Fantasy Score",
];

// Pitcher prop types -- shown instead of BATTER_STATS when the searched
// player is a pitcher (position "P").
const PITCHER_STATS = [
  "Strikeouts (Pitcher)", "Pitching Outs", "Earned Runs Allowed",
  "Hits Allowed", "Fantasy Score (Pitcher)",
];

// Combined list used only when a player's position isn't known yet (e.g.
// typed-then-Entered names that never went through autocomplete) -- shows
// everything rather than guessing wrong and hiding a valid option.
const STANDARD_STATS = [...BATTER_STATS, ...PITCHER_STATS];

// Typical opening line per stat, used only when there's no static data to
// infer a line from. A flat fallback capped every stat's slider at the
// same narrow range regardless of typical scale (a pitcher K prop routinely
// opens at 5.5+, a batter's own K prop rarely clears 1.5) -- this gives
// each stat a sane starting point and a proportional slider range.
const STAT_DEFAULT_LINE = {
  "Hits+Runs+RBIs": 1.5,
  "Hits": 0.5,
  "Total Bases": 1.5,
  "Home Runs": 0.5,
  "RBIs": 0.5,
  "Runs Scored": 0.5,
  "Strikeouts": 1.5,
  "Walks": 0.5,
  "Fantasy Score": 8.5,
  "Strikeouts (Pitcher)": 5.5,
  "Pitching Outs": 15.5,
  "Earned Runs Allowed": 2.5,
  "Hits Allowed": 5.5,
  "Fantasy Score (Pitcher)": 15.5,
};

const state = {
  props: [],
  activeIndex: -1,
  savedProps: loadSaved(), // Map<id, prop>


  parlaySelection: new Set(),
  currentTab: "research",
};

const els = {};

const THEME_KEY = "vortex_theme_mode";
const ACCENT_KEY = "vortex_theme_accent";
const CUSTOM_ACCENT_KEY = "vortex_theme_custom_hex";
// Mirrors --bg per data-theme in styles.css -- must be declared before the
// applyTheme() call below (which runs at script top-level, immediately),
// not down near the function definition, or it's a temporal-dead-zone
// ReferenceError the instant this file loads (same class of bug as
// CUSTOM_ACCENT_KEY earlier -- a top-level call reaching a later `const`
// before the script has "gotten there" in top-to-bottom execution).
const THEME_BG = { dark: "#101114", grey: "#2a2b30", light: "#f3f3f4" };
applyTheme(localStorage.getItem(THEME_KEY) || "dark");
applyAccent(localStorage.getItem(ACCENT_KEY) || "red");

init();

async function init() {
  cacheEls();
  try {
    await checkAuth();
  } catch (err) {
    console.error("checkAuth failed:", err);
  }
  try {
    wireSettingsPanel();
  } catch (err) {
    console.error("wireSettingsPanel failed:", err);
  }
  try {
    wireGameLogModal();
  } catch (err) {
    console.error("wireGameLogModal failed:", err);
  }
  try {
    wireTeamModal();
  } catch (err) {
    console.error("wireTeamModal failed:", err);
  }
  try {
    wireChromeAutoHide();
  } catch (err) {
    console.error("wireChromeAutoHide failed:", err);
  }

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
  els.profileStatsWrap = document.getElementById("profile-stats-wrap");
  els.profileStatsTrigger = document.getElementById("profile-stats-trigger");
  els.profileStatsTriggerLabel = document.getElementById("profile-stats-trigger-label");
  els.profileStatsMenu = document.getElementById("profile-stats-menu");

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

  els.bootLoading = document.getElementById("boot-loading");
  els.appShell = document.getElementById("app-shell");
  els.authGate = document.getElementById("auth-gate");
  els.authGateMsg = document.getElementById("auth-gate-msg");
  els.userBadge = document.getElementById("user-badge");
  els.userBadgeName = document.getElementById("user-badge-name");

  els.settingsBtn = document.getElementById("settings-btn");
  els.settingsPanel = document.getElementById("settings-panel");
  els.modeRow = document.getElementById("mode-row");
  els.accentRow = document.getElementById("accent-row");
  els.customAccentInput = document.getElementById("custom-accent-input");

  els.gamelogOverlay = document.getElementById("gamelog-overlay");
  els.gamelogTitle = document.getElementById("gamelog-title");
  els.gamelogClose = document.getElementById("gamelog-close");
  els.gamelogTabs = document.getElementById("gamelog-tabs");
  els.gamelogSub = document.getElementById("gamelog-sub");
  els.gamelogChart = document.getElementById("gamelog-chart");
  els.gamelogSubfilters = document.getElementById("gamelog-subfilters");
  els.glHandFilter = document.getElementById("gl-hand-filter");
  els.glVenueFilter = document.getElementById("gl-venue-filter");

  els.teamOverlay = document.getElementById("team-overlay");
  els.teamTitle = document.getElementById("team-title");
  els.teamClose = document.getElementById("team-close");
  els.teamTabs = document.getElementById("team-tabs");
  els.teamViewOrder = document.getElementById("team-view-order");
  els.teamViewArsenal = document.getElementById("team-view-arsenal");
  els.orderFilterRow = document.getElementById("order-filter-row");
  els.orderTbody = document.getElementById("order-tbody");
  els.orderEmpty = document.getElementById("order-empty");
  els.arsenalFilterRow = document.getElementById("arsenal-filter-row");
  els.arsenalTbody = document.getElementById("arsenal-tbody");
  els.arsenalEmpty = document.getElementById("arsenal-empty");
}

/* ---------- Theme (mode + accent) ---------- */

function applyTheme(mode) {
  document.documentElement.setAttribute("data-theme", mode);
  localStorage.setItem(THEME_KEY, mode);
  document.querySelectorAll(".mode-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.mode === mode);
  });
  const meta = document.getElementById("theme-color-meta");
  if (meta && THEME_BG[mode]) meta.setAttribute("content", THEME_BG[mode]);
}

function hexToRgb(hex) {
  const clean = hex.replace("#", "");
  const n = parseInt(clean.length === 3 ? clean.split("").map((c) => c + c).join("") : clean, 16);
  return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
}

function mixWithWhite(hex, amount) {
  const { r, g, b } = hexToRgb(hex);
  const mix = (c) => Math.round(c + (255 - c) * amount);
  return `rgb(${mix(r)}, ${mix(g)}, ${mix(b)})`;
}

function applyAccent(accent, customHex) {
  document.documentElement.setAttribute("data-accent", accent);
  localStorage.setItem(ACCENT_KEY, accent);

  if (accent === "custom" && customHex) {
    const { r, g, b } = hexToRgb(customHex);
    document.documentElement.style.setProperty("--accent", customHex);
    document.documentElement.style.setProperty("--accent-soft", mixWithWhite(customHex, 0.45));
    document.documentElement.style.setProperty("--accent-dim", `rgba(${r}, ${g}, ${b}, 0.16)`);
    localStorage.setItem(CUSTOM_ACCENT_KEY, customHex);
    els.customAccentInput.value = customHex;
  } else {
    // Preset accents are driven purely by the [data-accent] CSS rules —
    // clear any inline overrides left over from a previous custom pick.
    document.documentElement.style.removeProperty("--accent");
    document.documentElement.style.removeProperty("--accent-soft");
    document.documentElement.style.removeProperty("--accent-dim");
  }

  document.querySelectorAll(".swatch-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.accent === accent);
  });
}

/* ---------- Auto-hide top-right chrome (user badge + settings gear) ---------- */

function wireChromeAutoHide() {
  let lastY = window.scrollY;
  let ticking = false;
  const THRESHOLD = 8; // ignore sub-pixel/rubber-band jitter, mobile especially

  const apply = () => {
    const y = window.scrollY;
    const delta = y - lastY;
    const nearTop = y < 40;

    if (nearTop || delta < -THRESHOLD) {
      els.userBadge.classList.remove("chrome-hidden");
      els.settingsBtn.classList.remove("chrome-hidden");
    } else if (delta > THRESHOLD) {
      els.userBadge.classList.add("chrome-hidden");
      els.settingsBtn.classList.add("chrome-hidden");
    }
    lastY = y;
    ticking = false;
  };

  window.addEventListener("scroll", () => {
    if (!ticking) {
      requestAnimationFrame(apply);
      ticking = true;
    }
  }, { passive: true });
}

function wireSettingsPanel() {
  const savedMode = localStorage.getItem(THEME_KEY) || "dark";
  const savedAccent = localStorage.getItem(ACCENT_KEY) || "red";
  const savedCustomHex = localStorage.getItem(CUSTOM_ACCENT_KEY) || "#35e0c4";
  applyTheme(savedMode);
  els.customAccentInput.value = savedCustomHex;
  applyAccent(savedAccent, savedCustomHex);

  els.settingsBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    els.settingsPanel.hidden = !els.settingsPanel.hidden;
  });
  document.addEventListener("click", (e) => {
    if (!els.settingsPanel.hidden && !els.settingsPanel.contains(e.target) && e.target !== els.settingsBtn) {
      els.settingsPanel.hidden = true;
    }
  });
  els.modeRow.querySelectorAll(".mode-btn").forEach((btn) => {
    btn.addEventListener("click", () => applyTheme(btn.dataset.mode));
  });
  els.accentRow.querySelectorAll(".swatch-btn:not(.swatch-wheel)").forEach((btn) => {
    btn.addEventListener("click", () => applyAccent(btn.dataset.accent));
  });
  els.customAccentInput.addEventListener("input", () => {
    applyAccent("custom", els.customAccentInput.value);
  });
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

/* ---------- Auth gate (Discord OAuth + Premium/Tester role) ---------- */

async function checkAuth() {
  // The OAuth callback redirects back here with ?auth=success|denied|error
  // (it can't show a message itself -- it's a bare redirect). Surface it
  // once, then scrub the query param so a refresh doesn't repeat it.
  const params = new URLSearchParams(location.search);
  const authResult = params.get("auth");
  if (authResult) {
    params.delete("auth");
    const clean = location.pathname + (params.toString() ? `?${params}` : "");
    history.replaceState({}, "", clean);
  }

  let data;
  try {
    const res = await fetch("/api/auth/me", { cache: "no-store" });
    data = await res.json();
  } catch (err) {
    data = { authenticated: false };
  }

  els.bootLoading.hidden = true;

  if (data.authenticated) {
    els.authGate.hidden = true;
    els.userBadge.hidden = false;
    els.userBadgeName.textContent = data.username || "Member";
    els.appShell.classList.remove("app-shell-hidden");
    if (authResult === "success") showToast(`Welcome, ${data.username || "Member"}.`);
    return;
  }

  // Not authenticated: app shell stays hidden (never revealed), only the
  // gate shows. No flash of the app content either way.
  els.userBadge.hidden = true;
  if (authResult === "denied") {
    els.authGateMsg.textContent = "You're signed in with Discord, but don't have Premium/Tester access yet. Join the community role first, then sign in again.";
  } else if (authResult === "error") {
    els.authGateMsg.textContent = "Login didn't go through — please try again.";
  } else {
    els.authGateMsg.textContent = "Members-only research. Sign in with Discord to continue.";
  }
  els.authGate.hidden = false;
}

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

/* ---------- Saved props (localStorage) ----------
   Saved entries store the FULL prop object, not just an id — live-looked-up
   props never live in state.props (only static demo entries do), so looking
   them up by id against that array would silently fail to find them. */

function loadSaved() {
  try {
    const raw = localStorage.getItem(SAVED_KEY);
    const arr = raw ? JSON.parse(raw) : [];
    return new Map(arr.map((p) => [p.id, p]));
  } catch {
    return new Map();
  }
}

function persistSaved() {
  localStorage.setItem(SAVED_KEY, JSON.stringify([...state.savedProps.values()]));
}

function isSaved(id) {
  return state.savedProps.has(id);
}

function toggleSave(prop, btnEl) {
  if (isSaved(prop.id)) {
    state.savedProps.delete(prop.id);
    showToast(`Removed ${prop.player} from saved props`, "warn");
  } else {
    state.savedProps.set(prop.id, prop);
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
  els.savedCount.textContent = state.savedProps.size;
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
  els.profileStats.addEventListener("change", () => selectStat(els.profileStats.value));
  wireStatsDropdown();
}

/**
 * Custom-styled dropdown replacing the stat <select>'s native OS popup
 * (the blue-highlighted browser-default listbox looked out of place next
 * to the rest of the themed UI). The real <select> stays in the DOM,
 * hidden, purely as the value/change-event source everything else reads.
 */
function wireStatsDropdown() {
  els.profileStatsTrigger.addEventListener("click", (e) => {
    e.stopPropagation();
    els.profileStatsMenu.hidden ? openStatsMenu() : closeStatsMenu();
  });
  document.addEventListener("click", (e) => {
    if (!els.profileStatsMenu.hidden && !els.profileStatsWrap.contains(e.target)) closeStatsMenu();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !els.profileStatsMenu.hidden) closeStatsMenu();
  });
}

function openStatsMenu() {
  els.profileStatsMenu.hidden = false;
  els.profileStatsTrigger.classList.add("open");
}

function closeStatsMenu() {
  els.profileStatsMenu.hidden = true;
  els.profileStatsTrigger.classList.remove("open");
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

/** Normalizes static demo groups into the same shape live API suggestions use. */
function staticEntriesFor(query) {
  return matchPlayers(query).map(([player, props]) => ({
    kind: "static",
    player,
    team: props[0].team,
    sport: props[0].sport,
    sub: `${props.length} prop${props.length > 1 ? "s" : ""} available`,
    headshot: props[0].headshot || null,
  }));
}

let searchDebounceTimer = null;
let searchRequestToken = 0;

function onSearchInput() {
  const query = els.searchInput.value;
  const isSearchable = query.trim().length >= 2;

  // Static demo matches render instantly; live MLB suggestions follow after
  // a short debounce so we're not firing an API call on every keystroke.
  renderResults(staticEntriesFor(query), { loading: isSearchable });

  clearTimeout(searchDebounceTimer);
  if (!isSearchable) {
    searchRequestToken++; // invalidate any in-flight fetch's result
    return;
  }
  searchDebounceTimer = setTimeout(() => fetchLiveSuggestions(query), 250);
}

async function fetchLiveSuggestions(query) {
  const token = ++searchRequestToken;
  let livePlayers = [];
  let fetchFailed = false;
  try {
    const res = await fetch(`${API_PLAYERS_SOURCE}?q=${encodeURIComponent(query)}`);
    const data = await res.json();
    livePlayers = data.players || [];
  } catch (err) {
    fetchFailed = true;
  }

  if (token !== searchRequestToken) return; // a newer keystroke superseded this fetch

  const staticEntries = staticEntriesFor(query);
  const staticNames = new Set(staticEntries.map((e) => e.player.toLowerCase()));
  const liveEntries = livePlayers
    .filter((p) => p.name && !staticNames.has(p.name.toLowerCase()))
    .map((p) => ({
      kind: "live",
      player: p.name,
      team: p.team,
      sport: "MLB",
      sub: p.position || "MLB",
      headshot: `https://img.mlbstatic.com/mlb-photos/image/upload/w_180,q_auto:best/v1/people/${p.id}/headshot/67/current`,
    }));

  renderResults([...staticEntries, ...liveEntries], { loading: false, fetchFailed });
}

function onSearchKeydown(e) {
  const items = els.searchResults.querySelectorAll(".search-result-item");
  if (!items.length) {
    if (e.key === "Enter" && els.searchInput.value.trim().length > 1) {
      e.preventDefault();
      const query = els.searchInput.value.trim();
      els.searchInput.value = "";
      hideResults();
      selectPlayer(query);
    }
    return;
  }

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

function renderResults(entries, { loading = false, fetchFailed = false } = {}) {
  state.activeIndex = -1;
  els.searchResults.innerHTML = "";

  const query = els.searchInput.value.trim();
  const haveNames = new Set(entries.map((e) => e.player.toLowerCase()));
  // Manual "search live" fallback only when nothing else is offered --
  // covers the rare case where the MLB name-search API itself comes up
  // empty for a short/ambiguous query, or the autocomplete fetch failed.
  const showLiveOption = query.length > 1 && !haveNames.has(query.toLowerCase()) && !loading && (entries.length === 0 || fetchFailed);

  if (entries.length === 0 && !loading && !showLiveOption) {
    hideResults();
    return;
  }

  entries.forEach((entry, i) => {
    const li = document.createElement("li");
    li.className = "search-result-item";
    li.style.animationDelay = `${i * 30}ms`;
    li.innerHTML = `
      ${avatarHtml(entry, "sm")}
      <span class="sr-main">
        <span class="sr-player">${escapeHtml(entry.player)}${entry.team ? " (" + escapeHtml(entry.team) + ")" : ""}</span>
        <span class="sr-pick">${escapeHtml(entry.sub || "")}</span>
      </span>
      <span class="sr-sport">${escapeHtml(entry.sport || "")}</span>
    `;
    li.addEventListener("mousedown", () => {
      els.searchInput.value = "";
      hideResults();
      // Live-search entries carry the real position in `sub` (e.g. "P",
      // "SS"); static demo entries put a prop-count string there instead,
      // so only trust it as a position hint for live results.
      selectPlayer(entry.player, entry.kind === "live" ? entry.sub : null);
    });
    els.searchResults.appendChild(li);
  });

  if (loading) {
    const li = document.createElement("li");
    li.className = "search-result-item search-result-loading";
    li.innerHTML = `<span class="loading-pulse"></span><span class="sr-main"><span class="sr-pick">Searching MLB players…</span></span>`;
    els.searchResults.appendChild(li);
  }

  if (showLiveOption) {
    const li = document.createElement("li");
    li.className = "search-result-item search-result-live";
    li.style.animationDelay = `${entries.length * 30}ms`;
    li.innerHTML = `
      <span class="sr-live-icon">⚡</span>
      <span class="sr-main">
        <span class="sr-player">Search "${escapeHtml(query)}" live</span>
        <span class="sr-pick">${fetchFailed ? "Player search failed — try an exact name" : "No matches yet — try the exact name"}</span>
      </span>
    `;
    li.addEventListener("mousedown", () => {
      els.searchInput.value = "";
      hideResults();
      selectPlayer(query);
    });
    els.searchResults.appendChild(li);
  }

  els.searchResults.hidden = false;
}

function hideResults() {
  els.searchResults.hidden = true;
}

// Quick-start suggestions for the "Or jump straight to:" row. These are just
// names, not data -- every lookup still goes through the live API, same as
// typing a name and picking "search live". Static predictions.json now
// ships with zero entries on purpose: any pre-baked demo data risked being
// shown instead of a real live result whenever a stat/line happened to
// match, which was actively misleading (e.g. a fabricated "Rockies"
// matchup appearing for a real Padres game).

function renderBrowseChips() {
  els.browseChips.innerHTML = "";
  SUGGESTED_PLAYERS.forEach((player) => {
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
  els.lineSlider.addEventListener("change", () => {
    setLineValue(Number(els.lineSlider.value), { immediate: true });
  });
  els.lineNumber.addEventListener("change", () => {
    setLineValue(Number(els.lineNumber.value), { immediate: true });
  });
  els.lineStepDown.addEventListener("click", () => setLineValue(cmd.line - 0.5, { immediate: true }));
  els.lineStepUp.addEventListener("click", () => setLineValue(cmd.line + 0.5, { immediate: true }));
}

// "P" -> pitcher-only stats, anything else known -> batter-only stats,
// undefined/null (position not known, e.g. typed-and-Entered names that
// skipped autocomplete) -> both, so a valid option is never hidden just
// because we couldn't confirm the position.
function selectPlayer(player, position, { autoSelectStat = true } = {}) {
  hideResults();
  cmd.player = player;
  cmd.stat = null;
  cmd.line = null;
  cmd.side = null;

  const staticProps = propsForPlayer();
  const first = staticProps[0];

  els.profileAvatar.innerHTML = first ? avatarHtml(first, "lg") : avatarHtml(player, "lg");
  els.profileName.textContent = first && first.team ? `${player} (${first.team})` : player;
  els.profileSub.textContent = first
    ? `${first.sport} · pick a stat to dial in a line`
    : "MLB · pick a stat to look up a live line";

  // "TWP" = two-way player (e.g. Ohtani) -- genuinely both a hitter and a
  // pitcher, so neither stat set alone is correct; show everything, same
  // as the unknown-position fallback.
  const standardForPosition =
    position === "P" ? PITCHER_STATS :
    position === "TWP" || !position ? STANDARD_STATS :
    BATTER_STATS;

  // Static stats first (instant), then any standard stats not already covered —
  // those fall through to the live API when selected.
  const staticStats = [...new Set(staticProps.map((p) => p.betType))];
  const stats = [...new Set([...staticStats, ...standardForPosition])];

  els.profileStats.innerHTML = "";
  els.profileStatsMenu.innerHTML = "";
  stats.forEach((stat) => {
    const opt = document.createElement("option");
    opt.value = stat;
    opt.textContent = stat;
    els.profileStats.appendChild(opt);

    const li = document.createElement("li");
    li.className = "profile-stats-menu-item";
    li.setAttribute("role", "option");
    li.textContent = stat;
    li.addEventListener("click", () => {
      els.profileStats.value = stat;
      closeStatsMenu();
      selectStat(stat);
    });
    els.profileStatsMenu.appendChild(li);
  });

  els.linePicker.hidden = true;
  els.playerProfile.hidden = false;
  clearReport();
  els.playerProfile.scrollIntoView({ behavior: "smooth", block: "nearest" });

  // Auto-select the first stat so the line picker appears immediately
  // instead of requiring an extra click just to see anything (a plain
  // <select> has no "nothing selected" affordance the way a button grid did).
  // Skipped when reopening a saved prop -- openExactProp sets the exact
  // saved stat/line/side itself right after this call, so auto-selecting
  // here would fire one wasted live fetch for the wrong (first) stat.
  if (autoSelectStat && stats.length) selectStat(stats[0]);
}

function selectStat(stat) {
  cmd.stat = stat;
  if (els.profileStats.value !== stat) els.profileStats.value = stat;
  els.profileStatsTriggerLabel.textContent = stat;
  els.profileStatsMenu.querySelectorAll(".profile-stats-menu-item").forEach((li) => {
    li.classList.toggle("active", li.textContent === stat);
  });

  const matches = propsForPlayer().filter((p) => p.betType === stat);
  const hasStaticData = matches.length > 0;
  const fallbackLine = STAT_DEFAULT_LINE[stat] ?? 0.5;
  const lines = hasStaticData ? matches.map((p) => p.line) : [fallbackLine];
  const defaultProp = matches[0];
  const availableSides = new Set(matches.map((p) => p.side));

  // Only lock out a side when we KNOW (from static data) it has no coverage.
  // For live lookups both sides are always computable, so leave them enabled.
  els.sideToggle.querySelectorAll(".side-btn").forEach((b) => {
    const hasData = !hasStaticData || availableSides.has(b.dataset.side);
    b.disabled = !hasData;
    b.title = hasData ? "" : `No ${b.dataset.side} data for ${stat}`;
  });

  // Slider spans a little past the known lines so there's room to explore.
  // The span scales with the line itself so high-volume stats (Strikeouts
  // routinely opens at 5.5+) get real headroom instead of a flat +/-1.5.
  const span = Math.max(1.5, Math.round(Math.max(...lines) * 0.6 * 2) / 2);
  const min = Math.max(0, Math.min(...lines) - span);
  const max = Math.max(...lines) + span;
  els.lineSlider.min = String(min);
  els.lineSlider.max = String(max);
  els.lineNumber.min = String(min);
  els.lineNumber.max = String(max);

  cmd.side = defaultProp ? defaultProp.side : "Over";
  els.sideToggle.querySelectorAll(".side-btn").forEach((b) => b.classList.toggle("active", b.dataset.side === cmd.side));

  els.linePicker.hidden = false;
  setLineValue(defaultProp ? defaultProp.line : lines[0]);
}

let lineDebounceTimer = null;

// Dragging the slider fires an "input" event per pixel of movement. Running
// applyLineSelection() (which re-renders a loading skeleton, or the whole
// report, on every tick) on each of those made the slider visibly stutter
// mid-drag. The value/fill/number stay perfectly live (cheap, no re-render);
// only the actual lookup is debounced until the drag settles.
function setLineValue(value, { immediate = false } = {}) {
  const min = Number(els.lineSlider.min);
  const max = Number(els.lineSlider.max);
  const snapped = Math.round(Math.max(min, Math.min(max, value)) * 2) / 2; // snap to 0.5
  cmd.line = snapped;

  els.lineSlider.value = String(snapped);
  els.lineNumber.value = String(snapped);
  const pct = max > min ? ((snapped - min) / (max - min)) * 100 : 0;
  els.lineSlider.style.setProperty("--fill", `${pct}%`);

  clearTimeout(lineDebounceTimer);
  if (immediate) {
    applyLineSelection();
  } else {
    lineDebounceTimer = setTimeout(applyLineSelection, 120);
  }
}

let lineSelectionToken = 0;

function applyLineSelection() {
  const match = propsForPlayer().find(
    (p) => p.betType === cmd.stat && Math.abs(p.line - cmd.line) < 0.01 && p.side === cmd.side
  );

  const token = ++lineSelectionToken; // race guard for fast slider drags

  if (match) {
    els.lineNoData.hidden = true;
    renderReport(match);
    return;
  }

  els.lineNoData.hidden = true;
  fetchLivePrediction(cmd.player, cmd.stat, cmd.line, cmd.side, token);
}

async function fetchLivePrediction(player, stat, line, side, token) {
  renderLoadingState(player, stat, line, side);

  let result = null;
  let errorMessage = null;
  try {
    const url = `${API_SOURCE}?player=${encodeURIComponent(player)}&stat=${encodeURIComponent(stat)}&line=${line}&side=${side.toLowerCase()}`;
    const res = await fetch(url);
    const data = await res.json();
    if (!res.ok || data.error) {
      errorMessage = data.error || `Request failed (${res.status})`;
    } else {
      result = data;
    }
  } catch (err) {
    errorMessage = err.message;
  }

  if (token !== lineSelectionToken) return; // a newer selection superseded this one

  if (result) {
    renderReport(result);
    return;
  }

  showNoDataMessage(stat, line, side, errorMessage);
}

function renderLoadingState(player, stat, line, side) {
  els.reportWrap.querySelector(".report")?.remove();
  els.reportWrap.querySelector(".report-skeleton")?.remove();
  els.emptyState.hidden = true;

  const skeleton = document.createElement("div");
  skeleton.className = "report-skeleton";
  skeleton.innerHTML = `
    <p class="skeleton-status">
      <span class="loading-pulse"></span>
      Computing live analysis for ${escapeHtml(player)} — ${escapeHtml(side)} ${line} ${escapeHtml(stat)}…
    </p>
    <div class="skel-block skel-header">
      <div class="skel-avatar"></div>
      <div class="skel-lines">
        <div class="skel-line skel-line-wide"></div>
        <div class="skel-line skel-line-narrow"></div>
      </div>
      <div class="skel-score"></div>
    </div>
    <div class="skel-block">
      <div class="skel-line skel-line-label"></div>
      <div class="skel-line"></div>
      <div class="skel-line"></div>
      <div class="skel-line skel-line-narrow"></div>
    </div>
    <div class="skel-block">
      <div class="skel-line skel-line-label"></div>
      <div class="skel-bars"></div>
    </div>
  `;
  els.reportWrap.appendChild(skeleton);
}

function showNoDataMessage(stat, line, side, liveError) {
  clearReport();
  const nearest = propsForPlayer()
    .filter((p) => p.betType === stat)
    .sort((a, b) => Math.abs(a.line - line) - Math.abs(b.line - line))[0];

  els.lineNoData.hidden = false;
  els.lineNoData.innerHTML = liveError
    ? `No computed analysis for ${escapeHtml(side)} ${line} and live lookup failed — ${escapeHtml(liveError)}.`
    : `No computed analysis for ${escapeHtml(side)} ${line} yet.`;

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

/** Once a live result confirms the real player name/team, fix up the profile
 * header (which only had whatever casing the user typed, e.g. "freddie freeman"). */
function syncProfileHeaderWithProp(p) {
  if (!els.playerProfile.hidden && p.player) {
    els.profileName.textContent = p.team ? `${p.player} (${p.team})` : p.player;
    els.profileAvatar.innerHTML = avatarHtml(p, "lg");
    cmd.player = p.player;
  }
}

const EMPTY_STATE_DEFAULT_TEXT = "Search for a player above to pull up their prop breakdown.";

function clearReport() {
  els.reportWrap.querySelector(".report")?.remove();
  els.reportWrap.querySelector(".report-skeleton")?.remove();
  els.emptyState.hidden = false;
  els.emptyState.textContent = EMPTY_STATE_DEFAULT_TEXT;
}

/* ---------- Report rendering (Research tab) ---------- */

function renderReport(p) {
  hideResults();
  els.emptyState.hidden = true;
  els.reportWrap.querySelector(".report")?.remove();
  els.reportWrap.querySelector(".report-skeleton")?.remove();
  syncProfileHeaderWithProp(p);

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
    fillSparkline(node, p.last5 || [], p.line);
  });

  const expandBtn = node.querySelector(".last5-expand-btn");
  if (p.gameLogChart && Object.keys(p.gameLogChart).length) {
    expandBtn.addEventListener("click", () => openGameLogModal(p));
  } else {
    expandBtn.classList.add("last5-expand-disabled");
  }

  if (state.currentTab === "research") {
    node.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

/* ---------- Expandable game log modal (L5/L10/L15/L20/H2H) ---------- */

let gameLogState = {
  chart: null, line: null, player: "", opponent: "", window: "l10",
  handFilter: "all", venueFilter: "all", handDataLoaded: false, teamId: null,
};

function openGameLogModal(p) {
  gameLogState.chart = p.gameLogChart || {};
  gameLogState.line = p.line;
  gameLogState.player = p.player;
  gameLogState.opponent = (p.matchup && p.matchup.opponent) || "";
  gameLogState.handFilter = "all";
  gameLogState.venueFilter = "all";
  gameLogState.handDataLoaded = false;
  gameLogState.teamId = (p.teamInsightsParams && p.teamInsightsParams.teamId) || null;
  // Default to the widest window that actually has data, so a prop with
  // only 5 games logged doesn't open on an empty L10 tab.
  gameLogState.window = ["l10", "l5", "l15", "l20"].find((w) => (gameLogState.chart[w] || []).length > 0) || "l10";

  els.gamelogOverlay.hidden = false;
  els.gamelogTitle.textContent = `${p.player} — ${p.betType}`;
  renderGameLogTabs();
  renderGameLogChart();

  // Handedness filter needs a lazy fetch (resolving each game's opposing
  // starter's hand costs real network time); venue (home/road) is already
  // in the data for free. Pitcher props don't get a hand filter at all --
  // one start faces a whole lineup of both hands, so "the game's
  // handedness" isn't a coherent concept the way it is for a batter.
  const isPitcherProp = PITCHER_STATS.includes(p.betType);
  els.glHandFilter.hidden = isPitcherProp;
  renderGameLogSubfilters();
  if (!isPitcherProp) fetchGameLogHandedness(p);
}

async function fetchGameLogHandedness(p) {
  els.glHandFilter.querySelectorAll(".gl-filter-chip").forEach((b) => { b.disabled = true; });
  try {
    const url = `/api/game-log-filters?player=${encodeURIComponent(p.player)}&stat=${encodeURIComponent(p.betType)}&line=${p.line}` +
      (gameLogState.teamId ? `&teamId=${gameLogState.teamId}` : "");
    const res = await fetch(url);
    const data = await res.json();
    if (res.ok && !data.error) {
      // Merge per-window, don't replace wholesale -- teamInsightsParams
      // (and so gameLogState.teamId) can be null when the lineup/pitcher
      // isn't confirmed yet, which means THIS fetch can't resolve H2H even
      // though the initial card load already had it. Overwriting the whole
      // chart in that case silently threw away good H2H data the moment
      // this lazy fetch resolved. Only replace windows this fetch actually
      // returned games for; leave everything else as it was.
      for (const key of Object.keys(data)) {
        if (data[key] && data[key].length) gameLogState.chart[key] = data[key];
      }
      gameLogState.handDataLoaded = true;
    }
  } catch (err) {
    console.error("game-log-filters fetch failed:", err);
  } finally {
    els.glHandFilter.querySelectorAll(".gl-filter-chip").forEach((b) => { b.disabled = false; });
    renderGameLogTabs();
    renderGameLogChart();
  }
}

function closeGameLogModal() {
  els.gamelogOverlay.hidden = true;
}

function filterGames(games) {
  return games.filter((g) => {
    if (gameLogState.handFilter !== "all" && g.oppHand !== gameLogState.handFilter) return false;
    if (gameLogState.venueFilter === "home" && g.isHome !== true) return false;
    if (gameLogState.venueFilter === "road" && g.isHome !== false) return false;
    return true;
  });
}

function renderGameLogSubfilters() {
  els.gamelogSubfilters.hidden = false;
  els.glHandFilter.querySelectorAll(".gl-filter-chip").forEach((b) => {
    b.classList.toggle("active", b.dataset.hand === gameLogState.handFilter);
  });
  els.glVenueFilter.querySelectorAll(".gl-filter-chip").forEach((b) => {
    b.classList.toggle("active", b.dataset.venue === gameLogState.venueFilter);
  });
}

function renderGameLogTabs() {
  els.gamelogTabs.querySelectorAll(".gamelog-tile").forEach((btn) => {
    const w = btn.dataset.window;
    const rawGames = gameLogState.chart[w] || [];
    const games = filterGames(rawGames);
    const hasData = rawGames.length > 0;
    btn.disabled = !hasData;
    btn.classList.toggle("active", w === gameLogState.window);

    const labelEl = btn.querySelector(".gl-tile-label");
    const rateEl = btn.querySelector(".gl-tile-rate");
    const avgEl = btn.querySelector(".gl-tile-avg");
    if (w === "h2h") {
      labelEl.textContent = gameLogState.opponent ? `H2H (${gameLogState.opponent})` : "H2H";
    }
    if (!hasData || !games.length) {
      rateEl.textContent = hasData ? "0 g" : "—";
      avgEl.textContent = "";
      rateEl.classList.remove("gl-tile-rate-good", "gl-tile-rate-bad");
      return;
    }
    const overCount = games.filter((g) => g.over).length;
    const rate = Math.round((overCount / games.length) * 100);
    const avg = games.reduce((sum, g) => sum + g.value, 0) / games.length;
    rateEl.textContent = `${rate}%`;
    rateEl.classList.toggle("gl-tile-rate-good", rate >= 55);
    rateEl.classList.toggle("gl-tile-rate-bad", rate <= 45);
    avgEl.textContent = `Avg ${avg.toFixed(2)}`;
  });
}

function renderGameLogChart() {
  const games = filterGames(gameLogState.chart[gameLogState.window] || []);
  const holder = els.gamelogChart;
  holder.innerHTML = "";

  const filterBits = [];
  if (gameLogState.handFilter !== "all") filterBits.push(`vs ${gameLogState.handFilter}HP`);
  if (gameLogState.venueFilter !== "all") filterBits.push(gameLogState.venueFilter === "home" ? "at home" : "on the road");
  const filterSuffix = filterBits.length ? ` (${filterBits.join(", ")})` : "";

  if (!games.length) {
    const rawLen = (gameLogState.chart[gameLogState.window] || []).length;
    els.gamelogSub.textContent = rawLen
      ? `No games in this window${filterSuffix}.`
      : "No games available for this window.";
    return;
  }

  const label = gameLogState.window === "h2h"
    ? `Every game vs ${gameLogState.opponent || "this opponent"} this season${filterSuffix}`
    : `Last ${games.length} games${filterSuffix}`;
  const overCount = games.filter((g) => g.over).length;
  els.gamelogSub.textContent = `${label} — ${overCount}/${games.length} over the ${gameLogState.line} line (${Math.round((overCount / games.length) * 100)}%).`;

  // Line value can exceed every game's value (e.g. a 5.5 K line with a
  // season-high of 5) -- widen the scale so the dashed marker never sits
  // above the chart's visible area.
  const line = gameLogState.line;
  const trackPx = 130;
  const max = Math.max(...games.map((g) => g.value), typeof line === "number" ? line : 0, 1);
  games.forEach((g) => {
    const col = document.createElement("div");
    col.className = "gl-col";
    const heightPx = Math.max(4, (g.value / max) * trackPx);
    col.innerHTML = `
      <div class="gl-track" style="height:${trackPx}px">
        <div class="gl-bar${g.over ? "" : " gl-bar-under"}" style="height:${heightPx}px">
          <span class="gl-val">${g.value}</span>
        </div>
      </div>
      <span class="gl-opp">${escapeHtml(g.opponent || "")}</span>
      <span class="gl-date">${escapeHtml(g.date || "")}</span>
    `;
    holder.appendChild(col);
  });

  if (typeof line === "number") {
    const topPx = Math.max(0, trackPx - (line / max) * trackPx);
    const marker = document.createElement("div");
    marker.className = "gl-line-marker";
    marker.style.top = `${topPx}px`;
    marker.innerHTML = `<span class="gl-line-tag">${line}</span>`;
    holder.appendChild(marker);
  }
}

function wireGameLogModal() {
  els.gamelogClose.addEventListener("click", closeGameLogModal);
  els.gamelogOverlay.addEventListener("click", (e) => {
    if (e.target === els.gamelogOverlay) closeGameLogModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !els.gamelogOverlay.hidden) closeGameLogModal();
  });
  els.gamelogTabs.querySelectorAll(".gamelog-tile").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.disabled) return;
      gameLogState.window = btn.dataset.window;
      renderGameLogTabs();
      renderGameLogChart();
    });
  });
  els.glHandFilter.querySelectorAll(".gl-filter-chip").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.disabled) return;
      gameLogState.handFilter = btn.dataset.hand;
      renderGameLogSubfilters();
      renderGameLogTabs();
      renderGameLogChart();
    });
  });
  els.glVenueFilter.querySelectorAll(".gl-filter-chip").forEach((btn) => {
    btn.addEventListener("click", () => {
      gameLogState.venueFilter = btn.dataset.venue;
      renderGameLogSubfilters();
      renderGameLogTabs();
      renderGameLogChart();
    });
  });
}

/* ---------- Team insights modal (Batting Order & Pitch Arsenal) ---------- */

const TEAM_INSIGHTS_SOURCE = "/api/team-insights";

const teamState = {
  data: null,        // last fetched response, keyed by cacheKey below
  cacheKey: "",       // teamId+pitcherId -- avoids refetching on reopen
  view: "order",      // "order" | "arsenal"
  orderFilter: "season", // "season" | "handL" | "handR" | "pitcher"
  pitchFilter: "",    // selected pitch_type code, "" = first available
};

// Fixed thresholds (not "vs this player's own baseline") -- green/red mean
// "statistically strong/weak performance," matching how the reference
// screenshots color raw values directly. Applied to every filter equally.
function tierClassFor(metric, value) {
  const v = parseFloat(value);
  if (value == null || value === "" || Number.isNaN(v)) return "";
  if (metric === "avg") return v >= 0.27 ? "tt-good" : v <= 0.2 ? "tt-bad" : "";
  if (metric === "ops") return v >= 0.8 ? "tt-good" : v <= 0.65 ? "tt-bad" : "";
  if (metric === "woba") return v >= 0.35 ? "tt-good" : v <= 0.29 ? "tt-bad" : "";
  if (metric === "k_pct") return v <= 20 ? "tt-good" : v >= 30 ? "tt-bad" : "";
  return "";
}

function openTeamModal(params, opponentName) {
  els.teamOverlay.hidden = false;
  els.teamTitle.textContent = opponentName ? `${opponentName} — Team Insights` : "Team Insights";

  const key = `${params.teamId}-${params.pitcherId}`;
  if (teamState.cacheKey === key && teamState.data) {
    renderTeamModal();
    return;
  }
  teamState.cacheKey = key;
  teamState.data = null;
  renderTeamModal(); // shows loading state
  fetchTeamInsights(params);
}

function closeTeamModal() {
  els.teamOverlay.hidden = true;
}

async function fetchTeamInsights(params) {
  const url = `${TEAM_INSIGHTS_SOURCE}?teamId=${params.teamId}&pitcherId=${params.pitcherId || ""}`
    + `&pitcherName=${encodeURIComponent(params.pitcherName || "")}&pitcherHand=${params.pitcherHand || "R"}`;
  try {
    const res = await fetch(url);
    const data = await res.json();
    if (`${params.teamId}-${params.pitcherId}` !== teamState.cacheKey) return; // stale response, modal moved on
    teamState.data = data.error ? { error: data.error } : data;
  } catch (err) {
    teamState.data = { error: err.message };
  }
  renderTeamModal();
}

function wireTeamModal() {
  els.teamClose.addEventListener("click", closeTeamModal);
  els.teamOverlay.addEventListener("click", (e) => {
    if (e.target === els.teamOverlay) closeTeamModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !els.teamOverlay.hidden) closeTeamModal();
  });
  els.teamTabs.querySelectorAll(".team-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      teamState.view = btn.dataset.view;
      renderTeamModal();
    });
  });
  els.orderFilterRow.querySelectorAll(".team-filter").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.disabled) return;
      teamState.orderFilter = btn.dataset.filter;
      renderTeamModal();
    });
  });
}

function renderTeamModal() {
  els.teamTabs.querySelectorAll(".team-tab").forEach((b) => b.classList.toggle("active", b.dataset.view === teamState.view));
  els.teamViewOrder.hidden = teamState.view !== "order";
  els.teamViewArsenal.hidden = teamState.view !== "arsenal";

  const data = teamState.data;
  if (!data) {
    els.orderEmpty.hidden = false;
    els.orderEmpty.textContent = "Loading lineup…";
    els.orderTbody.innerHTML = "";
    els.arsenalEmpty.hidden = false;
    els.arsenalEmpty.textContent = "Loading lineup…";
    els.arsenalTbody.innerHTML = "";
    return;
  }
  if (data.error || !data.battingOrder || !data.battingOrder.length) {
    const msg = data.error ? `Couldn't load lineup — ${data.error}` : "Lineup not confirmed yet for tonight's game.";
    els.orderEmpty.hidden = false;
    els.orderEmpty.textContent = msg;
    els.orderTbody.innerHTML = "";
    els.arsenalEmpty.hidden = false;
    els.arsenalEmpty.textContent = msg;
    els.arsenalTbody.innerHTML = "";
    return;
  }

  renderOrderView(data);
  renderArsenalView(data);
}

function renderOrderView(data) {
  els.orderEmpty.hidden = true;

  const pitcherBtn = els.orderFilterRow.querySelector('[data-filter="pitcher"]');
  const tonightHand = data.opponentPitcherHand === "L" ? "L" : "R";
  pitcherBtn.textContent = data.opponentPitcherName ? `vs ${data.opponentPitcherName}` : "vs Pitcher";
  pitcherBtn.disabled = !data.opponentPitcherName;

  // Both hands are always shown side by side (as separate filters) so you
  // can compare a batter's platoon split, not just whichever hand happens
  // to be pitching tonight -- that one gets a small marker for context.
  ["handL", "handR"].forEach((key) => {
    const btn = els.orderFilterRow.querySelector(`[data-filter="${key}"]`);
    const isTonight = (key === "handL" && tonightHand === "L") || (key === "handR" && tonightHand === "R");
    btn.classList.toggle("tt-tonight", isTonight);
    btn.title = isTonight ? "Tonight's starter throws this hand" : "";
  });

  els.orderFilterRow.querySelectorAll(".team-filter").forEach((b) => {
    b.classList.toggle("active", b.dataset.filter === teamState.orderFilter);
  });

  const fieldFor = { season: "season", handL: "handSplitL", handR: "handSplitR", pitcher: "vsPitcher" }[teamState.orderFilter];
  els.orderTbody.innerHTML = "";
  data.battingOrder.forEach((row) => {
    const stat = row[fieldFor];
    const tr = document.createElement("tr");
    if (!stat) {
      tr.innerHTML = `
        <td class="tt-player"><span class="tt-order">${row.order}</span> ${escapeHtml(row.name)} <span class="tt-pos">${escapeHtml(row.position)}</span></td>
        <td colspan="6" class="tt-nodata">${teamState.orderFilter === "pitcher" ? "No history vs this pitcher" : "No data"}</td>
      `;
    } else {
      tr.innerHTML = `
        <td class="tt-player"><span class="tt-order">${row.order}</span> ${escapeHtml(row.name)} <span class="tt-pos">${escapeHtml(row.position)}</span></td>
        <td>${stat.ab ?? "—"}</td>
        <td class="${tierClassFor("avg", stat.avg)}">${stat.avg ?? "—"}</td>
        <td>${stat.hr ?? "—"}</td>
        <td>${stat.rbi ?? "—"}</td>
        <td class="${tierClassFor("ops", stat.ops)}">${stat.ops ?? "—"}</td>
        <td class="${tierClassFor("k_pct", stat.k_pct)}">${stat.k_pct != null ? stat.k_pct + "%" : "—"}</td>
      `;
    }
    els.orderTbody.appendChild(tr);
  });
}

function renderArsenalView(data) {
  els.arsenalEmpty.hidden = true;
  const pitchTypes = data.pitchTypes || [];
  if (!pitchTypes.length) {
    els.arsenalEmpty.hidden = false;
    els.arsenalEmpty.textContent = "No pitch-mix data available for tonight's starter.";
    els.arsenalTbody.innerHTML = "";
    return;
  }
  if (!teamState.pitchFilter || (teamState.pitchFilter !== "ALL" && !pitchTypes.some((p) => p.code === teamState.pitchFilter))) {
    teamState.pitchFilter = pitchTypes[0].code;
  }

  els.arsenalFilterRow.innerHTML = "";
  const allBtn = document.createElement("button");
  allBtn.type = "button";
  allBtn.className = "team-filter" + (teamState.pitchFilter === "ALL" ? " active" : "");
  allBtn.textContent = "All";
  allBtn.title = "Combined across every pitch tonight's starter throws";
  allBtn.addEventListener("click", () => {
    teamState.pitchFilter = "ALL";
    renderArsenalView(data);
  });
  els.arsenalFilterRow.appendChild(allBtn);
  pitchTypes.forEach((p) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "team-filter" + (p.code === teamState.pitchFilter ? " active" : "");
    btn.textContent = p.code;
    btn.title = p.name;
    btn.addEventListener("click", () => {
      teamState.pitchFilter = p.code;
      renderArsenalView(data);
    });
    els.arsenalFilterRow.appendChild(btn);
  });

  // "All" = PA-weighted aggregate across every pitch type tonight's starter
  // actually throws (not every pitch this batter has ever seen from anyone)
  // -- answers "how does he do against this pitcher's whole mix," using
  // only data the modal already fetched, no new calls.
  function combinedStat(byPitch) {
    const rows = pitchTypes.map((p) => byPitch[p.code]).filter(Boolean);
    if (!rows.length) return null;
    const pa = rows.reduce((s, r) => s + (r.pa || 0), 0);
    const pitches = rows.reduce((s, r) => s + (r.pitches || 0), 0);
    if (!pa) return null;
    const wAvg = (key) => rows.reduce((s, r) => s + (parseFloat(r[key]) || 0) * (r.pa || 0), 0) / pa;
    return { pa, pitches, k_pct: Math.round(wAvg("k_pct") * 10) / 10, woba: wAvg("woba").toFixed(3) };
  }

  els.arsenalTbody.innerHTML = "";
  (data.pitchRows || []).forEach((row) => {
    const stat = teamState.pitchFilter === "ALL" ? combinedStat(row.byPitch || {}) : (row.byPitch || {})[teamState.pitchFilter];
    const tr = document.createElement("tr");
    if (!stat) {
      tr.innerHTML = `
        <td class="tt-player"><span class="tt-order">${row.order}</span> ${escapeHtml(row.name)} <span class="tt-pos">${escapeHtml(row.position)}</span></td>
        <td colspan="4" class="tt-nodata">No data vs this pitch</td>
      `;
    } else {
      tr.innerHTML = `
        <td class="tt-player"><span class="tt-order">${row.order}</span> ${escapeHtml(row.name)} <span class="tt-pos">${escapeHtml(row.position)}</span></td>
        <td>${stat.pa ?? "—"}</td>
        <td>${stat.pitches ?? "—"}</td>
        <td class="${tierClassFor("k_pct", stat.k_pct)}">${stat.k_pct != null ? stat.k_pct + "%" : "—"}</td>
        <td class="${tierClassFor("woba", stat.woba)}">${stat.woba ?? "—"}</td>
      `;
    }
    els.arsenalTbody.appendChild(tr);
  });
}

/**
 * Rebuilds the player profile + line picker for an exact prop, used when
 * reopening a saved prop or a parlay leg from the Saved tab.
 */
/**
 * Reopens a saved prop by rendering its stored snapshot directly. Deliberately
 * does NOT go through setLineValue()/applyLineSelection() — those search
 * state.props (only the static demo entries) and would either miss a saved
 * live result entirely or trigger a pointless (and possibly different, since
 * stats move) re-fetch instead of showing what was actually saved.
 */
function openExactProp(p) {
  selectPlayer(p.player, null, { autoSelectStat: false }); // builds the profile shell (avatar, stat select)
  cmd.player = p.player;
  cmd.stat = p.betType;
  cmd.line = p.line;
  cmd.side = p.side;

  if (![...els.profileStats.options].some((o) => o.value === p.betType)) {
    const opt = document.createElement("option");
    opt.value = p.betType;
    opt.textContent = p.betType;
    els.profileStats.appendChild(opt);
  }
  els.profileStats.value = p.betType;
  els.profileStatsTriggerLabel.textContent = p.betType;
  els.profileStatsMenu.querySelectorAll(".profile-stats-menu-item").forEach((li) => {
    li.classList.toggle("active", li.textContent === p.betType);
  });

  // Same scaled span as selectStat() — a flat ±1.5 boxed in high lines
  // (e.g. a saved 5.5 K prop could only slide 4–7 after reopening).
  const span = Math.max(1.5, Math.round(p.line * 0.6 * 2) / 2);
  const min = Math.max(0, p.line - span);
  const max = p.line + span;
  els.lineSlider.min = String(min);
  els.lineSlider.max = String(max);
  els.lineNumber.min = String(min);
  els.lineNumber.max = String(max);
  els.lineSlider.value = String(p.line);
  els.lineNumber.value = String(p.line);
  els.lineSlider.style.setProperty("--fill", `${((p.line - min) / (max - min)) * 100}%`);

  els.sideToggle.querySelectorAll(".side-btn").forEach((b) => {
    b.disabled = false;
    b.classList.toggle("active", b.dataset.side === p.side);
  });

  els.linePicker.hidden = false;
  els.lineNoData.hidden = true;

  renderReport(p);
}

function buildReportNode(p) {
  const template = document.getElementById("report-template");
  const node = template.content.firstElementChild.cloneNode(true);

  node.querySelector(".rt-avatar-slot").innerHTML = avatarHtml(p, "lg");

  fillHeader(node, p);
  fillProjection(node, p);
  fillWhyItHits(node, p);
  fillBiggestEdgesRisks(node, p);
  fillConfidenceBreakdown(node, p);
  fillScorecardV2(node, p);
  fillDistribution(node, p);
  fillPitchArsenal(node, p);
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

function fillConfidenceBreakdown(node, p) {
  const block = node.querySelector(".confidence-block");
  const items = p.confidenceBreakdown || [];
  if (!items.length) {
    block.hidden = true;
    return;
  }
  block.hidden = false;
  const holder = block.querySelector(".confidence-rows");
  holder.innerHTML = "";
  items.forEach((item) => {
    const row = document.createElement("div");
    row.className = "conf-row";
    const pct = Math.max(0, Math.min(100, (item.score / 10) * 100));
    const tone = item.score >= 6.5 ? "good" : item.score <= 3.5 ? "bad" : "mid";
    row.innerHTML = `
      <span class="conf-label">${escapeHtml(item.label)}</span>
      <div class="conf-track"><div class="conf-fill conf-fill-${tone}" style="width:${pct}%"></div></div>
      <span class="conf-score">${item.score.toFixed(1)}</span>
    `;
    holder.appendChild(row);
  });
}

const V2_CATEGORY_LABELS = {
  projection: "Projection", matchup: "Matchup", skill: "Skill", context: "Context",
  form: "Form", variance: "Variance", hidden_edge: "Hidden Edge",
};

function fillScorecardV2(node, p) {
  const block = node.querySelector(".v2-block");
  const sc = p.scorecardV2;
  if (!sc) {
    block.hidden = true;
    return;
  }
  block.hidden = false;

  const labelClass = sc.final_score >= 8.5 ? "v2-elite" : sc.final_score >= 7.5 ? "v2-strong"
    : sc.final_score >= 6.5 ? "v2-lean" : sc.final_score >= 5.5 ? "v2-neutral" : "v2-avoid";
  block.querySelector(".v2-final-score").textContent = sc.final_score.toFixed(2);
  const labelEl = block.querySelector(".v2-final-label");
  labelEl.textContent = sc.label;
  labelEl.className = `v2-final-label ${labelClass}`;
  const agreeEl = block.querySelector(".v2-agreement");
  agreeEl.textContent = sc.agreement_pct != null ? `${sc.agreement_pct}% category agreement` : "";

  const holder = block.querySelector(".v2-cat-rows");
  holder.innerHTML = "";
  Object.entries(V2_CATEGORY_LABELS).forEach(([key, label]) => {
    const cat = sc.categories[key];
    if (!cat) return;
    const pct = Math.max(0, Math.min(100, (cat.score / 10) * 100));
    const tone = cat.score >= 6.5 ? "good" : cat.score <= 3.5 ? "bad" : "mid";
    const weight = sc.weights[key];
    const row = document.createElement("div");
    row.className = "conf-row";
    row.innerHTML = `
      <span class="conf-label">${escapeHtml(label)}${weight ? ` <span class="v2-weight">(${Math.round(weight * 100)}%)</span>` : ""}</span>
      <div class="conf-track"><div class="conf-fill conf-fill-${tone}" style="width:${pct}%"></div></div>
      <span class="conf-score">${cat.score.toFixed(1)}</span>
    `;
    holder.appendChild(row);
  });

  const riskEl = block.querySelector(".v2-risk");
  if (sc.risk_penalty > 0 && sc.risk_reasons && sc.risk_reasons.length) {
    riskEl.hidden = false;
    riskEl.textContent = `Risk penalty: -${sc.risk_penalty} — ${sc.risk_reasons.join(" ")}`;
  } else {
    riskEl.hidden = true;
  }
}

function fillDistribution(node, p) {
  const block = node.querySelector(".distribution-block");
  const dist = p.distribution;
  if (!dist || !dist.buckets || !dist.buckets.length) {
    block.hidden = true;
    return;
  }
  block.hidden = false;

  block.querySelector(".distribution-sub").textContent =
    `Actual outcomes over the last ${dist.gamesSampled} games`;

  const barsHolder = block.querySelector(".distribution-bars");
  barsHolder.innerHTML = "";
  const maxPct = Math.max(...dist.buckets.map((b) => b.pct), 1);
  dist.buckets.forEach((b) => {
    const col = document.createElement("div");
    col.className = "dist-bar-col";
    const heightPct = Math.max(4, (b.pct / maxPct) * 100);
    col.innerHTML = `
      <span class="dist-bar-pct">${b.pct}%</span>
      <div class="dist-bar-track"><div class="dist-bar-fill" style="height:${heightPct}%"></div></div>
      <span class="dist-bar-label">${b.value}</span>
    `;
    barsHolder.appendChild(col);
  });

  block.querySelector(".dist-split-over").style.width = `${dist.overPct}%`;
  block.querySelector(".dist-split-over-label").textContent = `Over: ${dist.overPct}%`;
  block.querySelector(".dist-split-under-label").textContent = `Under: ${dist.underPct}%`;

  const fairEl = block.querySelector(".dist-fair-odds");
  if (dist.fairOverOdds && dist.fairUnderOdds) {
    fairEl.textContent = `Sample-implied fair odds: Over ${dist.fairOverOdds} · Under ${dist.fairUnderOdds} — from these ${dist.gamesSampled} games, not a sportsbook line.`;
    fairEl.hidden = false;
  } else {
    fairEl.hidden = true;
  }
}

function fillPitchArsenal(node, p) {
  const block = node.querySelector(".arsenal-block");
  const pitches = p.pitchArsenal || [];
  if (!pitches.length) {
    block.hidden = true;
    return;
  }
  block.hidden = false;
  block.querySelector(".arsenal-sub").textContent = p.pitchArsenalLabel || "";

  const holder = block.querySelector(".arsenal-rows");
  holder.innerHTML = "";
  const maxPct = Math.max(...pitches.map((x) => x.pct), 1);
  pitches.forEach((pitch) => {
    const row = document.createElement("div");
    row.className = "arsenal-row";
    row.innerHTML = `
      <span class="arsenal-name">${escapeHtml(pitch.name)}</span>
      <div class="arsenal-track"><div class="arsenal-fill" style="width:${(pitch.pct / maxPct) * 100}%"></div></div>
      <span class="arsenal-pct">${pitch.pct}%</span>
      <span class="arsenal-speed">${pitch.speed != null ? pitch.speed + " mph" : ""}</span>
    `;
    holder.appendChild(row);

    const vs = pitch.batterVs;
    if (vs) {
      const tierClass =
        vs.tier === "Crushes it" ? "vs-tier-elite" :
        vs.tier === "Strong" ? "vs-tier-good" :
        vs.tier === "Struggles" ? "vs-tier-bad" : "vs-tier-avg";
      const detail = document.createElement("div");
      detail.className = "arsenal-vs";
      detail.innerHTML = `
        <span class="vs-tier ${tierClass}">${escapeHtml(vs.tier)}</span>
        <span class="vs-stats">${escapeHtml(vs.avg)} AVG · ${escapeHtml(vs.slg)} SLG · ${escapeHtml(String(vs.whiffPct))}% whiff <span class="vs-pa">(${vs.pa} PA)</span></span>
      `;
      holder.appendChild(detail);
    }
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
  node.querySelector(".matchup-lineup").textContent = m.lineup || "";
  node.querySelector(".matchup-bullpen").textContent = m.bullpen || "";

  const teamBtn = node.querySelector(".team-insights-btn");
  if (p.teamInsightsParams) {
    teamBtn.hidden = false;
    teamBtn.onclick = () => openTeamModal(p.teamInsightsParams, m.opponent || "");
  } else {
    teamBtn.hidden = true;
  }
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
  const runsEl = node.querySelector(".env-runs");
  if (p.runEnvironment && p.runEnvironment.projected_runs != null) {
    const re = p.runEnvironment;
    const diff = re.projected_runs - re.season_runs_pg;
    const dirWord = diff >= 0.3 ? "above" : diff <= -0.3 ? "below" : "in line with";
    runsEl.textContent = `Team run environment: ${re.projected_runs} projected runs tonight (season avg ${re.season_runs_pg}) — ${dirWord} their own baseline vs this opposing pitching (${re.opp_blended_era} blended ERA).`;
    runsEl.hidden = false;
  } else {
    runsEl.hidden = true;
  }
  const list = node.querySelector(".risk-list");
  const signals = p.negativeSignals !== undefined ? p.negativeSignals : p.risk;
  const items = signals && signals.length ? signals : ["No major red flags in available data."];
  items.forEach((line) => {
    const li = document.createElement("li");
    li.textContent = line.replace(/\*\*(.+?)\*\*/g, "$1"); // strip Discord-style **bold**
    list.appendChild(li);
  });
}

function fillProjection(node, p) {
  const section = node.querySelector(".projection-block");
  const hasTrend = !!p.trend;
  const fc = p.floorCeiling;
  const hasFc = fc && fc.median != null;
  if (!hasTrend && !hasFc && !p.paDistribution) {
    section.hidden = true;
    return;
  }
  section.hidden = false;

  const trendEl = section.querySelector(".trend-badge");
  if (hasTrend) {
    const t = p.trend;
    const cls = t === "HOT" ? "trend-hot" : t === "COLD" ? "trend-cold" : t === "WARM" || t === "HEATING UP" ? "trend-warm" : t === "COOLING" ? "trend-cooling" : "trend-neutral";
    trendEl.textContent = t.replace("_", " ");
    trendEl.className = `trend-badge ${cls}`;
    trendEl.hidden = false;
  } else {
    trendEl.hidden = true;
  }

  section.querySelector(".fmc-floor").textContent = hasFc ? fc.floor : "—";
  section.querySelector(".fmc-median").textContent = hasFc ? fc.median : "—";
  section.querySelector(".fmc-ceiling").textContent = hasFc ? fc.ceiling : "—";

  const paWrap = section.querySelector(".pa-dist-wrap");
  if (p.paDistribution && p.paDistribution.buckets && p.paDistribution.buckets.length) {
    const pa = p.paDistribution;
    paWrap.hidden = false;
    paWrap.querySelector(".pa-dist-label").textContent = `Real plate-appearance counts, last ${pa.games_sampled} games — avg ${pa.avg_pa} PA/game.`;
    const barsEl = paWrap.querySelector(".pa-dist-bars");
    barsEl.innerHTML = "";
    pa.buckets.forEach((b) => {
      const row = document.createElement("div");
      row.className = "pa-bar-row";
      row.innerHTML = `
        <span class="pa-bar-label">${escapeHtml(b.pa)} PA</span>
        <div class="pa-bar-track"><div class="pa-bar-fill" style="width:${b.pct}%"></div></div>
        <span class="pa-bar-pct">${b.pct}%</span>
      `;
      barsEl.appendChild(row);
    });
  } else {
    paWrap.hidden = true;
  }
}

function fillBiggestEdgesRisks(node, p) {
  const section = node.querySelector(".biggest-grid");
  const edges = p.biggestEdges || [];
  const risks = p.biggestRisks || [];
  if (!edges.length && !risks.length) {
    section.hidden = true;
    return;
  }
  section.hidden = false;

  const edgesList = section.querySelector(".edges-list");
  edgesList.innerHTML = "";
  if (edges.length) {
    edges.forEach((line) => {
      const li = document.createElement("li");
      li.textContent = line;
      edgesList.appendChild(li);
    });
  } else {
    edgesList.innerHTML = `<li class="tt-nodata-inline">No standout edges beyond baseline form.</li>`;
  }

  const risksList = section.querySelector(".risks-top-list");
  risksList.innerHTML = "";
  if (risks.length) {
    risks.forEach((line) => {
      const li = document.createElement("li");
      li.textContent = line.replace(/\*\*(.+?)\*\*/g, "$1");
      risksList.appendChild(li);
    });
  } else {
    risksList.innerHTML = `<li class="tt-nodata-inline">No major red flags in available data.</li>`;
  }
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

function fillSparkline(node, entries, line) {
  const holder = node.querySelector("#sparkline-holder");
  holder.innerHTML = "";
  if (!entries.length) return;

  // Static demo data still ships as plain numbers — normalize both shapes.
  const games = entries.map((e) => (typeof e === "object" && e !== null ? e : { value: e }));
  const max = Math.max(...games.map((g) => g.value), 1);

  games.forEach((g) => {
    const col = document.createElement("div");
    col.className = "spark-col";

    const track = document.createElement("div");
    track.className = "spark-bar-track";

    const bar = document.createElement("div");
    bar.className = "spark-bar";
    // Under the line = red (a miss for the Over), at/above = normal teal.
    if (typeof line === "number" && g.value < line) bar.classList.add("spark-bar-under");

    const valEl = document.createElement("span");
    valEl.className = "spark-val";
    valEl.textContent = g.value;
    bar.appendChild(valEl);
    track.appendChild(bar);
    col.appendChild(track);

    if (g.opponent || g.date) {
      const label = document.createElement("span");
      label.className = "spark-label";
      label.innerHTML = [
        g.opponent ? `<span class="spark-opp">${escapeHtml(g.opponent)}</span>` : "",
        g.date ? `<span class="spark-date">${escapeHtml(g.date)}</span>` : "",
      ].filter(Boolean).join("<br>");
      col.appendChild(label);
    }

    holder.appendChild(col);
    const trackPx = 90;
    const heightPx = Math.max(24, (g.value / max) * trackPx);
    requestAnimationFrame(() => {
      bar.style.height = `${heightPx}px`;
    });
  });

  // Dashed marker for the actual line being researched (e.g. 0.5, 1.5) —
  // positioned against the same fixed 90px track the bars animate within.
  if (typeof line === "number") {
    const trackPx = 90;
    const topPx = Math.max(0, Math.min(trackPx, trackPx - (line / max) * trackPx));
    const marker = document.createElement("div");
    marker.className = "spark-line-marker";
    marker.style.top = `${topPx}px`;
    const tag = document.createElement("span");
    tag.className = "spark-line-tag";
    tag.textContent = line;
    marker.appendChild(tag);
    holder.appendChild(marker);
  }
}

/* ---------- Saved tab ---------- */

function wireSavedToolbar() {
  els.clearSavedBtn.addEventListener("click", () => {
    if (state.savedProps.size === 0) return;
    if (!confirm("Clear all saved props? This can't be undone.")) return;
    state.savedProps.clear();
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
  return [...state.savedProps.values()];
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
    // Clicking anywhere in the wrapping <label> (the visible checkmark,
    // not just the invisible native input) fires its OWN bubbling click
    // event in addition to the synthetic one the label dispatches on the
    // input -- stopPropagation() on the checkbox alone only silences that
    // synthetic click, not the real one from the label, so the card's
    // click-to-open handler still fired. Stop it at the label too.
    node.querySelector(".saved-select").addEventListener("click", (e) => e.stopPropagation());
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
      state.savedProps.delete(p.id);
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
  const legs = getSavedProps().filter((p) => state.parlaySelection.has(p.id));
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
