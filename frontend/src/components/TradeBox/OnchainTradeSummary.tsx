import React from 'react';
import { useTranslation } from 'react-i18next';
import { Box, Grid, Paper, Typography } from '@mui/material';

import { pn } from '../../utils';
import { type OnchainSummary } from '../../utils/escrow/client';
import { TxLine } from './Prompts/OnchainEscrow';

/** What an onchain trade left to us: our role, what we received, our bond back, our fee,
 * the mining fees we paid and the deposit and release transactions. */
const OnchainTradeSummary = ({ summary }: { summary: OnchainSummary }): React.JSX.Element => {
  const { t } = useTranslation();
  const row = (label: string, value: string): React.JSX.Element => (
    <Box sx={{ display: 'flex', justifyContent: 'space-between', gap: 2 }}>
      <Typography variant='body2' color='text.secondary'>
        {label}
      </Typography>
      <Typography variant='body2'>
        <b>{value}</b>
      </Typography>
    </Box>
  );
  const mining = (summary.deposit_mining_sats ?? 0) + (summary.release_mining_sats ?? 0);
  return (
    <Paper elevation={0} variant='outlined' sx={{ padding: 1.5, width: '100%' }}>
      <Grid container spacing={1} sx={{ flexDirection: 'column' }}>
        <Grid>
          <Typography variant='subtitle1' align='center'>
            <b>{t('Onchain trade summary')}</b>
          </Typography>
        </Grid>
        <Grid>
          {row(t('Your role'), summary.is_buyer ? t('Buyer') : t('Seller'))}
          {row(t('Trade amount'), `${pn(summary.trade_sats)} Sats`)}
          {row(t('You received onchain'), `${pn(summary.received_sats)} Sats`)}
          {row(
            summary.lightning_bond ? t('Your bond (Lightning)') : t('Your bond'),
            `${pn(summary.bond_sats)} Sats`,
          )}
          {row(
            summary.lightning_bond ? t('Bond unlocked in your wallet') : t('Bond back to you'),
            `${pn(summary.bond_back_sats)} Sats`,
          )}
          {row(
            summary.fee_from_bond
              ? t('Your fee (out of your bond)')
              : summary.fee_at_release
                ? t('Your fee (out of what you received)')
                : t('Your fee'),
            `${pn(summary.coordinator_fee_sats)} Sats`,
          )}
          {row(t('Mining fees you paid'), `${pn(mining)} Sats`)}
        </Grid>
        {summary.deposit_txid ? (
          <Grid>
            <TxLine label={t('Escrow deposit')} txid={summary.deposit_txid} />
          </Grid>
        ) : null}
        {summary.release_txid ? (
          <Grid>
            <TxLine label={t('Escrow release')} txid={summary.release_txid} />
          </Grid>
        ) : null}
      </Grid>
    </Paper>
  );
};

export default OnchainTradeSummary;
