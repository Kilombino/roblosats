// Esplora/mempool-style explorer used by the rescue screen (no coordinator involved).
// Default: mempool.kilombino.com (the BLAKE2b chain). The user can set another one.
import { v4 as uuidv4 } from 'uuid';

import { type Utxo } from './rescue';

export const DEFAULT_EXPLORER = 'https://mempool.kilombino.com/api';
export const EXPLORER_SETTING = 'onchain_rescue_explorer';
// 0.9.12: an Esplora API of the SHA-256 chain ("Spamchain"), for Spamcoin sent by mistake to one
// of our on-chain addresses. In the Android app it goes through Tor when Tor is on, like the rest.
// blockstream.info answers through Tor exits; mempool.space refuses most of them (21 nov 2026).
export const DEFAULT_SPAMCHAIN_EXPLORER = 'https://blockstream.info/api';
export const ALT_SPAMCHAIN_EXPLORER = 'https://mempool.space/api';
export const SPAMCHAIN_EXPLORER_SETTING = 'onchain_rescue_explorer_sha256';

export class ExplorerError extends Error {}

const isAndroid = (): boolean =>
  typeof window !== 'undefined' && window.navigator.userAgent.includes('AndroidRobosats');

interface HttpResult {
  status: number;
  body: string;
}

const nativeHttp = async (
  method: 'GET' | 'POST',
  url: string,
  body: string,
): Promise<HttpResult> => {
  const raw = await new Promise<string>((resolve, reject) => {
    const uuid = uuidv4();
    window.AndroidAppRobosats?.sendText?.(uuid, method, url, 'text/plain', body);
    window.AndroidRobosats?.storePromise(uuid, resolve, reject);
  });
  const parsed = JSON.parse(raw) as { status: number; body: string };
  const bytes = Uint8Array.from(atob(parsed.body), (c) => c.charCodeAt(0));
  return { status: parsed.status, body: new TextDecoder().decode(bytes) };
};

const http = async (method: 'GET' | 'POST', url: string, body = ''): Promise<HttpResult> => {
  // The Android app sends it through its own client (Tor when on), like every other request.
  if (isAndroid() && window.AndroidAppRobosats?.sendText)
    return await nativeHttp(method, url, body);
  const r = await fetch(url, {
    method,
    body: method === 'POST' ? body : undefined,
    headers: method === 'POST' ? { 'Content-Type': 'text/plain' } : undefined,
  });
  return { status: r.status, body: await r.text() };
};

export class Explorer {
  constructor(public base: string = DEFAULT_EXPLORER) {
    this.base = base.replace(/\/+$/, '');
  }

  private async get(path: string): Promise<string> {
    const r = await http('GET', this.base + path);
    if (r.status === 404) throw new ExplorerError('not found');
    if (r.status >= 400) throw new ExplorerError(r.body.slice(0, 200) || `HTTP ${r.status}`);
    return r.body;
  }

  async tipHeight(): Promise<number> {
    return Number((await this.get('/blocks/tip/height')).trim());
  }

  /** Height of the block that confirmed [txid], or null while unconfirmed (or unknown). */
  async confirmedAt(txid: string): Promise<number | null> {
    try {
      const s = JSON.parse(await this.get(`/tx/${txid}/status`)) as {
        confirmed: boolean;
        block_height?: number;
      };
      return s.confirmed ? (s.block_height ?? null) : null;
    } catch (e) {
      if (e instanceof ExplorerError && e.message === 'not found') return null;
      throw e;
    }
  }

  /** Who spent [txid]:[vout], if anyone (txid of the spender). */
  async spentBy(txid: string, vout: number): Promise<string | null> {
    const s = JSON.parse(await this.get(`/tx/${txid}/outspend/${vout}`)) as {
      spent: boolean;
      txid?: string;
    };
    return s.spent ? (s.txid ?? 'unknown') : null;
  }

  async outputValue(txid: string, vout: number): Promise<number> {
    const t = JSON.parse(await this.get(`/tx/${txid}`)) as { vout: Array<{ value: number }> };
    return t.vout[vout].value;
  }

  async utxos(address: string): Promise<Array<Utxo & { height: number | null }>> {
    const list = JSON.parse(await this.get(`/address/${address}/utxo`)) as Array<{
      txid: string;
      vout: number;
      value: number;
      status?: { confirmed: boolean; block_height?: number };
    }>;
    return list.map((u) => ({
      txid: u.txid,
      vout: u.vout,
      value: u.value,
      height: u.status?.confirmed ? (u.status.block_height ?? null) : null,
    }));
  }

  async feeRate(): Promise<number> {
    try {
      const f = JSON.parse(await this.get('/v1/fees/recommended')) as { halfHourFee?: number };
      return Math.max(1, Math.ceil(f.halfHourFee ?? 2));
    } catch {
      return 2;
    }
  }

  async broadcast(hex: string): Promise<string> {
    const r = await http('POST', this.base + '/tx', hex);
    if (r.status >= 400) throw new ExplorerError(r.body.slice(0, 300) || `HTTP ${r.status}`);
    return r.body.trim();
  }
}
