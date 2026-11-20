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

export interface HostInfo {
  host: 'kilowallet';
  version: string;
  /** Clearnet mode chosen in Roblosats' settings (0.9.11: also isClearnet / settings_clearnet). */
  clearnet: boolean;
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
 * Wallet functions Kilowallet will offer in a later phase (pay a bond or a deposit from the hot
 * wallet, use one of its addresses, an invoice from its Lightning node). None is shown yet: the
 * client keeps paying by QR / copy until each one is reviewed.
 */
export const hostWalletEnabled = (): boolean => false;
