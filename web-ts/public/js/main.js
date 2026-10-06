// Resume search: chat on the left, results on the right.
import { html, render, useEffect, useRef, useState } from './lib.js';
import * as I from './icons.js';
import { num } from './format.js';
import { Chat } from './chat.js';
import { Results } from './results.js';
import { Detail } from './detail.js';
import { act, boot, chatWidth, close, closeDetail, newSession, saveChatWidth, set, toggle, useStore } from './store.js';
import { SettingsPop } from './settings.js';
import { Login } from './login.js';
import { Check } from './signup.js';

function useMedia(query) {
  const [hit, setHit] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const m = window.matchMedia(query);
    const on = () => setHit(m.matches);
    m.addEventListener('change', on);
    return () => m.removeEventListener('change', on);
  }, [query]);
  return hit;
}

function Divider({ fixed }) {
  const [drag, setDrag] = useState(false);
  const down = (e) => {
    if (fixed) return;
    e.preventDefault();
    setDrag(true);
    const move = (ev) => {
      const w = Math.max(340, Math.min(Math.min(720, window.innerWidth - 560), ev.clientX));
      document.documentElement.style.setProperty('--chat-w', w + 'px');
    };
    const up = (ev) => {
      setDrag(false);
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      saveChatWidth(Math.max(340, Math.min(720, ev.clientX)));
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  };
  const key = (e) => {
    if (fixed || (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight')) return;
    const w = Math.max(340, Math.min(720, chatWidth() + (e.key === 'ArrowLeft' ? -20 : 20)));
    document.documentElement.style.setProperty('--chat-w', w + 'px');
    saveChatWidth(w);
  };
  return html`<div class=${'divider' + (drag ? ' drag' : '')} role="separator" aria-orientation="vertical" aria-label="Resize the chat" tabindex=${fixed ? -1 : 0}
    onPointerDown=${down} onKeyDown=${key} onDblClick=${() => { if (!fixed) { document.documentElement.style.setProperty('--chat-w', '500px'); saveChatWidth(500); } }}>
    <div class="grip"></div>
  </div>`;
}

function PhoneTop({ s }) {
  const { snap, config: c } = s;
  const title = (snap && snap.session.title) || c.name;
  const busy = s.busy || !!snap.rank.running;
  const show = (tab) => set({ tab, pop: null, ...(tab === 'results' ? { unseen: false } : {}) });
  return html`<div class="m-top">
    <header class="m-head">
      <button class="brand" onClick=${(e) => { e.stopPropagation(); toggle('sessions'); }} aria-expanded=${s.pop === 'sessions'}>
        <span class="mark"><${I.Search} size=${12} color="var(--on-accent)" /></span>
        <span class="name">${title}</span>
        ${c.demo && html`<span class="tag" title="A demo over public resumes, not a product">demo</span>`}
        <${I.Down} size=${12} color="var(--ink-3)" />
      </button>
      <div class="tools">
        ${s.tab === 'results' && html`
          <button aria-label="Undo" disabled=${!snap.set.prev || busy} onClick=${() => act({ type: 'undo' })}><${I.Undo} size=${18} /></button>
          <button aria-label="History" onClick=${(e) => { e.stopPropagation(); toggle('history'); }}><${I.Clock} size=${18} /></button>`}
        <button aria-label="New session" onClick=${newSession}><${I.Plus} size=${18} /></button>
        <button aria-label="Settings" aria-expanded=${s.pop === 'settings'} onClick=${(e) => { e.stopPropagation(); toggle('settings'); }}><${I.Gear} size=${18} /></button>
      </div>
      ${s.pop === 'settings' && html`<${SettingsPop} s=${s} />`}
    </header>
    <nav class="tabs" aria-label="View">
      <button class=${'tab' + (s.tab === 'chat' ? ' on' : '')} aria-current=${s.tab === 'chat' ? 'page' : null} onClick=${() => show('chat')}>Chat</button>
      <button class=${'tab' + (s.tab === 'results' ? ' on' : '')} aria-current=${s.tab === 'results' ? 'page' : null} onClick=${() => show('results')}>Results
        <span class="n">${num(snap.set.count)}${s.unseen && s.tab === 'chat' && html`<span class="new" aria-label="changed"></span>`}</span>
      </button>
    </nav>
  </div>`;
}

function Fatal({ error }) {
  const built = error.code !== 'INDEX_NOT_BUILT';
  return html`<div class="zero" style="height:100dvh"><div class="in">
    <h2>${built ? 'The search engine does not answer.' : 'There is no index yet.'}</h2>
    <p>${built ? error.text : 'Build it once, then reload this page:'} ${!built && html`<span class="mono">resumes corpus build && resumes index build</span>`}</p>
    <div class="choices"><button class="choice main" onClick=${() => location.reload()}><span>Try again</span><span class="n">reload</span></button></div>
  </div></div>`;
}

function App() {
  const s = useStore();
  const phone = useMedia('(max-width: 760px)');
  const wide = useMedia('(min-width: 1280px)');
  const booted = useRef(false);
  useEffect(() => {
    if (booted.current) return;
    booted.current = true;
    document.documentElement.style.setProperty('--chat-w', chatWidth() + 'px');
    boot();
    window.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
  }, []);
  if (s.fatal) return html`<${Fatal} error=${s.fatal} />`;
  if (s.locked) return s.me && s.me.signup ? html`<${Check} />` : html`<${Login} />`;
  if (!s.ready || !s.snap) return html`<div class="app" aria-busy="true"></div>`;
  const detail = !!s.detail;
  const third = detail && wide && !phone;                 // the resume as a third column
  const drawer = detail && !wide && !phone;               // … or as a drawer over the results
  const cls = ['app', third ? 'with-detail' : '', drawer ? 'drawer' : '', phone ? 'tab-' + s.tab : ''].filter(Boolean).join(' ');
  return html`<div class=${cls} onClick=${close}>
    ${phone && html`<${PhoneTop} s=${s} />`}
    <${Chat} s=${s} />
    <${Divider} fixed=${third} />
    <${Results} s=${s} compact=${third} />
    ${drawer && html`<button class="scrim" aria-label="Close" onClick=${closeDetail}></button>`}
    ${detail && html`<${Detail} s=${s} />`}
    ${phone && s.pop && html`<button class="pop-scrim" aria-label="Close" onClick=${close}></button>`}
  </div>`;
}

render(html`<${App} />`, document.getElementById('app'));
