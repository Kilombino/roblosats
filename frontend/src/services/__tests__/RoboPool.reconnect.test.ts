import type { Settings } from '../../models';
import RoboPool, { RECONNECT_MIN_MS } from '../RoboPool';
import { websocketClient, WebsocketState, type WebsocketConnection } from '../Websocket';

jest.mock('../System', () => ({ systemClient: { setItem: jest.fn() } }));

// 0.9.10 (#29): a phone whose relay socket dropped mid-trade never saw a chat message again:
// the pool did not reconnect, nor subscribe again.
function relay() {
  let onMessage: (message: object) => void = () => {};
  let onClose: () => void = () => {};
  let state: number = WebsocketState.OPEN;
  const connection: WebsocketConnection = {
    send: jest.fn(),
    close: jest.fn(),
    getReadyState: () => state,
    onMessage: (callback) => {
      onMessage = callback;
    },
    onError: () => {},
    onClose: (callback) => {
      onClose = callback;
    },
  };
  return {
    connection,
    receive: (...message: unknown[]) => onMessage({ data: JSON.stringify(message) }),
    drop: () => {
      state = WebsocketState.CLOSED;
      onClose();
    },
    sent: () => jest.mocked(connection.send).mock.calls.map(([m]) => JSON.parse(m)),
  };
}

const flush = async (): Promise<void> => {
  for (let i = 0; i < 5; i++) await Promise.resolve();
};

describe('RoboPool reconnects a dropped relay', () => {
  let pool: RoboPool;
  let first: ReturnType<typeof relay>;
  let second: ReturnType<typeof relay>;

  beforeEach(async () => {
    jest.useFakeTimers();
    jest.spyOn(console, 'log').mockImplementation(() => {});
    jest.spyOn(console, 'error').mockImplementation(() => {});
    first = relay();
    second = relay();
    jest
      .spyOn(websocketClient, 'open')
      .mockResolvedValueOnce(first.connection)
      .mockResolvedValue(second.connection);
    pool = new RoboPool({ network: 'mainnet' } as Settings);
    pool.connect(['ws://relay.onion/relay/']);
    await flush();
  });

  afterEach(() => {
    pool.close();
    jest.clearAllTimers();
    jest.useRealTimers();
    jest.restoreAllMocks();
  });

  it('opens the relay again and repeats the open subscriptions, so the chat keeps arriving', async () => {
    const events: string[] = [];
    pool.updateNotificationSubscriptions({
      pubkeys: ['ab'.repeat(32)],
      events: { onevent: (e) => events.push(e.id), oneose: () => {} },
    });
    jest.advanceTimersByTime(600);
    const req = first.sent().find(([t]) => t === 'REQ');
    expect(req).toBeTruthy();

    first.drop();
    expect(pool.webSockets['ws://relay.onion/relay/']).toBeNull();
    jest.advanceTimersByTime(RECONNECT_MIN_MS);
    await flush();
    expect(websocketClient.open).toHaveBeenCalledTimes(2);
    expect(pool.webSockets['ws://relay.onion/relay/']).toBe(second.connection);
    // the same subscription goes to the new socket
    expect(second.sent()).toContainEqual(req);
    // and what it brings reaches the chat
    second.receive('EVENT', req![1], { id: 'gift-wrap-1', kind: 1059, tags: [], content: '' });
    expect(events).toEqual(['gift-wrap-1']);
  });

  it('a chat message sent while the relay is down goes out once it is back', async () => {
    first.drop();
    pool.sendEvent({ id: 'mine', kind: 1059 } as never);
    jest.advanceTimersByTime(RECONNECT_MIN_MS + 600);
    await flush();
    jest.advanceTimersByTime(600);
    expect(second.sent()).toContainEqual(['EVENT', { id: 'mine', kind: 1059 }]);
    expect(first.connection.send).not.toHaveBeenCalledWith(expect.stringContaining('"mine"'));
  });

  it('a closed subscription is not repeated', async () => {
    pool.updateNotificationSubscriptions({
      pubkeys: ['cd'.repeat(32)],
      events: { onevent: () => {}, oneose: () => {} },
    });
    pool.clearNotificationSubscriptions();
    expect(pool.activeRequests.size).toBe(0);
  });

  it('retries a relay that does not answer, with a growing delay', async () => {
    jest.mocked(websocketClient.open).mockReset();
    jest.mocked(websocketClient.open).mockRejectedValue(new Error('no route'));
    first.drop();
    jest.advanceTimersByTime(RECONNECT_MIN_MS);
    await flush();
    expect(websocketClient.open).toHaveBeenCalledTimes(1);
    jest.advanceTimersByTime(RECONNECT_MIN_MS);
    await flush();
    expect(websocketClient.open).toHaveBeenCalledTimes(1); // the next try waits twice as long
    jest.advanceTimersByTime(RECONNECT_MIN_MS * 2);
    await flush();
    expect(websocketClient.open).toHaveBeenCalledTimes(2);
  });
});
