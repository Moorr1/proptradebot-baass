// =============================================================================
// Ladder settings model (Web + Agent step 2). Shared rules for validation and
// "does this change add risk?". The app (settings_sync.py) implements the SAME
// rules and is the authority; both are tested against test/settings_vectors.json
// so they cannot drift apart silently.
// =============================================================================

const POINT_VALUE = { MES: 5, ES: 50, MNQ: 2, NQ: 20 };
const LEG_INSTRUMENTS = { sp: ['MES', 'ES'], nq: ['MNQ', 'NQ'] };
const FULL_SIZE = new Set(['ES', 'NQ']);
const MAX_CONTRACTS = 50;               // server sanity cap; the app's local limit is usually far lower
const LEG_NAMES = { sp: 'S&P leg', nq: 'Nasdaq leg' };

const isInt = (v) => Number.isInteger(v);
const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const hhmm = (s) => typeof s === 'string' && /^([01]\d|2[0-3]):[0-5]\d$/.test(s);
const minutes = (s) => (hhmm(s) ? Number(s.slice(0, 2)) * 60 + Number(s.slice(3)) : null);

function legRisk(leg) {
  if (!leg || !leg.contracts) return 0;
  return leg.contracts * leg.stop * (POINT_VALUE[leg.instrument] || 0);
}

// Returns a list of problems (empty = valid). Messages are shown to the customer.
function validate(s, { knownRefs } = {}) {
  const e = [];
  if (!s || typeof s !== 'object') return ['Settings are missing.'];
  if (s.schema !== 1) e.push('Unsupported settings version.');
  const legs = s.legs || {};
  for (const k of ['sp', 'nq']) {
    const L = legs[k], name = LEG_NAMES[k];
    if (!L || typeof L !== 'object') { e.push(`${name} is missing.`); continue; }
    if (!LEG_INSTRUMENTS[k].includes(L.instrument)) e.push(`${name}: instrument must be ${LEG_INSTRUMENTS[k].join(' or ')}.`);
    for (const f of ['contracts', 't1_contracts', 't2_contracts', 'runner_contracts'])
      if (!isInt(L[f]) || L[f] < 0) e.push(`${name}: ${f.replace('_', ' ')} must be a whole number, 0 or more.`);
    for (const f of ['stop', 'stop_after_t1', 't1_points', 't2_points', 'runner_close'])
      if (!isNum(L[f]) || L[f] < 0) e.push(`${name}: ${f.replace(/_/g, ' ')} must be a number, 0 or more.`);
    if (e.length) continue;
    if (L.contracts > MAX_CONTRACTS) e.push(`${name}: ${L.contracts} contracts is above the ${MAX_CONTRACTS} maximum.`);
    if (L.contracts === 0) continue;                         // leg off: the rest doesn't matter
    if (L.t1_contracts + L.t2_contracts + L.runner_contracts !== L.contracts)
      e.push(`${name}: T1 + T2 + runner (${L.t1_contracts + L.t2_contracts + L.runner_contracts}) must equal contracts (${L.contracts}).`);
    if (!(L.stop > 0)) e.push(`${name}: set an initial stop. Every entry needs one.`);
    if (L.stop_after_t1 > L.stop) e.push(`${name}: the stop after T1 is further away than the initial stop.`);
    if (L.t1_contracts > 0 && !(L.t1_points > 0)) e.push(`${name}: T1 target must be above 0.`);
    if (L.t2_contracts > 0 && !(L.t1_points < L.t2_points)) e.push(`${name}: T2 must be further than T1.`);
    if (L.runner_contracts > 0 && !(L.t2_points < L.runner_close)) e.push(`${name}: runner close must be further than T2.`);
  }
  if (typeof s.broker_ladder !== 'boolean') e.push('Broker ladder must be on or off.');
  if (!s.eod || typeof s.eod.enabled !== 'boolean' || !hhmm(s.eod.time)) e.push('EOD flatten needs on/off and a time like 15:55.');
  if (!Array.isArray(s.accounts)) e.push('Accounts are missing.');
  else {
    for (const a of s.accounts) {
      if (!a || typeof a.ref !== 'string' || !/^a_[0-9a-f]{16}$/.test(a.ref) || typeof a.enabled !== 'boolean')
        { e.push('An account entry is malformed.'); break; }
    }
    if (knownRefs) {
      const refs = s.accounts.map((a) => a && a.ref);
      if (refs.length !== knownRefs.length || refs.some((r) => !knownRefs.includes(r)))
        e.push('Accounts can only be added or removed in the app on your computer.');
    }
  }
  return e;
}

// Reasons this change ADDS risk (empty = same or less). The app applies a
// change with no reasons straight away and asks for approval otherwise.
function riskIncrease(oldS, newS) {
  const r = [];
  if (!oldS) return ['First settings from the website.'];
  for (const k of ['sp', 'nq']) {
    const a = oldS.legs[k] || {}, b = newS.legs[k] || {}, name = LEG_NAMES[k];
    if ((b.contracts || 0) === 0) continue;
    if ((b.contracts || 0) > (a.contracts || 0)) r.push(`${name}: more contracts (${a.contracts || 0} → ${b.contracts}).`);
    if (FULL_SIZE.has(b.instrument) && !FULL_SIZE.has(a.instrument)) r.push(`${name}: switch to full-size ${b.instrument}.`);
    if ((a.contracts || 0) > 0 && b.stop > a.stop) r.push(`${name}: wider stop (${a.stop} → ${b.stop} pts).`);
    if ((a.contracts || 0) > 0 && b.stop_after_t1 > a.stop_after_t1) r.push(`${name}: looser stop after T1 (${a.stop_after_t1} → ${b.stop_after_t1} pts).`);
    if (legRisk(b) > legRisk(a) + 1e-9 && !r.some((x) => x.startsWith(name))) r.push(`${name}: more money at risk per trade.`);
  }
  const was = new Map((oldS.accounts || []).map((x) => [x.ref, x.enabled]));
  for (const acc of newS.accounts || []) if (acc.enabled && !was.get(acc.ref)) r.push(`Account ${acc.label || acc.ref} turned on.`);
  if (oldS.eod.enabled && !newS.eod.enabled) r.push('EOD flatten turned off.');
  if (oldS.eod.enabled && newS.eod.enabled && minutes(newS.eod.time) > minutes(oldS.eod.time)) r.push(`EOD flatten later (${oldS.eod.time} → ${newS.eod.time}).`);
  if (oldS.broker_ladder && !newS.broker_ladder) r.push('Broker ladder turned off (exits then depend on the app staying up).');
  return r;
}

function totalRisk(s) {
  const n = (s.accounts || []).filter((a) => a.enabled).length;
  const per = legRisk(s.legs.sp) + legRisk(s.legs.nq);
  return { perAccount: per, accounts: n, total: per * n };
}

const api = { POINT_VALUE, validate, riskIncrease, totalRisk, legRisk };
if (typeof module !== 'undefined' && module.exports) module.exports = api;   // server + tests
else window.PTBSettings = api;                                               // settings page
