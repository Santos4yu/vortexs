import { desc } from "drizzle-orm";
import { getDb } from "../../../db";
import { evaluations } from "../../../db/schema";

export async function GET() {
  try {
    const rows = await getDb().select().from(evaluations).orderBy(desc(evaluations.createdAt)).limit(250);
    return Response.json({ records: rows });
  } catch (error) {
    return Response.json({ records: [], error: error instanceof Error ? error.message : "Database unavailable" }, { status: 503 });
  }
}

export async function POST(request: Request) {
  try {
    const payload = await request.json() as typeof evaluations.$inferInsert;
    const [record] = await getDb().insert(evaluations).values(payload).returning();
    return Response.json({ record }, { status: 201 });
  } catch (error) {
    return Response.json({ error: error instanceof Error ? error.message : "Could not save evaluation" }, { status: 400 });
  }
}
