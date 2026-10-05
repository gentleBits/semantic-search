// What opens over the right pane: a chip's facts, the filter form, the sort keys, the history, the ranking question.
import { html, useEffect, useRef, useState } from './lib.js';
import * as I from './icons.js';
import { chipText, num } from './format.js';
import { act, say, set } from './store.js';

const hold = (e) => e.stopPropagation();

function useFocus() {
  const ref = useRef(null);
  useEffect(() => { if (ref.current) ref.current.focus(); }, []);
  return ref;
}

// ---------------------------------------------------------------- a chip's facts

export function ChipTip({ f, s }) {
  const c = s.config;
  const u = f.unknown;
  const busy = s.busy || !!s.snap.rank.running;
  const terms = (f.terms || []).filter((t) => t.expanded.length);
  return html`<div class="pop dark tip" role="tooltip" onClick=${hold}>
    ${f.said && html`<div class="said"><h4>Your words</h4><span class="quoted">“${f.said}”</span></div>`}
    <div class="two">
      <div class="box"><span class="n">${num(f.alone)}</span><span class="w">match it alone</span></div>
      <div class="box"><span class="n">${num(f.without)}</span><span class="w">without it</span></div>
    </div>
    ${terms.map((t) => html`<span class="more">${t.kind} <em>${t.name}</em> includes ${t.expanded.join(', ')}${t.more ? ` +${t.more}` : ''}</span>`)}
    ${f.like && html`<span class="more">${f.like.requirements} requirements read${f.like.resolved.length ? html` · understood <em>${f.like.resolved.slice(0, 8).join(', ')}</em>` : ''}. Best match first.</span>`}
    ${(f.unresolved || []).length > 0 && html`<span class="more">No known skill or topic matched <em>“${f.unresolved.join(' ')}”</em> — searched as free text.</span>`}
    ${f.by_meaning > 0 && html`<span class="more"><em>${num(f.by_meaning)}</em> of the ${num(s.snap.set.count)} found by meaning, not by exact words.</span>`}
    ${f.estimated > 0 && html`<span class="more">Includes ${num(f.estimated)} estimated rate${f.estimated === 1 ? '' : 's'}.</span>`}
    ${u && (u.included
      ? html`<span class="more">Includes ${c.nouns} with no ${u.what}.</span>
          <button class="toggle" disabled=${busy} onClick=${() => act({ type: 'unknown', id: f.id, include: false })}>Leave them out</button>`
      : html`<span class="more">${u.count ? html`<em>+${num(u.count)}</em> with no ${u.what} are left out.` : `${cap(c.nouns)} with no ${u.what} are left out.`}</span>
          ${u.count > 0 && html`<button class="toggle" disabled=${busy} onClick=${() => act({ type: 'unknown', id: f.id, include: true })}>Include them</button>`}`)}
  </div>`;
}
const cap = (w) => w[0].toUpperCase() + w.slice(1);

// ---------------------------------------------------------------- + Filter

const KINDS = [
  ['topic', 'Topic', 'an activity or a domain'],
  ['skill', 'Skill', 'a technology or a tool'],
  ['text', 'In their words', 'a sentence, matched by meaning'],
  ['like', 'Like a job description', 'paste it; best match first'],
  ['years', 'Years', 'at least'],
  ['rate', 'Rate', 'at most, per hour'],
  ['seniority', 'Seniority', 'junior … director'],
  ['availability', 'Available', 'now, in 2 weeks, …'],
  ['location', 'Location', 'a city or a country'],
  ['remote', 'Remote', 'open to remote work'],
];

function FilterForm({ kind, s, back }) {
  const c = s.config;
  const first = useFocus();
  const [v, setV] = useState('');
  const [picked, setPicked] = useState([]);
  const [anyOf, setAnyOf] = useState(false);
  const [used, setUsed] = useState(false);
  const [unknown, setUnknown] = useState(false);
  const [err, setErr] = useState(null);
  const pick = (x) => setPicked((p) => (p.includes(x) ? p.filter((y) => y !== x) : [...p, x]));
  const list = () => v.split(',').map((x) => x.trim()).filter(Boolean);
  const submit = (e) => {
    e.preventDefault();
    let action = null;
    if (kind === 'topic' && list().length) action = { type: 'filter', args: { topic: list(), ...(anyOf && list().length > 1 ? { mode: 'any' } : {}) } };
    if (kind === 'skill' && list().length) action = { type: 'filter', args: { skill: list(), ...(anyOf && list().length > 1 ? { mode: 'any' } : {}), ...(used ? { level_used: true } : {}) } };
    if (kind === 'text' && v.trim()) action = { type: 'filter', args: { text: v.trim() } };
    if (kind === 'like' && v.trim().length > 40) action = { type: 'jd', text: v };
    if (kind === 'years' && v.trim()) action = { type: 'filter', args: { min_years: v.trim(), include_unknown: unknown } };
    if (kind === 'rate' && v.trim()) action = { type: 'filter', args: { rate_max: v.trim(), include_unknown: unknown } };
    if (kind === 'location' && v.trim()) action = { type: 'filter', args: { location: v.trim() } };
    if (kind === 'seniority' && picked.length) action = { type: 'filter', args: { seniority: picked } };
    if (kind === 'availability' && picked.length) action = { type: 'filter', args: { availability: picked } };
    if (!action) { setErr(kind === 'like' ? 'Paste the job description: a few lines at least.' : 'Say what to filter by.'); return; }
    act(action);
  };
  const title = KINDS.find((k) => k[0] === kind)[1];
  let body = null;
  if (kind === 'topic' || kind === 'skill') {
    body = html`<div class="field">
      <label for="f-v">${title}</label>
      <input id="f-v" class="input" ref=${first} value=${v} onInput=${(e) => setV(e.target.value)} autocomplete="off"
        placeholder=${kind === 'topic' ? 'data pipelines, payments, …' : 'elixir, kubernetes, …'} />
      <span class="help">In your own words; several with commas. The chip shows how it was understood.</span>
    </div>
    ${list().length > 1 && html`<label class="switch"><input type="checkbox" checked=${anyOf} onChange=${(e) => setAnyOf(e.target.checked)} />any of them is enough (one chip)</label>`}
    ${kind === 'skill' && html`<label class="switch"><input type="checkbox" checked=${used} onChange=${(e) => setUsed(e.target.checked)} />used in a job, not only listed</label>`}`;
  } else if (kind === 'text') {
    body = html`<div class="field">
      <label for="f-v">In their words</label>
      <textarea id="f-v" class="input" rows="3" ref=${first} value=${v} onInput=${(e) => setV(e.target.value)} placeholder="moved data between systems at night"></textarea>
      <span class="help">Found by meaning as well as by the words. Takes about half a second.</span>
    </div>`;
  } else if (kind === 'like') {
    body = html`<div class="field">
      <label for="f-v">Job description</label>
      <textarea id="f-v" class="input" rows="7" ref=${first} value=${v} onInput=${(e) => setV(e.target.value)} placeholder="Paste the text. One requirement per line works best."></textarea>
      <span class="help">${cap(c.nouns)} who cover enough of its requirements, best match first. For the years or the place it asks for, add a filter, or give it to the assistant.</span>
    </div>`;
  } else if (kind === 'years' || kind === 'rate') {
    body = html`<div class="field">
      <label for="f-v">${kind === 'years' ? 'At least' : 'At most'}</label>
      <div class="with-unit">
        <input id="f-v" class="input" ref=${first} inputmode="decimal" value=${v} onInput=${(e) => setV(e.target.value)} autocomplete="off" placeholder=${kind === 'years' ? '5' : '80'} />
        <span>${kind === 'years' ? 'years' : `${c.symbol} per hour`}</span>
      </div>
    </div>
    <label class="switch"><input type="checkbox" checked=${unknown} onChange=${(e) => setUnknown(e.target.checked)} />keep ${c.nouns} whose ${kind} is unknown</label>`;
  } else if (kind === 'location') {
    body = html`<div class="field">
      <label for="f-v">City or country</label>
      <input id="f-v" class="input" ref=${first} value=${v} onInput=${(e) => setV(e.target.value)} autocomplete="off" placeholder="Berlin" />
    </div>`;
  } else {
    const options = kind === 'seniority' ? c.seniorities.map((x) => [x, x]) : c.availabilities.map((a) => [a.code, a.label]);
    body = html`<div class="field">
      <span class="lab">${title} — any of</span>
      <div class="checks">${options.map(([code, label], k) => html`<button type="button" ref=${k === 0 ? first : null} class=${'check' + (picked.includes(code) ? ' on' : '')}
        aria-pressed=${picked.includes(code)} onClick=${() => pick(code)}>${label}</button>`)}</div>
      ${kind === 'availability' && html`<span class="help">Only ${c.nouns} who state when they can start.</span>`}
    </div>`;
  }
  return html`<form onSubmit=${submit}>
    <button type="button" class="back" onClick=${back}><${I.Prev} size=${12} />All filters</button>
    ${body}
    ${err && html`<span class="form-error" role="alert">${err}</span>`}
    <div class="pop-actions"><button class="btn primary" type="submit">Add filter</button></div>
  </form>`;
}

export function FilterPop({ s }) {
  const [kind, setKind] = useState(null);
  const active = s.snap.filters;
  const has = (k) => active.some((f) => (k === 'remote' ? f.value === 'remote' : f.kind === k));
  const choose = (k) => (k === 'remote' ? act({ type: 'filter', args: { remote: true } }) : setKind(k));
  return html`<div class="pop filter-pop" role="dialog" aria-label="Add a filter" onClick=${hold}>
    ${kind ? html`<${FilterForm} kind=${kind} s=${s} back=${() => setKind(null)} />`
      : html`<div class="kinds">${KINDS.map(([k, name, help]) => html`<button class="kind" disabled=${k === 'remote' && has('remote')} onClick=${() => choose(k)}
          style=${k === 'remote' && has('remote') ? 'opacity:.45' : ''}><b>${name}</b><span>${help}</span></button>`)}</div>`}
  </div>`;
}

// ---------------------------------------------------------------- sort

export function SortPop({ s }) {
  const { snap, config: c } = s;
  const keys = snap.sort.keys;
  const ranked = !!snap.ranking || snap.rankings.some((r) => r.covers > 0);
  const apply = (next) => act({ type: 'sort', by: next.length ? next.map((k) => `${k.key}:${k.dir}`).join(',') : 'relevance' }, { keep: true });
  const toggleKey = (k) => {
    const at = keys.findIndex((x) => x.key === k.key);
    apply(at === -1 ? [...keys, { key: k.key, dir: k.dir }] : keys.filter((x) => x.key !== k.key));
  };
  const flip = (k) => apply(keys.map((x) => (x.key === k.key ? { ...x, dir: x.dir === 'asc' ? 'desc' : 'asc' } : x)));
  return html`<div class="pop menu sort-pop" role="dialog" aria-label="Sort" onClick=${hold}>
    <div class="mh"><h4>Sort by, in this order</h4></div>
    ${c.sort_keys.map((k) => {
      const at = keys.findIndex((x) => x.key === k.key);
      const on = at !== -1;
      const off = k.key === 'judgment' && !ranked;
      const dir = on ? keys[at].dir : k.dir;
      return html`<div class="sort-key">
        <button class="pick" disabled=${off} aria-pressed=${on} onClick=${() => toggleKey(k)} title=${off ? 'Nothing is ranked yet' : ''}>
          <span class=${'ord' + (on ? '' : ' no')}>${on ? at + 1 : '·'}</span>${k.name}
        </button>
        ${on && html`<button class="dir" aria-label=${`${k.name}: ${dir === 'asc' ? 'lowest first' : 'highest first'} — flip`} title=${dir === 'asc' ? 'lowest first' : 'highest first'}
          onClick=${() => flip(k)}>${dir === 'asc' ? '↑' : '↓'}</button>`}
      </div>`;
    })}
    <hr />
    <button class="mi" disabled=${!keys.length} onClick=${() => act({ type: 'sort', by: 'relevance' })}>${snap.sort.base_label}<span class="r">base order</span></button>
    <div class="key-note" style="padding:6px 10px 4px;font-size:11px;color:var(--ink-3)">${cap(c.nouns)} without a value go last.</div>
  </div>`;
}

// ---------------------------------------------------------------- history

export function HistoryPop({ s }) {
  const steps = s.snap.history;
  const busy = s.busy || !!s.snap.rank.running;
  const box = useRef(null);
  useEffect(() => { const el = box.current && box.current.querySelector('.cur'); if (el) el.scrollIntoView({ block: 'nearest' }); }, []);
  return html`<div class="pop dark history" role="dialog" aria-label="History" onClick=${hold}>
    <div class="hh"><h4>History · this session</h4><span>click a step to jump</span></div>
    <div class="steps" ref=${box}>
      ${steps.map((h) => html`<button class=${'hstep' + (h.current ? ' cur' : '')} disabled=${busy && !h.current} aria-current=${h.current ? 'step' : null}
        onClick=${() => (h.current ? set({ pop: null }) : act({ type: 'use', set: h.id }))}>
        <span class="i">${h.n}</span>
        <span class="c">${num(h.count)}</span>
        <span class="o" title=${h.label}>${h.sign && h.sign !== '?' ? h.sign + ' ' : h.sign === '?' ? '+ ' : ''}${h.label}</span>
        <span class="d"></span>
      </button>`)}
    </div>
  </div>`;
}

// ---------------------------------------------------------------- ranking

const EXAMPLES = ['talented, decent price', 'leadership', 'hands-on, available soon'];

export function RankPop({ s }) {
  const { snap, config: c } = s;
  const last = [...snap.rankings].reverse()[0];
  const first = useFocus();
  const [v, setV] = useState(snap.rank.pending || '');
  const go = (e) => {
    e.preventDefault();
    const t = v.trim();
    if (!t) return;
    say(`Rank them for: ${t}`);
  };
  return html`<form class="pop rank-pop" role="dialog" aria-label="Rank" onClick=${hold} onSubmit=${go}>
    <div class="field">
      <label for="crit">Rank ${num(snap.set.count)} ${snap.set.count === 1 ? c.noun : c.nouns} for…</label>
      <input id="crit" class="input" ref=${first} value=${v} onInput=${(e) => setV(e.target.value)} autocomplete="off" placeholder="in your own words" />
      <span class="help">The assistant reads every ${c.document} and scores it 0–100 with a note. About ${Math.max(5, Math.ceil(snap.set.count / 25) * 8)}–${Math.max(10, Math.ceil(snap.set.count / 25) * 20)} s.</span>
    </div>
    <div class="eg">${[...(last ? [last.criterion] : []), ...EXAMPLES.filter((x) => !last || x !== last.criterion)].slice(0, 3)
      .map((x) => html`<button type="button" onClick=${() => setV(x)}>${x}</button>`)}</div>
    <div class="pop-actions"><button class="btn primary" type="submit" disabled=${!v.trim()}>Rank</button></div>
  </form>`;
}

export function CriteriaPop({ s }) {
  const { snap } = s;
  const cur = snap.ranking && snap.ranking.judgment;
  return html`<div class="pop menu crit-pop" role="dialog" aria-label="Rankings" onClick=${hold} style="color:var(--ink)">
    <div class="mh"><h4>Rankings of this search</h4></div>
    ${snap.rankings.map((r) => html`<button class="mi" disabled=${s.busy || !r.covers} onClick=${() => (r.judgment === cur ? set({ pop: null }) : act({ type: 'rank_on', judgment: r.judgment }))}>
      <${I.Star} size=${11} color=${r.judgment === cur ? 'var(--rank)' : 'var(--faint)'} />${r.criterion}<span class="r">${num(r.covers)}/${num(snap.set.count)}</span>
    </button>`)}
  </div>`;
}

export { chipText };
