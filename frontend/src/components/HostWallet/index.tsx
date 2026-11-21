// Buttons for Kilowallet's wallet inside its ROBLO tab (nothing is shown anywhere else).
import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Alert, Button, Grid, Typography } from '@mui/material';
import { AccountBalanceWallet } from '@mui/icons-material';
import {
  hostWalletAvailable,
  walletAddress,
  walletInvoice,
  walletPay,
  walletPayInvoice,
} from '../../utils/hostWallet';

const cancelled = (e: unknown): boolean => /cancel/i.test(String(e));

/** "PAY WITH KILOWALLET" under an on-chain payment box (bond, deposit, fee). */
export const HostPayOnchain = ({
  address,
  sats,
  orderRef,
  feeHint,
}: {
  address: string;
  sats: number;
  orderRef: string;
  feeHint?: number;
}): React.JSX.Element | null => {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const [txid, setTxid] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  if (!hostWalletAvailable() || !address || !(sats > 0)) return null;
  return (
    <Grid sx={{ width: '100%', textAlign: 'center' }}>
      {txid ? (
        <Alert severity='success'>
          {t('Sent from Kilowallet')}: {txid.slice(0, 10)}…{txid.slice(-6)}
        </Alert>
      ) : (
        <Button
          variant='contained'
          color='secondary'
          disabled={busy}
          startIcon={<AccountBalanceWallet />}
          onClick={() => {
            setBusy(true);
            setError(null);
            walletPay(address, sats, orderRef, feeHint ?? 0)
              .then(setTxid)
              .catch((e) => {
                if (!cancelled(e)) setError(String(e));
              })
              .finally(() => {
                setBusy(false);
              });
          }}
        >
          {t('Pay with Kilowallet')}
        </Button>
      )}
      {error ? <Alert severity='error'>{error}</Alert> : null}
    </Grid>
  );
};

/** "PAY WITH KILOWALLET (LIGHTNING)" under a hold invoice (bond or trade escrow). */
export const HostPayInvoice = ({
  invoice,
  orderRef,
}: {
  invoice: string;
  orderRef: string;
}): React.JSX.Element | null => {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!hostWalletAvailable() || !invoice) return null;
  return (
    <Grid sx={{ width: '100%', textAlign: 'center' }}>
      {done ? (
        <Alert severity='success'>
          {t('Locked from Kilowallet. It stays locked until the trade ends.')}
        </Alert>
      ) : (
        <Button
          variant='contained'
          color='secondary'
          disabled={busy}
          startIcon={<AccountBalanceWallet />}
          onClick={() => {
            setBusy(true);
            setError(null);
            walletPayInvoice(invoice, orderRef)
              .then(() => {
                setDone(true);
              })
              .catch((e) => {
                if (!cancelled(e)) setError(String(e));
              })
              .finally(() => {
                setBusy(false);
              });
          }}
        >
          {t('Pay with Kilowallet (Lightning)')}
        </Button>
      )}
      {error ? <Alert severity='error'>{error}</Alert> : null}
    </Grid>
  );
};

/** "USE A KILOWALLET ADDRESS": the user picks one in Kilowallet's RECEIVE menu. */
export const HostAddressButton = ({
  purpose,
  orderRef,
  onAddress,
}: {
  purpose: string;
  orderRef: string;
  onAddress: (address: string) => void;
}): React.JSX.Element | null => {
  const { t } = useTranslation();
  const [error, setError] = useState<string | null>(null);
  if (!hostWalletAvailable()) return null;
  return (
    <Grid sx={{ width: '100%', textAlign: 'center' }}>
      <Button
        size='small'
        variant='outlined'
        startIcon={<AccountBalanceWallet />}
        onClick={() => {
          setError(null);
          walletAddress(purpose, orderRef)
            .then(onAddress)
            .catch((e) => {
              if (!cancelled(e)) setError(String(e));
            });
        }}
      >
        {t('Use a Kilowallet address')}
      </Button>
      {error ? (
        <Typography variant='caption' color='error' component='div'>
          {error}
        </Typography>
      ) : null}
    </Grid>
  );
};

/** "INVOICE FROM MY KILOWALLET NODE" for the Lightning payout. */
export const HostInvoiceButton = ({
  sats,
  memo,
  onInvoice,
}: {
  sats: number;
  memo: string;
  onInvoice: (invoice: string) => void;
}): React.JSX.Element | null => {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!hostWalletAvailable() || !(sats > 0)) return null;
  return (
    <Grid sx={{ width: '100%', textAlign: 'center', marginTop: 1 }}>
      <Button
        size='small'
        variant='outlined'
        disabled={busy}
        startIcon={<AccountBalanceWallet />}
        onClick={() => {
          setBusy(true);
          setError(null);
          walletInvoice(sats, memo)
            .then(onInvoice)
            .catch((e) => {
              if (!cancelled(e)) setError(String(e));
            })
            .finally(() => {
              setBusy(false);
            });
        }}
      >
        {t('Invoice from my Kilowallet node')}
      </Button>
      {error ? (
        <Typography variant='caption' color='error' component='div'>
          {error}
        </Typography>
      ) : null}
    </Grid>
  );
};
