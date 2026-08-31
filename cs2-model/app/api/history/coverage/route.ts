import { coverageForPlayers } from "../../../../providers/history-store";

export async function POST(request: Request) {
  try {
    const body = await request.json() as { players?: string[] };
    return Response.json({ coverage: await coverageForPlayers(body.players ?? []) });
  } catch (error) {
    return Response.json({ coverage: [], error: error instanceof Error ? error.message : "Coverage lookup failed" }, { status: 503 });
  }
}
