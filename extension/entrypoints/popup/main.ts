import { STORAGE_KEY_PORT, DEFAULT_PORT } from '@/utils/constants';
import { getToken, generateAndStoreToken } from '@/utils/token';
import type { PopupState } from '@/utils/messages';

const powerBtn = document.getElementById('power-btn')!;
const powerLabel = document.getElementById('power-label')!;
const connectionEl = document.getElementById('connection')!;
const statTabs = document.getElementById('stat-tabs')!;
const statEvents = document.getElementById('stat-events')!;
const statErrors = document.getElementById('stat-errors')!;
const tokenEl = document.getElementById('token')!;
const toggleBtn = document.getElementById('toggle-token')!;
const copyBtn = document.getElementById('copy-token')!;
const portInput = document.getElementById('port') as HTMLInputElement;
const savePortBtn = document.getElementById('save-port')!;
const regenBtn = document.getElementById('regenerate-token')!;
const resetBtn = document.getElementById('reset-stats')!;

let tokenVisible = false;
let currentToken = '';
let currentTabId: number | null = null;

async function init() {
  currentToken = await getToken();
  updateTokenDisplay();

  const data = await browser.storage.local.get(STORAGE_KEY_PORT);
  portInput.value = String((data[STORAGE_KEY_PORT] as number) || DEFAULT_PORT);

  await refreshState();
}

function updateTokenDisplay() {
  tokenEl.textContent = tokenVisible ? currentToken : '\u2022'.repeat(16);
}

async function refreshState() {
  try {
    const state: PopupState = await browser.runtime.sendMessage({ type: 'get_popup_state' });
    currentTabId = state.currentTabId;

    // Power button
    if (state.currentTabEnabled) {
      powerBtn.className = 'power-btn on';
      powerLabel.textContent = 'Active';
      powerLabel.className = 'power-label on';
    } else {
      powerBtn.className = 'power-btn off';
      powerLabel.textContent = 'Inactive';
      powerLabel.className = 'power-label';
    }

    // Connection
    const cs = state.connectionStatus;
    connectionEl.textContent = cs.charAt(0).toUpperCase() + cs.slice(1);
    connectionEl.className = `connection ${cs}`;

    // Stats
    statTabs.textContent = String(state.stats.enabledTabs);
    statEvents.textContent = String(state.stats.totalEvents);
    statErrors.textContent = String(state.stats.totalErrors);
  } catch {
    connectionEl.textContent = 'Unknown';
    connectionEl.className = 'connection disconnected';
  }
}

powerBtn.addEventListener('click', async () => {
  if (currentTabId == null) return;
  await browser.runtime.sendMessage({ type: 'toggle_tab', tabId: currentTabId });
  await refreshState();
});

toggleBtn.addEventListener('click', () => {
  tokenVisible = !tokenVisible;
  toggleBtn.textContent = tokenVisible ? 'Hide' : 'Show';
  updateTokenDisplay();
});

copyBtn.addEventListener('click', async () => {
  await navigator.clipboard.writeText(currentToken);
  copyBtn.textContent = 'Copied!';
  setTimeout(() => { copyBtn.textContent = 'Copy'; }, 1500);
});

savePortBtn.addEventListener('click', async () => {
  const port = parseInt(portInput.value, 10);
  if (port >= 1024 && port <= 65535) {
    await browser.storage.local.set({ [STORAGE_KEY_PORT]: port });
    savePortBtn.textContent = 'Saved!';
    setTimeout(() => { savePortBtn.textContent = 'Save'; }, 1500);
    browser.runtime.sendMessage({ type: 'reconnect' });
  }
});

regenBtn.addEventListener('click', async () => {
  currentToken = await generateAndStoreToken();
  updateTokenDisplay();
  browser.runtime.sendMessage({ type: 'reconnect' });
});

resetBtn.addEventListener('click', async () => {
  await browser.runtime.sendMessage({ type: 'reset_stats' });
  await refreshState();
});

init();
