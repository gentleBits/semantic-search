// One resume: the card facts on top, the score and its note, the redacted text, the neighbours.
import { html, useEffect, useRef } from './lib.js';
import * as I from './icons.js';
import { num, ordinal, resume, seniorityLine } from './format.js';
import { act, closeDetail, openPerson } from './store.js';

export function Detail({ s }) {
  const d = s.detail;
  const p = d.person;
  const c = s.config;
  const body = useRef(null);
  useEffect(() => { if (body.current) body.current.scrollTop = 0; }, [p && p.id]);
  useEffect(() => {
    const onKey = (e) => {
      if (e.target.matches('input, textarea')) return;
      if (e.key === 'Escape') closeDetail();
      if (p && e.key === 'ArrowLeft' && p.prev) openPerson(p.prev);
      if (p && e.key === 'ArrowRight' && p.next) openPerson(p.next);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [p && p.id, p && p.prev, p && p.next]);

  if (!p) {
    return html`<section class="detail" aria-label=${cap(c.document)}>
      <div class="d-head"><span class="at">…</span><span class="grow"></span>
        <button class="nav close" aria-label="Close" onClick=${closeDetail}><${I.X} /></button></div>
      <div class="d-loading">Opening…</div>
    </section>`;
  }
  const original = p.link && !p.link.startsWith('file:') ? p.link : `/api/sessions/${s.sid}/people/${p.id}/text`;
  const busy = s.busy || !!s.snap.rank.running;
  return html`<section class="detail" aria-label=${cap(c.document)}>
    <div class="d-head">
      <span class="at">${p.in_set ? html`<b>#${num(p.rank)}</b> of ${num(p.of)}` : html`<b>${p.id}</b> · not in this list`}</span>
      <button class="nav" aria-label=${'Previous ' + c.noun} disabled=${!p.prev} onClick=${() => openPerson(p.prev)}><${I.Prev} /></button>
      <button class="nav" aria-label=${'Next ' + c.noun} disabled=${!p.next} onClick=${() => openPerson(p.next)}><${I.Next} /></button>
      <button class="orig ghost" style="border:0;background:transparent" disabled=${busy} title=${`Filter: ${c.nouns} like this one`}
        onClick=${() => act({ type: 'filter', args: { like: p.id } })}><span>More like this</span></button>
      <a class="orig" style="margin-left:0" href=${original} target="_blank" rel="noopener" aria-label="Open original" title="Open original"><span>Open original</span><${I.Out} size=${12} /></a>
      <button class="nav close" aria-label="Close" onClick=${closeDetail}><${I.X} color="var(--ink)" /></button>
    </div>
    <div class="d-body" ref=${body}>
      <div class="d-title">
        <h1>${p.title}</h1>
        <div class="pills">
          ${seniorityLine(p) && html`<span class="pill">${seniorityLine(p)}</span>`}
          ${p.rate != null && html`<span class="pill mono" title=${p.rate_estimated ? 'estimated: this resume states no rate' : null}>${p.rate_label}/h${p.rate_estimated ? ' · estimated' : ''}</span>`}
          ${(p.location || p.remote) && html`<span class="pill">${p.location_full || p.location || ''}${p.remote && html`<${I.Globe} size=${11} color="var(--ink-2)" />remote ok`}</span>`}
          ${p.avail && html`<span class=${'pill' + (p.avail === 'now' ? ' blue' : '')}>${p.avail === 'other' ? p.avail_label : 'available ' + (p.avail === 'now' ? 'now' : 'in ' + p.avail_label)}</span>`}
          ${p.versions > 0 && html`<span class="pill">+${p.versions} older version${p.versions > 1 ? 's' : ''}</span>`}
          ${p.by_meaning && html`<span class="pill" title="Found by meaning, not by exact words">≈ found by meaning</span>`}
        </div>
      </div>
      ${p.score != null ? html`<div class="verdict">
        <div class="big"><${I.Star} size=${16} /><span>${p.score}</span></div>
        <div class="w"><b>${p.note || 'No note.'}</b>
          <span>for “${p.criterion}”${p.place ? ` · ${ordinal(p.place)} of ${num(p.judged)}` : ''}</span></div>
      </div>` : s.snap.ranking && p.in_set ? html`<div class="verdict none">Not ranked yet for “${s.snap.ranking.criterion}”.</div>` : null}
      <div class="d-facts">
        <div class="kv"><span class="k">Used in jobs</span><span class="tags">${p.used.length ? p.used.map((k) => html`<span class="used">${k}</span>`) : html`<span class="hint" style="line-height:22px">none found</span>`}</span></div>
        ${p.listed.length > 0 && html`<div class="kv"><span class="k">Listed only</span><span class="tags">${p.listed.map((k) => html`<span class="listed">${k}</span>`)}</span></div>`}
        ${p.did && html`<div class="kv"><span class="k">Did</span><span class="quote">“${p.did}”</span></div>`}
      </div>
      <div class="d-rule"><span class="label">${cap(c.document)}</span><i></i><span class="lock"><${I.Lock} size=${11} />contact details redacted</span></div>
      <article class="resume">${resume(p.markdown, p.title)}</article>
    </div>
  </section>`;
}
const cap = (w) => w[0].toUpperCase() + w.slice(1);
