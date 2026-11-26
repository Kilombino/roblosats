// 0.9.6: the update notice (as in Kilowallet). On Android the app checks GitHub itself (through
// its Tor) and can install the checked APK; on the web it only shows what is new.
import { getClientVersion } from './checkVer';

export const RELEASES_API = 'https://api.github.com/repos/Kilombino/roblosats/releases/latest';
export const RELEASES_PAGE = 'https://github.com/Kilombino/roblosats/releases/latest';

export interface UpdateInfo {
  status: 'newer' | 'uptodate' | 'unreachable';
  version?: string;
  current?: string;
  url?: string;
  notes?: string;
  canInstall?: boolean;
}

const parse = (v: string): { nums: number[]; pre: boolean } => {
  const clean = v.replace(/^v/, '').trim();
  return {
    nums: clean
      .split('-')[0]
      .split('.')
      .map((x) => Number.parseInt(x, 10) || 0),
    pre: clean.includes('-'),
  };
};

/** A v4 UUID for the Android bridge (crypto.randomUUID in every WebView we run on). */
const newUuid = (): string =>
  typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
        const r = Math.floor(Math.random() * 16);
        return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
      });

/** True when remote is newer than current (a final release beats its own betas). */
export const isNewer = (remote: string, current: string): boolean => {
  const r = parse(remote);
  const c = parse(current);
  for (let i = 0; i < Math.max(r.nums.length, c.nums.length); i++) {
    const a = r.nums[i] ?? 0;
    const b = c.nums[i] ?? 0;
    if (a !== b) return a > b;
  }
  return c.pre && !r.pre;
};

/** The release notes as people read them: without the SHA-256 block and its heading. */
export const readableNotes = (body: string): string =>
  body
    .replace(/###\s*SHA-256\s*\n```[\s\S]*?```/g, '')
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/\*\*/g, '')
    .replace(/\n{3,}/g, '\n\n')
    .trim();

/** This client's version as "0.9.6" (or "0.9.14-beta2": a beta keeps its suffix, so the final
 * release is still offered to it; isNewer reads the suffix). */
export const clientVersionString = (): string => getClientVersion().long.replace(/^v/, '');

/** Web: ask GitHub for the latest release and compare. Never throws. */
export const checkUpdateWeb = async (
  fetcher: typeof fetch = fetch,
  current: string = clientVersionString(),
): Promise<UpdateInfo> => {
  try {
    const r = await fetcher(RELEASES_API, { headers: { Accept: 'application/vnd.github+json' } });
    if (!r.ok) return { status: 'unreachable', current };
    const o = (await r.json()) as { tag_name?: string; html_url?: string; body?: string };
    const tag = (o.tag_name ?? '').replace(/^v/, '');
    if (!tag || !isNewer(tag, current)) return { status: 'uptodate', current };
    return {
      status: 'newer',
      version: tag,
      current,
      url: o.html_url ?? RELEASES_PAGE,
      notes: o.body ?? '',
      canInstall: false,
    };
  } catch {
    return { status: 'unreachable', current };
  }
};

/** Android: the app checks (through its own Tor when it uses it). */
export const checkUpdateAndroid = async (): Promise<UpdateInfo> =>
  await new Promise<UpdateInfo>((resolve) => {
    const app = window.AndroidAppRobosats;
    if (!app?.checkUpdate || !window.AndroidRobosats) {
      resolve({ status: 'unreachable' });
      return;
    }
    const uuid: string = newUuid();
    window.AndroidRobosats.storePromise(
      uuid,
      (res: string) => {
        try {
          resolve(JSON.parse(res) as UpdateInfo);
        } catch {
          resolve({ status: 'unreachable' });
        }
      },
      () => {
        resolve({ status: 'unreachable' });
      },
    );
    app.checkUpdate(uuid);
  });

export const checkUpdate = async (client: string): Promise<UpdateInfo> =>
  client === 'mobile' && window.AndroidAppRobosats?.checkUpdate
    ? await checkUpdateAndroid()
    : await checkUpdateWeb();
