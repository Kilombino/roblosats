/* eslint-disable @typescript-eslint/no-require-imports */
// Kilowallet's ROBLO tab: detection by user agent, its hostInfo bridge call, and the origin
// (Tor by default, clearnet only when chosen there).
type Win = {
  navigator: { userAgent: string };
  AndroidAppRobosats?: { hostInfo?: () => string };
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

  it('wallet functions stay hidden in this phase', () => {
    const { hostWalletEnabled } = require('./host');
    setWindow('AndroidRobosats Kilowallet/1.6.1');
    expect(hostWalletEnabled()).toBe(false);
  });
});
