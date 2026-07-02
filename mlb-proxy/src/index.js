const MLB_API      = 'https://statsapi.mlb.com';
const PRIZEPICKS_API = 'https://api.prizepicks.com';

export default {
  async fetch(request) {
    const url   = new URL(request.url);
    const path  = url.pathname;

    // Route /prizepicks/* → api.prizepicks.com/*
    if (path.startsWith('/prizepicks')) {
      const target = PRIZEPICKS_API + path.replace('/prizepicks', '') + url.search;

      const headers = new Headers({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://app.prizepicks.com/',
        'Origin': 'https://app.prizepicks.com',
      });

      const resp = await fetch(target, { headers });

      const respHeaders = new Headers(resp.headers);
      respHeaders.set('Access-Control-Allow-Origin', '*');

      return new Response(resp.body, {
        status: resp.status,
        statusText: resp.statusText,
        headers: respHeaders,
      });
    }

    // Route /* → statsapi.mlb.com/*  (existing MLB proxy)
    const target  = MLB_API + path + url.search;

    const headers = new Headers({
      'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
      'Accept': 'application/json, text/html, application/xhtml+xml, */*',
      'Accept-Language': 'en-US,en;q=0.9',
      'Origin': 'https://www.mlb.com',
      'Referer': 'https://www.mlb.com/',
    });

    const resp = await fetch(target, { headers });

    const respHeaders = new Headers(resp.headers);
    respHeaders.set('Access-Control-Allow-Origin', '*');
    respHeaders.set('Access-Control-Allow-Methods', 'GET, OPTIONS');
    respHeaders.set('Access-Control-Allow-Headers', 'Content-Type');

    return new Response(resp.body, {
      status: resp.status,
      statusText: resp.statusText,
      headers: respHeaders,
    });
  },
};
