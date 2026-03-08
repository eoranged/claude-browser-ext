import {
  DEFAULT_PORT, RECONNECT_INITIAL_MS, RECONNECT_MAX_MS,
  KEEPALIVE_ALARM_NAME, KEEPALIVE_INTERVAL_MINUTES, STORAGE_KEY_PORT,
} from '@/utils/constants';
import { getToken } from '@/utils/token';
import { EventLog } from '@/utils/event-log';
import { injectCapture } from '@/utils/capture';
import type {
  Command, CommandResponse, AuthMessage, AuthResult, ConnectionStatus,
  TabInfo, RequestEntry, BridgeMessage,
} from '@/utils/messages';

export default defineBackground(() => {
  let ws: WebSocket | null = null;
  let connectionStatus: ConnectionStatus = 'disconnected';
  let reconnectDelay = RECONNECT_INITIAL_MS;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  const enabledTabs = new Map<number, TabInfo>();
  const eventLog = new EventLog();
  // Map webRequest requestId → event log entry id
  const pendingRequests = new Map<string, number>();

  // --- WebSocket connection ---

  async function getPort(): Promise<number> {
    const data = await browser.storage.local.get(STORAGE_KEY_PORT);
    return (data[STORAGE_KEY_PORT] as number) || DEFAULT_PORT;
  }

  async function connect() {
    if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) {
      return;
    }

    connectionStatus = 'connecting';
    let port: number;
    let token: string;
    try {
      port = await getPort();
      token = await getToken();
    } catch (err) {
      console.error('[bridge] Failed to read port/token from storage:', err);
      scheduleReconnect();
      return;
    }

    console.log(`[bridge] Connecting to ws://127.0.0.1:${port}`);

    try {
      ws = new WebSocket(`ws://127.0.0.1:${port}`);
    } catch (err) {
      console.error('[bridge] WebSocket constructor error:', err);
      scheduleReconnect();
      return;
    }

    ws.onopen = () => {
      console.log('[bridge] WebSocket opened, sending auth');
      try {
        const auth: AuthMessage = { type: 'auth', token };
        ws!.send(JSON.stringify(auth));
        console.log('[bridge] Auth sent');
      } catch (err) {
        console.error('[bridge] Failed to send auth:', err);
      }
    };

    ws.onmessage = async (event) => {
      let msg: AuthResult | Command;
      try {
        msg = JSON.parse(event.data as string);
      } catch {
        return;
      }

      if ('type' in msg && msg.type === 'auth_result') {
        if (msg.ok) {
          console.log('[bridge] Authenticated successfully');
          connectionStatus = 'connected';
          reconnectDelay = RECONNECT_INITIAL_MS;
        } else {
          console.error('[bridge] Auth rejected by server');
          ws?.close();
        }
        return;
      }

      if ('command' in msg) {
        const response = await handleCommand(msg as Command);
        ws?.send(JSON.stringify(response));
      }
    };

    ws.onclose = (event) => {
      console.log(`[bridge] WebSocket closed: code=${event.code} reason=${event.reason} wasClean=${event.wasClean}`);
      connectionStatus = 'disconnected';
      ws = null;
      scheduleReconnect();
    };

    ws.onerror = (event) => {
      console.error('[bridge] WebSocket error:', event);
    };
  }

  function scheduleReconnect() {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
      connect();
    }, reconnectDelay);
  }

  // --- Command handlers ---

  async function handleCommand(cmd: Command): Promise<CommandResponse> {
    const id = cmd.id;
    try {
      switch (cmd.command) {
        case 'list_tabs':
          return { id, result: Array.from(enabledTabs.values()), error: null };
        case 'get_url':
          return { id, result: await getTabUrl(cmd.params.tabId), error: null };
        case 'get_dom':
          return { id, result: await getTabDom(cmd.params.tabId), error: null };
        case 'execute':
          return { id, result: await executeScript(cmd.params.tabId, cmd.params.code), error: null };
        case 'screenshot':
          return { id, result: await takeScreenshot(cmd.params.tabId), error: null };
        case 'get_events':
          return { id, result: eventLog.query(cmd.params), error: null };
        default:
          return { id, result: null, error: `Unknown command: ${(cmd as Command).command}` };
      }
    } catch (err) {
      return { id, result: null, error: String(err) };
    }
  }

  function assertTabEnabled(tabId: number) {
    if (!enabledTabs.has(tabId)) throw new Error(`Tab ${tabId} is not enabled`);
  }

  async function getTabUrl(tabId: number): Promise<string> {
    assertTabEnabled(tabId);
    const tab = await browser.tabs.get(tabId);
    return tab.url ?? '';
  }

  async function getTabDom(tabId: number): Promise<string> {
    assertTabEnabled(tabId);
    const results = await browser.scripting.executeScript({
      target: { tabId },
      func: () => document.documentElement.outerHTML,
    });
    if (!results.length) throw new Error('Script returned no results');
    return results[0].result as string;
  }

  async function executeScript(tabId: number, code: string): Promise<unknown> {
    assertTabEnabled(tabId);
    const func = new Function(code) as () => unknown;
    const results = await browser.scripting.executeScript({
      target: { tabId },
      func,
    });
    if (!results.length) throw new Error('Script returned no results');
    return results[0].result;
  }

  async function takeScreenshot(tabId: number): Promise<string> {
    assertTabEnabled(tabId);
    // Ensure the tab's window is focused for captureVisibleTab
    const tab = await browser.tabs.get(tabId);
    if (tab.windowId != null) {
      await browser.windows.update(tab.windowId, { focused: true });
    }
    await browser.tabs.update(tabId, { active: true });
    // Small delay to let the browser render
    await new Promise(r => setTimeout(r, 100));
    return await browser.tabs.captureVisibleTab({ format: 'png' });
  }

  // --- Tab management ---

  async function enableTab(tabId: number) {
    if (enabledTabs.has(tabId)) return;
    const tab = await browser.tabs.get(tabId);
    enabledTabs.set(tabId, {
      tabId,
      url: tab.url ?? '',
      title: tab.title ?? '',
      enabledAt: Date.now(),
    });
    await injectCapture(tabId);
  }

  function disableTab(tabId: number) {
    enabledTabs.delete(tabId);
    pendingRequests.forEach((eventId, reqId) => {
      // Clean up pending requests for this tab — they'll just stay as-is
    });
  }

  // Clean up when tab is closed
  browser.tabs.onRemoved.addListener((tabId) => {
    enabledTabs.delete(tabId);
  });

  // Re-inject on navigation
  browser.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
    if (!enabledTabs.has(tabId)) return;
    if (changeInfo.status === 'complete') {
      enabledTabs.set(tabId, {
        ...enabledTabs.get(tabId)!,
        url: tab.url ?? '',
        title: tab.title ?? '',
      });
      injectCapture(tabId).catch(() => {});
    }
  });

  // --- webRequest listeners ---

  function setupWebRequestListeners() {
    const filter: Browser.webRequest.RequestFilter = { urls: ['<all_urls>'] };

    browser.webRequest.onBeforeRequest.addListener(
      (details): undefined => {
        if (!enabledTabs.has(details.tabId)) return;
        const entry = eventLog.add({
          tabId: details.tabId,
          timestamp: details.timeStamp,
          type: 'request',
          method: details.method,
          url: details.url,
          requestHeaders: null,
          requestBody: null,
          status: null,
          statusText: null,
          responseHeaders: null,
          responseBody: null,
          duration: null,
          resourceType: details.type,
        });
        pendingRequests.set(details.requestId, entry.id);
      },
      filter,
    );

    browser.webRequest.onSendHeaders.addListener(
      (details) => {
        const eventId = pendingRequests.get(details.requestId);
        if (eventId == null) return;
        const entry = findEntryById(eventId);
        if (entry && entry.type === 'request' && details.requestHeaders) {
          entry.requestHeaders = headersToRecord(details.requestHeaders);
        }
      },
      filter,
      ['requestHeaders'],
    );

    browser.webRequest.onHeadersReceived.addListener(
      (details): undefined => {
        const eventId = pendingRequests.get(details.requestId);
        if (eventId == null) return;
        const entry = findEntryById(eventId);
        if (entry && entry.type === 'request') {
          entry.status = details.statusCode;
          entry.statusText = details.statusLine ?? null;
          if (details.responseHeaders) {
            entry.responseHeaders = headersToRecord(details.responseHeaders);
          }
        }
      },
      filter,
      ['responseHeaders'],
    );

    browser.webRequest.onCompleted.addListener(
      (details) => {
        const eventId = pendingRequests.get(details.requestId);
        if (eventId == null) return;
        pendingRequests.delete(details.requestId);
        const entry = findEntryById(eventId);
        if (entry && entry.type === 'request') {
          // onBeforeRequest timeStamp → onCompleted timeStamp = duration
          entry.duration = details.timeStamp - entry.timestamp;
        }
      },
      filter,
    );

    browser.webRequest.onErrorOccurred.addListener(
      (details) => {
        const eventId = pendingRequests.get(details.requestId);
        if (eventId == null) return;
        pendingRequests.delete(details.requestId);
        const entry = findEntryById(eventId);
        if (entry && entry.type === 'request') {
          entry.statusText = details.error;
          entry.duration = details.timeStamp - entry.timestamp;
        }
      },
      filter,
    );
  }

  function findEntryById(id: number): RequestEntry | undefined {
    // Walk backwards — recent entries are most likely matches
    const result = eventLog.query({ fromId: id, pageSize: 1 });
    const entry = result.events[0];
    if (entry && entry.id === id && entry.type === 'request') return entry as RequestEntry;
    return undefined;
  }

  function headersToRecord(headers: Browser.webRequest.HttpHeader[]): Record<string, string> {
    const rec: Record<string, string> = {};
    for (const h of headers) {
      if (h.value != null) rec[h.name.toLowerCase()] = h.value;
    }
    return rec;
  }

  // --- Messages from content script / popup ---

  browser.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (!msg || typeof msg !== 'object') return;

    // Bridge messages from content script relay
    if (msg.__bridge === true) {
      const tabId = sender.tab?.id;
      if (tabId == null || !enabledTabs.has(tabId)) return;
      handleBridgeMessage(tabId, msg as BridgeMessage);
      return;
    }

    // Popup messages
    if (msg.type === 'get_popup_state') {
      (async () => {
        let currentTabId: number | null = null;
        try {
          const tabs = await browser.tabs.query({ active: true, currentWindow: true });
          if (tabs.length) currentTabId = tabs[0].id ?? null;
        } catch { /* ignore */ }

        sendResponse({
          connectionStatus,
          currentTabEnabled: currentTabId != null && enabledTabs.has(currentTabId),
          currentTabId,
          stats: {
            enabledTabs: enabledTabs.size,
            totalEvents: eventLog.totalEvents,
            totalErrors: eventLog.totalErrors,
          },
        });
      })();
      return true; // async sendResponse
    }

    if (msg.type === 'toggle_tab' && typeof msg.tabId === 'number') {
      (async () => {
        if (enabledTabs.has(msg.tabId)) {
          disableTab(msg.tabId);
        } else {
          await enableTab(msg.tabId);
        }
        sendResponse({ ok: true, enabled: enabledTabs.has(msg.tabId) });
      })();
      return true;
    }

    if (msg.type === 'reset_stats') {
      eventLog.reset();
      sendResponse({ ok: true });
      return;
    }

    if (msg.type === 'reconnect') {
      reconnectDelay = RECONNECT_INITIAL_MS;
      if (ws) ws.close();
      else connect();
      sendResponse({ ok: true });
      return;
    }
  });

  function handleBridgeMessage(tabId: number, msg: BridgeMessage) {
    if (msg.type === 'log') {
      eventLog.add({
        tabId,
        timestamp: Date.now(),
        type: 'log',
        level: msg.level,
        args: msg.args,
      });
    } else if (msg.type === 'request_body') {
      // Try to match to a webRequest entry
      const entry = eventLog.findRequestForBody(msg.url, msg.method);
      if (entry && entry.type === 'request') {
        (entry as RequestEntry).requestBody = msg.requestBody;
        (entry as RequestEntry).responseBody = msg.responseBody;
      }
    }
  }

  // --- Initialization ---

  setupWebRequestListeners();

  browser.alarms.create(KEEPALIVE_ALARM_NAME, {
    periodInMinutes: KEEPALIVE_INTERVAL_MINUTES,
  });

  browser.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === KEEPALIVE_ALARM_NAME) {
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        connect();
      }
    }
  });

  connect();
});
