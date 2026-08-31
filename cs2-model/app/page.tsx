"use client";

import { useEffect, useMemo, useState } from "react";
import { decodeImportedBoard, parsePrizePicksText } from "../ingestion/prizepicks-text";
import { gradeBoard } from "../model/grader";
import type { PlayerEvidence, Projection, PropLine } from "../model/types";

type Tab = "board" | "import" | "records" | "setup";
type ProviderState = { connected: boolean; fixturesConnected?: boolean; historicalConnected?: boolean; provider: string; reason?: string };
type FreeEvidenceResult = { player: string; market: PropLine["market"]; evidence?: PlayerEvidence; status: "ready" | "thin" | "missing" | "unsupported"; reason?: string };

const evidenceKey = (player: string, market: PropLine["market"]) => `${player.toLowerCase().normalize("NFKD").replace(/[^a-z0-9]/g, "")}|${market}`;
const handledExtensionImports = new Set<string>();

const demoEvidence: PlayerEvidence = {
  sampleMaps: 0,
  recentMaps: 0,
  killsPerRound: 0.68,
  recentKillsPerRound: 0.68,
  headshotShare: 0.49,
  expectedRounds: 43,
  mapPoolAdjustment: 1,
  opponentAdjustment: 1,
  roleAdjustment: 1,
  rosterStable: false,
  mapCoverage: 0,
  source: "demo",
};

export default function Home() {
  const [tab, setTab] = useState<Tab>("board");
  const [provider, setProvider] = useState<ProviderState>({ connected: false, provider: "PandaScore", reason: "Checking connection" });
  const [lines, setLines] = useState<PropLine[]>([]);
  const [projections, setProjections] = useState<Projection[]>([]);
  const [paste, setPaste] = useState("");
  const [notice, setNotice] = useState("");
  const [analyzing, setAnalyzing] = useState(false);
  const [freeDataOnline, setFreeDataOnline] = useState(false);

  useEffect(() => {
    const receiveExtensionBoard = (event: MessageEvent) => {
      if (event.source !== window) return;
      if (event.data?.type === "CS2_PROP_LAB_BOARD" && Array.isArray(event.data.lines)) {
        const messageKey = `board:${event.data.lines.length}:${event.data.lines[0]?.player ?? ""}`;
        if (handledExtensionImports.has(messageKey)) return;
        handledExtensionImports.add(messageKey);
        const imported = event.data.lines as PropLine[];
        setLines(imported);
        setTab("import");
        setNotice(`${imported.length} PrizePicks props loaded. Confirm the rows, then build history from the extension.`);
        window.history.replaceState({}, "", window.location.pathname);
      }
      if (event.data?.type === "CS2_PROP_LAB_HISTORY" && Array.isArray(event.data.rows)) {
        const messageKey = `history:${event.data.rows.length}:${event.data.rows[0]?.sourceMapId ?? ""}`;
        if (handledExtensionImports.has(messageKey)) return;
        handledExtensionImports.add(messageKey);
        const rows = event.data.rows as unknown[];
        setTab("setup");
        setNotice(`Saving ${rows.length} captured HLTV map rows...`);
        fetch("/api/history/import", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ rows }) })
          .then(async (response) => ({ ok: response.ok, body: await response.json() as { inserted?: number; error?: string } }))
          .then(({ ok, body }) => setNotice(ok ? `${body.inserted ?? 0} new map logs saved permanently. Return to Import and press Confirm and analyze.` : body.error || "History import failed."))
          .catch(() => setNotice("History import failed. No captured rows were saved."));
        window.history.replaceState({}, "", window.location.pathname);
      }
    };
    window.addEventListener("message", receiveExtensionBoard);
    fetch("/api/provider/status").then((response) => response.json()).then(setProvider).catch(() => setProvider({ connected: false, provider: "PandaScore", reason: "Connection check failed" }));
    const encoded = new URLSearchParams(window.location.search).get("board");
    if (encoded) {
      const imported = decodeImportedBoard(encoded);
      setLines(imported);
      setTab("import");
      setNotice(imported.length ? `${imported.length} PrizePicks props captured. Confirm every row.` : "The browser helper did not find usable CS2 lines.");
      window.history.replaceState({}, "", window.location.pathname);
    }
    const importId = new URLSearchParams(window.location.search).get("importId");
    if (importId) {
      setNotice("Loading the captured PrizePicks board...");
      fetch(`/api/board-import?id=${encodeURIComponent(importId)}`)
        .then(async (response) => ({ ok: response.ok, body: await response.json() as { lines?: PropLine[]; error?: string } }))
        .then(({ ok, body }) => {
          if (!ok || !body.lines) throw new Error(body.error || "Saved board not found");
          setLines(body.lines);
          setTab("import");
          setNotice(`${body.lines.length} PrizePicks props loaded. Confirm the board, then build history from the extension.`);
        })
        .catch((error) => setNotice(error instanceof Error ? error.message : "Could not load the captured board."));
      window.history.replaceState({}, "", window.location.pathname);
    }
    const historyEncoded = new URLSearchParams(window.location.search).get("history");
    if (historyEncoded) {
      try {
        const rows = JSON.parse(decodeURIComponent(escape(atob(historyEncoded)))) as unknown[];
        setNotice(`Saving ${rows.length} captured player-map rows...`);
        fetch("/api/history/import", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ rows }) })
          .then(async (response) => ({ ok: response.ok, body: await response.json() as { inserted?: number; received?: number; error?: string } }))
          .then(({ ok, body }) => setNotice(ok ? `${body.inserted ?? 0} new player-map logs saved permanently. Repeated rows were safely skipped.` : body.error || "History import failed."))
          .catch(() => setNotice("History import failed. The captured rows were not added."));
      } catch {
        setNotice("The history capture could not be read. Open the map-stat page and scan it again.");
      }
      window.history.replaceState({}, "", window.location.pathname);
      setTab("setup");
    }
    return () => window.removeEventListener("message", receiveExtensionBoard);
  }, []);

  const qualified = useMemo(() => projections.filter((projection) => projection.qualified), [projections]);
  const kills = lines.filter((line) => line.market === "maps_1_2_kills").length;
  const headshots = lines.length - kills;

  function parseBoard() {
    const parsed = parsePrizePicksText(paste);
    setLines(parsed);
    setNotice(parsed.length ? `${parsed.length} CS2 props found. Review names and decimals before analysis.` : "No Maps 1-2 CS2 lines were detected in that text.");
  }

  function updateLine(index: number, key: keyof PropLine, value: string) {
    setLines((current) => current.map((line, rowIndex) => rowIndex === index ? { ...line, [key]: key === "line" ? Number(value) : value } as PropLine : line));
  }

  async function analyze() {
    const invalid = lines.filter((line) => !line.player.trim() || line.player === "Needs review" || /^\d+(?:\.5)?$/.test(line.player.trim()) || !Number.isFinite(line.line) || line.line <= 0);
    if (invalid.length) {
      setNotice(`${invalid.length} row${invalid.length === 1 ? "" : "s"} still need a valid player name or line. Fix the highlighted import before analysis.`);
      setTab("import");
      return;
    }
    setAnalyzing(true);
    setNotice(`Collecting real map logs for ${kills} kill props. Headshots remain locked because the free source does not provide them.`);
    try {
      const response = await fetch("/api/provider/free-evidence", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lines }),
      });
      const payload = await response.json() as { evidence?: FreeEvidenceResult[]; error?: string };
      if (!response.ok || !payload.evidence) throw new Error(payload.error || "Free data scan failed");
      const evidenceByLine = new Map(payload.evidence.map((result) => [evidenceKey(result.player, result.market), result]));
      const output = gradeBoard(lines.map((line) => ({
        line,
        evidence: evidenceByLine.get(evidenceKey(line.player, line.market))?.evidence ?? demoEvidence,
      })));
      const ready = payload.evidence.filter((result) => result.status === "ready").length;
      const thin = payload.evidence.filter((result) => result.status === "thin").length;
      const missing = payload.evidence.filter((result) => result.status === "missing").length;
      const unsupported = payload.evidence.filter((result) => result.status === "unsupported").length;
      setProjections(output);
      setFreeDataOnline(true);
      setTab("board");
      setNotice(`Free data scan complete: ${ready} ready, ${thin} thin, ${missing} missing, ${unsupported} unsupported. Only props clearing every model gate are qualified.`);
    } catch (error) {
      setFreeDataOnline(false);
      setNotice(error instanceof Error ? error.message : "Free data scan failed. No directions were published.");
    } finally {
      setAnalyzing(false);
    }
  }

  return (
    <main className="lab-shell">
      <header className="topbar">
        <div className="brand-mark">C2</div>
        <div><p className="eyebrow">Independent research engine</p><h1>CS2 Prop Lab</h1></div>
        <nav aria-label="Main navigation">
          {(["board", "import", "records", "setup"] as Tab[]).map((item) => <button key={item} className={tab === item ? "selected" : ""} onClick={() => setTab(item)}>{item}</button>)}
        </nav>
        <div className={`provider-chip ${freeDataOnline || provider.fixturesConnected ? "online" : "offline"}`}><span />{freeDataOnline ? "Free data online" : provider.fixturesConnected ? "Lines online" : "Setup required"}</div>
      </header>

      {notice && <div className="notice"><span>i</span>{notice}<button onClick={() => setNotice("")}>x</button></div>}

      {tab === "board" && <section className="product-page">
        <div className="page-title"><div><p className="eyebrow cyan">Today / qualified board</p><h2>Model decisions, with every gate visible.</h2><p>Recent greens alone never create a play. Opportunity, map pool, opponent, sample quality and projection clearance must agree.</p></div><button className="primary" onClick={() => setTab("import")}>Import PrizePicks board</button></div>
        <div className="metrics">
          <article><small>Imported lines</small><strong>{lines.length}</strong><span>{kills} kills / {headshots} headshots</span></article>
          <article><small>Qualified</small><strong>{qualified.length}</strong><span>STRONG and LEAN only</span></article>
          <article><small>Map-log provider</small><strong className={freeDataOnline ? "good" : "warn"}>{freeDataOnline ? "LIVE" : "READY"}</strong><span>{freeDataOnline ? "Free CS map logs loaded" : "Runs when you analyze"}</span></article>
          <article><small>Model version</small><strong className="model-id">v0.1</strong><span>Maps 1-2 foundation</span></article>
        </div>

        {!projections.length ? <div className="blank-board"><div className="target-rings"><span /><span /><b /></div><h3>No evaluated board yet</h3><p>Capture the CS2 board from PrizePicks, confirm the lines, and run the data-quality gates.</p><button className="primary" onClick={() => setTab("import")}>Start board import</button></div> :
          <div className="projection-list">{projections.map((projection, index) => <article className="projection-card" key={`${projection.player}-${index}`}>
            <div className="tier-column"><span className={`tier tier-${projection.tier.toLowerCase().replace("_", "-")}`}>{projection.tier.replace("_", " ")}</span><b>{projection.dataConfidence}</b><small>DATA</small></div>
            <div className="projection-main"><div className="prop-heading"><div><h3>{projection.player}</h3><p>{projection.market === "maps_1_2_kills" ? "Maps 1-2 Kills" : "Maps 1-2 Headshots"} / line {projection.line}</p></div><strong>{projection.tier === "NO_DATA" ? "DIRECTION LOCKED" : `${projection.side.toUpperCase()} ${projection.line}`}</strong></div>
              <div className="projection-grid"><span><small>Projection A / B</small><b>{projection.tier === "NO_DATA" ? "--" : `${projection.modelAProjection} / ${projection.modelBProjection}`}</b></span><span><small>Probability</small><b>{projection.tier === "NO_DATA" ? "--" : `${projection.probability}%`}</b></span><span><small>Exact-line L8</small><b>{projection.tier === "NO_DATA" ? "--" : `${projection.exactLineHitRate}%`}</b></span><span><small>Conviction</small><b>{projection.tier === "NO_DATA" ? "0/5" : `${projection.conviction}/5`}</b></span></div>
              <div className="reason-row">{projection.dimensions?.map((dimension) => <span className={dimension.passed ? "dimension-pass" : "dimension-fail"} key={dimension.key}>{dimension.key}: {dimension.note}</span>)}</div>
              {projection.warnings.length > 0 && <div className="warning-row">{projection.warnings.join(" / ")}</div>}
            </div>
          </article>)}</div>}
      </section>}

      {tab === "import" && <section className="product-page">
        <div className="page-title compact"><div><p className="eyebrow cyan">Board intake</p><h2>Capture once. Confirm before modeling.</h2><p>Use the browser helper for automatic capture, or paste the copied board as a fallback.</p></div></div>
        <div className="import-layout">
          <aside className="capture-guide"><p className="eyebrow">PrizePicks helper</p><h3>One-button daily capture</h3><ol><li><b>1</b><span>Open PrizePicks and choose CS2.</span></li><li><b>2</b><span>Scroll until every player card is loaded.</span></li><li><b>3</b><span>Press the CS2 Prop Lab browser button.</span></li><li><b>4</b><span>Confirm names and decimals here.</span></li></ol><div className="safe-note">The helper reads the page you already opened. It does not store your PrizePicks login.</div></aside>
          <div className="import-workspace">
            <div className="paste-box"><label htmlFor="board-text">Fallback: paste copied board text</label><textarea id="board-text" value={paste} onChange={(event) => setPaste(event.target.value)} placeholder="Paste the full PrizePicks CS2 board text here..."/><button onClick={parseBoard}>Find CS2 lines</button></div>
            <div className="review-table"><div className="review-title"><div><p className="eyebrow">Confirmation</p><h3>{lines.length} lines ready</h3></div><span>{kills} K / {headshots} HS</span></div>
              {!lines.length ? <div className="table-empty">Captured lines will appear here.</div> : <div className="table-scroll"><table><thead><tr><th>Player</th><th>Team</th><th>Opponent</th><th>Market</th><th>Line</th><th /></tr></thead><tbody>{lines.map((line, index) => { const invalidPlayer = !line.player.trim() || line.player === "Needs review" || /^\d+(?:\.5)?$/.test(line.player.trim()); return <tr className={invalidPlayer ? "invalid-row" : ""} key={`${line.player}-${index}`}><td><input value={line.player} onChange={(event) => updateLine(index, "player", event.target.value)}/></td><td><input value={line.team} onChange={(event) => updateLine(index, "team", event.target.value)}/></td><td><input value={line.opponent} onChange={(event) => updateLine(index, "opponent", event.target.value)}/></td><td><select value={line.market} onChange={(event) => updateLine(index, "market", event.target.value)}><option value="maps_1_2_kills">Maps 1-2 Kills</option><option value="maps_1_2_headshots">Maps 1-2 Headshots</option></select></td><td><input className="short" type="number" step="0.5" value={line.line} onChange={(event) => updateLine(index, "line", event.target.value)}/></td><td><button className="remove" onClick={() => setLines((current) => current.filter((_, rowIndex) => rowIndex !== index))}>x</button></td></tr>;})}</tbody></table></div>}
              <div className="review-actions"><span>All lines require confirmation before evaluation.</span><button className="primary" disabled={!lines.length || analyzing} onClick={analyze}>{analyzing ? "Collecting free data..." : "Confirm and analyze"}</button></div>
            </div>
          </div>
        </div>
      </section>}

      {tab === "records" && <section className="product-page"><div className="page-title compact"><div><p className="eyebrow cyan">Permanent audit log</p><h2>Every evaluation stays in the record.</h2><p>Pending, won, lost and void results will remain separated by model version and date.</p></div></div><div className="blank-board small"><h3>No saved live evaluations</h3><p>The model will not seed this page with invented wins or losses.</p></div></section>}

      {tab === "setup" && <section className="product-page"><div className="page-title compact"><div><p className="eyebrow cyan">Connections</p><h2>Automation health</h2><p>Free map history powers kill props. Missing evidence always locks the direction.</p></div></div><div className="setup-grid">
        <article><div className="connection-icon ok">CS</div><div><h3>Free CS map history</h3><p>Current teams, recent matches, map scores and player kills. No key or payment required.</p><span className="connected">Built / checked during analysis</span></div></article>
        <article><div className={`connection-icon ${provider.fixturesConnected ? "ok" : ""}`}>P</div><div><h3>PandaScore fixtures</h3><p>Optional schedule and roster matching. Historical statistics are not required for the free kill model.</p><span className={provider.fixturesConnected ? "connected" : "missing"}>{provider.fixturesConnected ? "Connected" : provider.reason}</span></div></article>
        <article><div className="connection-icon ok">C</div><div><h3>CS2 browser collector</h3><p>Captures PrizePicks markets plus visible HLTV and bo3.gg map-stat tables. Each map is stored once.</p><span className="connected">Version 0.2 / reload extension</span></div></article>
        <article><div className="connection-icon">M</div><div><h3>Map document importer</h3><p>Bo3.gg win, pick and ban screenshots remain source-labeled.</p><span className="missing">Parser calibration pending</span></div></article>
        <article><div className="connection-icon ok">DB</div><div><h3>Permanent records</h3><p>Stores imports, projections, model versions and graded outcomes.</p><span className="connected">Schema ready</span></div></article>
      </div><div className="setup-warning"><b>Kill props only for now</b><p>The free source does not expose headshot logs, so headshot props remain NO DATA. The free source can also have gaps; those players stay locked instead of receiving invented scores.</p></div></section>}

      <footer><span>CS2 PROP LAB</span><p>Standalone project / confirmation-first / no Discord dependency</p></footer>
    </main>
  );
}
