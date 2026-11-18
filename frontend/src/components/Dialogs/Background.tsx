import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { v4 as uuidv4 } from 'uuid';
import {
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  List,
  ListItem,
  ListItemText,
  Typography,
} from '@mui/material';
import { systemClient } from '../../services/System';

export const BACKGROUND_EVENT = 'roblosats-background';

interface Status {
  notifications: boolean;
  batteryExempt: boolean;
}

const status = async (): Promise<Status | null> =>
  await new Promise((resolve) => {
    const app = window.AndroidAppRobosats;
    if (!app?.backgroundStatus || !window.AndroidRobosats) {
      resolve(null);
      return;
    }
    const uuid: string = uuidv4();
    window.AndroidRobosats.storePromise(
      uuid,
      (res: string) => {
        try {
          resolve(JSON.parse(res) as Status);
        } catch {
          resolve(null);
        }
      },
      () => {
        resolve(null);
      },
    );
    app.backgroundStatus(uuid);
  });

// 0.9.6 (Android): at the first start, and from Settings, explain why Roblosats asks for the
// notification permission and offers to be left out of battery optimization. Nothing is forced.
const BackgroundDialog = (): React.JSX.Element => {
  const { t } = useTranslation();
  const [open, setOpen] = useState<boolean>(false);
  const [st, setSt] = useState<Status | null>(null);

  const refresh = async (): Promise<Status | null> => {
    const s = await status();
    setSt(s);
    return s;
  };

  useEffect(() => {
    if (!window.AndroidAppRobosats?.backgroundStatus) return;
    void systemClient.getItem('background_asked').then(async (asked) => {
      const s = await refresh();
      if (asked !== 'true' && s && (!s.notifications || !s.batteryExempt)) setOpen(true);
    });
    const onAsk = (): void => {
      void refresh().then(() => {
        setOpen(true);
      });
    };
    window.addEventListener(BACKGROUND_EVENT, onAsk);
    // back from Android's settings: show what changed
    const onFocus = (): void => {
      void refresh();
    };
    document.addEventListener('visibilitychange', onFocus);
    return () => {
      window.removeEventListener(BACKGROUND_EVENT, onAsk);
      document.removeEventListener('visibilitychange', onFocus);
    };
  }, []);

  const close = (): void => {
    systemClient.setItem('background_asked', 'true');
    setOpen(false);
  };

  return (
    <Dialog open={open} onClose={close}>
      <DialogTitle>{t('Notifications when the app is closed')}</DialogTitle>
      <DialogContent>
        <Typography variant='body2'>
          {t(
            'Roblosats tells you when an order needs you (taken, pay now, payment received, sign the release…) even with the app closed. For that it needs to show notifications, and on many phones it must not be stopped by battery saving. Both are optional.',
          )}
        </Typography>
        <List dense>
          <ListItem
            secondaryAction={
              st?.notifications ? (
                <Typography variant='body2' color='success.main'>
                  {t('Allowed')}
                </Typography>
              ) : (
                <Button
                  onClick={() => window.AndroidAppRobosats?.requestNotificationPermission?.()}
                >
                  {t('Allow')}
                </Button>
              )
            }
          >
            <ListItemText primary={t('Notifications')} secondary={t('So you see the notices.')} />
          </ListItem>
          <ListItem
            secondaryAction={
              st?.batteryExempt ? (
                <Typography variant='body2' color='success.main'>
                  {t('Allowed')}
                </Typography>
              ) : (
                <Button onClick={() => window.AndroidAppRobosats?.requestBatteryExemption?.()}>
                  {t('Allow')}
                </Button>
              )
            }
          >
            <ListItemText
              primary={t('No battery optimization')}
              secondary={t(
                'So the phone does not stop the notification service. It uses little battery.',
              )}
            />
          </ListItem>
        </List>
      </DialogContent>
      <DialogActions>
        <Button onClick={close}>
          {st?.notifications && st.batteryExempt ? t('Done') : t('Not now')}
        </Button>
      </DialogActions>
    </Dialog>
  );
};

export default BackgroundDialog;
