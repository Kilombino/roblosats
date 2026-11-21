/**
 * 0.9.13: in Clearnet mode the app connects once at start (before Tor is ready) and again when
 * Tor is ON. The second connection used to reuse the first one's in-flight info batch (no new
 * requests) while clearing the info that batch had already received: the federation vote ran
 * with 0 voters ("no majority" at every start). Also: a vote without majority votes again once
 * the coordinators that were late (onions through a Tor that just started) answer.
 */
import { apiClient } from '../../services/api';
import { type Settings } from '../Settings.model';
import { Federation } from '../Federation.model';

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
    relays = [];
    updateRelays = jest.fn();
    close = jest.fn();
  }
  return { __esModule: true, default: RoboPoolMock, setFederationPubkeys: jest.fn() };
});

const HASH = 'c'.repeat(64);
const INFO = {
  maker_fee: 0.0002,
  taker_fee: 0.0017,
  federation_hash: HASH,
  version: { major: 0, minor: 9, patch: 13 },
};
const settings = {
  network: 'mainnet',
  selfhostedClient: false,
  client: 'web',
  connection: 'api',
} as unknown as Settings;

const flush = async (): Promise<void> => {
  for (let i = 0; i < 10; i++) await new Promise((resolve) => setTimeout(resolve, 0));
};

interface Deferred {
  resolve: (v: unknown) => void;
}

describe('Federation reconnection while info is in flight', () => {
  const slow: Record<string, Deferred[]> = {};
  let fastAlias = '';
  let votes: number[];

  beforeAll(() => {
    const location = { protocol: 'http:', host: 'host.example', href: 'http://host.example/' };
    const win = { location } as unknown as Record<string, unknown>;
    win.parent = win;
    Object.assign(globalThis, { window: win, document: { location, referrer: '' } });
  });

  const setup = (): Federation => {
    const federation = new Federation('onion', settings, 'http://host.example');
    fastAlias = federation.getCoordinatorsAlias()[0];
    const fastUrl = federation.getCoordinator(fastAlias)!.url;
    votes = [];
    jest.spyOn(apiClient, 'get').mockImplementation((url: string, path: string) => {
      if (path !== '/api/info/') return Promise.resolve(null);
      if (url === fastUrl) return Promise.resolve(INFO as unknown as null);
      // the onion-only coordinators answer only when the test says so
      return new Promise((resolve) => {
        (slow[url] ??= []).push({ resolve });
      });
    });
    jest.spyOn(federation, 'loadDevFund').mockResolvedValue(undefined);
    jest.spyOn(federation, 'refreshFederationList').mockImplementation(async () => {
      const voters = federation.getCoordinators().filter((c) => c.info !== undefined).length;
      votes.push(voters);
      federation.majorityFederationHash = voters >= 2 ? HASH : null;
    });
    return federation;
  };

  const answerSlow = (): void => {
    Object.values(slow).forEach((list) => list.splice(0).forEach((d) => d.resolve(INFO)));
  };

  beforeEach(() => {
    Object.keys(slow).forEach((k) => delete slow[k]);
    jest.spyOn(console, 'log').mockImplementation(() => {});
  });

  afterEach(() => jest.restoreAllMocks());

  it('a second connection asks the answered coordinators again instead of reusing the old batch', async () => {
    const federation = setup();
    federation.setConnection('onion', settings, 'http://host.example', 'any');
    await flush();
    expect(federation.getCoordinator(fastAlias)!.info).toBeDefined();

    // Tor is ON: the app connects again; the onion coordinators are still on their way
    federation.setConnection('onion', settings, 'http://host.example', 'any');
    await flush();
    // the fast coordinator was asked again and its info is back
    expect(federation.getCoordinator(fastAlias)!.info).toBeDefined();

    answerSlow();
    await flush();
    expect(votes.length).toBeGreaterThan(0);
    expect(votes[votes.length - 1]).toBe(federation.getCoordinators().length);
    expect(federation.majorityFederationHash).toBe(HASH);
  });

  it('votes again when coordinators that were late answer after a vote without majority', async () => {
    jest.useFakeTimers({ doNotFake: ['setImmediate', 'nextTick', 'queueMicrotask'] });
    try {
      const federation = setup();
      federation.setConnection('onion', settings, 'http://host.example', 'any');
      for (let i = 0; i < 5; i++) await Promise.resolve();
      // the 15 s wall: the vote runs with only the fast coordinator
      await jest.advanceTimersByTimeAsync(16_000);
      expect(votes).toEqual([1]);
      expect(federation.majorityFederationHash).toBeNull();

      // the onions answer later (Tor's first circuits)
      answerSlow();
      await jest.advanceTimersByTimeAsync(10);
      expect(votes[0]).toBe(1);
      expect(votes.length).toBeGreaterThan(1);
      expect(votes[votes.length - 1]).toBeGreaterThanOrEqual(2);
      expect(federation.majorityFederationHash).toBe(HASH);
    } finally {
      jest.useRealTimers();
    }
  });
});
