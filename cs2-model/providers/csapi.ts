import type { PlayerEvidence, PropLine } from "../model/types";

const BASE = "https://api.csapi.de";
const TTL = 1000 * 60 * 60 * 6;
const cache = new Map<string, { expires: number; value: unknown }>();

type Team = { id: number; name: string };
type Player = { id: number; name: string; team?: Team; stats?: { k: number; N: number; adr?: number; kast?: number; rating?: number } };
type HistoryMap = { id: number; name: string; team1_score: number; team2_score: number };
type MatchHistory = { id: number; date: string; maps: HistoryMap[]; team1: Team; team2: Team };
type StatPlayer = { id: number; name: string; k: number };
type MapStat = { id: number; name: string; team1: { id: number; players: StatPlayer[] }; team2: { id: number; players: StatPlayer[] } };

const normalize = (value: string) => value.toLowerCase().normalize("NFKD").replace(/[^a-z0-9]/g, "");

async function getJson<T>(path: string): Promise<T> {
  const cached = cache.get(path);
  if (cached && cached.expires > Date.now()) return cached.value as T;
  const response = await fetch(`${BASE}${path}`, { headers: { Accept: "application/json", "User-Agent": "CS2-Prop-Lab/0.1" } });
  if (!response.ok) throw new Error(`CS API returned ${response.status} for ${path}`);
  const value = await response.json() as T;
  cache.set(path, { expires: Date.now() + TTL, value });
  return value;
}

async function mapLimit<T, R>(items: T[], limit: number, worker: (item: T) => Promise<R>) {
  const output = new Array<R>(items.length);
  let cursor = 0;
  async function run() {
    while (cursor < items.length) {
      const index = cursor++;
      output[index] = await worker(items[index]);
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, run));
  return output;
}

async function findPlayer(name: string) {
  const candidates = await getJson<Array<{ id: number; name: string }>>(`/players/?name=${encodeURIComponent(name)}&limit=8`);
  return candidates.find((player) => normalize(player.name) === normalize(name)) ?? candidates[0] ?? null;
}

function unwrapMaps(value: unknown): MapStat[] {
  if (Array.isArray(value)) return value as MapStat[];
  if (value && typeof value === "object" && Array.isArray((value as { value?: unknown }).value)) return (value as { value: MapStat[] }).value;
  return [];
}

export type EvidenceResult = { player: string; market: PropLine["market"]; matchedName?: string; evidence?: PlayerEvidence; status: "ready" | "thin" | "missing" | "unsupported"; reason?: string };

export async function buildFreeEvidence(line: PropLine): Promise<EvidenceResult> {
  if (line.market === "maps_1_2_headshots") return { player: line.player, market: line.market, status: "unsupported", reason: "Free source does not provide headshots" };
  const matched = await findPlayer(line.player);
  if (!matched) return { player: line.player, market: line.market, status: "missing", reason: "Player not found in free source" };
  const player = await getJson<Player>(`/players/${matched.id}`);
  if (!player.team?.id || !player.stats) return { player: line.player, market: line.market, matchedName: player.name, status: "missing", reason: "Current team or player baseline unavailable" };
  const history = await getJson<MatchHistory[]>(`/teams/${player.team.id}/matchhistory`);
  const recentHistory = [...history].sort((a, b) => b.date.localeCompare(a.date)).slice(0, 10);
  const logRows = await mapLimit(recentHistory, 5, async (match) => {
    try {
      const raw = await getJson<unknown>(`/matches/${match.id}/stats?by_map=true`);
      const maps = unwrapMaps(raw).filter((map) => map.id !== 0);
      const firstTwo = match.maps.slice(0, 2);
      let kills = 0;
      let rounds = 0;
      let mapsFound = 0;
      for (const played of firstTwo) {
        const mapStats = maps.find((map) => map.id === played.id || normalize(map.name) === normalize(played.name));
        if (!mapStats) continue;
        const allPlayers = [...(mapStats.team1?.players ?? []), ...(mapStats.team2?.players ?? [])];
        const stat = allPlayers.find((candidate) => candidate.id === player.id || normalize(candidate.name) === normalize(player.name));
        if (!stat) continue;
        kills += Number(stat.k) || 0;
        rounds += Number(played.team1_score || 0) + Number(played.team2_score || 0);
        mapsFound += 1;
      }
      return mapsFound === 2 ? { date: match.date, kills, rounds, mapsFound } : null;
    } catch {
      return null;
    }
  });
  const logs = logRows.filter((row): row is NonNullable<typeof row> => Boolean(row));
  const totalKills = logs.reduce((sum, row) => sum + row.kills, 0);
  const totalRounds = logs.reduce((sum, row) => sum + row.rounds, 0);
  const recent = logs.slice(0, 5);
  const recentKills = recent.reduce((sum, row) => sum + row.kills, 0);
  const recentRounds = recent.reduce((sum, row) => sum + row.rounds, 0);
  const expectedRounds = logs.length ? logs.reduce((sum, row) => sum + row.rounds, 0) / logs.length : 43;
  const baselinePerMap = Number(player.stats.k) || 0;
  const fallbackKpr = baselinePerMap > 0 ? baselinePerMap / 21.5 : 0;
  const latestDate = logs[0]?.date;
  const freshnessDays = latestDate ? (Date.now() - new Date(`${latestDate}T00:00:00Z`).getTime()) / 86400000 : Infinity;
  const evidence: PlayerEvidence = {
    playerId: player.id,
    sampleMaps: Math.max(Number(player.stats.N) || 0, logs.length * 2),
    recentMaps: logs.length * 2,
    killsPerRound: totalRounds > 0 ? totalKills / totalRounds : fallbackKpr,
    recentKillsPerRound: recentRounds > 0 ? recentKills / recentRounds : fallbackKpr,
    expectedRounds,
    mapPoolAdjustment: 1,
    opponentAdjustment: 1,
    roleAdjustment: 1,
    rosterStable: logs.length >= 4 && freshnessDays <= 120,
    // Coverage is measured against an eight-match target, not merely against
    // however many rows the free source happened to return. One log is 12.5%,
    // never a misleading 100%.
    mapCoverage: Math.min(1, logs.length / 8),
    source: "csapi",
    updatedAt: latestDate ? `${latestDate}T00:00:00Z` : undefined,
    gameLogs: logs.map((row) => row.kills),
    mapAdjustedAverage: baselinePerMap > 0 ? baselinePerMap * 2 : undefined,
  };
  const status = logs.length >= 4 && evidence.sampleMaps >= 12 ? "ready" : "thin";
  return { player: line.player, market: line.market, matchedName: player.name, evidence, status, reason: status === "thin" ? `Only ${logs.length} valid Maps 1-2 logs found` : undefined };
}

export async function buildFreeBoardEvidence(lines: PropLine[]) {
  const unique = [...new Map(lines.map((line) => [`${normalize(line.player)}|${line.market}`, line])).values()];
  return mapLimit(unique, 5, async (line) => {
    try { return await buildFreeEvidence(line); }
    catch (error) { return { player: line.player, market: line.market, status: "missing", reason: error instanceof Error ? error.message : "Free data lookup failed" } as EvidenceResult; }
  });
}
