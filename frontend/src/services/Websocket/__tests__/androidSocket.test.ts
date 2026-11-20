let mockN = 0;
jest.mock('uuid', () => ({
  v4: () => `00000000-0000-4000-8000-${String(++mockN).padStart(12, '0')}`,
}));

// 0.9.10 (#29): on Android a socket that failed after opening still said OPEN for ever.
describe('Android websocket bridge', () => {
  const sendWsMessage = jest.fn();
  const g = globalThis as unknown as { window: Record<string, unknown> };

  it('a native failure closes the connection and stops sending into it', async () => {
    g.window = { navigator: { userAgent: 'AndroidRobosats' } };
    /* eslint-disable @typescript-eslint/no-require-imports */
    const AndroidRobosats = require('../../Android').default;
    const { WebsocketState, websocketClient } = require('..');
    /* eslint-enable @typescript-eslint/no-require-imports */
    const bridge = new AndroidRobosats();
    g.window.AndroidRobosats = bridge;
    g.window.AndroidAppRobosats = {
      openWS: (uuid: string) => setTimeout(() => bridge.onResolvePromise(uuid, 'ok'), 0),
      sendWsMessage,
    };
    const conn = await websocketClient.open('ws://relay.onion/relay/');
    const closed = jest.fn();
    conn.onClose(closed);
    expect(conn.getReadyState()).toBe(WebsocketState.OPEN);
    conn.send('["REQ","a",{}]');
    expect(sendWsMessage).toHaveBeenCalledTimes(1);
    bridge.onWsError('ws://relay.onion/relay/');
    expect(conn.getReadyState()).toBe(WebsocketState.CLOSED);
    expect(closed).toHaveBeenCalledTimes(1);
    conn.send('["REQ","b",{}]');
    expect(sendWsMessage).toHaveBeenCalledTimes(1);
    bridge.onWsClose('ws://relay.onion/relay/');
    expect(closed).toHaveBeenCalledTimes(1); // told once
  });

  it('every send has its promise: no "No promise found" and a refused send closes', async () => {
    const AndroidRobosats = require('../../Android').default; // eslint-disable-line @typescript-eslint/no-require-imports
    const { WebsocketState, websocketClient } = require('..'); // eslint-disable-line @typescript-eslint/no-require-imports
    const bridge = new AndroidRobosats();
    g.window.AndroidRobosats = bridge;
    const answers: string[] = [];
    g.window.AndroidAppRobosats = {
      openWS: (uuid: string) => setTimeout(() => bridge.onResolvePromise(uuid, 'ok'), 0),
      sendWsMessage: (uuid: string) => answers.push(uuid),
    };
    const warn = jest.spyOn(console, 'warn').mockImplementation(() => {});
    const conn = await websocketClient.open('ws://relay2.onion/relay/');
    const closed = jest.fn();
    conn.onClose(closed);
    conn.send('["EVENT",{}]');
    bridge.onResolvePromise(answers[0], 'true');
    expect(warn).not.toHaveBeenCalled();
    conn.send('["EVENT",{}]');
    bridge.onRejectPromise(answers[1], 'Error sending WebSocket message');
    expect(warn).not.toHaveBeenCalled();
    expect(conn.getReadyState()).toBe(WebsocketState.CLOSED);
    expect(closed).toHaveBeenCalledTimes(1);
  });
});
