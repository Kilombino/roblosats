import React from 'react';
import { useTranslation } from 'react-i18next';
import {
  Dialog,
  DialogTitle,
  DialogActions,
  DialogContent,
  DialogContentText,
  Button,
} from '@mui/material';

interface Props {
  open: boolean;
  onClose: () => void;
  onAccept: () => void;
  /** 0.9.8: the bond is an onchain payment (not a Lightning hold invoice) */
  onchainBond?: boolean;
}

const CancelOrderDialog = ({
  open,
  onClose,
  onAccept,
  onchainBond = false,
}: Props): React.JSX.Element => {
  const { t } = useTranslation();

  return (
    <Dialog open={open} onClose={onClose}>
      <DialogTitle>{t('Cancel the order?')}</DialogTitle>

      <DialogContent>
        <DialogContentText>
          {onchainBond
            ? t(
                'If you already paid the bond, you will be able to take it back from this order once it has 1 confirmation (Get my bond back).',
              )
            : t(
                'If the order is cancelled now but you already tried to pay the invoice, you might loose your bond.',
              )}
        </DialogContentText>
      </DialogContent>

      <DialogActions>
        <Button
          onClick={() => {
            onClose();
          }}
        >
          {t('Back')}
        </Button>
        <Button onClick={onAccept}>{t('Confirm cancellation')}</Button>
      </DialogActions>
    </Dialog>
  );
};

export default CancelOrderDialog;
