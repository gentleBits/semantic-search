// The left pane: short answers, what the assistant did, what the user did on the panel.
import { html, useEffect, useLayoutEffect, useRef, useState } from './lib.js';
import * as I from './icons.js';
import { ago, clock, num, people, rich } from './format.js';
import { act, close, deleteSession, follow_up, get, newSession, openSession, pointAt, say, set, stop, toggle } from './store.js';
import { SettingsPop } from './settings.js';

// ---------------------------------------------------------------- header

// The header reads as a path: the collection, then this search (its menu: the past ones). New session is a labelled
// button; light/dark and the account are in Settings.
export const UNTITLED = 'Untitled search';

export function ChatHead({ s }) {
  const { config: c, snap } = s;
  const title = (snap && snap.session.title) || UNTITLED;
  return html`<header class="chat-head">
    <div class="crumbs">
      <button class="brand" onClick=${(e) => { e.stopPropagation(); toggle('collection'); }} aria-expanded=${s.pop === 'collection'} aria-haspopup="dialog">
        <span class="mark"><${I.Search} size=${12} color="var(--on-accent)" /></span>
        <span class="name">${c.name}</span>
        ${c.demo && html`<span class="tag" title="A demo over public resumes, not a product">demo</span>`}
        <span class="n">${num(c.count)}</span>
      </button>
      <span class="crumb-sep" aria-hidden="true">/</span>
      <button class="session-btn" onClick=${(e) => { e.stopPropagation(); toggle('sessions'); }} aria-expanded=${s.pop === 'sessions'} aria-haspopup="dialog"
        title="Past searches">
        <span class="t">${title}</span><${I.Down} size=${12} width=${2} color="var(--ink-3)" />
      </button>
    </div>
    <div class="head-right">
      <button class="new-search" title="New session" onClick=${newSession}><${I.Plus} size=${14} width=${2} color="var(--ink)" /><span>New session</span></button>
      <button class=${'icon-btn' + (c.assistant ? '' : ' attention')} aria-label="Settings" title=${c.assistant ? 'Settings: the model' : 'Settings — ' + c.assistant_off}
        aria-expanded=${s.pop === 'settings'} aria-haspopup="dialog" onClick=${(e) => { e.stopPropagation(); toggle('settings'); }}><${I.Gear} size=${16} color="var(--ink)" /></button>
    </div>
    ${s.pop === 'collection' && html`<${CollectionPop} c=${c} />`}
    ${s.pop === 'sessions' && html`<${SessionsPop} s=${s} />`}
    ${s.pop === 'settings' && html`<${SettingsPop} s=${s} />`}
  </header>`;
}

function CollectionPop({ c }) {
  return html`<div class="pop coll-pop" role="dialog" aria-label="Collection" onClick=${(e) => e.stopPropagation()}>
    <div class="row1"><span class="brand" style="padding:0"><span class="mark"><${I.Search} size=${12} color="var(--on-accent)" /></span></span><b>${c.name}</b></div>
    <dl>
      <dt>${c.nouns}</dt><dd>${num(c.count)}</dd>
      <dt>${c.document}s</dt><dd>${num(c.documents)}${c.documents !== c.count ? ' · older versions fold into the newest' : ''}</dd>
      <dt>index</dt><dd>${c.index_version}</dd>
      <dt>assistant</dt><dd>${c.assistant ? c.model : 'off — the panel and / commands work'} <button class="link" onClick=${() => set({ pop: 'settings' })}>change</button></dd>
      <dt>ranks up to</dt><dd>${c.limit} ${c.nouns}</dd>
    </dl>
    <span class="hint">One collection for now. Others will be listed here.</span>
  </div>`;
}

function SessionsPop({ s }) {
  const rows = s.sessions;
  return html`<div class="pop menu sessions-pop" role="dialog" aria-label="Past searches" onClick=${(e) => e.stopPropagation()}>
    <div class="mh"><h4>Searches</h4></div>
    ${rows.map((r) => html`<div class=${'sess' + (r.id === s.sid ? ' cur' : '')} key=${r.id}>
      <button class="go" onClick=${() => openSession(r.id)}>
        <b>${r.title || UNTITLED}</b>
        <span>${r.count != null ? people(s.config, r.count) + ' · ' : ''}${ago(r.updated_at)}${r.busy ? ' · working' : ''}</span>
      </button>
      <button class="x" aria-label=${'Delete ' + (r.title || 'this search')} title="Delete" onClick=${() => deleteSession(r.id)}><${I.Trash} size=${13} /></button>
    </div>`)}
    ${!rows.length && html`<span class="hint" style="padding:6px 10px">Nothing yet.</span>`}
    <hr />
    <button class="mi" onClick=${newSession}><${I.Plus} />New session</button>
  </div>`;
}

// ---------------------------------------------------------------- messages

function Avatar({ busy }) {
  return html`<span class="avatar"><i class=${busy ? 'pulse' : ''}></i></span>`;
}

function Cue({ cue, cfg }) {
  if (cue.kind === 'understood') {
    const n = cue.terms.length;
    return html`<div class="cue"><${I.Info} size=${12} />
      <span>Understood as ${cue.terms.map((t, i) => html`${i ? (i === n - 1 ? ' and ' : ', ') : ''}${t.kind} <em>${t.name}</em>${t.expanded.length
        ? html` · incl. ${t.expanded.join(', ')}${t.more ? ` +${t.more}` : ''}` : ''}`)}${cue.by_meaning
        ? html` · ${num(cue.by_meaning)} found by meaning, not exact words` : ''}</span>
    </div>`;
  }
  if (cue.kind === 'unresolved') {
    return html`<div class="cue"><${I.Info} size=${12} /><span>No known skill or topic matched <em>“${cue.words.join(' ')}”</em> — searched as free text</span></div>`;
  }
  if (cue.kind === 'meaning') {
    return html`<div class="cue"><${I.Info} size=${12} /><span>Searched by meaning: <em>${cue.value}</em>${cue.by_meaning ? ` · ${num(cue.by_meaning)} found by meaning, not exact words` : ''}</span></div>`;
  }
  if (cue.kind === 'like') {
    return html`<div class="cue"><${I.Info} size=${12} /><span>Matched against <em>${cue.source}</em> · ${cue.requirements} requirements${cue.resolved.length
      ? html` · understood ${cue.resolved.slice(0, 6).join(', ')}${cue.resolved.length > 6 ? ` +${cue.resolved.length - 6}` : ''}` : ''} · best match first</span></div>`;
  }
  if (cue.kind === 'unknown') {
    return html`<div class="cue"><${I.Info} size=${12} /><span>+${num(cue.count)} with unknown ${cue.what} are left out ·${' '}
      <button class="link" onClick=${() => act({ type: 'unknown', id: cue.id, include: true })}>include them</button></span></div>`;
  }
  if (cue.kind === 'estimated') {
    return html`<div class="cue"><${I.Info} size=${12} /><span>Rate is estimated for ${num(cue.count)} of the ${num(cue.of)} — marked with ~</span></div>`;
  }
  return null;
}

function Steps({ steps, open }) {
  if (!steps || !steps.length) return null;
  return html`<details class="did" open=${open}>
    <summary><${I.Right} size=${10} />What I did · ${steps.length} step${steps.length === 1 ? '' : 's'}</summary>
    <div class="steps">${steps.map((st) => html`<div class="step">
      <span class=${'sign' + (st.sign === '★' ? ' star' : '')}>${st.sign === '?' ? '+' : st.sign}</span>${st.text} <span class="amt">${st.detail}</span>
    </div>`)}</div>
  </details>`;
}

// A button under an answer acts only while it still describes the screen: the filter it removes is active,
// the people it offers to rank are still unranked. Old ones stay readable, greyed.
function valid(a, snap) {
  if (!snap) return false;
  if (a.action && a.action.type === 'drop') return (a.action.targets || []).every((t) => snap.filters.some((f) => f.id === t));
  if (a.action && a.action.type === 'page') return a.action.n <= snap.set.pages;
  const m = a.say && /^Rank the (\d+) (new|left)/.exec(a.say);
  if (m) return !!snap.ranking && snap.rank.possible && snap.rank.unranked === Number(m[1]);
  return true;
}

function Suggestions({ chips, disabled }) {
  return html`<div class="opts">${chips.map((c) => html`<button class="sugg" disabled=${disabled} onClick=${() => act({ type: 'filter', args: c.args })}>
    ${c.kind && html`<span class="k">${c.kind}</span>`}${c.value} <span class="n">→ ${num(c.count)}</span>
  </button>`)}</div>`;
}

function Actions({ actions, disabled, snap }) {
  return html`<div class="opts">${actions.map((a) => {
    const off = disabled || !valid(a, snap);
    return a.star || a.say
      ? html`<button class="act-dark" disabled=${off} onClick=${() => follow_up(a)}>${a.star && html`<${I.Star} color="var(--rank-3)" />`}${a.label}</button>
          ${a.hint && !off && html`<span class="hint">${a.hint}</span>`}`
      : html`<button class="sugg" disabled=${off} onClick=${() => follow_up(a)}>${a.label} ${a.count != null && html`<span class="n">→ ${num(a.count)}</span>`}</button>`;
  })}</div>`;
}

function Message({ m, s, last }) {
  const cfg = s.config;
  // only the last answer's options act; older ones stay readable
  const stale = !last || s.busy;
  if (m.role === 'user') {
    return html`<div class=${'msg bubble' + (m.mono ? ' cmd' : '')}>
      ${m.attachment && html`<div class="att"><${I.Clip} size=${13} /><span>${m.attachment.name}</span><span class="meta">${m.attachment.lines} lines</span></div>`}
      ${m.text && html`<div class="said">${m.text}</div>`}
    </div>`;
  }
  if (m.role === 'event') return html`<div class="msg event"><${I.Cursor} size=${11} />${m.text}</div>`;
  if (m.role === 'info') return html`<div class="msg info">${m.text}</div>`;
  if (m.role === 'error') {
    return html`<div class="msg alert" role="alert"><${I.Warn} color="var(--red)" /><span>${m.text}</span>
      ${m.fix && !stale && valid(m.fix, s.snap) && html`<button class="fix" title=${m.fix.hint} onClick=${() => follow_up(m.fix)}>${m.fix.label}</button>`}
    </div>`;
  }
  return html`<div class="msg turn">
    <${Avatar} />
    <div class="turn-body">
      <div class="text">${rich(m.text, m.refs, pointAt)}</div>
      ${(m.cues || []).map((c) => html`<${Cue} cue=${c} cfg=${cfg} />`)}
      ${m.chips && html`<${Suggestions} chips=${m.chips} disabled=${stale || !(s.snap && s.snap.too_many)} />`}
      ${m.chips && html`<div class="cue" style="padding-left:0">Or tell me what matters most — a skill, a city, a seniority.</div>`}
      ${m.actions && html`<${Actions} actions=${m.actions} disabled=${stale} snap=${s.snap} />`}
      <${Steps} steps=${m.steps} open=${false} />
    </div>
  </div>`;
}

function useTick(on, ms = 500) {
  const [, tick] = useState(0);
  useEffect(() => {
    if (!on) return undefined;
    const t = setInterval(() => tick((n) => n + 1), ms);
    return () => clearInterval(t);
  }, [on, ms]);
}

function Progress({ rank, cfg }) {
  useTick(true);
  const seconds = rank.seconds + (Date.now() - rank.at) / 1000;
  const pct = rank.total ? Math.round((rank.done / rank.total) * 100) : 0;
  const tick = html`<${I.Check} size=${13} color="var(--blue)" />`;
  return html`<div class="progress">
    <div class="top">
      <span class="title"><${I.Arc} cls="spin" color="var(--rank)" />Ranking ${people(cfg, rank.total)}</span>
      <span class="clock">${clock(seconds)}</span>
    </div>
    <div class="todo">
      <div class="li">${tick}Criterion: ${rank.criterion}</div>
      <div class="li">${tick}Read ${num(rank.total)} ${cfg.document}${rank.total === 1 ? '' : 's'}${rank.already ? ` · ${num(rank.already)} keep their score` : ''}</div>
      <div class="li doing"><span class="dot"><i class="pulse"></i></span>Scoring <span class="mono">${num(rank.done)} of ${num(rank.total)}</span></div>
      <div class="li todo-next"><span class="dot open"><i></i></span>Order the list</div>
    </div>
    <div class="bar"><i style=${`width:${pct}%`}></i></div>
    <div class="end">
      <span>You can keep browsing — scores fill in as they land.</span>
      <button class="stop-btn" onClick=${stop}><${I.Square} color="var(--ink)" />Stop</button>
    </div>
  </div>`;
}

function Live({ live, s }) {
  const text = live.text || live.status;
  return html`<div class="msg turn" aria-live="polite">
    <${Avatar} busy=${true} />
    <div class="turn-body">
      ${text ? html`<div class="text">${rich(text, null, null)}</div>`
        : html`<div class="thinking" aria-label="The assistant is working"><i></i><i></i><i></i></div>`}
      ${live.rank && html`<${Progress} rank=${live.rank} cfg=${s.config} />`}
      <${Steps} steps=${live.steps} open=${!live.rank} />
    </div>
  </div>`;
}

function Welcome({ s, onJd }) {
  const c = s.config;
  return html`<div class="welcome">
    <div class="lead">
      <h1>Ask about ${num(c.count)} ${c.document}s<br />in plain words.</h1>
      <p>I turn what you say into filters you can see — and remove — on the right. Paging, sorting and opening a ${c.document} work there directly.</p>
      ${!c.assistant && html`<p class="off-note"><${I.Warn} color="var(--rank-ink)" /><span>${c.assistant_off}. <button class="link" onClick=${(e) => { e.stopPropagation(); set({ pop: 'settings' }); }}>Open Settings</button> — the panel and the / commands work meanwhile.</span></p>`}
    </div>
    <div class="starters">
      <span class="label">Try</span>
      ${c.starters.map((q) => html`<button class="starter" disabled=${!c.assistant} onClick=${() => say(q)}>${q}<${I.Arrow} color="var(--ink-3)" /></button>`)}
      <button class="starter add" disabled=${!c.assistant} onClick=${onJd} aria-describedby="add-tip-w">
        <${I.Plus} size=${16} width=${2} color="var(--ink-2)" />
        <span class="two"><b>Add file</b><small>A .md or .txt file</small></span>
        <span class="add-tip" id="add-tip-w" role="tooltip">${attachTip(c)}</span>
      </button>
    </div>
  </div>`;
}

function Thread({ s }) {
  const box = useRef(null);
  const stuck = useRef(true);
  const lastTop = useRef(0);
  const n = s.chat.length;
  const live = s.live;
  useLayoutEffect(() => {
    const el = box.current;
    if (el && stuck.current) el.scrollTop = el.scrollHeight;
  }, [n, live && live.text, live && live.status, live && live.steps.length, live && live.rank && live.rank.done, !!live]);
  useLayoutEffect(() => { stuck.current = true; if (box.current) box.current.scrollTop = box.current.scrollHeight; }, [s.sid]);
  // Only the reader scrolling up lets go of the bottom: our own scroll to the bottom fires its event a frame later, when
  // more may have arrived, and would look like the reader had left.
  const onScroll = () => {
    const el = box.current;
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 60) stuck.current = true;
    else if (el.scrollTop < lastTop.current) stuck.current = false;
    lastTop.current = el.scrollTop;
  };
  useEffect(() => {
    // the pane gets narrower or lower (a resume opens, the window changes): what was at the bottom stays there
    const el = box.current;
    if (!el || typeof ResizeObserver === 'undefined') return undefined;
    const ro = new ResizeObserver(() => { if (stuck.current) el.scrollTop = el.scrollHeight; });
    ro.observe(el);
    if (el.firstElementChild) ro.observe(el.firstElementChild);
    return () => ro.disconnect();
  }, []);
  // runs of "you opened …" collapse to the last one: the transcript keeps them all, the eye needs one
  const shown = s.chat.filter((m, i) => !(m.role === 'event' && m.kind === 'open' && s.chat[i + 1] && s.chat[i + 1].role === 'event' && s.chat[i + 1].kind === 'open'));
  const lastAnswer = [...shown].reverse().find((m) => m.role === 'assistant' || m.role === 'error');
  return html`<div class="thread-wrap">
    <div class="thread" ref=${box} onScroll=${onScroll}>
      <div class="thread-in">
        ${shown.map((m) => html`<${Message} key=${m.id} m=${m} s=${s} last=${m === lastAnswer} />`)}
        ${live && html`<${Live} live=${live} s=${s} />`}
      </div>
    </div>
    <div class="thread-fade"></div>
  </div>`;
}

// ---------------------------------------------------------------- composer

const COMMANDS = [
  ['/next', 'next page'], ['/prev', 'previous page'], ['/page 10', 'go to a page'], ['/show #3', 'open a resume'],
  ['/filters', 'what is active'], ['/drop elixir', 'remove a filter (a word of it, f2, or rank)'], ['/clear-filters', 'everyone again'],
  ['/sort rate,years', 'order the list'], ['/back', 'undo the last step'], ['/sets', 'the history'], ['/rank talented', 'rank for a criterion'],
];
const JD_TYPES = '.md,.txt,.markdown,text/plain,text/markdown';
const JD_MAX = 60000;

// The model the next message goes to, as the chip says it: no provider ("openai:"), no vendor ("meta-llama/").
const shortModel = (m) => String(m || '').replace(/^[^:]+:/, '').replace(/^.*\//, '');

// Which model answers, always in sight while one types; a click picks another (the chat model of Settings, alone).
function ModelChip({ s, cls = '' }) {
  const c = s.config;
  const open = s.pop === 'model';
  return html`<button class=${'model-chip' + (c.assistant ? '' : ' off') + (cls ? ' ' + cls : '')} aria-haspopup="dialog" aria-expanded=${open}
    title=${c.assistant ? `The assistant runs on ${c.model} — click to change` : c.assistant_off}
    onClick=${(e) => { e.stopPropagation(); toggle('model'); }}>
    <span class="nm">${c.assistant ? shortModel(c.model) : 'No model'}</span><${I.Down} size=${11} width=${2} color="var(--ink-3)" />
  </button>`;
}

// What "+ Add file" says on hover: a file goes into the conversation; the collection says what it usually is here.
function attachTip(c) {
  return 'Add a file to the conversation (.md or .txt)' + (c.attach ? `, e.g. ${c.attach}.` : '.');
}

function Composer({ s }) {
  const [file, setFile] = useState(null);           // { name, text }
  const [over, setOver] = useState(false);
  const [pick, setPick] = useState(0);
  const area = useRef(null);
  const input = useRef(null);
  const text = s.draft;
  const setText = (t) => set({ draft: t });
  const busy = s.busy;
  const off = !s.config.assistant;
  const opened = s.detail && s.detail.person;

  const grow = () => {
    const el = area.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = Math.min(180, el.scrollHeight) + 'px';
  };
  useLayoutEffect(grow, [text]);
  useEffect(() => { if (!busy && area.current && window.matchMedia('(min-width: 761px)').matches) area.current.focus(); }, [busy, s.sid]);

  const typed = text.trim();
  const menu = typed.startsWith('/') && !typed.includes(' ') && !file
    ? COMMANDS.filter(([c]) => c.split(' ')[0].startsWith(typed.toLowerCase())) : [];
  useEffect(() => setPick(0), [menu.length]);

  const send = () => {
    if (busy) return;
    const t = text.trim();
    if (!t && !file) return;
    if (off && !t.startsWith('/')) return;
    say(t, { attachment: file });
    setText('');
    setFile(null);
  };
  const take = (f) => {
    if (!f) return;
    if (f.size > JD_MAX) { set({ notice: { role: 'error', code: 'TOO_LONG', text: 'That file is too long (60 KB at most).' } }); return; }
    const r = new FileReader();
    r.onload = () => { setFile({ name: f.name.replace(/\.(md|markdown|txt)$/i, ''), text: String(r.result || '') }); if (area.current) area.current.focus(); };
    r.readAsText(f);
  };
  const onKey = (e) => {
    if (menu.length && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
      e.preventDefault();
      setPick((p) => (p + (e.key === 'ArrowDown' ? 1 : menu.length - 1)) % menu.length);
      return;
    }
    if (menu.length && e.key === 'Tab') { e.preventDefault(); choose(menu[pick][0]); return; }
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      if (menu.length && menu[pick][0].split(' ')[0] !== typed) { choose(menu[pick][0]); return; }
      send();
    }
    if (e.key === 'Escape' && typed.startsWith('/')) setText('');
  };
  const choose = (cmd) => {
    const bare = cmd.split(' ')[0];
    const takesArg = cmd.includes(' ');
    setText(takesArg ? bare + ' ' : bare);
    if (area.current) area.current.focus();
  };
  const phone = window.matchMedia('(max-width: 760px)').matches;
  const placeholder = off ? (phone ? '/ for commands' : 'No assistant key — type / for commands, or use the panel')
    : opened && opened.rank ? `Ask about #${opened.rank}, or anything else…` : 'Ask or refine…';

  return html`<div class="composer-wrap">
    <div class="model-line"><${ModelChip} s=${s} /></div>
    ${s.pop === 'model' && html`<${SettingsPop} s=${s} only="model" />`}
    ${menu.length > 0 && html`<div class="cmds" role="listbox" aria-label="Commands">
      ${menu.map(([c, d], i) => html`<button class=${'cmd-row' + (i === pick ? ' on' : '')} role="option" aria-selected=${i === pick}
        onMouseEnter=${() => setPick(i)} onClick=${() => choose(c)}><span class="c">${c}</span><span class="d">${d}</span></button>`)}
      <div class="key-note">Commands act at once — no assistant involved.</div>
    </div>`}
    <div class=${'composer' + (over ? ' over' : '')}
      onDragOver=${(e) => { e.preventDefault(); setOver(true); }} onDragLeave=${() => setOver(false)}
      onDrop=${(e) => { e.preventDefault(); setOver(false); take(e.dataTransfer.files[0]); }}>
      <div class="stack-in">
        ${file && html`<div class="attached"><${I.Clip} size=${13} /><span class="nm">${file.name}</span>
          <span class="meta">${file.text.split('\n').filter((l) => l.trim()).length} lines</span>
          <button class="x" aria-label="Remove the file" onClick=${() => setFile(null)}><${I.X} size=${11} /></button></div>`}
        <label class="sr" for="ask">Ask</label>
        <textarea id="ask" ref=${area} rows="2" placeholder=${placeholder} value=${text}
          onInput=${(e) => setText(e.target.value)} onKeyDown=${onKey}></textarea>
      </div>
      <div class="bar">
        <div class="left">
          <button class="jd-btn" disabled=${off} onClick=${() => input.current && input.current.click()} aria-label="Add file"
            aria-describedby="add-tip"><${I.Plus} size=${15} width=${2} /><span>Add file</span>
            <span class="add-tip" id="add-tip" role="tooltip">${attachTip(s.config)}</span>
          </button>
          <input ref=${input} type="file" accept=${JD_TYPES} hidden onChange=${(e) => { take(e.target.files[0]); e.target.value = ''; }} />
        </div>
        <div class="right">
          <${ModelChip} s=${s} />
          ${busy
            ? html`<button class="send stop" aria-label=${s.live && s.live.rank ? 'Stop ranking' : 'Stop'} onClick=${stop}><${I.Square} color="var(--surface)" /></button>`
            : html`<button class="send" aria-label="Send" disabled=${(!typed && !file) || (off && !typed.startsWith('/'))} onClick=${send}><${I.Send} color="var(--on-accent)" /></button>`}
        </div>
      </div>
    </div>
  </div>`;
}

export function Chat({ s }) {
  const input = useRef(null);
  const empty = s.chat.length === 0 && !s.live;
  const pickJd = () => { const el = document.querySelector('.composer input[type=file]'); if (el) el.click(); };
  return html`<aside class="chat" aria-label="Chat" onClick=${close}>
    <${ChatHead} s=${s} />
    ${empty ? html`<${Welcome} s=${s} onJd=${pickJd} />` : html`<${Thread} s=${s} />`}
    <${Composer} s=${s} ref=${input} />
  </aside>`;
}

export { get };
