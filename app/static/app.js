// Fire Forex single-page frontend — MT5-style optimisation workbench.
// Plain ES modules, no bundler, no framework.

const $ = (id) => document.getElementById(id);

// Server always generates the full superset schema (level 10). The "Complexity"
// slider is a client-side convenience that pre-fills group tickboxes + step
// multiplier. User tickboxes remain authoritative on top.
const FULL_SCHEMA_LEVEL = 10;

const state = {
  pairs: {},
  recipe: { pair: null, main_tf: null, sub_tf: null, name: null,
            start_date: null, end_date: null },
  presetLevel: 6,
  overrides: { groups: {}, knobs: {}, signal_families: {}, global: { step_multiplier: 1.0 } },
  featurePreset: 'balanced',
  bundle: null,
  explain: {},
  baseline: null,
  jobId: null,
  pollTimer: null,
  lastJob: null,
  inventory: null,
  dlJobId: null,
  dlTimer: null,
  tickDlJobId: null,
  tickDlTimer: null,
};

const ALL_OPTIONAL_GROUPS = ['trailing', 'breakeven', 'partial', 'stale', 'session', 'max_bars'];
const ALL_SIGNAL_FAMILIES = ['ema_cross', 'macd_cross', 'donchian'];

// Feature-set presets drive BOTH which optional groups are on AND which signal
// families are loaded. Dropping a signal family is the main way to cut runtime.
const FEATURE_PRESETS = {
  minimal:  { groups: [],                                  families: ['ema_cross'] },
  balanced: { groups: ['trailing', 'breakeven'],           families: ['ema_cross', 'macd_cross'] },
  all:      { groups: null,                                families: null },
};

function levelToStepMult(level) {
  // 1 → very coarse · 10 → very fine
  if (level <= 2) return 3.0;
  if (level <= 4) return 2.0;
  if (level <= 6) return 1.0;
  if (level <= 8) return 0.5;
  return 0.25;
}

function levelToGroupsOn(level) {
  if (level <= 2) return [];
  if (level === 3) return ['trailing'];
  if (level === 4) return ['trailing', 'breakeven'];
  if (level === 5) return ['trailing', 'breakeven', 'max_bars'];
  if (level === 6) return ['trailing', 'breakeven', 'max_bars', 'partial'];
  if (level === 7) return ['trailing', 'breakeven', 'max_bars', 'partial', 'stale'];
  if (level === 8) return ['trailing', 'breakeven', 'max_bars', 'partial', 'stale', 'session'];
  return ALL_OPTIONAL_GROUPS;
}

function levelToFamiliesOn(level) {
  if (level <= 2) return ['ema_cross'];
  if (level <= 6) return ['ema_cross', 'macd_cross'];
  return ALL_SIGNAL_FAMILIES;
}

// ── fetch helpers ──────────────────────────────────────────────────────

async function api(path, opts = {}) {
  const { timeoutMs, ...fetchOpts } = opts;
  let signal = fetchOpts.signal;
  let timer;
  if (timeoutMs && !signal) {
    const ctrl = new AbortController();
    signal = ctrl.signal;
    timer = setTimeout(() => ctrl.abort(), timeoutMs);
  }
  try {
    const r = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...fetchOpts,
      signal,
    });
    if (!r.ok) {
      let detail = r.statusText;
      try { detail = (await r.json()).detail || detail; } catch {}
      throw new Error(`${r.status} ${detail}`);
    }
    return r.json();
  } catch (e) {
    if (e.name === 'AbortError') {
      throw new Error(`timeout after ${Math.round((timeoutMs || 0) / 1000)}s`);
    }
    throw e;
  } finally {
    if (timer) clearTimeout(timer);
  }
}

const debounce = (fn, ms = 300) => {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
};

const escapeAttr = (s) => String(s).replace(/"/g, '&quot;');
const escapeHtml = (s) => String(s ?? '')
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

// ── tabs ───────────────────────────────────────────────────────────────

function switchTab(name) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('tab-active', b.dataset.tab === name));
  document.querySelectorAll('.pane').forEach(p => p.classList.toggle('hidden', p.dataset.pane !== name));
  if (name === 'history') refreshHistory();
  if (name === 'data') refreshInventory();
  if (name === 'results' && state.lastJob?.result?.equity_curve?.length) {
    // Canvas element measures incorrectly while hidden; redraw after the pane is visible.
    requestAnimationFrame(() => drawEquityCurve(state.lastJob.result.equity_curve));
  }
}

// ── tooltips ───────────────────────────────────────────────────────────

const tooltip = {
  el: null, title: null, range: null, why: null,
  init() {
    this.el = $('tooltip');
    this.title = $('tip-title');
    this.range = $('tip-range');
    this.why = $('tip-why');
    document.addEventListener('mouseover', (e) => {
      const t = e.target.closest('[data-help]');
      if (!t) return this.hide();
      const key = t.dataset.help;
      const info = state.explain[key];
      if (!info) return this.hide();
      this.title.textContent = info.title || key;
      this.range.textContent = info.range || '';
      this.why.textContent = info.why || '';
      this.el.classList.remove('hidden');
      this.place(e);
    });
    document.addEventListener('mousemove', (e) => {
      if (!this.el.classList.contains('hidden')) this.place(e);
    });
    document.addEventListener('mouseout', (e) => {
      if (!e.relatedTarget || !e.relatedTarget.closest || !e.relatedTarget.closest('[data-help]')) this.hide();
    });
  },
  place(e) {
    const x = Math.min(window.innerWidth - 340, e.clientX + 14);
    const y = Math.min(window.innerHeight - 140, e.clientY + 16);
    this.el.style.left = x + 'px';
    this.el.style.top = y + 'px';
  },
  hide() { this.el.classList.add('hidden'); },
};

// ── boot ───────────────────────────────────────────────────────────────

async function boot() {
  tooltip.init();

  let firstSeenBootId = null;
  try {
    const [pairsResp, { items }, { baseline }, versionResp] = await Promise.all([
      api('/api/pairs'),
      api('/api/explain-bundle'),
      api('/api/baseline'),
      api('/api/version'),
    ]);
    state.pairs = pairsResp.pairs;
    state.pairs_groups = pairsResp.groups || null;
    state.explain = items;
    state.baseline = baseline;
    const vp = document.getElementById('version-pill');
    if (vp && versionResp?.version) vp.textContent = versionResp.version;
    if (versionResp?.boot_id) firstSeenBootId = versionResp.boot_id;
  } catch (e) {
    setServerStatus(false, e.message);
    return;
  }

  // Server-restart auto-reload. The boot_id changes on every server start;
  // poll /api/version and reload once when it differs from the first-seen
  // value. Clears the interval on reload to guarantee one-shot behaviour.
  if (firstSeenBootId !== null) {
    const bootPoller = setInterval(async () => {
      try {
        const r = await api('/api/version');
        if (r?.boot_id && r.boot_id !== firstSeenBootId) {
          clearInterval(bootPoller);
          console.info('[boot] server restarted — reloading');
          location.reload();
        }
      } catch (_err) {
        // Transient network blip during restart — ignore and try again.
      }
    }, 5000);
  }
  updateBaselinePill();

  populatePairSelect();
  const defaultPair = state.pairs.EUR_USD ? 'EUR_USD' : Object.keys(state.pairs)[0];
  $('pair').value = defaultPair;
  state.recipe.pair = defaultPair;
  populateTfSelects();
  $('main_tf').value = 'H1';
  state.recipe.main_tf = 'H1';
  $('sub_tf').value = 'M1';
  state.recipe.sub_tf = 'M1';

  wireEvents();
  applyLevelPreset(state.presetLevel);   // kicks off the first refresh
}

function setServerStatus(ok, msg = '') {
  const pill = $('server-pill');
  if (ok) {
    pill.textContent = 'connected';
    pill.className = 'text-[11px] px-2 py-0.5 rounded-full bg-ok-500/10 text-ok-400 border border-ok-500/30';
  } else {
    pill.textContent = msg || 'offline';
    pill.title = msg;
    pill.className = 'text-[11px] px-2 py-0.5 rounded-full bg-bad-500/10 text-bad-400 border border-bad-500/30';
  }
}

function populatePairSelect() {
  const sel = $('pair');
  sel.innerHTML = '';
  for (const p of Object.keys(state.pairs)) {
    const opt = document.createElement('option');
    opt.value = p; opt.textContent = p.replace('_', '/');
    sel.appendChild(opt);
  }
}

// Mirror of ff/harness.py::TF_MINUTES — keep in sync.
const TF_MINUTES = {
  M1: 1, M5: 5, M15: 15, M30: 30,
  H1: 60, H4: 240, D: 1440, W: 10080,
};
const BACKTEST_TFS = Object.keys(TF_MINUTES);

function populateTfSelects() {
  const tfs = state.pairs[state.recipe.pair] || [];
  // main_tf: only TFs the engine knows, and strictly > M1
  // (M1 can't be a main because nothing finer exists as sub).
  const mainTfOpts = tfs.filter(t => BACKTEST_TFS.includes(t)
                                     && TF_MINUTES[t] > 1);
  fillSelect('main_tf', mainTfOpts, state.recipe.main_tf);
  // sub_tf: engine-known and strictly finer than the current main.
  const mainMin = TF_MINUTES[state.recipe.main_tf || 'H1'] || 60;
  const subTfOpts = tfs.filter(t => BACKTEST_TFS.includes(t)
                                    && TF_MINUTES[t] < mainMin);
  // If the currently-selected sub_tf just became invalid, snap to M1
  // (always the finest and always valid when a main > M1 exists).
  let subPick = state.recipe.sub_tf;
  if (!subTfOpts.includes(subPick)) subPick = 'M1';
  fillSelect('sub_tf', subTfOpts, subPick);
  state.recipe.sub_tf = subPick;
}

function fillSelect(id, values, selected) {
  const sel = $(id);
  sel.innerHTML = '';
  for (const v of values) {
    const o = document.createElement('option');
    o.value = v; o.textContent = v;
    if (v === selected) o.selected = true;
    sel.appendChild(o);
  }
}

// ── wiring ─────────────────────────────────────────────────────────────

function wireEvents() {
  document.getElementById('tabs').addEventListener('click', (e) => {
    const b = e.target.closest('[data-tab]'); if (!b) return;
    switchTab(b.dataset.tab);
  });

  $('pair').addEventListener('change', () => {
    state.recipe.pair = $('pair').value;
    populateTfSelects();
    state.recipe.main_tf = $('main_tf').value;
    state.recipe.sub_tf = $('sub_tf').value;
    clearDateRange();          // prior window no longer valid for the new pair
    refreshDefaults();
  });
  $('main_tf').addEventListener('change', () => {
    state.recipe.main_tf = $('main_tf').value;
    populateTfSelects();       // sub_tf list depends on main_tf
    clearDateRange();          // bounds change with TF
    refreshDefaults();
  });
  $('sub_tf').addEventListener('change', () => {
    state.recipe.sub_tf = $('sub_tf').value;
    refreshDefaults();
  });

  $('start_date').addEventListener('change', () => {
    state.recipe.start_date = $('start_date').value || null;
    updateRangeInfo();
  });
  $('end_date').addEventListener('change', () => {
    state.recipe.end_date = $('end_date').value || null;
    updateRangeInfo();
  });

  // 8 preset buttons: 1M / 3M / 6M / YTD / 1Y / 2Y / 5Y / Full
  const rangeRow = document.getElementById('range-presets');
  if (rangeRow) {
    rangeRow.addEventListener('click', (e) => {
      const b = e.target.closest('[data-range]');
      if (!b) return;
      applyRangePreset(b.dataset.range);
    });
  }

  // Persist Market section open/closed across reload (same storage key pattern
  // used by the dynamic signal sections in renderFeatureSection).
  const marketDetails = document.getElementById('market-section');
  if (marketDetails) {
    const storageKey = 'ff.section.market.open';
    const saved = localStorage.getItem(storageKey);
    if (saved !== null) marketDetails.open = saved === '1';
    marketDetails.addEventListener('toggle', () => {
      localStorage.setItem(storageKey, marketDetails.open ? '1' : '0');
    });
  }

  // ── Data tab ──
  $('inventory-rescan').addEventListener('click', () => refreshInventory(true));
  $('inventory-body').addEventListener('click', (e) => {
    const tr = e.target.closest('tr[data-pair]'); if (!tr) return;
    const btn = e.target.closest('[data-action]');
    if (btn) {
      e.stopPropagation();
      runInventoryAction(btn.dataset.action, tr.dataset.pair);
      return;
    }
    runHealthCheck(tr.dataset.pair, tr.dataset.tf);
  });
  $('download-form').addEventListener('submit', (e) => { e.preventDefault(); submitDownload(); });
  $('dl-cancel').addEventListener('click', cancelDownload);
  $('tick-download-form').addEventListener('submit', (e) => { e.preventDefault(); submitTickDownload(); });
  $('tdl-cancel').addEventListener('click', cancelTickDownload);

  // Auto-fill Start = last-held bar / End = today on pair switch (with force
  // so we overwrite the previous pair's dates).
  $('dl-pair').addEventListener('change', () => autoFillDownloadDates('bars', { force: true }));
  $('tdl-pair').addEventListener('change', () => autoFillDownloadDates('tick', { force: true }));

  // 8-button preset rows on the Bars and Tick download cards (mirror the
  // Parameters Market section presets).
  const dlPresets = $('dl-presets');
  if (dlPresets) {
    dlPresets.addEventListener('click', (e) => {
      const b = e.target.closest('[data-range]'); if (!b) return;
      applyDownloadRangePreset('bars', b.dataset.range);
    });
  }
  const tdlPresets = $('tdl-presets');
  if (tdlPresets) {
    tdlPresets.addEventListener('click', (e) => {
      const b = e.target.closest('[data-range]'); if (!b) return;
      applyDownloadRangePreset('tick', b.dataset.range);
    });
  }

  $('level').addEventListener('input', () => {
    state.presetLevel = parseInt($('level').value, 10);
    $('level-value').textContent = String(state.presetLevel);
    applyLevelPreset(state.presetLevel);
  });

  document.getElementById('step-granularity').addEventListener('click', (e) => {
    const b = e.target.closest('[data-mult]'); if (!b) return;
    document.querySelectorAll('#step-granularity .seg-btn').forEach(x => x.classList.remove('seg-active'));
    b.classList.add('seg-active');
    // Clear per-knob step overrides so the new multiplier is visible
    for (const p of Object.keys(state.overrides.knobs || {})) {
      if (state.overrides.knobs[p]) delete state.overrides.knobs[p].step;
      if (state.overrides.knobs[p] && Object.keys(state.overrides.knobs[p]).length === 0)
        delete state.overrides.knobs[p];
    }
    state.overrides.global.step_multiplier = parseFloat(b.dataset.mult);
    refreshDefaults();
  });

  document.getElementById('feature-preset').addEventListener('click', (e) => {
    const b = e.target.closest('[data-preset]'); if (!b) return;
    applyFeaturePreset(b.dataset.preset);
  });

  $('reset-overrides').addEventListener('click', () => {
    state.overrides = { groups: {}, knobs: {}, global: { step_multiplier: 1.0 } };
    applyLevelPreset(state.presetLevel);
  });

  $('run-btn').addEventListener('click', onRunClick);
  $('pin-baseline-btn').addEventListener('click', onPinBaseline);
  $('clear-baseline-btn').addEventListener('click', onClearBaseline);
  $('scatter-metric').addEventListener('change', onScatterMetricChange);
  $('scatter-canvas').addEventListener('click', onScatterCanvasClick);
  $('scatter-reset').addEventListener('click', onScatterReset);
  $('scatter-jump-best').addEventListener('click', onScatterJumpBest);
  // Redraw the scatter when the canvas becomes visible (e.g. after the
  // user switches to the Results tab) or the window resizes. Without this,
  // drawing during a hidden pane produces a zero-size canvas with no dots.
  if (typeof ResizeObserver !== 'undefined') {
    const ro = new ResizeObserver(() => drawScatter());
    ro.observe($('scatter-canvas'));
  }
  window.addEventListener('resize', () => drawScatter());
}

const debouncedRefresh = debounce(refreshDefaults, 300);

function applyFeaturePreset(name) {
  state.featurePreset = name;
  document.querySelectorAll('#feature-preset .seg-btn').forEach(b => b.classList.toggle('seg-active', b.dataset.preset === name));
  const preset = FEATURE_PRESETS[name];
  state.overrides.groups = {};
  if (preset.groups !== null) {
    for (const g of ALL_OPTIONAL_GROUPS) state.overrides.groups[g] = preset.groups.includes(g);
  } else {
    for (const g of ALL_OPTIONAL_GROUPS) state.overrides.groups[g] = true;
  }
  state.overrides.signal_families = {};
  if (preset.families !== null) {
    for (const f of ALL_SIGNAL_FAMILIES) state.overrides.signal_families[f] = preset.families.includes(f);
  } else {
    for (const f of ALL_SIGNAL_FAMILIES) state.overrides.signal_families[f] = true;
  }
  refreshDefaults();
}

function applyLevelPreset(level) {
  // Changing the preset wipes per-knob min/step/max overrides so the new
  // step sizes are actually visible in the table.
  state.overrides.knobs = {};
  state.overrides.global.step_multiplier = levelToStepMult(level);
  document.querySelectorAll('#step-granularity .seg-btn').forEach(x => {
    x.classList.toggle('seg-active', Math.abs(parseFloat(x.dataset.mult) - state.overrides.global.step_multiplier) < 1e-6);
  });
  const onG = new Set(levelToGroupsOn(level));
  state.overrides.groups = {};
  for (const g of ALL_OPTIONAL_GROUPS) state.overrides.groups[g] = onG.has(g);
  const onF = new Set(levelToFamiliesOn(level));
  state.overrides.signal_families = {};
  for (const f of ALL_SIGNAL_FAMILIES) state.overrides.signal_families[f] = onF.has(f);
  const matchName = level <= 2 ? 'minimal' : level <= 6 ? 'balanced' : 'all';
  state.featurePreset = matchName;
  document.querySelectorAll('#feature-preset .seg-btn').forEach(b => b.classList.toggle('seg-active', b.dataset.preset === matchName));
  refreshDefaults();
}

// ── defaults refresh ───────────────────────────────────────────────────

async function refreshDefaults() {
  const { pair, main_tf, sub_tf } = state.recipe;
  if (!pair || !main_tf) return;
  try {
    const bundle = await api('/api/defaults', {
      method: 'POST',
      body: JSON.stringify({ pair, main_tf, sub_tf, level: FULL_SCHEMA_LEVEL, overrides: state.overrides }),
    });
    state.bundle = bundle;
    renderPreview(bundle.preflight);
    renderFeatures(bundle.flat_schema);
    renderSignals(bundle.flat_schema);
    setServerStatus(true);
  } catch (e) {
    setServerStatus(false, e.message);
  }
}

// ── preview tiles ──────────────────────────────────────────────────────

function fmtNum(n, decimals = 1) {
  if (n === null || n === undefined) return '—';
  const a = Math.abs(n);
  if (a >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (a >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  if (Number.isInteger(n)) return String(n);
  return n.toFixed(decimals);
}

function fmtDuration(s) {
  if (s == null || !isFinite(s)) return '—';
  if (s < 60) return s.toFixed(1) + 's';
  const m = Math.floor(s / 60), r = Math.round(s % 60);
  return `${m}m ${r}s`;
}

function renderPreview(pf) {
  const lib = pf?.signals?.library_size ?? null;
  $('prev-lib').textContent = lib != null ? lib.toLocaleString() : '—';
  $('prev-lib-sub').textContent = lib != null ? 'signal variants to test' : 'variants';

  const eng = pf?.engine ?? {};
  const dMin = eng.eff_dims_min, dMean = eng.eff_dims_mean, dMax = eng.eff_dims_max;
  if (dMean != null) {
    $('prev-dims').textContent = `${Math.round(dMean)} knobs`;
    $('prev-dims-sub').textContent = (dMin === dMax)
      ? 'every trial tunes this many'
      : `${dMin}–${dMax} depending on which on/off features fire each trial`;
  } else {
    $('prev-dims').textContent = '—';
    $('prev-dims-sub').textContent = '';
  }

  const low = pf?.estimates?.total_s_low ?? null;
  const high = pf?.estimates?.total_s_high ?? null;
  if (low != null && high != null) $('prev-runtime').textContent = fmtDuration((low + high) / 2);
  else $('prev-runtime').textContent = '—';
}

// ── features / knobs rendering ─────────────────────────────────────────

const GROUP_FRIENDLY_NAMES = {
  stop_loss:    'Stop loss',
  take_profit:  'Take profit',
  trailing:     'Trailing stop',
  chandelier:   'Chandelier stop (peak-anchored ATR trail)',
  breakeven:    'Breakeven (move stop to entry when in profit)',
  partial:      'Partial close (take some off the table)',
  stale:        'Stale exit (kill stuck trades)',
  session:      'Trading hours filter',
  days:         'Days of the week to trade',
  max_bars:     'Hard cap on how long a trade can live',
};

const GROUP_SUBHEAD = {
  stop_loss:   'Pick a fixed pip distance OR an ATR multiple — the optimiser tries both.',
  take_profit: 'Pick a risk:reward ratio, an ATR multiple, or fixed pips — all are tried.',
  trailing:    'Moves the stop along behind a winning trade.',
  chandelier:  'Anchors the stop to the highest-high (or lowest-low) since entry, minus an ATR multiple. Ratchets one-way — never loosens.',
  breakeven:   'Once the trade is healthy in profit, the stop jumps to near the entry price.',
  partial:     'Closes part of the position at an interim target to lock some pips in.',
  stale:       'Exits trades that are going nowhere in flat, low-volatility markets.',
  session:     'Only trade within a chosen hour window (UTC).',
  max_bars:    'A safety net — every trade closes if it lives longer than this.',
  days:        'Which days of the week the strategy is allowed to trade.',
};

// Collapsible section groupings — each section is a <details> in the UI.
// Any group not listed here falls into an "Other" section at the end.
const FEATURE_SECTIONS = [
  { key: 'targets',    title: 'Targets (stop / take-profit)',    groups: ['stop_loss', 'take_profit'] },
  { key: 'stop_mgmt',  title: 'Stop management',                  groups: ['trailing', 'chandelier', 'breakeven'] },
  { key: 'lifecycle',  title: 'Trade lifecycle',                  groups: ['partial', 'stale', 'max_bars'] },
  { key: 'filters',    title: 'Time & session filters',           groups: ['session', 'days'] },
];

const KNOB_LABELS = {
  // Stop loss
  'stop_loss.fixed.pips':                             'Fixed stop distance (pips)',
  'stop_loss.atr.mult':                               'ATR-scaled stop (× ATR)',
  // Take profit
  'take_profit.rr.ratio':                             'Risk : reward ratio',
  'take_profit.atr.mult':                             'ATR-scaled target (× ATR)',
  'take_profit.fixed.pips':                           'Fixed target distance (pips)',
  // Trailing
  'trailing.when_on.mode.fixed.distance':             'Trailing distance (pips)',
  'trailing.when_on.mode.atr.mult':                   'Trailing distance (× ATR)',
  'trailing.when_on.activate':                        'Start trailing after this much profit (pips)',
  // Breakeven
  'breakeven.when_on.trigger':                        'Move stop to breakeven after (pips profit)',
  'breakeven.when_on.offset':                         'Breakeven offset from entry (pips)',
  // Partial
  'partial.when_on.pct':                              'How much of the position to close (%)',
  'partial.when_on.trigger':                          'Trigger partial close at (pips profit)',
  // Stale
  'stale.when_on.bars':                               'Max bars before stall exit',
  'stale.when_on.atr_thresh':                         'Volatility threshold (× ATR)',
  // Session
  'session.when_on.hours_start':                      'Trading start hour (UTC 0–23)',
  'session.when_on.hours_end':                        'Trading end hour (UTC 0–23)',
  // Max bars
  'max_bars.when_on.bars':                            'Hard cap on bars per trade',
  // Chandelier
  'chandelier.when_on.activate':                      'Arm chandelier after this much profit (pips)',
  'chandelier.when_on.atr_mult':                      'Chandelier distance from peak (× ATR)',
  // Signals
  'ema_cross.fast':                                   'Fast EMA period',
  'ema_cross.slow':                                   'Slow EMA period',
  'macd_cross.fast':                                  'MACD fast EMA',
  'macd_cross.slow':                                  'MACD slow EMA',
  'macd_cross.signal':                                'MACD signal line',
  'donchian.lookback':                                'Donchian lookback bars',
};

const FAMILY_LABELS = {
  ema_cross:  { title: 'EMA crossover',    sub: 'Buy when a fast EMA crosses above a slow EMA. Sell when it crosses below.' },
  macd_cross: { title: 'MACD crossover',   sub: 'A smoother version of EMA cross using the MACD signal line.' },
  donchian:   { title: 'Donchian breakout', sub: 'Buy when price breaks above the N-bar high. Sell when below the N-bar low.' },
};

const DAYS_LABELS = { 31: 'Mon–Fri', 63: 'Mon–Sat', 127: 'All week (Mon–Sun)' };

function segAtPath(path, flatList) {
  return flatList.find(e => e.path === path);
}

function renderFeatures(flat) {
  const container = $('features');
  container.innerHTML = '';
  const engine = flat.engine || [];
  // Group flat entries by top-level segment.
  const groups = new Map();
  for (const e of engine) {
    const top = e.path.split('.')[0];
    if (!groups.has(top)) groups.set(top, []);
    groups.get(top).push(e);
  }

  const rendered = new Set();
  for (const section of FEATURE_SECTIONS) {
    const cards = [];
    for (const name of section.groups) {
      if (!groups.has(name)) continue;
      rendered.add(name);
      cards.push(renderFeatureCard(name, groups.get(name)));
    }
    if (cards.length === 0) continue;
    container.appendChild(renderFeatureSection(section.key, section.title, cards));
  }

  // Anything the sections don't claim goes into a catch-all "Other"
  // section at the bottom, so new knobs added to the engine never
  // disappear from the UI.
  const orphans = [];
  for (const [name, entries] of groups) {
    if (rendered.has(name)) continue;
    orphans.push(renderFeatureCard(name, entries));
  }
  if (orphans.length) {
    container.appendChild(renderFeatureSection('other', 'Other', orphans));
  }
}

function renderFeatureSection(key, title, cards) {
  const wrap = document.createElement('details');
  wrap.className = 'feature-section';
  // Persist open/closed state per-section across page reloads.
  const storageKey = `ff.section.${key}.open`;
  const saved = localStorage.getItem(storageKey);
  wrap.open = saved === null ? true : saved === '1';
  wrap.addEventListener('toggle', () => {
    localStorage.setItem(storageKey, wrap.open ? '1' : '0');
  });

  const summary = document.createElement('summary');
  summary.className = 'feature-section__summary';
  summary.innerHTML = `
    <span class="feature-section__chevron">▸</span>
    <span class="feature-section__title">${title}</span>
    <span class="feature-section__count">${cards.length} feature${cards.length === 1 ? '' : 's'}</span>
  `;
  wrap.appendChild(summary);

  const body = document.createElement('div');
  body.className = 'feature-section__body';
  for (const card of cards) body.appendChild(card);
  wrap.appendChild(body);

  return wrap;
}

function renderFeatureCard(name, entries) {
  const root = document.createElement('div');
  root.className = 'feature-row';

  const header = document.createElement('div');
  header.className = 'flex items-center gap-3 px-4 py-2.5';

  const top = entries.find(e => e.path === name);
  const headerLabel = GROUP_FRIENDLY_NAMES[name] || name;
  const sub = GROUP_SUBHEAD[name] || '';
  let toggle = '<span class="w-5"></span>';
  let status = '';

  if (top && top.kind === 'group') {
    const enabled = top.enabled !== false;
    toggle = `<input type="checkbox" class="group-toggle" data-path="${escapeAttr(name)}" ${enabled ? 'checked' : ''} />`;
    status = enabled
      ? '<span class="text-ok-400 text-[10px] uppercase tracking-wider">ON</span>'
      : '<span class="text-slate-500 text-[10px] uppercase tracking-wider">OFF</span>';
  }

  header.innerHTML = `
    ${toggle}
    <div class="flex-1">
      <div class="flex items-center gap-2">
        <div class="font-semibold text-slate-200" data-help="${escapeAttr(name)}">${headerLabel} <span class="help-dot">?</span></div>
        ${status}
      </div>
      ${sub ? `<div class="text-[11px] text-slate-500 mt-0.5">${sub}</div>` : ''}
    </div>
  `;
  root.appendChild(header);

  const body = document.createElement('div');
  body.className = 'px-4 pb-3';

  if (name === 'days' && top && top.kind === 'choice') {
    body.appendChild(renderDaysRow(top));
  } else {
    const leaves = entries.filter(e => e !== top && (e.kind === 'float' || e.kind === 'int'));
    if (!leaves.length) {
      body.innerHTML = '<div class="text-[11px] text-slate-500 italic">(nothing to tune here)</div>';
    } else {
      const tbl = document.createElement('div');
      tbl.className = 'knob-table';
      tbl.innerHTML = `
        <div class="knob-head">
          <div></div>
          <div>Parameter</div>
          <div class="text-right" data-help="min">Min <span class="help-dot">?</span></div>
          <div class="text-right" data-help="step">Step <span class="help-dot">?</span></div>
          <div class="text-right" data-help="max">Max <span class="help-dot">?</span></div>
        </div>
      `;
      for (const leaf of leaves) tbl.appendChild(renderKnobRow(leaf));
      body.appendChild(tbl);
    }
  }
  root.appendChild(body);

  if (top && top.kind === 'group' && top.enabled === false) {
    body.classList.add('opacity-40', 'pointer-events-none');
  }
  return root;
}

function renderSignals(flat) {
  const c = $('signals');
  c.innerHTML = '';
  const signals = flat.signals || [];
  // Always show every registered family so the user can tick one back on
  // even if the current overrides have dropped it.
  const present = new Map();
  for (const e of signals) {
    const family = e.path.split('.')[0];
    if (!present.has(family)) present.set(family, []);
    present.get(family).push(e);
  }
  const familyRows = [];
  for (const family of ALL_SIGNAL_FAMILIES) {
    const enabled = state.overrides.signal_families?.[family] !== false;
    const entries = present.get(family) || [];
    const body = entries.filter(e => e.kind === 'int' || e.kind === 'float');
    const meta = FAMILY_LABELS[family] || { title: family, sub: '' };

    const row = document.createElement('div');
    row.className = 'feature-row';
    const header = document.createElement('div');
    header.className = 'flex items-center gap-3 px-4 py-2.5';
    header.innerHTML = `
      <input type="checkbox" class="family-toggle" data-family="${escapeAttr(family)}" ${enabled ? 'checked' : ''} />
      <div class="flex-1">
        <div class="flex items-center gap-2">
          <div class="font-semibold text-slate-200" data-help="signals.${family}">${meta.title} <span class="help-dot">?</span></div>
          <span class="${enabled ? 'text-ok-400' : 'text-slate-500'} text-[10px] uppercase tracking-wider">${enabled ? 'ON' : 'OFF'}</span>
        </div>
        <div class="text-[11px] text-slate-500 mt-0.5">${meta.sub}</div>
      </div>
    `;
    row.appendChild(header);

    const wrap = document.createElement('div');
    wrap.className = 'px-4 pb-3';
    if (!enabled) {
      wrap.innerHTML = '<div class="text-[11px] text-slate-500 italic">Tick the box above to include this signal family.</div>';
    } else if (!body.length) {
      wrap.innerHTML = '<div class="text-[11px] text-slate-500 italic">(no tunable knobs)</div>';
    } else {
      const tbl = document.createElement('div');
      tbl.className = 'knob-table';
      tbl.innerHTML = `
        <div class="knob-head">
          <div></div>
          <div>Parameter</div>
          <div class="text-right" data-help="min">Min <span class="help-dot">?</span></div>
          <div class="text-right" data-help="step">Step <span class="help-dot">?</span></div>
          <div class="text-right" data-help="max">Max <span class="help-dot">?</span></div>
        </div>
      `;
      body.map(renderKnobRow).forEach(tr => tbl.appendChild(tr));
      wrap.appendChild(tbl);
    }
    row.appendChild(wrap);
    familyRows.push(row);
  }
  if (familyRows.length) {
    c.appendChild(renderFeatureSection('signals', 'Signal families', familyRows));
  }
}

function renderKnobRow(leaf) {
  const row = document.createElement('div');
  row.className = 'knob-row';
  const minOv = state.overrides.knobs?.[leaf.path] || {};
  const enabled = minOv.enabled !== false;
  const stepStr = leaf.step == null ? '' : String(leaf.step);
  const label = KNOB_LABELS[leaf.path] || prettifyPath(leaf.path);
  row.innerHTML = `
    <label class="flex items-center" title="Include this parameter in the optimisation"><input type="checkbox" class="knob-enabled" data-path="${escapeAttr(leaf.path)}" ${enabled ? 'checked' : ''} /></label>
    <div class="text-slate-200">${label}</div>
    <input type="number" class="knob-input knob-min" data-field="min" data-path="${escapeAttr(leaf.path)}" value="${leaf.min}" step="any" />
    <input type="number" class="knob-input knob-step" data-field="step" data-path="${escapeAttr(leaf.path)}" value="${stepStr}" min="0" step="any" placeholder="auto" />
    <input type="number" class="knob-input knob-max" data-field="max" data-path="${escapeAttr(leaf.path)}" value="${leaf.max}" step="any" />
  `;
  return row;
}

function renderDaysRow(leaf) {
  const wrap = document.createElement('div');
  wrap.className = 'text-xs text-slate-300 py-1';
  const parts = (leaf.values || []).map(v => DAYS_LABELS[v] || String(v));
  wrap.innerHTML = `<span class="text-slate-400">Will try:</span> ${parts.join(' · ')}`;
  return wrap;
}

function prettifyPath(path) {
  // Fallback for unrecognised paths — drop internal plumbing, title-case.
  return path.split('.').filter(p => p !== 'when_on').map(p =>
    p.replace(/_/g, ' ')
  ).join(' · ');
}

// Delegated listeners for knob/group changes
document.addEventListener('change', (e) => {
  const t = e.target;
  if (t.classList?.contains('group-toggle')) {
    const p = t.dataset.path;
    state.overrides.groups[p] = t.checked;
    debouncedRefresh();
  } else if (t.classList?.contains('family-toggle')) {
    const f = t.dataset.family;
    state.overrides.signal_families[f] = t.checked;
    debouncedRefresh();
  } else if (t.classList?.contains('knob-enabled')) {
    const p = t.dataset.path;
    state.overrides.knobs[p] = state.overrides.knobs[p] || {};
    state.overrides.knobs[p].enabled = t.checked;
    debouncedRefresh();
  } else if (t.classList?.contains('knob-input')) {
    const p = t.dataset.path;
    const field = t.dataset.field;
    state.overrides.knobs[p] = state.overrides.knobs[p] || {};
    if (t.value === '') {
      state.overrides.knobs[p][field] = null;
    } else {
      let v = parseFloat(t.value);
      if (!Number.isFinite(v)) v = 0;
      if (field === 'step' && v < 0) { v = 0; t.value = '0'; }
      state.overrides.knobs[p][field] = v;
    }
    debouncedRefresh();
  }
});

// ── run flow ───────────────────────────────────────────────────────────

async function onRunClick() {
  if (!state.recipe.pair) return;
  const btn = $('run-btn');
  btn.disabled = true; btn.textContent = 'Starting…';
  $('progress-wrap').classList.remove('hidden');
  setProgress(0, 'queued');
  try {
    const body = {
      recipe: { ...state.recipe, level: FULL_SCHEMA_LEVEL },
      overrides: state.overrides,
      n_trials: parseInt($('n_trials').value, 10),
      seed: parseInt($('seed').value, 10),
      layer_name: $('layer_name').value || null,
      start_date: state.recipe.start_date || null,
      end_date: state.recipe.end_date || null,
    };
    const { job_id } = await api('/api/run', { method: 'POST', body: JSON.stringify(body) });
    state.jobId = job_id;
    pollJob();
  } catch (e) {
    setProgress(0, 'error: ' + e.message);
    btn.disabled = false; btn.textContent = 'Run backtest';
  }
}

function setProgress(frac, msg, elapsedS) {
  $('progress-bar').style.width = (Math.max(0, Math.min(1, frac)) * 100).toFixed(0) + '%';
  const pctTxt = (Math.max(0, Math.min(1, frac)) * 100).toFixed(0) + '%';
  const elapsedTxt = elapsedS != null ? ` · ${fmtDuration(elapsedS)}` : '';
  $('progress-pct').textContent = pctTxt + elapsedTxt;
  $('progress-msg').textContent = msg || '';
}

async function pollJob() {
  if (!state.jobId) return;
  clearTimeout(state.pollTimer);
  try {
    const j = await api(`/api/jobs/${state.jobId}`);
    const elapsed = j.started_at ? ((j.finished_at || Date.now() / 1000) - j.started_at) : null;
    setProgress(j.progress || 0, j.message || j.status, elapsed);
    if (j.status === 'running') {
      state.pollTimer = setTimeout(pollJob, 500);
    } else if (j.status === 'done') {
      renderResults(j);
      finishRun();
      refreshHistory();
      switchTab('results');
    } else if (j.status === 'error') {
      setProgress(0, 'error: ' + (j.error || '').split('\n')[0], elapsed);
      finishRun();
    }
  } catch (e) {
    setProgress(0, 'poll error: ' + e.message);
    finishRun();
  }
}

function finishRun() {
  const btn = $('run-btn');
  btn.disabled = false; btn.textContent = 'Run backtest';
}

// ── results ────────────────────────────────────────────────────────────

// Plain-text signed pips for the KPI cards (the History tab uses
// signedPipsCell for in-table HTML with red/green tint; the cards keep
// the same format but without colour, to stay consistent with the rest
// of the summary tiles).
function fmtSignedPips(v) {
  if (v == null || Number.isNaN(Number(v))) return '—';
  const n = Number(v);
  if (n === 0) return '0';
  const sign = n > 0 ? '+' : '';
  return `${sign}${n.toFixed(1)}`;
}

const KPI_DEFS = [
  { key: 'trades',            label: 'Trades',        fmt: (v) => fmtNum(v, 0) },
  { key: 'win_rate_pct',      label: 'Win rate',      fmt: (v) => v == null ? '—' : `${Number(v).toFixed(1)}%` },
  { key: 'total_pips',        label: 'Total pips',    fmt: (v) => fmtNum(v, 1) },
  { key: 'expectancy_pips',   label: 'Expectancy',    fmt: (v) => v == null ? '—' : `${Number(v).toFixed(2)} pips` },
  { key: 'max_dd_pct',        label: 'Max DD',        fmt: (v) => v == null ? '—' : `${Number(v).toFixed(1)}%` },
  { key: 'profit_factor',     label: 'Profit factor', fmt: (v) => v == null ? '—' : Number(v).toFixed(2) },
  { key: 'sharpe',            label: 'Sharpe',        fmt: (v) => v == null ? '—' : Number(v).toFixed(2) },
  { key: 'return_pct',        label: 'Return %',      fmt: (v) => v == null ? '—' : `${Number(v).toFixed(1)}%` },
  // Cost-realism cards — same fields as the History tab columns, surfaced
  // on the Last-run summary panel so users do not have to scroll to see
  // adjusted P&L vs raw on the run they just executed.
  { key: 'adjusted_total_pips', label: 'Adj. pips', fmt: (v) => fmtNum(v, 1) },
  { key: 'gate_save_pips',      label: 'Gate save', fmt: (v) => fmtSignedPips(v) },
  { key: 'cost_overhead_pips',  label: 'Cost',      fmt: (v) => fmtSignedPips(v) },
  { key: 'n_gated_trades',      label: 'Gated',     fmt: (v) => fmtNum(v, 0) },
];

function renderResults(job) {
  state.lastJob = job;
  $('no-results').classList.add('hidden');
  $('results-body').classList.remove('hidden');

  const r = job.result || {};
  const runtime = (job.finished_at && job.started_at) ? (job.finished_at - job.started_at).toFixed(1) : '?';
  $('results-sub').textContent = `Layer: ${r.layer ?? '?'} · ${runtime}s · ${job.recipe?.pair}/${job.recipe?.main_tf}/${job.recipe?.sub_tf}`;

  const kpiEl = $('kpis');
  kpiEl.innerHTML = '';
  const kpis = r.kpis || {};
  const delta = r.baseline_delta || null;
  for (const def of KPI_DEFS) {
    const v = kpis[def.key];
    const d = delta ? delta[def.key] : null;
    const tile = document.createElement('div');
    tile.className = 'kpi-tile';
    tile.setAttribute('data-help', def.key);
    let deltaHtml = '';
    if (d && d.delta != null) {
      const good = isBetter(def.key, d.delta);
      const cls = good ? 'text-ok-400' : (d.delta === 0 ? 'text-slate-500' : 'text-bad-400');
      const sign = d.delta > 0 ? '+' : '';
      deltaHtml = `<div class="kpi-delta ${cls}">${sign}${Number(d.delta).toFixed(2)} vs baseline</div>`;
    }
    tile.innerHTML = `
      <div class="kpi-label">${def.label} <span class="help-dot">?</span></div>
      <div class="kpi-value">${def.fmt(v)}</div>
      ${deltaHtml}
    `;
    kpiEl.appendChild(tile);
  }

  drawEquityCurve(r.equity_curve || []);
  $('equity-sub').textContent = "Cumulative pips across the best variant's trades.";
  const bp = (r.best_params_english || []).join('\n');
  $('best-params').textContent = bp || '(no details)';

  state.viewingTrial = false;
  $('scatter-reset').disabled = true;
  loadScatterForRun(runFileBasename(r.run_file));
}

function runFileBasename(p) {
  if (!p) return null;
  return String(p).split(/[\\/]/).pop();
}

function isBetter(key, delta) {
  // More is better, except drawdown — less is better.
  const inverted = new Set(['max_dd_pct']);
  return inverted.has(key) ? delta < 0 : delta > 0;
}

function drawEquityCurve(series) {
  const canvas = $('equity-canvas');
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const W = canvas.width = Math.floor(rect.width * dpr);
  const H = canvas.height = Math.floor(rect.height * dpr);
  ctx.clearRect(0, 0, W, H);

  if (!series || series.length < 2) {
    ctx.fillStyle = '#64748b'; ctx.font = `${12 * dpr}px sans-serif`;
    ctx.fillText('no equity data', 12 * dpr, 20 * dpr);
    return;
  }

  const pad = { l: 40 * dpr, r: 12 * dpr, t: 12 * dpr, b: 18 * dpr };
  const plotW = W - pad.l - pad.r;
  const plotH = H - pad.t - pad.b;
  const lo = Math.min(...series), hi = Math.max(...series);
  const rng = Math.max(1e-9, hi - lo);
  const xs = (i) => pad.l + (i / (series.length - 1)) * plotW;
  const ys = (v) => pad.t + (1 - (v - lo) / rng) * plotH;

  ctx.strokeStyle = '#1a2030'; ctx.lineWidth = 1;
  for (let g = 0; g <= 4; g++) {
    const y = pad.t + (g / 4) * plotH;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
  }
  if (lo < 0 && hi > 0) {
    ctx.strokeStyle = '#2a3548'; ctx.setLineDash([4 * dpr, 4 * dpr]);
    ctx.beginPath(); ctx.moveTo(pad.l, ys(0)); ctx.lineTo(W - pad.r, ys(0)); ctx.stroke();
    ctx.setLineDash([]);
  }
  ctx.strokeStyle = '#ff7a18'; ctx.lineWidth = 1.6 * dpr;
  ctx.beginPath();
  for (let i = 0; i < series.length; i++) {
    const x = xs(i), y = ys(series[i]);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.stroke();
  ctx.fillStyle = '#64748b'; ctx.font = `${10 * dpr}px sans-serif`;
  ctx.fillText(`${lo.toFixed(1)}`, 2 * dpr, H - 4 * dpr);
  ctx.fillText(`${hi.toFixed(1)}`, 2 * dpr, pad.t + 10 * dpr);
}

// ── scatter ────────────────────────────────────────────────────────────
// Per-trial scatter of the current run. X=trial index, Y=user-selected
// metric column. Colour = diverging gradient centred on pinned baseline's
// best quality score (green above, red below); falls back to a neutral
// palette if no baseline is pinned. Click a dot → fetch that trial's
// equity curve + metrics and replace the equity chart + KPI tiles above.
// Fallback labels for legacy API responses missing metric_labels.
const SCATTER_METRIC_LABELS_FALLBACK = {
  trades: 'Trades', win_rate: 'Win rate', profit_factor: 'Profit factor',
  sharpe: 'Sharpe', sortino: 'Sortino', max_dd_pct: 'Max DD %',
  return_pct: 'Return %', r_squared: 'R²', ulcer: 'Ulcer',
  quality: 'Quality v1 (legacy)', total_pips: 'Total pips',
};

function labelFor(key, ss) {
  if (ss && ss.data && ss.data.metric_labels && ss.data.metric_columns) {
    const i = ss.data.metric_columns.indexOf(key);
    if (i >= 0) return ss.data.metric_labels[i];
  }
  return SCATTER_METRIC_LABELS_FALLBACK[key] || key;
}

// Metrics where LOWER is better — argmin instead of argmax on Jump-to-best.
const SCATTER_METRIC_LOWER_IS_BETTER = new Set([
  'max_dd_pct', 'ulcer', 'max_consec_loss',
]);

const SCATTER_METRIC_TOOLTIPS = {
  quality: 'Composite: ln(1+Sortino) · clamp(K-Ratio/3,0,1) · min(PF,5) · trades_f / (Ulcer+5). Codex-reviewed formula shipped 2026-04-19 — see docs/metrics.md.',
  quality_v2: 'Alias of Quality — kept for NPZ schema stability. Hidden from the UI dropdown.',
  dsr: 'Deflated Sharpe (Lopez de Prado). Corrects PSR for best-of-N selection bias. Recommended default objective for random sweeps.',
  psr: 'Probabilistic Sharpe — prob(true Sharpe > 0) given observed N, skew, kurtosis.',
  sqn: 'System Quality Number (Van Tharp): √N · mean(R) / std(R). Catches low-N lucky trials.',
  k_ratio: 'Kestner 2013: slope / (stderr·√n) of equity regression. Significance of the up-slope — complementary to R² linearity.',
  calmar: 'Annualised PnL / |Max DD|. Catches single catastrophic drawdowns.',
  recovery: 'Total PnL / |Max DD|. Non-annualised Calmar.',
  upi: 'Return % / Ulcer Index. Rewards smooth recoveries.',
  tail_ratio: '|P95| / |P5| of per-trade pnl. Catches negative skew.',
  omega: 'Σmax(r,0) / Σmax(-r,0). At τ=0 this equals Profit Factor; τ configurability pending.',
  expectancy_r: 'Mean per-trade R-multiple. Unit-free across pairs.',
  expectancy_pips: 'Mean per-trade pnl in pips.',
  max_consec_loss: 'Longest losing streak — psychological tradability.',
  avg_hold_bars: 'Placeholder — per-trial bar-duration tracking not wired yet.',
  trades_per_day: 'Placeholder — per-trial bar-duration tracking not wired yet.',
};

function populateScatterDropdown(ss) {
  const sel = $('scatter-metric');
  if (!sel || !ss.data || !ss.data.metric_columns) return;
  const prevValue = sel.value || 'total_pips';
  const keys = ss.data.metric_columns;
  const labels = ss.data.metric_labels || keys;
  const groups = ss.data.metric_groups || keys.map(() => 'Metrics');
  const byGroup = new Map();
  const groupOrder = ['Return', 'Risk-Adjusted', 'Risk', 'Overfit-Aware', 'Composite', 'Activity', 'Forex'];
  for (let i = 0; i < keys.length; i++) {
    const g = groups[i];
    if (g === '_hidden') continue;  // alias/deprecated slots — keep in API, hide from UI.
    if (!byGroup.has(g)) byGroup.set(g, []);
    byGroup.get(g).push({ key: keys[i], label: labels[i] });
  }
  sel.innerHTML = '';
  const seen = new Set();
  const orderedGroups = [
    ...groupOrder.filter(g => byGroup.has(g)),
    ...[...byGroup.keys()].filter(g => !groupOrder.includes(g)),
  ];
  for (const g of orderedGroups) {
    if (seen.has(g)) continue;
    seen.add(g);
    const og = document.createElement('optgroup');
    og.label = g;
    for (const { key, label } of byGroup.get(g)) {
      const opt = document.createElement('option');
      opt.value = key;
      opt.textContent = label;
      if (SCATTER_METRIC_TOOLTIPS[key]) opt.title = SCATTER_METRIC_TOOLTIPS[key];
      og.appendChild(opt);
    }
    sel.appendChild(og);
  }
  sel.value = keys.includes(prevValue) ? prevValue : (keys.includes('total_pips') ? 'total_pips' : keys[0]);
}

function scatterState() {
  if (!state.scatter) state.scatter = { runFile: null, data: null, points: [] };
  return state.scatter;
}

async function loadScatterForRun(runFile) {
  const ss = scatterState();
  ss.runFile = runFile;
  ss.data = null; ss.points = [];
  $('scatter-jump-best').disabled = true;
  if (!runFile) { drawScatter(); return; }
  try {
    const res = await fetch(`/api/runs/${encodeURIComponent(runFile)}/scatter`);
    if (!res.ok) { drawScatter(); return; }
    ss.data = await res.json();
  } catch {
    ss.data = null;
  }
  if (ss.data) populateScatterDropdown(ss);
  $('scatter-jump-best').disabled = !(ss.data && ss.data.metrics.length);
  drawScatter();
}

function onScatterMetricChange() { drawScatter(); }

function currentScatterMetricIdx() {
  const sel = $('scatter-metric').value;
  const ss = scatterState();
  if (!ss.data) return -1;
  return ss.data.metric_columns.indexOf(sel);
}

function fallbackMetricIdx(ss) {
  // Prefer quality, then profit_factor, then first column.
  const prefer = ['quality', 'profit_factor', 'sharpe', 'trades'];
  for (const name of prefer) {
    const i = ss.data.metric_columns.indexOf(name);
    if (i >= 0) return { idx: i, name };
  }
  return { idx: 0, name: ss.data.metric_columns[0] };
}

function drawScatter() {
  const canvas = $('scatter-canvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  // Canvas may still be display:none / zero-size if we render before the
  // tab is made visible. Defer via rAF; the resize observer will also
  // catch the eventual reveal.
  if (rect.width < 10 || rect.height < 10) {
    if (!scatterState()._pending) {
      scatterState()._pending = true;
      requestAnimationFrame(() => { scatterState()._pending = false; drawScatter(); });
    }
    return;
  }
  const W = canvas.width = Math.floor(rect.width * dpr);
  const H = canvas.height = Math.floor(rect.height * dpr);
  ctx.clearRect(0, 0, W, H);

  const ss = scatterState();
  const legend = $('scatter-legend');
  if (!ss.data || !ss.data.metrics.length) {
    ctx.fillStyle = '#64748b'; ctx.font = `${12 * dpr}px sans-serif`;
    ctx.fillText('run a backtest to see per-trial scatter', 12 * dpr, 20 * dpr);
    legend.textContent = '';
    ss.points = [];
    return;
  }

  let mi = currentScatterMetricIdx();
  let fallbackMsg = '';
  if (mi < 0) {
    const fb = fallbackMetricIdx(ss);
    mi = fb.idx;
    fallbackMsg = ` · "${$('scatter-metric').value}" not in this run, showing ${fb.name} (restart the server if you just updated)`;
  }
  const rows = ss.data.metrics;
  const indices = ss.data.indices;
  const sel = $('scatter-metric').value;
  const isQuality = sel === 'quality';
  const baselineQ = ss.data.baseline_quality;

  const ysRaw = rows.map(r => r[mi]);
  const ysValid = ysRaw.filter(v => v != null && Number.isFinite(v));
  if (!ysValid.length) {
    ctx.fillStyle = '#64748b'; ctx.font = `${12 * dpr}px sans-serif`;
    ctx.fillText(`"${labelFor($('scatter-metric').value, ss)}" had no numeric value for any trial`, 12 * dpr, 20 * dpr);
    legend.textContent = '';
    ss.points = [];
    return;
  }
  let lo = Math.min(...ysValid), hi = Math.max(...ysValid);
  const degenerate = (lo === hi);
  if (degenerate) {
    // All trials produced the same value for this metric (common when a
    // losing sweep collapses every quality score to 0). Pad the axis so
    // the dots sit on a visible line instead of a zero-height strip.
    const pad = Math.abs(lo) > 1 ? Math.abs(lo) * 0.1 : 0.5;
    lo -= pad; hi += pad;
  }
  const pad = { l: 44 * dpr, r: 12 * dpr, t: 12 * dpr, b: 22 * dpr };
  const plotW = W - pad.l - pad.r;
  const plotH = H - pad.t - pad.b;
  const nTotal = ss.data.n_trials;
  const xPos = (trialIdx) => pad.l + (trialIdx / Math.max(1, nTotal - 1)) * plotW;
  const yPos = (v) => pad.t + (1 - (v - lo) / (hi - lo)) * plotH;

  // Gridlines.
  ctx.strokeStyle = '#1a2030'; ctx.lineWidth = 1;
  for (let g = 0; g <= 4; g++) {
    const y = pad.t + (g / 4) * plotH;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
  }

  // Baseline reference line when plotting quality and a baseline is pinned.
  if (isQuality && baselineQ != null && baselineQ >= lo && baselineQ <= hi) {
    ctx.strokeStyle = '#64748b'; ctx.setLineDash([4 * dpr, 4 * dpr]);
    ctx.beginPath(); ctx.moveTo(pad.l, yPos(baselineQ)); ctx.lineTo(W - pad.r, yPos(baselineQ)); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = '#94a3b8'; ctx.font = `${10 * dpr}px sans-serif`;
    ctx.fillText(`baseline ${baselineQ.toFixed(3)}`, W - pad.r - 90 * dpr, yPos(baselineQ) - 4 * dpr);
  }

  // Dots. Colour encodes quality vs baseline (diverging) regardless of the
  // Y metric — keeps visual meaning consistent across dropdown changes.
  const qCol = ss.data.metric_columns.indexOf('quality');
  const qualities = rows.map(r => r[qCol]);
  const qLo = Math.min(...qualities), qHi = Math.max(...qualities);
  const pivot = (baselineQ != null) ? baselineQ : (qLo + qHi) / 2;
  const r = 3.2 * dpr;
  const points = [];
  for (let i = 0; i < rows.length; i++) {
    const v = rows[i][mi];
    if (v == null || !Number.isFinite(v)) continue;
    const trialIdx = indices[i];
    const x = xPos(trialIdx);
    const y = yPos(v);
    ctx.fillStyle = scatterColor(qualities[i], pivot, qLo, qHi);
    ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill();
    points.push({ x, y, trialIdx });
  }
  ss.points = points;

  // Axis labels.
  ctx.fillStyle = '#64748b'; ctx.font = `${10 * dpr}px sans-serif`;
  ctx.fillText(hi.toFixed(3), 2 * dpr, pad.t + 10 * dpr);
  ctx.fillText(lo.toFixed(3), 2 * dpr, H - pad.b + 12 * dpr);
  ctx.fillText('trial #', pad.l, H - 4 * dpr);
  ctx.fillText(labelFor(sel, ss), 2 * dpr, 10 * dpr);

  // Legend text.
  const decim = ss.data.n_points < ss.data.n_trials
    ? ` · showing ${ss.data.n_points} of ${ss.data.n_trials} trials (strided)`
    : ` · ${ss.data.n_trials} trials`;
  const shownMetric = ss.data.metric_columns[mi];
  const degenMsg = degenerate ? ` · all trials share the same ${shownMetric} value (${ys[0].toFixed(3)})` : '';
  legend.textContent = (baselineQ != null)
    ? `Colour = quality vs baseline (${baselineQ.toFixed(3)}): red below, green above${decim}${fallbackMsg}${degenMsg}`
    : `Colour = quality (no baseline pinned)${decim}${fallbackMsg}${degenMsg}`;
}

function scatterColor(q, pivot, qLo, qHi) {
  // Diverging red → neutral → green, normalised so the furthest side from
  // pivot hits full saturation.
  const down = Math.max(1e-9, pivot - qLo);
  const up = Math.max(1e-9, qHi - pivot);
  let t;
  if (q >= pivot) {
    t = Math.min(1, (q - pivot) / up);
    return `rgb(${Math.round(120 - 80 * t)},${Math.round(160 + 60 * t)},${Math.round(120 - 80 * t)})`;
  }
  t = Math.min(1, (pivot - q) / down);
  return `rgb(${Math.round(160 + 60 * t)},${Math.round(120 - 80 * t)},${Math.round(120 - 80 * t)})`;
}

function onScatterCanvasClick(e) {
  const ss = scatterState();
  if (!ss.points.length) return;
  const canvas = $('scatter-canvas');
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const cx = (e.clientX - rect.left) * dpr;
  const cy = (e.clientY - rect.top) * dpr;
  let best = null, bestD2 = Infinity;
  for (const p of ss.points) {
    const dx = p.x - cx, dy = p.y - cy;
    const d2 = dx * dx + dy * dy;
    if (d2 < bestD2) { bestD2 = d2; best = p; }
  }
  const hitRadius = 12 * dpr;
  if (best && bestD2 <= hitRadius * hitRadius) {
    showTrial(best.trialIdx);
  }
}

async function showTrial(trialIdx) {
  const ss = scatterState();
  if (!ss.runFile) return;
  try {
    const res = await fetch(`/api/runs/${encodeURIComponent(ss.runFile)}/trial/${trialIdx}`);
    if (!res.ok) throw new Error(await res.text());
    const d = await res.json();
    renderTrialView(d);
  } catch (err) {
    console.error('trial fetch failed', err);
  }
}

function renderTrialView(trial) {
  state.viewingTrial = true;
  $('scatter-reset').disabled = false;
  const equity = trial.equity || [];
  drawEquityCurve(equity);
  $('equity-sub').textContent = `Trial #${trial.trial_idx} · ${trial.n_trades} trades`;

  // Swap KPI tiles with per-trial values. Derive win_rate_pct, total_pips,
  // expectancy from the trial metrics row + equity curve (Rust metrics
  // expose win_rate as a fraction; total pips is the final equity value).
  const m = trial.metrics || {};
  const totalPips = equity.length ? equity[equity.length - 1] : 0;
  const trades = m.trades || 0;
  const kpis = {
    trades: trades,
    win_rate_pct: m.win_rate != null ? (m.win_rate <= 1 ? m.win_rate * 100 : m.win_rate) : null,
    total_pips: totalPips,
    expectancy_pips: trades ? totalPips / trades : 0,
    max_dd_pct: m.max_dd_pct,
    profit_factor: m.profit_factor,
    sharpe: m.sharpe,
    return_pct: m.return_pct,
  };
  const kpiEl = $('kpis');
  kpiEl.innerHTML = '';
  for (const def of KPI_DEFS) {
    const tile = document.createElement('div');
    tile.className = 'kpi-tile';
    tile.setAttribute('data-help', def.key);
    tile.innerHTML = `
      <div class="kpi-label">${def.label} <span class="help-dot">?</span></div>
      <div class="kpi-value">${def.fmt(kpis[def.key])}</div>
      <div class="kpi-delta text-slate-500">trial #${trial.trial_idx}</div>
    `;
    kpiEl.appendChild(tile);
  }
}

function onScatterReset() {
  if (state.lastJob) renderResults(state.lastJob);
}

function onScatterJumpBest() {
  // Jump to the best trial by the currently-selected Y-axis metric,
  // with two safety rules so "best" always means a *winning* trial:
  //   1. Profitability gate: skip trials with total_pips <= 0 (or when
  //      total_pips is unavailable, fall back to profit_factor > 1).
  //      Metrics like R², K-Ratio, Tail Ratio can be maximised by trials
  //      that are mathematically "clean" but lose money.
  //   2. Direction: metrics in SCATTER_METRIC_LOWER_IS_BETTER (max_dd_pct,
  //      ulcer, max_consec_loss) are argmin, not argmax.
  // If no profitable trial exists, fall back to unconstrained best and
  // flag the caveat in the legend.
  const ss = scatterState();
  if (!ss.data) return;
  const sel = $('scatter-metric').value;
  const objCol = ss.data.metric_columns.indexOf(sel);
  if (objCol < 0) return;
  const rCol = ss.data.metric_columns.indexOf('return_pct');
  const qCol = ss.data.metric_columns.indexOf('quality');
  const pipsCol = ss.data.metric_columns.indexOf('total_pips');
  const pfCol = ss.data.metric_columns.indexOf('profit_factor');
  const lowerBetter = SCATTER_METRIC_LOWER_IS_BETTER.has(sel);
  const dir = lowerBetter ? -1 : 1;

  const isProfitable = (row) => {
    if (pipsCol >= 0) {
      const p = row[pipsCol];
      if (p != null && Number.isFinite(p)) return p > 0;
    }
    if (pfCol >= 0) {
      const pf = row[pfCol];
      if (pf != null && Number.isFinite(pf)) return pf > 1.0;
    }
    return true; // unable to determine — don't gate
  };

  const pickFrom = (filter) => {
    let bestIdx = -1, bestScore = -Infinity;
    for (let i = 0; i < ss.data.metrics.length; i++) {
      const row = ss.data.metrics[i];
      const v = row[objCol];
      if (v == null || !Number.isFinite(v)) continue;
      if (filter && !filter(row)) continue;
      const r = rCol >= 0 ? (row[rCol] || 0) : 0;
      const q = qCol >= 0 ? (row[qCol] || 0) : 0;
      // Lexicographic: direction·objective >> return_pct >> quality.
      const score = (dir * v) * 1e18 + r * 1e6 + q;
      if (score > bestScore) { bestScore = score; bestIdx = i; }
    }
    return bestIdx;
  };

  let bestIdx = pickFrom(isProfitable);
  const legend = $('scatter-legend');
  if (bestIdx < 0) {
    // No profitable trial — fall back to best overall and warn the user.
    bestIdx = pickFrom(null);
    if (bestIdx >= 0 && legend) {
      legend.textContent = `no profitable trials in this run — showing best ${sel} overall (still a loser)`;
    }
  }
  if (bestIdx >= 0) showTrial(ss.data.indices[bestIdx]);
}

// ── baseline ───────────────────────────────────────────────────────────

async function onPinBaseline() {
  if (!state.jobId) return;
  try {
    const { baseline } = await api('/api/baseline', {
      method: 'POST',
      body: JSON.stringify({ job_id: state.jobId }),
    });
    state.baseline = baseline;
    updateBaselinePill();
    if (state.lastJob) renderResults(state.lastJob);
  } catch (e) {
    alert('failed to pin baseline: ' + e.message);
  }
}

async function onClearBaseline() {
  try {
    await api('/api/baseline', { method: 'DELETE' });
    state.baseline = null;
    updateBaselinePill();
    if (state.lastJob) renderResults(state.lastJob);
  } catch (e) {
    alert('failed to clear baseline: ' + e.message);
  }
}

function updateBaselinePill() {
  const pill = $('baseline-pill');
  if (state.baseline) {
    pill.textContent = `baseline: ${state.baseline.layer || '—'}`;
    pill.className = 'text-[11px] px-2 py-0.5 rounded-full bg-flame-500/10 text-flame-400 border border-flame-500/40';
  } else {
    pill.textContent = 'no baseline';
    pill.className = 'text-[11px] px-2 py-0.5 rounded-full bg-ink-800 text-slate-400 border border-ink-600';
  }
}

// ── history ────────────────────────────────────────────────────────────

// Cost-realism overlay column helpers. The harness writes a status field
// alongside adjusted_total_pips so a silent overlay exception can't
// publish raw P&L as "adjusted". We render that status as a coloured pill
// next to the adjusted column.
function costRealismBadge(status) {
  if (!status) return '<span class="text-slate-500" title="No cost-realism status (older run)">—</span>';
  const map = {
    ok:     ['bg-emerald-500/20 text-emerald-300', 'overlay applied'],
    empty:  ['bg-slate-500/20 text-slate-400',     'best trial had zero trades'],
    failed: ['bg-rose-500/20 text-rose-300',       'overlay raised — adjusted col fell back to raw'],
  };
  const value = Object.prototype.hasOwnProperty.call(map, status) ? map[status] : null;
  const [cls, hint] = Array.isArray(value) ? value : ['bg-slate-500/20 text-slate-400', status];
  return `<span class="px-1.5 py-0.5 rounded ${cls}" title="${escapeHtml(hint)}">${escapeHtml(status)}</span>`;
}
function costRealismCell(r) {
  // Tint the adjusted-pips cell red if the overlay failed — otherwise
  // adjusted will equal total_pips and look identical, which is exactly
  // what the cost_realism_status="failed" marker is there to flag.
  return r.cost_realism_status === 'failed' ? 'text-rose-300' : '';
}
function signedPipsCell(v) {
  // Render gate_save / cost_overhead as a signed integer with green/red
  // tint. Empty / NaN show "—". A leading + sign on positives makes the
  // sign visible at a glance — these columns can swing either way.
  if (v === undefined || v === '' || v === null) return '<span class="text-slate-500">—</span>';
  const n = +v;
  if (!Number.isFinite(n)) return '<span class="text-slate-500">—</span>';
  const rounded = Math.round(n);
  const display = Object.is(rounded, -0) ? 0 : rounded;
  const cls = display > 0 ? 'text-emerald-300' : (display < 0 ? 'text-rose-300' : 'text-slate-400');
  const sign = display > 0 ? '+' : '';
  return `<span class="${cls}">${sign}${display}</span>`;
}
function integerCell(v) {
  if (v === undefined || v === '' || v === null) return '—';
  const n = +v;
  return Number.isFinite(n) ? n.toFixed(0) : '—';
}

async function refreshHistory() {
  try {
    const { rows } = await api('/api/history');
    const body = $('history-body');
    body.innerHTML = '';
    const last = rows.slice(-30).reverse();
    for (const r of last) {
      const tr = document.createElement('tr');
      const runFile = escapeAttr(r.run_file || '');
      tr.innerHTML = `
        <td class="px-2 py-1.5 text-center">
          <input type="checkbox" class="history-row-cb accent-flame-500" data-run-file="${runFile}" ${runFile ? '' : 'disabled'} />
        </td>
        <td class="px-3 py-1.5 text-slate-400">${escapeHtml(r.datetime)}</td>
        <td class="px-3 py-1.5 text-slate-200">${escapeHtml(r.layer)}</td>
        <td class="px-3 py-1.5 text-slate-400">${escapeHtml(r.pair)}/${escapeHtml(r.main_tf)}</td>
        <td class="px-3 py-1.5 text-right tabular-nums">${escapeHtml(r.n_trials || '')}</td>
        <td class="px-3 py-1.5 text-right tabular-nums">${r.total_pips ? (+r.total_pips).toFixed(0) : '—'}</td>
        <td class="px-3 py-1.5 text-right tabular-nums ${costRealismCell(r)}">${integerCell(r.adjusted_total_pips)}</td>
        <td class="px-3 py-1.5 text-right tabular-nums">${signedPipsCell(r.gate_save_pips)}</td>
        <td class="px-3 py-1.5 text-right tabular-nums">${signedPipsCell(r.cost_overhead_pips)}</td>
        <td class="px-3 py-1.5 text-right tabular-nums text-slate-400">${integerCell(r.n_gated_trades)}</td>
        <td class="px-3 py-1.5 text-center text-[11px]">${costRealismBadge(r.cost_realism_status)}</td>
        <td class="px-3 py-1.5 text-right tabular-nums">${r.max_dd_pct ? (+r.max_dd_pct).toFixed(1) : '—'}</td>
        <td class="px-3 py-1.5 text-right tabular-nums">${r.profit_factor ? (+r.profit_factor).toFixed(2) : '—'}</td>
        <td class="px-3 py-1.5 text-right tabular-nums">${r.sharpe ? (+r.sharpe).toFixed(2) : '—'}</td>
        <td class="px-3 py-1.5 text-right tabular-nums">${r.return_pct ? (+r.return_pct).toFixed(1) : '—'}</td>
        <td class="px-3 py-1.5 text-right">
          <button class="pin-history-btn text-[11px] px-2 py-0.5 rounded border border-ink-600 hover:border-flame-500 hover:text-flame-400 transition" data-run-file="${runFile}">Pin</button>
        </td>
      `;
      body.appendChild(tr);
    }
    // reset header checkbox + delete button enabled state
    const sel = document.getElementById('history-select-all');
    if (sel) sel.checked = false;
    updateDeleteSelectedEnabled();
  } catch {}
}

function getCheckedRunFiles() {
  return Array.from(document.querySelectorAll('.history-row-cb:checked'))
    .map((cb) => cb.dataset.runFile)
    .filter(Boolean);
}

function updateDeleteSelectedEnabled() {
  const btn = document.getElementById('history-delete-selected');
  if (!btn) return;
  btn.disabled = getCheckedRunFiles().length === 0;
}

async function pinHistoryRow(runFile) {
  try {
    const { baseline } = await api('/api/baseline', {
      method: 'POST',
      body: JSON.stringify({ run_file: runFile }),
    });
    state.baseline = baseline;
    updateBaselinePill();
    if (state.lastJob) renderResults(state.lastJob);
  } catch (e) {
    alert('failed to pin baseline: ' + e.message);
  }
}

async function deleteSelectedRuns() {
  const runFiles = getCheckedRunFiles();
  if (!runFiles.length) return;
  if (!confirm(`Delete ${runFiles.length} run(s)? This removes the row from history and the .npz file.`)) return;
  try {
    await api('/api/history/delete', { method: 'POST', body: JSON.stringify({ run_files: runFiles }) });
    await refreshHistory();
  } catch (e) {
    alert('delete failed: ' + e.message);
  }
}

async function clearAllHistory() {
  if (!confirm('Delete ALL history and every saved run file? This cannot be undone.')) return;
  try {
    await api('/api/history/clear', { method: 'POST', body: JSON.stringify({}) });
    await refreshHistory();
  } catch (e) {
    alert('clear failed: ' + e.message);
  }
}

document.addEventListener('click', (e) => {
  const pinBtn = e.target.closest?.('.pin-history-btn');
  if (pinBtn) { pinHistoryRow(pinBtn.dataset.runFile); return; }
  if (e.target.id === 'history-delete-selected') { deleteSelectedRuns(); return; }
  if (e.target.id === 'history-clear-all')       { clearAllHistory();   return; }
});

document.addEventListener('change', (e) => {
  if (e.target.id === 'history-select-all') {
    const on = !!e.target.checked;
    document.querySelectorAll('.history-row-cb').forEach((cb) => {
      if (!cb.disabled) cb.checked = on;
    });
    updateDeleteSelectedEnabled();
    return;
  }
  if (e.target.classList?.contains('history-row-cb')) {
    updateDeleteSelectedEnabled();
  }
});

// ── Data tab ───────────────────────────────────────────────────────────

function updateRangeInfo() {
  const el = $('range-info'); if (!el) return;
  const { start_date, end_date } = state.recipe;
  if (start_date || end_date) {
    el.textContent = `${start_date || '…'} → ${end_date || '…'}`;
    return;
  }
  const rec = inventoryRecord(state.recipe.pair, state.recipe.main_tf);
  if (rec && rec.start_ts && rec.end_ts) {
    el.textContent = `available: ${fmtDate(rec.start_ts)} → ${fmtDate(rec.end_ts)} · pick a preset`;
  } else {
    el.textContent = 'pick a preset';
  }
}

function inventoryRecord(pair, tf) {
  if (!state.inventory || !pair || !tf) return null;
  return state.inventory.find(r => r.pair === pair && r.tf === tf) || null;
}

function clearDateRange() {
  $('start_date').value = '';
  $('end_date').value = '';
  state.recipe.start_date = null;
  state.recipe.end_date = null;
  updateRangeInfo();
}

// Local-date YYYY-MM-DD (avoid toISOString() — it shifts to UTC and silently
// off-by-ones for users east of GMT when the clock is at local midnight).
function localISODate(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

// Shared date-math for all preset rows (Parameters + Data tab download cards).
// Returns ISO YYYY-MM-DD strings clipped to [invStart, invEnd].
function computePresetWindow(key, invStart, invEnd) {
  const end = new Date(invEnd);
  let start = new Date(invEnd);
  switch (key) {
    case '1m':  start.setMonth(end.getMonth() - 1);  break;
    case '3m':  start.setMonth(end.getMonth() - 3);  break;
    case '6m':  start.setMonth(end.getMonth() - 6);  break;
    case 'ytd': start = new Date(end.getFullYear(), 0, 1); break;
    case '1y':  start.setFullYear(end.getFullYear() - 1); break;
    case '2y':  start.setFullYear(end.getFullYear() - 2); break;
    case '5y':  start.setFullYear(end.getFullYear() - 5); break;
    case 'full': start = new Date(invStart); break;
    default: return null;
  }
  if (start < invStart) start = new Date(invStart);
  if (end > invEnd) start = new Date(invStart);
  return { start: localISODate(start), end: localISODate(end) };
}

async function applyRangePreset(key) {
  // Inventory caches the (start_ts, end_ts) for every parquet on disk. Load it
  // on first use so the Parameters tab works before the Data tab is opened.
  if (!state.inventory) {
    try { await refreshInventory(false); } catch {}
  }
  const rec = inventoryRecord(state.recipe.pair, state.recipe.main_tf);
  if (!rec || !rec.end_ts) {
    $('range-info').textContent = 'no inventory for this pair/TF — open the Data tab';
    return;
  }
  const win = computePresetWindow(key, new Date(rec.start_ts), new Date(rec.end_ts));
  if (!win) return;
  $('start_date').value = win.start;
  $('end_date').value = win.end;
  state.recipe.start_date = win.start;
  state.recipe.end_date = win.end;
  updateRangeInfo();
}

// Download-card preset: anchor = today (so we always pull "up to now"), and
// invStart falls back to 2005-01-01 when we have no inventory yet. The Tick
// card clamps Full to today − 1y because tick files are ~500 MB / pair / yr.
function applyDownloadRangePreset(card, key) {
  const cfg = card === 'tick'
    ? { pairId: 'tdl-pair', startId: 'tdl-start', endId: 'tdl-end', tf: 'TICK' }
    : { pairId: 'dl-pair',  startId: 'dl-start',  endId: 'dl-end',  tf: 'M1'   };
  const pair = $(cfg.pairId).value;
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const rec = pair ? inventoryRecord(pair, cfg.tf) : null;
  const invStart = rec && rec.start_ts ? new Date(rec.start_ts) : new Date('2005-01-01');
  let win = computePresetWindow(key, invStart, today);
  if (!win) return;
  if (card === 'tick' && key === 'full') {
    const clamp = new Date(today); clamp.setFullYear(today.getFullYear() - 1);
    if (new Date(win.start) < clamp) win.start = localISODate(clamp);
  }
  $(cfg.startId).value = win.start;
  $(cfg.endId).value = win.end;
}

// On pair change, default Start = last-held bar date (so click Append =
// download only what's missing) and End = today. Pass force=true from the
// preset handler when we explicitly want to overwrite the user's typed value.
function autoFillDownloadDates(card, { force = false } = {}) {
  const cfg = card === 'tick'
    ? { pairId: 'tdl-pair', startId: 'tdl-start', endId: 'tdl-end', tf: 'TICK' }
    : { pairId: 'dl-pair',  startId: 'dl-start',  endId: 'dl-end',  tf: 'M1'   };
  const pairSel = $(cfg.pairId);
  const startInp = $(cfg.startId);
  const endInp = $(cfg.endId);
  if (!pairSel || !startInp || !endInp) return;
  const today = localISODate(new Date());
  if (force || !endInp.value) endInp.value = today;
  const rec = inventoryRecord(pairSel.value, cfg.tf);
  if (rec && rec.end_ts) {
    if (force || !startInp.value) startInp.value = String(rec.end_ts).slice(0, 10);
  } else if (force) {
    startInp.value = '';
  }
}

function fmtMB(bytes) {
  if (!bytes) return '—';
  return (bytes / (1024 * 1024)).toFixed(1);
}

function fmtDate(iso) {
  if (!iso) return '—';
  return String(iso).slice(0, 10);
}

function fmtMtime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function statusChip(s) {
  const cls = {
    ok:   'bg-ok-500/10 text-ok-400 border-ok-500/30',
    thin: 'bg-flame-500/10 text-flame-400 border-flame-500/30',
    empty:'bg-bad-500/10 text-bad-400 border-bad-500/30',
    error:'bg-bad-500/10 text-bad-400 border-bad-500/30',
  }[s] || 'bg-ink-800 text-slate-400 border-ink-600';
  return `<span class="px-1.5 py-0.5 rounded text-[10px] border ${cls}">${escapeHtml(s)}</span>`;
}

async function refreshInventory(force = false) {
  const btn = $('inventory-rescan');
  if (btn) { btn.disabled = true; btn.textContent = force ? 'Rescanning…' : 'Loading…'; }
  try {
    const ep = force ? '/api/data/inventory/rescan' : '/api/data/inventory';
    const opts = force
      ? { method: 'POST', body: '{}', timeoutMs: 60_000 }
      : { timeoutMs: 15_000 };
    const { files } = await api(ep, opts);
    state.inventory = files;
    renderInventoryTable(files);
    populateDownloadPairSelect(files);
  } catch (e) {
    const msg = /timeout/i.test(e.message)
      ? 'server unresponsive — run <code>scripts\\ff_kill_server.ps1</code> then restart <code>run.py web</code>'
      : escapeHtml(e.message);
    $('inventory-body').innerHTML = `<tr><td colspan="9" class="px-3 py-3 text-bad-400">${msg}</td></tr>`;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Rescan'; }
  }
}

function renderInventoryTable(files) {
  const root = $('inventory-body');
  if (!files.length) {
    root.innerHTML = '<div class="px-3 py-3 text-slate-500 text-xs">no parquet files found</div>';
    $('inventory-summary').textContent = '0 files';
    return;
  }

  // TF order so M1 / M5 / .. / W / TICK sort consistently inside each pair.
  const TF_ORDER = ['TICK', 'M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D', 'W'];
  const tfRank = (t) => { const i = TF_ORDER.indexOf(t); return i < 0 ? 99 : i; };
  const sortFiles = (rows) => rows.slice().sort((a, b) =>
    a.pair === b.pair ? tfRank(a.tf) - tfRank(b.tf) : a.pair.localeCompare(b.pair));

  const fileRow = (r) => {
    let action = '';
    if (r.tf === 'M1') {
      action = `<button class="ml-2 text-[10px] px-1.5 py-0.5 rounded border border-ink-600 hover:border-flame-500 hover:text-flame-400" data-action="derive" title="Resample M1 → M5..W">Roll up</button>`;
    } else if (r.tf === 'TICK') {
      action = `<button class="ml-2 text-[10px] px-1.5 py-0.5 rounded border border-ink-600 hover:border-flame-500 hover:text-flame-400" data-action="tick-rebuild" title="Tick → M1 → M5..W">Rebuild</button>`;
    }
    return `
    <tr data-pair="${escapeAttr(r.pair)}" data-tf="${escapeAttr(r.tf)}"
        class="cursor-pointer hover:bg-ink-800/50">
      <td class="px-3 py-1.5 text-slate-200">${escapeHtml(r.pair)}</td>
      <td class="px-3 py-1.5 text-slate-400">${escapeHtml(r.tf)}</td>
      <td class="px-3 py-1.5 text-right tabular-nums">${(r.bars || 0).toLocaleString()}</td>
      <td class="px-3 py-1.5 text-slate-400">${escapeHtml(fmtDate(r.start_ts))}</td>
      <td class="px-3 py-1.5 text-slate-400">${escapeHtml(fmtDate(r.end_ts))}</td>
      <td class="px-3 py-1.5 text-right tabular-nums">${escapeHtml(fmtMB(r.size_bytes))}</td>
      <td class="px-3 py-1.5 text-slate-500">${escapeHtml(fmtMtime(r.mtime))}</td>
      <td class="px-3 py-1.5">${r.has_spread ? '✓' : '—'}</td>
      <td class="px-3 py-1.5">${statusChip(r.status || 'ok')}${action}</td>
    </tr>`;
  };

  // Bucket files by category. Fall back to single "All" group when /api/pairs
  // never returned the groups field (older server / first paint pre-boot).
  const groups = state.pairs_groups;
  const sections = [];
  if (groups) {
    const seen = new Set();
    for (const [name, pairs] of Object.entries(groups)) {
      const set = new Set(pairs);
      const bucket = files.filter(f => set.has(f.pair));
      if (bucket.length) { sections.push([name, sortFiles(bucket)]); bucket.forEach(b => seen.add(b.pair)); }
    }
    const leftover = files.filter(f => !sections.some(([_, fs]) => fs.includes(f)));
    if (leftover.length) sections.push(['Other', sortFiles(leftover)]);
  } else {
    sections.push(['All', sortFiles(files)]);
  }

  // Per-group <details> card with embedded table. open/closed state persists
  // across renders via localStorage so a rescan doesn't slam everything closed.
  const storageKey = (name) => `ff.inventory.group.${name}.open`;
  const isOpen = (name) => {
    const saved = localStorage.getItem(storageKey(name));
    return saved === null ? true : saved === '1';  // default open
  };
  const groupCount = (rows) => new Set(rows.map(r => r.pair)).size;
  const groupSize = (rows) => rows.reduce((s, r) => s + (r.size_bytes || 0), 0);

  const tableHead = `
    <thead class="bg-ink-800/40 text-slate-400 uppercase text-[10px]">
      <tr>
        <th class="text-left px-3 py-1.5">Pair</th>
        <th class="text-left px-3 py-1.5">TF</th>
        <th class="text-right px-3 py-1.5">Bars</th>
        <th class="text-left px-3 py-1.5">From</th>
        <th class="text-left px-3 py-1.5">To</th>
        <th class="text-right px-3 py-1.5">MB</th>
        <th class="text-left px-3 py-1.5">Modified</th>
        <th class="text-left px-3 py-1.5">Spread</th>
        <th class="text-left px-3 py-1.5">Status</th>
      </tr>
    </thead>`;

  root.innerHTML = sections.map(([name, rows]) => `
    <details class="rounded border border-ink-700/60 bg-ink-900/40 overflow-hidden" data-group="${escapeAttr(name)}" ${isOpen(name) ? 'open' : ''}>
      <summary class="cursor-pointer select-none px-3 py-2 flex items-center justify-between bg-ink-800/40 hover:bg-ink-800/60">
        <span class="text-flame-400 text-[11px] uppercase tracking-wider font-semibold">
          ${escapeHtml(name)}
          <span class="text-slate-500 normal-case font-normal ml-2">${groupCount(rows)} pairs · ${rows.length} files · ${(groupSize(rows) / (1024 ** 3)).toFixed(2)} GB</span>
        </span>
        <span class="text-slate-500 text-[10px]">click to toggle</span>
      </summary>
      <div class="overflow-x-auto">
        <table class="w-full text-xs">
          ${tableHead}
          <tbody class="divide-y divide-ink-700/40 font-mono">${rows.map(fileRow).join('')}</tbody>
        </table>
      </div>
    </details>
  `).join('');

  // Persist toggle state per group.
  root.querySelectorAll('details[data-group]').forEach(d => {
    d.addEventListener('toggle', () => {
      localStorage.setItem(storageKey(d.dataset.group), d.open ? '1' : '0');
    });
  });

  const totalBytes = files.reduce((s, r) => s + (r.size_bytes || 0), 0);
  $('inventory-summary').textContent = `${files.length} files · ${(totalBytes / (1024 ** 3)).toFixed(2)} GB · ${sections.length} group${sections.length === 1 ? '' : 's'}`;
}

function populateDownloadPairSelect(files) {
  const pairs = Array.from(new Set(files.map(r => r.pair))).sort();
  // Server-side groups (Majors/Crosses/Metals/Indices/Crypto/Other) come from
  // /api/pairs and are cached on state. Filter each group to what we actually
  // have on disk so empty headings don't appear.
  const groups = state.pairs_groups || null;
  const buckets = [];
  if (groups) {
    const seen = new Set();
    for (const [name, list] of Object.entries(groups)) {
      const present = list.filter(p => pairs.includes(p));
      if (present.length) { buckets.push([name, present]); present.forEach(p => seen.add(p)); }
    }
    const leftover = pairs.filter(p => !seen.has(p)).sort();
    if (leftover.length) buckets.push(['Other', leftover]);
  } else {
    buckets.push(['', pairs]);
  }
  for (const id of ['dl-pair', 'tdl-pair']) {
    const sel = $(id); if (!sel) continue;
    const prev = sel.value;
    sel.innerHTML = '';
    for (const [label, list] of buckets) {
      const parent = label
        ? sel.appendChild(Object.assign(document.createElement('optgroup'), { label }))
        : sel;
      for (const p of list) {
        const o = document.createElement('option'); o.value = p; o.textContent = p.replace('_', '/');
        parent.appendChild(o);
      }
    }
    if (pairs.includes(prev)) sel.value = prev;
    else if (pairs.includes('EUR_USD')) sel.value = 'EUR_USD';
  }
  // Parameter-tab presets depend on inventory; refresh the info line whenever
  // new data is loaded.
  updateRangeInfo();
  // Pre-fill the download date inputs from the now-known inventory bounds.
  autoFillDownloadDates('bars');
  autoFillDownloadDates('tick');
}

async function runHealthCheck(pair, tf) {
  const body = $('health-body');
  $('health-subtitle').textContent = `${pair} ${tf} — running…`;
  body.innerHTML = `
    <div class="text-slate-400 text-sm">scanning parquet…</div>
    <div class="text-slate-500 text-[11px] mt-1">
      cold files on Google Drive can take 1–2 min on first read
    </div>`;
  try {
    const rep = await api(`/api/data/health/${pair}/${tf}`, { timeoutMs: 120_000 });
    renderHealthReport(rep);
  } catch (e) {
    const isTimeout = /timeout/i.test(e.message);
    const banner = isTimeout
      ? `health check timed out after 2 min — the parquet is either very cold or the server is hung.
         run <code>scripts\\ff_kill_server.ps1</code> then restart <code>run.py web</code> if retry also times out.`
      : escapeHtml(e.message);
    body.innerHTML = `
      <div class="text-bad-400 text-sm mb-2">${banner}</div>
      <button id="health-retry-btn"
              class="text-[11px] px-2 py-1 rounded border border-ink-600 hover:border-flame-500 hover:text-flame-400">
        Retry
      </button>`;
    const retry = document.getElementById('health-retry-btn');
    if (retry) retry.addEventListener('click', () => runHealthCheck(pair, tf));
  }
}

function renderHealthReport(r) {
  $('health-subtitle').textContent =
    `${r.pair} ${r.tf} — ${r.bars?.toLocaleString() || 0} bars · ${fmtDate(r.range?.start)} → ${fmtDate(r.range?.end)}`;
  const sect = (title, obj) => {
    const pairs = Object.entries(obj || {})
      .map(([k, v]) => `<div class="flex justify-between"><span class="text-slate-500">${escapeHtml(k)}</span><span class="tabular-nums">${escapeHtml(String(v))}</span></div>`).join('');
    return `<div class="bg-ink-800/40 rounded p-2 border border-ink-700/60"><div class="text-[10px] uppercase text-slate-400 mb-1">${escapeHtml(title)}</div>${pairs || '<div class="text-slate-600 text-[11px]">n/a</div>'}</div>`;
  };
  const gaps = (r.gap_samples || []).filter(g => !g._more);
  const moreCount = (r.gap_samples || []).find(g => g._more)?._more || 0;
  const gapsHtml = gaps.length === 0
    ? '<div class="text-slate-500">no non-weekend gaps detected</div>'
    : gaps.map(g => `<div class="flex justify-between text-[11px]"><span class="text-slate-400">${escapeHtml(g.from)} → ${escapeHtml(g.to)}</span><span class="tabular-nums text-flame-400">${escapeHtml(g.gap_minutes)}m</span></div>`).join('')
      + (moreCount ? `<div class="text-slate-500 mt-1">+${moreCount} more</div>` : '');
  const errBanner = r.error_detail
    ? `<div class="mb-3 p-2 rounded border border-bad-500/40 bg-bad-500/10 text-bad-400 text-xs">
         ${escapeHtml(r.error_detail)}
       </div>`
    : '';
  $('health-body').innerHTML = `
    ${errBanner}
    <div class="flex items-center gap-2 mb-3">
      <span class="text-xs text-slate-400">Summary:</span>
      ${statusChip(r.summary || 'ok')}
    </div>
    <div class="grid grid-cols-1 md:grid-cols-4 gap-3 text-xs">
      ${sect('NaN counts', r.nan_counts)}
      ${sect('OHLC violations', r.ohlc_violations)}
      ${sect('Timestamps', r.timestamp_issues)}
      ${sect('Spread', r.spread)}
    </div>
    <div class="mt-3">
      <div class="text-[10px] uppercase text-slate-400 mb-1">Gap samples (weekend-filtered)</div>
      ${gapsHtml}
    </div>`;
}

async function submitDownload() {
  // M1-locked card; higher TFs come from the local rollup chain after the
  // M1 fetch lands (see ff/data/resample.derive_higher_tfs).
  const body = {
    pair: $('dl-pair').value,
    tf: 'M1',
    start: $('dl-start').value,
    end: $('dl-end').value,
    append: $('dl-append').checked,
  };
  if (!body.pair || !body.start || !body.end) {
    $('dl-status').textContent = 'pair, start and end are required';
    return;
  }
  try {
    $('dl-log').textContent = '';
    $('dl-status').textContent = 'starting…';
    $('dl-submit').disabled = true;
    $('dl-cancel').classList.remove('hidden');
    const { job_id } = await api('/api/data/download', { method: 'POST', body: JSON.stringify(body) });
    state.dlJobId = job_id;
    pollDownloadJob();
  } catch (e) {
    $('dl-status').textContent = 'error: ' + e.message;
    $('dl-submit').disabled = false;
    $('dl-cancel').classList.add('hidden');
  }
}

async function pollDownloadJob() {
  if (!state.dlJobId) return;
  clearTimeout(state.dlTimer);
  try {
    const j = await api(`/api/data/download/${state.dlJobId}`);
    $('dl-status').textContent = `${j.status} — ${j.message || ''}`;
    if (Array.isArray(j.tail_lines)) $('dl-log').textContent = j.tail_lines.join('\n');
    $('dl-log').scrollTop = $('dl-log').scrollHeight;
    if (j.status === 'running') {
      state.dlTimer = setTimeout(pollDownloadJob, 1000);
    } else {
      $('dl-submit').disabled = false;
      $('dl-cancel').classList.add('hidden');
      state.dlJobId = null;
      refreshInventory(true);
    }
  } catch (e) {
    $('dl-status').textContent = 'poll error: ' + e.message;
    state.dlTimer = setTimeout(pollDownloadJob, 2000);
  }
}

async function cancelDownload() {
  if (!state.dlJobId) return;
  try {
    await api(`/api/data/download/${state.dlJobId}/cancel`, { method: 'POST', body: '{}' });
  } catch (e) {
    $('dl-status').textContent = 'cancel error: ' + e.message;
  }
}

// ── Tick downloads (separate pipeline: ticks → M1 → M5..W) ────────────

async function submitTickDownload() {
  const body = {
    pair: $('tdl-pair').value,
    start: $('tdl-start').value,
    end: $('tdl-end').value,
    append: $('tdl-append').checked,
  };
  if (!body.pair || !body.start || !body.end) {
    $('tdl-status').textContent = 'pair, start and end are required';
    return;
  }
  try {
    $('tdl-log').textContent = '';
    $('tdl-status').textContent = 'starting…';
    $('tdl-submit').disabled = true;
    $('tdl-cancel').classList.remove('hidden');
    const { job_id } = await api('/api/data/download/tick', { method: 'POST', body: JSON.stringify(body) });
    state.tickDlJobId = job_id;
    pollTickDownloadJob();
  } catch (e) {
    $('tdl-status').textContent = 'error: ' + e.message;
    $('tdl-submit').disabled = false;
    $('tdl-cancel').classList.add('hidden');
  }
}

async function pollTickDownloadJob() {
  if (!state.tickDlJobId) return;
  clearTimeout(state.tickDlTimer);
  try {
    const j = await api(`/api/data/download/tick/${state.tickDlJobId}`);
    $('tdl-status').textContent = `${j.status} — ${j.message || ''}`;
    if (Array.isArray(j.tail_lines)) $('tdl-log').textContent = j.tail_lines.join('\n');
    $('tdl-log').scrollTop = $('tdl-log').scrollHeight;
    if (j.status === 'running') {
      state.tickDlTimer = setTimeout(pollTickDownloadJob, 1500);
    } else {
      $('tdl-submit').disabled = false;
      $('tdl-cancel').classList.add('hidden');
      state.tickDlJobId = null;
      refreshInventory(true);
    }
  } catch (e) {
    $('tdl-status').textContent = 'poll error: ' + e.message;
    state.tickDlTimer = setTimeout(pollTickDownloadJob, 2500);
  }
}

async function cancelTickDownload() {
  if (!state.tickDlJobId) return;
  try {
    await api(`/api/data/download/tick/${state.tickDlJobId}/cancel`, { method: 'POST', body: '{}' });
  } catch (e) {
    $('tdl-status').textContent = 'cancel error: ' + e.message;
  }
}

// ── Inventory row actions (Roll-up / Rebuild from ticks) ───────────────

async function runInventoryAction(action, pair) {
  const url = action === 'derive' ? `/api/data/derive/${pair}`
            : action === 'tick-rebuild' ? `/api/data/tick-to-m1/${pair}`
            : null;
  if (!url) return;
  const btn = document.querySelector(
    `tr[data-pair="${pair}"] [data-action="${action}"]`
  );
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  try {
    const rep = await api(url, { method: 'POST', body: '{}' });
    if (action === 'tick-rebuild') {
      // Chain derive after tick→M1 finishes so every TF reflects the rebuild.
      await api(`/api/data/derive/${pair}`, { method: 'POST', body: '{}' });
    }
    console.log('action ok', action, pair, rep);
  } catch (e) {
    alert(`${action} failed: ${e.message}`);
  } finally {
    refreshInventory(true);
  }
}

// ── Live tab ───────────────────────────────────────────────────────────

const LIVE_POLL_MS = 5000;
let _livePollHandle = null;
let _liveLastPlanId = null;

function _liveFmt(x, d=5) { return x == null || Number.isNaN(+x) ? '–' : (+x).toFixed(d); }

async function liveRefreshStatus() {
  try {
    const s = await api('/api/live/status');
    const pill = document.getElementById('live-status-pill');
    if (pill) {
      pill.textContent = s.status;
      pill.className = 'px-2 py-0.5 rounded-full border ' + (
        s.status === 'running' ? 'bg-ok-500/10 text-ok-400 border-ok-500/30'
        : s.status === 'error' ? 'bg-bad-500/10 text-bad-400 border-bad-500/40'
        : 'bg-ink-800 text-slate-400 border-ink-600'
      );
    }
    const up = document.getElementById('live-uptime');
    if (up) up.textContent = (s.uptime_sec > 0) ? `${Math.round(s.uptime_sec)}s` : '–';
    const pairs = document.getElementById('live-pairs');
    if (pairs) pairs.textContent = (s.pairs || []).join(', ') || '–';
    const openN = document.getElementById('live-open-n');
    if (openN) {
      const n = Object.values(s.open_positions || {}).reduce((acc, m) => acc + Object.keys(m || {}).length, 0);
      openN.textContent = String(n);
    }
    const plansN = document.getElementById('live-plans-n');
    if (plansN) plansN.textContent = String(s.plans_today ?? 0);
  } catch (e) {
    console.warn('live status', e);
  }
}

async function liveRefreshPlans() {
  try {
    const r = await api('/api/live/plans?limit=200');
    const rows = (r.plans || []);
    const body = document.getElementById('live-plans-body');
    if (!body) return;
    body.innerHTML = rows.slice().reverse().map(p => `
      <tr>
        <td class="px-3 py-1 text-slate-300">${p.signal_bar_ts || '–'}</td>
        <td class="px-3 py-1">${p.pair || '–'}</td>
        <td class="px-3 py-1 text-right ${p.direction > 0 ? 'text-ok-400' : 'text-bad-400'}">${p.direction > 0 ? '+1' : '-1'}</td>
        <td class="px-3 py-1 text-right">${_liveFmt(p.entry_ref_price)}</td>
        <td class="px-3 py-1 text-right">${_liveFmt(p.sl_price)}</td>
        <td class="px-3 py-1 text-right">${_liveFmt(p.tp_price)}</td>
        <td class="px-3 py-1 text-slate-500 text-[11px]">${p.plan_id || ''}</td>
      </tr>`).join('');
    if (rows.length) _liveLastPlanId = rows[rows.length - 1].plan_id;
  } catch (e) {
    console.warn('live plans', e);
  }
}

async function liveRefreshPairCards() {
  const host = document.getElementById('live-pair-cards');
  if (!host) return;
  try {
    const r = await api('/api/live/stats_by_pair');
    const pairs = r.pairs || {};
    const entries = Object.entries(pairs).sort((a, b) => a[0].localeCompare(b[0]));
    if (!entries.length) {
      host.innerHTML = `<div class="text-slate-500 col-span-full">
        No replay run yet. Run <code>python run.py replay</code> to populate parity cards.
      </div>`;
      return;
    }
    host.innerHTML = entries.map(([pair, s]) => {
      const open = s.has_open_position ? '<span class="text-flame-400">●</span>' : '<span class="text-slate-600">○</span>';
      const delta = Number(s.delta_pips || 0);
      const deltaCls = delta > 0.5 ? 'text-ok-400'
                     : delta < -0.5 ? 'text-bad-400'
                     : 'text-slate-400';
      const mismatches =
        (s.n_mismatched_signal || 0) +
        (s.n_mismatched_spread || 0) +
        (s.n_mismatched_slippage || 0) +
        (s.n_mismatched_closure || 0);
      return `
        <div class="rounded border border-ink-600 bg-ink-900/60 p-2">
          <div class="flex items-center justify-between">
            <span class="text-slate-200 font-semibold">${pair}</span>
            ${open}
          </div>
          <div class="mt-1 text-slate-400">
            live ${s.matched || 0} · bt ${(s.matched || 0) + (s.missing_in_live || 0)}
          </div>
          <div class="${deltaCls}">Δ ${delta >= 0 ? '+' : ''}${delta.toFixed(1)} pips</div>
          ${mismatches ? `<div class="text-flame-400">${mismatches} mismatch${mismatches > 1 ? 'es' : ''}</div>` : ''}
        </div>`;
    }).join('');
  } catch (e) {
    console.warn('live pair cards', e);
  }
}

function liveStartPolling() {
  if (_livePollHandle) return;
  liveRefreshStatus(); liveRefreshPlans(); liveRefreshPairCards();
  _livePollHandle = setInterval(() => {
    liveRefreshStatus(); liveRefreshPlans(); liveRefreshPairCards();
  }, LIVE_POLL_MS);
}

function liveStopPolling() {
  if (_livePollHandle) { clearInterval(_livePollHandle); _livePollHandle = null; }
}

async function liveStart() {
  const body = {
    recipe: state.recipe,
    overrides: state.overrides || {},
    pairs: state.recipe?.pair ? [state.recipe.pair] : [],
    // Broker profile comes from the VPS .env.live — UI sends empty placeholder;
    // the runner service is expected to merge creds before calling broker.connect().
    // For dev testing the Start button will error out unless a broker dict is posted.
    broker: {
      login: 0, password: "", server: "",
      deviation_pips: 3, magic_number: 20260420,
      symbol_map: {}
    }
  };
  try {
    const r = await api('/api/live/start', { method: 'POST', body: JSON.stringify(body) });
    console.log('live start', r);
    liveRefreshStatus();
  } catch (e) {
    alert(`live start failed: ${e.message}`);
  }
}

async function liveStop() {
  try {
    await api('/api/live/stop', { method: 'POST', body: '{}' });
    liveRefreshStatus();
  } catch (e) {
    alert(`live stop failed: ${e.message}`);
  }
}

async function liveReconcile() {
  const runId = prompt('run_id to reconcile against (stem of artifacts/runs/*.npz):');
  if (!runId) return;
  try {
    const r = await api('/api/live/reconcile/run', {
      method: 'POST',
      body: JSON.stringify({ run_id: runId }),
    });
    console.log('reconcile', r);
    const f = document.getElementById('live-reconcile-frame');
    if (f) f.src = `/api/live/reconcile/latest.html?t=${Date.now()}`;
  } catch (e) {
    alert(`reconcile failed: ${e.message}`);
  }
}

document.addEventListener('click', (e) => {
  const t = e.target;
  if (!t || !t.id) return;
  if (t.id === 'live-start-btn') liveStart();
  if (t.id === 'live-stop-btn')  liveStop();
  if (t.id === 'live-reconcile-btn') liveReconcile();
  if (t.id === 'deploy-live-btn') deployToLive();
});

async function deployToLive() {
  const runFile = state?.scatter?.runFile;
  if (!runFile) { alert('No run loaded — run a backtest first.'); return; }

  const pair = state?.recipe?.pair;
  if (!pair) { alert('No pair set on the Parameters tab.'); return; }

  const extraPairs = prompt(
    `Trade these pairs live (comma-separated). Default is just ${pair}.`,
    pair
  );
  if (!extraPairs) return;
  const pairs = extraPairs.split(',').map(s => s.trim()).filter(Boolean);

  const body = {
    run_id: runFile,
    recipe: state.recipe,
    overrides: state.overrides || {},
    pairs,
    poll_interval_sec: 1.0,
    size_lots: 0.01,
  };

  try {
    const r = await api('/api/live/deploy_from_run', {
      method: 'POST',
      body: JSON.stringify(body),
    });
    const lines = [`Deployed.`, ``, `source_run_id = ${r.source_run_id}`];
    if (r.git_pushed) {
      lines.push(``, `✔ Config pushed to GitHub.`);
      lines.push(`→ On the VPS: double-click the "Deploy Fire Forex" desktop shortcut.`);
    } else if (r.git_error) {
      lines.push(``, `⚠ Git push failed: ${r.git_error}`);
      lines.push(`Copy ${r.service_config_path} to the VPS manually.`);
    }
    if (r.runner_kicked) {
      lines.push(``, `✔ Local runner restarted via Scheduled Task.`);
    }
    alert(lines.join('\n'));
    switchTab('live');
  } catch (e) {
    alert(`Deploy failed: ${e.message}`);
  }
}

// Start/stop polling when the Live tab becomes visible.
const _origSwitchTab = switchTab;
switchTab = function(name) {
  _origSwitchTab(name);
  if (name === 'live') liveStartPolling();
  else liveStopPolling();
};

// ── go ─────────────────────────────────────────────────────────────────

boot();
