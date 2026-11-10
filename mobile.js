/* Optional home-screen installation. No permissions or push subscriptions. */
const standaloneMedia = window.matchMedia?.('(display-mode: standalone)');
const nativeApp = Boolean(window.TraceNative);
const ios = /iPad|iPhone|iPod/.test(navigator.userAgent) ||
  (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
let installPrompt = null;
let registration = null;
const state = {
  supported: !nativeApp && 'serviceWorker' in navigator && window.isSecureContext,
  standalone: nativeApp || Boolean(standaloneMedia?.matches || navigator.standalone),
  canInstall: false,
  updateAvailable: false,
  refreshRequired: false,
  offlineError: false,
};

function emit(name, detail = {}) {
  window.dispatchEvent(new CustomEvent(`trace:mobile-${name}`, { detail: { ...state, ...detail } }));
}

function renderState() {
  document.querySelectorAll('[data-install-trace]').forEach(button => {
    button.hidden = state.standalone;
  });
  document.querySelectorAll('[data-update-trace]').forEach(button => {
    button.hidden = !state.updateAvailable && !state.refreshRequired;
  });
  emit('state');
}

function guidance(message) {
  document.querySelectorAll('[data-install-trace-status]').forEach(element => {
    element.textContent = message;
  });
  emit('guidance', { message, platform: ios ? 'ios' : 'other' });
  return { outcome: 'guidance', message };
}

async function install() {
  if (!state.supported) return guidance('Open Trace in Safari on iPhone or Chrome on Android to install it. Offline installation is unavailable in this browser.');
  if (state.standalone) return guidance('Trace is already open as a home-screen app.');
  if (installPrompt) {
    const prompt = installPrompt;
    installPrompt = null;
    state.canInstall = false;
    renderState();
    await prompt.prompt();
    const choice = await prompt.userChoice;
    emit('install', { outcome: choice.outcome });
    return choice;
  }
  if (!window.isSecureContext) {
    return guidance('Open Trace over HTTPS to add it to your home screen. Your local computer can also use localhost.');
  }
  return guidance(ios
    ? 'In Safari, tap Share, then Add to Home Screen. Trace opens from that icon as a home-screen app.'
    : 'Open your browser menu and choose Install app or Add to Home screen. Trace is a home-screen app; no app store download is needed.');
}

function applyUpdate({ reload = false } = {}) {
  if (registration?.waiting) {
    if (reload) navigator.serviceWorker.addEventListener('controllerchange', () => window.location.reload(), { once: true });
    registration.waiting.postMessage({ type: 'TRACE_ACTIVATE_UPDATE' });
    return true;
  }
  if (reload && state.refreshRequired) window.location.reload();
  return false;
}

window.TraceMobile = Object.freeze({
  get state() { return { ...state }; },
  install,
  applyUpdate,
});

window.addEventListener('beforeinstallprompt', event => {
  event.preventDefault();
  installPrompt = event;
  state.canInstall = true;
  renderState();
});
window.addEventListener('appinstalled', () => {
  installPrompt = null;
  state.canInstall = false;
  state.standalone = true;
  renderState();
  emit('install', { outcome: 'accepted' });
});
standaloneMedia?.addEventListener?.('change', event => {
  state.standalone = event.matches || Boolean(navigator.standalone);
  renderState();
});
document.addEventListener('click', event => {
  if (event.target.closest('[data-install-trace]')) install().catch(() => {
    guidance('Installation was not completed. You can try again from your browser menu.');
  });
  // This button is an explicit refresh action; background updates never reload a page.
  if (event.target.closest('[data-update-trace]')) applyUpdate({ reload: true });
});

async function register() {
  if (!state.supported) { renderState(); return; }
  try {
    registration = await navigator.serviceWorker.register('/sw.js', { scope: '/', updateViaCache: 'none' });
    function updateReady() {
      if (!registration.waiting || !navigator.serviceWorker.controller) return;
      state.updateAvailable = true;
      renderState();
      emit('update', { message: 'An update is ready. Finish any edits, then refresh Trace.' });
    }
    registration.addEventListener('updatefound', () => {
      const installing = registration.installing;
      installing?.addEventListener('statechange', () => {
        if (installing.state === 'installed') updateReady();
      });
    });
    navigator.serviceWorker.addEventListener('controllerchange', () => {
      if (!state.updateAvailable) return;
      state.updateAvailable = false;
      state.refreshRequired = true;
      renderState();
      emit('update', { message: 'Trace has updated. Finish any edits, then refresh to use it.' });
    });
    updateReady();
    renderState();
  } catch {
    state.offlineError = true;
    emit('error', { message: 'Home-screen offline support could not start. Trace is still available in this browser.' });
    renderState();
  }
}

register();
