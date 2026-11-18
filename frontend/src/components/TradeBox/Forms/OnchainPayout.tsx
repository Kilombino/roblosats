import React, { useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { Grid, Typography, TextField, List, Divider, ListItemText, ListItem } from '@mui/material';
import { type Order } from '../../../models';
import { LoadingButton } from '@mui/lab';
import { pn } from '../../../utils';
import { onchainPayout } from '../../../utils/hybridSwap';

export interface OnchainForm {
  address: string;
  miningFee: number;
  badAddress: string;
}

export const defaultOnchain: OnchainForm = {
  address: '',
  miningFee: 10,
  badAddress: '',
};

interface OnchainPayoutFormProps {
  order: Order;
  loading: boolean;
  onchain: OnchainForm;
  setOnchain: (state: OnchainForm) => void;
  onClickSubmit: () => void;
}

export const OnchainPayoutForm = ({
  order,
  loading,
  onClickSubmit,
  onchain,
  setOnchain,
}: OnchainPayoutFormProps): React.JSX.Element => {
  const { t } = useTranslation();

  const minMiningFee = 2;
  const maxMiningFee = 500;
  const invalidFee = onchain.miningFee < minMiningFee || onchain.miningFee > maxMiningFee;
  const costPerVByte = 280;

  const handleMiningFeeChange = (e: React.ChangeEvent<HTMLInputElement>): void => {
    const miningFee = Number(e.target.value);
    setOnchain({ ...onchain, miningFee });
  };

  useEffect(() => {
    setOnchain({ ...onchain, miningFee: order.suggested_mining_fee_rate });
  }, []);

  // rl31-beta21, hybrid B: the seller already paid this swap (up to this much)
  const paidBySeller = Number(
    (order as unknown as { swap_paid_by_seller_sats?: number }).swap_paid_by_seller_sats ?? 0,
  );
  const miningFeeSats = Math.floor(Math.max(minMiningFee, onchain.miningFee) * costPerVByte);
  const payout = onchainPayout({
    invoiceAmount: order.invoice_amount,
    swapFeeRate: Number(order.swap_fee_rate),
    miningFeeSats,
    paidBySeller,
  });
  const bySeller = paidBySeller > 0;

  return (
    <Grid
      container

      spacing={0.5}
      sx={{ alignItems: 'center', justifyContent: 'flex-start', flexDirection: 'column' }}
    >
      <List dense={true}>
        <ListItem>
          <Typography variant='body2'>
            {t('RoboSats coordinator will do a swap and send the Sats to your onchain address.')}
          </Typography>
        </ListItem>

        <ListItem>
          <Typography variant='body2'>
            <b>
              {bySeller
                ? payout.covered < payout.cost
                  ? t(
                      'No swap fee for you: the seller pays the swap to onchain. Only the mining fee above the one the seller paid ({{sats}} Sats) comes out of what you receive.',
                      { sats: pn(payout.cost - payout.covered) },
                    )
                  : t('No swap fee for you: the seller pays the swap to onchain.')
                : t(
                    'You (the buyer) pay the swap fee and the mining fee: both are deducted from what you receive.',
                  )}
            </b>
          </Typography>
        </ListItem>

        <Divider />

        <ListItem>
          <ListItemText
            primary={`${pn(Math.floor((order.invoice_amount * order.swap_fee_rate) / 100))} Sats (${Number(
              Number(order.swap_fee_rate).toFixed(2),
            )}%)`}
            secondary={bySeller ? t('Swap fee (paid by the seller)') : t('Swap fee')}
          />
        </ListItem>

        <Divider />

        <ListItem>
          <ListItemText
            primary={`${pn(
              Math.floor(Math.max(minMiningFee, onchain.miningFee) * costPerVByte),
            )} Sats (${Math.max(minMiningFee, onchain.miningFee)} Sats/vByte)`}
            secondary={bySeller ? t('Mining fee (paid by the seller up to the suggested one)') : t('Mining fee')}
          />
        </ListItem>

        <Divider />

        <ListItem>
          <ListItemText
            primary={
              <b>
                {pn(payout.received) + ' Sats'}
              </b>
            }
            secondary={t('Final amount you will receive')}
          />
        </ListItem>
      </List>

      <Grid>
        <Grid
          container
          direction='row'
          spacing={0}
          sx={{ alignItems: 'flex-start', justifyContent: 'center' }}
        >
          <Grid size={7}>
            <TextField
              error={onchain.badAddress !== ''}
              helperText={onchain.badAddress !== '' ? t(onchain.badAddress) : ''}
              label={t('Bitcoin Address')}
              required
              value={onchain.address}
              fullWidth
              maxRows={1}
              onChange={(e) => {
                setOnchain({ ...onchain, address: e.target.value });
              }}
            />
          </Grid>
          <Grid size={5}>
            <TextField
              error={invalidFee}
              helperText={invalidFee ? t('Invalid') : ''}
              label={t('Mining Fee')}
              required
              fullWidth={true}
              value={onchain.miningFee}
              type='number'
              slotProps={{
                htmlInput: {
                  max: maxMiningFee,
                  min: minMiningFee,
                  style: { textAlign: 'center' },
                },
              }}
              onChange={(e) => handleMiningFeeChange(e as React.ChangeEvent<HTMLInputElement>)}
            />
          </Grid>
        </Grid>
      </Grid>

      <Grid style={{ marginTop: 16 }}>
        <LoadingButton
          loading={loading}
          onClick={onClickSubmit}
          disabled={invalidFee}
          variant='outlined'
          color='primary'
          size='large'
        >
          {t('Submit')}
        </LoadingButton>
      </Grid>
    </Grid>
  );
};

export default OnchainPayoutForm;
