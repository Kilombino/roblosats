import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Grid, Typography, ToggleButtonGroup, ToggleButton } from '@mui/material';

import currencies from '../../../utils/currencies';
import { exchangeUnit, toDisplayTotal } from '../../../utils/satOrders';

import { type Order, type Settings } from '../../../models';
import { pn } from '../../../utils';
import { Bolt, Link } from '@mui/icons-material';
import { defaultPayoutTab, onchainPayoutAllowed } from '../payoutOptions';
import {
  LightningPayoutForm,
  type LightningForm,
  OnchainPayoutForm,
  type OnchainForm,
} from '../Forms';

interface PayoutPrompProps {
  order: Order;
  onClickSubmitInvoice: (invoice: string) => void;
  lightning: LightningForm;
  loadingLightning: boolean;
  setLightning: (state: LightningForm) => void;
  onClickSubmitAddress: () => void;
  onchain: OnchainForm;
  setOnchain: (state: OnchainForm) => void;
  loadingOnchain: boolean;
  settings: Settings;
}

export const PayoutPrompt = ({
  order,
  onClickSubmitInvoice,
  loadingLightning,
  lightning,
  setLightning,
  onClickSubmitAddress,
  loadingOnchain,
  onchain,
  setOnchain,
  settings,
}: PayoutPrompProps): React.JSX.Element => {
  const { t } = useTranslation();
  const currencyCode: string = currencies[`${order.currency}`];

  // Roblosats: an onchain order taken with Lightning (hybrid B) pays the buyer onchain by default.
  const [tab, setTab] = useState<'lightning' | 'onchain'>(defaultPayoutTab(order));
  const onchainAllowed = onchainPayoutAllowed(order);

  return (
    <Grid
      container
      spacing={1}
      sx={{
        alignItems: 'center',
        justifyContent: 'flex-start',
        flexDirection: 'column',
        padding: 1,
      }}
    >
      <Grid>
        <Typography variant='body2'>
          {t(
            'Before letting you send {{amountFiat}} {{currencyCode}}, we want to make sure you are able to receive the BTC.',
            {
              // Roblosats: currency 1000 is Spamcoin, read in Spamsats
              amountFiat: pn(
                order.currency === 1000
                  ? Math.round(toDisplayTotal(Number(order.amount ?? 0), 1000))
                  : parseFloat(Number(order.amount ?? 0).toFixed(4)),
              ),
              currencyCode: order.currency === 1000 ? exchangeUnit(1000) : currencyCode,
            },
          )}
        </Typography>
      </Grid>

      <Grid>
        <ToggleButtonGroup
          size='medium'
          value={tab}
          exclusive
          onChange={(mouseEvent, value) => {
            setTab(value == null ? tab : value);
          }}
        >
          <ToggleButton value='lightning'>
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                flexWrap: 'wrap',
              }}
            >
              <Bolt /> {t('Lightning')}
            </div>
          </ToggleButton>
          <ToggleButton value='onchain' disabled={!onchainAllowed}>
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                flexWrap: 'wrap',
              }}
            >
              <Link /> {t('Onchain')}
            </div>
          </ToggleButton>
        </ToggleButtonGroup>
      </Grid>

      {onchainAllowed && tab === 'onchain' ? (
        <Grid>
          <Typography variant='caption' color='text.secondary'>
            {t('Lightning: no swap fee')}
          </Typography>
        </Grid>
      ) : null}

      <Grid style={{ display: tab === 'lightning' ? '' : 'none' }}>
        <LightningPayoutForm
          order={order}
          settings={settings}
          loading={loadingLightning}
          lightning={lightning}
          setLightning={setLightning}
          onClickSubmit={onClickSubmitInvoice}
        />
      </Grid>

      {/* ONCHAIN PAYOUT TAB */}
      <Grid style={{ display: tab === 'onchain' ? '' : 'none' }}>
        <OnchainPayoutForm
          order={order}
          loading={loadingOnchain}
          onchain={onchain}
          setOnchain={setOnchain}
          onClickSubmit={onClickSubmitAddress}
        />
      </Grid>
    </Grid>
  );
};

export default PayoutPrompt;
