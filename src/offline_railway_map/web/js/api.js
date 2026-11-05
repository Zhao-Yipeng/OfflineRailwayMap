// Talking to the local backend (SPEC §10.1, §10.2).
// Every path carries the per-run token, which is the first path segment of
// the page URL: /<token>/app/index.html

export const BASE = location.pathname.split('/app/')[0];
export const ORIGIN = location.origin;

/** Absolute URL for a backend path (tile URLs must be absolute for map workers). */
export function url(path) {
  return `${ORIGIN}${BASE}${path}`;
}

// Online tiles come from a second local port (same token), see SPEC §10.1.
let onlineOrigin = ORIGIN;
export function setOnlineOrigin(origin) {
  onlineOrigin = origin;
}
export function onlineUrl(path) {
  return `${onlineOrigin}${BASE}${path}`;
}

async function request(method, path, body) {
  const init = { method, headers: {} };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  const res = await fetch(url(path), init);
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const error = new Error((data && data.error) || `HTTP ${res.status}`);
    error.status = res.status;
    error.data = data;
    throw error;
  }
  return data;
}

export const api = {
  get: (path) => request('GET', path),
  post: (path, body = {}) => request('POST', path, body),
};

/** Subscribe to backend events. `handlers` maps event names to callbacks. */
export function subscribe(handlers) {
  const source = new EventSource(url('/api/events'));
  for (const [name, fn] of Object.entries(handlers)) {
    source.addEventListener(name, (ev) => fn(JSON.parse(ev.data)));
  }
  return source;
}

/** Open a link in the system browser. */
export function openExternal(link) {
  return api.post('/api/open-external', { url: link });
}
