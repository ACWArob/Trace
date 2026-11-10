import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

const workerSource = await readFile(new URL('../sw.js', import.meta.url), 'utf8');
const mobileSource = await readFile(new URL('../mobile.js', import.meta.url), 'utf8');
const origin = 'https://trace.test';

function worker(fetchImpl) {
  const listeners = new Map();
  const stores = new Map();
  const fetched = [];
  const writes = [];
  const deleted = [];
  let activations = 0;
  const caches = {
    async open(name) {
      if (!stores.has(name)) stores.set(name, new Map());
      const entries = stores.get(name);
      return {
        async put(path, response) { writes.push(path); entries.set(path, response); },
        async match(path) { return entries.get(path); },
      };
    },
    async keys() { return [...stores.keys()]; },
    async delete(name) { deleted.push(name); return stores.delete(name); },
  };
  const self = {
    location: {origin}, clients: {async claim() {}},
    addEventListener(name, fn) { listeners.set(name, fn); },
    skipWaiting() { activations++; },
  };
  vm.runInNewContext(workerSource, {
    self, caches, URL, Request, Set, Promise,
    async fetch(request) { fetched.push(request); return fetchImpl(request); },
  });
  return {
    stores, fetched, writes, deleted,
    get activations() { return activations; },
    async lifecycle(name) {
      let task;
      listeners.get(name)({waitUntil(promise) { task = promise; }});
      await task;
    },
    message(data) { listeners.get('message')({data}); },
    request(path, options = {}) {
      let result;
      const request = {url: new URL(path, origin).href, method: 'GET', mode: 'cors', ...options};
      listeners.get('fetch')({request, respondWith(promise) { result = promise; }});
      return result;
    },
  };
}

function publicFile(request) {
  const path = new URL(request.url).pathname;
  const type = /\.m?js$/.test(path) ? 'text/javascript' : path.endsWith('.css') ? 'text/css' :
    path.endsWith('.html') ? 'text/html' : path.endsWith('.webmanifest') ? 'application/manifest+json' : 'image/png';
  return new Response(`public ${path}`, {headers: {'Content-Type': type}});
}

test('Install caches only credential-free fixed public shell files and tolerates missing optional assets', async () => {
  const app = worker(request => {
    const path = new URL(request.url).pathname;
    if (path === '/assets/trace-app-512.png') throw new Error('Missing optional image');
    if (path === '/social.js') return new Response('private', {headers: {'Content-Type': 'text/javascript', 'Cache-Control': 'private'}});
    if (path === '/library_sync.mjs') return new Response('sign in', {headers: {'Content-Type': 'text/html'}});
    return publicFile(request);
  });
  await app.lifecycle('install');
  assert.ok(app.writes.includes('/index.html'));
  assert.ok(!app.writes.includes('/social.js'));
  assert.ok(!app.writes.includes('/library_sync.mjs'));
  assert.ok(!app.writes.includes('/assets/trace-app-512.png'));
  assert.ok(app.fetched.every(request => request.credentials === 'omit' && request.redirect === 'error'));
  assert.ok(app.fetched.every(request => !new URL(request.url).pathname.startsWith('/api/')));
  assert.equal(app.activations, 0, 'Installing an update must not interrupt an open editing session');
});

test('API, auth, private paths, providers, POSTs and unrecognized query strings bypass the worker', () => {
  const app = worker(() => { throw new Error('Must not fetch through worker'); });
  for (const path of ['/api/library', '/api/auth/session', '/auth', '/login', '/session', '/uploads/photo.png',
    '/.trace/keys', '/collection.json', '/app.js?token=private', '/index.html?account=private',
    'https://provider.test/app.js']) {
    assert.equal(app.request(path), undefined, path);
  }
  assert.equal(app.request('/api/library', {method: 'POST'}), undefined);
  assert.equal(app.request('/app.js', {method: 'POST'}), undefined);
  assert.equal(app.fetched.length, 0);
  assert.equal(app.writes.length, 0);
});

test('Offline root navigation and versioned modules use public shell; online responses never enter cache', async () => {
  let offline = false;
  const app = worker(request => {
    if (offline) throw new Error('Offline');
    return publicFile(request);
  });
  await app.lifecycle('install');
  const writeCount = app.writes.length;
  await app.request('/app.js?v=5');
  assert.equal(app.writes.length, writeCount);
  offline = true;
  assert.equal(await (await app.request('/', {mode: 'navigate'})).text(), 'public /index.html');
  assert.equal(await (await app.request('/app.js?v=5')).text(), 'public /app.js');
  assert.equal(app.writes.length, writeCount);
});

test('Activation deletes only obsolete Trace shell caches and requires explicit update message', async () => {
  const app = worker(publicFile);
  await app.lifecycle('install');
  app.stores.set('trace-shell-old', new Map());
  app.stores.set('another-app-cache', new Map());
  app.stores.set('trace-library', new Map());
  await app.lifecycle('activate');
  assert.deepEqual(app.deleted, ['trace-shell-old']);
  assert.ok(app.stores.has('trace-library'));
  assert.ok(app.stores.has('another-app-cache'));
  app.message({type: 'OTHER'});
  assert.equal(app.activations, 0);
  app.message({type: 'TRACE_ACTIVATE_UPDATE'});
  assert.equal(app.activations, 1);
});

function mobile({ios = false, waiting = null, native = false} = {}) {
  const windowListeners = new Map();
  const serviceListeners = new Map();
  const status = {textContent: ''};
  const events = [];
  let reloads = 0;
  let registrations = 0;
  const serviceWorker = {
    controller: {},
    async register() { registrations++; return {waiting, addEventListener() {}}; },
    addEventListener(name, fn) { serviceListeners.set(name, fn); },
  };
  const window = {
    isSecureContext: true,
    matchMedia() { return {matches: false, addEventListener() {}}; },
    addEventListener(name, fn) { windowListeners.set(name, fn); },
    dispatchEvent(event) { events.push(event); },
    location: {reload() { reloads++; }},
    ...(native ? {TraceNative: {platform: 'ios'}} : {}),
  };
  vm.runInNewContext(mobileSource, {
    window,
    navigator: {serviceWorker, userAgent: ios ? 'iPhone' : 'Android', platform: ios ? 'iPhone' : 'Linux', maxTouchPoints: 1},
    document: {querySelectorAll(selector) { return selector === '[data-install-trace-status]' ? [status] : []; }, addEventListener() {}},
    CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options.detail; } },
  });
  return {window, status, events, windowListeners, serviceListeners, get reloads() { return reloads; }, get registrations() { return registrations; }};
}

test('Native package is standalone and never registers a browser service worker', () => {
  const app = mobile({native: true});
  assert.equal(app.window.TraceMobile.state.standalone, true);
  assert.equal(app.window.TraceMobile.state.supported, false);
  assert.equal(app.registrations, 0);
});

test('iPhone install guidance explains Safari home-screen installation without permissions', async () => {
  const app = mobile({ios: true});
  const result = await app.window.TraceMobile.install();
  assert.equal(result.outcome, 'guidance');
  assert.match(app.status.textContent, /Safari.*Share.*Add to Home Screen/);
  assert.equal(app.reloads, 0);
});

test('Android deferred install prompt runs only after a user action', async () => {
  const app = mobile();
  let prompts = 0;
  let prevented = false;
  app.windowListeners.get('beforeinstallprompt')({
    preventDefault() { prevented = true; },
    async prompt() { prompts++; },
    userChoice: Promise.resolve({outcome: 'accepted'}),
  });
  assert.ok(prevented);
  assert.equal(prompts, 0);
  assert.equal(app.window.TraceMobile.state.canInstall, true);
  assert.equal((await app.window.TraceMobile.install()).outcome, 'accepted');
  assert.equal(prompts, 1);
  assert.equal(app.window.TraceMobile.state.canInstall, false);
});

test('A waiting app update is advertised without activation or reload until explicitly accepted', async () => {
  const messages = [];
  const app = mobile({waiting: {postMessage(message) { messages.push(message); }}});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(app.window.TraceMobile.state.updateAvailable, true);
  assert.equal(messages.length, 0);
  assert.equal(app.reloads, 0);
  assert.ok(app.events.some(event => event.type === 'trace:mobile-update'));
  app.window.TraceMobile.applyUpdate();
  assert.equal(messages[0].type, 'TRACE_ACTIVATE_UPDATE');
  app.serviceListeners.get('controllerchange')();
  assert.equal(app.reloads, 0, 'Update acceptance alone must not reload a page with unfinished edits');
  assert.equal(app.window.TraceMobile.state.refreshRequired, true);
});
