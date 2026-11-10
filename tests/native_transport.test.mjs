import test from 'node:test';
import assert from 'node:assert/strict';
import {createApiFetch, validateApiOrigin, pinNativeOrigin} from '../native_transport.mjs';

test('web delegates unchanged to browser fetch', async () => {
  const init = {credentials: 'same-origin'}, result = {};
  const fetch = createApiFetch({webFetch: (path, options) => {
    assert.equal(path, '/api/auth/session'); assert.equal(options, init); return result;
  }});
  assert.equal(await fetch('/api/auth/session', init), result);
});

test('native validates exact secure origins and restricts development HTTP', () => {
  assert.equal(validateApiOrigin('https://trace.example:8443'), 'https://trace.example:8443');
  for (const origin of ['', 'https://trace.example/', 'https://trace.example/path',
    'https://trace.example?q=1', 'https://user:pass@trace.example', 'http://trace.example',
    'http://127.0.0.1:5173', 'https://TRACE.example', 'https://trace.example:443']) {
    assert.throws(() => validateApiOrigin(origin));
  }
  for (const host of ['localhost', '127.0.0.1', '10.0.2.2']) {
    assert.equal(validateApiOrigin(`http://${host}:5177`, {development: true}), `http://${host}:5177`);
  }
  assert.throws(() => validateApiOrigin('http://192.168.1.10:5177', {development: true}));
});

test('native retains JSON and CSRF and uses native cookie handling', async () => {
  let options;
  const fetch = createApiFetch({native: true, origin: 'https://trace.example', nativeRequest: async value => {
    options = value; return {status: 201, headers: {'Content-Type': 'application/json', 'Set-Cookie': 'private'}, data: {accepted: [{id: 'a'}]}, url: value.url};
  }});
  const response = await fetch('/api/library/sync', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Trace-CSRF': 'test-only'}, body: JSON.stringify({changes: [{id: 'a', baseRevision: 0}]})});
  assert.equal(options.url, 'https://trace.example/api/library/sync');
  assert.equal(options.headers.origin, 'https://trace.example');
  assert.equal(options.headers['x-trace-csrf'], 'test-only');
  assert.deepEqual(options.data, {changes: [{id: 'a', baseRevision: 0}]});
  assert.equal(options.disableRedirects, true);
  assert.equal(options.connectTimeout, 30000);
  assert.equal(options.readTimeout, 30000);
  assert.equal(response.status, 201); assert.equal(response.ok, true);
  assert.equal(response.headers.get('set-cookie'), null);
  assert.deepEqual(await response.json(), {accepted: [{id: 'a'}]});
});

test('native rejects absolute, traversal, encoded separator and malformed paths before transport', async () => {
  let calls = 0;
  const fetch = createApiFetch({native: true, origin: 'https://trace.example', nativeRequest: () => {calls++;}});
  for (const path of ['https://evil.example/api/x', '//evil.example/api/x', '/other', '/api/../auth',
    '/api/%2e%2e/auth', '/api/%252e%252e/auth', '/api/a%2fb', '/api/a%5cb', '/api/%zz',
    '/api/x#fragment', '/api/x\nheader', '/api/\\evil', '/api/a b']) {
    await assert.rejects(fetch(path));
  }
  assert.equal(calls, 0);
});

test('native query strings remain encoded and error responses retain status and body', async () => {
  const fetch = createApiFetch({native: true, origin: 'https://trace.example', nativeRequest: async value => {
    assert.equal(value.url, 'https://trace.example/api/search?q=A%20%26%20B&type=movie');
    return {status: 401, data: '{"error":"Sign in again"}'};
  }});
  const response = await fetch('/api/search?q=A%20%26%20B&type=movie');
  assert.equal(response.ok, false); assert.equal(response.status, 401);
  assert.equal(await response.text(), '{"error":"Sign in again"}');
  assert.deepEqual(await response.json(), {error: 'Sign in again'});
});

test('native refuses cross-server redirects, manual cookies, non-JSON and malformed responses', async () => {
  const make = result => createApiFetch({native: true, origin: 'https://trace.example', nativeRequest: async () => result});
  await assert.rejects(make({status: 200, url: 'https://evil.example/api/x', data: {}})('/api/x'));
  await assert.rejects(make({status: 0})('/api/x'));
  await assert.rejects(make({status: 200})('/api/x', {headers: {Cookie: 'private'}}));
  await assert.rejects(make({status: 200})('/api/x', {method: 'POST', body: 'text'}));
  await assert.rejects(make({status: 200})('/api/x', {method: 'GET', headers: {'Content-Type': 'application/json'}, body: '{}'}));
  await assert.rejects(make({status: 200, data: '<html>error</html>'})('/api/x').then(response => response.json()));
});

test('aborts before dispatch and rejects an in-flight request without exposing its result', async () => {
  const early = new AbortController(); early.abort();
  let calls = 0, finish;
  const fetch = createApiFetch({native: true, origin: 'https://trace.example', nativeRequest: () => {calls++; return new Promise(resolve => {finish = resolve;});}});
  await assert.rejects(fetch('/api/x', {signal: early.signal}), {name: 'AbortError'});
  assert.equal(calls, 0);
  const controller = new AbortController();
  const request = fetch('/api/x', {signal: controller.signal});
  await Promise.resolve(); controller.abort();
  await assert.rejects(request, {name: 'AbortError'});
  finish({status: 200, data: '{}'});
});

test('missing configuration preserves construction for a usable local shelf', async () => {
  const fetch = createApiFetch({native: true});
  await assert.rejects(fetch('/api/auth/session'), /No Trace server connected/);
  assert.throws(() => createApiFetch({native: true, timeout: 0}), /timeout/);
  assert.throws(() => createApiFetch({native: true, timeout: Infinity}), /timeout/);
});

function markerStorage() {
  const values = new Map();
  return {values, writes: 0, getItem(key) { return values.get(key) ?? null; },
    setItem(key, value) { this.writes++; values.set(key, value); }};
}

test('native server pin persists once and preserves same-server and offline access', () => {
  const storage = markerStorage();
  pinNativeOrigin(storage, 'https://trace.example');
  assert.equal(storage.values.get('trace.native-server.v1'), 'https://trace.example');
  pinNativeOrigin(storage, 'https://trace.example');
  pinNativeOrigin(storage, '');
  assert.equal(storage.writes, 1);
  // Offline builds need no writable/readable storage marker.
  pinNativeOrigin(null, '');
});

test('native server changes fail closed without replacing cache identity', () => {
  const storage = markerStorage();
  storage.values.set('private-library', 'preserved');
  pinNativeOrigin(storage, 'https://first.example');
  assert.throws(() => pinNativeOrigin(storage, 'https://second.example'), /different server/);
  assert.equal(storage.values.get('trace.native-server.v1'), 'https://first.example');
  assert.equal(storage.values.get('private-library'), 'preserved');
  assert.equal(storage.writes, 1);
});

test('native server marker storage failures pause network and retain data', async () => {
  let calls = 0;
  for (const storage of [null,
    {getItem() { throw Error('unavailable'); }},
    {getItem() { return null; }, setItem() { throw Error('full'); }},
    {getItem() { return null; }, setItem() {}},
  ]) {
    let failure;
    try { pinNativeOrigin(storage, 'https://trace.example'); } catch (error) { failure = error; }
    assert.match(failure?.message || '', /Network access is paused/);
    const transport = createApiFetch({native: true, origin: 'https://trace.example', nativeRequest: async () => {calls++;}});
    const guarded = (...args) => failure ? Promise.reject(failure) : transport(...args);
    await assert.rejects(guarded('/api/library/sync'), /Network access is paused/);
  }
  assert.equal(calls, 0);
});
