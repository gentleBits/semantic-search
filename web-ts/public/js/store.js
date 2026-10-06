// One shared state: whichever side acts — the chat or the panel — the other follows.
// The server holds the truth (the engine session); this is what the page shows of it, plus what is only the page's:
// which popover is open, which row is expanded, how wide the chat is.
import { useEffect, useState } from './lib.js';
import * as api from './api.js';
import { loadFeatures } from './slots.js';

let state = {
  ready: false,
  fatal: null,          // the server cannot be reached / the index is not built
  me: null,             // GET /api/me: { login, user, name } — whether the server wants a login, and who is logged in
  locked: false,        // nobody is let in yet: the page shows the email and phone check (or, without one, the login)
  config: null,         // the collection: its words, its limits
  sessions: [],
  sid: null,
  chat: [],
  snap: null,
  busy: false,          // a turn of the assistant is running
  live: null,           // the answer that is being written: { status, text, steps, rank, since }
  detail: null,         // the resume that is open: { id, person, loading }
  notice: null,         // one line from a panel action that failed
  pop: null,            // the popover that is open: 'history' | 'sort' | 'filter' | 'rank' | 'criteria' | 'sessions' | 'collection' | 'settings' | 'model' | 'chip:f2'
  expanded: null,       // id of the expanded row
  overview: false,
  tab: 'chat',          // phone: which tab
  unseen: false,        // phone: the results changed while the chat was shown
  fresh: [],            // ids of the filters the last step added (their chips glow for a moment)
  hit: null,            // id of the row a #N link points at
  deltaAt: 0,
  draft: '',            // what the composer holds (the Rank… popover and the starters write into the conversation)
};

const listeners = new Set();
export const get = () => state;
export function set(patch) {
  state = { ...state, ...(typeof patch === 'function' ? patch(state) : patch) };
  listeners.forEach((l) => l());
}
export function useStore() {
  const [, tick] = useState(0);
  useEffect(() => {
    const l = () => tick((n) => n + 1);
    listeners.add(l);
    return () => listeners.delete(l);
  }, []);
  return state;
}

const WIDTH_KEY = 'resumes.chat-width';
export function chatWidth() {
  try { return Math.max(340, Math.min(720, parseInt(localStorage.getItem(WIDTH_KEY), 10) || 500)); } catch { return 500; }
}
export function saveChatWidth(px) {
  try { localStorage.setItem(WIDTH_KEY, String(Math.round(px))); } catch { /* private mode */ }
}

// ---------------------------------------------------------------- snapshots

function filterIds(snap) {
  return snap ? snap.filters.map((f) => f.id) : [];
}

// Every new snapshot goes through here, from whichever side it came.
function show(snap, { from = 'panel' } = {}) {
  if (!snap) return;
  const before = state.snap;
  const changed = !before || before.set.id !== snap.set.id;
  const had = new Set(filterIds(before));
  const fresh = changed && before ? filterIds(snap).filter((id) => !had.has(id)) : state.fresh;
  const patch = { snap, fresh };
  if (changed) {
    patch.expanded = null;
    patch.deltaAt = Date.now();
    if (snap.set.op === 'search' && snap.set.count) patch.overview = true;       // open after a new question …
    if (state.detail && before) patch.detail = state.detail;                     // the open resume stays; its place is re-read below
    if (state.tab === 'chat' && before) patch.unseen = true;
  } else if (before && before.set.page !== snap.set.page) {
    patch.overview = false;                                                      // … closed while browsing
    patch.expanded = null;
  }
  if (snap.too_many && !(before && before.too_many)) patch.overview = false;     // the banner needs the room
  set(patch);
  if (changed && fresh.length) setTimeout(() => state.fresh === fresh && set({ fresh: [] }), 2600);
  if (changed && state.detail && state.detail.person && from !== 'open') refreshDetail();
}

// The address is the search's, as chat apps do it: /c/<id>. An open resume is not in it. Links of before
// (#/<id>, #/<id>/<resume>, #/signup) still open their search.
function route() {
  const m = /^\/c\/([^/]+)\/?$/.exec(location.pathname);
  if (m) return { sid: decodeURIComponent(m[1]) };
  const old = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean)[0];
  return { sid: old && old !== 'signup' ? old : null };
}
function go(sid) {
  const path = sid ? '/c/' + encodeURIComponent(sid) : '/';
  if (location.pathname !== path || location.hash) history.replaceState(null, '', path);
}

// ---------------------------------------------------------------- start, conversations

export async function boot() {
  api.onLoginRequired(() => set({ locked: true, ready: true, busy: false, live: null, pop: null }));
  const me = await api.get('/me');
  if (!me.ok) { set({ ready: true, fatal: me.error }); return; }
  set({ me: me.data });
  await loadFeatures(me.data.features);
  if (me.data.login && !me.data.user) { set({ ready: true, locked: true }); return; }
  set({ locked: false });
  const cfg = await api.get('/config');
  if (!cfg.ok) { set({ ready: true, fatal: cfg.error }); return; }
  set({ config: cfg.data });
  document.title = cfg.data.name + (cfg.data.demo ? ' — search demo' : ' — search');
  const want = route();
  const list = await api.get('/sessions');
  const sessions = list.ok ? list.data.sessions : [];
  set({ sessions });
  let opened = null;
  if (want.sid) opened = await api.get('/sessions/' + encodeURIComponent(want.sid));
  if (!opened || !opened.ok) opened = sessions.length ? await api.get('/sessions/' + sessions[0].id) : await api.post('/sessions');
  if (!opened.ok) { set({ ready: true, fatal: opened.error }); return; }
  enter(opened.data);
  set({ ready: true });
  if (!sessions.some((x) => x.id === opened.data.session.id)) refreshSessions();     // made just now: the menu lists it too
  if (opened.data.busy) follow();
}

// The login. After it, the page starts over — and reopens the search its address names, if it is this person's.
export async function login(user, password) {
  const r = await api.post('/login', { user, password });
  if (r.ok) await boot();
  return r;
}

// The email and phone check passed: the page starts over, as after the login.
export async function checked() {
  if (location.hash.startsWith('#/signup')) history.replaceState(null, '', '/');     // an old link to the sign-up
  await boot();
}

export async function logout() {
  await api.post('/logout');
  document.title = 'Semantic search';     // the front door's name, as before boot
  set({ locked: true, pop: null, sid: null, chat: [], snap: null, sessions: [], detail: null, live: null, busy: false, me: { ...(state.me || {}), user: null } });
  go(null);
}

// After Settings changed the model or a key: the header, the starters and the composer follow at once.
export async function reloadConfig() {
  const cfg = await api.get('/config');
  if (cfg.ok) set({ config: cfg.data });
}

function enter(data) {
  set({ sid: data.session.id, chat: data.chat, snap: null, live: null, busy: !!data.busy, detail: null, pop: null, expanded: null, notice: null,
        overview: false, fresh: [], unseen: false, draft: '' });
  show(data.state);
  set({ deltaAt: 0 });
  go(data.session.id);
}

export async function refreshSessions() {
  const list = await api.get('/sessions');
  if (list.ok) set({ sessions: list.data.sessions });
}

export async function openSession(sid) {
  if (sid === state.sid) { set({ pop: null }); return; }
  const r = await api.get('/sessions/' + encodeURIComponent(sid));
  if (!r.ok) { set({ notice: r.error, pop: null }); return; }
  enter(r.data);
  if (r.data.busy) follow();
  refreshSessions();
}

export async function newSession() {
  const empty = state.chat.length === 0 && state.snap && state.snap.history.length <= 1;
  if (empty) { set({ pop: null, tab: 'chat' }); return; }          // this one is still new: no second empty conversation
  const r = await api.post('/sessions');
  if (!r.ok) { set({ notice: r.error }); return; }
  enter(r.data);
  set({ tab: 'chat' });
  refreshSessions();
}

export async function deleteSession(sid) {
  const r = await api.del('/sessions/' + encodeURIComponent(sid));
  if (!r.ok) { set({ notice: r.error }); return; }
  await refreshSessions();
  if (sid === state.sid) {
    const next = state.sessions[0];
    const opened = next ? await api.get('/sessions/' + next.id) : await api.post('/sessions');
    if (opened.ok) enter(opened.data);
    refreshSessions();
  }
}

// A turn is running that this page did not start (the page was reloaded): watch the snapshot until it is over.
async function follow() {
  const sid = state.sid;
  for (;;) {
    await new Promise((r) => setTimeout(r, 900));
    if (state.sid !== sid) return;
    const r = await api.get('/sessions/' + sid + '/state');
    if (!r.ok) return;
    show(r.data.state);
    const run = r.data.state.rank.running;
    set({ live: { status: run ? `Ranking for “${run.criterion}”…` : 'Working…', text: '', steps: [], since: Date.now(),
                  rank: run ? { ...run, running: true, already: 0, at: Date.now() } : null } });
    if (!r.data.busy) {
      const full = await api.get('/sessions/' + sid);
      if (full.ok && state.sid === sid) set({ chat: full.data.chat, busy: false, live: null });
      return;
    }
    set({ busy: true });
  }
}

// ---------------------------------------------------------------- the panel: mechanical steps

let noticeTimer = null;
export function notify(error) {
  clearTimeout(noticeTimer);
  set({ notice: error });
  if (error) noticeTimer = setTimeout(() => set({ notice: null }), 7000);
}

const NARROWS = new Set(['filter', 'unknown']);

export async function act(action, { keep = false } = {}) {
  if (!state.sid) return null;
  if (!keep) set({ pop: null });
  const r = await api.post('/sessions/' + state.sid + '/action', action);
  if (!r.ok) {
    if (r.state) show(r.state);
    notify(r.error);
    return null;
  }
  notify(null);
  const d = r.data;
  if (d.event && d.event.id) set((s) => ({ chat: [...s.chat, d.event] }));
  show(d.state, { from: action.type === 'open' ? 'open' : 'panel' });
  if (d.person) {
    set({ detail: { id: d.person.id, person: d.person, loading: false } });
  }
  if (action.type !== 'page' && action.type !== 'next' && action.type !== 'prev' && action.type !== 'open') refreshSessions();
  const rank = d.state.rank;
  if (NARROWS.has(action.type) && rank.pending && rank.possible && !state.busy) say('', { auto: 'rank_pending' });     // "I'll rank right after"
  return d;
}

export const page = (n) => act({ type: 'page', n });

export async function openPerson(ref, { quiet = false } = {}) {
  if (!state.sid) return;
  set((s) => ({ detail: { id: ref, person: s.detail && s.detail.id === ref ? s.detail.person : null, loading: true }, pop: null }));
  if (quiet) {
    const r = await api.get('/sessions/' + state.sid + '/people/' + encodeURIComponent(ref));
    if (!r.ok) { set({ detail: null }); notify(r.error); return; }
    set({ detail: { id: r.data.id, person: r.data, loading: false } });
    return;
  }
  const d = await act({ type: 'open', id: ref }, { keep: true });
  if (!d) set({ detail: null });
}

async function refreshDetail() {
  const d = state.detail;
  if (!d || !d.person) return;
  const r = await api.get('/sessions/' + state.sid + '/people/' + encodeURIComponent(d.person.id));
  if (r.ok && state.detail && state.detail.id === d.id) set({ detail: { id: r.data.id, person: r.data, loading: false } });
}

export function closeDetail() {
  set({ detail: null });
}

// A #N in an answer: show the row (go to its page) and open the resume.
export async function pointAt(id) {
  set({ hit: id });
  setTimeout(() => state.hit === id && set({ hit: null }), 1800);
  await openPerson(id);
  const p = state.detail && state.detail.person;
  if (p && p.rank && state.snap) {
    const want = Math.ceil(p.rank / state.snap.set.page_size);
    if (want !== state.snap.set.page) await act({ type: 'page', n: want }, { keep: true });
  }
  if (window.matchMedia('(max-width: 760px)').matches) set({ tab: 'results', unseen: false });
}

// ---------------------------------------------------------------- the chat: the assistant's turn

export async function say(text, { attachment = null, auto = null } = {}) {
  if (!state.sid || state.busy) return;
  const sid = state.sid;
  set({ busy: true, pop: null, notice: null, live: { status: null, text: '', steps: [], rank: null, since: Date.now(), quiet: !!auto } });
  const live = (patch) => set((s) => (s.sid === sid && s.live ? { live: { ...s.live, ...(typeof patch === 'function' ? patch(s.live) : patch) } } : {}));
  const push = (...msgs) => set((s) => (s.sid === sid ? { chat: [...s.chat, ...msgs.filter((m) => m && m.id && !s.chat.some((c) => c.id === m.id))] } : {}));
  const r = await api.stream('/sessions/' + sid + '/chat', { text, attachment, auto }, (ev) => {
    if (state.sid !== sid) return;
    switch (ev.type) {
      case 'user': push(ev.message); break;
      case 'status': live({ status: ev.text, text: '' }); break;
      case 'delta': live((l) => ({ text: l.text + ev.text })); break;
      case 'interim': live((l) => ({ status: ev.text || l.status, text: '' })); break;
      case 'step': live((l) => ({ steps: [...l.steps, ev.step] })); break;
      case 'state': show(ev.state, { from: 'chat' }); break;
      case 'rank':
        live({ rank: ev.progress.running ? { ...ev.progress, at: Date.now() } : null });
        landed(ev.progress);
        break;
      case 'open':
        set({ detail: { id: ev.id, person: ev.person, loading: false } });
        break;
      case 'done':
        if (ev.message) push(ev.message);
        if (ev.messages) push(...ev.messages);
        set({ live: null });
        if (ev.state) show(ev.state, { from: 'chat' });
        if (ev.ui && ev.ui.history) set({ pop: 'history', tab: 'results' });
        if (ev.ui && ev.ui.open) openPerson(ev.ui.open, { quiet: true });
        break;
      case 'error':
        push(ev.message.id ? ev.message : { ...ev.message, id: 'e' + Date.now() });
        set({ live: null });
        if (ev.state) show(ev.state, { from: 'chat' });
        break;
      default:
    }
  });
  if (state.sid === sid) {
    if (!r.ok) set((s) => ({ chat: [...s.chat, { ...r.error, id: 'e' + Date.now() }] }));
    set({ busy: false, live: null });
    const fresh = await api.get('/sessions/' + sid + '/state');
    if (fresh.ok && state.sid === sid) show(fresh.data.state, { from: 'chat' });
  }
  refreshSessions();
}

// Scores fill in as they land: the progress of the job, written into the list that is shown.
function landed(p) {
  const snap = state.snap;
  if (!snap || !p.running || snap.set.id !== p.set) return;
  const got = new Map((p.landed || []).map((x) => [x.id, x]));
  const items = snap.items.map((i) => (got.has(i.id) && i.score == null
    ? { ...i, score: got.get(i.id).score, note: got.get(i.id).note || null, pending: false } : i));
  const running = { criterion: p.criterion, done: p.done, total: p.total, seconds: p.seconds, set: p.set };
  set({ snap: { ...snap, items, rank: { ...snap.rank, running } } });
}

export async function stop() {
  if (state.sid) await api.post('/sessions/' + state.sid + '/stop');
}

// What a button under an answer does: a step on the panel, or words for the assistant.
export function follow_up(a) {
  if (a.action) return act(a.action);
  if (a.say) return say(a.say);
  return null;
}

export const toggle = (name) => set((s) => ({ pop: s.pop === name ? null : name }));
export const close = () => state.pop && set({ pop: null });
