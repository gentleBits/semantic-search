// The sign-up: email → its code → phone → its code → a password, one step at a time on the login
// card. The server keeps the flow and says the step (GET /api/signup only reads: opening or refreshing this page never
// sends a code); this card shows what it says. "Back" goes to fixed steps, never history.back(). The server's one-line
// errors are shown as they are.
import { html, useEffect, useRef, useState } from './lib.js';
import * as I from './icons.js';
import * as api from './api.js';
import { closeSignup, signedUp } from './store.js';

const STEPS = { email: 1, email_code: 2, phone: 3, phone_code: 4, password: 5 };
const clock = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;

export function Signup() {
  const [view, setView] = useState(null);     // what the server says: { step, email, phone, resend_in, sends_left, expired, work_email }
  const [got, setGot] = useState(0);          // when `view` arrived: the resend countdown runs from it
  const [now, setNow] = useState(Date.now());
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [wait, setWait] = useState(null);     // { until } — a WAIT or LIMIT answer's retry_in, counting down
  const [note, setNote] = useState(null);     // one line that is not an error ("A new code is on its way.")
  const [email, setEmail] = useState('');
  const [phone, setPhone] = useState('');
  const [code, setCode] = useState('');
  const [pw, setPw] = useState('');
  const [pw2, setPw2] = useState('');
  const first = useRef(null);

  const take = (v) => { const at = Date.now(); setView(v); setGot(at); setNow(at); setCode(''); };
  useEffect(() => {
    (async () => {
      const r = await api.get('/signup');
      if (!r.ok) { setErr(r.error.text); setView({ step: 'email' }); return; }
      take(r.data);
      if (r.data.expired) setNote('That sign-up ran out — start again.');
    })();
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  useEffect(() => { if (first.current) first.current.focus(); }, [view && view.step]);

  if (!view) return html`<div class="login"><div class="login-card signup-card" aria-busy="true"></div></div>`;
  const step = view.step;
  const resendIn = Math.max(0, (view.resend_in || 0) - Math.floor(Math.max(0, now - got) / 1000));   // the tick may be older than the answer
  const waitLeft = wait ? Math.max(0, Math.ceil((wait.until - now) / 1000)) : 0;

  // one call → the new view, or the server's line; an expired flow goes back to the start
  const send = async (fn, after) => {
    if (busy) return;
    setBusy(true); setErr(null); setNote(null);
    const r = await fn();
    setBusy(false);
    if (!r.ok) {
      const e = r.error;
      if (e.code === 'SIGNUP_EXPIRED') { take({ step: 'email' }); setNote(e.text); return; }
      if (e.code === 'OUT_OF_ORDER' && e.view) take(e.view);
      if (e.code === 'WAIT' && e.retry_in) {
        const at = Date.now();
        setNow(at);
        setWait({ until: at + e.retry_in * 1000 });   // the line counts down by itself and goes at 0
        return;
      }
      setErr(e.text);
      if (e.code === 'WRONG_CODE' || e.code === 'BAD_CODE' || e.code === 'TOO_MANY_TRIES') setCode('');
      return;
    }
    setWait(null);
    if (after) after(r.data); else take(r.data);
  };
  const post = (path, body = {}) => () => api.post('/signup' + path, body);
  const del = (path) => () => api.del('/signup' + path);
  const startOver = () => send(del(''), (d) => { take(d); setEmail(''); setPhone(''); });
  const resend = () => send(post('/resend'), (d) => { take(d); setNote('A new code is on its way — use the newest one.'); });
  const digits = (v) => v.replace(/\D/g, '').slice(0, 6);
  const typed = (v, path) => {
    const d = digits(v);
    setCode(d);
    if (d.length === 6 && !busy) send(post(path, { code: d }));   // a pasted or typed code goes as soon as it is whole
  };

  const submit = (e) => {
    e.preventDefault();
    if (busy) return;
    if (step === 'email') return email.trim() && send(post('/email', { email: email.trim() }));
    if (step === 'email_code') return code.length === 6 && send(post('/email/verify', { code }));
    if (step === 'phone') return phone.trim() && send(post('/phone', { phone: phone.trim() }));
    if (step === 'phone_code') return code.length === 6 && send(post('/phone/verify', { code }));
    if (step === 'password') {
      if (pw !== pw2) { setErr('The two passwords differ.'); return; }
      return send(post('/password', { password: pw }), () => signedUp());
    }
  };

  const codeField = (path) => html`<div class="field">
    <label for="s-code">Code</label>
    <input id="s-code" class="input code-input" ref=${first} value=${code} onInput=${(e) => typed(e.target.value, path)}
      inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]*" maxlength="7" spellcheck="false" placeholder="••••••" aria-describedby="s-help" />
  </div>`;
  const again = () => {
    if (!view.sends_left) return html`<span class="hint">That was the last code for this step — <button type="button" class="link" onClick=${startOver}>start again</button>.</span>`;
    if (resendIn > 0) return html`<span class="hint">Didn't get it? Resend in ${clock(resendIn)}</span>`;
    return html`<span class="hint">Didn't get it? <button type="button" class="link" onClick=${resend} disabled=${busy}>Resend code</button></span>`;
  };

  const what = {
    email: [html`Create an account`, html`Verify your email and your phone, then pick a password.`],
    email_code: [html`Check your email`, html`We sent a 6-digit code to <b>${view.email}</b>. It's good for 10 minutes.`],
    phone: [html`Your phone`, html`We'll text a code to it. One account per number.`],
    phone_code: [html`Check your phone`, html`We sent a 6-digit code to <b>${view.phone}</b>.`],
    password: [html`Pick a password`, html`At least 10 characters. There's no reset yet, so keep it somewhere safe.`],
  }[step];
  const label = {
    email: busy ? 'Sending…' : 'Send code', email_code: busy ? 'Checking…' : 'Verify', phone: busy ? 'Sending…' : 'Send code',
    phone_code: busy ? 'Checking…' : 'Verify', password: busy ? 'Creating…' : 'Create account',
  }[step];
  const ready = {
    email: !!email.trim(), email_code: code.length === 6, phone: !!phone.trim(), phone_code: code.length === 6, password: pw.length > 0 && pw2.length > 0,
  }[step] && !busy && !(waitLeft && (step === 'email' || step === 'phone'));

  return html`<div class="login">
    <form class="login-card signup-card" onSubmit=${submit} aria-label="Create an account" novalidate>
      <div class="login-brand"><span class="mark"><${I.Search} size=${12} color="var(--on-accent)" /></span><b>Semantic search</b><span class="signup-step">Step ${STEPS[step]} of 5</span></div>
      <h2>${what[0]}</h2>
      <p id="s-help">${what[1]}</p>
      ${step === 'email' && html`<div class="field">
        <label for="s-email">${view.work_email === false ? 'Email' : 'Business email'}</label>
        <input id="s-email" class="input" type="email" ref=${first} value=${email} onInput=${(e) => setEmail(e.target.value)} autocomplete="username" spellcheck="false" inputmode="email" />
      </div>`}
      ${step === 'email_code' && codeField('/email/verify')}
      ${step === 'phone' && html`<div class="field">
        <label for="s-phone">Mobile number</label>
        <input id="s-phone" class="input" type="tel" ref=${first} value=${phone} onInput=${(e) => setPhone(e.target.value)} autocomplete="tel" inputmode="tel" placeholder="+40 712 345 678" />
        <span class="help">With your country code.</span>
      </div>`}
      ${step === 'phone_code' && codeField('/phone/verify')}
      ${step === 'password' && html`<div class="field">
        <label for="s-pw">Password</label>
        <input id="s-pw" class="input" type="password" ref=${first} value=${pw} onInput=${(e) => setPw(e.target.value)} autocomplete="new-password" minlength="10" />
      </div>
      <div class="field">
        <label for="s-pw2">The same again</label>
        <input id="s-pw2" class="input" type="password" value=${pw2} onInput=${(e) => setPw2(e.target.value)} autocomplete="new-password" minlength="10" />
      </div>`}
      ${waitLeft > 0 && !err && html`<span class="form-error" role="alert">Wait ${clock(waitLeft)} before asking for another code.</span>`}
      ${err && html`<span class="form-error" role="alert">${err}</span>`}
      ${note && !err && html`<span class="help ok" role="status">${note}</span>`}
      <button class="choice main" type="submit" disabled=${!ready}><span>${label}</span><span class="n">↵</span></button>
      <div class="signup-links">
        ${(step === 'email_code' || step === 'phone_code') && again()}
        ${step === 'email_code' && html`<button type="button" class="link" onClick=${startOver}>Use another email</button>`}
        ${(step === 'phone_code' || step === 'password') && html`<button type="button" class="link" onClick=${() => send(del('/phone'))}>Use another number</button>`}
        ${(step === 'phone' || step === 'password') && html`<button type="button" class="link" onClick=${startOver}>Start over</button>`}
      </div>
      <span class="hint">Have an account? <button type="button" class="link" onClick=${closeSignup}>Log in</button></span>
    </form>
  </div>`;
}
