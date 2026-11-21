// Kilowallet embeds this client in its ROBLO tab (webpack `--env target=kilowallet`): the same
// Android bridge (AndroidAppRobosats), implemented by Kilowallet on its own engines (its Tor,
// its encrypted storage), the bundle served from a local https origin instead of file://.

// Set by webpack's DefinePlugin per target; undefined in tests and in the web builds.
// eslint-disable-next-line @typescript-eslint/naming-convention
declare const __MOBILE_ASSET_BASE__: string | undefined;

/** Where the Android builds find their bundled assets (sounds, coordinator avatars). */
export const mobileAssetBase: string =
  typeof __MOBILE_ASSET_BASE__ === 'string' ? __MOBILE_ASSET_BASE__ : 'file:///android_asset/';

/** Running inside Kilowallet (its WebView adds "Kilowallet/<version>" to the user agent). */
export const isKilowallet = (): boolean =>
  typeof window !== 'undefined' && /\bKilowallet\/\S+/.test(window.navigator.userAgent);

/** Where Kilowallet runs this client (kw4): its phone app, its desktop app or its iPhone app. */
export type HostPlatform = 'android' | 'desktop' | 'ios';

/** The wallet functions Kilowallet may offer; each button shows only when its own one is there. */
export type HostMethod = 'walletPay' | 'walletPayInvoice' | 'walletAddress' | 'walletInvoice';

export interface HostInfo {
  host: 'kilowallet';
  version: string;
  /** Clearnet mode chosen in Roblosats' settings (0.9.11: also isClearnet / settings_clearnet). */
  clearnet: boolean;
  /** kw4: which Kilowallet app (absent in the kw3 Android beta). */
  platform?: HostPlatform;
  /**
   * kw4: the wallet functions this Kilowallet offers right now (absent in the kw3 Android beta,
   * which offers all four). A Kilowallet without Lightning leaves out walletPayInvoice and
   * walletInvoice even if its bridge has the methods.
   */
  features?: string[];
}

/** What Kilowallet says about itself; null outside it or when it does not answer. */
export const hostInfo = (): HostInfo | null => {
  if (!isKilowallet()) return null;
  try {
    const raw = window.AndroidAppRobosats?.hostInfo?.();
    return raw ? (JSON.parse(raw) as HostInfo) : null;
  } catch {
    return null;
  }
};

/**
 * Kilowallet offers this one wallet function here: its bridge has the method and, when it lists
 * its features (kw4), the function is among them. Lightning buttons ask for walletPayInvoice or
 * walletInvoice, not for walletPay: a Kilowallet may pay on-chain without having Lightning.
 */
export const hostCan = (method: HostMethod): boolean => {
  if (!isKilowallet()) return false;
  const bridge = window.AndroidAppRobosats as Record<string, unknown> | undefined;
  if (typeof bridge?.[method] !== 'function') return false;
  const features = hostInfo()?.features;
  return Array.isArray(features) ? features.includes(method) : true;
};

/**
 * Kilowallet offers its wallet at all (pay a bond or a deposit from the hot wallet, use one of
 * its addresses, an invoice from its Lightning node): see utils/hostWallet.ts.
 */
export const hostWalletEnabled = (): boolean =>
  (['walletPay', 'walletPayInvoice', 'walletAddress', 'walletInvoice'] as HostMethod[]).some(
    hostCan,
  );
