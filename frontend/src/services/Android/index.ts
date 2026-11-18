declare global {
  interface Window {
    AndroidDataRobosats?: AndroidDataRobosats;
    AndroidAppRobosats?: AndroidAppRobosats;
    AndroidRobosats?: AndroidRobosats;
    RobosatsSettings: 'web-basic' | 'web-pro' | 'selfhosted-basic' | 'selfhosted-pro';
  }
}

interface AndroidDataRobosats {
  navigateToPage: string;
}

interface AndroidAppRobosats {
  getEncryptedStorage: (uuid: string, key: string) => void;
  setEncryptedStorage: (uuid: string, key: string, value: string) => void;
  deleteEncryptedStorage: (uuid: string, key: string) => void;
  generateRoboname: (uuid: string, initialString: string) => void;
  generateRobohash: (uuid: string, initialString: string) => void;
  copyToClipboard: (value: string) => void;
  getTorStatus: (uuid: string) => void;
  // 0.9.3: FLAG_KEEP_SCREEN_ON while an onchain signature of this app is pending
  keepScreenOn?: (on: boolean) => void;
  openWS: (uuid: string, path: string) => void;
  sendWsMessage: (uuid: string, path: string, message: string) => void;
  sendRequest: (
    uuid: string,
    action: 'GET' | 'POST' | 'DELETE' | 'PUT',
    url: string,
    headers: string,
    body: string,
  ) => void;
  sendBinary: (uuid: string, url: string, headers: string, base64Data: string) => void;
  getBinary: (uuid: string, url: string) => void;
  // rl31-beta12: plain-text request (rescue explorer); resolves {status, body (base64)}
  sendText?: (uuid: string, method: string, url: string, contentType: string, body: string) => void;
  restart: () => void;
}

class AndroidRobosats {
  private WSConnections: Record<string, (message?: string) => void> = {};
  private WSError: Record<string, () => void> = {};
  private WSClose: Record<string, () => void> = {};

  private promises: Record<
    string,
    {
      resolve: (value: string | PromiseLike<string>) => void;
      reject: (reason?: string) => void;
    }
  > = {};

  public navigateToPage: (orderPath: string) => void = (orderPath) => {
    console.log('orderPath', orderPath);
  };

  public storePromise: (
    uuid: string,
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    resolve: (value: any | PromiseLike<any>) => void,
    reject?: (reason?: string) => void,
  ) => void = (uuid, resolve, reject) => {
    this.promises[uuid] = {
      resolve,
      reject: reject || ((error) => console.error('Promise rejected:', error)),
    };
  };

  public onResolvePromise: (uuid: string, response: string) => void = (uuid, response) => {
    if (this.promises[uuid]) {
      this.promises[uuid].resolve(response);
      delete this.promises[uuid]; // Clean up after resolving
    } else {
      console.warn(`No promise found for UUID: ${uuid}`);
    }
  };

  public onRejectPromise: (uuid: string, error: string) => void = (uuid, error) => {
    if (this.promises[uuid]) {
      this.promises[uuid].reject(error);
      delete this.promises[uuid]; // Clean up after rejecting
    } else {
      console.warn(`No promise found for UUID: ${uuid}`);
    }
  };

  public registerWSConnection: (
    path: string,
    onMesage: (message?: string) => void,
    onClose: () => void,
    onError: () => void,
  ) => void = (path, onMesage, onClose, onError) => {
    this.WSConnections[path] = onMesage;
    this.WSError[path] = onError;
    this.WSClose[path] = onClose;
  };

  // Roblosats (17 nov 2026): a late close() of an old connection to the same relay path
  // (the relays are reconnected several times at start-up) removed the CURRENT connection's
  // handler: the phone kept sending chat messages but never received any. Only remove the
  // handler that belongs to the closing connection.
  public removeWSConnection: (path: string, onMesage?: (message?: string) => void) => void = (
    path,
    onMesage,
  ) => {
    if (onMesage && this.WSConnections[path] !== onMesage) return;
    delete this.WSConnections[path];
    delete this.WSError[path];
    delete this.WSClose[path];
  };

  public onWSMessage: (path: string, message: string) => void = (path, message) => {
    this.WSConnections[path]?.(message);
  };

  public onWsError: (path: string) => void = (path) => {
    this.WSError[path]?.();
  };

  public onWsClose: (path: string) => void = (path) => {
    this.WSClose[path]?.();
  };
}

export default AndroidRobosats;
