import { evaluateProp } from "./engine";
import type { ConvictionDimension, PlayerEvidence, Projection, PropLine, Side } from "./types";

const clamp = (value: number, min: number, max: number) => Math.max(min, Math.min(max, value));
const mean = (values: number[]) => values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : 0;
const median = (values: number[]) => {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
};

function logSummary(logs: number[], line: number, side: Side) {
  const outcomes = logs.map((value) => side === "more" ? value > line : value < line);
  const pushes = logs.filter((value) => value === line).length;
  const clearances = logs.map((value) => side === "more" ? value - line : line - value);
  return {
    rate: logs.length ? outcomes.filter(Boolean).length / logs.length : 0,
    medianClearance: median(clearances),
    pushes,
  };
}

function independentProjection(line: PropLine, evidence: PlayerEvidence) {
  const logs = evidence.gameLogs ?? [];
  const recent = logs.slice(0, 8);
  const stableLogMean = recent.length >= 4 ? (mean(recent) * 0.65 + median(recent) * 0.35) : 0;
  const mapBaseline = evidence.mapAdjustedAverage ?? evidence.expectedRounds * evidence.killsPerRound;
  const opponent = clamp(evidence.opponentDefensiveFactor ?? evidence.opponentAdjustment, 0.82, 1.18);
  const role = clamp(evidence.roleAdjustment, 0.88, 1.12);
  const headshotMultiplier = line.market === "maps_1_2_headshots" ? clamp(evidence.headshotShare ?? 0, 0, 0.8) : 1;
  const raw = (stableLogMean ? stableLogMean * 0.55 + mapBaseline * 0.45 : mapBaseline) * opponent * role;
  return raw * headshotMultiplier;
}

export function gradeProp(line: PropLine, evidence: PlayerEvidence): Projection {
  const base = evaluateProp(line, evidence);
  const modelB = independentProjection(line, evidence);
  const modelBSide: Side = modelB >= line.line ? "more" : "less";
  const logs = evidence.gameLogs ?? [];
  const log = logSummary(logs.slice(0, 8), line.line, base.side);
  const modelAgreement = base.side === modelBSide && Math.abs(base.projection - modelB) <= Math.max(4.5, line.line * 0.17);
  const marketMove = evidence.openingLine == null ? 0 : line.line - evidence.openingLine;
  const marketSupports = evidence.openingLine != null && (marketMove === 0 || (base.side === "more" ? marketMove > 0 : marketMove < 0));
  const projectionClearance = Math.abs(base.projection - line.line);
  const teamWinProbability = evidence.teamWinProbability ?? 0.5;

  const dimensions: ConvictionDimension[] = [
    { key: "projection", passed: modelAgreement && projectionClearance >= (line.market === "maps_1_2_headshots" ? 1.3 : 2.3), score: clamp(projectionClearance / 5, 0, 1), note: modelAgreement ? "Independent models agree" : "Independent models conflict" },
    { key: "history", passed: logs.length >= 6 && log.rate >= 0.625 && log.medianClearance > 0, score: logs.length ? log.rate : 0, note: logs.length ? `${Math.round(log.rate * 100)}% exact-line rate over ${Math.min(8, logs.length)} logs` : "Exact-line logs unavailable" },
    { key: "matchup", passed: evidence.mapCoverage >= 0.65 && evidence.opponentAdjustment >= 0.93 && evidence.opponentAdjustment <= 1.09, score: clamp(evidence.mapCoverage, 0, 1), note: `${Math.round(evidence.mapCoverage * 100)}% map coverage` },
    { key: "quality", passed: evidence.sampleMaps >= 20 && evidence.recentMaps >= 6 && evidence.rosterStable && evidence.source !== "demo", score: base.dataConfidence / 100, note: `${evidence.sampleMaps} maps / roster ${evidence.rosterStable ? "stable" : "changed"}` },
    { key: "market", passed: marketSupports, score: marketSupports ? 1 : 0, note: evidence.openingLine == null ? "Market movement unavailable" : marketSupports ? "Market moved with model" : "Market moved against model" },
  ];
  const conviction = dimensions.filter((dimension) => dimension.passed).length;
  const warnings = [...base.warnings];
  if (!modelAgreement) warnings.push("MODEL_DISAGREEMENT");
  if (logs.length < 6) warnings.push("EXACT_LINE_LOG_THIN");
  if (evidence.openingLine == null) warnings.push("MARKET_MOVEMENT_UNAVAILABLE");
  else if (!marketSupports) warnings.push("MARKET_MOVED_AGAINST_MODEL");
  if (base.side === "more" && teamWinProbability < 0.42) warnings.push("UNDERDOG_OVER_RISK");
  if (log.medianClearance <= 0 && logs.length) warnings.push("WEAK_HISTORICAL_CLEARANCE");

  const qualified = base.qualified && modelAgreement && conviction >= 4 && log.rate >= 0.625;
  const tier = qualified && conviction === 5 && base.probability >= 62 ? "STRONG" : qualified ? "LEAN" : base.tier === "NO_DATA" ? "NO_DATA" : "PASS";
  return {
    ...base,
    qualified,
    tier,
    warnings: [...new Set(warnings)],
    modelAProjection: base.projection,
    modelBProjection: Number(modelB.toFixed(2)),
    modelAgreement,
    exactLineHitRate: Number((log.rate * 100).toFixed(1)),
    medianClearance: Number(log.medianClearance.toFixed(2)),
    conviction,
    dimensions,
    correlationKey: `${line.team.toLowerCase()}|${line.opponent.toLowerCase()}`,
  };
}

export function gradeBoard(rows: Array<{ line: PropLine; evidence: PlayerEvidence }>) {
  const graded = rows.map(({ line, evidence }) => gradeProp(line, evidence));
  const groups = new Map<string, Projection[]>();
  for (const projection of graded.filter((item) => item.qualified)) {
    const key = projection.correlationKey ?? "";
    groups.set(key, [...(groups.get(key) ?? []), projection]);
  }
  for (const group of groups.values()) {
    const overs = group.filter((item) => item.side === "more");
    if (overs.length >= 3) for (const item of overs) item.warnings = [...new Set([...item.warnings, `CORRELATED_TEAM_OVERS_${overs.length}`])];
  }
  return graded.sort((a, b) => (b.conviction ?? 0) - (a.conviction ?? 0) || b.probability - a.probability || b.dataConfidence - a.dataConfidence);
}
