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
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error?.message || 'Request failed');
    return j;
  }};
})();
