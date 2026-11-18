import React, { useContext, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Alert,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  LinearProgress,
  Typography,
} from '@mui/material';
import { type UseAppStoreType, AppContext } from '../../contexts/AppContext';
import { systemClient } from '../../services/System';
import { RELEASES_PAGE, checkUpdate, readableNotes, type UpdateInfo } from '../../utils/appUpdate';

/** Settings → "Check for updates now" opens this dialog through this event. */
export const CHECK_UPDATE_EVENT = 'roblosats-check-update';

interface Progress {
  state: string;
  percent?: number;
  message?: string;
}

// 0.9.6: the update notice, as in Kilowallet: READ (the release notes), CANCEL, UPDATE. On
// Android UPDATE downloads the APK for this phone, checks its SHA-256 against the release notes
// and its signing certificate, and hands it to Android's installer; when the notes do not
// publish the hash, or on the web, it opens the release page instead.
const AppUpdateDialog = (): React.JSX.Element => {
  const { t } = useTranslation();
  const { client } = useContext<UseAppStoreType>(AppContext);
  const [info, setInfo] = useState<UpdateInfo | null>(null);
  const [open, setOpen] = useState<boolean>(false);
  const [reading, setReading] = useState<boolean>(false);
  const [manual, setManual] = useState<boolean>(false);
  const [progress, setProgress] = useState<Progress | null>(null);

  const run = (byHand: boolean): void => {
    setManual(byHand);
    void checkUpdate(client).then(async (res) => {
      setInfo(res);
      if (res.status !== 'newer') {
        if (byHand) setOpen(true);
        return;
      }
      const dismissed = await systemClient.getItem('update_dismissed');
      if (byHand || dismissed !== res.version) setOpen(true);
    });
  };

  useEffect(() => {
    void systemClient.getItem('settings_check_updates').then((v) => {
      if (v !== 'false') run(false);
    });
    const onAsk = (): void => {
      run(true);
    };
    window.addEventListener(CHECK_UPDATE_EVENT, onAsk);
    window.AndroidRobosats?.setUpdateListener((s: string) => {
      try {
        setProgress(JSON.parse(s) as Progress);
      } catch {
        // ignore a malformed state
      }
    });
    return () => {
      window.removeEventListener(CHECK_UPDATE_EVENT, onAsk);
      window.AndroidRobosats?.setUpdateListener(null);
    };
  }, []);

  const close = (): void => {
    if (info?.status === 'newer' && info.version && !manual)
      systemClient.setItem('update_dismissed', info.version);
    setOpen(false);
    setReading(false);
  };

  const openPage = (): void => {
    const url = info?.url ?? RELEASES_PAGE;
    if (window.AndroidAppRobosats?.openReleasePage) window.AndroidAppRobosats.openReleasePage(url);
    else window.open(url, '_blank', 'noopener,noreferrer');
  };

  const update = (): void => {
    if (client === 'mobile' && info?.canInstall && window.AndroidAppRobosats?.startUpdate) {
      setProgress({ state: 'downloading', percent: 0 });
      window.AndroidAppRobosats.startUpdate();
    } else {
      openPage();
    }
  };

  const busy =
    progress !== null && ['downloading', 'verifying', 'installing'].includes(progress.state);
  const installable = client === 'mobile' && info?.canInstall === true;

  return (
    <Dialog open={open} onClose={busy ? undefined : close} fullWidth maxWidth='sm'>
      <DialogTitle>
        {info?.status === 'newer'
          ? t('Roblosats {{version}} is available', { version: info.version })
          : info?.status === 'uptodate'
            ? t('Roblosats is up to date')
            : t('Could not check for updates')}
      </DialogTitle>
      <DialogContent>
        {info?.status === 'newer' ? (
          <Typography variant='body2' sx={{ mb: 1 }}>
            {t('You have {{current}}.', { current: info.current ?? '?' })}{' '}
            {installable
              ? t('The app checks the download against the release notes before installing it.')
              : client === 'mobile'
                ? t('This release can be installed from its page.')
                : t('Refresh this page or update the app on your node to get it.')}
          </Typography>
        ) : null}
        {info?.status === 'uptodate' ? (
          <Typography variant='body2'>
            {t('You have the latest version ({{current}}).', { current: info.current ?? '?' })}
          </Typography>
        ) : null}
        {info?.status === 'unreachable' ? (
          <Typography variant='body2'>{t('GitHub did not answer. Try again later.')}</Typography>
        ) : null}
        {reading && info?.notes ? (
          <Typography
            variant='body2'
            component='pre'
            sx={{
              whiteSpace: 'pre-wrap',
              fontFamily: 'inherit',
              mt: 1,
              maxHeight: '50vh',
              overflow: 'auto',
            }}
            data-testid='update-notes'
          >
            {readableNotes(info.notes)}
          </Typography>
        ) : null}
        {progress?.state === 'downloading' ? (
          <>
            <Typography variant='body2' sx={{ mt: 1 }}>
              {t('Downloading… {{percent}}%', { percent: progress.percent ?? 0 })}
            </Typography>
            <LinearProgress variant='determinate' value={progress.percent ?? 0} />
          </>
        ) : null}
        {progress?.state === 'verifying' ? (
          <Typography variant='body2' sx={{ mt: 1 }}>
            {t('Checking the download (SHA-256 and signature)…')}
          </Typography>
        ) : null}
        {progress?.state === 'installing' ? (
          <Typography variant='body2' sx={{ mt: 1 }}>
            {t('Android is installing the update…')}
          </Typography>
        ) : null}
        {progress?.state === 'needs-permission' ? (
          <Alert severity='info' sx={{ mt: 1 }}>
            {t(
              'Android needs you to allow installs from Roblosats once. Allow it, come back and tap Continue.',
            )}
          </Alert>
        ) : null}
        {progress?.state === 'failed' ? (
          <Alert severity='error' sx={{ mt: 1 }}>
            {progress.message}
          </Alert>
        ) : null}
      </DialogContent>
      <DialogActions>
        {info?.status === 'newer' && info.notes ? (
          <Button
            onClick={() => {
              setReading(!reading);
            }}
          >
            {t('Read')}
          </Button>
        ) : null}
        <Button onClick={close} disabled={busy}>
          {info?.status === 'newer' ? t('Cancel') : t('Close')}
        </Button>
        {progress?.state === 'needs-permission' ? (
          <>
            <Button onClick={() => window.AndroidAppRobosats?.allowInstalls?.()}>
              {t('Allow')}
            </Button>
            <Button variant='contained' onClick={() => window.AndroidAppRobosats?.resumeUpdate?.()}>
              {t('Continue')}
            </Button>
          </>
        ) : info?.status === 'newer' ? (
          <Button variant='contained' onClick={update} disabled={busy}>
            {installable ? t('Update') : t('Open')}
          </Button>
        ) : null}
      </DialogActions>
    </Dialog>
  );
};

export default AppUpdateDialog;
