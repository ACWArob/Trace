import {Capacitor, CapacitorHttp} from '@capacitor/core';
import {App} from '@capacitor/app';
import {Browser} from '@capacitor/browser';
import {createApiFetch, pinNativeOrigin} from './native_transport.mjs';

// This configuration is public build metadata, never a credential or API key.
let config = {apiOrigin: '', development: false};
try {
  const response = await fetch('/native-config.json', {cache: 'no-store'});
  if (!response.ok) throw Error('Native configuration unavailable.');
  config = await response.json();
} catch {
  // A bundled device shelf remains usable without a server connection.
}
let connectionError;
try { pinNativeOrigin(localStorage, config.apiOrigin); }
catch (error) { connectionError = error; }
globalThis.TraceNative = Object.freeze({
  platform: Capacitor.getPlatform(),
  apiFetch: connectionError ? async () => { throw connectionError; } : createApiFetch({
    native: true, origin: config.apiOrigin, development: config.development === true,
    nativeRequest: options => CapacitorHttp.request({...options, disableRedirects: true}),
  }),
});

await import('./app.js');
await import('./mobile.js');

if (!config.apiOrigin || connectionError) {
  const notice = document.createElement('p');
  notice.className = 'native-server-notice';
  notice.setAttribute('role', 'status');
  notice.textContent = connectionError?.message || 'Device shelf · This build has no Trace server connected. Your shelf works here; search, sign-in, and sync need a connected build.';
  document.querySelector('main')?.before(notice);
}

// External sources open outside Trace's trusted application WebView.
document.addEventListener('click', event => {
  const anchor = event.target.closest?.('a[target="_blank"]');
  if (!anchor || anchor.hasAttribute('download')) return;
  let url;
  try { url = new URL(anchor.href); } catch { return; }
  if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) return;
  event.preventDefault();
  Browser.open({url: url.href}).catch(() => {
    window.dispatchEvent(new CustomEvent('trace:mobile-guidance', {detail: {message: 'The source could not be opened. Try again when connected.'}}));
  });
});

App.addListener('appStateChange', ({isActive}) => {
  if (isActive && navigator.onLine) window.dispatchEvent(new Event('online'));
});
if (Capacitor.getPlatform() === 'android') {
  App.addListener('backButton', ({canGoBack}) => {
    const open = [...document.querySelectorAll('dialog[open]')].at(-1);
    if (open) { open.close(); return; }
    if (canGoBack) window.history.back();
    else App.minimizeApp();
  });
}
