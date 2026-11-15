/**
 * Regression tests for Federation.refreshFederationList (consensus adoption).
 *
 * When the winning federation document changes an identity field of a
 * coordinator already in the list (addresses, nostr key), the coordinator is
 * replaced by a fresh Coordinator instance. That instance starts without info
 * or limits, so it must be loaded like a newly added one; otherwise it stays a
 * skeleton (no fees in the maker form, no limits, no on-chain switch) until the
 * app restarts. A document that only spells absent addresses differently must
 * not replace anything.
 */

import { apiClient } from '../../services/api';
import defaultFederation from '../../../static/federation.json';
import { type Settings } from '../Settings.model';
import { Federation } from '../Federation.model';
import type { FederationDoc } from '../../services/FederationDiscovery';

const WINNER_HASH = 'b'.repeat(64);

let mockWinnerDoc: FederationDoc;

jest.mock('../../services/FederationDiscovery', () => ({
  voteOnHashes: () => ({
    winnerHash: 'b'.repeat(64),
    weightByHash: new Map([['b'.repeat(64), 10]]),
    totalWeight: 10,
    voterRows: [],
    now: new Date(),
  }),
  getSeedHash: () => Promise.resolve('a'.repeat(64)),
  fetchAndVerifyDoc: () => Promise.resolve(mockWinnerDoc),
}));

jest.mock('../../pgp', () => ({}));

jest.mock('../../utils/nostr', () => ({
  __esModule: true,
  default: () => null,
  setLiveCoordinators: () => {},
  verifyCoordinatorToken: () => false,
}));

jest.mock('../../services/DevFundProfile', () => ({
  fetchDevFundProfiles: () => Promise.resolve({}),
}));

jest.mock('../../i18n/Web', () => ({
  __esModule: true,
  default: { language: 'en', changeLanguage: () => {}, t: (k: string) => k },
}));

jest.mock('../../services/System', () => ({
  systemClient: {
    getItem: () => Promise.resolve(''),
    setItem: () => {},
    getSyncItem: () => undefined,
  },
}));

jest.mock('../../services/RoboPool', () => {
  class RoboPoolMock {
    updateRelays = jest.fn();
    close = jest.fn();
  }
  return { __esModule: true, default: RoboPoolMock, setFederationPubkeys: jest.fn() };
});

const INFO = { maker_fee: 0.0002, taker_fee: 0.0017, federation_hash: WINNER_HASH };
const LIMITS = {
  1: { code: 'USD', price: 1, min_amount: 1, max_amount: 2, max_bondless_amount: 0 },
};

const settings = {
  network: 'mainnet',
  selfhostedClient: false,
  client: 'web',
} as unknown as Settings;

const seed = defaultFederation as unknown as FederationDoc;

const cloneSeed = (): FederationDoc => JSON.parse(JSON.stringify(seed)) as FederationDoc;

/** Coordinators vote with the federation_hash of their (already loaded) info. */
const voteFor = (federation: Federation): void => {
  federation.getCoordinators().forEach((c) => {
    c.info = { ...INFO } as unknown as typeof c.info;
  });
};

const flush = async (): Promise<void> => {
  for (let i = 0; i < 5; i++) await new Promise((resolve) => setTimeout(resolve, 0));
};

describe('Federation.refreshFederationList — identity changes', () => {
  let getSpy: jest.SpyInstance;

  beforeAll(() => {
    // The constructor resolves the current host through window/document.
    const location = { protocol: 'http:', host: 'host.example', href: 'http://host.example/' };
    const win = { location } as unknown as Record<string, unknown>;
    win.parent = win;
    Object.assign(globalThis, { window: win, document: { location, referrer: '' } });
  });

  beforeEach(() => {
    getSpy = jest.spyOn(apiClient, 'get').mockImplementation((_url: string, path: string) => {
      if (path === '/api/info/') return Promise.resolve(INFO as unknown as null);
      if (path === '/api/limits/') return Promise.resolve(LIMITS as unknown as null);
      return Promise.resolve(null);
    });
    jest.spyOn(console, 'log').mockImplementation(() => {});
    jest.spyOn(console, 'group').mockImplementation(() => {});
    jest.spyOn(console, 'groupEnd').mockImplementation(() => {});
    jest.spyOn(console, 'table').mockImplementation(() => {});
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('loads info and limits of a coordinator replaced because its address changed', async () => {
    const alias = Object.keys(seed)[0];
    const federation = new Federation('onion', settings, 'http://host.example');
    voteFor(federation);
    const before = federation.getCoordinator(alias);

    mockWinnerDoc = cloneSeed();
    (mockWinnerDoc[alias].mainnet as Record<string, string>).onion = 'http://moved.onion';

    await federation.refreshFederationList();
    await flush();

    const after = federation.getCoordinator(alias);
    expect(after).not.toBe(before);
    expect(after?.url).toBe('http://moved.onion');
    expect(after?.info?.maker_fee).toBe(INFO.maker_fee);
    expect(after?.info?.taker_fee).toBe(INFO.taker_fee);
    expect(after?.loadingInfo).toBe(false);
    expect(Object.keys(after?.limits ?? {})).toHaveLength(1);
    expect(after?.loadingLimits).toBe(false);
    expect(getSpy).toHaveBeenCalledWith('http://moved.onion', '/api/info/', undefined, true);
    expect(getSpy).toHaveBeenCalledWith('http://moved.onion', '/api/limits/', undefined, true);
  });

  it('keeps loaded coordinators when the document only spells absent addresses differently', async () => {
    const federation = new Federation('onion', settings, 'http://host.example');
    voteFor(federation);
    const before = Object.fromEntries(
      Object.keys(seed).map((alias) => [alias, federation.getCoordinator(alias)]),
    );

    // null → '' / missing, and the network keys in another order: same identity.
    mockWinnerDoc = cloneSeed();
    for (const entry of Object.values(mockWinnerDoc)) {
      for (const net of ['mainnet', 'testnet'] as const) {
        const obj = entry[net] as Record<string, string | null>;
        const reordered: Record<string, string> = {};
        for (const k of ['i2p', 'clearnet', 'onion']) {
          if (obj[k] !== null && obj[k] !== undefined) reordered[k] = obj[k] as string;
          else if (k === 'clearnet') reordered[k] = '';
        }
        entry[net] = reordered;
      }
    }

    const warn = jest.spyOn(console, 'warn');
    await federation.refreshFederationList();
    await flush();

    expect(warn).not.toHaveBeenCalled();
    for (const alias of Object.keys(seed)) {
      expect(federation.getCoordinator(alias)).toBe(before[alias]);
    }
  });
});
