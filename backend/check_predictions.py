import sqlite3
conn = sqlite3.connect('vortex.db')
conn.row_factory = sqlite3.Row

rows = conn.execute("""
    SELECT tier, COUNT(*) as cnt, 
           SUM(CASE WHEN result=1 THEN 1 ELSE 0 END) as wins,
           SUM(CASE WHEN result=0 THEN 1 ELSE 0 END) as losses,
           SUM(CASE WHEN result IS NULL THEN 1 ELSE 0 END) as pending
    FROM predictions GROUP BY tier ORDER BY cnt DESC
""").fetchall()
print("=== All tiers in predictions ===")
for r in rows:
    tier = r["tier"] or "NULL"
    wins = r["wins"] or 0
    losses = r["losses"] or 0
    pending = r["pending"] or 0
    cnt = r["cnt"]
    decided = wins + losses
    pct = wins / decided * 100 if decided else 0
    print(f"  {tier:12s}  {wins}W / {losses}L / {pending}P  ({pct:.1f}% of {decided} decided)  [{cnt} total]")

total = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
es = conn.execute("SELECT COUNT(*) FROM predictions WHERE tier IN ('ELITE','STRONG')").fetchone()[0]
other = total - es
print(f"\nTotal: {total}  |  ELITE+STRONG: {es}  |  Other: {other}")

print("\n=== Sample non-ELITE/STRONG predictions ===")
samples = conn.execute("""
    SELECT player_name, stat_type, direction, tier, confidence, hit_rate, result, game_date
    FROM predictions 
    WHERE tier NOT IN ('ELITE','STRONG')
    ORDER BY game_date DESC
    LIMIT 15
""").fetchall()
for s in samples:
    result_str = "WIN" if s["result"]==1 else ("LOSS" if s["result"]==0 else "PENDING")
    print(f"  {s['player_name']:20s} {s['stat_type']:18s} {s['direction']:5s}  {s['tier']:8s} conf={s['confidence']}  hr={s['hit_rate']}  {result_str}  {s['game_date']}")

print("\n=== How non-ELITE/STRONG got in (check tiers) ===")
samples2 = conn.execute("""
    SELECT tier, confidence, hit_rate, confidence_tier, result, player_name
    FROM predictions 
    WHERE tier NOT IN ('ELITE','STRONG')
    LIMIT 10
""").fetchall()
for s in samples2:
    result_str = "WIN" if s["result"]==1 else ("LOSS" if s["result"]==0 else "PENDING")
    print(f"  tier={s['tier']:8s}  conf={s['confidence']}  hr={s['hit_rate']}  conf_tier={s['confidence_tier']}  {result_str}  {s['player_name']}")

conn.close()
