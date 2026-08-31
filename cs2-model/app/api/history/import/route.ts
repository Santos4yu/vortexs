import { importMapLogs, type ImportedMapLog } from "../../../../providers/history-store";

export async function POST(request: Request) {
  try {
    const body = await request.json() as { rows?: ImportedMapLog[] };
    const rows = Array.isArray(body.rows) ? body.rows : [];
    if (!rows.length) return Response.json({ error: "No map logs supplied" }, { status: 400, headers: corsHeaders() });
    return Response.json(await importMapLogs(rows), { status: 201, headers: corsHeaders() });
  } catch (error) {
    return Response.json({ error: error instanceof Error ? error.message : "History import failed" }, { status: 400, headers: corsHeaders() });
  }
}

export async function OPTIONS() {
  return new Response(null, { status: 204, headers: corsHeaders() });
}

function corsHeaders() {
  return { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST,OPTIONS" };
}
