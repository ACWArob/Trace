/* The web keeps browser cookies; native HTTP owns its own HttpOnly cookie jar.
 * This adapter never reads, stores, or copies session cookies into JavaScript. */
export function validateApiOrigin(origin, {development = false} = {}) {
  if (typeof origin !== 'string' || !origin) throw new Error('No Trace server connected. Configure an HTTPS server for this build first.');
  let url;
  try { url = new URL(origin); } catch { throw new Error('Trace server must be an exact HTTPS origin.'); }
  const local = ['localhost', '127.0.0.1', '10.0.2.2'].includes(url.hostname);
  if (origin !== url.origin || url.username || url.password ||
      (url.protocol !== 'https:' && !(development && local && url.protocol === 'http:'))) {
    throw new Error('Trace server must be an exact HTTPS origin (local HTTP is development only).');
  }
  return url.origin;
}

// A native WebView keeps the same localhost storage origin across app updates.
// Pin its private cache to one backend so user-ID collisions cannot replay a
// previous server's queued library edits into another server's account.
export function pinNativeOrigin(storage, origin) {
  if (origin === '') return;
  if (typeof origin !== 'string') throw new Error('Trace server configuration is invalid.');
  const key = 'trace.native-server.v1';
  let prior;
  try { prior = storage.getItem(key); } catch {
    throw new Error('Trace cannot verify the saved server. Network access is paused; your offline library is kept.');
  }
  if (prior !== null && prior !== origin) {
    throw new Error('This Trace installation has data from a different server. Network access is paused to protect your offline library.');
  }
  if (prior === origin) return;
  try {
    storage.setItem(key, origin);
    if (storage.getItem(key) !== origin) throw new Error('Server marker was not saved.');
  } catch {
    throw new Error('Trace cannot save its server connection safely. Network access is paused; your offline library is kept.');
  }
}

function apiUrl(path, origin) {
  if (typeof path !== 'string' || !path.startsWith('/api/') || /[\\#\x00-\x20\x7f]/.test(path)) {
    throw new TypeError('Only relative Trace API paths are allowed.');
  }
  const pathname = path.split('?')[0];
  for (const segment of pathname.split('/')) {
    let decoded = segment;
    try {
      for (let round = 0; round < 3; round++) decoded = decodeURIComponent(decoded);
    } catch { throw new TypeError('Invalid Trace API path.'); }
    if (decoded === '.' || decoded === '..' || /[/\\%\x00-\x20\x7f]/.test(decoded)) {
      throw new TypeError('Invalid Trace API path.');
    }
  }
  const url = new URL(path, origin);
  if (url.origin !== origin || url.pathname !== pathname) throw new TypeError('Invalid Trace API path.');
  return url.href;
}

const aborted = () => new DOMException('The request was aborted.', 'AbortError');

function nativeResponse(result, origin) {
  if (!result || !Number.isInteger(result.status) || result.status < 100 || result.status > 599) {
    throw new Error('Trace could not read the server response.');
  }
  if (result.url) {
    let final;
    try { final = new URL(result.url); } catch { throw new Error('Trace returned an invalid response URL.'); }
    if (final.origin !== origin) throw new Error('Trace refused a response from a different server.');
  }
  const headers = new Headers(result.headers || {});
  headers.delete('set-cookie');
  headers.delete('set-cookie2');
  const body = typeof result.data === 'string' ? result.data :
    result.data === undefined ? '' : JSON.stringify(result.data);
  return {
    ok: result.status >= 200 && result.status < 300,
    status: result.status,
    headers,
    url: result.url || origin,
    async text() { return body; },
    async json() { return JSON.parse(body); },
  };
}

export function createApiFetch({native = false, origin = '', development = false,
  nativeRequest, webFetch = globalThis.fetch, timeout = 30000} = {}) {
  if (!native) return (path, init) => webFetch(path, init);
  if (!Number.isFinite(timeout) || timeout < 1000 || timeout > 60000) {
    throw new TypeError('Trace request timeout must be between 1000 and 60000 milliseconds.');
  }
  // Validate lazily so a build without a server can still open its local shelf.
  return async (path, init = {}) => {
    const backend = validateApiOrigin(origin, {development});
    const url = apiUrl(path, backend);
    if (typeof nativeRequest !== 'function') throw new Error('Native server connection is unavailable.');
    if (init.signal?.aborted) throw aborted();
    const method = (init.method || 'GET').toUpperCase();
    if (!['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) throw new TypeError('Unsupported Trace request method.');
    const headers = new Headers(init.headers || {});
    if (headers.has('cookie') || headers.has('cookie2')) throw new TypeError('Native HTTP manages Trace session cookies.');
    headers.set('Origin', backend);
    let data;
    if (init.body !== undefined && init.body !== null) {
      if (method === 'GET' || method === 'HEAD' || typeof init.body !== 'string' ||
          !/^application\/json(?:\s*;|$)/i.test(headers.get('content-type') || '')) {
        throw new TypeError('Trace native requests support JSON bodies only.');
      }
      data = JSON.parse(init.body);
    }
    // Capacitor cannot cancel an in-flight native request. Abort rejects promptly
    // and ignores the result, but an already submitted write may still reach SQL.
    let onAbort;
    const pending = Promise.resolve().then(() => {
      if (init.signal?.aborted) throw aborted();
      return nativeRequest({url, method,
        headers: Object.fromEntries(headers), ...(data !== undefined ? {data} : {}),
        connectTimeout: timeout, readTimeout: timeout, responseType: 'text',
        disableRedirects: true,
      });
    });
    const abort = new Promise((_, reject) => {
      if (!init.signal) return;
      onAbort = () => reject(aborted());
      init.signal.addEventListener('abort', onAbort, {once: true});
      if (init.signal.aborted) onAbort();
    });
    try {
      const result = await Promise.race([pending, abort]);
      if (init.signal?.aborted) throw aborted();
      return nativeResponse(result, backend);
    } finally {
      if (onAbort) init.signal.removeEventListener('abort', onAbort);
    }
  };
}
