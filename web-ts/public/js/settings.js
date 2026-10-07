// Settings: which model the assistant runs on and which one ranks, picked from the models the server offers (the
// providers it has a key for, and OpenRouter's free ones). No key is shown or typed here. The pick is the logged-in
// person's own, saved at once (no Save button) and used from the next message; a member picks among the models with a
// known price and sees the day's allowance. Light/dark and Log out (with the email and phone check: "Forget this
// browser") are here too, so the header keeps to the search.
import { html, useEffect, useRef, useState } from './lib.js';
import * as api from './api.js';
import { num } from './format.js';
import { logout, reloadConfig, set } from './store.js';

const hold = (e) => e.stopPropagation();

// Light or dark: theme.js chose at load; a click is a choice and is kept. It follows <html data-theme>, so the phone's
// popover and the desktop's agree.
function Appearance() {
  const root = document.documentElement;
  const [theme, setTheme] = useState(() => root.dataset.theme);
  useEffect(() => {
    const o = new MutationObserver(() => setTheme(root.dataset.theme));
    o.observe(root, { attributes: true, attributeFilter: ['data-theme'] });
    return () => o.disconnect();
  }, []);
  const choose = (t) => {
    try { localStorage.setItem('gb-theme', t); } catch (e) { /* private window: this page only */ }
    root.dataset.theme = t;
  };
  return html`<div class="field appearance">
    <span class="lab">Appearance</span>
    <div class="seg" role="group" aria-label="Appearance">${[['light', 'Light'], ['dark', 'Dark']].map(([t, name]) =>
      html`<button type="button" class=${t === theme ? 'on' : ''} aria-pressed=${t === theme} onClick=${() => choose(t)}>${name}</button>`)}</div>
  </div>`;
}

const Account = ({ me }) => me && me.user && html`<div class="me"><span title=${me.user}>${me.user}</span>${me.signup
  ? html`<button type="button" class="link" onClick=${logout} title="Your email and phone will be asked for again">Forget this browser</button>`
  : html`<button type="button" class="link" onClick=${logout}>Log out</button>`}</div>`;
const SHOW = 40;

const money = (x) => (x == null ? '' : x === 0 ? 'free' : x < 0.01 ? `$${x.toFixed(4)}` : x < 1 ? `$${x.toFixed(2)}` : `$${x.toFixed(x >= 10 ? 0 : 1)}`);
const ctx = (n) => (!n ? '' : n >= 1000000 ? `${(n / 1000000).toFixed(n % 1000000 ? 1 : 0)}M` : `${Math.round(n / 1000)}k`);

// A level the model does not take shows as the one it runs at: the next one up, else the nearest below (as pi clamps).
const ORDER = ['off', 'minimal', 'low', 'medium', 'high'];
function clampLevel(lv, l) {
  if (lv.includes(l)) return l;
  const i = Math.max(0, ORDER.indexOf(l));
  return ORDER.slice(i).find((x) => lv.includes(x)) || ORDER.slice(0, i).reverse().find((x) => lv.includes(x)) || lv[0];
}

// The provider's list, filtered as one types, in its own order: a pick is marked where it stands, nothing moves. Only a
// model of the list can be picked.
function ModelPicker({ id, value, onPick, list, thinking, onThinking, levels }) {
  const [q, setQ] = useState('');
  const rows = list.models || [];
  const needle = q.trim().toLowerCase();
  const hits = needle ? rows.filter((m) => m.id.toLowerCase().includes(needle) || m.name.toLowerCase().includes(needle)) : rows;
  const current = rows.find((m) => m.id === value);
  const shown = hits.slice(0, SHOW);
  if (current && !needle && !shown.includes(current)) shown.push(current);   // a pick further down a long list stays in sight
  const canThink = !current || current.reasoning;
  const lv = (current && current.levels) || levels;   // Sol and Astra start at low
  return html`<div class="picker">
    <input id=${id} class="input" value=${q} onInput=${(e) => setQ(e.target.value)} autocomplete="off" spellcheck="false"
      placeholder=${list.loading ? 'Loading the list…' : rows.length ? `Filter ${num(rows.length)} models…` : 'No models'} />
    ${list.note && html`<span class="help warn">${list.note}</span>`}
    <div class="models" role="listbox" aria-label="Models">
      ${shown.map((m) => html`<button type="button" role="option" aria-selected=${m.id === value} class=${'model-row' + (m.id === value ? ' on' : '')} key=${m.id} onClick=${() => onPick(m.id)}>
        <span class="nm">${m.name}</span>
        <span class="idm">${m.id}</span>
        <span class="ctx">${ctx(m.context)}</span>
        <span class="pr" title="$ per million tokens in / out">${m.in_per_m == null ? '' : `${money(m.in_per_m)} / ${money(m.out_per_m)}`}</span>
      </button>`)}
      ${!shown.length && !list.loading && html`<span class="help pad">${rows.length ? 'Nothing matches.' : 'No list yet.'}</span>`}
      ${hits.length > SHOW && html`<span class="help pad">${num(hits.length - SHOW)} more — type to narrow.</span>`}
    </div>
    <div class="row-2">
      <span class="chosen">${value ? html`<b>${value}</b>` : html`<span class="help">Pick a model</span>`}</span>
      <label class="think-lvl">thinking
        <select class="input" value=${clampLevel(lv, thinking)} disabled=${!canThink} onChange=${(e) => onThinking(e.target.value)}>${lv.map((l) => html`<option value=${l}>${l}</option>`)}</select>
      </label>
    </div>
  </div>`;
}

// `only="model"`: the composer's model chip opens the chat model alone (provider, list, thinking), saved the same way;
// the rest stays in Settings, one click away.
export function SettingsPop({ s, only = null }) {
  const lean = only === 'model';
  const box = lean ? 'pop model-pop' : 'pop settings-pop';
  const name = lean ? 'Model' : 'Settings';
  const [st, setSt] = useState(null);           // GET /api/settings, as last read
  const [form, setForm] = useState(null);
  const [lists, setLists] = useState({});       // provider → { models, source, note, loading }
  const [note, setNote] = useState(null);       // { ok, text } under the form
  const last = useRef(0);                        // the newest save: an older answer arriving late is ignored
  const fade = useRef(null);                     // the "Saved" line goes after a few seconds; an error stays
  useEffect(() => () => clearTimeout(fade.current), []);

  const fetchList = async (p, refresh = false) => {
    setLists((l) => ({ ...l, [p]: { ...(l[p] || { models: [] }), loading: true } }));
    const r = await api.get(`/providers/${p}/models${refresh ? '?refresh=1' : ''}`);
    setLists((l) => ({ ...l, [p]: r.ok ? { ...r.data, loading: false } : { models: [], note: r.error.text, loading: false } }));
  };
  useEffect(() => {
    (async () => {
      const r = await api.get('/settings');
      if (!r.ok) { setNote({ ok: false, text: r.error.text }); return; }
      const d = r.data;
      setSt(d);
      // a chosen provider without a key on this server: one that has a key is put forward, its list loaded, to pick from
      const usable = d.configured.includes(d.chat.provider) ? d.chat.provider : d.configured[0];
      setForm({ provider: usable || d.chat.provider, model: usable === d.chat.provider ? d.chat.model : '', thinking: d.chat.thinking,
                same: d.judge.same, jprovider: d.judge.provider, jmodel: d.judge.model, jthinking: d.judge.thinking });
      if (usable) fetchList(usable);
      if (!d.judge.same && d.judge.provider !== d.chat.provider && d.configured.includes(d.judge.provider)) fetchList(d.judge.provider);
    })();
  }, []);
  if (!form) {
    return html`<div class=${box} role="dialog" aria-label=${name} onClick=${hold}><div class="mh"><h4>${name}</h4></div>${!lean && html`<${Appearance} />`}
      ${note && html`<span class="form-error">${note.text}</span>`}${!lean && html`<${Account} me=${s.me} />`}</div>`;
  }

  const f = (patch) => setForm((x) => ({ ...x, ...patch }));
  // a change is saved as soon as the form names a model for each job; until then (a provider was switched) it waits
  const commit = async (patch) => {
    const next = { ...form, ...patch };
    setForm(next);
    clearTimeout(fade.current);
    if (!next.model || (!next.same && !next.jmodel)) { setNote(null); return; }
    const n = ++last.current;
    const r = await api.put('/settings', {
      chat: { provider: next.provider, model: next.model, thinking: next.thinking },
      judge: next.same ? { same: true } : { provider: next.jprovider, model: next.jmodel, thinking: next.jthinking },
    });
    if (n !== last.current) return;
    if (!r.ok) { setNote({ ok: false, text: r.error.text }); return; }
    setSt(r.data);
    await reloadConfig();
    setNote({ ok: true, text: r.data.assistant ? `Saved — the next message goes to ${r.data.model}.` : `Saved. ${r.data.assistant_off}.` });
    if (r.data.assistant) fade.current = setTimeout(() => setNote(null), 3000);
  };
  const P = st.providers.filter((x) => x.configured);
  const p = form.provider;
  const prov = (id) => st.providers.find((x) => x.id === id) || { name: id };
  const offered = (id) => st.configured.includes(id);
  const pickProvider = (id, judge) => {
    if (id === (judge ? form.jprovider : form.provider)) return;      // the one already chosen: the model stays
    if (judge) f({ jprovider: id, jmodel: '' }); else f({ provider: id, model: '' });
    if (!lists[id]) fetchList(id);
    setNote(null);
  };
  const sameChanged = (on) => {
    const jp = offered(form.jprovider) ? form.jprovider : p;
    commit({ same: on, ...(on ? {} : { jprovider: jp, jmodel: form.jmodel || (jp === p ? form.model : '') }) });
    if (!on && !lists[jp]) fetchList(jp);
  };
  const listNote = (id) => {
    const l = lists[id];
    if (!l || !l.source) return null;
    return `${l.source}${l.fetched_at ? ` · ${l.fetched_at.slice(0, 16).replace('T', ' ')}` : ''}${id === 'openrouter' ? ' · models that take tools' : ''}${st.priced_only && l.max_usd_per_m_out != null ? ` · with a known price, up to $${l.max_usd_per_m_out}/M out` : ''}`;
  };
  const a = st.allowance;
  const usd = (x) => `$${Number(x || 0).toFixed(2)}`;
  const segment = (chosen, judge) => (P.length > 1
    ? html`<div class="seg" role="group" aria-label=${judge ? 'Ranking provider' : 'Provider'}>${P.map((x) => html`<button type="button" class=${x.id === chosen ? 'on' : ''} aria-pressed=${x.id === chosen} onClick=${() => pickProvider(x.id, judge)}>${x.name}</button>`)}</div>`
    : html`<span class="help">${P.length ? prov(P[0].id).name : 'none'}</span>`);

  return html`<div class=${box} role="dialog" aria-label=${name} onClick=${hold}>
    <div class="mh"><h4>${name}</h4><span class=${'help' + (st.assistant ? '' : ' warn')}>${st.assistant ? `${lean ? 'now' : 'assistant'}: ${st.model}` : st.assistant_off}</span></div>
    ${!lean && html`<${Appearance} />`}
    ${st.yours && html`<span class="help yours">Your model — a pick here changes nothing for anyone else.</span>`}
    ${a && !lean && html`<span class="help allowance">Today: ${a.turns} of ${a.turns_per_day} assistant turns · ${usd(a.cost)} of ${usd(a.usd_per_day)}${a.sessions_per_day != null ? ` · ${a.sessions} of ${a.sessions_per_day} new sessions` : ''}</span>`}
    ${!P.length && html`<span class="form-error" role="alert">No model provider is set up on this server: it needs OPENAI_API_KEY or OPENROUTER_API_KEY in its environment (or the keys of .resumes/settings.json). Nothing to pick here.</span>`}
    ${P.length > 0 && html`<div class="field">
      <span class="lab">Provider</span>
      ${segment(p, false)}
      ${!offered(p) && html`<span class="help warn">The chosen provider (${prov(p).name}) has no key on this server — pick one of the above.</span>`}
    </div>
    <div class="field">
      <div class="lab-row"><label for="s-model">Chat model</label>${offered(p) && !st.priced_only && html`<button type="button" class="link" onClick=${() => fetchList(p, true)}>refresh the list</button>`}</div>
      <${ModelPicker} id="s-model" value=${form.model} onPick=${(m) => commit({ model: m })} list=${lists[p] || { models: [], loading: offered(p) }}
        thinking=${form.thinking} onThinking=${(v) => commit({ thinking: v })} levels=${st.thinking_levels} />
      ${listNote(p) && html`<span class="help">${listNote(p)}</span>`}
    </div>
    ${lean && html`<span class="help">${form.same ? 'Ranking uses this model too. ' : ''}<button type="button" class="link" onClick=${() => set({ pop: 'settings' })}>All settings</button></span>`}
    ${!lean && html`<label class="switch"><input type="checkbox" checked=${form.same} onChange=${(e) => sameChanged(e.target.checked)} />Ranking uses the same model</label>`}
    ${!lean && !form.same && html`<div class="field judge">
      <span class="lab">Ranking model</span>
      ${segment(form.jprovider, true)}
      <${ModelPicker} id="s-judge" value=${form.jmodel} onPick=${(m) => commit({ jmodel: m })} list=${lists[form.jprovider] || { models: [], loading: offered(form.jprovider) }}
        thinking=${form.jthinking} onThinking=${(v) => commit({ jthinking: v })} levels=${st.thinking_levels} />
      ${listNote(form.jprovider) && html`<span class="help">${listNote(form.jprovider)}</span>`}
    </div>`}`}
    ${note && html`<span class=${note.ok ? 'help ok' : 'form-error'} role=${note.ok ? 'status' : 'alert'}>${note.text}</span>`}
    ${!lean && html`<${Account} me=${s.me} />`}
  </div>`;
}
