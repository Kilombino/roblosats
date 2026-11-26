import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Box, Button, Typography } from '@mui/material';
import { type Order } from '../../../models';
import { type SubmitActionProps } from '../index';
import { useDisputeChat } from './useDisputeChat';

interface AttachChatPromptProps {
  order: Order;
  shortAlias: string;
  submitAction: (props: SubmitActionProps) => void;
}

/** 26 nov 2026 (Kilombino TG 15581/15584): after its statement, a party that did not attach
 * the chat can attach it once, while the dispute is open. */
export const AttachChatPrompt = ({
  order,
  shortAlias,
  submitAction,
}: AttachChatPromptProps): React.JSX.Element | null => {
  const { t } = useTranslation();
  const { messages, loading } = useDisputeChat(order, shortAlias);
  const [sent, setSent] = useState<boolean>(false);
  const attached = Boolean((order as unknown as { chat_attached?: boolean }).chat_attached);

  if (attached) {
    return (
      <Typography variant='body2' color='text.secondary'>
        {t('Your chat is attached to the dispute.')}
      </Typography>
    );
  }
  return (
    <Box
      sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 1, width: '100%' }}
    >
      <Typography variant='body2'>
        {t(
          'You did not attach the chat to your statement. You can attach it now, once: it helps the coordinator decide, but it shows them your conversation.',
        )}
      </Typography>
      <Button
        variant='contained'
        disabled={loading || sent || messages.length === 0}
        onClick={() => {
          setSent(true);
          submitAction({ action: 'attach_chat', chat_log: JSON.stringify(messages) });
        }}
      >
        {t('Attach the chat')}
      </Button>
      {!loading && messages.length === 0 ? (
        <Typography variant='caption' color='text.secondary'>
          {t('There are no chat messages to attach.')}
        </Typography>
      ) : null}
    </Box>
  );
};

export default AttachChatPrompt;
