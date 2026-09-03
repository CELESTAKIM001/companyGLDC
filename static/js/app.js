// GLDC frontend API helper: never assume a server error is JSON.
(() => {
  const originalFetch = window.fetch.bind(window);
  async function parseResponse(r) {
    const text = await r.text();
    let data = {};
    try { data = text ? JSON.parse(text) : {}; }
    catch (_) { data = {ok:false,error:{code:'NON_JSON_RESPONSE',message:text.slice(0,240) || `HTTP ${r.status}`}}; }
    return {response:r,data};
  }
  window.fetch = (input, init = {}) => {
    const method = String(init.method || (input && input.method) || 'GET').toUpperCase();
    const url = typeof input === 'string' ? input : input.url;
    if (method !== 'GET' && method !== 'HEAD' && method !== 'OPTIONS' && url && new URL(url, location.href).origin === location.origin) {
      const headers = new Headers(init.headers || {});
      const token = document.querySelector('meta[name=csrf-token]')?.content;
      if (token) headers.set('X-CSRF-Token', token);
      headers.set('X-Request-ID', crypto.randomUUID ? crypto.randomUUID() : String(Date.now()));
      init = {...init, headers, credentials: init.credentials || 'same-origin'};
    }
    return originalFetch(input, init);
  };
  window.GLDCApi = {
    async request(url, options = {}) {
      const {response,data} = await parseResponse(await fetch(url, options));
      if (!response.ok || data.ok === false) {
        const e = data.error || {};
        const err = new Error(e.message || `Request failed (${response.status})`);
        err.code = e.code; err.requestId = e.requestId || response.headers.get('X-Request-ID'); err.status = response.status;
        throw err;
      }
      return data;
    },
    parseResponse
  };
})();
