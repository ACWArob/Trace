/* Only public, fixed app files belong here. Personal collections stay in IndexedDB. */
const CACHE_PREFIX = 'trace-shell-';
const CACHE_NAME = `${CACHE_PREFIX}v9`;
const SHELL_FILES = [
  '/index.html', '/styles.css', '/app.js', '/core.mjs', '/data.js', '/views.mjs',
  '/social.js', '/library_sync.mjs', '/api_client.mjs', '/artwork.mjs', '/mobile.js', '/manifest.webmanifest',
  '/assets/trace-app-192.png', '/assets/trace-app-512.png',
  '/assets/trace-wordmark.png', '/assets/trace-icon.svg',
];
const SHELL_PATHS = new Set(SHELL_FILES);

function shellPath(request) {
  if (request.method !== 'GET') return null;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || url.username || url.password) return null;
  // Versioned script/style URLs share their fixed public file's offline copy.
  if (url.search && !/^\?v=[\w.-]+$/.test(url.search)) return null;
  if (url.pathname === '/' && request.mode === 'navigate') return '/index.html';
  return SHELL_PATHS.has(url.pathname) ? url.pathname : null;
}

function isPublicFile(response, path) {
  if (!response.ok || response.status !== 200 || response.redirected) return false;
  if (!['basic', 'default'].includes(response.type)) return false;
  if (/\b(private|no-store)\b/i.test(response.headers.get('Cache-Control') || '')) return false;
  if (response.headers.has('Set-Cookie') || response.headers.get('Vary')?.includes('*')) return false;
  const type = response.headers.get('Content-Type') || '';
  if (/\.(?:m?js)$/.test(path)) return /(?:javascript|ecmascript)/i.test(type);
  if (path.endsWith('.css')) return /text\/css/i.test(type);
  if (path.endsWith('.html')) return /text\/html/i.test(type);
  if (path.endsWith('.webmanifest')) return /(?:manifest\+json|application\/json)/i.test(type);
  return /image\//i.test(type);
}

self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_NAME);
    // A missing optional icon/module must not prevent installation of the shell.
    await Promise.allSettled(SHELL_FILES.map(async path => {
      const response = await fetch(new Request(new URL(path, self.location.origin), {
        credentials: 'omit', cache: 'reload', redirect: 'error',
      }));
      if (isPublicFile(response, path)) await cache.put(path, response);
    }));
  })());
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names.filter(name => name.startsWith(CACHE_PREFIX) && name !== CACHE_NAME)
      .map(name => caches.delete(name)));
    await self.clients.claim();
  })());
});

self.addEventListener('message', event => {
  if (event.data?.type === 'TRACE_ACTIVATE_UPDATE') self.skipWaiting();
});

self.addEventListener('fetch', event => {
  const path = shellPath(event.request);
  // All API/auth/session traffic, writes, uploads, and providers use the browser network.
  if (!path) return;
  event.respondWith((async () => {
    try {
      return await fetch(event.request);
    } catch (error) {
      const cache = await caches.open(CACHE_NAME);
      const cached = await cache.match(path);
      if (cached) return cached;
      if (event.request.mode === 'navigate') {
        const shell = await cache.match('/index.html');
        if (shell) return shell;
      }
      throw error;
    }
  })());
});
