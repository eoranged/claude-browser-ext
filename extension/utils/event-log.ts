import { MAX_EVENTS } from './constants';
import type { EventEntry, EventQueryParams, LogEntry, RequestEntry } from './messages';

type NewLogEntry = Omit<LogEntry, 'id'>;
type NewRequestEntry = Omit<RequestEntry, 'id'>;

export class EventLog {
  private entries: EventEntry[] = [];
  private nextId = 1;

  add(entry: NewLogEntry | NewRequestEntry): EventEntry {
    const full = { ...entry, id: this.nextId++ } as EventEntry;
    this.entries.push(full);
    if (this.entries.length > MAX_EVENTS) {
      this.entries.splice(0, this.entries.length - MAX_EVENTS);
    }
    return full;
  }

  query(params: EventQueryParams): { events: EventEntry[]; has_more: boolean } {
    const fromId = params.fromId ?? 0;
    const pageSize = params.pageSize ?? 20;

    let filtered = this.entries;

    if (fromId > 0) {
      // Binary search for start index since IDs are monotonic
      let lo = 0, hi = filtered.length;
      while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (filtered[mid].id < fromId) lo = mid + 1;
        else hi = mid;
      }
      filtered = filtered.slice(lo);
    }

    if (params.tabId != null) {
      filtered = filtered.filter(e => e.tabId === params.tabId);
    }
    if (params.type) {
      filtered = filtered.filter(e => e.type === params.type);
    }
    if (params.level && params.type === 'log') {
      filtered = filtered.filter(e => e.type === 'log' && e.level === params.level);
    }
    if (params.method && params.type === 'request') {
      const m = params.method.toUpperCase();
      filtered = filtered.filter(e => e.type === 'request' && e.method === m);
    }
    if (params.urlPattern) {
      const pat = params.urlPattern.toLowerCase();
      filtered = filtered.filter(e => e.type === 'request' && e.url.toLowerCase().includes(pat));
    }

    const page = filtered.slice(0, pageSize);
    const has_more = filtered.length > pageSize;
    return { events: page, has_more };
  }

  /** Find most recent request entry matching URL+method that has no body yet */
  findRequestForBody(url: string, method: string): EventEntry | undefined {
    for (let i = this.entries.length - 1; i >= 0; i--) {
      const e = this.entries[i];
      if (
        e.type === 'request' &&
        e.url === url &&
        e.method === method &&
        e.requestBody === null &&
        e.responseBody === null
      ) {
        return e;
      }
    }
    return undefined;
  }

  get totalEvents(): number {
    return this.entries.length;
  }

  get totalErrors(): number {
    return this.entries.filter(e =>
      (e.type === 'log' && e.level === 'error') ||
      (e.type === 'request' && e.status != null && e.status >= 400)
    ).length;
  }

  reset(): void {
    this.entries = [];
    // keep nextId monotonic — don't reset
  }
}
