/* eslint-disable @typescript-eslint/no-require-imports */
// Kilowallet's wallet functions: offered only inside Kilowallet, each call answered once by uuid.
type Promised = { resolve: (v: string) => void; reject: (e: string) => void };
const g = global as unknown as { window?: Record<string, unknown> };

describe('hostWallet', () => {
  afterEach(() => {
    delete g.window;
    jest.resetModules();
  });

  const setup = (ua: string, app: Record<string, unknown>): Record<string, Promised> => {
    const promises: Record<string, Promised> = {};
    g.window = {
      navigator: { userAgent: ua },
      AndroidAppRobosats: app,
      AndroidRobosats: {
        storePromise: (u: string, resolve: (v: string) => void, reject: (e: string) => void) => {
          promises[u] = { resolve, reject };
        },
      },
    };
    return promises;
  };

  it('is not offered outside Kilowallet or without the bridge method', () => {
    setup('AndroidRobosats', { walletPay: () => {} });
    expect(require('./hostWallet').hostWalletAvailable()).toBe(false);
    jest.resetModules();
    setup('AndroidRobosats Kilowallet/1.6.1-roblo', {});
    expect(require('./hostWallet').hostWalletAvailable()).toBe(false);
  });

  it('pays and gets the txid back through the uuid', async () => {
    let args: unknown[] = [];
    const promises = setup('AndroidRobosats Kilowallet/1.6.1-roblo', {
      walletPay: (...a: unknown[]) => {
        args = a;
      },
    });
    const hw = require('./hostWallet');
    expect(hw.hostWalletAvailable()).toBe(true);
    const p = hw.walletPay('bc1qxyz', 3330, 'kilobot/24', 1.2);
    expect(args.slice(1)).toEqual(['bc1qxyz', 3330, 'kilobot/24', 1.2]);
    promises[args[0] as string].resolve('ab'.repeat(32));
    await expect(p).resolves.toBe('ab'.repeat(32));
  });

  it('a cancelled sheet rejects', async () => {
    let uuid = '';
    const promises = setup('AndroidRobosats Kilowallet/1.6.1-roblo', {
      walletPay: () => {},
      walletAddress: (u: string) => {
        uuid = u;
      },
    });
    const p = require('./hostWallet').walletAddress('payout', 'kilobot/24');
    promises[uuid].reject('cancelled');
    await expect(p).rejects.toBe('cancelled');
  });
});
