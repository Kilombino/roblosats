// 0.9.7: addresses this app itself gave (payout, bond return). The app only pre-signs the
// automatic return of a bond to one of them, so a coordinator cannot slip in its own address.
import { systemClient } from '../services/System';

const KEY = 'my_onchain_addresses';
const MAX = 50;

const read = async (): Promise<string[]> => {
  try {
    const raw = await systemClient.getItem(KEY);
    const list = raw ? (JSON.parse(raw) as unknown) : [];
    return Array.isArray(list) ? list.filter((a): a is string => typeof a === 'string') : [];
  } catch {
    return [];
  }
};

export const rememberMyAddress = async (address: string): Promise<void> => {
  const a = address.trim();
  if (!a) return;
  const list = (await read()).filter((x) => x !== a);
  list.unshift(a);
  systemClient.setItem(KEY, JSON.stringify(list.slice(0, MAX)));
};

export const isMyAddress = async (address: string | undefined): Promise<boolean> =>
  Boolean(address) && (await read()).includes((address ?? '').trim());

/** The return address typed in the make/take form for this robot, until its bond key is sent. */
export const pendingReturnKey = (token: string): string => `bond_return_${token.slice(0, 12)}`;
