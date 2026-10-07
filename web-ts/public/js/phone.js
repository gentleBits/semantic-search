// The check's number field: a country (only those the server can text) and the number as that country writes it,
// grouped while it is typed. libphonenumber-js is vendored (its "min" metadata) and loads with the check, not the app.
let L = null;
let examples = {};

export async function load() {
  if (L) return;
  const [, ex] = await Promise.all([import('../vendor/libphonenumber-min.js'), import('../vendor/phone-examples.js')]);
  L = globalThis.libphonenumber;
  examples = ex.default;
}

const names = new Intl.DisplayNames(['en'], { type: 'region' });

// "GB" → 🇬🇧 for the list (Windows shows the two letters; the picker itself shows an image)
const emoji = (c) => String.fromCodePoint(...[...c].map((ch) => 0x1f1a5 + ch.charCodeAt(0)));

/** The list for the picker, by name: [{ code: 'DE', name: 'Germany', dial: '49', flag: '🇩🇪' }, …]. */
export const options = (countries) => countries.filter((c) => L.isSupportedCountry(c))
  .map((c) => ({ code: c, name: names.of(c) || c, dial: L.getCountryCallingCode(c), flag: emoji(c) }))
  .sort((a, b) => a.name.localeCompare(b.name));

/** The visitor's country by the browser's language (en-GB → GB) when it is in the list; else the US, else the first. */
export function guess(countries) {
  for (const tag of navigator.languages || [navigator.language]) {
    try {
      const region = new Intl.Locale(tag).region;
      if (region && countries.includes(region)) return region;
    } catch {}
  }
  return countries.includes('US') ? 'US' : countries[0];
}

// The box shows the country's code, so the field shows what follows it: the number as written after "+40" ("712 034 567"),
// never the country's own trunk 0 ("0712 034 567" next to +40 reads as a wrong number)
function after(nsn, country) {
  const dial = L.getCountryCallingCode(country);
  const out = new L.AsYouType().input(`+${dial}${nsn}`);
  return out.startsWith(`+${dial}`) ? out.slice(dial.length + 1).trim() : nsn;
}

/** The grey example: a real mobile number of the country, as written after its code ("712 034 567", "201 555 0123"). */
export function example(country) {
  const n = country && L.getExampleNumber(country, examples);
  return n ? after(n.nationalNumber, country) : '';
}

const intl = (t) => /^\s*(\+|00)/.test(t);

// the country's trunk prefix, dialled only inside it: 0 in most of Europe, 06 in Hungary, none in Italy or the US
// (its example's national form, minus the number itself)
function trunk(country) {
  const n = L.getExampleNumber(country, examples);
  const d = n ? n.formatNational().replace(/\D/g, '') : '';
  return n && d.endsWith(n.nationalNumber) ? d.slice(0, d.length - n.nationalNumber.length) : '';
}

// the digits that follow the country's code: what was typed, less a trunk prefix typed out of habit
function rest(text, country) {
  const d = text.replace(/\D/g, '');
  const t = trunk(country);
  return t && d.startsWith(t) ? d.slice(t.length) : d;
}

// what was typed for a country → as written after its code (a lone 0 stays, so the key press shows)
function group(text, country) {
  const d = rest(text, country);
  return d ? after(d, country) : text.trim();
}

/** What was typed → { country, text }. Grouped when typed at the end (an edit in the middle stays as it is). A number
 * typed with its + code takes its country, when that is in the list, and drops the code. */
export function typed(text, country, countries, atEnd) {
  if (intl(text)) {
    const a = new L.AsYouType();
    const shown = a.input(text.trim().replace(/^00/, '+'));
    const c = a.getCountry();
    if (c && countries.includes(c)) return { country: c, text: after(a.getNationalNumber(), c) };
    return { country, text: atEnd ? shown : text };
  }
  return { country, text: atEnd && country ? group(text, country) : text };
}

/** The server's "+40 712 345 678" → { country, text } for the field; country null when it is not in the list. */
export function split(international, countries) {
  const p = L.parsePhoneNumberFromString(international || '');
  if (p && p.country && countries.includes(p.country)) return { country: p.country, text: after(p.nationalNumber, p.country) };
  return { country: null, text: international || '' };
}

/** The number for the server: the country's code and the digits after it, exactly what the box and the field show;
 * a number typed with its own + code goes as typed. */
export function full(text, country) {
  const t = text.trim();
  if (intl(t) || !country) return t;
  return `+${L.getCountryCallingCode(country)}${rest(t, country)}`;
}
