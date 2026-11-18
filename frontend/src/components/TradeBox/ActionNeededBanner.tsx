import React, { useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { Alert } from '@mui/material';
import { NotificationsActive } from '@mui/icons-material';
import { actionNeeded, type ActionOrder } from '../../utils/actionNeeded';
import getSettings from '../../utils/settings';

const soundPath =
  getSettings().client === 'mobile'
    ? 'file:///android_asset/static/assets/sounds'
    : '/static/assets/sounds';

let ding: HTMLAudioElement | null = null;
const playDing = (): void => {
  try {
    ding = ding ?? new Audio(`${soundPath}/locked-invoice.mp3`);
    void ding.play().catch(() => {});
  } catch {
    // no audio (tests, old webviews)
  }
};

interface Props {
  order: ActionOrder | null | undefined;
  /** Settings: sound and vibration when an order needs you (default on). */
  alerts: boolean;
}

/**
 * 0.9.4: "this order needs you" — a banner on the order page, a sound and a vibration when a new
 * step needs the robot, and the browser tab title flashing "● Action needed" while it is pending.
 */
const ActionNeededBanner = ({ order, alerts }: Props): React.JSX.Element | null => {
  const { t } = useTranslation();
  const key = actionNeeded(order);
  const last = useRef<string | null>(null);

  useEffect(() => {
    if (key && key !== last.current && alerts) {
      playDing();
      try {
        navigator.vibrate?.([200, 100, 200]);
      } catch {
        // vibration not allowed
      }
    }
    last.current = key;
  }, [key, alerts]);

  useEffect(() => {
    if (!key || typeof document === 'undefined') return;
    const original = document.title;
    const flag = `● ${t('Action needed')}`;
    let on = false;
    const timer = setInterval(() => {
      on = !on;
      document.title = on ? flag : original;
    }, 1000);
    return () => {
      clearInterval(timer);
      document.title = original;
    };
  }, [key, t]);

  if (!key) return null;
  return (
    <Alert
      severity='warning'
      icon={<NotificationsActive />}
      sx={{ width: '100%', mb: 1, fontWeight: 'bold' }}
      data-testid='action-needed'
    >
      {t(key)}
    </Alert>
  );
};

export default ActionNeededBanner;
