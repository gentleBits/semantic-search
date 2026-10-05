// Places in the page that optional features (public/features/<name>.js) fill; an empty slot renders nothing.
import { html } from './lib.js';

const slots = {};

export function fill(name, Component) {
  (slots[name] ||= []).push(Component);
}

export function Slot({ name, s }) {
  return (slots[name] || []).map((C) => html`<${C} s=${s} />`);
}

export async function loadFeatures(names) {
  for (const n of names || []) {
    try {
      await import(`/features/${encodeURIComponent(n)}.js`);
    } catch (e) {
      console.error(`feature ${n} did not load`, e);
    }
  }
}
