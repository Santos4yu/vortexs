import type { PlayerEvidence, Projection, PropLine } from "./types";

export const MODEL_VERSION = "cs2-kills-v0.1";

const clamp = (value: number, min: number, max: number) => Math.max(min, Math.min(max, value));

function erf(x: number) {
  const sign = x < 0 ? -1 : 1;
  const value = Math.abs(x);
  const a1 = 0.254829592;
  const a2 = -0.284496736;
  const a3 = 1.421413741;
  const a4 = -1.453152027;
  const a5 = 1.061405429;
  const p = 0.3275911;
  const t = 1 / (1 + p * value);
  const y = 1 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * Math.exp(-value * value);
  return sign * y;
}

function normalCdf(x: number, mean: number, sd: number) {
  return 0.5 * (1 + erf((x - mean) / (sd * Math.sqrt(2))));
}

function americanOdds(probability: number) {
  const p = clamp(probability, 0.01, 0.99);
  return Math.round(p >= 0.5 ? (-100 * p) / (1 - p) : (100 * (1 - p)) / p);
}

export function evaluateProp(line: PropLine, evidence: PlayerEvidence): Projection {
  const longWeight = clamp(evidence.sampleMaps / 40, 0.35, 0.72);
  const recentWeight = 1 - longWeight;
  const blendedKpr = evidence.killsPerRound * longWeight + evidence.recentKillsPerRound * recentWeight;
  const adjustedKpr = blendedKpr * evidence.mapPoolAdjustment * evidence.opponentAdjustment * evidence.roleAdjustment;
  const killProjection = adjustedKpr * evidence.expectedRounds;
  const projection = line.market === "maps_1_2_headshots"
    ? killProjection * clamp(evidence.headshotShare ?? 0, 0, 0.8)
    : killProjection;

  const varianceRate = line.market === "maps_1_2_headshots" ? 0.29 : 0.21;
  const sd = Math.max(line.market === "maps_1_2_headshots" ? 2.6 : 4.2, projection * varianceRate);
  const overProbability = 1 - normalCdf(line.line, projection, sd);
  const side = overProbability >= 0.5 ? "more" : "less";
  const probability = side === "more" ? overProbability : 1 - overProbability;
  const edgePp = (probability - 0.5) * 100;
  const clearance = side === "more" ? projection - line.line : line.line - projection;

  const sampleScore = clamp(evidence.sampleMaps / 35, 0, 1);
  const recentScore = clamp(evidence.recentMaps / 10, 0, 1);
  const freshnessScore = evidence.updatedAt
    ? clamp(1 - (Date.now() - new Date(evidence.updatedAt).getTime()) / (1000 * 60 * 60 * 24 * 14), 0, 1)
    : 0.35;
  const confidence = 100 * (
    0.34 * sampleScore +
    0.18 * recentScore +
    0.2 * clamp(evidence.mapCoverage, 0, 1) +
    0.16 * (evidence.rosterStable ? 1 : 0.25) +
    0.12 * freshnessScore
  );

  const warnings: string[] = [];
  if (evidence.source === "demo") warnings.push("DEMO_DATA_NOT_BETTABLE");
  if (evidence.sampleMaps < 15) warnings.push("LOW_MAP_SAMPLE");
  if (evidence.recentMaps < 5) warnings.push("LOW_RECENT_SAMPLE");
  if (evidence.mapCoverage < 0.6) warnings.push("MAP_POOL_LOW_COVERAGE");
  if (!evidence.rosterStable) warnings.push("ROSTER_CHANGE_RISK");
  if (line.market === "maps_1_2_headshots" && !evidence.headshotShare) warnings.push("HEADSHOT_RATE_MISSING");

  const hardDataGate = evidence.source !== "demo"
    && evidence.sampleMaps >= 12
    && evidence.recentMaps >= 6
    && evidence.mapCoverage >= 0.5
    && evidence.rosterStable;
  const qualified = hardDataGate && confidence >= 60 && edgePp >= 5.5 && warnings.every((warning) => warning !== "HEADSHOT_RATE_MISSING");
  const tier = !hardDataGate ? "NO_DATA" : qualified && confidence >= 76 && edgePp >= 8 ? "STRONG" : qualified ? "LEAN" : "PASS";

  const reasons = [
    `${evidence.expectedRounds.toFixed(1)} expected rounds across Maps 1-2`,
    `${adjustedKpr.toFixed(3)} adjusted kills per round`,
    `${evidence.sampleMaps} map long sample and ${evidence.recentMaps} map recent sample`,
    `${Math.round(evidence.mapCoverage * 100)}% projected-map coverage`,
  ];

  return {
    player: line.player,
    market: line.market,
    line: line.line,
    projection: Number(projection.toFixed(2)),
    side,
    probability: Number((probability * 100).toFixed(1)),
    edgePp: Number(edgePp.toFixed(1)),
    clearance: Number(clearance.toFixed(2)),
    fairOdds: americanOdds(probability),
    dataConfidence: Math.round(confidence),
    tier,
    qualified,
    reasons,
    warnings,
    modelVersion: MODEL_VERSION,
  };
}
