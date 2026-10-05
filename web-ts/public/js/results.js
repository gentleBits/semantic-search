// The right pane: the truth about the current set — how many, which filters, in what order.
import { html, useEffect, useRef, useState } from './lib.js';
import * as I from './icons.js';
import { chipText, factsLine, num, pageList, people, plural, seniorityLine } from './format.js';
import { act, close, get, openPerson, page, say, set, stop, toggle } from './store.js';
import { ChipTip, CriteriaPop, FilterPop, HistoryPop, RankPop, SortPop } from './popovers.js';
import { Slot } from './slots.js';

const stopHere = (fn) => (e) => { e.stopPropagation(); fn(e); };
const SENIORITY_SHADE = { intern: 'var(--sh-1)', junior: 'var(--sh-2)', mid: 'var(--sh-3)', senior: 'var(--sh-4)', lead: 'var(--sh-5)', principal: 'var(--sh-6)', manager: 'var(--sh-7)', director: 'var(--sh-8)', executive: 'var(--sh-9)' };
const SHORT = { junior: 'jr', director: 'dir', principal: 'princ', manager: 'mgr', executive: 'exec' };
const AVAIL_SHADE = { now: 'var(--sh-4)', '1w': 'var(--sh-3)', '2w': 'var(--sh-3)', '1m': 'var(--sh-3)', '3m': 'var(--sh-2)' };

// ---------------------------------------------------------------- header: count, chips, order

function Delta({ s }) {
  const d = s.snap.set.delta;
  const [old, setOld] = useState(false);
  useEffect(() => {
    setOld(false);
    const t = setTimeout(() => setOld(true), 9000);        // the count change shows for a moment, then fades
    return () => clearTimeout(t);
  }, [s.snap.set.id, s.deltaAt]);
  if (!d) return null;
  return html`<span class=${'delta' + (d[1] === 0 ? ' none-left' : '') + (old ? ' old' : '')} aria-label=${`from ${d[0]} to ${d[1]}`}>${num(d[0])} → ${num(d[1])}</span>`;
}

function Chip({ f, s, zero }) {
  const name = 'chip:' + f.id;
  const open = s.pop === name;
  const busy = !!s.snap.rank.running || s.busy;
  const timer = useRef(null);
  const enter = () => { clearTimeout(timer.current); timer.current = setTimeout(() => get().pop == null && set({ pop: name }), 350); };
  const leave = () => { clearTimeout(timer.current); timer.current = setTimeout(() => get().pop === name && set({ pop: null }), 180); };
  useEffect(() => () => clearTimeout(timer.current), []);
  const cls = 'chip' + (open ? ' open' : '') + (s.fresh.includes(f.id) ? ' new' : '') + (f.culprit ? ' culprit' : '');
  return html`<span class="chip-wrap" onMouseEnter=${enter} onMouseLeave=${leave}>
    <span class=${cls}>
      <button class="body" onClick=${stopHere(() => toggle(name))} aria-expanded=${open} aria-label=${chipText(f) + ': details'}>
        ${f.kind && html`<span class="k">${f.kind}</span>`}<span class="v">${f.value}</span>
      </button>
      <button class="x" disabled=${busy} aria-label=${'Remove ' + chipText(f)} title="Remove" onClick=${stopHere(() => act({ type: 'drop', targets: [f.id] }))}><${I.X} size=${11} /></button>
    </span>
    ${zero && html`<span class=${'without' + (f.culprit ? ' culprit' : '')}>without it: ${num(f.without)}</span>`}
    ${open && html`<${ChipTip} f=${f} s=${s} />`}
  </span>`;
}

function Badge({ s }) {
  const { ranking: r, rank, rankings, ranking_off: off } = s.snap;
  const run = rank.running;
  if (run) {
    return html`<span class="badge running" role="status">
      <${I.Arc} cls="spin" size=${12} width=${3} color="var(--rank)" />
      <span>Ranking</span><span class="crit">${run.criterion}</span>
      <span class="cov">${num(run.done)}/${num(run.total)}</span>
      <button class="stop" onClick=${stop}>Stop</button>
    </span>`;
  }
  if (r) {
    const part = r.judged < r.total;
    const several = rankings.length > 1;
    return html`<span class="badge">
      <${I.Star} />
      <span class="crit" title=${r.criterion}>${r.criterion}</span>
      <span class="cov">${num(r.judged)}/${num(r.total)}${part && html`<span class="mini"><i style=${`width:${Math.round((r.judged / Math.max(1, r.total)) * 100)}%`}></i></span>`}</span>
      ${several && html`<button class="b" aria-label="Switch ranking criterion" aria-expanded=${s.pop === 'criteria'} onClick=${stopHere(() => toggle('criteria'))}><${I.Down} size=${12} width=${2} /></button>`}
      <button class="b" aria-label="Remove ranking" title="Remove the ranking" disabled=${s.busy} onClick=${stopHere(() => act({ type: 'rank_off' }))}><${I.X} size=${11} /></button>
      ${s.pop === 'criteria' && html`<${CriteriaPop} s=${s} />`}
    </span>`;
  }
  const back = [...rankings].reverse().find((x) => x.covers > 0);
  if (off && back) {
    return html`<span class="badge off">
      <${I.StarLine} size=${12} color="var(--rank-ink)" />
      <span class="crit" title=${back.criterion}>${back.criterion}</span>
      <button class="on-btn" disabled=${s.busy} onClick=${() => act({ type: 'rank_on', judgment: back.judgment })}>Turn on</button>
      ${rankings.length > 1 && html`<button class="b" aria-label="Switch ranking criterion" onClick=${stopHere(() => toggle('criteria'))}><${I.Down} size=${12} width=${2} /></button>`}
      ${s.pop === 'criteria' && html`<${CriteriaPop} s=${s} />`}
    </span>`;
  }
  return null;
}

function RankControl({ s }) {
  const { set: st, rank, ranking: r } = s.snap;
  const c = s.config;
  if (rank.running) return html`<div class="order-right"><span class="hint wide">Order switches to ranking when done</span></div>`;
  if (st.count > rank.limit) {
    return html`<div class="order-right">
      ${!s.snap.too_many && html`<span class="hint wide">Narrow to ${rank.limit} or fewer to rank</span>`}
      <button class="rank-btn" disabled title=${`Ranking reads every ${c.document}: narrow to ${rank.limit} or fewer`}><${I.StarLine} size=${13} /><span>Rank…</span></button>
    </div>`;
  }
  const off = !c.assistant || s.busy || st.count === 0;
  if (r && r.judged < r.total && r.judged > 0) {
    const n = r.total - r.judged;
    return html`<div class="order-right">
      <button class="act-dark rank-btn dark" style="border:0;background:var(--accent);color:var(--on-accent)" disabled=${off} onClick=${() => say(`Rank the ${n} new.`)}><${I.Star} color="var(--rank-3)" />Rank <span class="long">the </span>${num(n)}<span class="long"> new</span></button>
    </div>`;
  }
  return html`<div class="order-right">
    <button class=${'rank-btn' + (s.pop === 'rank' ? ' on' : '')} disabled=${off} aria-expanded=${s.pop === 'rank'} aria-haspopup="dialog"
      title=${c.assistant ? '' : 'Ranking is done by the assistant: it needs its key'} onClick=${stopHere(() => toggle('rank'))} aria-label="Rank"><${I.StarLine} size=${13} /><span>Rank…</span></button>
    ${s.pop === 'rank' && html`<${RankPop} s=${s} />`}
  </div>`;
}

function SetHead({ s }) {
  const { snap, config: c } = s;
  const st = snap.set;
  const busy = !!snap.rank.running || s.busy;
  const zero = st.count === 0 && snap.filters.length > 0;
  return html`<section class="set-head">
    <div class="set-top">
      <div class="count" aria-live="polite">
        <span class=${'n' + (zero ? ' none-left' : '')}>${num(st.count)}</span>
        <span class="w">${plural(c, st.count)}</span>
        <${Delta} s=${s} />
      </div>
      <div class="set-tools">
        <button class="tool" disabled=${!st.prev || busy} onClick=${() => act({ type: 'undo' })}><${I.Undo} />Undo</button>
        <button class=${'tool' + (s.pop === 'history' ? ' on' : '')} aria-expanded=${s.pop === 'history'} aria-haspopup="dialog" onClick=${stopHere(() => toggle('history'))}>
          <${I.Clock} />History ${snap.history.length > 1 && html`<span class="n">${snap.history.length}</span>`}
        </button>
      </div>
    </div>
    <div class=${'chips' + (zero ? ' none-left' : '')}>
      ${snap.filters.map((f) => html`<${Chip} key=${f.id} f=${f} s=${s} zero=${zero} />`)}
      <span style="position:relative;display:flex">
        <button class=${'add-filter' + (s.pop === 'filter' ? ' on' : '')} disabled=${busy} aria-expanded=${s.pop === 'filter'} aria-haspopup="dialog"
          onClick=${stopHere(() => toggle('filter'))}><${I.Plus} size=${12} width=${2} />Filter</button>
        ${s.pop === 'filter' && html`<${FilterPop} s=${s} />`}
      </span>
      ${snap.filters.length > 0
        ? html`<button class="clear-all" disabled=${busy} onClick=${() => act({ type: 'clear' })}>Clear all</button>`
        : html`<span class="no-filters">No filters — the whole collection, ${snap.sort.keys.length ? 'sorted' : 'newest first'}.</span>`}
    </div>
    ${!zero && html`<div class="order">
      <div class="order-left">
        <button class=${'sort-btn' + (s.pop === 'sort' ? ' on' : '')} disabled=${busy || st.count === 0} aria-expanded=${s.pop === 'sort'} aria-haspopup="dialog"
          onClick=${stopHere(() => toggle('sort'))}>
          <${I.Sort} color="var(--ink-3)" /><span class="k">Sort</span><span class="v">${snap.sort.label}</span><span class="v short">${snap.sort.label.replace(/ [↓↑]/g, '')}</span><${I.Down} size=${12} width=${2} color="var(--ink-3)" />
        </button>
        ${s.pop === 'sort' && html`<${SortPop} s=${s} />`}
        ${(snap.ranking || snap.rank.running || (snap.ranking_off && snap.rankings.some((x) => x.covers > 0))) && html`<span class="sep"></span><${Badge} s=${s} />`}
      </div>
      <${RankControl} s=${s} />
    </div>`}
    ${s.pop === 'history' && html`<${HistoryPop} s=${s} />`}
  </section>`;
}

function CompactHead({ s }) {
  const { snap, config: c } = s;
  const r = snap.ranking;
  return html`<section class="set-head">
    <div class="count">
      <span class="n">${num(snap.set.count)}</span><span class="w">${plural(c, snap.set.count)}</span>
      ${r && html`<span class="badge"><${I.Star} size=${11} /><span class="crit">${r.criterion}</span><span class="cov">${num(r.judged)}/${num(r.total)}</span></span>`}
    </div>
    <div class="c-chips">
      ${snap.filters.map((f) => html`<span class="c-chip">${f.kind && html`<span class="k">${f.kind}</span>`}<span class="v">${f.value}</span></span>`)}
      ${!snap.filters.length && html`<span class="hint">everyone</span>`}
      <span class="ord">${snap.sort.label}</span>
    </div>
  </section>`;
}

function Banner({ s }) {
  const t = s.snap.too_many;
  const c = s.config;
  return html`<div class="banner" role="status">
    <div class="top">
      <span class="tile"><${I.Star} size=${13} /></span>
      <div class="words">
        <span class="t1">Too many to rank — narrow to ${t.limit} or fewer</span>
        <span class="t2">Ranking reads every ${c.document}. Pending: “${t.pending}”. The list below still works.</span>
      </div>
      <span class="of"><span><b>${num(t.count)}</b> / ${t.limit}</span>
        <button class="x" aria-label="Forget the pending ranking" title="Forget the pending ranking" onClick=${() => act({ type: 'forget_pending' })}><${I.X} size=${11} /></button>
      </span>
    </div>
    <div class="opts">
      ${t.suggestions.map((g) => html`<button class="sugg" disabled=${s.busy} onClick=${() => act({ type: 'filter', args: g.args })}>
        ${g.kind && html`<span class="k">${g.kind}</span>`}<span class="v">${g.value}</span><span class="n">${num(g.count)}</span>
      </button>`)}
      ${!t.suggestions.length && html`<span class="hint">No single filter gets there — combine two.</span>`}
    </div>
  </div>`;
}

// ---------------------------------------------------------------- overview

function Range({ q, fmt }) {
  if (q.p50 == null) return html`<div class="hint">unknown</div>`;
  const lo = Math.min(q.min != null ? q.min : q.p25, q.p25);
  const hi = Math.max(q.max != null ? q.max : q.p75, q.p75);
  const span = Math.max(1, hi - lo);
  const at = (v) => Math.max(0, Math.min(100, ((v - lo) / span) * 100));
  return html`<div class="range" title=${`from ${fmt(Math.round(lo))} to ${fmt(Math.round(hi))}`}>
    <div class="rail"></div>
    <div class="box" style=${`left:${at(q.p25)}%;width:${Math.max(2, at(q.p75) - at(q.p25))}%`}></div>
    <div class="mid" style=${`left:calc(${at(q.p50)}% - 1px)`}></div>
  </div>`;
}

function Overview({ s }) {
  const { snap, config: c } = s;
  const o = snap.overview;
  if (!o || !o.count) return null;
  const busy = !!snap.rank.running || s.busy;
  const add = (args) => () => act({ type: 'filter', args });
  const has = (kind, value) => snap.filters.some((f) => f.kind === kind && (value == null || String(f.value).toLowerCase().includes(String(value).toLowerCase())));
  const sym = o.rate.symbol;
  const top = [...o.seniority].sort((a, b) => b.count - a.count)[0];
  const sum = [
    top && `${top.name} ${num(top.count)}`,
    o.years.p50 != null && `years p50 ${o.years.p50}`,
    o.rate.p50 != null && `rate p50 ${sym}${o.rate.p50}`,
    ...o.skills.slice(0, 2).map((k) => `${k.name} ${num(k.count)}`),
    o.remote_ok ? `remote-ok ${num(o.remote_ok)}` : null,
  ].filter(Boolean).join(' · ');
  const total = o.seniority.reduce((n, x) => n + x.count, 0) || 1;
  const most = Math.max(1, ...o.availability.map((a) => a.count));
  const estimated = o.rate.estimated;
  return html`<details class="overview" open=${s.overview} onToggle=${(e) => e.target.open !== get().overview && set({ overview: e.target.open })}>
    <summary>
      <${I.Right} size=${10} />
      <span class="ttl">Overview</span>
      ${s.overview ? html`<span style="color:var(--ink-3)">click any bar or chip to filter</span>` : html`<span class="sum">${sum}</span>`}
      ${o.by_meaning
        ? (s.overview
          ? html`<span class="split"><span><b>${num(o.direct)}</b> direct matches</span><span><b>${num(o.by_meaning)}</b> by meaning</span></span>`
          : html`<span class="note">${num(o.by_meaning)} found by meaning</span>`)
        : estimated ? html`<span class="note"><${I.Info} size=${12} />rate estimated for ${num(estimated)} of ${num(o.count)}</span>` : null}
    </summary>
    <div class="ov-body">
      <div class="ov-grid">
        <div class="ov-col">
          <div class="ov-h"><span class="l">Seniority</span></div>
          <div class="stack">${o.seniority.map((x) => html`<button aria-label=${`${x.name} ${x.count}`} title=${`${x.name} ${x.count}`} disabled=${busy || has('seniority', x.name)}
            style=${`width:${(x.count / total) * 100}%;background:${SENIORITY_SHADE[x.name] || 'var(--sh-3)'}`} onClick=${add({ seniority: [x.name] })}></button>`)}</div>
          <div class="legend">${o.seniority.map((x) => html`<button disabled=${busy || has('seniority', x.name)} onClick=${add({ seniority: [x.name] })} title=${x.name}>${SHORT[x.name] || x.name} <b>${num(x.count)}</b></button>`)}</div>
        </div>
        <div class="ov-col">
          <div class="ov-h"><span class="l">Years</span>${o.years.unknown ? html`<span class="r">+${num(o.years.unknown)} unknown</span>` : null}</div>
          <${Range} q=${o.years} fmt=${(v) => v + 'y'} />
          <div class="q">${['p25', 'p50', 'p75'].map((k) => html`<button disabled=${busy || o.years[k] == null || has('years')} title=${`at least ${o.years[k]} years`}
            onClick=${add({ min_years: o.years[k] })}>${k} <b>${o.years[k] ?? '—'}</b></button>`)}</div>
        </div>
        <div class="ov-col">
          <div class="ov-h"><span class="l">Rate ${sym}/h</span>${estimated ? html`<span class="r">${num(estimated)} estimated</span>` : null}</div>
          <${Range} q=${o.rate} fmt=${(v) => sym + v} />
          <div class="q">${['p25', 'p50', 'p75'].map((k) => html`<button disabled=${busy || o.rate[k] == null || has('rate')} title=${`at most ${sym}${o.rate[k]} an hour`}
            onClick=${add({ rate_max: o.rate[k] })}>${k} <b>${o.rate[k] != null ? sym + o.rate[k] : '—'}</b></button>`)}</div>
        </div>
        <div class="ov-col">
          <div class="ov-h"><span class="l">Available</span></div>
          ${o.availability.length ? html`<div class="bars">${o.availability.map((a) => html`<button class="bar-row" disabled=${busy || a.code === 'other' || has('available', a.label)}
            onClick=${add({ availability: [a.code] })}>${a.label}<span><i style=${`width:${Math.max(4, (a.count / most) * 100)}%;background:${AVAIL_SHADE[a.code] || 'var(--sh-2)'}`}></i></span><b>${num(a.count)}</b></button>`)}</div>`
            : html`<span class="hint">nobody here states one</span>`}
        </div>
      </div>
      <div class="ov-row">
        <div class="ov-tags"><span class="l">Skills</span>
          <div class="tags">${o.skills.map((k) => html`<button class="facet" disabled=${busy || has('skill', k.name)} onClick=${add({ skill: [k.name] })}>${k.name}<span class="n">${num(k.count)}</span></button>`)}</div>
        </div>
        <div class="ov-tags"><span class="l">Where</span>
          <div class="tags">
            ${o.remote_ok > 0 && html`<button class="facet" disabled=${busy || snap.filters.some((f) => f.value === 'remote')} onClick=${add({ remote: true })}><${I.Globe} size=${11} color="var(--ink-2)" />remote-ok<span class="n">${num(o.remote_ok)}</span></button>`}
            ${o.cities.map((k) => html`<button class="facet" disabled=${busy || has('location', k.name)} onClick=${add({ location: k.name })}>${k.name}<span class="n">${num(k.count)}</span></button>`)}
            ${!o.remote_ok && !o.cities.length && html`<span class="hint">nobody here states a place</span>`}
          </div>
        </div>
      </div>
    </div>
  </details>`;
}

// ---------------------------------------------------------------- the list

function ScoreCell({ i }) {
  if (i.score != null) {
    return html`<span class="score"><${I.Star} size=${10} />${i.score}</span>
      <span class="score-bar"><i style=${`width:${i.score}%`}></i></span>`;
  }
  if (i.pending) return html`<span class="scoring pulse">scoring…</span>`;
  return html`<span class="unranked">not ranked yet</span>`;
}

function Row({ i, s, scored, compact }) {
  const open = s.expanded === i.id && !compact;
  const sel = s.detail && s.detail.id === i.id;
  const phone = typeof window !== 'undefined' && window.matchMedia('(max-width: 760px)').matches;
  if (i.removed) return html`<div class="item"><div class="row"><span class="c-rank">${i.rank}</span><span class="who"><span class="l2">removed from the index</span></span></div></div>`;
  const second = compact || phone ? factsLine(i) : (i.note || i.skills || '');
  const click = () => (compact || phone ? openPerson(i.id) : set({ expanded: open ? null : i.id }));
  return html`<div class=${'item' + (open ? ' open' : '') + (sel && compact ? ' sel' : '') + (s.hit === i.id ? ' hit' : '')} aria-current=${sel && compact ? 'true' : null}>
    <button class="row" onClick=${click} onDblClick=${() => openPerson(i.id)} aria-expanded=${compact ? null : open}>
      <span class=${'c-rank' + (s.snap.set.count > 999 && !compact ? ' wide' : '')}>${num(i.rank)}</span>
      <span class="who">
        <span class="l1">
          <span class="title" title=${i.title}>${i.title}</span>
          ${i.by_meaning && !compact && html`<span class="tag meaning" title="Found by meaning, not by exact words">≈ by meaning</span>`}
          ${i.versions > 0 && !compact && html`<span class="tag">+${i.versions} older version${i.versions > 1 ? 's' : ''}</span>`}
        </span>
        <span class=${'l2' + (i.note && !compact && !phone ? ' note' : '')}>${second}</span>
      </span>
      ${!compact && html`
        <span class=${'c-yrs' + (i.years == null ? ' none' : '')}>${i.years_label}</span>
        <span class=${'c-rate' + (i.rate == null ? ' none' : '')} title=${i.rate_estimated ? 'estimated rate' : null}>${i.rate_label}</span>
        <span class="c-loc"><span class=${'place' + (i.location ? '' : ' none')}>${i.location || '—'}</span>${i.remote && html`<${I.Globe} size=${12} color="var(--ink-3)" label="remote ok" />`}</span>
        <span class="c-av">${i.avail === 'now' ? html`<span class="now">now</span>` : html`<span class=${'later' + (i.avail ? '' : ' none')} title=${i.avail === 'other' ? i.avail_label : null}>${i.avail === 'other' ? 'other' : (i.avail_label || '—')}</span>`}</span>`}
      ${scored && html`<span class=${compact ? '' : 'c-score'} style=${compact ? 'display:flex;flex-shrink:0' : ''}>
        ${compact ? (i.score != null ? html`<span class="score"><${I.Star} size=${10} />${i.score}</span>` : html`<span class="unranked">not ranked</span>`) : html`<${ScoreCell} i=${i} />`}
      </span>`}
    </button>
    ${open && html`<div class="card">
      <div class="facts">
        <div class="kv"><span class="k">Used in jobs</span><span class="tags">${i.used.length ? i.used.map((k) => html`<span class="used">${k}</span>`) : html`<span class="hint" style="line-height:22px">none found</span>`}</span></div>
        ${i.listed.length > 0 && html`<div class="kv"><span class="k">Listed only</span><span class="tags">${i.listed.map((k) => html`<span class="listed">${k}</span>`)}</span></div>`}
        ${i.did && html`<div class="kv" style="margin-top:2px"><span class="k">Did</span><span class="quote">“${i.did}”</span></div>`}
      </div>
      <div class="side">
        <button class="open-resume" onClick=${() => openPerson(i.id)}><${I.Doc} size=${13} />Open ${s.config.document}</button>
        <span class="facts-line">${seniorityLine(i)}</span>
      </div>
    </div>`}
  </div>`;
}

function Columns({ s, scored }) {
  const { snap } = s;
  const keys = snap.sort.keys;
  const mark = (k) => { const hit = keys.find((x) => x.key === k); return hit ? (hit.dir === 'desc' ? ' ↓' : ' ↑') : ''; };
  const busy = !!snap.rank.running || s.busy || snap.set.count === 0;
  const by = (k, d) => () => {
    const hit = keys.find((x) => x.key === k);
    const dir = hit ? (hit.dir === 'asc' ? 'desc' : 'asc') : d;
    act({ type: 'sort', by: `${k}:${dir}` });
  };
  const col = (cls, k, d, label) => html`<button class=${cls + (mark(k) ? ' by' : '')} disabled=${busy} title=${'Sort by ' + label.toLowerCase()} onClick=${by(k, d)}>${label}${mark(k)}</button>`;
  return html`<div class=${'cols' + (scored ? ' ranked' : '')}>
    <span class=${'c-rank' + (snap.set.count > 999 ? ' wide' : '')}>#</span>
    <span class="grow">${cap(s.config.noun)}</span>
    ${col('c-yrs', 'years', 'desc', 'Yrs')}
    ${col('c-rate', 'rate', 'asc', 'Rate/h')}
    <span class="c-loc">Location</span>
    <span class="c-av">Avail</span>
    ${scored && (snap.ranking ? col('c-score', 'judgment', 'desc', 'Score') : html`<span class="c-score">Score</span>`)}
  </div>`;
}
const cap = (w) => w[0].toUpperCase() + w.slice(1);

function List({ s, compact }) {
  const { snap } = s;
  const r = snap.ranking;
  const run = snap.rank.running;
  const scored = !!r || (!!run && run.set === snap.set.id);
  const box = useRef(null);
  useEffect(() => { if (box.current) box.current.scrollTop = 0; }, [snap.set.id, snap.set.page, !!snap.too_many]);
  useEffect(() => {
    if (!s.hit || !box.current) return;
    const el = box.current.querySelector('.item.hit');
    if (el) el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }, [s.hit, snap.set.page]);
  const rows = [];
  const byRank = r && r.sorted && r.judged < r.total && r.judged > 0;
  const n = r ? r.total - r.judged : 0;
  snap.items.forEach((i) => {
    if (byRank && i.rank === r.judged + 1 && !compact) {
      rows.push(html`<div class="rest" key="rest">
        <span class="c-rank"></span>
        <span class="words"><b>${num(n)} not ranked yet</b><span>· they sort after ranked ${s.config.nouns}; earlier scores never change</span></span>
        ${n <= snap.rank.limit && snap.set.count <= snap.rank.limit && html`<button disabled=${s.busy || !s.config.assistant} onClick=${() => say(`Rank the ${n} new.`)}><${I.Star} size=${11} />Rank them</button>`}
      </div>`);
    }
    rows.push(html`<${Row} key=${i.id || i.rank} i=${i} s=${s} scored=${scored} compact=${compact} />`);
  });
  return html`<div class=${'list' + (scored ? ' ranked' : '') + (snap.set.everyone ? ' first-open' : '')} ref=${box}>${rows}</div>`;
}

function Foot({ s, compact }) {
  const { snap } = s;
  const st = snap.set;
  const [bad, setBad] = useState(false);
  const est = snap.overview && snap.overview.rate && snap.overview.rate.estimated;
  const jump = (e) => {
    e.preventDefault();
    const el = e.target.elements.n;
    const n = parseInt(el.value, 10);
    if (!n || n < 1 || n > st.pages) { setBad(true); setTimeout(() => setBad(false), 900); return; }
    el.value = '';
    el.blur();
    page(n);
  };
  if (compact) {
    return html`<footer class="foot">
      <span class="where">${num(st.start)}–${num(st.end)} of ${num(st.count)}</span>
      <span class="mini-pages">
        <button aria-label="Previous page" disabled=${st.page <= 1} onClick=${() => page(st.page - 1)}>‹</button>
        ${st.page} / ${num(st.pages)}
        <button aria-label="Next page" disabled=${st.page >= st.pages} onClick=${() => page(st.page + 1)}>›</button>
      </span>
    </footer>`;
  }
  return html`<footer class="foot">
    <span class="where">${num(st.start)}–${num(st.end)} of ${num(st.count)}${est ? ` · rate estimated for ${num(est)}` : ''}${st.index_changed ? ' · index updated since this list was made' : ''}</span>
    <nav class="pages" aria-label="Pages">
      <button class="pg" aria-label="Previous page" disabled=${st.page <= 1} onClick=${() => page(st.page - 1)}><${I.Prev} /></button>
      <span class="m-where">${num(st.start)}–${num(st.end)} <span>of ${num(st.count)} · page ${st.page}/${num(st.pages)}</span></span>
      ${pageList(st.page, st.pages).map((p) => (p == null
        ? html`<span class="gap">…</span>`
        : html`<button class=${'pg num' + (p === st.page ? ' cur' : '')} aria-current=${p === st.page ? 'page' : null} onClick=${() => p !== st.page && page(p)}>${num(p)}</button>`))}
      <button class="pg next" aria-label="Next page" disabled=${st.page >= st.pages} onClick=${() => page(st.page + 1)}><${I.Next} /></button>
    </nav>
    <form class="goto" onSubmit=${jump}>
      <label for="goto">Go to page</label>
      <input id="goto" name="n" type="text" inputmode="numeric" autocomplete="off" class=${bad ? 'bad' : ''} aria-label="Go to page" />
    </form>
  </footer>`;
}

function Zero({ s }) {
  const z = s.snap.zero;
  const n = z.filters;
  const words = ['no', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight'];
  const culprit = s.snap.filters.find((f) => f.id === z.culprit);
  const busy = s.busy;
  return html`<div class="zero">
    <div class="in">
      <div>
        <h2>No one passes ${n === 1 ? 'this filter' : `all ${words[n] || n} filters`}.</h2>
        <p>${culprit && html`<span class="mono">${chipText(culprit)}</span> emptied the set. `}${z.options.length ? `Remove one to get ${s.config.nouns} back:` : 'No single filter brings anyone back — clear them and start again.'}</p>
      </div>
      <div class="choices">
        ${z.options.map((o, k) => html`<button class=${'choice' + (k === 0 ? ' main' : '')} disabled=${busy} onClick=${() => act({ type: 'drop', targets: [o.id] })}>
          <span>Remove ${chipText(o)}</span><span class="n">→ ${num(o.without)}${k === 0 ? ' ' + plural(s.config, o.without) : ''}</span></button>`)}
        ${!z.options.length && html`<button class="choice main" disabled=${busy} onClick=${() => act({ type: 'clear' })}><span>Clear all filters</span><span class="n">→ ${num(s.config.count)}</span></button>`}
      </div>
      <span class="last">Or step back in history — every step is kept.</span>
    </div>
  </div>`;
}

function Notice({ n }) {
  return html`<div class="alert notice" role="alert"><${I.Warn} color="var(--red)" /><span>${n.text}</span>
    ${n.fix && html`<button class="fix" title=${n.fix.hint} onClick=${() => { set({ notice: null }); act(n.fix.action); }}>${n.fix.label}</button>`}
    <button class="x" style="margin-left:${n.fix ? '0' : 'auto'};color:var(--red-ink)" aria-label="Dismiss" onClick=${() => set({ notice: null })}><${I.X} size=${11} /></button>
  </div>`;
}

export function Results({ s, compact }) {
  const { snap } = s;
  const run = snap.rank.running;
  const zero = snap.set.count === 0 && snap.zero;
  return html`<main class=${'results' + (compact ? ' compact' : '')} aria-label="Results" onClick=${close}>
    ${run && html`<div class="rank-rail" style=${`width:${Math.round((run.done / Math.max(1, run.total)) * 100)}%`}></div>`}
    <${Slot} name="results-top" s=${s} />
    ${compact ? html`<${CompactHead} s=${s} />` : html`<${SetHead} s=${s} />`}
    ${!compact && snap.too_many && html`<${Banner} s=${s} />`}
    ${zero ? html`<${Zero} s=${s} />` : html`
      ${!compact && html`<${Overview} s=${s} />`}
      ${!compact && html`<${Columns} s=${s} scored=${!!snap.ranking || (!!run && run.set === snap.set.id)} />`}
      <${List} s=${s} compact=${compact} />
      <${Foot} s=${s} compact=${compact} />`}
    ${s.notice && html`<${Notice} n=${s.notice} />`}
  </main>`;
}

export { people };
