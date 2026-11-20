import { type SystemClient } from '..';
import AndroidRobosats from '../../Android';
import { v4 as uuidv4 } from 'uuid';

class SystemAndroidClient implements SystemClient {
  constructor() {
    window.AndroidRobosats = new AndroidRobosats();
  }

  public loading = false;

  // Clipboard
  public copyToClipboard: (value: string) => void = (value) => {
    window.AndroidAppRobosats?.copyToClipboard(value ?? '');
  };

  // Local storage
  public getItem: (key: string) => Promise<string | undefined> = async (key) => {
    try {
      const result = await new Promise<string>((resolve, reject) => {
        const uuid: string = uuidv4();
        window.AndroidAppRobosats?.getEncryptedStorage(uuid, key);
        window.AndroidRobosats?.storePromise(uuid, resolve, reject);
      });

      return result;
    } catch (error) {
      console.error('Error generating roboname:', error);
      return;
    }
  };

  // 0.9.10: native answers every call; without a stored promise each answer logged "No promise
  // found for UUID" (dozens per start in the phone's Recent logs). A failed write is logged.
  public setItem: (key: string, value: string) => void = (key, value) => {
    const uuid: string = uuidv4();
    window.AndroidRobosats?.storePromise(
      uuid,
      () => {},
      (error) => console.error(`Storage write failed (${key}):`, error),
    );
    window.AndroidAppRobosats?.setEncryptedStorage(uuid, key, value);
  };

  public deleteItem: (key: string) => void = (key) => {
    const uuid: string = uuidv4();
    window.AndroidRobosats?.storePromise(
      uuid,
      () => {},
      (error) => console.error(`Storage delete failed (${key}):`, error),
    );
    window.AndroidAppRobosats?.deleteEncryptedStorage(uuid, key);
  };

  public restart: () => void = () => {
    window.AndroidAppRobosats?.restart();
  };
}

export default SystemAndroidClient;
