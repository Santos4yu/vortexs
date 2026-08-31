import { sql } from "drizzle-orm";
import { index, integer, real, sqliteTable, text } from "drizzle-orm/sqlite-core";

export const boardImports = sqliteTable("board_imports", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  source: text("source").notNull(),
  propCount: integer("prop_count").notNull(),
  payloadJson: text("payload_json").notNull(),
  status: text("status").notNull().default("confirmed"),
  createdAt: text("created_at").notNull().default(sql`CURRENT_TIMESTAMP`),
});

export const evaluations = sqliteTable("cs2_evaluations", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  importId: integer("import_id"),
  eventDate: text("event_date").notNull(),
  player: text("player").notNull(),
  team: text("team").notNull(),
  opponent: text("opponent").notNull(),
  market: text("market").notNull(),
  line: real("line").notNull(),
  side: text("side").notNull(),
  projection: real("projection").notNull(),
  probability: real("probability").notNull(),
  edgePp: real("edge_pp").notNull(),
  dataConfidence: integer("data_confidence").notNull(),
  tier: text("tier").notNull(),
  qualified: integer("qualified", { mode: "boolean" }).notNull(),
  modelVersion: text("model_version").notNull(),
  modelBProjection: real("model_b_projection"),
  conviction: integer("conviction"),
  exactLineHitRate: real("exact_line_hit_rate"),
  correlationKey: text("correlation_key"),
  evidenceJson: text("evidence_json").notNull(),
  warningsJson: text("warnings_json").notNull(),
  actual: real("actual"),
  result: text("result").notNull().default("pending"),
  createdAt: text("created_at").notNull().default(sql`CURRENT_TIMESTAMP`),
  gradedAt: text("graded_at"),
}, (table) => [
  index("idx_cs2_evaluations_created_at").on(table.createdAt),
  index("idx_cs2_evaluations_result_date").on(table.result, table.eventDate),
]);

export const playerMapLogs = sqliteTable("cs2_player_map_logs", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  source: text("source").notNull(),
  sourceMapId: text("source_map_id").notNull(),
  matchKey: text("match_key").notNull(),
  playedAt: text("played_at").notNull(),
  player: text("player").notNull(),
  playerKey: text("player_key").notNull(),
  team: text("team").notNull().default(""),
  opponent: text("opponent").notNull().default(""),
  mapNumber: integer("map_number").notNull(),
  mapName: text("map_name").notNull().default("Unknown"),
  kills: integer("kills").notNull(),
  headshots: integer("headshots"),
  rounds: integer("rounds").notNull(),
  sourceUrl: text("source_url").notNull(),
  importedAt: text("imported_at").notNull().default(sql`CURRENT_TIMESTAMP`),
}, (table) => [
  index("idx_cs2_map_logs_player_date").on(table.playerKey, table.playedAt),
  index("idx_cs2_map_logs_match_player").on(table.matchKey, table.playerKey),
]);
