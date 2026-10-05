// Numbers and words, the way the design writes them.
import { html } from './lib.js';

export const num = (n) => (n == null ? '' : Number(n).toLocaleString('en-US'));
export const plural = (cfg, n) => (n === 1 ? cfg.noun : cfg.nouns);
export const people = (cfg, n) => `${num(n)} ${plural(cfg, n)}`;
export const chipText = (f) => (f.kind ? `${f.kind}: ${f.value}` : String(f.value));
export const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);

export function ordinal(n) {
  const v = n % 100;
  if (v >= 11 && v <= 13) return n + 'th';
  return n + (['th', 'st', 'nd', 'rd'][n % 10] || 'th');
}

export function clock(seconds) {
  const s = Math.max(0, Math.round(seconds));
  return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
}

export function ago(iso) {
  if (!iso) return '';
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return 'just now';
  if (s < 3600) return Math.floor(s / 60) + ' min ago';
  if (s < 86400) return Math.floor(s / 3600) + ' h ago';
  if (s < 7 * 86400) return Math.floor(s / 86400) + ' d ago';
  return new Date(iso).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
}

// The facts of a row in one line: `11y · €77 · Bucharest · now`
export function factsLine(i) {
  return [i.years_label !== '—' ? i.years_label : null, i.rate_label !== '—' ? i.rate_label : null, i.location, i.avail_label]
    .filter(Boolean).join(' · ');
}

export function seniorityLine(i) {
  const bits = [];
  if (i.seniority) bits.push(cap(i.seniority));
  if (i.years != null) bits.push(`${Math.round(i.years)} year${Math.round(i.years) === 1 ? '' : 's'}`);
  return bits.join(' · ');
}

// 1 2 3 … 17   ·   1 … 7 8 9 … 17
export function pageList(page, pages) {
  if (pages <= 7) return Array.from({ length: pages }, (_, i) => i + 1);
  const keep = new Set([1, pages, page - 1, page, page + 1]);
  if (page <= 2) [1, 2, 3].forEach((p) => keep.add(p));
  if (page >= pages - 1) [pages - 2, pages - 1, pages].forEach((p) => keep.add(p));
  const list = [...keep].filter((p) => p >= 1 && p <= pages).sort((a, b) => a - b);
  const out = [];
  list.forEach((p, i) => {
    if (i && p - list[i - 1] > 1) out.push(null);
    out.push(p);
  });
  return out;
}

// **bold** and #3 (a link to that person) inside the assistant's sentences. Text only: nothing is parsed as HTML.
export function rich(text, refs, onRef) {
  const paras = String(text || '').split(/\n{2,}/).filter((p) => p.trim());
  return paras.map((p) => html`<p>${inline(p, refs, onRef)}</p>`);
}

export function inline(text, refs, onRef) {
  const out = [];
  const re = /\*\*([^*]+)\*\*|#(\d{1,4})\b|\n/g;
  let last = 0;
  let m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    if (m[1] != null) out.push(html`<strong>${inline(m[1], refs, onRef)}</strong>`);
    else if (m[2] != null) {
      const id = refs && refs[m[2]];
      out.push(id && onRef
        ? html`<button class="ref" onClick=${() => onRef(id)} aria-label=${'Open number ' + m[2]}>#${m[2]}</button>`
        : '#' + m[2]);
    } else out.push(html`<br />`);
    last = re.lastIndex;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

const emphasis = (text) => {
  const out = [];
  const re = /\*\*([^*]+)\*\*|\*([^*\n]+)\*|_([^_\n]+)_/g;
  let last = 0;
  let m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    out.push(m[1] != null ? html`<strong>${m[1]}</strong>` : html`<em>${m[2] != null ? m[2] : m[3]}</em>`);
    last = re.lastIndex;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
};

// A resume in the corpus's markdown → the design's article: section labels, jobs with their dates on the right, bullets.
// Headings, paragraphs, lists, bold and italics; anything else is shown as the text it is.
export function resume(md, title) {
  const lines = String(md || '').replace(/\r/g, '').split('\n');
  const blocks = [];
  let para = [];
  let list = null;
  let job = null;
  const flushPara = () => { if (para.length) { (job ? job.body : blocks).push({ t: 'p', text: para.join(' ') }); para = []; } };
  const flushList = () => { if (list) { (job ? job.body : blocks).push({ t: 'ul', items: list }); list = null; } };
  const flushJob = () => { flushPara(); flushList(); if (job) { blocks.push(job); job = null; } };
  lines.forEach((raw, n) => {
    const line = raw.trim();
    if (!line) { flushPara(); flushList(); return; }
    let m;
    if ((m = /^#\s+(.*)$/.exec(line))) {
      flushJob();
      const same = title && m[1].toLowerCase().replace(/\s*\([^)]*\)\s*$/, '') === title.toLowerCase();
      if (!(n < 3 && (same || blocks.length === 0))) blocks.push({ t: 'h', text: m[1] });
      return;
    }
    if ((m = /^##\s+(.*)$/.exec(line))) { flushJob(); blocks.push({ t: 'h', text: m[1] }); return; }
    if ((m = /^#{3,}\s+(.*)$/.exec(line))) { flushJob(); job = { t: 'job', title: m[1], where: null, when: null, body: [] }; return; }
    if (job && !job.where && !job.body.length && !para.length && (m = /^\*([^*]+)\*$/.exec(line))) {
      const parts = m[1].split(' · ');
      job.when = parts.length > 1 ? parts.pop().replace(/\bto\b/i, '–') : null;
      job.where = parts.join(' · ').replace(/^Company Name — City, State$/, '').replace(/^Company Name — /, '');
      return;
    }
    if ((m = /^[-*•]\s+(.*)$/.exec(line))) { flushPara(); (list = list || []).push(m[1]); return; }
    flushList();
    para.push(line);
  });
  flushJob();
  const body = (items) => items.map((b) => (b.t === 'ul'
    ? html`<ul>${b.items.map((i) => html`<li>${emphasis(i)}</li>`)}</ul>`
    : html`<p>${emphasis(b.text)}</p>`));
  return blocks.map((b) => {
    if (b.t === 'h') return html`<h3>${b.text}</h3>`;
    if (b.t === 'job') {
      return html`<div class="job">
        <div class="job-h"><b>${b.title}${b.where ? ' · ' + b.where : ''}</b>${b.when && html`<span class="when">${b.when}</span>`}</div>
        ${body(b.body)}
      </div>`;
    }
    return body([b]);
  });
}
