import { desc, eq } from "drizzle-orm";
import { getDb } from "../../../db";
import { boardImports } from "../../../db/schema";
import type { PropLine } from "../../../model/types";

export async function OPTIONS() {
  return new Response(null, { status: 204, headers: corsHeaders() });
}

export async function POST(request: Request) {
  try {
    const body = await request.json() as { lines?: PropLine[] };
    const lines = (body.lines ?? []).filter((line) => line.player && Number.isFinite(Number(line.line))).slice(0, 500);
    if (!lines.length) return Response.json({ error: "No valid CS2 lines supplied" }, { status: 400, headers: corsHeaders() });
    const [record] = await getDb().insert(boardImports).values({ source: "PrizePicks extension", propCount: lines.length, payloadJson: JSON.stringify(lines), status: "confirmed" }).returning({ id: boardImports.id });
    return Response.json({ id: record.id, count: lines.length }, { status: 201, headers: corsHeaders() });
  } catch (error) {
    return Response.json({ error: error instanceof Error ? error.message : "Board import failed" }, { status: 400, headers: corsHeaders() });
  }
}

export async function GET(request: Request) {
  try {
    const id = Number(new URL(request.url).searchParams.get("id"));
    const rows = Number.isFinite(id) && id > 0
      ? await getDb().select().from(boardImports).where(eq(boardImports.id, id)).limit(1)
      : await getDb().select().from(boardImports).orderBy(desc(boardImports.createdAt)).limit(1);
    if (!rows.length) return Response.json({ error: "Saved board not found" }, { status: 404, headers: corsHeaders() });
    return Response.json({ id: rows[0].id, lines: JSON.parse(rows[0].payloadJson), count: rows[0].propCount }, { headers: corsHeaders() });
  } catch (error) {
    return Response.json({ error: error instanceof Error ? error.message : "Could not load saved board" }, { status: 400, headers: corsHeaders() });
  }
}

function corsHeaders() {
  return { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "GET,POST,OPTIONS" };
}
