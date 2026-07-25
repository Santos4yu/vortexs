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
// The board mirrors the Discord bot's engine, whose MLB stat labels
// (backend/update_board.py MARKET_LABELS) don't all match Research's display
// stat strings 1:1 — the bot calls the pitcher-K market plain "Strikeouts",
// Research calls it "Strikeouts (Pitcher)". This maps a board prop to the
// exact Research stat string so Deep Dive opens the right dropdown option.
// Bot labels missing here (NBA/WNBA stats) simply don't offer Deep Dive —
// Research is MLB-only.
const BOT_STAT_TO_RESEARCH_STAT = {
  "Hits": "Hits",
  "Total Bases": "Total Bases",
  "Home Runs": "Home Runs",
  "RBIs": "RBIs",
  "Runs Scored": "Runs Scored",
  "Hits+Runs+RBIs": "Hits+Runs+RBIs",
  "Fantasy Score (PP)": "Fantasy Score",
  "Strikeouts": "Strikeouts (Pitcher)",
  "Outs": "Pitching Outs",
  "Hits Allowed": "Hits Allowed",
  "Earned Runs": "Earned Runs Allowed",
};
// Bot labels that belong to the pitcher pipeline (position "P" in Research).
const BOT_PITCHER_STATS = new Set(["Strikeouts", "Outs", "Hits Allowed", "Earned Runs"]);

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
  slateLoaded: false,
  v2BoardLoaded: false,
  v2BoardData: null,
  boardFilter: "all",
  specialsLoaded: false,
  specialsData: null,
  moneylineGamePk: null,
  moneylineSide: "home",
  adminRecords: null,
  adminRecordTab: "props",
  adminUnlocked: false,
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
const BOOT_MIN_MS = 1600;
let finishBootDelay = null;
applyTheme(localStorage.getItem(THEME_KEY) || "dark");
applyAccent(localStorage.getItem(ACCENT_KEY) || "amber");

init();

async function init() {
  cacheEls();
  wireBootLoading();
  try {
    await Promise.all([
      checkAuth(),
      new Promise((resolve) => { finishBootDelay = resolve; setTimeout(resolve, BOOT_MIN_MS); }),
    ]);
  } catch (err) {
    console.error("checkAuth failed:", err);
  }
  dismissBootLoading();
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
  clearIntroAnimations();
  wireSearch();
  wireLinePicker();
  renderBrowseChips();
  wireSavedToolbar();
  renderSavedGrid();
  wireSlate();
  wireV2Board();
  wireAdminPanel();
  wireSidePanel();
  wireSpecialMarkets();
  wirePlayerDetailModal();
  updateSavedCount();
  updateParlayBar();
}

function cacheEls() {
  els.searchInput = document.getElementById("search-input");
  els.searchResults = document.getElementById("search-results");
  els.reportWrap = document.getElementById("report-wrap");
  els.emptyState = document.getElementById("empty-state");
  els.browseChips = document.getElementById("browse-chips");
  els.v2BackBtn = document.getElementById("v2-back-btn");

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

  els.panelSlate = document.getElementById("panel-slate");
  els.slateList = document.getElementById("slate-list");
  els.slateEmpty = document.getElementById("slate-empty");
  els.slateLoading = document.getElementById("slate-loading");
  els.slateError = document.getElementById("slate-error");
  els.slateDate = document.getElementById("slate-date");
  els.slateRefreshBtn = document.getElementById("slate-refresh-btn");

  els.panelV2 = document.getElementById("panel-v2");
  els.panelMoneyline = document.getElementById("panel-moneyline");
  els.panelNrfi = document.getElementById("panel-nrfi");
  els.panelAdmin = document.getElementById("panel-admin");
  els.moneylineList = document.getElementById("moneyline-list");
  els.moneylineEmpty = document.getElementById("moneyline-empty");
  els.nrfiList = document.getElementById("nrfi-list");
  els.nrfiEmpty = document.getElementById("nrfi-empty");
  els.moneylineRefresh = document.getElementById("moneyline-refresh");
  els.nrfiRefresh = document.getElementById("nrfi-refresh");
  els.adminUnlock = document.getElementById("admin-unlock");
  els.adminResultTabs = document.getElementById("admin-result-tabs");
  els.adminResultsList = document.getElementById("admin-results-list");
  els.sidePanel = document.getElementById("side-panel");
  els.sideScrim = document.getElementById("side-scrim");
  els.sideMenuToggle = document.getElementById("side-menu-toggle");
  els.sideMenuClose = document.getElementById("side-menu-close");
  els.v2BoardList = document.getElementById("v2-board-list");
  els.v2BoardEmpty = document.getElementById("v2-board-empty");
  els.v2BoardLoading = document.getElementById("v2-board-loading");
  els.v2BoardError = document.getElementById("v2-board-error");
  els.v2BoardDate = document.getElementById("v2-board-date");
  els.v2RefreshBtn = document.getElementById("v2-refresh-btn");
  els.boardFilterRow = document.getElementById("board-filter-row");

  els.v2PinOverlay = document.getElementById("v2-pin-overlay");
  els.v2PinInput = document.getElementById("v2-pin-input");
  els.v2PinSubmit = document.getElementById("v2-pin-submit");
  els.v2PinClose = document.getElementById("v2-pin-close");
  els.v2PinError = document.getElementById("v2-pin-error");
  els.v2AdminOverlay = document.getElementById("v2-admin-overlay");
  els.v2AdminClose = document.getElementById("v2-admin-close");
  els.v2AdminKeyStatus = document.getElementById("v2-admin-key-status");
  els.v2AdminKeyInput = document.getElementById("v2-admin-key-input");
  els.v2AdminKeySave = document.getElementById("v2-admin-key-save");
  els.v2AdminKeyMsg = document.getElementById("v2-admin-key-msg");
  els.v2AdminScanBtn = document.getElementById("v2-admin-scan-btn");
  els.v2AdminScanMsg = document.getElementById("v2-admin-scan-msg");

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
  els.bootSkip = document.getElementById("boot-skip");
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
  els.gamelogAvatar = document.getElementById("gamelog-avatar");
  els.gamelogPropTitle = document.getElementById("gamelog-prop-title");
  els.gamelogMarket = document.getElementById("gamelog-market");
  els.gamelogLineDown = document.getElementById("gamelog-line-down");
  els.gamelogLineUp = document.getElementById("gamelog-line-up");
  els.gamelogTitle = document.getElementById("gamelog-title");
  els.gamelogPlayer = document.getElementById("gamelog-player");
  els.gamelogLine = document.getElementById("gamelog-line");
  els.gamelogClose = document.getElementById("gamelog-close");
  els.gamelogTabs = document.getElementById("gamelog-tabs");
  els.gamelogSub = document.getElementById("gamelog-sub");
  els.gamelogChart = document.getElementById("gamelog-chart");
  els.gamelogSubfilters = document.getElementById("gamelog-subfilters");
  els.glHandFilter = document.getElementById("gl-hand-filter");
  els.glVenueFilter = document.getElementById("gl-venue-filter");

  els.teamOverlay = document.getElementById("team-overlay");
  els.teamTitle = document.getElementById("team-title");
  els.teamLineupNote = document.getElementById("team-lineup-note");
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

/* ---------- Keep navigation out of the way once the reader leaves the top ---------- */

function wireChromeAutoHide() {
  let ticking = false;

  const apply = () => {
    const y = window.scrollY;
    // The menu is intentionally available only at the very top of a page.
    // It stays hidden while scrolling in either direction until the user
    // returns all the way to the top.
    els.sideMenuToggle.classList.toggle("chrome-hidden", y > 2);
    ticking = false;
  };

  window.addEventListener("scroll", () => {
    if (!ticking) {
      requestAnimationFrame(apply);
      ticking = true;
    }
  }, { passive: true });
  apply();
}

function wireSettingsPanel() {
  const savedMode = localStorage.getItem(THEME_KEY) || "dark";
  const savedAccent = localStorage.getItem(ACCENT_KEY) || "amber";
  const savedCustomHex = localStorage.getItem(CUSTOM_ACCENT_KEY) || "#35e0c4";
  applyTheme(savedMode);
  els.customAccentInput.value = savedCustomHex;
  applyAccent(savedAccent, savedCustomHex);

  // 5 rapid clicks on the settings gear opens the hidden admin PIN prompt
  // instead of the normal theme panel -- the PIN itself is never checked
  // here, only on the server (api/v2-admin-auth.py).
  let v2ClickTimes = [];
  els.settingsBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    const now = Date.now();
    // Window measured from the FIRST click in the run, not a rolling
    // per-click cutoff -- gives 5 real clicks (mouse or trackpad) a full
    // 4 seconds to land instead of resetting after any single gap.
    if (v2ClickTimes.length === 0 || now - v2ClickTimes[0] > 4000) {
      v2ClickTimes = [now];
    } else {
      v2ClickTimes.push(now);
    }
    if (v2ClickTimes.length >= 5) {
      v2ClickTimes = [];
      els.settingsPanel.hidden = true;
      openV2PinPrompt();
      return;
    }
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
  requestAnimationFrame(() => moveIndicator(els.tabs.querySelector(".tab-btn.active")));
  window.addEventListener("resize", () => moveIndicator(els.tabs.querySelector(".tab-btn.active")));

  const bottomNav = document.getElementById("bottom-nav");
  if (bottomNav) {
    bottomNav.querySelectorAll(".bottom-nav-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        switchTab(btn.dataset.tab);
      });
    });
  }

  document.querySelectorAll("[data-home-tab]").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.homeTab));
  });
}

function switchTab(tab) {
  // The Admin route is not a preview page. It never renders until the
  // server-issued PIN session exists.
  if (tab === "admin" && !state.adminUnlocked) {
    openV2PinPrompt();
    return;
  }
  state.currentTab = tab;
  els.tabs.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  const topBtn = els.tabs.querySelector(`.tab-btn[data-tab="${tab}"]`);
  if (topBtn) moveIndicator(topBtn);

  const bottomNav = document.getElementById("bottom-nav");
  if (bottomNav) {
    bottomNav.querySelectorAll(".bottom-nav-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  }

  const homeModeSwitch = document.getElementById("home-mode-switch");
  if (homeModeSwitch) {
    const isHomeMode = tab === "research" || tab === "moneyline";
    homeModeSwitch.hidden = !isHomeMode;
    homeModeSwitch.querySelectorAll("[data-home-tab]").forEach((b) => {
      b.classList.toggle("active", b.dataset.homeTab === tab);
    });
  }

  els.panelResearch.hidden = tab !== "research";
  els.panelSlate.hidden = tab !== "slate";
  els.panelV2.hidden = tab !== "v2";
  els.panelMoneyline.hidden = tab !== "moneyline";
  els.panelNrfi.hidden = tab !== "nrfi";
  els.panelAdmin.hidden = tab !== "admin";
  els.panelSaved.hidden = tab !== "saved";
  els.parlayBar.hidden = tab !== "saved" || state.parlaySelection.size === 0;

  if (tab === "saved") renderSavedGrid();
  if (tab === "slate" && !state.slateLoaded) loadSlate();
  if (tab === "v2" && !state.v2BoardLoaded) loadV2Board();
  if ((tab === "moneyline" || tab === "nrfi") && !state.specialsLoaded) loadSpecialMarkets();
  document.querySelectorAll(".side-link").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
}

function wireSidePanel() {
  els.sideMenuToggle.setAttribute("aria-controls", "side-panel");
  els.sideMenuToggle.setAttribute("aria-expanded", "false");
  const close = () => {
    els.sidePanel.hidden = true;
    els.sideScrim.hidden = true;
    els.sideMenuToggle.setAttribute("aria-expanded", "false");
    document.body.classList.remove("side-panel-open");
  };
  const open = () => {
    els.sidePanel.hidden = false;
    els.sideScrim.hidden = false;
    els.sideMenuToggle.setAttribute("aria-expanded", "true");
    document.body.classList.add("side-panel-open");
    els.sideMenuClose.focus();
  };
  els.sideMenuToggle.addEventListener("click", open);
  els.sideMenuClose.addEventListener("click", close); els.sideScrim.addEventListener("click", close);
  document.querySelectorAll(".side-link").forEach((btn) => btn.addEventListener("click", () => { switchTab(btn.dataset.tab); close(); }));
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !els.sidePanel.hidden) close(); });
}

function wireSpecialMarkets() {
  els.moneylineRefresh.addEventListener("click", () => loadSpecialMarkets(true));
  els.nrfiRefresh.addEventListener("click", () => loadSpecialMarkets(true));
  els.adminUnlock.addEventListener("click", openV2PinPrompt);
  els.adminResultTabs.querySelectorAll("button").forEach((btn) => btn.addEventListener("click", () => {
    state.adminRecordTab = btn.dataset.record;
    els.adminResultTabs.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b === btn));
    renderAdminRecords();
  }));
}

async function loadSpecialMarkets(force = false) {
  if (state.specialsLoaded && !force) return;
  try {
    const res = await fetch("/api/board?view=specials", { cache: "no-store" });
    if (!res.ok) throw new Error("Live markets unavailable");
    state.specialsData = await res.json(); state.specialsLoaded = true;
    renderSpecialMarkets();
  } catch (err) { showToast(err.message, "warn"); }
}

function renderSpecialMarkets() {
  const data = state.specialsData || {};
  const nrfis = data.nrfi || [];
  renderMoneylineResearch(data.moneyline_research || data.moneylines || []);
  els.nrfiList.innerHTML = nrfis.map((p) => `<article class="market-card"><div class="market-card-top"><span class="market-badge ${p.confidence === "LEAN" ? "lean" : ""}">${escapeHtml(p.confidence || "READY")}</span><span class="market-sport">FIRST INNING</span></div><strong>${escapeHtml(p.recommendation || "—")} <em>${escapeHtml(p.away_abbr || "?")} @ ${escapeHtml(p.home_abbr || "?")}</em></strong><p class="market-matchup">${escapeHtml(p.home_pitcher || "TBD")} vs ${escapeHtml(p.away_pitcher || "TBD")}</p><div class="market-confirmed"><span>LINEUPS</span><b>Confirmed</b><span>MODEL</span><b>${escapeHtml(String(p.model_rating || p.confidence_pct || "—"))}/100</b></div></article>`).join("");
  els.nrfiEmpty.hidden = nrfis.length > 0;
}

function moneylineValue(value, fallback = "—") { return value === null || value === undefined || value === "" ? fallback : escapeHtml(String(value)); }

function renderMoneylineResearch(games) {
  const shell = document.getElementById("moneyline-research"), picker = document.getElementById("moneyline-game-picker"), report = document.getElementById("moneyline-report");
  if (!games.length) { shell.hidden = true; picker.innerHTML = ""; report.innerHTML = ""; els.moneylineEmpty.hidden = false; return; }
  shell.hidden = false;
  els.moneylineEmpty.hidden = true;
  if (!games.some((game) => String(game.game_pk) === String(state.moneylineGamePk))) state.moneylineGamePk = games[0].game_pk;
  const game = games.find((item) => String(item.game_pk) === String(state.moneylineGamePk)) || games[0];
  const selected = state.moneylineSide === "away" ? "away" : "home", other = selected === "home" ? "away" : "home";
  const team = game[`${selected}_team`] || (selected === "home" ? game.rec_team : game.opponent), opponent = game[`${other}_team`] || (selected === "home" ? game.opponent : game.rec_team);
  const model = Number(game[`${selected}_pct`]), market = Number(game[`${selected}_market_prob`]), edge = Number.isFinite(model) && Number.isFinite(market) ? model - market : null;
  const pitcher = game[`${selected}_pitcher`] || "TBD", oppPitcher = game[`${other}_pitcher`] || "TBD", offense = game[`${selected}_offense`] || {}, bullpen = game[`${selected}_bullpen`] || {};
  const isModelSide = game.rec_is_home === (selected === "home"), readiness = game.lineups_confirmed ? "Lineups confirmed" : "Pre-lineup read";
  const factorScores = game.factor_scores || {}, factorDirection = isModelSide ? 1 : -1;
  const signed = (key) => { if (!Object.prototype.hasOwnProperty.call(factorScores, key)) return "—"; const n = Number(factorScores[key]) * factorDirection; return `${n >= 0 ? "+" : ""}${n.toFixed(1)}%`; };
  const weather = game.weather || {};
  const tier = String(game.tier || "PASS").toUpperCase();
  const actionable = game.lineups_confirmed && (tier === "LEAN" || tier === "STRONG");
  const noBetReason = !game.lineups_confirmed
    ? "Awaiting confirmed lineups and reliable starters. This is research, not a pick."
    : game.volatility === "HIGH"
      ? "No bet — game volatility is too high for a reliable moneyline call."
      : "No bet — the model has not found a qualified, actionable edge.";
  const weatherText = typeof weather === "string" ? weather : (weather.dome ? "Indoor / roof context" : [weather.temperature, weather.speed_mph ? `${weather.speed_mph} mph wind` : ""].filter(Boolean).join(" · ") || "Weather pending");
  picker.innerHTML = games.map((item) => `<button class="ml-game-choice ${String(item.game_pk) === String(game.game_pk) ? "active" : ""}" data-ml-game="${escapeHtml(String(item.game_pk))}"><span>${escapeHtml(item.away_abbr || item.away_team || "AWY")} @ ${escapeHtml(item.home_abbr || item.home_team || "HME")}</span><small>${item.lineups_confirmed ? "confirmed" : "pre-game"}</small></button>`).join("");
  report.innerHTML = `<div class="ml-matchup-head"><div><p class="eyebrow">${readiness.toUpperCase()}</p><h3>${escapeHtml(team)} <span>vs ${escapeHtml(opponent)}</span></h3><p>${escapeHtml(pitcher)} vs ${escapeHtml(oppPitcher)} · ${moneylineValue(game.commence_time, "Game time TBD")}</p></div><div class="ml-side-toggle" role="group" aria-label="Team to research"><button class="${selected === "away" ? "active" : ""}" data-ml-side="away">${escapeHtml(game.away_abbr || "Away")}</button><button class="${selected === "home" ? "active" : ""}" data-ml-side="home">${escapeHtml(game.home_abbr || "Home")}</button></div></div><div class="ml-score-row"><div class="ml-score"><span>VORTEX WIN</span><strong>${Number.isFinite(model) ? model.toFixed(1) : "—"}%</strong><small>${isModelSide ? "model-selected side" : "your selected side"}</small></div><div class="ml-score"><span>MARKET</span><strong>${Number.isFinite(market) ? market.toFixed(1) : "—"}%</strong><small>${moneylineValue(game[`${selected}_odds`], "—")} odds</small></div><div class="ml-score ${edge !== null && edge >= 0 ? "positive" : ""}"><span>PRICING GAP</span><strong>${edge === null ? "—" : `${edge >= 0 ? "+" : ""}${edge.toFixed(1)}%`}</strong><small>${edge !== null && edge >= 0 ? "model above price" : "market above model"}</small></div></div><div class="ml-evidence"><article><span>STARTER</span><strong>${escapeHtml(pitcher)}</strong><p>FIP ${moneylineValue(game[`${selected}_fip`])} · opponent: ${escapeHtml(oppPitcher)}</p></article><article><span>TEAM FORM</span><strong>${moneylineValue(game[`${selected}_record`])}</strong><p>${moneylineValue(offense.wrc_plus || offense.wrc)} wRC+ · ${moneylineValue(offense.iso)} ISO · ${moneylineValue(offense.bb_pct)}% BB</p></article><article><span>BULLPEN</span><strong>${moneylineValue(bullpen.era)} ERA</strong><p>${moneylineValue(bullpen.fatigued_count, "0")} fatigued arms · late-game context</p></article><article><span>CONTEXT</span><strong>${moneylineValue(game.park_factor)} park factor</strong><p>${escapeHtml(game.weather || "Weather not available")}</p></article></div><div class="ml-verdict"><span>${edge !== null && edge >= 0 ? "VORTEX LEAN" : "CAUTION"}</span><p>${escapeHtml(game.insight || "The model is weighing starters, team quality, bullpen condition, market price, park and game context.")}</p></div>`;
  report.querySelector(".ml-evidence article:last-child p").textContent = weatherText;
  if (!actionable) {
    const verdict = report.querySelector(".ml-verdict");
    verdict.querySelector("span").textContent = game.lineups_confirmed ? "NO BET" : "AWAITING LINEUPS";
    verdict.querySelector("p").textContent = noBetReason;
    verdict.classList.add("ml-no-bet");
    report.querySelector(".ml-score.positive")?.classList.remove("positive");
  }
  report.insertAdjacentHTML("beforeend", `<section class="ml-model-card"><div class="ml-model-head"><div><span>MODEL BREAKDOWN</span><strong>${escapeHtml(game.game_archetype || "Awaiting current model data")}</strong></div><div><span>CONFIDENCE</span><strong>${game.confidence_score === undefined ? "Awaiting refresh" : `${moneylineValue(game.confidence_score)}/100 · ${escapeHtml(game.confidence_band || "—")}`}</strong></div><div><span>VOLATILITY</span><strong class="vol-${String(game.volatility || "low").toLowerCase()}">${game.volatility_score === undefined ? "Awaiting refresh" : `${escapeHtml(game.volatility || "—")} ${moneylineValue(game.volatility_score)}/100`}</strong></div></div><div class="ml-factor-grid">${[["Pitching", "pitching"], ["Offense", "offense"], ["Bullpen", "bullpen"], ["Team form", "team_form"], ["Venue / H2H", "venue"], ["Market value", "market_value"]].map(([label, key]) => { const exists = Object.prototype.hasOwnProperty.call(factorScores, key); const value = Number(factorScores[key]) * factorDirection; return `<div><span>${label}</span><strong class="${exists && value < 0 ? "minus" : exists ? "plus" : ""}">${signed(key)}</strong></div>`; }).join("")}</div><div class="ml-action-row"><div><span>FAIR ODDS</span><strong>${moneylineValue(game.fair_odds)}</strong></div><div><span>STATUS</span><strong>${escapeHtml(game.tier || "PASS")}</strong></div><p>${(game.volatility_reasons || []).length ? escapeHtml(game.volatility_reasons.join(" · ")) : "Run a bot refresh to calculate the current scorecard."}</p></div></section>`);
  const evidence = report.querySelectorAll(".ml-evidence article");
  if (evidence[1]) {
    evidence[1].querySelector("span").textContent = "TEAM QUALITY";
    evidence[1].querySelector("p").textContent = `OPS ${moneylineValue(offense.ops)} · ISO ${moneylineValue(offense.iso)} · season context only`;
  }
  if (evidence[2]) {
    evidence[2].querySelector("p").textContent = bullpen.sample === "l7" && Number(bullpen.total_ip) >= 12
      ? `${moneylineValue(bullpen.total_ip)} relief IP · sample-qualified context`
      : "Short relief sample unavailable — not used to make a pick";
  }
  if (evidence[3]) evidence[3].querySelector("span").textContent = "CONTEXT (NOT SCORED)";

  const modelHead = report.querySelectorAll(".ml-model-head div");
  if (modelHead[1]) modelHead[1].querySelector("span").textContent = "MODEL QUALITY";

  const factorCells = [...report.querySelectorAll(".ml-factor-grid > div")];
  const modelFactors = [
    ["Pitching", "pitching"], null, ["Bullpen", "bullpen"],
    ["Team quality", "team_quality"], null, ["Market value", "market_value"],
  ];
  factorCells.forEach((cell, index) => {
    const factor = modelFactors[index];
    if (!factor) { cell.remove(); return; }
    const [label, key] = factor;
    const exists = Object.prototype.hasOwnProperty.call(factorScores, key);
    const value = Number(factorScores[key]) * factorDirection;
    cell.querySelector("span").textContent = label;
    const score = cell.querySelector("strong");
    score.textContent = signed(key);
    score.className = exists && value < 0 ? "minus" : exists ? "plus" : "";
  });

  const homeStarter = game.home_starter_profile || {}, awayStarter = game.away_starter_profile || {};
  const stat = (profile, key) => moneylineValue(profile[key]);
  const arsenal = (profile) => (profile.arsenal || []).map((pitch) => `${escapeHtml(pitch.pitch_name || pitch.pitch_type || "Pitch")} ${moneylineValue(pitch.pct)}%`).join(" · ") || "Pitch mix pending";
  const recent = (profile) => (profile.recent_starts || []).map((start) => `${escapeHtml(start.ip || "—")} IP · ${moneylineValue(start.k)} K · ${moneylineValue(start.er)} ER`).join("<br>") || "Recent-start log pending";
  report.insertAdjacentHTML("beforeend", `<section class="ml-workbench"><div class="ml-workbench-title"><div><span>RESEARCH WORKBENCH</span><h4>Compare the matchup yourself</h4></div><p>Raw data is separate from VORTEX's recommendation.</p></div><div class="ml-research-section"><h5>Starting pitchers</h5><div class="ml-compare"><article><b>${escapeHtml(game.away_abbr || "Away")} · ${escapeHtml(awayStarter.name || game.away_pitcher || "TBD")}</b><div class="ml-stat-lines"><span>ERA <strong>${stat(awayStarter,"era")}</strong></span><span>FIP <strong>${stat(awayStarter,"fip")}</strong></span><span>WHIP <strong>${stat(awayStarter,"whip")}</strong></span><span>K/9 <strong>${stat(awayStarter,"k_per_9")}</strong></span><span>BB/9 <strong>${stat(awayStarter,"bb_per_9")}</strong></span><span>HR/9 <strong>${stat(awayStarter,"hr_per_9")}</strong></span></div><p><em>Arsenal</em> ${arsenal(awayStarter)}</p><p><em>Last starts</em><br>${recent(awayStarter)}</p></article><article><b>${escapeHtml(game.home_abbr || "Home")} · ${escapeHtml(homeStarter.name || game.home_pitcher || "TBD")}</b><div class="ml-stat-lines"><span>ERA <strong>${stat(homeStarter,"era")}</strong></span><span>FIP <strong>${stat(homeStarter,"fip")}</strong></span><span>WHIP <strong>${stat(homeStarter,"whip")}</strong></span><span>K/9 <strong>${stat(homeStarter,"k_per_9")}</strong></span><span>BB/9 <strong>${stat(homeStarter,"bb_per_9")}</strong></span><span>HR/9 <strong>${stat(homeStarter,"hr_per_9")}</strong></span></div><p><em>Arsenal</em> ${arsenal(homeStarter)}</p><p><em>Last starts</em><br>${recent(homeStarter)}</p></article></div></div><div class="ml-research-section"><h5>Offense and bullpen</h5><div class="ml-compare">${[[game.away_abbr || "Away", game.away_offense || {}, game.away_bullpen || {}], [game.home_abbr || "Home", game.home_offense || {}, game.home_bullpen || {}]].map(([abbr, off, pen]) => `<article><b>${escapeHtml(abbr)}</b><div class="ml-stat-lines"><span>wRC+ <strong>${moneylineValue(off.wrc_plus)}</strong></span><span>ISO <strong>${moneylineValue(off.iso)}</strong></span><span>BB% <strong>${moneylineValue(off.bb_pct)}</strong></span><span>K% <strong>${moneylineValue(off.k_pct)}</strong></span><span>Runs/G <strong>${moneylineValue(off.runs_pg)}</strong></span><span>BP ERA <strong>${moneylineValue(pen.era)}</strong></span></div><p><em>Bullpen</em> ${moneylineValue(pen.whip)} WHIP · ${moneylineValue(pen.hr9)} HR/9 · ${moneylineValue(pen.fatigued_count,"0")} fatigued arms</p></article>`).join("")}</div></div><div class="ml-research-section"><h5>Game context</h5><div class="ml-context-data"><span>Park factor <strong>${moneylineValue(game.park_factor)}</strong></span><span>Weather <strong>${escapeHtml(weatherText)}</strong></span><span>Lineups <strong>${game.lineups_confirmed ? "Confirmed" : "Projected"}</strong></span><span>Volatility <strong>${escapeHtml(game.volatility || "—")} ${moneylineValue(game.volatility_score)}/100</strong></span></div></div></section>`);
  const offenseSection = [...report.querySelectorAll(".ml-research-section")].find((section) => section.querySelector("h5")?.textContent === "Offense and bullpen");
  if (offenseSection) {
    const cards = offenseSection.querySelectorAll(".ml-compare article");
    [[game.away_offense || {}, game.away_bullpen || {}], [game.home_offense || {}, game.home_bullpen || {}]].forEach(([off, pen], index) => {
      const card = cards[index];
      if (!card) return;
      const firstMetric = card.querySelector(".ml-stat-lines span");
      if (firstMetric) firstMetric.innerHTML = `OPS <strong>${moneylineValue(off.ops)}</strong>`;
      const bullpenNote = card.querySelector("p");
      if (bullpenNote) bullpenNote.textContent = pen.sample === "l7" && Number(pen.total_ip) >= 12
        ? `Recent relief sample: ${moneylineValue(pen.total_ip)} IP · ${moneylineValue(pen.era)} ERA`
        : "Recent relief sample is not large enough to score.";
    });
  }
  picker.querySelectorAll("[data-ml-game]").forEach((button) => button.addEventListener("click", () => { state.moneylineGamePk = button.dataset.mlGame; renderMoneylineResearch(games); }));
  report.querySelectorAll("[data-ml-side]").forEach((button) => button.addEventListener("click", () => { state.moneylineSide = button.dataset.mlSide; renderMoneylineResearch(games); }));
}

async function loadAdminRecords() {
  const res = await fetch("/api/board?view=results", { cache: "no-store", credentials: "same-origin" });
  if (!res.ok) throw new Error("Admin reporting is still locked.");
  const data = await res.json(); state.adminRecords = data.records || {}; state.adminUnlocked = true;
  els.adminResultTabs.hidden = false; els.adminResultsList.hidden = false; renderAdminRecords();
}

function renderAdminRecords() {
  const rows = (state.adminRecords || {})[state.adminRecordTab] || [];
  const settled = rows.filter((r) => r.result === "hit" || r.result === "miss");
  const wins = settled.filter((r) => r.result === "hit").length;
  const rate = settled.length ? `${Math.round((wins / settled.length) * 100)}%` : "—";
  const summary = `<div class="record-summary"><span><b>${wins}-${Math.max(0, settled.length - wins)}</b>record</span><span><b>${rate}</b>hit rate</span><span><b>${settled.length}</b>settled</span></div>`;
  els.adminResultsList.innerHTML = rows.length ? summary + rows.map((r) => {
    const hit = r.result === "hit"; const label = state.adminRecordTab === "props" ? `${r.player_name} · ${r.side} ${r.line} ${r.stat_type}` : state.adminRecordTab === "moneyline" ? `${r.rec_team} vs ${r.opponent} · ${r.odds}` : `${r.recommendation} · ${r.away_abbr} @ ${r.home_abbr}`;
    const outcome = state.adminRecordTab === "nrfi" && r.result ? `${hit ? "Hit" : "Miss"} · ${r.first_inning_away_runs ?? "?"}-${r.first_inning_home_runs ?? "?"} after 1` : hit ? "Hit" : r.result === "miss" ? "Miss" : "Pending";
    return `<div class="admin-result-row ${hit ? "hit" : r.result === "miss" ? "miss" : ""}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(outcome)}</strong></div>`;
  }).join("") : "<p class=\"empty-state\">No settled results in this tab yet.</p>";
}

// The search bar / browse chips play a one-time fade-in (`.intro-anim`,
// opacity:0 + `animation ... forwards`) on first load. If that animation is
// ever re-triggered later -- which happens because the research tab panel
// toggles display:none/block when switching tabs -- mobile Safari can drop
// the replay and leave the element stuck invisible at its pre-animation
// opacity:0 until something else forces a re-render. Stripping the class
// once the intro has played means later tab switches never touch the
// animation system again, so there's nothing left to get stuck.
function clearIntroAnimations() {
  setTimeout(() => {
    document.querySelectorAll(".intro-anim").forEach((el) => el.classList.remove("intro-anim"));
  }, 1200);
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

function wireBootLoading() {
  els.bootSkip.addEventListener("click", () => {
    if (finishBootDelay) finishBootDelay();
  });
}

function dismissBootLoading() {
  if (els.bootLoading.hidden) return;
  els.bootLoading.classList.add("boot-leaving");
  setTimeout(() => { els.bootLoading.hidden = true; }, 420);
}

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
  const bc = document.getElementById("bottom-saved-count");
  if (bc) {
    bc.textContent = state.savedProps.size;
    bc.hidden = state.savedProps.size === 0;
  }
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
  searchDebounceTimer = setTimeout(() => fetchLiveSuggestions(query), 120);
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
function selectPlayer(player, position, { autoSelectStat = true, viaDeepDive = false } = {}) {
  if (!viaDeepDive) {
    state.v2DeepDiveReturn = null;
    els.v2BackBtn.hidden = true;
  }
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
  let min = Number(els.lineSlider.min);
  let max = Number(els.lineSlider.max);
  // The slider's min/max is only a starting guess (STAT_DEFAULT_LINE, or the
  // range around whatever static lines happen to be loaded) -- it's often
  // nowhere near a given player's actual line (e.g. a default guess of 8.5
  // for Fantasy Score puts the floor at 3.5, but plenty of real lines sit
  // well under that). Typing the real sportsbook line or stepping past
  // either edge should widen the range to fit it, never silently reject it.
  const snapped = Math.max(0, Math.round(value * 2) / 2); // snap to 0.5, floor at 0
  if (snapped < min) {
    min = Math.max(0, snapped - 1);
    els.lineSlider.min = String(min);
    els.lineNumber.min = String(min);
  }
  if (snapped > max) {
    max = snapped + 1;
    els.lineSlider.max = String(max);
    els.lineNumber.max = String(max);
  }
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
    <div class="skeleton-status" role="status">
      <span class="analysis-orbit"><i></i></span>
      <div><span class="analysis-kicker">Live analysis</span><strong>Building ${escapeHtml(player)}’s ${escapeHtml(stat)} read</strong></div>
    </div>
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
  // Server no-game messages are already complete sentences written for the
  // user ("...is currently in the minors (Nashville Sounds) — no MLB game to
  // research.") — show those as-is. Anything without terminal punctuation is
  // a raw failure ("Failed to fetch", "Request failed (500)") that still
  // needs framing.
  els.lineNoData.innerHTML = liveError
    ? (/[.!?]$/.test(liveError.trim())
        ? escapeHtml(liveError.trim())
        : `Live lookup failed for ${escapeHtml(side)} ${line} — ${escapeHtml(liveError)}.`)
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
    expandBtn.addEventListener("click", () => openGameLogPage(p));
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
  handFilter: "all", venueFilter: "all", handDataLoaded: false, teamId: null, side: "Over",
};

function openGameLogPage(p) {
  gameLogState.chart = p.gameLogChart || {};
  gameLogState.line = p.line;
  gameLogState.player = p.player;
  gameLogState.opponent = (p.matchup && p.matchup.opponent) || "";
  gameLogState.handFilter = "all";
  gameLogState.venueFilter = "all";
  gameLogState.side = p.side || "Over";
  gameLogState.handDataLoaded = false;
  // Deliberately NOT teamInsightsParams.teamId -- that's the player's own
  // team (for the Team Insights lineup view), while H2H filtering here needs
  // the actual opponent's team id.
  gameLogState.teamId = p.opponentTeamId || null;
  // Default to the widest window that actually has data, so a prop with
  // only 5 games logged doesn't open on an empty L10 tab.
  gameLogState.window = ["l10", "l5", "l15", "l20"].find((w) => (gameLogState.chart[w] || []).length > 0) || "l10";

  els.gamelogOverlay.hidden = false;
  document.body.classList.add("gamelog-page-open");
  els.gamelogPlayer.textContent = p.player;
  els.gamelogAvatar.innerHTML = avatarHtml(p, "lg");
  els.gamelogPropTitle.textContent = p.betType;
  els.gamelogMarket.textContent = p.betType;
  els.gamelogLine.textContent = p.line;
  document.querySelectorAll("[data-gl-side]").forEach((button) => button.classList.toggle("active", button.dataset.glSide === gameLogState.side));
  history.pushState({ vortexView: "game-log" }, "", `#history/${encodeURIComponent(p.id || `${p.player}-${p.betType}`)}`);
  els.gamelogTitle.textContent = `${p.betType} · ${(p.matchup && p.matchup.opponent) || "Performance history"}`;
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
  document.body.classList.remove("gamelog-page-open");
  if (location.hash.startsWith("#history/")) {
    history.replaceState({}, "", `${location.pathname}${location.search}`);
  }
}

function filterGames(games) {
  return games.filter((g) => {
    if (gameLogState.handFilter !== "all" && g.oppHand !== gameLogState.handFilter) return false;
    if (gameLogState.venueFilter === "home" && g.isHome !== true) return false;
    if (gameLogState.venueFilter === "road" && g.isHome !== false) return false;
    return true;
  });
}

function gameClearsLine(game) {
  return gameLogState.side === "Under" ? game.value < gameLogState.line : game.value > gameLogState.line;
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
    const overCount = games.filter(gameClearsLine).length;
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
  const overCount = games.filter(gameClearsLine).length;
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
        <div class="gl-bar${gameClearsLine(g) ? "" : " gl-bar-under"}" style="height:${heightPx}px">
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
  const refreshGameLog = () => {
    els.gamelogLine.textContent = gameLogState.line.toFixed(1);
    renderGameLogTabs();
    renderGameLogChart();
  };
  els.gamelogLineDown.addEventListener("click", () => { gameLogState.line = Math.max(0, gameLogState.line - 0.5); refreshGameLog(); });
  els.gamelogLineUp.addEventListener("click", () => { gameLogState.line += 0.5; refreshGameLog(); });
  document.querySelectorAll("[data-gl-side]").forEach((button) => button.addEventListener("click", () => {
    gameLogState.side = button.dataset.glSide;
    document.querySelectorAll("[data-gl-side]").forEach((item) => item.classList.toggle("active", item === button));
    refreshGameLog();
  }));
  els.gamelogOverlay.addEventListener("click", (e) => {
    if (e.target === els.gamelogOverlay) closeGameLogModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !els.gamelogOverlay.hidden) closeGameLogModal();
  });
  window.addEventListener("popstate", () => {
    if (!els.gamelogOverlay.hidden) closeGameLogModal();
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
    els.teamLineupNote.hidden = true;
    els.orderEmpty.hidden = false;
    els.orderEmpty.textContent = "Loading roster…";
    els.orderTbody.innerHTML = "";
    els.arsenalEmpty.hidden = false;
    els.arsenalEmpty.textContent = "Loading roster…";
    els.arsenalTbody.innerHTML = "";
    return;
  }
  if (data.error || !data.battingOrder || !data.battingOrder.length) {
    els.teamLineupNote.hidden = true;
    const msg = data.error ? `Couldn't load roster — ${data.error}` : "No roster data available for this team.";
    els.orderEmpty.hidden = false;
    els.orderEmpty.textContent = msg;
    els.orderTbody.innerHTML = "";
    els.arsenalEmpty.hidden = false;
    els.arsenalEmpty.textContent = msg;
    els.arsenalTbody.innerHTML = "";
    return;
  }

  els.teamLineupNote.hidden = data.lineupConfirmed !== false;

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
  fillResearchSnapshot(node, p);
  fillResearchHealth(node, p);
  fillPlayability(node, p);
  fillDecisionPanel(node, p);
  fillFormLadder(node, p);
  fillOpportunity(node, p);
  fillKPath(node, p);
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

function fillResearchSnapshot(node, p) {
  const meta = p.researchMeta || {};
  const fc = p.floorCeiling || {};
  const projection = fc.median != null ? fc.median : (p.estHitRate != null ? `${p.estHitRate}%` : "—");
  const l10 = p.hitRates?.l10 != null ? `${p.hitRates.l10}%` : "—";
  const recent = fc.median != null ? `Med ${fc.median}` : "Live log";
  const matchup = p.matchup?.opponent || "—";
  const trend = p.trend || meta.status || "—";
  node.querySelector(".snapshot-projection").textContent = projection;
  node.querySelector(".snapshot-l10").textContent = l10;
  node.querySelector(".snapshot-avg").textContent = recent;
  node.querySelector(".snapshot-matchup").textContent = matchup;
  node.querySelector(".snapshot-trend").textContent = trend.replaceAll("_", " ");
}

function fillResearchHealth(node, p) {
  const block = node.querySelector(".research-health-block");
  const meta = p.researchMeta;
  if (!meta) { block.hidden = true; return; }
  block.hidden = false;
  const status = String(meta.status || "LIMITED");
  const statusEl = block.querySelector(".research-health-status");
  statusEl.textContent = status === "FULL" ? "FULL COVERAGE" : status === "LIMITED" ? "LIMITED COVERAGE" : "PENDING DATA";
  statusEl.className = `research-health-status health-${status.toLowerCase()}`;
  block.querySelector(".research-health-rate").textContent = meta.exactLineRate != null ? `${meta.exactLineRate}%` : "—";
  const range = Array.isArray(meta.historicalRange) ? ` · uncertainty ${meta.historicalRange[0]}–${meta.historicalRange[1]}%` : "";
  block.querySelector(".research-health-label").textContent = `${meta.exactLineLabel || "Exact-line history"} · ${meta.sampleGames || 0} games${range}`;
  block.querySelector(".research-health-note").textContent = meta.methodNote || "";

  const sources = block.querySelector(".research-health-sources");
  sources.innerHTML = (meta.sources || []).map((source) => `<span>${escapeHtml(source)}</span>`).join("");
  const limits = block.querySelector(".research-health-limits");
  const limitations = meta.limitations || [];
  limits.innerHTML = limitations.map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  limits.hidden = limitations.length === 0;
}

function fillPlayability(node, p) {
  const meta = p.researchMeta || {};
  const block = node.querySelector(".playability-block");
  const lineup = p.matchup?.lineup;
  const starter = p.matchup?.pitcher;
  const chips = [
    [lineup ? "Lineup confirmed" : "Lineup pending", !!lineup],
    [starter && !starter.includes("not announced") ? "Starter confirmed" : "Starter pending", !!starter && !starter.includes("not announced")],
    [`${meta.sampleGames || 0}-game exact-line sample`, (meta.sampleGames || 0) >= 12],
    [meta.limitations?.length ? "Research has limits" : "Research complete", !meta.limitations?.length],
  ];
  block.querySelector(".playability-label").textContent = meta.status === "FULL" ? "READY" : "CHECK CONTEXT";
  block.querySelector(".playability-chips").innerHTML = chips.map(([label, good]) => `<span class="${good ? "ready" : "pending"}">${good ? "●" : "○"} ${escapeHtml(label)}</span>`).join("");
}

function fillDecisionPanel(node, p) {
  const meta = p.researchMeta || {};
  const rate = meta.exactLineRate ?? p.estHitRate;
  node.querySelector(".decision-probability").textContent = rate != null ? `${rate}% historical exact-line rate` : "Evidence still building";
  node.querySelector(".decision-detail").textContent = meta.historicalRange ? `Observed range: ${meta.historicalRange[0]}–${meta.historicalRange[1]}% across ${meta.sampleGames} games.` : "Use the full evidence card before deciding.";
  const marketValue = p.marketProb ?? p.trueProb;
  node.querySelector(".decision-market-value").textContent = marketValue != null ? `${Math.round(marketValue * 100)}% fair probability` : "Price not attached";
  node.querySelector(".decision-market-note").textContent = marketValue != null ? "No-vig market context" : "Research-only card — not a priced recommendation.";
}

function fillFormLadder(node, p) {
  const block = node.querySelector(".form-ladder-block");
  const games = p.gameLogChart?.l10 || [];
  if (!games.length) { block.hidden = true; return; }
  block.hidden = false;
  block.querySelector(".form-ladder-note").textContent = `Line ${p.line}`;
  block.querySelector(".form-ladder").innerHTML = games.map((g) => `<div class="form-game ${g.over ? "hit" : "miss"}" title="${escapeHtml(g.date || "Game")} vs ${escapeHtml(g.opponent || "")}: ${g.value}"><strong>${g.value}</strong><span>${escapeHtml(g.opponent || "—")}</span></div>`).join("");
}

function fillOpportunity(node, p) {
  const block = node.querySelector(".opportunity-block");
  const isPitcher = /Pitcher|Pitching Outs|Hits Allowed|Earned Runs/.test(p.betType || "");
  if (isPitcher) { block.hidden = true; return; }
  const m = p.matchup || {}, re = p.runEnvironment || {};
  const rows = [
    ["Batting order", m.lineup || "Not confirmed"],
    ["Starter", m.pitcher || "Pending"],
    ["Bullpen", m.bullpen || "Not available"],
    ["Team environment", re.projected_runs != null ? `${re.projected_runs} projected runs` : "Not available"],
  ];
  block.hidden = false;
  block.querySelector(".opportunity-grid").innerHTML = rows.map(([label, value]) => `<div><span>${label}</span><strong>${escapeHtml(String(value))}</strong></div>`).join("");
}

function fillKPath(node, p) {
  const block = node.querySelector(".k-path-block");
  const isK = (p.betType || "").includes("Strikeouts (Pitcher)");
  if (!isK) { block.hidden = true; return; }
  const stats = p.seasonStats || p.season_stats || {};
  const seasonK9 = stats.k_per_9 ?? "—";
  const projection = p.projKs ?? p.proj_ks ?? p.floorCeiling?.median ?? "—";
  const opp = p.oppKpct ?? p.opp_kpct;
  block.hidden = false;
  block.querySelector(".k-path-equation").textContent = `${projection} projected Ks · ${seasonK9} K/9 · ${p.avgIp ?? p.avg_ip ?? "—"} projected innings`;
  const rows = [["Opponent K%", opp != null ? `${opp}%` : "Pending"], ["Recent K/9", p.recentK9 ?? p.recent_k9 ?? "—"], ["Umpire", p.umpName ?? p.ump_name ?? "Pending"], ["Line", p.line]];
  block.querySelector(".k-path-grid").innerHTML = rows.map(([label, value]) => `<div><span>${label}</span><strong>${escapeHtml(String(value))}</strong></div>`).join("");
}

function fillWhyItHits(node, p) {
  const list = node.querySelector(".why-list");
  (p.whyItHits || []).forEach((line) => {
    const li = document.createElement("li");
    const emoji = line.match(/^[🔥❄️📊📈📉⚠️✅❌🎯💪🧠🎲🏆💡⚡️🌊🌪️🏠✈️🤝]/) ? "" : "✅ ";
    li.textContent = emoji + line;
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
  node.querySelector(".road-note").textContent = split.roadOverRate != null ? `📈 ${split.roadOverRate}% over rate` : "";
  node.querySelector(".home-avg").textContent = split.homeAvg != null ? `${split.homeAvg}` : "—";
  node.querySelector(".home-note").textContent = split.homeOverRate != null ? `📈 ${split.homeOverRate}% over rate` : "";
  node.querySelector(".split-callout").textContent = split.callout ? `🔍 ${split.callout}` : "";
  node.querySelector(".volume-note").textContent = split.volume ? `📊 ${split.volume}` : "";
}

function fillMatchup(node, p) {
  const m = p.matchup || {};
  node.querySelector(".matchup-opp").textContent = m.opponent ? `⚔️ Opponent: ${m.opponent}` : "";
  node.querySelector(".matchup-pitcher").textContent = m.pitcher ? `⚾ ${m.pitcher}` : "";

  const bvpEl = node.querySelector(".matchup-bvp");
  if (m.bvp) {
    bvpEl.innerHTML = `<b>🤝 BvP:</b> ${escapeHtml(m.bvp)}${m.bvpNote ? "<br>" + escapeHtml(m.bvpNote) : ""}`;
    bvpEl.hidden = false;
  } else {
    bvpEl.hidden = true;
  }

  node.querySelector(".matchup-leash").textContent = m.leash ? `🪢 ${m.leash}` : "";
  node.querySelector(".matchup-handedness").textContent = m.handedness ? `🖐️ ${m.handedness}` : "";
  node.querySelector(".matchup-lineup").textContent = m.lineup ? `📋 ${m.lineup}` : "";
  node.querySelector(".matchup-bullpen").textContent = m.bullpen ? `🛡️ ${m.bullpen}` : "";

  const teamBtn = node.querySelector(".team-insights-btn");
  if (p.teamInsightsParams) {
    teamBtn.hidden = false;
    // teamInsightsTeamName always names whichever team teamInsightsParams.teamId
    // points at -- the opposing lineup for a pitcher prop, this player's own
    // team for a batter prop. Don't assume it's m.opponent either way.
    teamBtn.textContent = "👀 View Batting Order & Pitch Arsenal →";
    teamBtn.onclick = () => openTeamModal(p.teamInsightsParams, p.teamInsightsTeamName || "");
  } else {
    teamBtn.hidden = true;
  }
}

function fillNarrative(node, p) {
  node.querySelector(".narrative-text").textContent = p.narrative ? `🏆 ${p.narrative}` : "";
}

function fillPerformance(node, p) {
  node.querySelector(".perf-season").textContent = p.seasonLine ? `📈 ${p.seasonLine}` : "";
}

function fillVsMatchup(node, p) {
  const vs = p.vsMatchup || {};

  const h2hEl = node.querySelector(".vs-h2h");
  if (vs.h2h) {
    h2hEl.innerHTML = `<b>🔄 H2H:</b> ${escapeHtml(vs.h2h)}${vs.h2hNote ? "<br>" + escapeHtml(vs.h2hNote) : ""}`;
    h2hEl.hidden = false;
  } else {
    h2hEl.hidden = true;
  }

  node.querySelector(".vs-career").textContent = vs.career ? `📊 ${vs.career}` : "";
  node.querySelector(".vs-season").textContent = vs.season ? `📈 ${vs.season}` : "";
}

function fillEnvRisk(node, p) {
  node.querySelector(".env-text").textContent = p.environment ? `🌤️ ${p.environment}` : "";
  node.querySelector(".env-wind").textContent = p.wind ? `💨 ${p.wind}` : "";
  const runsEl = node.querySelector(".env-runs");
  if (p.runEnvironment && p.runEnvironment.projected_runs != null) {
    const re = p.runEnvironment;
    const diff = re.projected_runs - re.season_runs_pg;
    const dirWord = diff >= 0.3 ? "above" : diff <= -0.3 ? "below" : "in line with";
    runsEl.textContent = `🏟️ Team run environment: ${re.projected_runs} projected runs tonight (season avg ${re.season_runs_pg}) — ${dirWord} their own baseline vs this opposing pitching (${re.opp_blended_era} blended ERA).`;
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
    const trendEmoji = t === "HOT" ? "🔥 " : t === "COLD" ? "❄️ " : t === "WARM" || t === "HEATING UP" ? "🌡️ " : t === "COOLING" ? "🧊 " : "";
    trendEl.textContent = trendEmoji + t.replace("_", " ");
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
    const related = saved.filter((other) => other.id !== p.id && (other.player === p.player || (p.team && other.team === p.team)));
    const correlation = node.querySelector(".saved-correlation");
    correlation.textContent = related.length ? `↗ ${related.length} related saved leg${related.length === 1 ? "" : "s"}` : "";
    correlation.hidden = related.length === 0;
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

/* ---------- Slate (Attack Board) ---------- */

const SLATE_BULLPEN_ICON = { ELITE: "🛡️", SOLID: "✓", AVERAGE: "~", WEAK: "💥", UNKNOWN: "?" };

function wireSlate() {
  els.slateRefreshBtn.addEventListener("click", () => loadSlate(true));
  const tools = document.querySelectorAll(".tool-tab");
  const labels = {
    attack: "Attack Board — starting-pitcher and bullpen vulnerability, ranked from the batter's side.",
    parks: "Best Ballparks — live park factors and venue run environment.",
    weather: "Weather + Park — live wind, temperature, roof status, and park context.",
    platoon: "Platoon Matchups — confirmed handedness matchups and season split context.",
    bvp: "BvP Matchups — career batter-versus-pitcher history, only where the sample is meaningful.",
    strikeouts: "Strikeout Spots — pitcher K skill, opposing lineup K rate, and projected workload."
  };
  tools.forEach((button) => button.addEventListener("click", () => {
    tools.forEach((item) => item.classList.toggle("active", item === button));
    const tool = button.dataset.tool;
    els.slateDate.textContent = labels[tool] || labels.attack;
    if (tool === "attack") {
      els.slateList.hidden = false;
      els.slateError.hidden = true;
      loadSlate();
      return;
    }
    els.slateList.innerHTML = "";
    els.slateList.hidden = true;
    els.slateEmpty.hidden = true;
    els.slateLoading.hidden = true;
    els.slateError.hidden = false;
    els.slateError.className = "tools-source-note";
    els.slateError.textContent = "Loading live MLB data…";
    fetch(`/api/slate?tool=${encodeURIComponent(tool)}`, {cache:"no-store"}).then(r => r.json()).then(data => {
      const rows = data.entries || [];
      els.slateError.innerHTML = rows.length ? rows.map(renderToolCard).join("") : `<strong>${button.textContent}</strong><span>No qualifying live data is available yet. No substitute list is shown.</span>`;
    }).catch(() => { els.slateError.textContent = "Live MLB data could not be loaded for this tool."; });
  }));
}

function renderToolCard(row) {
  const evidence = (row.evidence || []).map(item => `<div class="tool-evidence"><span>${escapeHtml(item.label)}</span><strong>${escapeHtml(item.value)}</strong><small>${escapeHtml(item.detail)}</small></div>`).join("");
  return `<article class="tool-result tool-${escapeHtml(row.tone || "neutral")}">
    <header><div><span class="tool-kicker">${escapeHtml(row.badge || "Live research")}</span><strong>${escapeHtml(row.title)}</strong></div></header>
    <p class="tool-summary">${escapeHtml(row.summary || row.detail || "")}</p>
    <div class="tool-evidence-grid">${evidence}</div>
    ${row.caution ? `<p class="tool-caution">${escapeHtml(row.caution)}</p>` : ""}
  </article>`;
}

async function loadSlate(force = false) {
  if (state.slateLoaded && !force) return;

  els.slateLoading.hidden = false;
  els.slateEmpty.hidden = true;
  els.slateError.hidden = true;
  els.slateList.innerHTML = "";

  try {
    const res = await fetch("/api/slate", { cache: "no-store" });
    const data = await res.json();
    els.slateLoading.hidden = true;

    if (!res.ok || data.error) {
      els.slateError.textContent = data.error || `Request failed (${res.status})`;
      els.slateError.hidden = false;
      return;
    }

    state.slateLoaded = true;
    renderSlate(data);
  } catch (err) {
    els.slateLoading.hidden = true;
    els.slateError.textContent = err.message || "Failed to load the slate.";
    els.slateError.hidden = false;
  }
}

function renderSlate(data) {
  const entries = data.entries || [];
    els.slateDate.textContent = entries.length
      ? `📅 ${data.date_label || data.date || "Today"} — easiest matchups on top (vulnerable pitcher + bullpen), hardest at the bottom. Click a matchup to see how the opposing lineup hits vs that pitcher.`
      : "Today's starting-pitcher matchups, easiest to hardest.";

  if (entries.length === 0) {
    els.slateEmpty.hidden = false;
    return;
  }

  els.slateList.innerHTML = "";
  entries.forEach((e, i) => {
    const row = document.createElement("div");
    row.className = "slate-row";
    row.style.animationDelay = `${i * 35}ms`;

    // Higher score = more vulnerable pitcher/bullpen = easier matchup for
    // hitters -- green. Lower score = tougher pitcher -- red.
    const difficultyClass = e.score >= 18 ? "slate-easy" : e.score >= 11 ? "slate-medium" : "slate-hard";
    const difficultyEmoji = e.score >= 18 ? "🟢" : e.score >= 11 ? "🟡" : "🔴";
    const rankEmoji = i === 0 ? "🥇" : i === 1 ? "🥈" : i === 2 ? "🥉" : `${String(i + 1).padStart(2, "0")}`;
    const bpIcon = SLATE_BULLPEN_ICON[e.bullpen_tier] || "❓";
    const bpText = e.bullpen_known
      ? `${bpIcon} ${e.bullpen_tier} (${e.bullpen_era.toFixed(2)} ERA)`
      : `${bpIcon} unknown`;

    row.innerHTML = `
      <span class="slate-rank">${rankEmoji}</span>
      <span class="slate-score-badge ${difficultyClass}">${difficultyEmoji} ${e.score.toFixed(1)}</span>
      <span class="slate-main">
        <span class="slate-pitcher">👤 ${escapeHtml(e.pitcher)} <span class="slate-hand">(${escapeHtml(e.hand)})</span>${e.team ? ` · ${escapeHtml(e.team)}` : ""}</span>
        <span class="slate-sub">⚔️ vs ${escapeHtml(e.opponent_abbr || e.opponent)} · 📊 ERA ${e.era.toFixed(2)} · HR/9 ${e.hr9.toFixed(2)} · K/9 ${e.k9.toFixed(2)} · 🛡️ ${bpText}</span>
      </span>
    `;
    row.addEventListener("click", () => {
      openTeamModal(
        {
          teamId: e.opponent_team_id,
          pitcherId: e.pitcher_id,
          pitcherName: e.pitcher,
          pitcherHand: e.hand,
        },
        e.opponent
      );
    });
    els.slateList.appendChild(row);
  });
}

/* ---------- Props Board (Discord bot mirror) ----------
   The board IS the Discord bot's board: backend/update_board.py mirrors its
   props_board table to the KV store after every engine run, /api/board reads
   it back, and this renders the same fields the bot's /menu embed shows —
   tier, Vortex Score, EV, hit-rate windows, pitcher matchup, BvP, risk. */

function wireV2Board() {
  els.v2RefreshBtn.addEventListener("click", () => loadV2Board(true));
  els.boardFilterRow.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-board-filter]");
    if (!btn) return;
    state.boardFilter = btn.dataset.boardFilter;
    els.boardFilterRow.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b === btn));
    if (state.v2BoardData) renderBotBoard(state.v2BoardData);
  });

  els.v2BoardList.addEventListener("click", (e) => {
    const btn = e.target.closest(".v2-deepdive-btn");
    if (!btn) return;
    e.stopPropagation(); // don't also toggle the row's own open/close
    const p = (state.v2BoardData?.props || [])[Number(btn.dataset.v2Idx)];
    if (!p) return;
    if (btn.textContent.includes("View Details")) {
      openPlayerDetail(p);
    } else {
      deepDiveIntoBotProp(p);
    }
  });

  els.v2BackBtn.addEventListener("click", () => {
    const returnScroll = state.v2DeepDiveReturn?.scrollY;
    switchTab("v2", document.querySelector('.tab-btn[data-tab="v2"]'));
    els.v2BackBtn.hidden = true;
    state.v2DeepDiveReturn = null;
    if (typeof returnScroll === "number") window.scrollTo(0, returnScroll);
  });
}

async function loadV2Board(force = false) {
  if (state.v2BoardLoaded && !force) return;

  els.v2BoardLoading.hidden = false;
  els.v2BoardEmpty.hidden = true;
  els.v2BoardError.hidden = true;
  els.v2BoardList.innerHTML = "";

  try {
    const res = await fetch("/api/board", { cache: "no-store" });
    const data = await res.json();
    els.v2BoardLoading.hidden = true;

    if (!res.ok || data.error) {
      els.v2BoardError.textContent = data.error || `Request failed (${res.status})`;
      els.v2BoardError.hidden = false;
      return;
    }

    state.v2BoardLoaded = true;
    state.v2BoardData = data;
    renderBotBoard(data);
  } catch (err) {
    els.v2BoardLoading.hidden = true;
    els.v2BoardError.textContent = err.message || "Failed to load the board.";
    els.v2BoardError.hidden = false;
  }
}

// Same tiers (and roughly the same badges) the Discord bot renders.
const BOT_TIER = {
  ELITE:  { badge: "💎 ELITE",  cls: "tier-elite" },
  STRONG: { badge: "💠 STRONG", cls: "tier-strong" },
  GOOD:   { badge: "🔷 GOOD",   cls: "tier-good" },
  LEAN:   { badge: "🔹 LEAN",   cls: "tier-lean" },
  RISKY:  { badge: "⚠️ RISKY",  cls: "tier-risky" },
  FADE:   { badge: "⛔ FADE",   cls: "tier-risky" },
};

// Rows with no stats-tier (no pitcher match / stats card unavailable) never
// show as bare "UNRATED" on the bot — it falls back to a score-based emoji
// (see _score_emoji in vortex.py). Mirror that exact scale here instead of
// inventing a separate "unrated" concept the bot doesn't have.
function botScoreBadge(score) {
  const s = Number(score);
  if (!Number.isFinite(s)) return { badge: "⚪ —", cls: "tier-none" };
  if (s >= 10) return { badge: `💎 ${s}`, cls: "tier-elite" };
  if (s >= 6)  return { badge: `🔥 ${s}`, cls: "tier-strong" };
  if (s >= 3)  return { badge: `✅ ${s}`, cls: "tier-good" };
  if (s >= 0)  return { badge: `➡️ ${s}`, cls: "tier-lean" };
  return { badge: `⚠️ ${s}`, cls: "tier-risky" };
}

const SPORT_EMOJI = { MLB: "⚾", NBA: "🏀", WNBA: "🏀", NFL: "🏈", NHL: "🏒" };

function fmtBotEv(p) {
  // The engine stores EV 0.0 when there was no real two-sided de-vig
  // (stats.ev_real=false) — show n/a instead of a fake +0.0%.
  if (p.stats && p.stats.ev_real === false) return "EV n/a";
  const ev = Number(p.ev_percentage) || 0;
  return `EV ${ev >= 0 ? "+" : ""}${ev.toFixed(1)}%`;
}

function renderBotBoard(data) {
  const allProps = (data.props || []).map((p, sourceIndex) => ({ ...p, _boardIndex: sourceIndex }))
    .filter((p) => p.stats && p.stats.player_id);
  const props = allProps.filter((p) => {
    const filter = state.boardFilter;
    if (filter === "all") return true;
    if (filter === "ready") return ["ELITE", "STRONG"].includes(p.tier);
    return (p.stat_type || "").toLowerCase().includes(filter);
  });
  els.v2BoardDate.textContent = props.length
    ? `${props.length} prop${props.length === 1 ? "" : "s"} · updated ${data.generated_at ? new Date(data.generated_at).toLocaleString() : "recently"}`
    : "VORTEX ACTIVE BOARD — data-driven props: filtered, scored, ranked.";
  els.v2BoardEmpty.textContent =
    "The board is empty right now — it fills as soon as the data engine runs (backend/update_board.py).";
  els.v2BoardList.innerHTML = "";

  if (props.length === 0) {
    els.v2BoardEmpty.hidden = false;
    return;
  }
  els.v2BoardEmpty.hidden = true;

  let lastMatchup = "";
  props.forEach((p, i) => {
    const row = document.createElement("div");
    row.className = "slate-row v2-row v2-card";
    row.style.animationDelay = `${i * 35}ms`;
    row.setAttribute("role", "button");
    row.setAttribute("tabindex", "0");
    row.setAttribute("aria-expanded", "false");

    const stats = p.stats || {};
    const sidePfx = stats.side === "under" ? "U" : "O";
    const tier = BOT_TIER[p.tier] || botScoreBadge(p.vortex_score);
    const sportTag = `${SPORT_EMOJI[p.sport] || "🎯"} ${p.sport || ""}`;

    const playerId = stats.player_id || "";
    let headshotUrl = "";
    if (playerId) {
      if (p.sport === "NBA") {
        headshotUrl = `https://cdn.nba.com/headshots/nba/latest/1040x760/${playerId}.png`;
      } else {
        headshotUrl = `https://img.mlbstatic.com/mlb-photos/image/upload/w_120,q_auto:best/v1/people/${playerId}/headshot/silo/current`;
      }
    }
    const sportEmoji = p.sport === "NBA" ? "🏀" : "⚾";
    const headshotHtml = headshotUrl
      ? `<img class="v2-card-headshot" src="${headshotUrl}" alt="" loading="lazy" onerror="this.src='https://img.mlbstatic.com/mlb-photos/image/upload/w_120,q_auto:best/v1/people/${playerId}/headshot/67/current'; this.onerror=null;" />`
      : `<div class="v2-card-headshot v2-card-headshot-fallback">${sportEmoji}</div>`;

    const opponent = stats.opponent || stats.matchup?.opponent || "";
    const gameTime = p.commence_time ? new Date(p.commence_time) : null;
    const timeStr = gameTime ? gameTime.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "";
    const isHome = stats.is_home;
    const matchupLine = opponent ? (isHome ? `vs ${opponent}` : `@ ${opponent}`) : "";

    const matchupKey = `${matchupLine}|${timeStr}`;
    if (matchupKey && matchupKey !== lastMatchup) {
      const group = document.createElement("div");
      group.className = "v2-game-group";
      group.textContent = `${matchupLine || "Matchup"}${timeStr ? ` · ${timeStr}` : ""}`;
      els.v2BoardList.appendChild(group);
      lastMatchup = matchupKey;
    }

    const evText = fmtBotEv(p);

    row.innerHTML = `
      ${headshotHtml}
      <span class="v2-card-body">
        <span class="v2-card-top">
          <span class="v2-card-name">${escapeHtml(p.player_name)}</span>
          <span class="v2-card-score">${escapeHtml(tier.badge)}</span>
        </span>
        <span class="v2-card-bottom">
          <span class="v2-card-matchup">${matchupLine}${timeStr ? ` · ${timeStr}` : ""}</span>
          <span class="v2-card-prop">${sidePfx} ${p.line} ${escapeHtml(p.stat_type)}</span>
          <span class="v2-card-ev">${escapeHtml(evText)}</span>
          <span class="v2-card-confidence">VORTEX ${escapeHtml(String(p.vortex_score ?? "—"))} · ${escapeHtml(stats.splits?.l10?.rate != null ? `${stats.splits.l10.rate}% L10` : "sample pending")}</span>
        </span>
      </span>
      <span class="v2-chevron" aria-hidden="true">▾</span>
    `;
    const toggle = () => {
      const open = row.classList.toggle("v2-open");
      row.setAttribute("aria-expanded", String(open));
      let detail = row.nextElementSibling;
      if (open) {
        if (!detail || !detail.classList.contains("v2-detail")) {
          detail = document.createElement("div");
          detail.className = "v2-detail";
          detail.innerHTML = buildBotDetailHtml(p, p._boardIndex);
          row.after(detail);
        }
        detail.hidden = false;
      } else if (detail && detail.classList.contains("v2-detail")) {
        detail.hidden = true;
      }
    };
    row.addEventListener("click", toggle);
    row.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
    });
    els.v2BoardList.appendChild(row);
  });
}

/* Expanded card — Silas-style emoji-rich format */
function buildBotDetailHtml(p, i) {
  const stats = p.stats || {};
  const splits = stats.splits || {};
  const pitcher = stats.pitcher || {};
  const bvp = stats.bvp || null;
  const sidePfx = stats.side === "under" ? "Under" : "Over";

  const hitEmoji = (rate) => (rate >= 80 ? "🔥" : rate >= 60 ? "✅" : rate >= 40 ? "⚠️" : "❌");
  const fmtRate = (r) =>
    r && typeof r.rate === "number"
      ? `${hitEmoji(r.rate)} ${r.rate}% (${r.hits}/${r.games})`
      : "n/a";

  // Trend line
  const streak = (splits.l5 && splits.l5.streak) || 0;
  const seasonAvg = splits.season_avg != null ? splits.season_avg : null;
  const l5Rate = splits.l5 && typeof splits.l5.rate === "number" ? splits.l5.rate : null;
  const l20Rate = splits.l20 && typeof splits.l20.rate === "number" ? splits.l20.rate : null;
  const trendLine = l5Rate != null && l20Rate != null
    ? `📈 Trending ${l5Rate > l20Rate ? "up" : l5Rate < l20Rate ? "down" : "steady"} — ${l5Rate}% L5 vs ${l20Rate}% L20.`
    : "";

  // Streak line
  const streakLine = streak >= 4
    ? `🔥 Active ${streak}-game hit streak.`
    : streak <= -3
    ? `⚠️ ${Math.abs(streak)}-game miss streak.`
    : "";

  // Season avg line
    const seasonLine = seasonAvg != null
    ? `📊 Season avg ${seasonAvg} over ${splits.games_played ?? "—"} games.`
    : "";

  let html = `
    <div class="v2-detail-section">
      <div class="v2-detail-title">The edge</div>
      <p class="v2-detail-text"><strong>${escapeHtml(fmtBotEv(p))}</strong> at ${escapeHtml(p.sportsbook || "best price")}${typeof stats.best_odds === "number" ? ` (${stats.best_odds > 0 ? "+" : ""}${stats.best_odds})` : ""}.</p>
    </div>
    <div class="v2-decision-strip">
      <span><b>WHY</b>${escapeHtml(String(p.case_summary || "Model and matchup signals aligned.").slice(0, 145))}</span>
      <span class="risk"><b>RISK</b>${escapeHtml(String(p.risk_summary || "No major conflict reported.").slice(0, 120))}</span>
      <span><b>MARKET</b>${stats.market_movement ? escapeHtml(String(stats.market_movement)) : "Movement unavailable — not used in score."}</span>
    </div>`;

  // Hit rates section with emojis
  const hasSplits = ["l5", "l10", "l20"].some((k) => splits[k] && typeof splits[k].rate === "number");
  if (hasSplits) {
    const l5 = splits.l5 && typeof splits.l5.rate === "number" ? `${splits.l5.rate}% L5` : "";
    const l10 = splits.l10 && typeof splits.l10.rate === "number" ? `${splits.l10.rate}% L10` : "";
    const l20 = splits.l20 && typeof splits.l20.rate === "number" ? `${splits.l20.rate}% L20` : "";
    const hitParts = [l5, l10, l20].filter(Boolean).join(" · ");
    const avgVal = splits.l10 && splits.l10.avg != null ? splits.l10.avg : null;
    const summaryLine = `📊 ${hitParts}${avgVal != null ? ` · avg ${avgVal}` : ""}`;

    html += `
    <div class="v2-detail-section">
      <p class="v2-detail-text">${escapeHtml(summaryLine)}</p>
      ${streakLine ? `<p class="v2-detail-text">${streakLine}</p>` : ""}
      ${trendLine ? `<p class="v2-detail-text">${escapeHtml(trendLine)}</p>` : ""}
      ${seasonLine ? `<p class="v2-detail-text">${seasonLine}</p>` : ""}
    </div>`;
  } else if (p.case_summary) {
    html += `
    <div class="v2-detail-section">
      <p class="v2-detail-text">📊 ${escapeHtml(p.case_summary)}</p>
    </div>`;
  }

  // Matchup with pitcher
  if (pitcher.name) {
    const platoon = stats.platoon_note ? `\n🤝 Platoon edge — ${escapeHtml(stats.platoon_note)}.` : "";
    html += `
    <div class="v2-detail-section">
      <div class="v2-detail-title">Matchup</div>
      <p class="v2-detail-text">🎯 <strong>${escapeHtml(pitcher.name)}</strong> (${escapeHtml(pitcher.hand ?? "?")}HP) — ERA ${pitcher.era ?? "—"} · FIP ${pitcher.fip ?? "—"} · K/9 ${pitcher.k_per_9 ?? "—"} · HR/9 ${pitcher.hr_per_9 ?? "—"} · WHIP ${pitcher.whip ?? "—"}${platoon}</p>
    </div>`;
  }

  // BvP
  if (bvp && bvp.ab >= 5) {
    html += `
    <div class="v2-detail-section">
      <p class="v2-detail-text">🤝 <strong>BvP (${escapeHtml(bvp.sample ?? "career")}):</strong> ${bvp.ab} AB · AVG <strong>${bvp.avg}</strong> · ${bvp.hr} HR · ${bvp.k} K · OPS ${bvp.ops}</p>
    </div>`;
  }

  // Trend
  if (stats.trend_signal) {
    const t = String(stats.trend_signal);
    const icon = t.includes("HOT") ? "🔥" : t.includes("COLD") ? "❄️" : "📈";
    html += `
    <div class="v2-detail-section">
      <p class="v2-detail-text">${icon} <strong>Trend:</strong> ${escapeHtml(t)}</p>
    </div>`;
  }

  // Risk
  if (p.risk_summary) {
    html += `
    <div class="v2-detail-section">
      <p class="v2-detail-text" style="color:var(--warn)">⚠️ <strong>Risk:</strong> ${escapeHtml(p.risk_summary)}</p>
    </div>`;
  }

  const canDeepDive = p.sport === "MLB" && BOT_STAT_TO_RESEARCH_STAT[p.stat_type];
  const detailBtns = [];
  detailBtns.push(`<button type="button" class="v2-deepdive-btn" data-v2-idx="${i}" style="margin-right:8px">View Details</button>`);
  if (canDeepDive) {
    detailBtns.push(`<button type="button" class="v2-deepdive-btn" data-v2-idx="${i}">Deep Dive →</button>`);
  }
  html += `<div style="display:flex;gap:8px;flex-wrap:wrap">${detailBtns.join("")}</div>`;
  return html;
}

/* Sends a board card's exact player/stat/line/side into the Research tab for
   the full breakdown, and remembers where to snap back to so "Back to Props"
   isn't a dead end. MLB only — Research has no NBA/WNBA pipeline. */
function deepDiveIntoBotProp(p) {
  const stat = BOT_STAT_TO_RESEARCH_STAT[p.stat_type];
  if (!stat || p.sport !== "MLB") return;

  state.v2DeepDiveReturn = { scrollY: window.scrollY };
  els.v2BackBtn.hidden = false;

  const isPitcher = BOT_PITCHER_STATS.has(p.stat_type);
  selectPlayer(p.player_name, isPitcher ? "P" : null, { autoSelectStat: false, viaDeepDive: true });
  selectStat(stat);

  const side = p.stats && p.stats.side === "under" ? "Under" : "Over";
  cmd.side = side;
  els.sideToggle.querySelectorAll(".side-btn").forEach((b) => b.classList.toggle("active", b.dataset.side === side));
  setLineValue(p.line, { immediate: true });

  switchTab("research", document.querySelector('.tab-btn[data-tab="research"]'));
}

/* ---------- V2 Admin panel (hidden, PIN-gated) ---------- */

function openV2PinPrompt() {
  els.v2PinInput.value = "";
  els.v2PinError.hidden = true;
  els.v2PinOverlay.hidden = false;
  els.v2PinInput.focus();
}

function openAdminPanel() {
  els.v2AdminOverlay.hidden = false;
  els.v2AdminKeyMsg.textContent = "";
  els.v2AdminScanMsg.textContent = "";
  refreshAdminKeyStatus();
}

// The admin endpoint always tries to answer in JSON, but a hard platform
// failure (function crash before our handler runs, gateway timeout) comes
// back as Vercel's plain-text error page — res.json() on that throws
// "Unexpected token 'A' …". Fall back to a readable error object instead.
async function readAdminJson(res) {
  try {
    return await res.json();
  } catch {
    return { error: `Server error (HTTP ${res.status}) — check the Vercel function logs.` };
  }
}

async function refreshAdminKeyStatus() {
  els.v2AdminKeyStatus.textContent = "Checking…";
  try {
    const res = await fetch("/api/v2-admin?action=key-status", { credentials: "same-origin" });
    const data = await readAdminJson(res);
    if (!res.ok) {
      els.v2AdminKeyStatus.textContent = data.error || "Could not check key.";
      return;
    }
    if (!data.keySet) {
      els.v2AdminKeyStatus.textContent = "No key set.";
    } else if (data.valid) {
      els.v2AdminKeyStatus.textContent = `Active — ${data.requests_remaining} credits remaining.`;
    } else {
      els.v2AdminKeyStatus.textContent = `Current key is invalid: ${data.error || ""}`;
    }
  } catch (err) {
    els.v2AdminKeyStatus.textContent = "Could not check key.";
  }
}

function wireAdminPanel() {
  els.v2PinClose.addEventListener("click", () => { els.v2PinOverlay.hidden = true; });
  els.v2AdminClose.addEventListener("click", () => { els.v2AdminOverlay.hidden = true; });

  const submitPin = async () => {
    const pin = els.v2PinInput.value.trim();
    if (!pin) return;
    try {
      const res = await fetch("/api/v2-admin", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ action: "auth", pin }),
      });
      const data = await readAdminJson(res);
      if (!res.ok || !data.ok) {
        els.v2PinError.textContent = data.error || "Incorrect PIN";
        els.v2PinError.hidden = false;
        return;
      }
      els.v2PinOverlay.hidden = true;
      try {
        await loadAdminRecords();
        switchTab("admin");
      } catch (err) {
        showToast(err.message || "Could not load admin results.", "warn");
      }
    } catch (err) {
      els.v2PinError.textContent = err.message || "Request failed";
      els.v2PinError.hidden = false;
    }
  };
  els.v2PinSubmit.addEventListener("click", submitPin);
  els.v2PinInput.addEventListener("keydown", (e) => { if (e.key === "Enter") submitPin(); });

  els.v2AdminKeySave.addEventListener("click", async () => {
    const key = els.v2AdminKeyInput.value.trim();
    if (!key) return;
    els.v2AdminKeyMsg.textContent = "Testing…";
    els.v2AdminKeyMsg.style.color = "";
    try {
      const res = await fetch("/api/v2-admin", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ action: "key", key }),
      });
      const data = await readAdminJson(res);
      if (!res.ok || !data.saved) {
        els.v2AdminKeyMsg.textContent = data.error || "Key rejected.";
        els.v2AdminKeyMsg.style.color = "#e05d5d";
        return;
      }
      els.v2AdminKeyInput.value = "";
      els.v2AdminKeyMsg.textContent = `Saved. ${data.requests_remaining} credits remaining.`;
      els.v2AdminKeyMsg.style.color = "#35e0c4";
      refreshAdminKeyStatus();
    } catch (err) {
      els.v2AdminKeyMsg.textContent = err.message || "Request failed";
      els.v2AdminKeyMsg.style.color = "#e05d5d";
    }
  });

  els.v2AdminScanBtn.addEventListener("click", async () => {
    els.v2AdminScanBtn.disabled = true;
    els.v2AdminScanMsg.textContent = "Scanning — this can take a minute or two…";
    els.v2AdminScanMsg.style.color = "";
    try {
      const res = await fetch("/api/v2-admin", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ action: "scan" }),
      });
      const data = await readAdminJson(res);
      els.v2AdminScanBtn.disabled = false;
      if (!res.ok || !data.ok) {
        els.v2AdminScanMsg.textContent = data.error || "Scan failed.";
        els.v2AdminScanMsg.style.color = "#e05d5d";
        return;
      }
      els.v2AdminScanMsg.textContent = `Done — ${data.n_props} props found, ${data.n_bait ?? 0} bait props flagged.`;
      els.v2AdminScanMsg.style.color = "#35e0c4";
      state.v2BoardLoaded = false;
      if (state.currentTab === "v2") loadV2Board(true);
    } catch (err) {
      els.v2AdminScanBtn.disabled = false;
      els.v2AdminScanMsg.textContent = err.message || "Request failed";
      els.v2AdminScanMsg.style.color = "#e05d5d";
    }
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

/* ---------- Player Detail Modal (Silas-style) ---------- */

let pdState = { prop: null, tab: "play" };

function openPlayerDetail(p) {
  pdState.prop = p;
  pdState.tab = "play";

  const overlay = document.getElementById("player-detail-overlay");
  overlay.hidden = false;

  // Hero image
  const heroImg = document.getElementById("pd-hero-img");
  const heroContent = document.getElementById("pd-hero-content");
  const playerId = p.stats?.player_id || "";
  if (playerId) {
    if (p.sport === "NBA") {
      heroImg.src = `https://cdn.nba.com/headshots/nba/latest/1040x760/${playerId}.png`;
    } else {
      heroImg.src = `https://img.mlbstatic.com/mlb-photos/image/upload/w_300,q_auto:best/v1/people/${playerId}/headshot/silo/current`;
    }
    heroImg.alt = p.player_name;
    heroImg.onerror = () => {
      heroImg.onerror = null;
      heroImg.src = `https://img.mlbstatic.com/mlb-photos/image/upload/w_300,q_auto:best/v1/people/${playerId}/headshot/67/current`;
    };
    heroImg.style.display = "";
  } else {
    heroImg.style.display = "none";
  }

  // Tier badge
  const tier = BOT_TIER[p.tier] || botScoreBadge(p.vortex_score);
  document.getElementById("pd-tier-badge").textContent = tier.badge;

  // Name + matchup
  document.getElementById("pd-player-name").textContent = p.player_name;
  const opponent = p.stats?.opponent || p.stats?.matchup?.opponent || "";
  const isHome = p.stats?.is_home;
  const matchupStr = opponent ? (isHome ? `vs ${opponent}` : `@ ${opponent}`) : "";
  const sidePfx = p.stats?.side === "under" ? "U" : "O";
  document.getElementById("pd-matchup").textContent = `${matchupStr}${matchupStr ? " · " : ""}${sidePfx} ${p.line} ${p.stat_type}`;

  // The play tab
  renderPdPlayTab(p);
  renderPdBreakdownTab(p);
  updatePdTabs();
}

function renderPdPlayTab(p) {
  const section = document.getElementById("pd-play-section");
  const stats = p.stats || {};
  const splits = stats.splits || {};
  const pitcher = stats.pitcher || {};
  const bvp = stats.bvp || null;
  const sidePfx = stats.side === "under" ? "Under" : "Over";

  document.getElementById("pd-play-line").textContent = `${sidePfx} ${p.line} ${p.stat_type}`;
  const evText = fmtBotEv(p);
  document.getElementById("pd-play-meta").textContent = `${evText} · ${p.sportsbook || "best price"}`;

  // Case text
  let caseHtml = "";
  const hitEmoji = (rate) => (rate >= 80 ? "🔥" : rate >= 60 ? "✅" : rate >= 40 ? "⚠️" : "❌");
  const fmtRate = (r) => r && typeof r.rate === "number" ? `${hitEmoji(r.rate)} ${r.rate}% (${r.hits}/${r.games})` : "n/a";

  const hasSplits = ["l5", "l10", "l20"].some((k) => splits[k] && typeof splits[k].rate === "number");
  if (hasSplits) {
    caseHtml += `<p style="margin:0 0 8px"><strong>Hit Rates</strong></p>`;
    caseHtml += `<p style="margin:0 0 4px">L5: ${fmtRate(splits.l5)} · L10: ${fmtRate(splits.l10)} · L20: ${fmtRate(splits.l20)}</p>`;
    const streak = splits.l5?.streak || 0;
    if (streak >= 4) caseHtml += `<p style="margin:6px 0 0;color:var(--good)">🔥 ${streak}-game hit streak active</p>`;
    else if (streak <= -3) caseHtml += `<p style="margin:6px 0 0;color:var(--bad)">⚠️ ${Math.abs(streak)}-game miss streak</p>`;
    const seasonAvg = splits.season_avg != null ? splits.season_avg : null;
    if (seasonAvg != null) caseHtml += `<p style="margin:6px 0 0;color:var(--text-dim)">📊 Season avg: ${seasonAvg}</p>`;
  } else if (p.case_summary) {
    caseHtml = `<p style="margin:0">${escapeHtml(p.case_summary)}</p>`;
  }

  document.getElementById("pd-case").innerHTML = caseHtml;
}

function renderPdBreakdownTab(p) {
  const section = document.getElementById("pd-breakdown-section");
  const stats = p.stats || {};
  const pitcher = stats.pitcher || {};
  const splits = stats.splits || {};
  let html = "";

  if (p.hitRates && (p.hitRates.l5 || p.hitRates.l10 || p.hitRates.l20)) {
    html += `<div class="pd-breakdown-section-inner">`;
    html += `<p style="margin:0 0 8px"><strong>📊 Hit Rates</strong></p>`;
    if (p.hitRates.l5 != null) html += `<p style="margin:0 0 2px">🔥 L5: <strong>${p.hitRates.l5}%</strong></p>`;
    if (p.hitRates.l10 != null) html += `<p style="margin:0 0 2px">📈 L10: <strong>${p.hitRates.l10}%</strong></p>`;
    if (p.hitRates.l20 != null) html += `<p style="margin:0 0 8px">📉 L20: <strong>${p.hitRates.l20}%</strong></p>`;
    html += `</div>`;
  }

  if (p.last5 && p.last5.length) {
    html += `<p style="margin:8px 0 4px"><strong>🎯 Last 5 Outcomes</strong></p>`;
    const vals = p.last5.map((e) => typeof e === "object" && e !== null ? e.value : e);
    const over = p.side === "Over" || p.side === "over";
    const tagged = vals.map((v) => {
      const hit = over ? v >= p.line : v <= p.line;
      return `<span style="color:${hit ? "var(--success)" : "var(--danger)"};font-weight:600">${v}</span>`;
    });
    html += `<p style="margin:0 0 8px;font-size:14px;letter-spacing:1px">${tagged.join(" → ")}</p>`;
  }

  if (pitcher.name) {
    html += `<p style="margin:0 0 8px"><strong>🎯 Pitcher Matchup</strong></p>`;
    html += `<p style="margin:0 0 4px"><strong>${escapeHtml(pitcher.name)}</strong> (${escapeHtml(pitcher.hand ?? "?")}HP) — ERA ${pitcher.era ?? "—"} · K/9 ${pitcher.k_per_9 ?? "—"} · HR/9 ${pitcher.hr_per_9 ?? "—"} · WHIP ${pitcher.whip ?? "—"}</p>`;
    if (stats.platoon_note) html += `<p style="margin:4px 0 0">🎯 Platoon: ${escapeHtml(stats.platoon_note)}</p>`;
  }

  const bvp = stats.bvp || null;
  if (bvp && bvp.ab >= 5) {
    html += `<p style="margin:8px 0 0"><strong>🤝 BvP:</strong> ${bvp.ab} AB · AVG <strong>${bvp.avg}</strong> · ${bvp.hr} HR · OPS ${bvp.ops}</p>`;
  }

  if (stats.trend_signal) {
    const t = String(stats.trend_signal);
    const icon = t.includes("HOT") ? "🔥" : t.includes("COLD") ? "❄️" : "📊";
    html += `<p style="margin:8px 0 0"><strong>📈 Trend:</strong> ${icon} ${escapeHtml(t)}</p>`;
  }

  if (p.seasonLine) {
    html += `<p style="margin:8px 0 0"><strong>📋 Season Line:</strong> ${escapeHtml(p.seasonLine)}</p>`;
  }

  if (p.risk_summary) {
    html += `<p style="margin:8px 0 0;color:var(--warn)"><strong>⚠️ Risk:</strong> ${escapeHtml(p.risk_summary)}</p>`;
  }

  if (p.matchup && p.matchup.bullpen) {
    html += `<p style="margin:8px 0 0"><strong>🛡️ Bullpen:</strong> ${escapeHtml(p.matchup.bullpen)}</p>`;
  }
  if (p.matchup && p.matchup.handedness) {
    html += `<p style="margin:4px 0 0"><strong>🖐️ Hand:</strong> ${escapeHtml(p.matchup.handedness)}</p>`;
  }

  if (!html) html = `<p style="color:var(--text-faint)">No breakdown data available.</p>`;
  section.innerHTML = html;
}

function updatePdTabs() {
  document.querySelectorAll(".pd-tab").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.pdTab === pdState.tab);
  });
  document.getElementById("pd-play-section").hidden = pdState.tab !== "play";
  document.getElementById("pd-breakdown-section").hidden = pdState.tab !== "breakdown";
}

function wirePlayerDetailModal() {
  const overlay = document.getElementById("player-detail-overlay");
  document.getElementById("pd-close-btn").addEventListener("click", () => { overlay.hidden = true; });
  overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.hidden = true; });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !overlay.hidden) overlay.hidden = true;
  });

  document.querySelectorAll(".pd-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      pdState.tab = btn.dataset.pdTab;
      updatePdTabs();
    });
  });

  // Tail = save the prop
  document.getElementById("pd-tail-btn").addEventListener("click", () => {
    if (!pdState.prop) return;
    const p = pdState.prop;
    // Build a saved-compatible prop from the board row
    const savedProp = {
      id: `board-${p.player_name}-${p.stat_type}-${p.line}`,
      player: p.player_name,
      betType: p.stat_type,
      line: p.line,
      side: p.stats?.side === "under" ? "Under" : "Over",
      team: p.stats?.team || "",
      sport: p.sport || "MLB",
      score: p.vortex_score,
      tierIcon: (BOT_TIER[p.tier] || botScoreBadge(p.vortex_score)).badge,
      estHitRate: p.stats?.splits?.l10?.rate,
    };
    toggleSave(savedProp, document.getElementById("pd-tail-btn"));
  });
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
