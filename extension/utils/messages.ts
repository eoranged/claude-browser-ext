// --- Event log entries ---

export interface LogEntry {
  id: number;
  tabId: number;
  timestamp: number;
  type: 'log';
  level: 'log' | 'warn' | 'error' | 'info' | 'debug';
  args: unknown[];
}

export interface RequestEntry {
  id: number;
  tabId: number;
  timestamp: number;
  type: 'request';
  method: string;
  url: string;
  requestHeaders: Record<string, string> | null;
  requestBody: string | null;
  status: number | null;
  statusText: string | null;
  responseHeaders: Record<string, string> | null;
  responseBody: string | null;
  duration: number | null;
  resourceType: string | null;
}

export type EventEntry = LogEntry | RequestEntry;

// --- Tab info ---

export interface TabInfo {
  tabId: number;
  url: string;
  title: string;
  enabledAt: number;
}

// --- WS protocol: server → extension ---

export type Command =
  | { id: string; command: 'list_tabs' }
  | { id: string; command: 'get_url'; params: { tabId: number } }
  | { id: string; command: 'get_dom'; params: { tabId: number } }
  | { id: string; command: 'execute'; params: { tabId: number; code: string } }
  | { id: string; command: 'screenshot'; params: { tabId: number } }
  | { id: string; command: 'get_events'; params: EventQueryParams };

export interface EventQueryParams {
  tabId?: number;
  fromId?: number;
  pageSize?: number;
  type?: 'log' | 'request';
  level?: string;
  method?: string;
  urlPattern?: string;
}

// --- WS protocol: extension → server ---

export interface CommandResponse {
  id: string;
  result: unknown;
  error: string | null;
}

// --- WS auth ---

export interface AuthMessage {
  type: 'auth';
  token: string;
}

export interface AuthResult {
  type: 'auth_result';
  ok: boolean;
}

// --- Popup ↔ background ---

export type ConnectionStatus = 'disconnected' | 'connecting' | 'connected';

export interface PopupState {
  connectionStatus: ConnectionStatus;
  currentTabEnabled: boolean;
  currentTabId: number | null;
  stats: {
    enabledTabs: number;
    totalEvents: number;
    totalErrors: number;
  };
}

// --- Content script → background (relay from MAIN world) ---

export interface BridgeLogMessage {
  __bridge: true;
  type: 'log';
  level: 'log' | 'warn' | 'error' | 'info' | 'debug';
  args: unknown[];
}

export interface BridgeRequestMessage {
  __bridge: true;
  type: 'request_body';
  method: string;
  url: string;
  requestBody: string | null;
  responseBody: string | null;
}

export type BridgeMessage = BridgeLogMessage | BridgeRequestMessage;
