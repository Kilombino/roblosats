/* eslint-disable @typescript-eslint/no-require-imports */
// Kilowallet's ROBLO tab: detection by user agent, its hostInfo bridge call, and the origin
// (Tor by default, clearnet only when chosen there).
type Win = {
  navigator: { userAgent: string };
  AndroidAppRobosats?: Record<string, unknown>;
  RobosatsSettings?: string;
};
const g = global as unknown as { window?: Win };

const setWindow = (ua: string, hostInfo?: () => string): void => {
  g.window = {
    navigator: { userAgent: ua },
    AndroidAppRobosats: hostInfo ? { hostInfo } : {},
    RobosatsSettings: 'mobile-basic',
  };
};

describe('host (Kilowallet)', () => {
  afterEach(() => {
    delete g.window;
    jest.resetModules();
  });

  it('is not Kilowallet in the Roblosats app or a browser', () => {
    const { isKilowallet, hostInfo } = require('./host');
    setWindow('AndroidRobosats');
    expect(isKilowallet()).toBe(false);
    expect(hostInfo()).toBeNull();
    setWindow('Mozilla/5.0 Firefox/128.0');
    expect(isKilowallet()).toBe(false);
  });

  it('is Kilowallet with its marker, and reads hostInfo', () => {
    const { isKilowallet, hostInfo } = require('./host');
    setWindow('AndroidRobosats Kilowallet/1.6.1-roblo', () =>
      JSON.stringify({ host: 'kilowallet', version: '1.6.1', clearnet: false }),
    );
    expect(isKilowallet()).toBe(true);
    expect(hostInfo()).toEqual({ host: 'kilowallet', version: '1.6.1', clearnet: false });
  });

  it('keeps the Android asset base outside the Kilowallet build', () => {
    const { mobileAssetBase } = require('./host');
    expect(mobileAssetBase).toBe('file:///android_asset/');
  });

  it('no wallet function without its bridge method', () => {
    const { hostWalletEnabled, hostCan } = require('./host');
    setWindow('AndroidRobosats Kilowallet/1.6.1');
    expect(hostWalletEnabled()).toBe(false);
    expect(hostCan('walletPay')).toBe(false);
  });

  it('kw4: hostInfo carries the platform and the features', () => {
    const { hostInfo } = require('./host');
    setWindow('AndroidRobosats Kilowallet/0.1.0-beta24 (desktop)', () =>
      JSON.stringify({
        host: 'kilowallet',
        version: '0.1.0-beta24',
        clearnet: false,
        platform: 'desktop',
        features: ['walletPay', 'walletAddress'],
      }),
    );
    expect(hostInfo()).toMatchObject({
      platform: 'desktop',
      features: ['walletPay', 'walletAddress'],
    });
  });

  const withMethods = (info?: object): void => {
    const noop = (): void => {};
    g.window = {
      navigator: { userAgent: 'AndroidRobosats Kilowallet/1.0.0 (iOS)' },
      AndroidAppRobosats: {
        walletPay: noop,
        walletAddress: noop,
        walletPayInvoice: noop,
        walletInvoice: noop,
        ...(info ? { hostInfo: () => JSON.stringify(info) } : {}),
      },
      RobosatsSettings: 'mobile-basic',
    };
  };

  it('kw4: each wallet function by its own method and feature (no Lightning on this host)', () => {
    const { hostCan, hostWalletEnabled } = require('./host');
    withMethods({
      host: 'kilowallet',
      version: '1.0.0',
      clearnet: false,
      platform: 'ios',
      features: ['walletPay', 'walletAddress'],
    });
    expect(hostCan('walletPay')).toBe(true);
    expect(hostCan('walletAddress')).toBe(true);
    expect(hostCan('walletPayInvoice')).toBe(false);
    expect(hostCan('walletInvoice')).toBe(false);
    expect(hostWalletEnabled()).toBe(true);
  });

  it('kw3 Android beta (no features list): every method it has is offered', () => {
    const { hostCan } = require('./host');
    withMethods({ host: 'kilowallet', version: '1.6.1', clearnet: false });
    expect(hostCan('walletPayInvoice')).toBe(true);
    expect(hostCan('walletInvoice')).toBe(true);
  });

  it('a Lightning method missing from the bridge hides only its own button', () => {
    const { hostCan } = require('./host');
    setWindow('AndroidRobosats Kilowallet/1.0.0 (iOS)');
    (g.window as Win).AndroidAppRobosats = { walletPay: () => {}, walletAddress: () => {} };
    expect(hostCan('walletPay')).toBe(true);
    expect(hostCan('walletPayInvoice')).toBe(false);
    expect(hostCan('walletInvoice')).toBe(false);
  });

  it('outside Kilowallet nothing is offered, even with the methods', () => {
    const { hostCan } = require('./host');
    withMethods();
    (g.window as Win).navigator.userAgent = 'AndroidRobosats';
    expect(hostCan('walletPay')).toBe(false);
  });
});
