import type { PropLine } from "../../../../model/types";
import { buildFreeBoardEvidence } from "../../../../providers/csapi";
import { buildStoredEvidence } from "../../../../providers/history-store";

export async function POST(request: Request) {
  try {
    const body = await request.json() as { lines?: PropLine[] };
    const lines = (body.lines ?? []).filter((line) => line.player && Number.isFinite(Number(line.line))).slice(0, 200);
    if (!lines.length) return Response.json({ error: "No valid lines supplied" }, { status: 400 });
    const free = await buildFreeBoardEvidence(lines);
    const evidence = await Promise.all(free.map(async (result) => {
      try {
        const line = lines.find((candidate) => candidate.player.toLowerCase() === result.player.toLowerCase() && candidate.market === result.market);
        const stored = line ? await buildStoredEvidence(line) : null;
        return stored ? { ...result, evidence: stored, status: "ready" as const, reason: undefined, matchedName: line?.player } : result;
      } catch {
        return result;
      }
    }));
    return Response.json({ source: "CS API", evidence, requested: lines.length });
  } catch (error) {
    return Response.json({ error: error instanceof Error ? error.message : "Free evidence scan failed" }, { status: 502 });
  }
}
