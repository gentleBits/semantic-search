// The server's routes. JSON in, JSON out; the assistant's turn is a stream of events.

// A 401 from any route means the login cookie is gone (expired, logged out elsewhere, the account removed);
// the store registers what to do then — show the login, keep the page where it was.
let loginRequired = null;
export function onLoginRequired(fn) { loginRequired = fn; }

async function read(res) {
  let data = null;
  try { data = await res.json(); } catch { data = null; }
  if (!res.ok) {
    const error = (data && data.error) || { role: 'error', code: 'HTTP_' + res.status, text: 'The server answered ' + res.status + '.' };
    if (res.status === 401 && error.code === 'LOGIN_REQUIRED' && loginRequired) loginRequired();
    return { ok: false, status: res.status, error, state: data && data.state };
  }
  return { ok: true, status: res.status, data };
}

async function call(method, path, body) {
  try {
    const res = await fetch('/api' + path, {
      method,
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    return await read(res);
  } catch (e) {
    return { ok: false, status: 0, error: { role: 'error', code: 'OFFLINE', text: 'The server cannot be reached. Is `resumes web` still running?' } };
  }
}

export const get = (path) => call('GET', path);
export const post = (path, body = {}) => call('POST', path, body);
export const put = (path, body = {}) => call('PUT', path, body);
export const del = (path) => call('DELETE', path);

// POST, then read `event: …\ndata: {…}\n\n` blocks as they arrive. Resolves when the stream ends.
export async function stream(path, body, onEvent) {
  let res;
  try {
    res = await fetch('/api' + path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  } catch (e) {
    return { ok: false, status: 0, error: { role: 'error', code: 'OFFLINE', text: 'The server cannot be reached. Is `resumes web` still running?' } };
  }
  if (!res.ok || !res.body) return read(res);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buffer.indexOf('\n\n')) !== -1) {
        const block = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);
        const line = block.split('\n').find((l) => l.startsWith('data: '));
        if (!line) continue;
        let ev = null;
        try { ev = JSON.parse(line.slice(6)); } catch { ev = null; }
        if (ev) onEvent(ev);
      }
    }
  } catch (e) {
    return { ok: false, status: 0, error: { role: 'error', code: 'BROKEN', text: 'The connection broke while the assistant was answering.' } };
  }
  return { ok: true, status: 200 };
}
