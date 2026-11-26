/**
 * 0.9.17 signed actions: every order action (onchain escrow_* / bond_* included) and every
 * order creation is signed silently by the robot of the order's coordinator.
 */
import Order from '../Order.model';
import { apiClient } from '../../services/api';

jest.mock('../../pgp', () => ({
  signCleartextMessage: jest.fn(
    async (content: string, key: string) => `SIGNED[${key}](${content})`,
  ),
}));

const robotFor = (alias: string, key: string): Record<string, unknown> => ({
  shortAlias: alias,
  token: 'token',
  encPrivKey: key,
  tokenSHA256: `sha-${alias}`,
  found: true,
  fetch: jest.fn(async () => undefined),
  getAuthHeaders: () => ({ tokenSHA256: `sha-${alias}` }),
});

const setup = (): {
  federation: unknown;
  slot: Record<string, unknown>;
  kilo: Record<string, unknown>;
} => {
  const kilo = robotFor('kilobot', 'KEY-KILOBOT');
  const other = robotFor('rosegarden', 'KEY-OTHER');
  const robots: Record<string, unknown> = { rosegarden: other, kilobot: kilo };
  const slot = {
    robots,
    getRobot: (alias?: string) => (alias ? robots[alias] : other),
    ensureRobot: (_f: unknown, alias: string) => robots[alias] ?? null,
  };
  const federation = { getCoordinator: () => ({ url: 'http://coord' }) };
  return { federation, slot, kilo };
};

afterEach(() => jest.restoreAllMocks());

describe('order actions are signed by the robot of the order coordinator', () => {
  it('an onchain escrow action carries {action}:{id}:{alias} signed with that robot key', async () => {
    const { federation, slot } = setup();
    const post = jest.spyOn(apiClient, 'post').mockResolvedValue({ id: 7, status: 6 });
    const order = new Order({ id: 7, shortAlias: 'kilobot' });
    await order.submitAction(
      federation as never,
      slot as never,
      {
        action: 'escrow_keys',
      } as never,
    );
    const [, path, body, auth] = post.mock.calls[0];
    expect(path).toBe('/api/order/?order_id=7');
    expect((body as Record<string, unknown>).signature).toBe(
      'SIGNED[KEY-KILOBOT](escrow_keys:7:kilobot)',
    );
    expect(auth).toEqual({ tokenSHA256: 'sha-kilobot' });
  });

  it('a dispute statement is signed too', async () => {
    const { federation, slot } = setup();
    const post = jest.spyOn(apiClient, 'post').mockResolvedValue({ id: 7 });
    const order = new Order({ id: 7, shortAlias: 'kilobot' });
    await order.submitAction(
      federation as never,
      slot as never,
      {
        action: 'submit_statement',
        statement: 'what happened',
      } as never,
    );
    const body = post.mock.calls[0][2] as Record<string, unknown>;
    expect(body.signature).toBe('SIGNED[KEY-KILOBOT](submit_statement:7:kilobot)');
    expect(body.statement).toBe('SIGNED[KEY-KILOBOT](what happened)');
  });

  it('on 1048 (stale key) it refreshes the robot once and retries', async () => {
    const { federation, slot, kilo } = setup();
    const post = jest
      .spyOn(apiClient, 'post')
      .mockResolvedValueOnce({ error_code: 1048 })
      .mockResolvedValueOnce({ id: 7, status: 9 });
    const order = new Order({ id: 7, shortAlias: 'kilobot' });
    await order.submitAction(federation as never, slot as never, { action: 'confirm' } as never);
    expect(post).toHaveBeenCalledTimes(2);
    expect(kilo.fetch).toHaveBeenCalledTimes(1);
  });

  it('a robot that never answered is fetched before signing (its key may be the old one)', async () => {
    const { federation, slot, kilo } = setup();
    delete kilo.found;
    jest.spyOn(apiClient, 'post').mockResolvedValue({ id: 7 });
    const order = new Order({ id: 7, shortAlias: 'kilobot' });
    await order.submitAction(federation as never, slot as never, { action: 'pause' } as never);
    expect(kilo.fetch).toHaveBeenCalledTimes(1);
  });
});

describe('making an order', () => {
  it('signs make:{alias}:{unix_ts} with the coordinator robot', async () => {
    const { federation, slot } = setup();
    const post = jest.spyOn(apiClient, 'post').mockResolvedValue({ id: 9 });
    const order = new Order({ shortAlias: 'kilobot', currency: 1, amount: 100 });
    await order.make(federation as never, slot as never);
    const body = post.mock.calls[0][2] as Record<string, unknown>;
    expect(body.signature).toMatch(/^SIGNED\[KEY-KILOBOT\]\(make:kilobot:\d+\)$/);
  });
});
