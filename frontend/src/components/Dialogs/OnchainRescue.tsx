// "Recover without the coordinator" (Roblosats rl31-beta12): takes this robot's onchain sats
// back with only the data kept on this device, an explorer and the robot token.
import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  TextField,
  Typography,
} from '@mui/material';

import { systemClient } from '../../services/System';
import { pn } from '../../utils';
import { DEFAULT_EXPLORER, EXPLORER_SETTING, Explorer } from '../../utils/escrow/explorer';
import {
  RescueError,
  blocksLeft,
  buildBondRefund,
  buildEscrowRescue,
  buildFundingSweep,
  fundingAddressOf,
  keysOf,
  loadRescues,
  storedFairRescue,
  type BuiltRescue,
  type RescueRecord,
  type Utxo,
} from '../../utils/escrow/rescue';
import { addressToScript } from '../../utils/escrow/taproot';

interface Props {
  open: boolean;
  onClose: () => void;
  token: string | undefined;
}

type Kind = 'funding' | 'bond' | 'escrow' | 'fair';

interface Item {
  rec: RescueRecord;
  kind: Kind;
  sats: number;
  utxos: Utxo[];
  key?: string; // two items of one kind and order (bond coins ready now and later)
  wait: number | null; // blocks still to wait (0 = now), null = not confirmed yet
  note?: string;
}

const MIN_PER_BLOCK = 9;

const OnchainRescueDialog = ({ open, onClose, token }: Props): React.JSX.Element => {
  const { t } = useTranslation();
  const [recs, setRecs] = useState<RescueRecord[]>([]);
  const [explorerUrl, setExplorerUrl] = useState<string>(DEFAULT_EXPLORER);
  const [dest, setDest] = useState<string>('');
  const [items, setItems] = useState<Item[] | null>(null);
  const [notes, setNotes] = useState<string[]>([]);
  const [tip, setTip] = useState<number | null>(null);
  const [busy, setBusy] = useState<boolean>(false);
  const [error, setError] = useState<string>('');
  const [done, setDone] = useState<Record<string, BuiltRescue>>({});

  useEffect(() => {
    if (!open || !token) return;
    setItems(null);
    setError('');
    void loadRescues(systemClient, token).then(setRecs);
    void systemClient.getItem(EXPLORER_SETTING).then((v) => {
      if (v) setExplorerUrl(v);
    });
  }, [open, token]);

  const explorer = (): Explorer => new Explorer(explorerUrl.trim() || DEFAULT_EXPLORER);

  const check = async (): Promise<void> => {
    if (!token) return;
    setBusy(true);
    setError('');
    try {
      systemClient.setItem(EXPLORER_SETTING, explorerUrl.trim() || DEFAULT_EXPLORER);
      const ex = explorer();
      const height = await ex.tipHeight();
      setTip(height);
      const found: Item[] = [];
      const info: string[] = [];
      for (const rec of recs) {
        // One order the explorer cannot answer for (a transaction it does not know, a network
        // hiccup) must not hide the others (rl31-beta16 QA: one stale order emptied the list).
        try {
          const keys = keysOf(token, rec);
          // 1) the funding address: ours alone, no timelock
          const faddr = fundingAddressOf(rec, keys);
          if (rec.fundingAddress === faddr || rec.escrow) {
            const us = await ex.utxos(faddr);
            if (us.length)
              found.push({
                rec,
                kind: 'funding',
                sats: us.reduce((a, u) => a + u.value, 0),
                utxos: us,
                wait: 0,
              });
          }
          // 2) everything at our bond address (rl31-beta18): the bond while it is still
          //    there, a top-up, and payments that were never the bond (short, extra or late).
          //    Each coin has its own timelock: the ready ones go together, the rest wait.
          if (rec.bond?.address) {
            const us = (await ex.utxos(rec.bond.address)).filter((u) => u.height !== null);
            const csv = rec.bond.refundCsv;
            const ready = us.filter((u) => blocksLeft(u.height, csv, height) === 0);
            const later = us.filter((u) => (blocksLeft(u.height, csv, height) ?? 1) > 0);
            if (ready.length)
              found.push({
                rec,
                kind: 'bond',
                sats: ready.reduce((a, u) => a + u.value, 0),
                utxos: ready,
                wait: 0,
              });
            if (later.length)
              found.push({
                rec,
                kind: 'bond',
                key: 'later',
                sats: later.reduce((a, u) => a + u.value, 0),
                utxos: later,
                wait: Math.min(...later.map((u) => blocksLeft(u.height, csv, height) ?? 0)),
              });
          }
          // 3) the escrow: plan 2 (rl31-beta16), the rescue both pre-signed gives each one its
          //    own after rescue_csv blocks; plan 1, the seller alone takes it all
          if (rec.escrow?.depositTxid) {
            const spent = await ex.spentBy(rec.escrow.depositTxid, 0);
            if (!spent && (rec.escrow.rescuePlan ?? 1) >= 2) {
              if (rec.escrow.rescueTx) {
                const conf = await ex.confirmedAt(rec.escrow.depositTxid);
                found.push({
                  rec,
                  kind: 'fair',
                  sats: rec.escrow.rescueToMe ?? 0,
                  utxos: [],
                  wait: blocksLeft(conf, rec.escrow.rescueCsv, height),
                });
              } else {
                info.push(
                  t(
                    'Order #{{id}}: the signed rescue is not saved on this device. Open the order on the device you traded with, or while the coordinator is up.',
                    { id: rec.orderId },
                  ),
                );
              }
            } else if (!spent) {
              if (rec.role === 'seller') {
                const conf = await ex.confirmedAt(rec.escrow.depositTxid);
                const value =
                  rec.escrow.outputSats ?? (await ex.outputValue(rec.escrow.depositTxid, 0));
                found.push({
                  rec,
                  kind: 'escrow',
                  sats: value,
                  utxos: [{ txid: rec.escrow.depositTxid, vout: 0, value }],
                  wait: blocksLeft(conf, rec.escrow.rescueCsv, height),
                });
              } else {
                info.push(
                  t(
                    'Order #{{id}}: the escrow is still locked. Only the seller can take it back alone (after the timelock); as the buyer you get it through the release with the seller, or a dispute while the coordinator is up.',
                    { id: rec.orderId },
                  ),
                );
              }
            }
          }
        } catch (e) {
          info.push(
            t('Order #{{id}}: could not be checked ({{e}}).', {
              id: rec.orderId,
              e: String((e as Error).message ?? e),
            }),
          );
        }
      }
      setItems(found);
      setNotes(info);
    } catch (e) {
      setError(t('Could not read the explorer: {{e}}', { e: String((e as Error).message ?? e) }));
    } finally {
      setBusy(false);
    }
  };

  const recover = async (it: Item): Promise<void> => {
    if (!token) return;
    setBusy(true);
    setError('');
    try {
      const ex = explorer();
      if (it.kind === 'fair') {
        // nothing to sign: the rescue was fixed and signed by both before the deposit
        const built = storedFairRescue(it.rec);
        await ex.broadcast(built.hex);
        setDone((d) => ({ ...d, [`${it.rec.orderId}-${it.kind}${it.key ?? ''}`]: built }));
        return;
      }
      let script: Uint8Array;
      try {
        script = addressToScript(dest.trim());
      } catch {
        throw new RescueError('Write a valid Bitcoin address of yours first');
      }
      const rate = await ex.feeRate();
      const keys = keysOf(token, it.rec);
      const built =
        it.kind === 'funding'
          ? buildFundingSweep(it.rec, keys, it.utxos, script, rate)
          : it.kind === 'bond'
            ? buildBondRefund(it.rec, keys, it.utxos, script, rate)
            : buildEscrowRescue(it.rec, keys, it.utxos[0], script, rate);
      await ex.broadcast(built.hex);
      setDone((d) => ({ ...d, [`${it.rec.orderId}-${it.kind}${it.key ?? ''}`]: built }));
    } catch (e) {
      setError(e instanceof RescueError ? t(e.message) : String((e as Error).message ?? e));
    } finally {
      setBusy(false);
    }
  };

  const label = (k: Kind): string =>
    k === 'funding'
      ? t('Sats left on your funding address')
      : k === 'bond'
        ? t('Your bond address (the bond and any other sats sent to it)')
        : k === 'fair'
          ? t('Your part of the escrow (pre-signed rescue)')
          : t('The escrow (seller rescue)');

  const waitText = (w: number | null): string =>
    w === null
      ? t('Waiting for its confirmation')
      : w === 0
        ? t('You can recover it now')
        : t('In {{blocks}} blocks (about {{hours}} h)', {
            blocks: pn(w),
            hours: Math.ceil((w * MIN_PER_BLOCK) / 60),
          });

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth='sm'>
      <DialogTitle>{t('Recover without the coordinator')}</DialogTitle>
      <DialogContent>
        <Typography variant='body2' sx={{ mb: 1 }}>
          {t(
            'If the coordinator disappears, this robot can take its onchain sats back alone: what is left on its funding address at any time, its bond after its timelock, and its part of the escrow after the rescue timelock (seller: the trade amount, its bond and its fee; buyer: its bond and its fee; orders before this version: the seller takes the whole escrow). The app uses the data it kept on this device and your robot token; nothing goes to the coordinator.',
          )}
        </Typography>
        {recs.length === 0 ? (
          <Alert severity='info'>
            {t('This robot has no onchain order saved on this device.')}
          </Alert>
        ) : (
          <Typography variant='caption'>
            {t('Saved onchain orders: {{ids}}', {
              ids: recs.map((r) => `#${r.orderId}`).join(', '),
            })}
          </Typography>
        )}
        <TextField
          fullWidth
          size='small'
          margin='dense'
          label={t('Explorer (Esplora API)')}
          value={explorerUrl}
          onChange={(e) => setExplorerUrl(e.target.value)}
          helperText={t('Default: {{url}}. Any Esplora/mempool API of the BLAKE2b chain works.', {
            url: DEFAULT_EXPLORER,
          })}
        />
        <TextField
          fullWidth
          size='small'
          margin='dense'
          label={t('Your Bitcoin address (where the sats go)')}
          value={dest}
          onChange={(e) => setDest(e.target.value)}
        />
        {error ? (
          <Alert severity='error' sx={{ mt: 1 }}>
            {error}
          </Alert>
        ) : null}
        {busy ? (
          <Box sx={{ textAlign: 'center', mt: 1 }}>
            <CircularProgress size={24} />
          </Box>
        ) : null}
        {items !== null ? (
          <Box sx={{ mt: 1 }}>
            {tip !== null ? (
              <Typography variant='caption'>{t('Current block: {{h}}', { h: pn(tip) })}</Typography>
            ) : null}
            {items.length === 0 && notes.length === 0 ? (
              <Alert severity='success' sx={{ mt: 1 }}>
                {t('Nothing left to recover for this robot.')}
              </Alert>
            ) : null}
            {items.map((it) => {
              const key = `${it.rec.orderId}-${it.kind}${it.key ?? ''}`;
              const ok = done[key];
              return (
                <Box key={key} sx={{ mt: 1 }}>
                  <Divider sx={{ mb: 1 }} />
                  <Typography variant='body2'>
                    <b>
                      {t('Order #{{id}}', { id: it.rec.orderId })} · {label(it.kind)}
                    </b>
                    {': '}
                    {pn(it.sats)} Sats
                  </Typography>
                  <Typography variant='caption'>{waitText(it.wait)}</Typography>
                  {ok ? (
                    <Alert severity='success' sx={{ mt: 0.5, wordBreak: 'break-all' }}>
                      {t('Sent: {{sats}} Sats (mining fee {{fee}}). TXID {{txid}}', {
                        sats: pn(ok.sent),
                        fee: pn(ok.fee),
                        txid: ok.txid,
                      })}
                      <Box>
                        <Button size='small' onClick={() => systemClient.copyToClipboard(ok.txid)}>
                          {t('Copy TXID')}
                        </Button>
                        <Button size='small' onClick={() => systemClient.copyToClipboard(ok.hex)}>
                          {t('Copy signed transaction')}
                        </Button>
                      </Box>
                    </Alert>
                  ) : (
                    <Box>
                      <Button
                        variant='contained'
                        size='small'
                        sx={{ mt: 0.5 }}
                        disabled={busy || it.wait !== 0 || (it.kind !== 'fair' && !dest.trim())}
                        onClick={() => {
                          void recover(it);
                        }}
                      >
                        {t('Recover')}
                      </Button>
                    </Box>
                  )}
                </Box>
              );
            })}
            {notes.map((n) => (
              <Alert key={n} severity='info' sx={{ mt: 1 }}>
                {n}
              </Alert>
            ))}
          </Box>
        ) : null}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>{t('Close')}</Button>
        <Button
          variant='contained'
          disabled={busy || recs.length === 0}
          onClick={() => {
            void check();
          }}
        >
          {t('Check')}
        </Button>
      </DialogActions>
    </Dialog>
  );
};

/** The "Recover without the coordinator" button of a robot page: shown only when this robot
 * has onchain orders kept on this device. */
export const OnchainRescueButton = ({
  token,
}: {
  token: string | undefined;
}): React.JSX.Element => {
  const { t } = useTranslation();
  const [open, setOpen] = useState<boolean>(false);
  const [count, setCount] = useState<number>(0);
  useEffect(() => {
    if (!token) {
      setCount(0);
      return;
    }
    void loadRescues(systemClient, token).then((r) => {
      setCount(r.length);
    });
  }, [token, open]);
  if (count === 0) return <></>;
  return (
    <>
      <Button
        size='small'
        color='secondary'
        onClick={() => {
          setOpen(true);
        }}
      >
        {t('Recover without the coordinator')}
      </Button>
      <OnchainRescueDialog
        open={open}
        onClose={() => {
          setOpen(false);
        }}
        token={token}
      />
    </>
  );
};

export default OnchainRescueDialog;
