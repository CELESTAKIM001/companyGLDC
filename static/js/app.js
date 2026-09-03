// Production-safe frontend helper. Mutating same-origin requests receive the server-issued CSRF token.
(() => {
  const originalFetch = window.fetch.bind(window);
  window.fetch = (input, init = {}) => {
    const method = String(init.method || (input && input.method) || 'GET').toUpperCase();
    const url = typeof input === 'string' ? input : input.url;
    if (method !== 'GET' && method !== 'HEAD' && method !== 'OPTIONS' && url && new URL(url, location.href).origin === location.origin) {
      const headers = new Headers(init.headers || {});
      const token = document.querySelector('meta[name=csrf-token]')?.content;
      if (token) headers.set('X-CSRF-Token', token);
      init = {...init, headers, credentials: init.credentials || 'same-origin'};
    }
    return originalFetch(input, init);
  };
  window.GLDCApi = { async request(url, options = {}) {
    const r = await fetch(url, options);
    const text = await r.text();
    let j = {};
    try { j = text ? JSON.parse(text) : {}; } catch (_) { j = { ok: false, error: { code: 'NON_JSON_RESPONSE', message: text || 'The server returned an empty response.' } }; }
    if (!r.ok) { const err = new Error(j.error?.message || j.message || `Request failed (${r.status})`); err.status = r.status; err.code = j.error?.code; err.requestId = j.requestId || r.headers.get('X-Request-ID'); throw err; }
    return j;
  }};
})();
