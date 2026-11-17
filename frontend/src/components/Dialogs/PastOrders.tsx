// "My past orders" (Roblosats rl31-beta14): the finished orders of this robot on every
// coordinator that keeps them (/api/pastorders/, upstream RoboSats has no such list), each one
// with its final summary. Kept on this device too, so the list still shows what was seen
// before when a coordinator is offline or slow over Tor.
import React, { useContext, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import {
  Accordion,
  AccordionDetails,
  AccordionSummary,
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Typography,
} from '@mui/material';
import { ExpandMore, History } from '@mui/icons-material';

import { apiClient } from '../../services/api';
import { systemClient } from '../../services/System';
import { pn } from '../../utils';
import { GarageContext, type UseGarageStoreType } from '../../contexts/GarageContext';
import { FederationContext, type UseFederationStoreType } from '../../contexts/FederationContext';
import FinalSummary, { outcomeOf, type FinalSummaryData } from '../FinalSummary';

interface CachedItem {
  shortAlias: string;
  summary: FinalSummaryData;
  seenAt: number;
}

type Filter = 'all' | 'success' | 'cancelled' | 'expired' | 'lost';

const cacheKey = (hashId: string): string => `roblosats_past_orders_${hashId}`;

export const loadPastOrders = async (hashId: string): Promise<Record<string, CachedItem>> => {
  try {
    const raw = await systemClient.getItem(cacheKey(hashId));
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Record<string, CachedItem>;
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
};

const savePastOrders = (hashId: string, items: Record<string, CachedItem>): void => {
  try {
    systemClient.setItem(cacheKey(hashId), JSON.stringify(items));
  } catch (e) {
    console.log(e);
  }
};

const matches = (s: FinalSummaryData, f: Filter): boolean => {
  if (f === 'all') return true;
  if (f === 'lost') return (s.bond_lost_sats ?? 0) > 0;
  if (f === 'success') return ['success', 'dispute_won', 'sending'].includes(s.outcome);
  if (f === 'cancelled')
    return ['cancelled', 'collab_cancelled', 'dispute_returned', 'taker_left'].includes(s.outcome);
  if (f === 'expired') return s.outcome === 'expired';
  return true;
};

const PastOrdersDialog = ({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}): React.JSX.Element => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { garage } = useContext<UseGarageStoreType>(GarageContext);
  const { federation } = useContext<UseFederationStoreType>(FederationContext);
  const slot = garage.getSlot();
  const hashId = slot?.hashId ?? '';
  const [items, setItems] = useState<Record<string, CachedItem>>({});
  const [loading, setLoading] = useState<boolean>(false);
  const [offline, setOffline] = useState<string[]>([]);
  const [filter, setFilter] = useState<Filter>('all');

  useEffect(() => {
    if (!open || !slot || !hashId) return;
    let cancelled = false;
    void (async () => {
      const cached = await loadPastOrders(hashId);
      if (cancelled) return;
      setItems(cached);
      setLoading(true);
      const down: string[] = [];
      const merged: Record<string, CachedItem> = { ...cached };
      await Promise.all(
        Object.values(slot.robots).map(async (robot) => {
          const coordinator = federation.getCoordinator(robot.shortAlias);
          const auth = robot.getAuthHeaders();
          if (!coordinator?.url || !coordinator.enabled || !auth || !robot.hasEnoughEntropy) return;
          try {
            const data = (await apiClient.get(coordinator.url, '/api/pastorders/', auth, true)) as {
              orders?: FinalSummaryData[];
            } | null;
            if (!data || !Array.isArray(data.orders)) {
              // upstream coordinators have no history: only complain about those we saw before
              if (Object.values(cached).some((c) => c.shortAlias === robot.shortAlias)) {
                down.push(coordinator.longAlias ?? robot.shortAlias);
              }
              return;
            }
            for (const s of data.orders) {
              if (!s || typeof s.order_id !== 'number') continue;
              merged[`${robot.shortAlias}:${s.order_id}`] = {
                shortAlias: robot.shortAlias,
                summary: s,
                seenAt: Date.now(),
              };
            }
          } catch {
            if (Object.values(cached).some((c) => c.shortAlias === robot.shortAlias)) {
              down.push(coordinator.longAlias ?? robot.shortAlias);
            }
          }
        }),
      );
      if (cancelled) return;
      savePastOrders(hashId, merged);
      setItems(merged);
      setOffline(down);
      setLoading(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [open, hashId]);

  const list = useMemo(
    () =>
      Object.values(items)
        .filter((i) => matches(i.summary, filter))
        .sort((a, b) =>
          String(b.summary.created_at ?? '').localeCompare(String(a.summary.created_at ?? '')),
        ),
    [items, filter],
  );

  const mainAmount = (s: FinalSummaryData): string => {
    if ((s.bond_lost_sats ?? 0) > 0) return `-${pn(s.bond_lost_sats ?? 0)} Sats`;
    if ((s.received_sats ?? 0) > 0) return `+${pn(s.received_sats ?? 0)} Sats`;
    if ((s.sent_sats ?? 0) > 0) return `-${pn(s.sent_sats ?? 0)} Sats`;
    return '';
  };

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth='sm' scroll='paper'>
      <DialogTitle>{t('My past orders')}</DialogTitle>
      <DialogContent dividers sx={{ px: 1 }}>
        <Box sx={{ display: 'flex', flexWrap: 'wrap', justifyContent: 'center', gap: 0.75, mb: 1 }}>
          {(
            [
              ['all', t('All')],
              ['success', t('Successful')],
              ['cancelled', t('Cancelled ones')],
              ['expired', t('Expired ones')],
              ['lost', t('Bond lost')],
            ] as Array<[Filter, string]>
          ).map(([f, label]) => (
            <Chip
              key={f}
              label={label}
              size='small'
              color={filter === f ? 'primary' : 'default'}
              variant={filter === f ? 'filled' : 'outlined'}
              onClick={() => {
                setFilter(f);
              }}
            />
          ))}
        </Box>
        {loading ? (
          <Box sx={{ display: 'flex', justifyContent: 'center', gap: 1, mb: 1 }}>
            <CircularProgress size={18} />
            <Typography variant='caption'>{t('Asking the coordinators…')}</Typography>
          </Box>
        ) : null}
        {offline.length > 0 ? (
          <Alert severity='info' sx={{ mb: 1 }}>
            {t('Not reachable now, showing what this device kept: {{names}}', {
              names: offline.join(', '),
            })}
          </Alert>
        ) : null}
        {!loading && list.length === 0 ? (
          <Typography align='center' color='text.secondary'>
            {t('No finished orders yet')}
          </Typography>
        ) : null}
        {list.map((i) => {
          const s = i.summary;
          const o = outcomeOf(s, t);
          const coordinator = federation.getCoordinator(i.shortAlias);
          const date = s.created_at ? new Date(s.created_at).toLocaleDateString() : '';
          // Lightning orders that were cancelled have no order page any more
          const canOpen = !([4, 12].includes(s.status) && s.escrow_type === 'lightning');
          return (
            <Accordion key={`${i.shortAlias}:${s.order_id}`} disableGutters>
              <AccordionSummary expandIcon={<ExpandMore />}>
                <Box
                  sx={{
                    display: 'flex',
                    flexWrap: 'wrap',
                    alignItems: 'center',
                    gap: 1,
                    width: '100%',
                  }}
                >
                  <Typography variant='body2'>
                    <b>#{s.order_id}</b> · {coordinator?.longAlias ?? i.shortAlias} · {date}
                  </Typography>
                  <Chip size='small' label={o.label} color={o.color} />
                  <Typography
                    variant='body2'
                    sx={{ ml: 'auto' }}
                    color={(s.bond_lost_sats ?? 0) > 0 ? 'error.main' : undefined}
                  >
                    {mainAmount(s)}
                  </Typography>
                </Box>
              </AccordionSummary>
              <AccordionDetails sx={{ px: 1 }}>
                <FinalSummary summary={s} title={false} />
                {canOpen ? (
                  <Box sx={{ textAlign: 'center', mt: 1 }}>
                    <Button
                      size='small'
                      onClick={() => {
                        onClose();
                        navigate(`/order/${i.shortAlias}/${s.order_id}`);
                      }}
                    >
                      {t('Open the order')}
                    </Button>
                  </Box>
                ) : null}
              </AccordionDetails>
            </Accordion>
          );
        })}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>{t('Close')}</Button>
      </DialogActions>
    </Dialog>
  );
};

export const PastOrdersButton = (): React.JSX.Element => {
  const { t } = useTranslation();
  const [open, setOpen] = useState<boolean>(false);
  return (
    <>
      <Button
        size='small'
        color='primary'
        startIcon={<History />}
        onClick={() => {
          setOpen(true);
        }}
      >
        {t('My past orders')}
      </Button>
      <PastOrdersDialog
        open={open}
        onClose={() => {
          setOpen(false);
        }}
      />
    </>
  );
};

export default PastOrdersDialog;
