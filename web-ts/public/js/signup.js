// The check before anyone may search: an email address and a phone number, every time. A code goes to the address
// every time; a number seen for the first time gets a code by text too. No password and nothing to make: the server
// keeps the flow and says the step (GET /api/signup only reads: opening or refreshing this page never sends a code);
// this card shows what it says. "Back" goes to fixed steps, never history.back(). The server's one-line errors are
// shown as they are.
import { html, useEffect, useRef, useState } from './lib.js';
import * as I from './icons.js';
import * as api from './api.js';
import { checked } from './store.js';

const clock = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;

export function Check() {
  const [view, setView] = useState(null);     // what the server says: { step, email, phone, resend_in, sends_left, expired, work_email }
  const [got, setGot] = useState(0);          // when `view` arrived: the resend countdown runs from it
  const [now, setNow] = useState(Date.now());
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [wait, setWait] = useState(null);     // { until } — a WAIT answer's retry_in, counting down
  const [note, setNote] = useState(null);     // one line that is not an error ("A new code is on its way.")
  const [email, setEmail] = useState('');
  const [phone, setPhone] = useState('');
  const [code, setCode] = useState('');
  const [workEmail, setWorkEmail] = useState(true);
  const first = useRef(null);

  const take = (v) => {
    const at = Date.now();
    setView(v); setGot(at); setNow(at); setCode('');
    if (v.work_email !== undefined) setWorkEmail(v.work_email !== false);
    if (v.email) setEmail(v.email);
    if (v.phone) setPhone(v.phone);
  };
  useEffect(() => {
    (async () => {
      const r = await api.get('/signup');
      if (!r.ok) { setErr(r.error.text); setView({ step: 'details' }); return; }
      take(r.data);
      if (r.data.expired) setNote('That took too long. Please start again.');
    })();
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  useEffect(() => { if (first.current) first.current.focus(); }, [view && view.step]);

  if (!view) return html`<div class="login"><div class="login-card signup-card" aria-busy="true"></div></div>`;
  const step = view.step;
  const resendIn = Math.max(0, (view.resend_in || 0) - Math.floor(Math.max(0, now - got) / 1000));   // the tick may be older than the answer
  const waitLeft = wait ? Math.max(0, Math.ceil((wait.until - now) / 1000)) : 0;

  // one call → the next step, in, or the server's line (with the step it leaves the flow on); an expired flow starts over
  const send = async (fn, after) => {
    if (busy) return;
    setBusy(true); setErr(null); setNote(null);
    const r = await fn();
    setBusy(false);
    if (!r.ok) {
      const e = r.error;
      if (e.code === 'SIGNUP_EXPIRED') { take({ step: 'details' }); setNote(e.text); return; }
      if (e.view) take(e.view);
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
    if (r.data && r.data.done) { await checked(); return; }
    if (after) after(r.data); else take(r.data);
  };
  const post = (path, body = {}) => () => api.post('/signup' + path, body);
  const del = (path) => () => api.del('/signup' + path);
  const startOver = () => send(del(''), (d) => take(d));
  const resend = () => send(post('/resend'), (d) => { take(d); setNote('A new code is on its way. Please use the newest one.'); });
  const typed = (v, path) => {
    const d = v.replace(/\D/g, '').slice(0, 6);
    setCode(d);
    if (d.length === 6 && !busy) send(post(path, { code: d }));   // a pasted or typed code goes as soon as it is whole
  };

  const submit = (e) => {
    e.preventDefault();
    if (busy) return;
    if (step === 'details') return email.trim() && phone.trim() && send(post('/start', { email: email.trim(), phone: phone.trim() }));
    if (step === 'email_code') return code.length === 6 && send(post('/email/verify', { code }));
    if (step === 'phone') return phone.trim() && send(post('/phone', { phone: phone.trim() }));
    if (step === 'phone_code') return code.length === 6 && send(post('/phone/verify', { code }));
  };

  const codeField = (path) => html`<div class="field">
    <label for="s-code">Code</label>
    <input id="s-code" class="input code-input" ref=${first} value=${code} onInput=${(e) => typed(e.target.value, path)}
      inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]*" maxlength="7" spellcheck="false" placeholder="••••••" aria-describedby="s-help" />
  </div>`;
  const phoneField = (ref) => html`<div class="field">
    <label for="s-phone">Mobile number</label>
    <input id="s-phone" class="input" type="tel" ref=${ref} value=${phone} onInput=${(e) => setPhone(e.target.value)} autocomplete="tel" inputmode="tel" placeholder="+40 712 345 678" />
    <span class="help">With your country code.</span>
  </div>`;
  const again = () => {
    if (!view.sends_left) return html`<span class="hint">That was the last code we can send this time. <button type="button" class="link" onClick=${startOver}>Start again</button></span>`;
    if (resendIn > 0) return html`<span class="hint">Didn't get it? Resend in ${clock(resendIn)}</span>`;
    return html`<span class="hint">Didn't get it? <button type="button" class="link" onClick=${resend} disabled=${busy}>Resend code</button></span>`;
  };

  const what = {
    details: [html`Before you search`, html`We need to make sure our service isn't being abused, so please provide your email and phone number.`],
    email_code: [html`Check your email`, html`We sent a 6-digit code to <b>${view.email}</b>.`],
    phone: [html`Your mobile number`, html`Your email is confirmed. We'll text a code to this number — only the first time we see it.`],
    phone_code: [html`Check your phone`, html`This number is new to us, so we also texted a code to <b>${view.phone}</b>. You'll only need to do this once.`],
  }[step];
  const label = busy ? (step === 'email_code' || step === 'phone_code' ? 'Checking…' : 'Sending…') : 'Continue';
  const ready = {
    details: !!email.trim() && !!phone.trim(), email_code: code.length === 6, phone: !!phone.trim(), phone_code: code.length === 6,
  }[step] && !busy && !(waitLeft && step === 'phone');

  return html`<div class="login">
    <form class="login-card signup-card" onSubmit=${submit} aria-label="Confirm your email and phone" novalidate>
      <div class="login-brand"><span class="mark"><${I.Search} size=${12} color="var(--on-accent)" /></span><b>Semantic search</b></div>
      <h2>${what[0]}</h2>
      <p id="s-help">${what[1]}</p>
      ${step === 'details' && html`<div class="field">
        <label for="s-email">${workEmail ? 'Business email' : 'Email'}</label>
        <input id="s-email" class="input" type="email" ref=${first} value=${email} onInput=${(e) => setEmail(e.target.value)} autocomplete="email" spellcheck="false" inputmode="email" />
      </div>
      ${phoneField(null)}`}
      ${step === 'email_code' && codeField('/email/verify')}
      ${step === 'phone' && phoneField(first)}
      ${step === 'phone_code' && codeField('/phone/verify')}
      ${waitLeft > 0 && !err && html`<span class="form-error" role="alert">Please wait ${clock(waitLeft)} before asking for another code.</span>`}
      ${err && html`<span class="form-error" role="alert">${err}</span>`}
      ${note && !err && html`<span class="help ok" role="status">${note}</span>`}
      <button class="choice main" type="submit" disabled=${!ready}><span>${label}</span><span class="n">↵</span></button>
      ${step === 'details' && html`<span class="hint">We'll email you a code. The first time, we'll also text one to your phone.</span>`}
      <div class="signup-links">
        ${(step === 'email_code' || step === 'phone_code') && again()}
        ${step === 'email_code' && html`<button type="button" class="link" onClick=${startOver}>Change email or number</button>`}
        ${step === 'phone_code' && html`<button type="button" class="link" onClick=${() => send(del('/phone'))}>Use another number</button>`}
        ${step === 'phone' && html`<button type="button" class="link" onClick=${startOver}>Start over</button>`}
      </div>
    </form>
  </div>`;
}
