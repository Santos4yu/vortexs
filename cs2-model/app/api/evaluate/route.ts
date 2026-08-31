import { gradeProp } from "../../../model/grader";
import type { PlayerEvidence, PropLine } from "../../../model/types";

export async function POST(request: Request) {
  try {
    const body = await request.json() as { line?: PropLine; evidence?: PlayerEvidence };
    if (!body.line || !body.evidence) return Response.json({ error: "line and evidence are required" }, { status: 400 });
    if (!body.line.player?.trim() || !Number.isFinite(body.line.line) || body.line.line <= 0) {
      return Response.json({ error: "valid player and line are required" }, { status: 400 });
    }
    return Response.json({ projection: gradeProp(body.line, body.evidence) });
  } catch {
    return Response.json({ error: "invalid evaluation payload" }, { status: 400 });
  }
}
