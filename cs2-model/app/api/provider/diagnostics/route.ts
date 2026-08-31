import { env } from "cloudflare:workers";

type ProviderEnv = { PANDASCORE_API_KEY?: string };

const keysOf = (value: unknown) => value && typeof value === "object" ? Object.keys(value as Record<string, unknown>) : [];

export async function GET(request: Request) {
  const token = (env as unknown as ProviderEnv).PANDASCORE_API_KEY?.trim();
  if (!token) return Response.json({ connected: false, error: "PandaScore key missing" }, { status: 503 });
  const playerName = new URL(request.url).searchParams.get("player")?.trim() || "nicoodoz";
  const headers = { Authorization: `Bearer ${token}`, Accept: "application/json" };

  try {
    const playersUrl = new URL("https://api.pandascore.co/csgo/players");
    playersUrl.searchParams.set("search[name]", playerName);
    playersUrl.searchParams.set("per_page", "10");
    const playersResponse = await fetch(playersUrl, { headers });
    const playersBody = await playersResponse.json() as unknown;
    const players = Array.isArray(playersBody) ? playersBody as Array<Record<string, unknown>> : [];
    const exact = players.find((player) => String(player.name ?? "").toLowerCase() === playerName.toLowerCase()) ?? players[0];
    if (!exact?.id) return Response.json({ connected: true, playerSearchStatus: playersResponse.status, playerFound: false, playerResponseKeys: keysOf(playersBody) });

    const statsResponse = await fetch(`https://api.pandascore.co/csgo/players/${exact.id}/stats?games_count=20`, { headers });
    const statsBody = await statsResponse.json() as unknown;
    const sample = Array.isArray(statsBody) ? statsBody[0] : statsBody;
    return Response.json({
      connected: true,
      playerSearchStatus: playersResponse.status,
      playerFound: true,
      player: { id: exact.id, name: exact.name, currentTeam: (exact.current_team as { name?: string } | null)?.name ?? null },
      historicalStatus: statsResponse.status,
      historicalUnlocked: statsResponse.ok,
      statsShape: Array.isArray(statsBody) ? "array" : typeof statsBody,
      statsKeys: keysOf(sample),
      nestedKeys: sample && typeof sample === "object" ? Object.fromEntries(Object.entries(sample as Record<string, unknown>).filter(([, value]) => value && typeof value === "object").slice(0, 12).map(([key, value]) => [key, keysOf(Array.isArray(value) ? value[0] : value)])) : {},
    });
  } catch (error) {
    return Response.json({ connected: false, error: error instanceof Error ? error.message : "Diagnostic failed" }, { status: 502 });
  }
}
