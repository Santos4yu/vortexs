import { env } from "cloudflare:workers";

type ProviderEnv = { PANDASCORE_API_KEY?: string };

export async function GET() {
  const token = (env as unknown as ProviderEnv).PANDASCORE_API_KEY?.trim();
  if (!token) return Response.json({ connected: false, provider: "PandaScore", reason: "PANDASCORE_API_KEY is not configured" });
  try {
    const headers = { Authorization: `Bearer ${token}`, Accept: "application/json" };
    const response = await fetch("https://api.pandascore.co/csgo/matches/upcoming?per_page=1", { headers });
    if (!response.ok) return Response.json({ connected: false, fixturesConnected: false, historicalConnected: false, provider: "PandaScore", reason: `Provider returned ${response.status}` });
    const historical = await fetch("https://api.pandascore.co/csgo/players/26530/stats?games_count=1", { headers });
    return Response.json({
      connected: historical.ok,
      fixturesConnected: true,
      historicalConnected: historical.ok,
      provider: "PandaScore",
      reason: historical.ok ? undefined : historical.status === 403 ? "Fixture access only - PandaScore Historical is required" : `Historical endpoint returned ${historical.status}`,
    });
  } catch {
    return Response.json({ connected: false, provider: "PandaScore", reason: "Provider is unreachable" });
  }
}
