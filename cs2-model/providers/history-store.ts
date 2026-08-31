import { and, desc, eq, inArray } from "drizzle-orm";
import { getDb } from "../db";
import { playerMapLogs } from "../db/schema";
import type { PlayerEvidence, PropLine } from "../model/types";
import { env } from "cloudflare:workers";

export type ImportedMapLog = {
  source: "hltv" | "bo3";
  sourceMapId: string;
  matchKey: string;
  playedAt: string;
  player: string;
  team?: string;
  opponent?: string;
  mapNumber: number;
  mapName?: string;
  kills: number;
  headshots?: number | null;
  rounds: number;
  sourceUrl: string;
};

export const playerKey = (value: string) => value.toLowerCase().normalize("NFKD").replace(/[^a-z0-9]/g, "");

let schemaReady: Promise<void> | null = null;
function ensureHistorySchema() {
  if (schemaReady) return schemaReady;
  schemaReady = (async () => {
    const d1 = (env as unknown as { DB: D1Database }).DB;
    await d1.batch([
      d1.prepare(`CREATE TABLE IF NOT EXISTS cs2_player_map_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
        source TEXT NOT NULL, source_map_id TEXT NOT NULL, match_key TEXT NOT NULL,
        played_at TEXT NOT NULL, player TEXT NOT NULL, player_key TEXT NOT NULL,
        team TEXT DEFAULT '' NOT NULL, opponent TEXT DEFAULT '' NOT NULL,
        map_number INTEGER NOT NULL, map_name TEXT DEFAULT 'Unknown' NOT NULL,
        kills INTEGER NOT NULL, headshots INTEGER, rounds INTEGER NOT NULL,
        source_url TEXT NOT NULL, imported_at TEXT DEFAULT CURRENT_TIMESTAMP NOT NULL
      )`),
      d1.prepare("CREATE INDEX IF NOT EXISTS idx_cs2_map_logs_player_date ON cs2_player_map_logs (player_key, played_at)"),
      d1.prepare("CREATE INDEX IF NOT EXISTS idx_cs2_map_logs_match_player ON cs2_player_map_logs (match_key, player_key)"),
    ]);
  })();
  return schemaReady;
}

export async function importMapLogs(rows: ImportedMapLog[]) {
  await ensureHistorySchema();
  const valid = rows.filter((row) => row.player && row.sourceMapId && row.matchKey && row.playedAt && [1, 2, 3, 4, 5].includes(Number(row.mapNumber)) && Number.isFinite(Number(row.kills)) && Number(row.kills) >= 0 && Number(row.rounds) > 0);
  let inserted = 0;
  for (const row of valid.slice(0, 3000)) {
    const key = playerKey(row.player);
    const existing = await getDb().select({ id: playerMapLogs.id }).from(playerMapLogs).where(and(eq(playerMapLogs.source, row.source), eq(playerMapLogs.sourceMapId, String(row.sourceMapId)), eq(playerMapLogs.playerKey, key))).limit(1);
    if (existing.length) continue;
    await getDb().insert(playerMapLogs).values({
      ...row,
      sourceMapId: String(row.sourceMapId),
      matchKey: String(row.matchKey),
      playerKey: key,
      team: row.team ?? "",
      opponent: row.opponent ?? "",
      mapName: row.mapName ?? "Unknown",
      headshots: row.headshots == null ? null : Number(row.headshots),
      kills: Number(row.kills),
      rounds: Number(row.rounds),
      mapNumber: Number(row.mapNumber),
    });
    inserted += 1;
  }
  return { received: rows.length, accepted: valid.length, inserted };
}

export async function buildStoredEvidence(line: PropLine): Promise<PlayerEvidence | null> {
  await ensureHistorySchema();
  const rows = await getDb().select().from(playerMapLogs).where(eq(playerMapLogs.playerKey, playerKey(line.player))).orderBy(desc(playerMapLogs.playedAt)).limit(120);
  if (!rows.length) return null;
  const byMatch = new Map<string, typeof rows>();
  for (const row of rows) byMatch.set(row.matchKey, [...(byMatch.get(row.matchKey) ?? []), row]);
  const matchLogs = [...byMatch.values()].map((maps) => {
    const firstTwo = maps.filter((map) => map.mapNumber === 1 || map.mapNumber === 2).sort((a, b) => a.mapNumber - b.mapNumber);
    if (firstTwo.length !== 2) return null;
    const kills = firstTwo.reduce((sum, map) => sum + map.kills, 0);
    const headshots = firstTwo.every((map) => map.headshots != null) ? firstTwo.reduce((sum, map) => sum + Number(map.headshots), 0) : null;
    const rounds = firstTwo.reduce((sum, map) => sum + map.rounds, 0);
    return { date: firstTwo[0].playedAt, kills, headshots, rounds, team: firstTwo[0].team };
  }).filter((row): row is NonNullable<typeof row> => Boolean(row)).sort((a, b) => b.date.localeCompare(a.date)).slice(0, 20);
  if (matchLogs.length < 3) return null;
  const totals = matchLogs.reduce((sum, row) => sum + row.kills, 0);
  const rounds = matchLogs.reduce((sum, row) => sum + row.rounds, 0);
  const recent = matchLogs.slice(0, 8);
  const recentKills = recent.reduce((sum, row) => sum + row.kills, 0);
  const recentRounds = recent.reduce((sum, row) => sum + row.rounds, 0);
  const latestTeam = matchLogs[0].team;
  const stableMatches = matchLogs.slice(0, 5).filter((row) => !latestTeam || row.team === latestTeam).length;
  const headshotShare = matchLogs.every((row) => row.headshots != null) ? matchLogs.reduce((sum, row) => sum + Number(row.headshots), 0) / Math.max(1, totals) : undefined;
  return {
    sampleMaps: matchLogs.length * 2,
    recentMaps: recent.length * 2,
    killsPerRound: totals / Math.max(1, rounds),
    recentKillsPerRound: recentKills / Math.max(1, recentRounds),
    headshotShare,
    expectedRounds: rounds / matchLogs.length,
    mapPoolAdjustment: 1,
    opponentAdjustment: 1,
    roleAdjustment: 1,
    rosterStable: stableMatches >= 4,
    mapCoverage: Math.min(1, matchLogs.length / 8),
    source: "manual",
    updatedAt: matchLogs[0].date,
    gameLogs: matchLogs.map((row) => line.market === "maps_1_2_headshots" ? Number(row.headshots ?? 0) : row.kills),
    mapAdjustedAverage: totals / matchLogs.length,
  };
}

export async function coverageForPlayers(players: string[]) {
  await ensureHistorySchema();
  const keys = [...new Set(players.map(playerKey).filter(Boolean))];
  if (!keys.length) return [];
  const rows = await getDb().select().from(playerMapLogs).where(inArray(playerMapLogs.playerKey, keys.slice(0, 300))).orderBy(desc(playerMapLogs.playedAt));
  const output = new Map<string, { player: string; maps: number; matches: Set<string>; latest: string }>();
  for (const row of rows) {
    const current = output.get(row.playerKey) ?? { player: row.player, maps: 0, matches: new Set<string>(), latest: row.playedAt };
    current.maps += 1;
    current.matches.add(row.matchKey);
    if (row.playedAt > current.latest) current.latest = row.playedAt;
    output.set(row.playerKey, current);
  }
  return keys.map((key) => { const item = output.get(key); return item ? { player: item.player, maps: item.maps, matches: item.matches.size, latest: item.latest } : { player: key, maps: 0, matches: 0, latest: "" }; });
}
