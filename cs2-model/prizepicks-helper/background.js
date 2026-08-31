let jobRunning = false;

chrome.runtime.onMessage.addListener((message) => {
  if (message?.type !== "START_HISTORY_JOB") return;
  startHistoryJob(message.players || [], message.target || "http://localhost:3000");
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "cs2-history-batch") processHistoryBatch();
});

async function startHistoryJob(players, target) {
  if (jobRunning || !players.length) return;
  const work = { players, target, index: 0, matched: 0, rows: [], lastProblem: "" };
  await chrome.storage.local.set({ historyWork: work, historyJob: { status: "running", total: players.length, index: 0, matched: 0, rows: 0, current: "Starting" } });
  processHistoryBatch();
}

async function processHistoryBatch() {
  if (jobRunning) return;
  const saved = await chrome.storage.local.get(["historyWork"]);
  const work = saved.historyWork;
  if (!work?.players?.length || work.index >= work.players.length) return;
  jobRunning = true;
  let workTab = null;
  const batchEnd = Math.min(work.index + 10, work.players.length);
  try {
    workTab = await chrome.tabs.create({ url: "about:blank", active: false });
    while (work.index < batchEnd) {
      const player = work.players[work.index];
      await chrome.storage.local.set({ historyJob: { status: "running", total: work.players.length, index: work.index + 1, matched: work.matched, rows: work.rows.length, current: player } });
      try {
        await navigateAndWait(workTab.id, `https://www.hltv.org/search?query=${encodeURIComponent(player)}`);
        const searchReady = await waitForHltvContent(workTab.id, "search");
        if (!searchReady.ready) work.lastProblem = searchReady.problem;
        else {
          const [{ result: historyUrl }] = await chrome.scripting.executeScript({ target: { tabId: workTab.id }, func: findHistoryUrl, args: [player] });
          if (!historyUrl) work.lastProblem = `HLTV search did not return a player profile for ${player}.`;
          else {
            await navigateAndWait(workTab.id, historyUrl);
            const historyReady = await waitForHltvContent(workTab.id, "history");
            if (!historyReady.ready) work.lastProblem = historyReady.problem;
            else {
              const [{ result: rows = [] }] = await chrome.scripting.executeScript({ target: { tabId: workTab.id }, func: parseHistoryPage, args: [player] });
              if (rows.length) { work.matched += 1; work.rows.push(...rows); }
              else work.lastProblem = `HLTV opened ${player}'s history, but its table format was not recognized.`;
            }
          }
        }
      } catch (error) { work.lastProblem = `${player}: ${error?.message || "history collection failed"}`; }
      work.index += 1;
      await chrome.storage.local.set({ historyWork: work });
    }
  } finally {
    if (workTab?.id) chrome.tabs.remove(workTab.id).catch(() => {});
    jobRunning = false;
  }
  if (work.index < work.players.length) {
    await chrome.storage.local.set({ historyWork: work, historyJob: { status: "running", total: work.players.length, index: work.index, matched: work.matched, rows: work.rows.length, current: "Saving progress and continuing" } });
    await chrome.alarms.create("cs2-history-batch", { when: Date.now() + 1500 });
  } else if (work.rows.length) {
    await chrome.storage.local.set({ pendingHistory: work.rows, historyWork: null, historyJob: { status: "complete", total: work.players.length, index: work.players.length, matched: work.matched, rows: work.rows.length, message: "Finished. Opening saved history in CS2 Prop Lab." } });
    await chrome.tabs.create({ url: `${work.target}/?historyExtensionImport=1` });
  } else {
    await chrome.storage.local.set({ historyWork: null, historyJob: { status: "failed", total: work.players.length, index: work.players.length, matched: 0, rows: 0, message: work.lastProblem || "HLTV pages loaded, but no supported player rows were found." } });
  }
}

async function navigateAndWait(tabId, url) {
  const waiting = waitForTab(tabId);
  await chrome.tabs.update(tabId, { url });
  await waiting;
}

async function waitForHltvContent(tabId, kind, timeoutMs = 15000) {
  const started = Date.now();
  let lastTitle = "";
  while (Date.now() - started < timeoutMs) {
    try {
      const [{ result }] = await chrome.scripting.executeScript({
        target: { tabId },
        func: (expectedKind) => {
          const title = document.title || "";
          const body = (document.body?.innerText || "").slice(0, 2000);
          const challenged = /just a moment|security verification|verify you are human|checking your browser/i.test(`${title} ${body}`);
          const ready = expectedKind === "history"
            ? document.querySelectorAll("table tr").length > 2 && /match history|K\s*-\s*D/i.test(body)
            : document.querySelectorAll("a[href*='/player/'], a[href*='/stats/players/']").length > 0;
          return { title, challenged, ready };
        },
        args: [kind]
      });
      lastTitle = result?.title || lastTitle;
      if (result?.ready) return { ready: true, problem: "" };
      if (!result?.challenged && Date.now() - started > 5000) break;
    } catch { /* navigation may still be replacing the document */ }
    await new Promise((resolve) => setTimeout(resolve, 750));
  }
  return { ready: false, problem: /just a moment/i.test(lastTitle) ? "HLTV security verification did not finish. Keep one normal HLTV tab open, complete its check, then retry." : `HLTV returned ${lastTitle || "a page"}, but the expected ${kind} content was missing.` };
}

function waitForTab(tabId, timeoutMs = 20000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { chrome.tabs.onUpdated.removeListener(listener); reject(new Error("Page timeout")); }, timeoutMs);
    function listener(updatedId, info) {
      if (updatedId !== tabId || info.status !== "complete") return;
      clearTimeout(timer); chrome.tabs.onUpdated.removeListener(listener); setTimeout(resolve, 350);
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

function findHistoryUrl(playerName) {
  const key = (value) => String(value || "").toLowerCase().normalize("NFKD").replace(/[^a-z0-9]/g, "");
  const links = [...document.querySelectorAll("a[href]")];
  const exact = links.find((link) => {
    const match = (link.getAttribute("href") || "").match(/\/(?:stats\/players|player)\/(\d+)\/([^/?#]+)/i);
    return match && key(match[2]) === key(playerName);
  });
  const fallback = exact || links.find((link) => /\/(?:stats\/players|player)\/\d+\//i.test(link.getAttribute("href") || "") && key(link.textContent).includes(key(playerName)));
  const match = (fallback?.getAttribute("href") || "").match(/\/(?:stats\/players|player)\/(\d+)\/([^/?#]+)/i);
  return match ? `https://www.hltv.org/stats/players/matches/${match[1]}/${match[2]}` : null;
}

function parseHistoryPage(playerName) {
  const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
  const key = (value) => clean(value).toLowerCase().normalize("NFKD").replace(/[^a-z0-9]/g, "");
  const mapCodes = new Set(["anc","anb","cch","d2","inf","mrg","nuke","ovp","trn","vtg","mirage","inferno","ancient","anubis","dust2","overpass","train","vertigo","cache"]);
  const playerId = location.pathname.match(/\/matches\/(\d+)\//)?.[1] || key(playerName);
  const parsed = [];
  for (const row of [...document.querySelectorAll("table tr")].slice(0, 120)) {
    const cells = [...row.querySelectorAll("td")].map((cell) => clean(cell.innerText || cell.textContent)).filter(Boolean);
    const date = cells.find((cell) => /^\d{1,2}\/\d{1,2}\/\d{2,4}$/.test(cell));
    const kd = cells.find((cell) => /^\d{1,3}\s*-\s*\d{1,3}$/.test(cell));
    const mapName = cells.find((cell) => mapCodes.has(key(cell)) || mapCodes.has(cell.toLowerCase()));
    if (!date || !kd || !mapName) continue;
    const middle = cells.slice(cells.indexOf(date) + 1, cells.indexOf(mapName));
    const teamCells = middle.filter((cell) => /\(\d{1,2}\)/.test(cell));
    const scores = teamCells.map((cell) => Number(cell.match(/\((\d{1,2})\)/)?.[1] || 0));
    const names = teamCells.map((cell) => cell.replace(/\s*\(\d{1,2}\)\s*/, "").trim()).filter(Boolean);
    if (scores.length < 2) scores.push(...middle.filter((cell) => /^\d{1,2}$/.test(cell)).map(Number).slice(0, 2));
    if (names.length < 2) {
      const imageNames = [...row.querySelectorAll("img[alt]")].map((image) => clean(image.getAttribute("alt"))).filter((name) => name && !/^(logo|flag)$/i.test(name));
      names.push(...imageNames.filter((name) => !names.includes(name)).slice(0, 2 - names.length));
    }
    const [kills] = kd.split("-").map((value) => Number(value.trim()));
    const link = row.querySelector("a[href*='mapstatsid']")?.getAttribute("href") || "";
    const sourceMapId = link.match(/mapstatsid\/(\d+)/i)?.[1] || `${playerId}-${date}-${parsed.length}`;
    const [day, month, year] = date.split("/");
    parsed.push({ source: "hltv", sourceMapId, playedAt: `${year.length === 2 ? `20${year}` : year}-${month.padStart(2,"0")}-${day.padStart(2,"0")}`, player: playerName, team: names[0] || "", opponent: names[1] || "", mapName, kills, rounds: (scores[0] || 0) + (scores[1] || 0), sourceUrl: `https://www.hltv.org${link}` });
  }
  const groups = new Map();
  for (const row of parsed) { const groupKey = `${row.playedAt}|${key(row.team)}|${key(row.opponent)}`; groups.set(groupKey, [...(groups.get(groupKey) || []), row]); }
  const output = [];
  for (const [groupKey, newestFirst] of groups) [...newestFirst].reverse().forEach((row, index) => output.push({ ...row, matchKey: `hltv-${playerId}-${groupKey}`, mapNumber: index + 1, headshots: null }));
  return output.filter((row) => row.rounds > 0 && row.mapNumber <= 5).slice(0, 60);
}
