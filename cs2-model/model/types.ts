export type Cs2Market = "maps_1_2_kills" | "maps_1_2_headshots";
export type Side = "more" | "less";
export type Tier = "STRONG" | "LEAN" | "PASS" | "NO_DATA";

export type PropLine = {
  externalId?: string;
  player: string;
  team: string;
  opponent: string;
  market: Cs2Market;
  line: number;
  startTime?: string;
  source: string;
};

export type PlayerEvidence = {
  playerId?: number;
  sampleMaps: number;
  recentMaps: number;
  killsPerRound: number;
  recentKillsPerRound: number;
  headshotShare?: number;
  expectedRounds: number;
  mapPoolAdjustment: number;
  opponentAdjustment: number;
  roleAdjustment: number;
  rosterStable: boolean;
  mapCoverage: number;
  source: "pandascore" | "csapi" | "manual" | "demo";
  updatedAt?: string;
  gameLogs?: number[];
  mapAdjustedAverage?: number;
  opponentDefensiveFactor?: number;
  teamWinProbability?: number;
  openingLine?: number;
};

export type ConvictionDimension = {
  key: "projection" | "history" | "matchup" | "quality" | "market";
  passed: boolean;
  score: number;
  note: string;
};

export type Projection = {
  player: string;
  market: Cs2Market;
  line: number;
  projection: number;
  side: Side;
  probability: number;
  edgePp: number;
  clearance: number;
  fairOdds: number;
  dataConfidence: number;
  tier: Tier;
  qualified: boolean;
  reasons: string[];
  warnings: string[];
  modelVersion: string;
  modelAProjection?: number;
  modelBProjection?: number;
  modelAgreement?: boolean;
  exactLineHitRate?: number;
  medianClearance?: number;
  conviction?: number;
  dimensions?: ConvictionDimension[];
  correlationKey?: string;
};
