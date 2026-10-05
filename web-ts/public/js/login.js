// The login: shown when the server requires one and nobody is logged in. A server with sign-up on also offers
// "Create one" (signup.js). It is the project's front door, so it names the project, not a collection.
import { html, useEffect, useRef, useState } from './lib.js';
import * as I from './icons.js';
import { login, openSignup } from './store.js';

export function Login({ s }) {
  const first = useRef(null);
  const [name, setName] = useState('');
  const [pw, setPw] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  useEffect(() => { if (first.current) first.current.focus(); }, []);
  const submit = async (e) => {
    e.preventDefault();
    if (!name.trim() || !pw || busy) return;
    setBusy(true);
    setErr(null);
    const r = await login(name.trim(), pw);
    setBusy(false);
    if (!r.ok) { setErr(r.error.text); setPw(''); }
  };
  return html`<div class="login">
    <form class="login-card" onSubmit=${submit} aria-label="Log in">
      <div class="login-brand"><span class="mark"><${I.Search} size=${12} color="var(--on-accent)" /></span><b>Semantic search</b></div>
      <h2>Log in</h2>
      <p>Your searches are yours alone: log in to come back to them.</p>
      <div class="field">
        <label for="l-name">Email</label>
        <input id="l-name" class="input" ref=${first} value=${name} onInput=${(e) => setName(e.target.value)} autocomplete="username" spellcheck="false" inputmode="email" />
      </div>
      <div class="field">
        <label for="l-pw">Password</label>
        <input id="l-pw" class="input" type="password" value=${pw} onInput=${(e) => setPw(e.target.value)} autocomplete="current-password" />
      </div>
      ${err && html`<span class="form-error" role="alert">${err}</span>`}
      <button class="choice main" type="submit" disabled=${busy || !name.trim() || !pw}><span>${busy ? 'Logging in…' : 'Log in'}</span><span class="n">↵</span></button>
      ${s && s.me && s.me.signup
        ? html`<span class="hint">No account? <button type="button" class="link" onClick=${openSignup}>Create one</button></span>`
        : html`<span class="hint">No account? Ask the person who runs this server.</span>`}
    </form>
  </div>`;
}
