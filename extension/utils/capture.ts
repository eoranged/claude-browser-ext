import { MAX_BODY_SIZE } from './constants';

/**
 * Inject capture scripts into a tab.
 * MAIN world: patches console/fetch/XHR.
 * ISOLATED world: relays postMessage to background.
 */
export async function injectCapture(tabId: number): Promise<void> {
  // Inject relay first (ISOLATED world) so it's ready when MAIN world sends messages
  await browser.scripting.executeScript({
    target: { tabId },
    func: relayScript,
    world: 'ISOLATED' as any,
  });

  await browser.scripting.executeScript({
    target: { tabId },
    func: mainWorldScript,
    args: [MAX_BODY_SIZE],
    world: 'MAIN' as any,
  });
}

/** ISOLATED world: relay __bridge messages to background */
function relayScript() {
  if ((window as any).__bridgeRelayInstalled) return;
  (window as any).__bridgeRelayInstalled = true;

  window.addEventListener('message', (event) => {
    if (event.source !== window) return;
    const data = event.data;
    if (data && data.__bridge === true) {
      browser.runtime.sendMessage(data).catch(() => {});
    }
  });
}

/** MAIN world: patch console, fetch, XMLHttpRequest */
function mainWorldScript(maxBodySize: number) {
  if ((window as any).__bridgeCaptureInstalled) return;
  (window as any).__bridgeCaptureInstalled = true;

  function serialize(val: unknown): unknown {
    try {
      if (val instanceof Error) return { message: val.message, stack: val.stack };
      if (typeof val === 'object' && val !== null) {
        const s = JSON.stringify(val);
        if (s.length > 4096) return s.slice(0, 4096) + '...[truncated]';
        return JSON.parse(s);
      }
      return val;
    } catch {
      return String(val);
    }
  }

  // --- Console patch ---
  const origConsole: Record<string, Function> = {};
  for (const level of ['log', 'warn', 'error', 'info', 'debug'] as const) {
    origConsole[level] = (console as any)[level];
    (console as any)[level] = function (...args: unknown[]) {
      origConsole[level].apply(console, args);
      try {
        window.postMessage({
          __bridge: true,
          type: 'log',
          level,
          args: args.map(serialize),
        }, '*');
      } catch { /* ignore */ }
    };
  }

  // --- Fetch patch ---
  const origFetch = window.fetch;
  window.fetch = async function (input: RequestInfo | URL, init?: RequestInit) {
    const url = typeof input === 'string' ? input
      : input instanceof URL ? input.href
      : input.url;
    const method = (init?.method || 'GET').toUpperCase();

    let requestBody: string | null = null;
    if (init?.body) {
      try {
        requestBody = typeof init.body === 'string' ? init.body : String(init.body);
        if (requestBody.length > maxBodySize) requestBody = requestBody.slice(0, maxBodySize) + '...[truncated]';
      } catch { requestBody = '[unreadable]'; }
    }

    try {
      const response = await origFetch.call(this, input, init);
      let responseBody: string | null = null;
      try {
        const clone = response.clone();
        const text = await clone.text();
        responseBody = text.length > maxBodySize ? text.slice(0, maxBodySize) + '...[truncated]' : text;
      } catch { /* ignore */ }

      window.postMessage({
        __bridge: true,
        type: 'request_body',
        method,
        url,
        requestBody,
        responseBody,
      }, '*');

      return response;
    } catch (err) {
      window.postMessage({
        __bridge: true,
        type: 'request_body',
        method,
        url,
        requestBody,
        responseBody: null,
      }, '*');
      throw err;
    }
  };

  // --- XHR patch ---
  const OrigXHR = window.XMLHttpRequest;
  const origOpen = OrigXHR.prototype.open;
  const origSend = OrigXHR.prototype.send;

  OrigXHR.prototype.open = function (method: string, url: string | URL, ...rest: any[]) {
    (this as any).__bridgeMethod = method.toUpperCase();
    (this as any).__bridgeUrl = typeof url === 'string' ? url : url.href;
    return origOpen.apply(this, [method, url, ...rest] as any);
  };

  OrigXHR.prototype.send = function (body?: Document | XMLHttpRequestBodyInit | null) {
    const method: string = (this as any).__bridgeMethod || 'GET';
    const url: string = (this as any).__bridgeUrl || '';
    let requestBody: string | null = null;
    if (body) {
      try {
        requestBody = typeof body === 'string' ? body : String(body);
        if (requestBody.length > maxBodySize) requestBody = requestBody.slice(0, maxBodySize) + '...[truncated]';
      } catch { requestBody = '[unreadable]'; }
    }

    this.addEventListener('load', function () {
      let responseBody: string | null = null;
      try {
        const text = this.responseText;
        responseBody = text.length > maxBodySize ? text.slice(0, maxBodySize) + '...[truncated]' : text;
      } catch { /* ignore */ }

      window.postMessage({
        __bridge: true,
        type: 'request_body',
        method,
        url,
        requestBody,
        responseBody,
      }, '*');
    });

    this.addEventListener('error', function () {
      window.postMessage({
        __bridge: true,
        type: 'request_body',
        method,
        url,
        requestBody,
        responseBody: null,
      }, '*');
    });

    return origSend.call(this, body);
  };
}
