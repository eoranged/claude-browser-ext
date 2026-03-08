import { STORAGE_KEY_TOKEN } from './constants';

export async function getToken(): Promise<string> {
  const data = await browser.storage.local.get(STORAGE_KEY_TOKEN);
  if (data[STORAGE_KEY_TOKEN]) {
    return data[STORAGE_KEY_TOKEN] as string;
  }
  return generateAndStoreToken();
}

export async function generateAndStoreToken(): Promise<string> {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  const token = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
  await browser.storage.local.set({ [STORAGE_KEY_TOKEN]: token });
  return token;
}
