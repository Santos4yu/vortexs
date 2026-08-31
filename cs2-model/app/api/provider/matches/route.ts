import { env } from "cloudflare:workers";

type ProviderEnv = { PANDASCORE_API_KEY?: string };

export async function GET() {
  const token = (env as unknown as ProviderEnv).PANDASCORE_API_KEY?.trim();
  if (!token) return Response.json({ error: "PandaScore is not connected" }, { status: 503 });
  const url = new URL("https://api.pandascore.co/csgo/matches/upcoming");
  url.searchParams.set("per_page", "50");
  url.searchParams.set("sort", "begin_at");
  const response = await fetch(url, { headers: { Authorization: `Bearer ${token}`, Accept: "application/json" } });
  if (!response.ok) return Response.json({ error: `PandaScore returned ${response.status}` }, { status: response.status });
  const raw = await response.json() as Array<Record<string, unknown>>;
  const matches = raw.map((match) => {
    const opponents = Array.isArray(match.opponents) ? match.opponents as Array<{ opponent?: { id?: number; name?: string } }> : [];
    return {
      id: match.id,
      startTime: match.begin_at,
      status: match.status,
      bestOf: match.number_of_games,
      tournament: (match.tournament as { name?: string } | null)?.name ?? "",
      teams: opponents.map((entry) => ({ id: entry.opponent?.id, name: entry.opponent?.name ?? "TBD" })),
    };
  });
  return Response.json({ matches, source: "PandaScore" });
}
