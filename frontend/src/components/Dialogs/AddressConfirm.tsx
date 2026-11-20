// 0.9.10 (Kilombino): before an onchain address the user typed is sent, show it back in groups
// of four characters with what it is for, and send it only after an explicit "Yes".
import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Box,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Typography,
} from '@mui/material';

import { DEFAULT_HRP, addressErrorText, checkAddress, groupAddress } from '../../utils/address';

interface AddressConfirmDialogProps {
  open: boolean;
  /** What the address is for, already translated ("Your refund will go to:"). */
  message: string;
  address: string;
  onCancel: () => void;
  onAccept: () => void;
}

export const AddressConfirmDialog = ({
  open,
  message,
  address,
  onCancel,
  onAccept,
}: AddressConfirmDialogProps): React.JSX.Element => {
  const { t } = useTranslation();
  const groups = groupAddress(address).split(' ').filter(Boolean);
  return (
    <Dialog open={open} onClose={onCancel} fullWidth maxWidth='xs'>
      <DialogTitle>{t('Check the address')}</DialogTitle>
      <DialogContent>
        <Typography variant='body1' sx={{ mb: 1.5 }}>
          {message}
        </Typography>
        <Box
          data-testid='address-groups'
          sx={{
            fontFamily: 'monospace',
            fontSize: { xs: '1.25rem', sm: '1.4rem' },
            lineHeight: 1.6,
            fontWeight: 600,
            padding: 1.5,
            borderRadius: 1,
            bgcolor: 'action.hover',
            wordBreak: 'normal',
            overflowWrap: 'anywhere',
          }}
        >
          {groups.map((g, i) => (
            <Box
              component='span'
              key={`${i}-${g}`}
              sx={{ display: 'inline-block', mr: '0.6ch', opacity: i % 2 === 0 ? 1 : 0.75 }}
            >
              {g}
            </Box>
          ))}
        </Box>
        <Typography variant='caption' color='text.secondary' sx={{ display: 'block', mt: 1.5 }}>
          {t(
            'Compare it with the address in your wallet, character by character. Sats sent to a wrong address cannot be recovered.',
          )}
        </Typography>
      </DialogContent>
      <DialogActions sx={{ flexWrap: 'wrap', gap: 1 }}>
        <Button onClick={onCancel}>{t('Cancel')}</Button>
        <Button variant='contained' onClick={onAccept}>
          {t('Yes, use this address')}
        </Button>
      </DialogActions>
    </Dialog>
  );
};

export interface ConfirmAddressRequest {
  address: string;
  /** bech32 HRP of the coordinator's network ("bc" when unknown). */
  hrp?: string | null;
  /** What the address is for, already translated. */
  message: string;
  /** Runs only after "Yes", with the address as it must be sent (bech32 in lower case). */
  onConfirm: (address: string) => void;
  onCancel?: () => void;
}

/** Checks a typed address and, if it is good, asks the user to confirm it. Returns the error
 * text to show (nothing is asked then) or null when the confirmation dialog opened. Render
 * `addressDialog` once in the component. */
export const useAddressConfirm = (): {
  confirmAddress: (request: ConfirmAddressRequest) => string | null;
  addressDialog: React.JSX.Element;
} => {
  const { t } = useTranslation();
  const [request, setRequest] = useState<(ConfirmAddressRequest & { checked: string }) | null>(
    null,
  );

  const confirmAddress = (r: ConfirmAddressRequest): string | null => {
    const hrp = r.hrp ?? DEFAULT_HRP;
    const check = checkAddress(r.address, hrp);
    if (!check.ok) return addressErrorText(check.error, t, hrp);
    setRequest({ ...r, checked: check.address });
    return null;
  };

  const addressDialog = (
    <AddressConfirmDialog
      open={request !== null}
      message={request?.message ?? ''}
      address={request?.checked ?? ''}
      onCancel={() => {
        const r = request;
        setRequest(null);
        r?.onCancel?.();
      }}
      onAccept={() => {
        const r = request;
        setRequest(null);
        if (r) r.onConfirm(r.checked);
      }}
    />
  );

  return { confirmAddress, addressDialog };
};

export default AddressConfirmDialog;
