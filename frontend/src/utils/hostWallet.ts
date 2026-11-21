// Kilowallet's wallet functions for the client inside its ROBLO tab. Each one opens a native
// sheet in Kilowallet (amount and address fixed, fingerprint) and resolves with a txid, a payment
// hash, an address or an invoice; the client never sees keys, balances or coins.
import { isKilowallet } from './host';

const newUuid = (): string =>
  typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
        const r = Math.floor(Math.random() * 16);
        return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
      });

/** Kilowallet offers its wallet here (an older Kilowallet, or the Roblosats app, does not). */
export const hostWalletAvailable = (): boolean =>
  isKilowallet() && typeof window.AndroidAppRobosats?.walletPay === 'function';

const ask = async (call: (uuid: string) => void): Promise<string> =>
  await new Promise<string>((resolve, reject) => {
    const uuid = newUuid();
    window.AndroidRobosats?.storePromise(uuid, resolve, reject);
    call(uuid);
  });

/** Pays [sats] to [address] from Kilowallet's hot wallet; resolves the txid. */
export const walletPay = async (
  address: string,
  sats: number,
  orderRef: string,
  feeHint: number,
): Promise<string> =>
  await ask((u) => window.AndroidAppRobosats?.walletPay?.(u, address, sats, orderRef, feeHint));

/** Pays (locks) a hold invoice with Kilowallet's Lightning node; resolves when it is locked. */
export const walletPayInvoice = async (bolt11: string, orderRef: string): Promise<string> =>
  await ask((u) => window.AndroidAppRobosats?.walletPayInvoice?.(u, bolt11, orderRef));

/** An address of Kilowallet's hot wallet the user picks; resolves the address. */
export const walletAddress = async (purpose: string, orderRef: string): Promise<string> =>
  await ask((u) => window.AndroidAppRobosats?.walletAddress?.(u, purpose, orderRef));

/** An invoice from Kilowallet's Lightning node for [sats]; resolves the bolt11. */
export const walletInvoice = async (sats: number, memo: string): Promise<string> =>
  await ask((u) => window.AndroidAppRobosats?.walletInvoice?.(u, sats, memo));
