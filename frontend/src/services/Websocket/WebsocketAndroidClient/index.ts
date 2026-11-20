import { WebsocketState, type WebsocketClient, type WebsocketConnection } from '..';
import WebsocketWebClient from '../WebsocketWebClient';
import { v4 as uuidv4 } from 'uuid';

class WebsocketConnectionAndroid implements WebsocketConnection {
  constructor(path: string) {
    this.path = path;
    this.handler = (message?: string) => {
      this.wsMessagePromises.forEach((f) => f({ data: message }));
    };
    // 0.9.10 (#29): the native side now says when THIS socket closed or failed (a Tor circuit
    // dropping used to be silent: the connection looked OPEN for ever and nothing arrived).
    window.AndroidRobosats?.registerWSConnection(
      path,
      this.handler,
      () => {
        this.markClosed();
      },
      () => {
        this.wsErrorPromises.forEach((f) => f({ path }));
        this.markClosed();
      },
    );
  }

  private state: number = WebsocketState.OPEN;
  private readonly wsErrorPromises: Array<(error: object) => void> = [];

  private markClosed(): void {
    if (this.state === WebsocketState.CLOSED) return;
    this.state = WebsocketState.CLOSED;
    this.wsClosePromises.forEach((f) => f());
  }

  private readonly path: string;
  private readonly handler: (message?: string) => void;

  private readonly wsMessagePromises: Array<(message: object) => void> = [];
  private readonly wsClosePromises: Array<() => void> = [];

  public send: (message: string) => void = (message: string) => {
    if (this.state !== WebsocketState.OPEN) return;
    const uuid: string = uuidv4();
    // 0.9.10: the native side answers every send; without a stored promise each answer logged
    // "No promise found for UUID". A refused send means native has no socket for this URL any
    // more: the connection is closed, so the pool opens it again.
    window.AndroidRobosats?.storePromise(
      uuid,
      () => {},
      () => {
        this.markClosed();
      },
    );
    window.AndroidAppRobosats?.sendWsMessage(uuid, this.path, message);
  };

  public close: () => void = () => {
    this.state = WebsocketState.CLOSED;
    window.AndroidRobosats?.removeWSConnection(this.path, this.handler);
  };

  public onMessage: (event: (message: object) => void) => void = (event) => {
    this.wsMessagePromises.push(event);
  };

  public onClose: (event: () => void) => void = (event) => {
    this.wsClosePromises.push(event);
  };

  public onError: (event: (error: object) => void) => void = (event) => {
    this.wsErrorPromises.push(event);
  };

  public getReadyState: () => number = () => this.state;
}

class WebsocketAndroidClient implements WebsocketClient {
  private readonly webClient: WebsocketWebClient = new WebsocketWebClient();

  public open: (path: string) => Promise<WebsocketConnection> = async (path) => {
    return new Promise<WebsocketConnectionAndroid>((resolve, reject) => {
      const uuid: string = uuidv4();
      window.AndroidAppRobosats?.openWS(uuid, path);
      window.AndroidRobosats?.storePromise(
        uuid,
        () => {
          resolve(new WebsocketConnectionAndroid(path));
        },
        () => {
          reject(new Error('Failed to establish a websocket connection.'));
        },
      );
    });
  };
}

export default WebsocketAndroidClient;
