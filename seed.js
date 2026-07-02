require('dotenv').config();
const db = require('./db');

const props = [
  {
    player_name:   'Nikola Jokic',
    sport:         'NBA',
    stat_type:     'Points',
    line:          26.5,
    vortex_score:   91,
    ev_percentage: 8.4,
    case_summary:
      'Jokic is averaging 29.1 pts over his last 10 games with a usage rate spike to 34.2% following Murray\'s absence. Denver is 3rd in pace over that stretch, inflating volume. Matchup vs. OKC projects as a neutral defensive scheme — no elite wing assigned to him in recent film. Implied total sits at 225.5, favoring high-output floors.',
    risk_summary:
      'Monitor Murray injury report (questionable). If Murray plays, usage reverts ~2-3 pts. Blowout risk is moderate given OKC\'s recent form. Line movement from -115 to -120 suggests sharp money on the over.',
    sportsbook: 'DraftKings',
  },
  {
    player_name:   'Freddie Freeman',
    sport:         'MLB',
    stat_type:     'Total Bases',
    line:          1.5,
    vortex_score:   88,
    ev_percentage: 7.1,
    case_summary:
      'Freeman has gone over 1.5 total bases in 7 of his last 9 starts. Facing RHP Kyle Gibson, who carries a .312 xBA against LHBs and is allowing a 42.1% hard-hit rate in 2024. Dodger Stadium day game — historically favorable for Freeman\'s pull-side power. Lineup construction remains elite with Ohtani batting second.',
    risk_summary:
      'Gibson has shown deceptive velocity in his last two outings; swinging-strike rate has ticked up. Wind is currently projected in from LF at 10 mph, suppressing fly-ball distance. Moderate weather risk — check pre-game conditions.',
    sportsbook: 'FanDuel',
  },
  {
    player_name:   'Jayson Tatum',
    sport:         'NBA',
    stat_type:     'Points + Rebounds + Assists',
    line:          44.5,
    vortex_score:   85,
    ev_percentage: 6.3,
    case_summary:
      'Tatum\'s PRA combo has cleared 44.5 in 6 of his last 8 games, with a median of 48.2. Boston is playing on normal rest against a Miami Heat defense ranked 22nd in defensive rating this month. Tatum\'s assist volume has increased as Brown deals with a minor hand issue, raising his floor in this market considerably.',
    risk_summary:
      'Heat\'s Jimmy Butler is a proven wing stopper who has historically suppressed Tatum\'s scoring efficiency (-4.1 pts per game in matchup history). Foul trouble is a recurring concern — Tatum fouled out once this month. Line is at juice -130, reducing raw EV.',
    sportsbook: 'BetMGM',
  },
  {
    player_name:   'Shohei Ohtani',
    sport:         'MLB',
    stat_type:     'Strikeouts (Batter)',
    line:          0.5,
    vortex_score:   79,
    ev_percentage: 5.5,
    case_summary:
      'Ohtani has recorded at least 1 strikeout in 18 of his last 20 plate-appearance games. Opposing SP Zack Wheeler carries a 29.8% K-rate vs. LHBs this season and relies heavily on his sweeper — a pitch Ohtani has whiffed on at 38% in 2024. Positive matchup on paper with strikeout upside.',
    risk_summary:
      'This is a razor-thin line — at 0.5, a single AB result can sink the prop. Wheeler may be on a strict pitch count (85-90) limiting exposure. If Ohtani reaches base early via walk, K opportunities drop. Line value is moderate; bankroll sizing should reflect the binary nature of this market.',
    sportsbook: 'Caesars',
  },
  {
    player_name:   'Anthony Edwards',
    sport:         'NBA',
    stat_type:     'Points',
    line:          28.5,
    vortex_score:   83,
    ev_percentage: 6.8,
    case_summary:
      'Edwards has scored 30+ in 5 straight road games and is in a clear shot-creation role with Towns shooting under 40% over his last 6. The Spurs rank 28th in perimeter defense and allow the 4th-most FG attempts to opposing SGs. Vegas implied total of 228.5 points supports a high-volume shooting environment for Edwards.',
    risk_summary:
      'Edwards has shown inconsistency against strong athletic perimeter defenders. If game pace slows below 97 possessions, scoring volume compresses for both teams. Line movement is flat — no sharp signal detected. Ensure line is available at -115 or better for positive EV threshold.',
    sportsbook: 'DraftKings',
  },
];

const insert = db.prepare(`
  INSERT INTO props_board
    (player_name, sport, stat_type, line, vortex_score, ev_percentage, case_summary, risk_summary, sportsbook)
  VALUES
    (@player_name, @sport, @stat_type, @line, @vortex_score, @ev_percentage, @case_summary, @risk_summary, @sportsbook)
`);

const count = db.prepare('SELECT COUNT(*) AS cnt FROM props_board').get();
if (count.cnt === 0) {
  const seedMany = db.transaction((rows) => {
    for (const row of rows) insert.run(row);
  });
  seedMany(props);
  console.log(`✅  Seeded ${props.length} props into props_board.`);
} else {
  console.log(`ℹ️   props_board already has ${count.cnt} rows — skipping seed.`);
}
