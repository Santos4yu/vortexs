require('dotenv').config();
const {
  Client,
  GatewayIntentBits,
  EmbedBuilder,
  ActivityType,
} = require('discord.js');
const db = require('./db');

// ── Constants ──────────────────────────────────────────────────────────────

const DIVIDER  = '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━';
const THIN_DIV = '┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄';

// Tier → embed left-border color
const TIER_COLOR = {
  ELITE:  0x00C853,   // green
  STRONG: 0x2979FF,   // blue
  LEAN:   0xFFAB00,   // gold/amber
  null:   0x5865F2,   // Discord blurple (NBA / no stats)
  undefined: 0x5865F2,
};

// ── Formatting helpers ─────────────────────────────────────────────────────

function sportEmoji(sport) {
  return { NBA: '🏀', MLB: '⚾', NFL: '🏈', NHL: '🏒' }[sport?.toUpperCase()] ?? '🎯';
}

function tierBadge(tier) {
  return { ELITE: '💎 ELITE', STRONG: '💠 STRONG', LEAN: '🔹 LEAN' }[tier] ?? '⬜ UNRATED';
}

function scoreBar(score) {
  const filled = Math.round(score / 10);
  return '█'.repeat(filled) + '░'.repeat(10 - filled) + `  **${score}**/100`;
}

function fmtEv(ev) {
  const sign = ev >= 0 ? '+' : '';
  const icon = ev >= 8 ? '🟢' : ev >= 4 ? '🟡' : ev >= 0 ? '⚪' : '🔴';
  return `${icon} ${sign}${ev.toFixed(1)}%`;
}

function hitRateEmoji(rate) {
  if (rate >= 80) return '🔥';
  if (rate >= 60) return '✅';
  if (rate >= 40) return '⚠️';
  return '❌';
}

function fmtRate(r) {
  if (!r) return 'n/a';
  const icon = hitRateEmoji(r.rate);
  return `${icon} **${r.rate}%** (${r.hits}/${r.games}) · avg **${r.avg}**`;
}

// ── Stats-aware card builder ───────────────────────────────────────────────

/**
 * Build a single prop card as an array of { name, value } field pairs.
 * Uses deep statistical data when stats_json is present; falls back to
 * the stored case_summary / risk_summary text for NBA / no-stats rows.
 */
function buildPropFields(prop, index) {
  const stats = prop.stats_json ? JSON.parse(prop.stats_json) : null;

  // ── Header line ────────────────────────────────────────────────────────
  const tierStr  = tierBadge(prop.tier);
  const evStr    = fmtEv(prop.ev_percentage);
  const header   =
    `${THIN_DIV}\n` +
    `${sportEmoji(prop.sport)} **${prop.player_name}**  ·  ${prop.sport}\n` +
    `\`O ${prop.line} ${prop.stat_type}\`  via **${prop.sportsbook}**  ·  ${tierStr}`;

  const metrics =
    `> **Vortex Score** ${scoreBar(prop.vortex_score)}\n` +
    `> **EV Edge**      ${evStr}`;

  const fields = [
    { name: `#${index + 1}  ·  ${header}`, value: metrics, inline: false },
  ];

  // ── Stats-enriched layout (MLB with stats_json) ────────────────────────
  if (stats) {
    const splits  = stats.splits  ?? {};
    const pitcher = stats.pitcher ?? {};
    const bvp     = stats.bvp     ?? {};
    const trend   = stats.trend_signal ?? '';
    const platoon = stats.platoon_note ?? '';

    // 📈  Multi-window hit rates
    const l5str  = fmtRate(splits.l5);
    const l10str = fmtRate(splits.l10);
    const l20str = fmtRate(splits.l20);
    const avgStr = splits.season_avg != null
      ? `Season avg **${splits.season_avg}** over **${splits.games_played}** G`
      : '';

    const streakNote = (() => {
      const s = splits.l5?.streak ?? 0;
      if (s >= 4)  return `\n🔥 **${s}-game hit streak** active`;
      if (s <= -3) return `\n⚠️ **${Math.abs(s)}-game miss streak** active`;
      return '';
    })();

    const hitRates =
      `📈 **L5:** ${l5str}\n` +
      `📈 **L10:** ${l10str}\n` +
      `📈 **L20:** ${l20str}\n` +
      (avgStr ? `📊 ${avgStr}${streakNote}` : streakNote || '​');

    fields.push({ name: '​', value: hitRates, inline: false });

    // 🎯  Matchup
    if (pitcher.name) {
      const matchup =
        `🎯 **Pitcher:** ${pitcher.name} (${pitcher.hand ?? '?'}HP)\n` +
        `🎯 **ERA** ${pitcher.era ?? '—'}  ·  **FIP** ${pitcher.fip ?? '—'}  ·  ` +
        `**K/9** ${pitcher.k_per_9 ?? '—'}  ·  **HR/9** ${pitcher.hr_per_9 ?? '—'}\n` +
        `🎯 **WHIP** ${pitcher.whip ?? '—'}  ·  **AVG against** ${pitcher.avg_against ?? '—'}\n` +
        (platoon ? `🎯 **Platoon:** ${platoon}` : '​');
      fields.push({ name: '​', value: matchup, inline: false });
    }

    // 👥  BvP
    if (bvp && bvp.ab >= 5) {
      const bvpBlock =
        `👥 **BvP (${bvp.sample}):**  ` +
        `${bvp.ab} AB  ·  AVG **${bvp.avg}**  ·  **${bvp.hr}** HR  ·  **${bvp.k}** K  ·  OPS ${bvp.ops}`;
      fields.push({ name: '​', value: bvpBlock, inline: false });
    } else if (bvp) {
      fields.push({
        name:  '​',
        value: '👥 **BvP:** No significant head-to-head history.',
        inline: false,
      });
    }

    // 💡  WHY
    const trendIcon = trend.includes('HOT') ? '🔥' : trend.includes('COLD') ? '❄️' : '📊';
    const why =
      `💡 **Trend:** ${trendIcon} ${trend}\n` +
      `💡 **Best price:** ${prop.sportsbook}`;
    fields.push({ name: '​', value: why, inline: false });

    // ⚠️  Risk
    const riskValue = prop.risk_summary?.slice(0, 900) ?? '—';
    fields.push({ name: '​', value: `⚠️ **Risk**\n${riskValue}`, inline: false });

  // ── Odds-only layout (NBA or no pitcher found) ────────────────────────
  } else {
    const caseValue = prop.case_summary?.slice(0, 900) ?? '—';
    const riskValue = prop.risk_summary?.slice(0, 900) ?? '—';
    fields.push(
      { name: '​', value: `💡 **Case**\n${caseValue}`,  inline: false },
      { name: '​', value: `⚠️ **Risk**\n${riskValue}`,  inline: false },
    );
  }

  return fields;
}

// ── Embed builder ──────────────────────────────────────────────────────────

/**
 * Build one embed for a slice of props.
 * Each embed holds PAGE_SIZE props.  The color is driven by the highest
 * tier on that page so the sidebar is always meaningful.
 */
function buildBoardEmbed(props, pageNum, totalPages) {
  const now = new Date().toLocaleDateString('en-US', {
    weekday: 'short', month: 'short', day: 'numeric', year: 'numeric',
  });

  // Pick color from the best tier present on this page
  const TIER_RANK = { ELITE: 3, STRONG: 2, LEAN: 1 };
  const topTier   = props.reduce((best, p) => {
    return (TIER_RANK[p.tier] ?? 0) > (TIER_RANK[best] ?? 0) ? p.tier : best;
  }, null);
  const color = TIER_COLOR[topTier] ?? TIER_COLOR[null];

  const pageLabel = totalPages > 1 ? `  ·  page ${pageNum}/${totalPages}` : '';

  const embed = new EmbedBuilder()
    .setColor(color)
    .setTitle('💠  V O R T E X  ·  A C T I V E  B O A R D')
    .setDescription(
      `*Data-driven props — filtered, scored, ranked*\n` +
      `\`${now}\`  ·  **${props.length} prop${props.length !== 1 ? 's' : ''}**${pageLabel}\n` +
      DIVIDER,
    )
    .setFooter({ text: 'Vortex · For informational purposes only. Gamble responsibly.' })
    .setTimestamp();

  // Accumulate fields; track char count to stay under Discord's 6000-char limit
  let charCount = embed.data.description?.length ?? 0;
  const CHAR_LIMIT = 5800;

  for (const [i, prop] of props.entries()) {
    const fields = buildPropFields(prop, i);
    for (const f of fields) {
      const fieldLen = (f.name?.length ?? 0) + (f.value?.length ?? 0);
      if (charCount + fieldLen > CHAR_LIMIT) break;
      embed.addFields(f);
      charCount += fieldLen;
    }
  }

  return embed;
}

// ── Bot client ─────────────────────────────────────────────────────────────

const client = new Client({
  intents:  [GatewayIntentBits.Guilds, GatewayIntentBits.DirectMessages],
  partials: ['CHANNEL'],
});

client.once('ready', () => {
  console.log(`✅  Logged in as ${client.user.tag}`);
  client.user.setActivity('the board 💠', { type: ActivityType.Watching });
});

client.on('interactionCreate', async (interaction) => {
  if (!interaction.isChatInputCommand()) return;
  if (interaction.commandName !== 'menu') return;

  await interaction.deferReply({ ephemeral: false });

  try {
    const props = db.prepare(
      'SELECT * FROM props_board ORDER BY vortex_score DESC'
    ).all();

    if (props.length === 0) {
      await interaction.editReply({
        content:
          '📭  The board is empty right now.\n' +
          'Run the data engine to fetch today\'s plays:\n' +
          '```\nbackend\\.venv\\Scripts\\python backend/update_board.py\n```',
      });
      return;
    }

    // 1 prop per embed — richer layout, stays well under the 6000-char limit
    const PAGE_SIZE  = 1;
    const pages      = [];
    for (let i = 0; i < props.length; i += PAGE_SIZE) {
      pages.push(props.slice(i, i + PAGE_SIZE));
    }
    const totalPages = pages.length;

    await interaction.editReply({
      embeds: [buildBoardEmbed(pages[0], 1, totalPages)],
    });

    for (let p = 1; p < pages.length; p++) {
      await interaction.followUp({
        embeds: [buildBoardEmbed(pages[p], p + 1, totalPages)],
      });
    }
  } catch (err) {
    console.error('Error handling /menu:', err);
    await interaction.editReply({
      content: '❌  Something went wrong loading the board. Please try again.',
    });
  }
});

client.login(process.env.DISCORD_TOKEN);
