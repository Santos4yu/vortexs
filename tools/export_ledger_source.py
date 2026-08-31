import csv
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "vortex.db"
CSV_OUT = ROOT / "reports" / "vortex_prop_ledger.csv"
JSON_OUT = ROOT / "outputs" / "019fe32e-bc79-7fa3-aa8d-c2c03fa18948" / "ledger_source.json"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
available = {row[1] for row in conn.execute("PRAGMA table_info(predictions)")}
columns = [c for c in [
    "game_date", "logged_at", "sport", "player_name", "stat_type", "market_key",
    "side", "line", "tier", "vortex_score", "matchup_score", "matchup_label",
    "ev_percentage", "l5_rate", "l10_rate", "l20_rate", "season_avg", "proj_edge",
    "stability_tier", "pitcher_name", "pitcher_era", "park_factor", "best_book",
    "best_odds", "case_summary", "risk_summary", "commence_time", "result",
    "actual_value", "graded_at",
] if c in available]
rows = [dict(row) for row in conn.execute(
    f"SELECT {','.join(columns)} FROM predictions ORDER BY game_date DESC, id DESC"
)]
conn.close()

CSV_OUT.parent.mkdir(parents=True, exist_ok=True)
with CSV_OUT.open("w", newline="", encoding="utf-8-sig") as handle:
    writer = csv.DictWriter(handle, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)

JSON_OUT.parent.mkdir(parents=True, exist_ok=True)
JSON_OUT.write_text(json.dumps({"columns": columns, "rows": rows}, ensure_ascii=False), encoding="utf-8")
print(f"{len(rows)} rows -> {CSV_OUT}")
