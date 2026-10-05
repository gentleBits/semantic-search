// The design's icons: 24-unit stroke drawings, sized where they are used. The colour is set as a style, so a theme
// token (var(--ink-3)) follows light and dark.
import { html } from './lib.js';

const stroke = (d, o = {}) => ({ size = 14, width = o.width || 1.8, color = 'currentColor', cls = '', label } = {}) => html`
  <svg class=${cls} width=${size} height=${size} viewBox="0 0 24 24" fill="none" style=${'stroke:' + color} stroke-width=${width}
    stroke-linecap="round" stroke-linejoin=${o.join || 'round'} aria-hidden=${label ? null : 'true'} aria-label=${label} role=${label ? 'img' : null}>
    ${d.map((p) => (p[0] === 'c' ? html`<circle cx=${p[1]} cy=${p[2]} r=${p[3]} />` : p[0] === 'r'
      ? html`<rect x=${p[1]} y=${p[2]} width=${p[3]} height=${p[4]} rx=${p[5]} />` : html`<path d=${p} />`))}
  </svg>`;

const STAR = 'M12 2.8l2.8 5.9 6.4.8-4.7 4.4 1.2 6.4L12 17.2l-5.7 3.1 1.2-6.4L2.8 9.5l6.4-.8z';

export const Search = stroke([['c', 10.5, 10.5, 6], 'M15 15l5 5'], { width: 2.4 });
export const Down = stroke(['M6 9l6 6 6-6'], { width: 2 });
export const Right = stroke(['M9 6l6 6-6 6'], { width: 2.4 });
export const Prev = stroke(['M15 6l-6 6 6 6'], { width: 2 });
export const Next = stroke(['M9 6l6 6-6 6'], { width: 2 });
export const Clock = stroke([['c', 12, 12, 9], 'M12 7v5l3 2']);
export const Plus = stroke(['M12 5v14M5 12h14']);
export const X = stroke(['M6 6l12 12M18 6L6 18'], { width: 2.2 });
export const Undo = stroke(['M9 14L4 9l5-5', 'M4 9h10.5a5.5 5.5 0 010 11H11']);
export const Sort = stroke(['M7 4v16M3 16l4 4 4-4M17 20V4M13 8l4-4 4 4']);
export const Info = stroke([['c', 12, 12, 9], 'M12 11v5M12 8h.01'], { width: 2 });
export const Clip = stroke(['M21 11.5l-8.5 8.5a5 5 0 01-7-7l9-9a3.5 3.5 0 015 5l-9 9a2 2 0 01-3-3l8-8']);
export const Send = stroke(['M12 19V5M5 12l7-7 7 7'], { width: 2.2 });
export const Arrow = stroke(['M5 12h14M13 6l6 6-6 6'], { width: 2 });
export const Globe = stroke([['c', 12, 12, 9], 'M3 12h18M12 3a14 14 0 010 18M12 3a14 14 0 000 18']);
export const Cursor = stroke(['M5 3l14 7-6 2-2 6z'], { width: 2 });
export const Warn = stroke(['M12 3l9.5 17h-19z', 'M12 10v4M12 17h.01'], { width: 2 });
export const Check = stroke(['M5 12l5 5L20 7'], { width: 2.4 });
export const Doc = stroke(['M14 3H7a2 2 0 00-2 2v14a2 2 0 002 2h10a2 2 0 002-2V8z', 'M14 3v5h5']);
export const Out = stroke(['M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 01-1 1H5a1 1 0 01-1-1V7a1 1 0 011-1h5'], { width: 2 });
export const Lock = stroke([['r', 5, 11, 14, 9, 2], 'M8 11V8a4 4 0 018 0v3'], { width: 2 });
export const Trash = stroke(['M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 002 2h6a2 2 0 002-2l1-12M9 7V4h6v3']);
export const Arc = stroke(['M12 3a9 9 0 019 9'], { width: 2.4 });
export const Gear = stroke([['c', 12, 12, 3], 'M19.4 15a1.7 1.7 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.8-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 11-4 0v-.1a1.7 1.7 0 00-1.1-1.5 1.7 1.7 0 00-1.8.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.8 1.7 1.7 0 00-1.5-1H3a2 2 0 110-4h.1a1.7 1.7 0 001.5-1.1 1.7 1.7 0 00-.3-1.8l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.8.3H9a1.7 1.7 0 001-1.5V3a2 2 0 114 0v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.8-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.8V9a1.7 1.7 0 001.5 1H21a2 2 0 110 4h-.1a1.7 1.7 0 00-1.5 1z']);
export const StarLine = stroke([STAR], { width: 1.8 });
export const Moon = stroke(['M20.5 13.2A8.5 8.5 0 1110.8 3.5a6.6 6.6 0 009.7 9.7z']);
export const Sun = stroke([['c', 12, 12, 4], 'M12 2.5v2M12 19.5v2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M2.5 12h2M19.5 12h2M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4']);

export const Star = ({ size = 12, color = 'var(--rank)' } = {}) => html`
  <svg width=${size} height=${size} viewBox="0 0 24 24" style=${'fill:' + color} aria-hidden="true"><path d=${STAR} /></svg>`;
export const Square = ({ size = 10, color = 'var(--surface)' } = {}) => html`
  <svg width=${size} height=${size} viewBox="0 0 24 24" style=${'fill:' + color} aria-hidden="true"><rect x="4" y="4" width="16" height="16" rx="2" /></svg>`;
