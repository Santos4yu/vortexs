let captured = [];
let mode = "board";
const status = document.getElementById("status");
const count = document.getElementById("count");
const result = document.getElementById("result");
const send = document.getElementById("send");
const target = document.getElementById("target");

chrome.storage.local.get(["targetUrl"], (saved) => { if (saved.targetUrl) target.value = saved.targetUrl; });
target.addEventListener("change", () => chrome.storage.local.set({ targetUrl: target.value.trim() }));

document.getElementById("scan").addEventListener("click", async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id || !tab.url) return;
  if (tab.url.includes("prizepicks.com")) {
    mode = "board";
    status.textContent = "Reading the visible PrizePicks CS2 cards...";
    const [{ result: rows = [] }] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: captureCs2Props });
    captured = rows;
    chrome.storage.local.set({ lastBoard: rows });
    count.textContent = `${rows.length} CS2 props found`;
  } else if (tab.url.includes("hltv.org") || tab.url.includes("bo3.gg")) {
    mode = "history";
    status.textContent = "Reading the visible map-stat table...";
    const [{ result: rows = [] }] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: captureMapStats });
    captured = rows;
    count.textContent = `${rows.length} player map logs found`;
  } else {
    status.textContent = "Open PrizePicks, HLTV, or bo3.gg in this tab first.";
    return;
  }
  result.hidden = false;
  status.textContent = captured.length ? "Capture ready. Send it to CS2 Prop Lab." : "No supported rows were found on this page. Open an individual map statistics page.";
  send.disabled = captured.length === 0;
});

document.getElementById("auto").addEventListener("click", async () => {
  const saved = await chrome.storage.local.get(["lastBoard"]);
  const board = Array.isArray(saved.lastBoard) ? saved.lastBoard : [];
  const players = [...new Set(board.filter((row) => row.market === "maps_1_2_kills").map((row) => row.player).filter(Boolean))];
  if (!players.length) {
    status.textContent = "Capture the PrizePicks board once first. Then press this button.";
    return;
  }
  const button = document.getElementById("auto");
  button.disabled = true;
  status.textContent = `Starting background history collection for ${players.length} players...`;
  await chrome.runtime.sendMessage({ type: "START_HISTORY_JOB", players, target: target.value.trim().replace(/\/$/, "") });
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local" || !changes.historyJob?.newValue) return;
  const job = changes.historyJob.newValue;
  result.hidden = false;
  count.textContent = `${job.matched || 0}/${job.total || 0} players matched · ${job.rows || 0} map rows`;
  status.textContent = job.status === "running" ? `Collecting ${job.current || "player history"} (${job.index || 0}/${job.total || 0}). You may close this popup.` : job.message || "History collection finished.";
  if (job.status !== "running") document.getElementById("auto").disabled = false;
});

chrome.storage.local.get(["historyJob", "historyWork"], ({ historyJob: job, historyWork }) => {
  if (!job) return;
  result.hidden = false;
  count.textContent = `${job.matched || 0}/${job.total || 0} players matched / ${job.rows || 0} map rows`;
  const interrupted = job.status === "running" && !historyWork;
  status.textContent = interrupted ? "The previous scan was interrupted. Press Build history to restart with saved batches." : job.status === "running" ? `Collecting ${job.current || "player history"} (${job.index || 0}/${job.total || 0}). You may close this popup.` : job.message || "History collection finished.";
  document.getElementById("auto").disabled = job.status === "running" && !interrupted;
});

async function fetchHltvPlayerHistory(playerName) {
  const cleanKey = (value) => String(value || "").toLowerCase().normalize("NFKD").replace(/[^a-z0-9]/g, "");
  const parser = new DOMParser();
  const searchResponse = await fetch(`https://www.hltv.org/search?query=${encodeURIComponent(playerName)}`, { credentials: "include" });
  if (!searchResponse.ok) return [];
  const searchDoc = parser.parseFromString(await searchResponse.text(), "text/html");
  const links = [...searchDoc.querySelectorAll("a[href]")];
  const playerLink = links.find((link) => {
    const href = link.getAttribute("href") || "";
    const alias = href.match(/\/(?:stats\/players|player)\/(\d+)\/([^/?#]+)/i)?.[2];
    return alias && cleanKey(alias) === cleanKey(playerName);
  }) || links.find((link) => /\/(?:stats\/players|player)\/\d+\//i.test(link.getAttribute("href") || "") && cleanKey(link.textContent).includes(cleanKey(playerName)));
  const match = (playerLink?.getAttribute("href") || "").match(/\/(?:stats\/players|player)\/(\d+)\/([^/?#]+)/i);
  if (!match) return [];
  const [, playerId, slug] = match;
  const historyUrl = `https://www.hltv.org/stats/players/matches/${playerId}/${slug}`;
  const historyResponse = await fetch(historyUrl, { credentials: "include" });
  if (!historyResponse.ok) return [];
  const doc = parser.parseFromString(await historyResponse.text(), "text/html");
  const mapCodes = new Set(["anc","anb","cch","d2","inf","mrg","nuke","ovp","trn","vtg","mirage","inferno","ancient","anubis","dust2","overpass","train","vertigo","cache"]);
  const parsed = [];
  for (const row of [...doc.querySelectorAll("table tbody tr")].slice(0, 80)) {
    const cells = [...row.querySelectorAll("td")].map((cell) => (cell.innerText || cell.textContent || "").replace(/\s+/g, " ").trim()).filter(Boolean);
    const date = cells.find((cell) => /^\d{1,2}\/\d{1,2}\/\d{2,4}$/.test(cell));
    const kd = cells.find((cell) => /^\d{1,3}\s*-\s*\d{1,3}$/.test(cell));
    const mapName = cells.find((cell) => mapCodes.has(cleanKey(cell)) || mapCodes.has(cell.toLowerCase()));
    if (!date || !kd || !mapName) continue;
    const dateIndex = cells.indexOf(date), mapIndex = cells.indexOf(mapName), kdIndex = cells.indexOf(kd);
    const middle = cells.slice(dateIndex + 1, mapIndex);
    const teamCells = middle.filter((cell) => /\(\d{1,2}\)/.test(cell));
    const scores = teamCells.map((cell) => Number(cell.match(/\((\d{1,2})\)/)?.[1] || 0));
    if (scores.length < 2) scores.push(...middle.filter((cell) => /^\d{1,2}$/.test(cell)).map(Number));
    const imageNames = [...row.querySelectorAll("img[alt]")].map((image) => image.getAttribute("alt") || "").filter((value) => value.length > 1);
    const names = teamCells.map((cell) => cell.replace(/\s*\(\d{1,2}\)\s*/, "").trim());
    if (names.length < 2) names.push(...imageNames);
    const [kills] = kd.split("-").map((value) => Number(value.trim()));
    const link = row.querySelector("a[href*='mapstatsid']")?.getAttribute("href") || "";
    const sourceMapId = link.match(/mapstatsid\/(\d+)/i)?.[1] || `${playerId}-${date}-${parsed.length}`;
    const [day, month, year] = date.split("/");
    const playedAt = `${year.length === 2 ? `20${year}` : year}-${month.padStart(2, "0")}-${day.padStart(2, "0")}`;
    parsed.push({ source: "hltv", sourceMapId, playedAt, player: playerName, team: names[0] || "", opponent: names[1] || "", mapName, kills, rounds: (scores[0] || 0) + (scores[1] || 0), sourceUrl: `https://www.hltv.org${link}` });
  }
  const groups = new Map();
  for (const row of parsed) {
    const key = `${row.playedAt}|${cleanKey(row.team)}|${cleanKey(row.opponent)}`;
    groups.set(key, [...(groups.get(key) || []), row]);
  }
  const output = [];
  for (const [key, newestFirst] of groups) {
    // HLTV lists the last map first, so reverse to recover Map 1, Map 2, Map 3.
    const ordered = [...newestFirst].reverse();
    ordered.forEach((row, index) => output.push({ ...row, matchKey: `hltv-${playerId}-${key}`, mapNumber: index + 1, headshots: null }));
  }
  return output.filter((row) => row.rounds > 0 && row.mapNumber <= 5).slice(0, 60);
}

send.addEventListener("click", async () => {
  const base = target.value.trim().replace(/\/$/, "");
  chrome.storage.local.set({ targetUrl: base });
  if (mode === "history") {
    const payload = btoa(unescape(encodeURIComponent(JSON.stringify(captured))));
    chrome.tabs.create({ url: `${base}/?history=${encodeURIComponent(payload)}` });
    return;
  }
  send.disabled = true;
  status.textContent = "Opening the captured board...";
  await chrome.storage.local.set({ pendingBoard: captured });
  chrome.tabs.create({ url: `${base}/?extensionImport=1` });
});

function captureMapStats() {
  const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
  const pageText = clean(document.body.innerText);
  const source = location.hostname.includes("hltv") ? "hltv" : "bo3";
  const sourceMapId = location.pathname.match(/(?:mapstatsid|maps?|match)[/-](\d+)/i)?.[1] || location.pathname.replace(/\W/g, "-");
  const matchLink = [...document.querySelectorAll("a[href]")].map((a) => a.getAttribute("href") || "").find((href) => /\/matches?\/\d+/i.test(href));
  const matchKey = matchLink?.match(/\/matches?\/(\d+)/i)?.[1] || `${source}-${sourceMapId}`;
  const mapLinks = [...document.querySelectorAll("a[href*='mapstatsid'],a[href*='/maps/']")];
  const activeIndex = mapLinks.findIndex((a) => (a.getAttribute("href") || "").includes(sourceMapId));
  const mapNumber = activeIndex >= 0 ? activeIndex + 1 : Number(document.querySelector("[data-map-number]")?.getAttribute("data-map-number")) || 1;
  const mapName = clean(document.querySelector(".dynamic-map-name-full,.stats-match-map-result-mapname,.map-name,[class*='mapName']")?.textContent) || "Unknown";
  const scoreMatches = [...pageText.matchAll(/(?:^|\s)(\d{1,2})\s*[:\-]\s*(\d{1,2})(?:\s|$)/g)].map((m) => [Number(m[1]), Number(m[2])]).filter(([a,b]) => a <= 40 && b <= 40);
  const score = scoreMatches.find(([a,b]) => a + b >= 13) || [0, 0];
  const rounds = score[0] + score[1];
  const dateRaw = document.querySelector("time[datetime]")?.getAttribute("datetime") || pageText.match(/\b\d{4}-\d{2}-\d{2}\b/)?.[0] || pageText.match(/\b\d{1,2}[/.]\d{1,2}[/.]\d{2,4}\b/)?.[0] || new Date().toISOString();
  const playedAt = /^\d{4}-\d{2}-\d{2}/.test(dateRaw) ? dateRaw.slice(0, 10) : new Date(dateRaw.replace(/\./g, "/")).toISOString().slice(0, 10);
  const teamNames = [...document.querySelectorAll(".team-left a,.team-right a,.teamName,[class*='team-name']")].map((node) => clean(node.textContent)).filter(Boolean);
  const rows = [];
  const tableRows = [...document.querySelectorAll("table tbody tr")];
  for (const row of tableRows) {
    const playerNode = row.querySelector("a[href*='/player/'],a[href*='/players/'],.st-player,[class*='player-name']");
    const player = clean(playerNode?.textContent);
    if (!player || player.length > 32) continue;
    const cells = [...row.querySelectorAll("td")];
    const killCell = row.querySelector(".st-kills,[class*='kills']") || cells.find((cell) => /^\s*\d{1,2}(?:\s*\(\d{1,2}\))?\s*$/.test(cell.textContent || ""));
    const killText = clean(killCell?.textContent);
    const killMatch = killText.match(/^(\d{1,2})(?:\s*\((\d{1,2})\))?/);
    if (!killMatch || !rounds) continue;
    const team = clean(row.closest("table")?.previousElementSibling?.textContent) || teamNames[rows.length >= 5 ? 1 : 0] || "";
    rows.push({ source, sourceMapId, matchKey, playedAt, player, team, opponent: teamNames.find((name) => name !== team) || "", mapNumber, mapName, kills: Number(killMatch[1]), headshots: killMatch[2] == null ? null : Number(killMatch[2]), rounds, sourceUrl: location.href });
  }
  return rows;
}

function captureCs2Props() {
  const MARKET = /(maps?\s*1\s*[-–]\s*2\s*(?:kills?|headshots?))/i;
  const LINE = /(?:^|\s)(\d{1,2}(?:\.5)?)(?=\s|$)/g;
  const rawText = (node) => String(node.innerText || node.textContent || "").trim();
  const textOf = (node) => rawText(node).replace(/\s+/g, " ").trim();
  const looksLikeCard = (node) => {
    const text = textOf(node);
    return MARKET.test(text) && /\bLess\b/i.test(text) && /\bMore\b/i.test(text) && text.length < 650;
  };
  const candidates = [...document.querySelectorAll("article,[role='button'],[class*='projection'],[class*='card'],li,div")].filter(looksLikeCard);
  const smallest = candidates.filter((node) => ![...node.children].some((child) => looksLikeCard(child)));
  const seen = new Set(); const rows = [];
  for (const node of smallest) {
    const raw = textOf(node); const marketMatch = raw.match(MARKET); if (!marketMatch) continue;
    const lines = rawText(node).split(/\r?\n/).map((line) => line.replace(/\s+/g, " ").trim()).filter(Boolean);
    const numbers = [...raw.matchAll(LINE)].map((match) => Number(match[1])).filter((number) => number >= 5 && number <= 60);
    const line = numbers.at(-1); if (!line) continue;
    const versusIndex = lines.findIndex((value) => /^(?:vs\.?|versus)\s+/i.test(value));
    const versusLine = versusIndex >= 0 ? lines[versusIndex] : "";
    const versus = versusLine.match(/^(?:vs\.?|versus)\s+(.+?)(?:\s+MAPS?\s*1\s*[-–]\s*2|\s+(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b|$)/i)?.[1]?.trim() ?? "";
    let player = versusIndex > 0 ? lines[versusIndex - 1] : "";
    if (!player || /(?:MAPS?\s*1|More|Less|\d{1,2}(?:\.5)?)/i.test(player)) {
      const marketIndex = lines.findIndex((value) => MARKET.test(value));
      player = lines.slice(0, marketIndex).reverse().find((value) => value.length <= 32 && !/^(?:More|Less|Demon|Goblin)$/i.test(value) && !/\d{1,2}(?:\.5)?/.test(value) && !/\s-\s[A-Z]$/i.test(value)) || "";
    }
    player = player.replace(/\b(More|Less|Demon|Goblin)\b/gi, "").trim();
    if (!player || player.length > 40) continue;
    const team = versusIndex > 1 ? lines[versusIndex - 2].replace(/\s*-\s*[A-Z]$/i, "").trim() : "";
    const market = /headshot/i.test(marketMatch[0]) ? "maps_1_2_headshots" : "maps_1_2_kills";
    const key = `${player.toLowerCase()}|${market}|${line}`; if (seen.has(key)) continue; seen.add(key);
    rows.push({ player, team, opponent: versus, market, line, source: "PrizePicks", capturedText: raw });
  }
  return rows;
}
