import React, { useContext, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import {
  List,
  ListItem,
  ListItemText,
  ListItemIcon,
  Divider,
  Grid,
  Typography,
  Tooltip,
  Alert,
} from '@mui/material';
import { LoadingButton } from '@mui/lab';

import currencies from '../../../utils/currencies';

import { isOnchainEscrow } from '../onchainEscrowSteps';
import { pendingTakeNotice, pendingTakeTxs } from '../pendingTakes';
import { TxLine } from './OnchainEscrow';
import { type Order } from '../../../models';
import {
  FederationContext,
  type UseFederationStoreType,
} from '../../../contexts/FederationContext';
import { AppContext, type UseAppStoreType } from '../../../contexts/AppContext';
import { PauseCircle, Storefront, Percent } from '@mui/icons-material';

interface PublicWaitPrompProps {
  order: Order;
  pauseLoading: boolean;
  onClickPauseOrder: () => void;
}

export const PublicWaitPrompt = ({
  order,
  pauseLoading,
  onClickPauseOrder,
}: PublicWaitPrompProps): React.JSX.Element => {
  const { t } = useTranslation();
  const { federation } = useContext<UseFederationStoreType>(FederationContext);
  const { federationUpdatedAt } = useContext<UseAppStoreType>(AppContext);

  const currencyCode = currencies[order.currency.toString()];

  const depositHoursMinutes = function (): {
    deposit_timer_hours: number;
    deposit_timer_minutes: number;
  } {
    const hours = Math.floor(order.escrow_duration / 3600);
    const minutes = Math.floor((order.escrow_duration - hours * 3600) / 60);
    const dict = { deposit_timer_hours: hours, deposit_timer_minutes: minutes };
    return dict;
  };

  const numSimilarOrders = useMemo((): number => {
    const orders = Object.values(federation.book) ?? [];
    const similarOrders = orders.filter(
      (bookOrder) =>
        // Public -> 1
        bookOrder?.currency == order.currency && order.status == 1,
    );
    return similarOrders.length;
  }, [federationUpdatedAt]);

  const premiumPercentile = useMemo((): number => {
    const orders = Object.values(federation.book) ?? [];
    const querySet = orders.filter(
      (bookOrder) =>
        bookOrder?.currency == order.currency && order.status == 1 && bookOrder.type == order.type,
    );

    if (querySet.length <= 1) {
      return 0.5;
    }

    const premiums = querySet.map((similarOrder) => parseFloat(similarOrder?.premium ?? '0'));
    const sumPremium = premiums.reduce((sum, rate) => sum + (rate < order.premium ? 1 : 0), 0);
    const percentile = (sumPremium / premiums.length) * 100;

    return Math.floor(parseFloat(percentile.toFixed(2)));
  }, [federationUpdatedAt]);

  const takeNotice = pendingTakeNotice(order);
  const takeTxs = pendingTakeTxs(order);

  return (
    <List dense={true}>
      {takeNotice ? (
        <ListItem>
          <Alert severity='warning' icon={<span>⛓️</span>} sx={{ width: '100%' }}>
            {t(takeNotice.key, { n: takeNotice.count })}
            {takeTxs.map((tx) => (
              <div key={tx.txid} style={{ marginTop: '0.6em' }}>
                <TxLine label={t(tx.labelKey)} txid={tx.txid} />
              </div>
            ))}
          </Alert>
        </ListItem>
      ) : null}
      <Divider />

      <ListItem>
        <Typography variant='body2' align='left'>
          {t(
            'Be patient while robots check the book. This box will ring 🔊 once a robot takes your order, then you will have {{deposit_timer_hours}}h {{deposit_timer_minutes}}m to reply. If you do not reply, you risk losing your bond.',
            depositHoursMinutes(),
          )}
        </Typography>
      </ListItem>

      <ListItem>
        <Typography variant='body2' align='left'>
          {isOnchainEscrow(order)
            ? t(
                'If the order expires untaken, your onchain bond is free: this screen will ask you for an address to send it back to.',
              )
            : t('If the order expires untaken, your bond will return to you (no action needed).')}
        </Typography>
      </ListItem>

      <Divider />

      <Grid container>
        <Grid size={10}>
          <ListItem>
            <ListItemIcon sx={{ minWidth: 56 }}>
              <Storefront />
            </ListItemIcon>
            <ListItemText
              primary={numSimilarOrders}
              secondary={t('Public orders for {{currencyCode}}', {
                currencyCode,
              })}
            />
          </ListItem>
        </Grid>

        <Grid size={2}>
          <div style={{ position: 'relative', top: '0.5em', right: '1em' }}>
            <Tooltip
              placement='top'
              enterTouchDelay={500}
              enterDelay={700}
              enterNextDelay={2000}
              title={t('Pause the public order')}
            >
              <div>
                <LoadingButton loading={pauseLoading} color='primary' onClick={onClickPauseOrder}>
                  <PauseCircle sx={{ width: '1.6em', height: '1.6em' }} />
                </LoadingButton>
              </div>
            </Tooltip>
          </div>
        </Grid>
      </Grid>

      <Divider />
      <ListItem>
        <ListItemIcon sx={{ minWidth: 56 }}>
          <Percent />
        </ListItemIcon>
        <ListItemText
          primary={`${t('Premium rank')} ${premiumPercentile}%`}
          secondary={t('Among public {{currencyCode}} orders (higher is cheaper)', {
            currencyCode,
          })}
        />
      </ListItem>
    </List>
  );
};

export default PublicWaitPrompt;
