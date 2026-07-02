import sqlite3
conn = sqlite3.connect('vortex.db')
conn.row_factory = sqlite3.Row

rows = conn.execute("""
    SELECT tier, COUNT(*) as cnt, 
           SUM(CASE WHEN result=1 THEN 1 ELSE 0 END) as wins 
    FROM predictions GROUP BY tier ORDER BY cnt DESC
""").fetchall()
print('=== All tiers in predictions ===')
for r in rows:
    tier = r['tier'] or 'NULL'
    wins = r['wins'] or 0
    cnt = r['cnt']
    pct = wins / cnt * 100 if cnt else 0
    print(f'  {tier:10s}  {wins}/{cnt}  ({pct:.1f}%)')

total = conn.execute('SELECT COUNT(*) FROM predictions').fetchone()[0]
es = conn.execute("SELECT COUNT(*) FROM predictions WHERE tier IN ('ELITE','STRONG')").fetchone()[0]
other = total - es
print(f'\nTotal: {total}  |  ELITE+STRONG: {es}  |  Other (to delete): {other}')

if other > 0:
    conn.execute("DELETE FROM predictions WHERE tier NOT IN ('ELITE','STRONG')")
    conn.commit()
    print(f'\nDeleted {other} non-ELITE/STRONG predictions.')
    total2 = conn.execute('SELECT COUNT(*) FROM predictions').fetchone()[0]
    wins2 = conn.execute('SELECT SUM(CASE WHEN result=1 THEN 1 ELSE 0 END) FROM predictions').fetchone()[0] or 0
    print(f'Remaining: {total2} predictions, {wins2}/{total2} ({wins2/total2*100:.1f}%)')
else:
    print('\nNothing to delete.')

conn.close()
