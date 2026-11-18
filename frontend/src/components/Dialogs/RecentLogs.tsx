import React, { useContext, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { v4 as uuidv4 } from 'uuid';
import {
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Typography,
} from '@mui/material';
import { type UseAppStoreType, AppContext } from '../../contexts/AppContext';
import { systemClient } from '../../services/System';
import { buildReport, type NativeLogs } from '../../utils/logs';

interface Props {
  open: boolean;
  onClose: () => void;
}

const nativeLogs = async (): Promise<NativeLogs | null> =>
  await new Promise((resolve) => {
    const app = window.AndroidAppRobosats;
    if (!app?.getRecentLogs || !window.AndroidRobosats) {
      resolve(null);
      return;
    }
    const uuid: string = uuidv4();
    window.AndroidRobosats.storePromise(
      uuid,
      (res: string) => {
        try {
          resolve(JSON.parse(res) as NativeLogs);
        } catch {
          resolve(null);
        }
      },
      () => {
        resolve(null);
      },
    );
    app.getRecentLogs(uuid);
  });

// 0.9.6: Settings → "Recent logs": the app's recent log and its last crash, redacted (no robot
// tokens, keys, PGP blocks, invoices or addresses), to copy or share when something breaks.
const RecentLogsDialog = ({ open, onClose }: Props): React.JSX.Element => {
  const { t } = useTranslation();
  const { clientVersion } = useContext<UseAppStoreType>(AppContext);
  const [report, setReport] = useState<string>('');
  const [copied, setCopied] = useState<boolean>(false);

  useEffect(() => {
    if (!open) return;
    setCopied(false);
    setReport(t('Loading…'));
    void nativeLogs().then((native) => {
      setReport(buildReport(clientVersion.long, native));
    });
  }, [open]);

  const share = (): void => {
    if (window.AndroidAppRobosats?.shareText) window.AndroidAppRobosats.shareText(report);
    else if ('share' in navigator) void navigator.share({ title: 'Roblosats logs', text: report });
  };

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth='md'>
      <DialogTitle>{t('Recent logs')}</DialogTitle>
      <DialogContent>
        <Typography variant='body2' sx={{ mb: 1 }}>
          {t(
            'What the app logged lately and its last crash, to send when something breaks. Robot tokens, keys, PGP blocks, invoices and addresses are removed. Read it before sending.',
          )}
        </Typography>
        <Typography
          component='pre'
          variant='caption'
          data-testid='recent-logs'
          sx={{
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-all',
            fontFamily: 'monospace',
            maxHeight: '55vh',
            overflow: 'auto',
            p: 1,
            bgcolor: 'action.hover',
          }}
        >
          {report}
        </Typography>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>{t('Close')}</Button>
        <Button
          onClick={() => {
            systemClient.copyToClipboard(report);
            setCopied(true);
          }}
        >
          {copied ? t('Copied!') : t('Copy')}
        </Button>
        {window.AndroidAppRobosats?.shareText !== undefined || 'share' in navigator ? (
          <Button variant='contained' onClick={share}>
            {t('Share')}
          </Button>
        ) : null}
      </DialogActions>
    </Dialog>
  );
};

export default RecentLogsDialog;
