/* ── VORTEX Web App ───────────────────────────────────────────────────────── */

// ── Particle background ─────────────────────────────────────────────────────
const canvas = document.getElementById('bg');
const ctx = canvas.getContext('2d');
let particles = [];

function resize() {
  canvas.width = window.innerWidth;
  canvas.height = window.innerHeight;
}
window.addEventListener('resize', resize);
resize();

class Particle {
  constructor() {
    this.x = Math.random() * canvas.width;
    this.y = Math.random() * canvas.height;
    this.size = Math.random() * 2 + 0.5;
    this.vx = (Math.random() - 0.5) * 0.4;
    this.vy = (Math.random() - 0.5) * 0.4;
    this.alpha = Math.random() * 0.4 + 0.1;
    this.hue = 0;
    this.sat = 0;
    this.light = 80 + Math.random() * 20;
  }
  update() {
    this.x += this.vx;
    this.y += this.vy;
    if (this.x < 0 || this.x > canvas.width) this.vx *= -1;
    if (this.y < 0 || this.y > canvas.height) this.vy *= -1;
  }
  draw() {
    ctx.beginPath();
    ctx.arc(this.x, this.y, this.size, 0, Math.PI * 2);
    ctx.fillStyle = `hsla(0, 0%, ${this.light}%, ${this.alpha})`;
    ctx.fill();
  }
}

for (let i = 0; i < 100; i++) particles.push(new Particle());

function connect() {
  for (let i = 0; i < particles.length; i++) {
    for (let j = i + 1; j < particles.length; j++) {
      const dx = particles[i].x - particles[j].x;
      const dy = particles[i].y - particles[j].y;
      const dist = Math.sqrt(dx * dx + dy * dy);
      if (dist < 100) {
        ctx.beginPath();
        ctx.moveTo(particles[i].x, particles[i].y);
        ctx.lineTo(particles[j].x, particles[j].y);
        ctx.strokeStyle = `rgba(255, 255, 255, ${0.04 * (1 - dist / 100)})`;
        ctx.lineWidth = 0.4;
        ctx.stroke();
      }
    }
  }
}

function animate(time) {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  particles.forEach(p => { p.update(); p.draw(); });
  connect();
  requestAnimationFrame(animate);
}
requestAnimationFrame(animate);

// ── API helpers ──────────────────────────────────────────────────────────────
// Set API_BASE to your backend URL when deploying frontend separately (e.g. Netlify)
// e.g. const API_BASE = 'https://vortex-api.example.com';
const API_BASE = window.VORTEX_API_BASE || '';

// Fix login link for cross-origin deployments
if (API_BASE) {
  const loginLink = document.getElementById('login-link');
  if (loginLink) loginLink.href = API_BASE + '/auth/login';
}

async function api(path) {
  const res = await fetch(API_BASE + path, { credentials: 'include' });
  if (res.status === 401) {
    document.getElementById('login-overlay').classList.remove('hidden');
    return null;
  }
  return res.json();
}

async function checkAuth() {
  const data = await api('/auth/me');
  if (!data || !data.username) {
    document.getElementById('login-overlay').classList.remove('hidden');
    return false;
  }
  document.getElementById('login-overlay').classList.add('hidden');
  const area = document.getElementById('user-area');
  const avatarUrl = data.avatar
    ? `https://cdn.discordapp.com/avatars/${data.id}/${data.avatar}.png`
    : 'https://cdn.discordapp.com/embed/avatars/0.png';
  area.innerHTML = `
    <img class="avatar" src="${avatarUrl}" />
    <span>${data.username}</span>
    <a href="/auth/logout" class="logout">logout</a>
  `;
  return true;
}

// ── Navigation ───────────────────────────────────────────────────────────────

document.querySelectorAll('.nav-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    document.getElementById(`view-${btn.dataset.view}`).classList.add('active');
    loadView(btn.dataset.view);
  });
});

function loadView(view) {
  switch (view) {
    case 'picks': loadPicks(); break;
    case 'elite': loadElite(); break;
    case 'nrfi': loadNRFI(); break;
    case 'record': loadRecord(); break;
    case 'parlay': loadParlay(); break;
  }
}

// ── Picks ────────────────────────────────────────────────────────────────────

let allPicks = [];

async function loadPicks(tier = 'all') {
  const list = document.getElementById('picks-list');
  list.innerHTML = '<div class="loading">🌀 Loading plays...</div>';
  const data = await api('/api/picks');
  if (!data) return;
  allPicks = data.picks || [];
  document.querySelectorAll('.tier-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.tier === tier);
  });
  const filtered = tier === 'all' ? allPicks : allPicks.filter(p => p.tier === tier);
  renderPicks(filtered, list);
}

document.querySelectorAll('.tier-btn').forEach(btn => {
  btn.addEventListener('click', () => loadPicks(btn.dataset.tier));
});

function renderPicks(picks, container) {
  if (!picks.length) {
    container.innerHTML = '<div class="error">No plays found</div>';
    return;
  }
  container.innerHTML = picks.map((p, i) => {
    const side = (p.side || 'O').toUpperCase().charAt(0);
    const sideLabel = side === 'U' ? 'U' : 'O';
    const sideColor = side === 'U' ? 'var(--red)' : 'var(--green)';
    return `
    <div class="card pick-card" onclick="openPickDetail('${p.player.replace(/'/g, "\\'")}','${p.prop.replace(/'/g, "\\'")}')" style="animation-delay:${i * 30}ms">
      <span class="tier-badge tier-${p.tier}">${p.tier}</span>
      <span class="player-name">${p.player}</span>
      <span class="prop-info">${p.prop.replace(/_/g, ' ')} <span style="color:${sideColor};font-weight:700">${sideLabel}</span>${p.line}</span>
      <span class="score">${p.score}</span>
      <span class="ev-badge">+${p.ev}%</span>
      <button class="parlay-add-btn" onclick="event.stopPropagation();addToParlay(${JSON.stringify(p).replace(/"/g, '&quot;')})" title="Add to parlay">+</button>
    </div>`;
  }).join('');
}

// ── Pick Detail Modal ────────────────────────────────────────────────────────

async function openPickDetail(player, stat) {
  const modal = document.getElementById('pick-modal');
  const body = document.getElementById('pick-detail-body');
  modal.classList.add('active');
  body.innerHTML = '<div class="loading">Loading analysis...</div>';

  const data = await api(`/api/pick?player=${encodeURIComponent(player)}&stat=${encodeURIComponent(stat)}`);
  if (!data || data.error) {
    body.innerHTML = `<div class="error">${data?.error || 'Failed to load'}</div>`;
    return;
  }

  const l5 = data.l5 || {};
  const l10 = data.l10 || {};
  const l20 = data.l20 || {};
  const pitcher = data.pitcher || {};
  const bvp = data.bvp || {};
  const sc = data.statcast || {};
  const homeAway = data.home_away || {};
  const lastVals = data.last_values || [];
  const bullpen = data.bullpen || {};
  const powerShape = data.power_shape || {};
  const weatherObj = data.weather || {};
  const oppK = data.opp_k || {};
  const last5 = data.last_5_starts || [];
  const scoreBd = data.score_breakdown || {};
  const seasonStats = data.season_stats || {};

  const isUnder = data.side === 'under';
  const sideLabel = isUnder ? 'UNDER' : 'OVER';
  const sideColor = isUnder ? 'var(--red)' : 'var(--green)';
  const trendIcon = {HOT:'🔥',WARM:'✅',COLD:'❄️',NEUTRAL:'➡️'}[data.trend] || '➡️';

  const pid = data.player_id || '';
  const headshotUrl = pid
    ? `https://img.mlbstatic.com/mlb-photos/image/upload/d_people:generic:headshot:67:current.png/w_120,q_auto:best/v1/people/${pid}/headshot/67/current`
    : '';

  const lineVal = parseFloat(data.line) || 0;
  const effL10 = data.eff_l10 != null ? data.eff_l10 : (l10.hr != null ? l10.hr : null);
  const effL5 = data.eff_l5 != null ? data.eff_l5 : (l5.hr != null ? l5.hr : null);
  const l10Avg = l10.hits != null && l10.games ? (l10.hits / l10.games) : null;
  const gap = l10Avg != null && lineVal ? (l10Avg - lineVal) : null;
  const gapStr = gap != null ? (gap >= 0 ? '+' + gap.toFixed(1) : gap.toFixed(1)) : null;

  let stability = 'Low', stabColor = 'var(--red)';
  if (effL10 != null) {
    if (effL10 >= 80) { stability = 'High'; stabColor = 'var(--green)'; }
    else if (effL10 >= 60) { stability = 'Medium'; stabColor = 'var(--orange)'; }
  }

  const dmgScore = data.damage_score || 0;
  const dmgLabel = dmgScore >= 4 ? 'Elite' : dmgScore >= 2 ? 'High' : dmgScore >= 1 ? 'Medium' : '';
  const dmgIcon = dmgScore >= 4 ? '🔥' : dmgScore >= 2 ? '💥' : dmgScore >= 1 ? '📊' : '';

  const unit = data.tier === 'ELITE' ? '1.0u' : data.tier === 'STRONG' ? '0.75u' : data.tier === 'GOOD' ? '0.5u' : '0.25u';
  const actionText = ['ELITE','STRONG','GOOD'].includes(data.tier) ? `Play it — ${unit}` : data.tier === 'LEAN' ? `Lean only — ${unit}` : 'Fade';

  // Streak
  let streak = '';
  if (lastVals.length >= 3) {
    let c = 0;
    for (const v of lastVals) {
      if (isUnder ? v < lineVal : v >= lineVal) c++; else break;
    }
    if (c >= 3) streak = `${c}-game ${sideLabel} streak`;
  }

  // Trend line
  let trendLine = '';
  if (effL5 != null && effL10 != null) {
    if (effL5 - effL10 >= 20) trendLine = `Trending up — ${effL5}% L5 vs ${effL10}% L10`;
    else if (effL10 - effL5 >= 20) trendLine = `Trending down — ${effL5}% L5 vs ${effL10}% L10`;
  }

  // Pitcher display (batter props)
  const pitcherName = pitcher.name || data.pitcher_name || 'TBD';
  const pEra = pitcher.era || seasonStats.era || '—';
  const pWhip = pitcher.whip || seasonStats.whip || '—';
  const pK9 = pitcher.k_per_9 || seasonStats.k_per_9 || '—';
  const pHand = pitcher.hand || '?';
  const pHr9 = pitcher.hr_per_9 || '—';
  const pFip = pitcher.fip || '—';
  const pId = pitcher.pitcher_id || pitcher.id || '';

  // Opponent K rate context
  let oppKLine = '';
  const oppKpct = data.opp_kpct || oppK.k_pct;
  const oppKrank = oppK.rank;
  if (oppKpct != null && oppKrank) {
    const kPctDisplay = (oppKpct * 100).toFixed(1);
    const isKProne = oppKpct >= 0.22;
    const kVerdict = (isUnder && !isKProne) || (!isUnder && isKProne) ? 'favors' : 'works against';
    oppKLine = `#${oppKrank}/30 in K rate (${kPctDisplay}%) — ${kVerdict} ${sideLabel}`;
  }

  // Matchup narrative
  const spotIcon = data.is_home === true ? 'Home' : data.is_home === false ? 'Away' : '';
  const lineupPos = data.lineup_pos;
  const paMap = {1:4.5,2:4.4,3:4.2,4:4.1,5:3.9,6:3.8,7:3.7,8:3.6,9:3.5};
  const paProj = lineupPos ? (paMap[lineupPos] || 4.0) : null;

  // Handedness
  const batterHand = pitcher.batter_hand || '';
  const isFavPlatoon = (batterHand === 'R' && pHand === 'L') || (batterHand === 'L' && pHand === 'R');
  const isSwitch = batterHand === 'S';
  let platoonLine = '';
  if (data.platoon) {
    platoonLine = data.platoon;
  } else if (batterHand && pHand !== '?' && pHand !== '?') {
    if (isSwitch) platoonLine = `Switch hitter vs ${pHand === 'L' ? 'left' : 'right'}-handed pitcher`;
    else if (isFavPlatoon) platoonLine = `Favorable platoon — ${batterHand}HP vs ${pHand === 'L' ? 'LHP' : 'RHP'}`;
    else platoonLine = `Same-side matchup — ${batterHand}HP vs ${pHand === 'L' ? 'LHP' : 'RHP'}`;
  }

  // Last 5 starts (pitcher K props)
  let last5Html = '';
  if (last5.length && data.is_pitcher) {
    const ks = last5.map(s => s.k || s.value || '?').join('K  ');
    last5Html = `<div style="font-size:0.85rem;color:var(--text-dim);margin-top:4px;">Last 5 starts: <span style="color:var(--text)">${ks}K</span></div>`;
  }

  // BvP
  let bvpHtml = '';
  if (bvp.ab >= 5) {
    bvpHtml = `<div style="margin-top:6px;font-size:0.85rem;">BvP: ${bvp.hits || 0}/${bvp.ab} AB — ${bvp.avg} AVG${bvp.k != null ? ' — ' + bvp.k + ' K' : ''}</div>`;
  } else if (bvp.ab > 0) {
    bvpHtml = `<div style="margin-top:6px;font-size:0.85rem;color:var(--text-dim);">BvP: Small sample (${bvp.ab} AB)</div>`;
  }

  // Last values bar
  const lastValBar = lastVals.length ? `
    <div class="detail-section">
      <div class="detail-section-title">Last ${lastVals.length} Games</div>
      <div class="detail-bar-row">
        ${lastVals.map(v => {
          const hit = isUnder ? v < lineVal : v >= lineVal;
          const barH = Math.max(8, (v / Math.max(lineVal * 2, 3)) * 44);
          return `<div class="detail-bar-col">
            <span class="detail-bar-val" style="color:${hit ? 'var(--green)' : 'var(--text-dim)'}">${v}</span>
            <div class="detail-bar" style="height:${barH}px;background:${hit ? 'var(--green)' : 'rgba(255,255,255,0.1)'};opacity:${hit ? 1 : 0.3};"></div>
          </div>`;
        }).join('')}
      </div>
    </div>` : '';

  // Statcast
  let statcastHtml = '';
  const scParts = [];
  if (sc.barrel_pct) scParts.push({label:'Barrel%', val:sc.barrel_pct+'%', w:Math.min(100,sc.barrel_pct*3)});
  if (sc.hard_hit_pct) scParts.push({label:'Hard Hit%', val:sc.hard_hit_pct+'%', w:Math.min(100,sc.hard_hit_pct)});
  if (sc.exit_velocity) scParts.push({label:'Exit Velo', val:sc.exit_velocity+' mph', w:Math.min(100,(sc.exit_velocity/98)*100)});
  if (sc.xslg) scParts.push({label:'xSLG', val:sc.xslg, w:Math.min(100,parseFloat(sc.xslg)*125)});
  if (sc.xwoba) scParts.push({label:'xwOBA', val:sc.xwoba, w:Math.min(100,parseFloat(sc.xwoba)*150)});
  if (scParts.length) {
    let scLabel = '📊 avg contact quality';
    if ((sc.barrel_pct||0) >= 10 || (sc.hard_hit_pct||0) >= 45) scLabel = '💥 elite contact quality';
    else if ((sc.barrel_pct||0) >= 6 || (sc.hard_hit_pct||0) >= 35) scLabel = '✅ above-avg contact';
    statcastHtml = `<div class="detail-section">
      <div class="detail-section-title">Statcast — ${scLabel}</div>
      ${scParts.map(p => `<div class="statcast-bar"><span class="bar-label">${p.label}</span><div class="bar-track"><div class="bar-fill" style="width:${p.w}%"></div></div><span class="bar-val">${p.val}</span></div>`).join('')}
    </div>`;
  }

  // Plate discipline
  let pdHtml = '';
  const pdParts = [];
  if (sc.chase_pct) pdParts.push(`Chase ${sc.chase_pct.toFixed(1)}%`);
  if (sc.zone_contact_pct) pdParts.push(`Z-Contact ${sc.zone_contact_pct.toFixed(1)}%`);
  if (sc.whiff_pct) pdParts.push(`Whiff ${sc.whiff_pct.toFixed(1)}%`);
  if (pdParts.length) {
    let pdNote = '';
    if ((sc.chase_pct||0) >= 32 && !isUnder) pdNote = '⚠️ high chase — breaking ball risk';
    else if ((sc.chase_pct||0) <= 22 && !isUnder) pdNote = '✅ disciplined eye';
    if ((sc.zone_contact_pct||0) >= 86) pdNote = '💥 elite bat-to-ball';
    else if ((sc.zone_contact_pct||0) <= 76) pdNote = '⚠️ weak in-zone contact';
    pdHtml = `<div style="font-size:0.85rem;color:var(--text-dim);margin-top:4px;">🎯 ${pdParts.join(' · ')}${pdNote ? ' — ' + pdNote : ''}</div>`;
  }

  // Power shape
  let powerHtml = '';
  if (powerShape.label) {
    powerHtml = `<div style="font-size:0.85rem;margin-top:4px;">${powerShape.label}${powerShape.barrel_pct ? ' — ' + powerShape.barrel_pct + '% barrel' : ''}${powerShape.hard_hit_pct ? ' · ' + powerShape.hard_hit_pct + '% hard-hit' : ''}</div>`;
  }

  // Weather
  let weatherHtml = '';
  if (data.weather_note || data.weather_boost !== 0 || weatherObj.speed_mph) {
    const parts = [];
    if (data.weather_note) parts.push(data.weather_note);
    if (weatherObj.dome) parts.push('Indoor — weather N/A');
    else {
      if (weatherObj.speed_mph >= 5) parts.push(`Wind: ${weatherObj.speed_mph} mph ${weatherObj.effect || ''}`);
      if (weatherObj.temp_f) parts.push(`Temp: ${weatherObj.temp_f}°F`);
    }
    if (data.weather_boost === 1) parts.push('🌬️ hitter-friendly wind');
    else if (data.weather_boost === -1) parts.push('🛑 pitcher-friendly wind');
    if (parts.length) weatherHtml = `<div style="font-size:0.85rem;">💨 ${parts.join(' · ')}</div>`;
  }

  // Park
  let parkHtml = '';
  const parkData = data.park;
  const parkFactor = parkData ? (parkData.run_factor || parkData.factor || parkData) : null;
  if (parkFactor && parkFactor !== 1.0) {
    const pf = parseFloat(parkFactor);
    let pfLabel = '', pfIcon = '';
    if (pf >= 1.05) { pfLabel = 'hitter-friendly park'; pfIcon = isUnder ? '🔴' : '🟢'; }
    else if (pf <= 0.95) { pfLabel = 'pitcher-friendly park'; pfIcon = isUnder ? '🟢' : '🔴'; }
    else { pfLabel = 'neutral park'; pfIcon = '🏟️'; }
    parkHtml = `<div style="font-size:0.85rem;">${pfIcon} ${pfLabel} (${pf.toFixed(2)}x)</div>`;
  }

  // Bullpen
  let bullpenHtml = '';
  if (bullpen.era) {
    const penIcon = bullpen.era >= 4.5 ? '🔥' : bullpen.era <= 3.0 ? '🛡️' : '⚪';
    let penLine = `${penIcon} Opp bullpen: ${bullpen.era} ERA · ${bullpen.whip || '?'} WHIP · ${bullpen.hr9 || '?'} HR/9`;
    if (bullpen.fatigued_count >= 2) penLine += ` · ⚠️ ${bullpen.fatigued_count} tired arms`;
    bullpenHtml = `<div style="font-size:0.85rem;">${penLine}</div>`;
  }

  // Compound spot
  let compoundHtml = '';
  if (data.compound_spot) {
    compoundHtml = `<div style="font-size:0.85rem;color:var(--orange);">⚠️ Compound spot — vulnerable starter + weak bullpen</div>`;
  }

  // Umpire
  let umpHtml = '';
  if (data.ump_name) {
    let umpTag = data.ump_name;
    if (data.ump_tier) umpTag += ` — ${data.ump_tier}`;
    umpHtml = `<div style="font-size:0.85rem;">⚖️ HP Ump: ${umpTag}</div>`;
  }

  // Projection (K props)
  let projHtml = '';
  if (data.proj_ks != null) {
    projHtml = `<div style="font-size:0.85rem;margin-top:4px;">📈 Model projection: ${data.proj_ks} Ks${oppKLine ? ' · ' + oppKLine : ''}</div>`;
  } else if (gapStr != null) {
    projHtml = `<div style="font-size:0.85rem;margin-top:4px;">📈 Projection edge: ${gapStr} vs line</div>`;
  }

  body.innerHTML = `
    <div class="detail-header">
      <div class="detail-header-left">
        ${headshotUrl ? `<img class="detail-headshot" src="${headshotUrl}" onerror="this.style.display='none'" />` : ''}
        <div>
          <h2 class="detail-title">${data.player}</h2>
          <div class="detail-sub">${data.prop_label || data.prop} — <span style="color:${sideColor};font-weight:700">${sideLabel}</span> ${data.line}</div>
        </div>
      </div>
      <div class="detail-score-ring">
        <span class="detail-score-num">${data.score}</span>
        <span class="detail-score-label">SCORE</span>
      </div>
    </div>

    <div class="detail-meta-row">
      <span class="tier-badge tier-${data.tier}">${data.tier}</span>
      <span class="detail-ev">+${data.ev}% EV</span>
      <span>${trendIcon} ${data.trend || 'N/A'}</span>
      ${data.sportsbook ? `<span class="detail-book">via ${data.sportsbook}</span>` : ''}
    </div>

    <div class="detail-section">
      <div class="detail-section-title">WHY IT HITS</div>
      ${l10.hits != null && l10.games ? `<div class="detail-case">${data.player} has gone ${sideLabel} ${data.line} in <b>${l10.hits}/${l10.games}</b> of his last 10 games (${effL10 != null ? effL10 : l10.hr}%)${l20.hits != null && l20.games ? `, and ${l20.hits}/${l20.games} over his last 20 (${effL20 != null ? effL20 : l20.hr}%)` : ''}.</div>` : ''}
      ${l5.hr != null ? `<div style="font-size:0.85rem;color:var(--text-dim);margin-top:4px;">L5: ${l5.hits}/${l5.games} (${effL5 != null ? effL5 : l5.hr}%) — ${trendIcon} ${data.trend || ''}</div>` : ''}
      ${streak ? `<div style="font-size:0.85rem;color:var(--green);margin-top:4px;">🔥 ${streak}</div>` : ''}
      ${trendLine ? `<div style="font-size:0.85rem;color:var(--text-dim);margin-top:4px;">📈 ${trendLine}</div>` : ''}
      ${powerHtml}
    </div>

    ${platoonLine ? `<div class="detail-section"><div class="detail-section-title">SPLIT FACTOR</div><div style="font-size:0.85rem;">${platoonLine}</div>${homeAway.home_avg != null && homeAway.away_avg != null ? `<div style="font-size:0.85rem;color:var(--text-dim);margin-top:4px;">Home avg ${homeAway.home_avg} vs Away avg ${homeAway.away_avg}</div>` : ''}</div>` : ''}

    <div class="detail-section">
      <div class="detail-section-title">MATCHUP${pitcherName !== 'TBD' ? ` — ${pitcherName}` : ''}</div>
      <div class="detail-split-row">
        <div class="split-box mini"><div class="split-label">ERA</div><div class="split-value">${pEra}</div></div>
        <div class="split-box mini"><div class="split-label">WHIP</div><div class="split-value">${pWhip}</div></div>
        <div class="split-box mini"><div class="split-label">K/9</div><div class="split-value">${pK9}</div></div>
        <div class="split-box mini"><div class="split-label">Hand</div><div class="split-value">${pHand}</div></div>
      </div>
      ${pHr9 !== '—' ? `<div style="font-size:0.85rem;color:var(--text-dim);margin-top:4px;">${pHr9} HR/9 · ${pFip} FIP</div>` : ''}
      ${bvpHtml}
      ${data.home_era != null && data.away_era != null ? `<div style="font-size:0.85rem;color:var(--text-dim);margin-top:4px;">Home ERA ${data.home_era} · Away ERA ${data.away_era}</div>` : ''}
      ${last5Html}
      ${oppKLine ? `<div style="font-size:0.85rem;margin-top:6px;">${isUnder && oppKpct >= 0.22 ? '⚠️' : '🟢'} Opponent ${oppKLine}</div>` : ''}
      ${lineupPos ? `<div style="font-size:0.85rem;color:var(--text-dim);margin-top:4px;">Batting #${lineupPos} — ~${paProj} projected PA</div>` : ''}
      ${spotIcon ? `<div style="font-size:0.85rem;color:var(--text-dim);margin-top:4px;">${data.is_home ? '🏠' : '✈️'} ${spotIcon}</div>` : ''}
    </div>

    <div class="detail-section">
      <div class="detail-section-title">VERDICT: ${sideLabel} ${data.line}</div>
      <div class="detail-verdict-stats">
        L10 hit rate: <b>${effL10 != null ? effL10 + '%' : '—'}</b> · 
        L10 avg: <b>${l10Avg != null ? l10Avg.toFixed(1) : '—'}</b> vs ${data.line} line 
        ${gapStr ? `(${gapStr})` : ''} · 
        Stability: <span style="color:${stabColor}">${stability}</span>
        ${dmgLabel ? ` · Damage: ${dmgIcon} ${dmgLabel}` : ''}
      </div>
      ${projHtml}
      <div class="detail-verdict-action">${actionText}</div>
    </div>

    <div class="detail-section">
      <div class="detail-section-title">PERFORMANCE</div>
      <div class="detail-split-row">
        <div class="split-box"><div class="split-label">L5</div><div class="split-value">${l5.hits != null ? l5.hits + '/' + l5.games : '—'}</div><div class="split-rate">${effL5 != null ? effL5 + '%' : '—'}</div></div>
        <div class="split-box"><div class="split-label">L10</div><div class="split-value">${l10.hits != null ? l10.hits + '/' + l10.games : '—'}</div><div class="split-rate">${effL10 != null ? effL10 + '%' : '—'}</div></div>
        <div class="split-box"><div class="split-label">L20</div><div class="split-value">${l20.hits != null ? l20.hits + '/' + l20.games : '—'}</div><div class="split-rate">${effL20 != null ? effL20 + '%' : '—'}</div></div>
        <div class="split-box"><div class="split-label">SEASON</div><div class="split-value">${data.season_avg || '—'}</div><div class="split-rate">${data.games_played || '—'} GP</div></div>
      </div>
    </div>

    ${lastValBar}

    ${homeAway.home_avg != null && homeAway.away_avg != null ? `
    <div class="detail-section">
      <div class="detail-section-title">HOME / AWAY</div>
      <div class="detail-split-row">
        <div class="split-box"><div class="split-label">Home</div><div class="split-value">${homeAway.home_avg}</div><div class="split-rate">${homeAway.home_games || 0} G</div></div>
        <div class="split-box"><div class="split-label">Away</div><div class="split-value">${homeAway.away_avg}</div><div class="split-rate">${homeAway.away_games || 0} G</div></div>
      </div>
    </div>` : ''}

    ${statcastHtml}

    <div class="detail-section">
      <div class="detail-section-title">ANALYSIS</div>
      <div class="detail-case">${formatMd(data.case || '')}</div>
      ${pdHtml}
    </div>

    <div class="detail-section">
      <div class="detail-section-title">RISK</div>
      <div class="detail-note">${formatMd(data.risk || '')}</div>
    </div>

    ${(weatherHtml || parkHtml || bullpenHtml || compoundHtml || umpHtml) ? `
    <div class="detail-section">
      <div class="detail-section-title">ENVIRONMENT</div>
      ${weatherHtml}
      ${parkHtml}
      ${bullpenHtml}
      ${compoundHtml}
      ${data.defense ? `<div style="font-size:0.85rem;">🛡️ ${data.defense}</div>` : ''}
      ${data.crush ? `<div style="font-size:0.85rem;color:var(--orange);">⚠️ ${data.crush}</div>` : ''}
      ${umpHtml}
    </div>` : ''}

    <div class="detail-scale">
      <div class="detail-scale-title">RATING SCALE</div>
      <div>💎 Elite (14+) · 🔥 Strong (8-13) · ✅ Good (4-7) · ➡️ Lean (1-3) · 🚫 Fade (&lt;1)</div>
      <div style="margin-top:4px;">Higher score = more data behind the pick.</div>
    </div>
  `;
}

function formatMd(text) {
  if (!text) return '';
  return text
    .replace(/\*\*(.*?)\*\*/g, '<b>$1</b>')
    .replace(/\n/g, '<br>');
}

document.getElementById('pick-modal-close').addEventListener('click', () => {
  document.getElementById('pick-modal').classList.remove('active');
});
document.getElementById('pick-modal').addEventListener('click', (e) => {
  if (e.target.id === 'pick-modal') e.target.classList.remove('active');
});

// ── Predict ─────────────────────────────────────────────────────────────────

/** Convert Discord markdown to safe HTML */
function discordMd(s) {
  if (!s) return '';
  return s
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
    .replace(/\*(.+?)\*/g, '<i>$1</i>')
    .replace(/`(.+?)`/g, '<code>$1</code>')
    .replace(/\n/g, '<br>');
}

async function runPrediction() {
  const player = document.getElementById('predict-player').value.trim();
  const stat = document.getElementById('predict-stat').value.trim();
  const line = parseFloat(document.getElementById('predict-line').value);
  const side = document.getElementById('predict-side').value;
  const result = document.getElementById('predict-result');
  const btn = document.getElementById('predict-btn');

  if (!player) { result.innerHTML = '<div class="error">Enter a player name</div>'; return; }
  if (!stat) { result.innerHTML = '<div class="error">Enter a stat (K, H, TB, etc.)</div>'; return; }
  if (!line || line <= 0) { result.innerHTML = '<div class="error">Enter a valid line</div>'; return; }

  btn.disabled = true;
  btn.textContent = 'Analyzing...';
  result.innerHTML = '<div class="loading">Running full analysis pipeline...</div>';

  const data = await api(`/api/predict?player=${encodeURIComponent(player)}&stat=${encodeURIComponent(stat)}&line=${line}&side=${side}`);

  btn.disabled = false;
  btn.textContent = 'Analyze';

  if (!data || data.error) {
    result.innerHTML = `<div class="error">${data?.error || 'Failed to run prediction'}</div>`;
    return;
  }

  const pid = data.player_id || '';
  const headshotUrl = pid
    ? `https://img.mlbstatic.com/mlb-photos/image/upload/d_people:generic:headshot:67:current.png/w_120,q_auto:best/v1/people/${pid}/headshot/67/current`
    : '';

  const sideLabel = data.side === 'under' ? 'UNDER' : 'OVER';
  const sideColor = data.side === 'under' ? 'var(--red)' : 'var(--green)';
  const fields = data.embed_fields || [];

  const sectionMap = {
    '— notice':     'NOTICE',
    '— why it hits':'WHY IT HITS',
    '— split factor':'SPLIT FACTOR',
    '— matchup dynamic':'MATCHUP',
    '— matchup':    'MATCHUP',
    '— recent games':'RECENT',
    '— risk flags': 'RISK',
    '— legend':     'LEGEND',
    '— verdict':    'VERDICT',
  };

  let sectionsHtml = fields.map(f => {
    const title = sectionMap[f.name.toLowerCase()] || f.name.replace(/^—\s*/, '').toUpperCase();
    return `<div class="detail-section">
      <div class="detail-section-title">${discordMd(title)}</div>
      <div style="font-size:0.88rem;line-height:1.6;">${discordMd(f.value)}</div>
    </div>`;
  }).join('');

  // Performance row (L5/L10/L20) from splits
  const splits = data.splits || {};
  const l5 = splits.l5 || {};
  const l10 = splits.l10 || {};
  const l20 = splits.l20 || {};
  const isUnder = data.side === 'under';
  const eff = (d) => {
    if (!d || !d.games) return '—';
    const r = d.rate || 0;
    return isUnder ? (100 - r).toFixed(0) + '%' : r.toFixed(0) + '%';
  };
  const perfHtml = `
    <div class="detail-section">
      <div class="detail-section-title">PERFORMANCE</div>
      <div class="detail-split-row">
        <div class="split-box"><div class="split-label">L5</div><div class="split-value">${l5.hits != null ? l5.hits+'/'+l5.games : '—'}</div><div class="split-rate">${eff(l5)}</div></div>
        <div class="split-box"><div class="split-label">L10</div><div class="split-value">${l10.hits != null ? l10.hits+'/'+l10.games : '—'}</div><div class="split-rate">${eff(l10)}</div></div>
        <div class="split-box"><div class="split-label">L20</div><div class="split-value">${l20.hits != null ? l20.hits+'/'+l20.games : '—'}</div><div class="split-rate">${eff(l20)}</div></div>
      </div>
    </div>`;

  // Last values bar
  const lastVals = splits.last_values || [];
  const lineVal = data.line;
  let lastValBar = '';
  if (lastVals.length) {
    lastValBar = `<div class="detail-section">
      <div class="detail-section-title">Last ${lastVals.length} Games</div>
      <div class="detail-bar-row">
        ${lastVals.map(v => {
          const hit = isUnder ? v < lineVal : v >= lineVal;
          const barH = Math.max(8, (v / Math.max(lineVal * 2, 3)) * 44);
          return `<div class="detail-bar-col">
            <span class="detail-bar-val" style="color:${hit ? 'var(--green)' : 'var(--text-dim)'}">${v}</span>
            <div class="detail-bar" style="height:${barH}px;background:${hit ? 'var(--green)' : 'rgba(255,255,255,0.1)'};opacity:${hit ? 1 : 0.3};"></div>
          </div>`;
        }).join('')}
      </div>
    </div>`;
  }

  // Statcast bars
  const sc = data.statcast || {};
  let statcastHtml = '';
  const scParts = [];
  if (sc.barrel_pct) scParts.push({label:'Barrel%', val:sc.barrel_pct+'%', w:Math.min(100,sc.barrel_pct*3)});
  if (sc.hard_hit_pct) scParts.push({label:'Hard Hit%', val:sc.hard_hit_pct+'%', w:Math.min(100,sc.hard_hit_pct)});
  if (sc.exit_velocity) scParts.push({label:'Exit Velo', val:sc.exit_velocity+' mph', w:Math.min(100,(sc.exit_velocity/98)*100)});
  if (scParts.length) {
    statcastHtml = `<div class="detail-section">
      <div class="detail-section-title">STATCAST</div>
      ${scParts.map(p => `<div class="statcast-bar"><span class="bar-label">${p.label}</span><div class="bar-track"><div class="bar-fill" style="width:${p.w}%"></div></div><span class="bar-val">${p.val}</span></div>`).join('')}
    </div>`;
  }

  // Environment (weather, park, bullpen, umpire)
  const weather = data.weather || {};
  const bullpen = data.bullpen || {};
  const umpire = data.umpire || {};
  const park = data.park_factor;
  const envParts = [];
  if (weather.speed_mph >= 5 || weather.temp_f || weather.dome) {
    if (weather.dome) envParts.push('Indoor');
    else {
      if (weather.speed_mph >= 5) envParts.push(`Wind: ${weather.speed_mph} mph ${weather.effect || ''}`);
      if (weather.temp_f) envParts.push(`Temp: ${weather.temp_f}°F`);
    }
  }
  if (bullpen.era) envParts.push(`Bullpen: ${bullpen.era} ERA · ${bullpen.whip || '?'} WHIP`);
  if (umpire.name) envParts.push(`Ump: ${umpire.name}`);
  let envHtml = '';
  if (envParts.length) {
    envHtml = `<div class="detail-section"><div class="detail-section-title">ENVIRONMENT</div>
      <div style="font-size:0.85rem;color:var(--text-dim);">${envParts.join(' · ')}</div></div>`;
  }

  result.innerHTML = `
    <div class="detail-section" style="margin-top:16px;">
      <div class="detail-header">
        <div class="detail-header-left">
          ${headshotUrl ? `<img class="detail-headshot" src="${headshotUrl}" onerror="this.style.display='none'" />` : ''}
          <div>
            <h2 class="detail-title">${data.player} <span style="color:var(--text-dim);font-size:0.8rem;">(${data.team})</span></h2>
            <div class="detail-sub">${data.prop} — <span style="color:${sideColor};font-weight:700">${sideLabel}</span> ${data.line}</div>
          </div>
        </div>
        <div class="detail-score-ring">
          <span class="detail-score-num">${data.score}</span>
          <span class="detail-score-label">SCORE</span>
        </div>
      </div>
    </div>
    <div class="detail-meta-row">
      <span class="tier-badge tier-${data.tier}">${data.tier}</span>
    </div>
    ${perfHtml}
    ${sectionsHtml}
    ${lastValBar}
    ${statcastHtml}
    ${envHtml}
    <div class="detail-scale">
      <div class="detail-scale-title">RATING SCALE</div>
      <div>💎 Elite (14+) · 🔥 Strong (8-13) · ✅ Good (4-7) · ➡️ Lean (1-3) · 🚫 Fade (&lt;1)</div>
    </div>
  `;
}

// ── Elite ────────────────────────────────────────────────────────────────────

async function loadElite() {
  const list = document.getElementById('elite-list');
  list.innerHTML = '<div class="loading">⭐ Loading elite plays...</div>';
  const data = await api('/api/elite');
  if (!data) return;
  const elite = data.elite || [];
  if (!elite.length) {
    list.innerHTML = '<div class="error">No elite plays right now</div>';
    return;
  }
  renderPicks(elite, list);
}

// ── NRFI ─────────────────────────────────────────────────────────────────────

async function loadNRFI() {
  const list = document.getElementById('nrfi-list');
  list.innerHTML = '<div class="loading">🌀 Analyzing games...</div>';
  const data = await api('/api/nrfi');
  if (!data) return;
  const plays = data.plays || [];
  if (!plays.length) {
    list.innerHTML = '<div class="error">No NRFI/YRFI plays right now</div>';
    return;
  }
  list.innerHTML = plays.map((p, i) => {
    const isNrfi = p.recommendation === 'NRFI';
    const score = isNrfi ? p.nrfi_score : p.yrfi_score;
    return `
      <div class="nrfi-card" style="animation-delay:${i * 80}ms">
        <div class="game-header">
          <span class="tier-badge ${isNrfi ? 'tier-STRONG' : 'tier-ELITE'}">${p.recommendation}</span>
          <span class="teams">${p.away_abbr} @ ${p.home_abbr}</span>
          <span class="nrfi-score">${score}</span>
          <span class="tier-badge ${p.confidence === 'STRONG' ? 'tier-ELITE' : 'tier-GOOD'}">${p.confidence}</span>
        </div>
        <div class="pitchers">${p.away_pitcher} → ${p.home_pitcher}</div>
        <div class="factors">
          ${(p.factors || []).map(f => `<span class="factor">${f}</span>`).join('')}
        </div>
      </div>
    `;
  }).join('');
}

// ── Player Research ──────────────────────────────────────────────────────────

document.getElementById('player-search-btn').addEventListener('click', doPlayerSearch);
document.getElementById('player-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') doPlayerSearch();
});

async function doPlayerSearch() {
  const name = document.getElementById('player-input').value.trim();
  if (!name) return;
  const container = document.getElementById('player-result');
  container.innerHTML = '<div class="loading">🔍 Searching...</div>';
  const data = await api(`/api/player?name=${encodeURIComponent(name)}`);
  if (!data || data.error) {
    container.innerHTML = `<div class="error">${data?.error || 'Player not found'}</div>`;
    return;
  }

  const isPitcher = data.is_pitcher;
  const splits = data.splits || {};
  const l5 = splits.l5 || {};
  const l10 = splits.l10 || {};
  const l20 = splits.l20 || {};
  const pitcher = data.pitcher_data || data.metrics || {};
  const bvp = data.bvp || {};
  const slash = data.slash_line || {};
  const statcast = data.statcast || {};
  const homeAway = data.home_away || {};
  const recentGames = data.recent_games || [];
  const seasonSum = data.season_summary || {};
  const gameInfo = data.game_info || {};
  const careerTeam = data.career_vs_team || {};
  const thumbUrl = data.thumbnail_url || '';
  const pid = data.player_id || '';
  const headshotUrl = pid
    ? `https://img.mlbstatic.com/mlb-photos/image/upload/d_people:generic:headshot:67:current.png/w_160,q_auto:best/v1/people/${pid}/headshot/67/current`
    : '';

  // Build headshot
  const headshotHtml = headshotUrl
    ? `<img class="research-headshot" src="${headshotUrl}" onerror="this.outerHTML='<div class=\\'no-headshot\\'>⚾</div>'" />`
    : '<div class="no-headshot">⚾</div>';

  // Slash line
  const slashHtml = (slash.avg || slash.ops) ? `
    <div class="research-section">
      <div class="research-label">Slash Line</div>
      <div class="slash-line">
        <div class="slash-item"><div class="slash-label">AVG</div><div class="slash-val">${slash.avg || '—'}</div></div>
        <div class="slash-item"><div class="slash-label">OBP</div><div class="slash-val">${slash.obp || '—'}</div></div>
        <div class="slash-item"><div class="slash-label">SLG</div><div class="slash-val">${slash.slg || '—'}</div></div>
        <div class="slash-item"><div class="slash-label">OPS</div><div class="slash-val">${slash.ops || '—'}</div></div>
        ${slash.sb ? `<div class="slash-item"><div class="slash-label">SB</div><div class="slash-val">${slash.sb}</div></div>` : ''}
      </div>
    </div>` : '';

  // Statcast
  const statcastHtml = (statcast.exit_velocity || statcast.barrel_pct || statcast.hard_hit_pct) ? `
    <div class="research-section">
      <div class="research-label">Statcast</div>
      ${statcast.exit_velocity ? `
      <div class="statcast-bar">
        <span class="bar-label">Exit Velo</span>
        <div class="bar-track"><div class="bar-fill" style="width:${Math.min(100, (statcast.exit_velocity / 98) * 100)}%"></div></div>
        <span class="bar-val">${statcast.exit_velocity} mph</span>
      </div>` : ''}
      ${statcast.barrel_pct ? `
      <div class="statcast-bar">
        <span class="bar-label">Barrel%</span>
        <div class="bar-track"><div class="bar-fill" style="width:${Math.min(100, statcast.barrel_pct * 3)}%"></div></div>
        <span class="bar-val">${statcast.barrel_pct}%</span>
      </div>` : ''}
      ${statcast.hard_hit_pct ? `
      <div class="statcast-bar">
        <span class="bar-label">Hard Hit%</span>
        <div class="bar-track"><div class="bar-fill" style="width:${Math.min(100, statcast.hard_hit_pct)}%"></div></div>
        <span class="bar-val">${statcast.hard_hit_pct}%</span>
      </div>` : ''}
      ${statcast.sweet_spot_pct ? `
      <div class="statcast-bar">
        <span class="bar-label">Sweet Spot%</span>
        <div class="bar-track"><div class="bar-fill" style="width:${Math.min(100, statcast.sweet_spot_pct * 2)}%"></div></div>
        <span class="bar-val">${statcast.sweet_spot_pct}%</span>
      </div>` : ''}
    </div>` : '';

  // Home/Away
  const homeAwayHtml = (homeAway.home_avg != null || homeAway.away_avg != null) ? `
    <div class="research-section">
      <div class="research-label">Home / Away</div>
      <div class="detail-split-row">
        <div class="split-box"><div class="split-label">Home</div><div class="split-value">${homeAway.home_avg ?? '—'}</div><div class="split-rate">${homeAway.home_games || 0} G</div></div>
        <div class="split-box"><div class="split-label">Away</div><div class="split-value">${homeAway.away_avg ?? '—'}</div><div class="split-rate">${homeAway.away_games || 0} G</div></div>
      </div>
    </div>` : '';

  // Game log table
  const gameLogHtml = recentGames.length ? `
    <div class="research-section">
      <div class="research-label">Recent Games</div>
      <div style="overflow-x:auto;">
        <table class="game-log-table">
          <thead><tr>
            <th>Date</th><th>Opp</th><th>H</th><th>R</th><th>RBI</th><th>HR</th><th>TB</th><th>BB</th><th>K</th>
          </tr></thead>
          <tbody>
            ${recentGames.map(g => {
              const isHit = g.h > 0;
              return `<tr>
                <td>${g.date || ''}</td>
                <td>${g.opp_abbr || ''}</td>
                <td class="${isHit ? 'stat-hit' : 'stat-miss'}">${g.h}</td>
                <td>${g.r}</td>
                <td>${g.rbi}</td>
                <td class="${g.hr > 0 ? 'stat-hot' : ''}">${g.hr}</td>
                <td>${g.tb}</td>
                <td>${g.bb}</td>
                <td>${g.k}</td>
              </tr>`;
            }).join('')}
          </tbody>
        </table>
      </div>
    </div>` : '';

  // Career vs opponent
  const careerHtml = careerTeam.ab ? `
    <div class="research-section">
      <div class="research-label">Career vs ${gameInfo.opponent || 'Opponent'}</div>
      <div class="detail-split-row">
        <div class="split-box"><div class="split-label">AVG</div><div class="split-value">${careerTeam.avg || '—'}</div></div>
        <div class="split-box"><div class="split-label">OPS</div><div class="split-value">${careerTeam.ops || '—'}</div></div>
        <div class="split-box"><div class="split-label">AB</div><div class="split-value">${careerTeam.ab}</div></div>
        <div class="split-box"><div class="split-label">HR</div><div class="split-value">${careerTeam.hr || 0}</div></div>
      </div>
    </div>` : '';

  // Pitcher-specific content
  if (isPitcher) {
    const metrics = data.metrics || {};
    const last5 = metrics.last_5_starts || [];
    const kHitRates = data.k_hit_rates || {};
    const oppK = data.opp_k_rate || {};
    const lineupBvp = data.lineup_bvp || [];
    const homeAwayEra = data.home_away_era || {};

    container.innerHTML = `
      <div class="research-card">
        <div class="research-hero">
          ${headshotHtml}
          <div>
            <div class="research-name">${data.name || name}</div>
            <div class="research-meta">${data.team || ''} · ${data.position || ''} · ${data.hand || ''}-Handed</div>
            <span class="research-tag pitcher">Pitcher</span>
            ${gameInfo.opponent ? `<div style="font-size:0.85rem;color:var(--text-dim);margin-top:4px;">vs ${gameInfo.opponent} ${gameInfo.is_home ? '(H)' : '(A)'}</div>` : ''}
          </div>
        </div>

        ${last5.length ? `
        <div class="research-section">
          <div class="research-label">Last 5 Starts — Strikeouts</div>
          <div style="display:flex;gap:6px;align-items:flex-end;height:40px;">
            ${last5.slice(0, 5).map((s, i) => {
              const k = s.k || 0;
              const barH = Math.max(8, (k / 12) * 36);
              return `<div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:2px;">
                <span style="font-size:0.7rem;font-weight:700;color:var(--green)">${k}</span>
                <div style="width:100%;height:${barH}px;background:linear-gradient(180deg, var(--accent), var(--green));border-radius:3px;"></div>
              </div>`;
            }).join('')}
          </div>
          <div style="display:flex;gap:6px;margin-top:4px;">
            ${last5.slice(0, 5).map((s, i) => `<div style="flex:1;text-align:center;font-size:0.6rem;color:var(--text-dim);">${s.date || 'G' + (i + 1)}</div>`).join('')}
          </div>
          <div style="margin-top:8px;font-size:0.85rem;color:var(--text-dim);">L5 K Avg: <b style="color:var(--text)">${data.l5_k_avg ?? '—'}</b> · K/Start: <b style="color:var(--text)">${data.k_per_gs ?? '—'}</b></div>
        </div>` : ''}

        ${lineupBvp.length ? `
        <div class="research-section">
          <div class="research-label">Tonight's Lineup — BvP K History</div>
          <div style="overflow-x:auto;">
            <table class="game-log-table">
              <thead><tr><th>Hitter</th><th>AB</th><th>K</th><th>K%</th><th>AVG</th></tr></thead>
              <tbody>
                ${lineupBvp.map(h => `<tr>
                  <td>${h.name}</td><td>${h.ab}</td><td>${h.k}</td>
                  <td class="${h.k_pct >= 30 ? 'stat-hot' : ''}">${h.k_pct}%</td>
                  <td>${h.avg}</td>
                </tr>`).join('')}
              </tbody>
            </table>
          </div>
        </div>` : ''}

        ${(homeAwayEra.home_era || homeAwayEra.away_era) ? `
        <div class="research-section">
          <div class="research-label">Home / Away ERA</div>
          <div class="detail-split-row">
            <div class="split-box"><div class="split-label">Home</div><div class="split-value">${homeAwayEra.home_era || '—'}</div><div class="split-rate">${homeAwayEra.home_ip || ''} IP</div></div>
            <div class="split-box"><div class="split-label">Away</div><div class="split-value">${homeAwayEra.away_era || '—'}</div><div class="split-rate">${homeAwayEra.away_ip || ''} IP</div></div>
          </div>
        </div>` : ''}

        ${data.career_vs_team ? `
        <div class="research-section">
          <div class="research-label">Career vs ${gameInfo.opponent || 'Opponent'}</div>
          <div class="detail-split-row">
            <div class="split-box"><div class="split-label">ERA</div><div class="split-value">${data.career_vs_team.era || '—'}</div></div>
            <div class="split-box"><div class="split-label">IP</div><div class="split-value">${data.career_vs_team.ip || '—'}</div></div>
            <div class="split-box"><div class="split-label">K</div><div class="split-value">${data.career_vs_team.k || 0}</div></div>
            <div class="split-box"><div class="split-label">AVG</div><div class="split-value">${data.career_vs_team.avg || '—'}</div></div>
          </div>
        </div>` : ''}

        ${Object.keys(kHitRates).length ? `
        <div class="research-section">
          <div class="research-label">K Hit Rates by Line</div>
          <div style="overflow-x:auto;">
            <table class="game-log-table">
              <thead><tr><th>Line</th><th>L5</th><th>L10</th><th>L20</th></tr></thead>
              <tbody>
                ${Object.entries(kHitRates).map(([line, rates]) => {
                  const r5 = rates.l5 || {};
                  const r10 = rates.l10 || {};
                  const r20 = rates.l20 || {};
                  return `<tr>
                    <td style="font-weight:700">${line}</td>
                    <td>${r5.hits != null ? r5.hits + '/' + r5.games : '—'}</td>
                    <td>${r10.hits != null ? r10.hits + '/' + r10.games : '—'}</td>
                    <td>${r20.hits != null ? r20.hits + '/' + r20.games : '—'}</td>
                  </tr>`;
                }).join('')}
              </tbody>
            </table>
          </div>
        </div>` : ''}
      </div>
    `;
    return;
  }

  // Batter research card
  container.innerHTML = `
    <div class="research-card">
      <div class="research-hero">
        ${headshotHtml}
        <div>
          <div class="research-name">${data.name || name}</div>
          <div class="research-meta">${data.team || ''} · ${data.position || ''} · ${data.bat_side || ''}s</div>
          <span class="research-tag batter">Batter</span>
          ${gameInfo.opponent ? `<div style="font-size:0.85rem;color:var(--text-dim);margin-top:4px;">vs ${gameInfo.opponent} ${gameInfo.is_home ? '(H)' : '(A)'}</div>` : ''}
        </div>
      </div>

      ${slashHtml}

      <div class="research-section">
        <div class="research-label">Recent Form</div>
        <div class="detail-split-row">
          <div class="split-box">
            <div class="split-label">L5</div>
            <div class="split-value">${l5.hits != null ? l5.hits + '/' + l5.games : '—'}</div>
            <div class="split-rate">${l5.hr != null ? l5.hr + '%' : '—'}</div>
          </div>
          <div class="split-box">
            <div class="split-label">L10</div>
            <div class="split-value">${l10.hits != null ? l10.hits + '/' + l10.games : '—'}</div>
            <div class="split-rate">${l10.hr != null ? l10.hr + '%' : '—'}</div>
          </div>
          <div class="split-box">
            <div class="split-label">L20</div>
            <div class="split-value">${l20.hits != null ? l20.hits + '/' + l20.games : '—'}</div>
            <div class="split-rate">${l20.hr != null ? l20.hr + '%' : '—'}</div>
          </div>
          <div class="split-box">
            <div class="split-label">AVG</div>
            <div class="split-value">${splits.season_avg || slash.avg || '—'}</div>
          </div>
          <div class="split-box">
            <div class="split-label">OPS</div>
            <div class="split-value">${slash.ops || '—'}</div>
          </div>
        </div>
      </div>

      ${splits.ops_vs_l || splits.ops_vs_r ? `
      <div class="research-section">
        <div class="research-label">Platoon Splits</div>
        <div class="detail-split-row">
          <div class="split-box"><div class="split-label">vs LHP</div><div class="split-value">${splits.ops_vs_l || '—'}</div></div>
          <div class="split-box"><div class="split-label">vs RHP</div><div class="split-value">${splits.ops_vs_r || '—'}</div></div>
        </div>
      </div>` : ''}

      ${homeAwayHtml}

      ${statcastHtml}

      ${gameLogHtml}

      ${careerHtml}

      ${bvp.ab > 0 ? `
      <div class="research-section">
        <div class="research-label">BvP History — ${pitcher.name || data.pitcher_name || 'Today\'s Pitcher'}</div>
        <div class="detail-split-row">
          <div class="split-box"><div class="split-label">AB</div><div class="split-value">${bvp.ab}</div></div>
          <div class="split-box"><div class="split-label">H</div><div class="split-value">${bvp.hits || 0}</div></div>
          <div class="split-box"><div class="split-label">AVG</div><div class="split-value">${bvp.avg || '—'}</div></div>
          <div class="split-box"><div class="split-label">HR</div><div class="split-value">${bvp.hr || 0}</div></div>
          <div class="split-box"><div class="split-label">K</div><div class="split-value">${bvp.k || 0}</div></div>
        </div>
      </div>` : ''}
    </div>
  `;
}

// ── Record ───────────────────────────────────────────────────────────────────

async function loadRecord() {
  const list = document.getElementById('record-list');
  list.innerHTML = '<div class="loading">📊 Loading record...</div>';
  const data = await api('/api/record');
  if (!data) return;
  const rows = data.rows || [];
  if (!rows.length) {
    list.innerHTML = '<div class="error">No predictions graded yet</div>';
    return;
  }
  list.innerHTML = `
    <table>
      <thead><tr><th>Tier</th><th>Result</th><th>Outcome</th><th>Count</th></tr></thead>
      <tbody>
        ${rows.map(r => `
          <tr>
            <td><span class="tier-badge tier-${r.tier}">${r.tier}</span></td>
            <td style="color:${r.result === 'win' ? 'var(--green)' : 'var(--red)'}">${r.result}</td>
            <td>${r.outcome || '—'}</td>
            <td>${r.total}</td>
          </tr>
        `).join('')}
      </tbody>
    </table>
  `;
}

// ── Parlay Builder ───────────────────────────────────────────────────────────

let parlayPicks = JSON.parse(localStorage.getItem('vortex_parlay') || '[]');

function saveParlay() {
  localStorage.setItem('vortex_parlay', JSON.stringify(parlayPicks));
  updateParlayCount();
}

function updateParlayCount() {
  const el = document.getElementById('parlay-count');
  if (parlayPicks.length > 0) {
    el.textContent = parlayPicks.length;
    el.classList.remove('hidden');
  } else {
    el.classList.add('hidden');
  }
}

function addToParlay(pick) {
  const key = `${pick.player}|${pick.prop}`;
  const exists = parlayPicks.find(p => `${p.player}|${p.prop}` === key);
  if (exists) {
    parlayPicks = parlayPicks.filter(p => `${p.player}|${p.prop}` !== key);
  } else {
    parlayPicks.push(pick);
  }
  saveParlay();
  renderParlayList();
}

function removeFromParlay(idx) {
  parlayPicks.splice(idx, 1);
  saveParlay();
  renderParlayList();
}

function clearParlay() {
  parlayPicks = [];
  saveParlay();
  renderParlayList();
}

function renderParlayList() {
  const list = document.getElementById('parlay-list');
  const empty = document.getElementById('parlay-empty');
  const summary = document.getElementById('parlay-summary');
  updateParlayCount();

  if (!parlayPicks.length) {
    list.innerHTML = '';
    empty.style.display = '';
    summary.classList.add('hidden');
    return;
  }
  empty.style.display = 'none';
  summary.classList.remove('hidden');

  const avgEv = parlayPicks.reduce((s, p) => s + (p.ev || 0), 0) / parlayPicks.length;
  const avgScore = parlayPicks.reduce((s, p) => s + (p.score || 0), 0) / parlayPicks.length;
  document.getElementById('parlay-leg-count').textContent = parlayPicks.length;
  document.getElementById('parlay-avg-ev').textContent = `+${avgEv.toFixed(1)}%`;
  document.getElementById('parlay-avg-score').textContent = avgScore.toFixed(1);

  list.innerHTML = parlayPicks.map((p, i) => {
    const side = (p.side || 'O').toUpperCase().charAt(0);
    const sideLabel = side === 'U' ? 'U' : 'O';
    const sideColor = side === 'U' ? 'var(--red)' : 'var(--green)';
    return `
    <div class="card pick-card parlay-card" onclick="openPickDetail('${p.player.replace(/'/g, "\\'")}','${p.prop.replace(/'/g, "\\'")}')" style="animation-delay:${i * 30}ms">
      <span class="tier-badge tier-${p.tier}">${p.tier}</span>
      <span class="player-name">${p.player}</span>
      <span class="prop-info">${p.prop.replace(/_/g, ' ')} <span style="color:${sideColor};font-weight:700">${sideLabel}</span>${p.line}</span>
      <span class="score">${p.score}</span>
      <span class="ev-badge">+${p.ev}%</span>
      <button class="parlay-remove-btn" onclick="event.stopPropagation();removeFromParlay(${i})" title="Remove">&times;</button>
    </div>`;
  }).join('');
}

const SPORTSBOOK_DEEP_LINKS = {
  draftkings: 'https://sportsbook.draftkings.com/',
  fanduel: 'https://sportsbook.fanduel.com/',
  betmgm: 'https://sports.betmgm.com/',
  caesars: 'https://sports.caesars.com/',
  underdog: 'https://underdogfantasy.com/',
  prizepicks: 'https://app.prizepicks.com/',
};

function exportParlay() {
  if (!parlayPicks.length) return;
  const book = document.getElementById('parlay-sportsbook').value;
  const bookName = book.charAt(0).toUpperCase() + book.slice(1);

  // Collect deep links from each pick's stats_json
  const deepLinks = parlayPicks
    .map(p => {
      const sj = p.stats_json || {};
      // Try preferred book first, then any available link
      const allLinks = sj.all_links || {};
      const side = (p.side || 'O').toLowerCase() === 'u' ? 'under' : 'over';
      const sideLinks = allLinks[side] || allLinks;
      return sideLinks[book] || sj.export_link || '';
    })
    .filter(Boolean);

  if (deepLinks.length > 0) {
    // Open deep links — each adds one leg to the sportsbook bet slip
    deepLinks.forEach((link, i) => {
      setTimeout(() => window.open(link, '_blank'), i * 400);
    });
    const msg = deepLinks.length === parlayPicks.length
      ? `Opened ${deepLinks.length} tabs — each leg is being added to ${bookName}. Build your parlay from the bet slip.`
      : `${deepLinks.length}/${parlayPicks.length} legs have deep links. Opened tabs for available legs — add the rest manually.`;
    showToast(msg, 'success');
  } else {
    // No deep links — fall back to text copy + homepage
    const lines = parlayPicks.map(p => {
      const side = (p.side || 'O').toUpperCase();
      return `${p.player} ${side} ${p.line} ${p.prop.replace(/_/g, ' ')}`;
    });
    const parlayText = lines.join('\n');
    navigator.clipboard.writeText(parlayText).then(() => {
      window.open(SPORTSBOOK_DEEP_LINKS[book] || SPORTSBOOK_DEEP_LINKS.draftkings, '_blank');
      showToast(`Copied ${parlayPicks.length} legs to clipboard — paste into ${bookName} bet slip`, 'success');
    }).catch(() => {
      window.open(SPORTSBOOK_DEEP_LINKS[book] || SPORTSBOOK_DEEP_LINKS.draftkings, '_blank');
    });
  }
}

function showToast(msg, type) {
  const existing = document.getElementById('vortex-toast');
  if (existing) existing.remove();
  const toast = document.createElement('div');
  toast.id = 'vortex-toast';
  toast.style.cssText = `
    position:fixed;bottom:24px;left:50%;transform:translateX(-50%);
    background:${type === 'success' ? '#16a34a' : '#dc2626'};color:#fff;
    padding:12px 24px;border-radius:8px;font-size:0.9rem;z-index:10000;
    box-shadow:0 4px 20px rgba(0,0,0,0.5);max-width:500px;text-align:center;
  `;
  toast.textContent = msg;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 6000);
}

// ── Load Parlay View ─────────────────────────────────────────────────────────

function loadParlay() {
  renderParlayList();
}

// ── Init ─────────────────────────────────────────────────────────────────────

(async () => {
  const authed = await checkAuth();
  if (authed) {
    loadPicks('all');
    updateParlayCount();
  }
})();
