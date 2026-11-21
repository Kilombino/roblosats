/**
 * Regression tests for Coordinator.updateUrl / getRelayUrl.
 *
 * Coordinators without an address for the current network/origin (e.g.
 * testnet.onion is null for bazaar/alice/freeport/ammanaya) must end up with
 * an empty url — never the strings 'null'/'undefined'. Previously
 * `String(null)` materialized the url 'null' and getRelayUrl() produced the
 * relative path 'null/relay/', which ReconnectingWebSocket resolved against
 * the current page (e.g. ws://<host>/offers/null/relay/ → connection refused).
 */

import Coordinator, { type CoordinatorConfig } from '../Coordinator.model';
import { type Settings } from '../Settings.model';

const baseConfig = {
  longAlias: 'Test Coord',
  shortAlias: 'test',
  description: '',
  motto: '',
  color: '#000',
  established: '2024-01-01',
  badges: { donatesToDevFund: 0 },
  nostrHexPubkey: 'a'.repeat(64),
  mainnet: {
    clearnet: 'https://test.example.com',
    onion: 'http://testexampleonion1234.onion',
    i2p: undefined,
  },
  testnet: { clearnet: null, onion: null, i2p: undefined },
};

const makeSettings = (overrides: Record<string, unknown> = {}): Settings =>
  ({ network: 'mainnet', selfhostedClient: false, ...overrides }) as unknown as Settings;

const makeCoordinator = (
  origin: 'onion' | 'clearnet',
  settings: Settings,
  hostUrl = 'https://host.example',
): Coordinator =>
  new Coordinator(baseConfig as unknown as CoordinatorConfig, origin, settings, hostUrl);

describe('Coordinator.updateUrl / getRelayUrl', () => {
  it('uses the onion address for the onion origin', () => {
    const coordinator = makeCoordinator('onion', makeSettings());
    expect(coordinator.url).toBe('http://testexampleonion1234.onion');
    expect(coordinator.getRelayUrl()).toBe('ws://testexampleonion1234.onion/relay/');
  });

  it('empty url and relay when the entry has no address for the network/origin', () => {
    const coordinator = makeCoordinator('onion', makeSettings({ network: 'testnet' }));
    expect(coordinator.url).toBe('');
    expect(coordinator.getRelayUrl()).toBe('');
  });

  it('clearnet address resolves with wss relay protocol', () => {
    const coordinator = makeCoordinator('clearnet', makeSettings());
    expect(coordinator.url).toBe('https://test.example.com');
    expect(coordinator.getRelayUrl()).toBe('wss://test.example.com/relay/');
  });

  it('selfhosted client builds the proxied host url', () => {
    const coordinator = makeCoordinator(
      'onion',
      makeSettings({ selfhostedClient: true }),
      'http://host.example',
    );
    expect(coordinator.url).toBe('http://host.example/mainnet/test');
    expect(coordinator.getRelayUrl()).toBe('ws://host.example/mainnet/test/relay/');
  });

  it('loaders resolve their loading flags when the coordinator has no url', async () => {
    const coordinator = makeCoordinator('onion', makeSettings({ network: 'testnet' }));
    expect(coordinator.url).toBe('');
    await coordinator.loadInfo();
    await coordinator.loadLimits();
    await coordinator.loadBook();
    expect(coordinator.loadingInfo).toBe(false);
    expect(coordinator.loadingLimits).toBe(false);
    expect(coordinator.loadingBook).toBe(false);
  });
});

describe('0.9.11: Clearnet mode (beta) in the app', () => {
  it('a coordinator with a clearnet address is reached there, over https and wss', () => {
    const c = makeCoordinator('onion', makeSettings({ client: 'mobile', clearnet: true }));
    expect(c.url).toBe('https://test.example.com');
    expect(c.getRelayUrl()).toBe('wss://test.example.com/relay/');
  });

  it('an onion-only coordinator keeps its onion (Tor)', () => {
    const cfg = {
      ...baseConfig,
      mainnet: { clearnet: null, onion: 'http://onlyonion.onion', i2p: undefined },
    };
    const c = new Coordinator(
      cfg as unknown as CoordinatorConfig,
      'onion',
      makeSettings({ client: 'mobile', clearnet: true }),
      'https://host.example',
    );
    expect(c.url).toBe('http://onlyonion.onion');
  });

  it('off by default, and never on the web client', () => {
    expect(makeCoordinator('onion', makeSettings({ client: 'mobile' })).url).toBe(
      'http://testexampleonion1234.onion',
    );
    expect(makeCoordinator('onion', makeSettings({ client: 'web', clearnet: true })).url).toBe(
      'http://testexampleonion1234.onion',
    );
  });
});

describe('0.9.11: the clearnet hosts the native side may reach directly', () => {
  it('only the coordinators clearnet hosts, never an onion', () => {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const { clearnetHosts } = require('../../utils/clearnetHosts');
    const fake = {
      getCoordinators: () => [
        {
          mainnet: { clearnet: 'https://Kilobot.Kilombino.com', onion: 'http://x.onion' },
          testnet: {},
        },
        {
          mainnet: { clearnet: null, onion: 'http://y.onion' },
          testnet: { clearnet: 'https://t.example' },
        },
      ],
    };
    expect(clearnetHosts(fake)).toEqual(['kilobot.kilombino.com', 't.example']);
  });
});
