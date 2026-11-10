// Native packages provide their transport before loading the shared UI.
// Browser requests keep the existing same-origin session behavior.
export function apiFetch(path, options) {
  return globalThis.TraceNative?.apiFetch
    ? globalThis.TraceNative.apiFetch(path, options)
    : globalThis.fetch(path, options);
}
