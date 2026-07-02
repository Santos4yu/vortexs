const Database = require('better-sqlite3');
const path = require('path');

const db = new Database(path.join(__dirname, 'vortex.db'));

db.exec(`
  CREATE TABLE IF NOT EXISTS props_board (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    player_name   TEXT    NOT NULL,
    sport         TEXT    NOT NULL,
    stat_type     TEXT    NOT NULL,
    line          REAL    NOT NULL,
    vortex_score  INTEGER NOT NULL,
    ev_percentage REAL    NOT NULL,
    case_summary  TEXT    NOT NULL,
    risk_summary  TEXT    NOT NULL,
    sportsbook    TEXT    NOT NULL,
    stats_json    TEXT    DEFAULT NULL,
    tier          TEXT    DEFAULT NULL
  )
`);

// Add new columns to existing DBs without wiping data
try { db.exec(`ALTER TABLE props_board ADD COLUMN stats_json TEXT DEFAULT NULL`); } catch {}
try { db.exec(`ALTER TABLE props_board ADD COLUMN tier TEXT DEFAULT NULL`); } catch {}

module.exports = db;
