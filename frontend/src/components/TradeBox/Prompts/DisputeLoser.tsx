import React, { useContext } from 'react';
import { useTranslation } from 'react-i18next';
import { List, ListItem, Divider, Typography } from '@mui/material';
import { type Order } from '../../../models';
import {
  FederationContext,
  type UseFederationStoreType,
} from '../../../contexts/FederationContext';

interface DisputeLoserPromptProps {
  order?: Order | null;
}

export const DisputeLoserPrompt = ({ order }: DisputeLoserPromptProps): React.JSX.Element => {
  const { t } = useTranslation();
  const { federation } = useContext<UseFederationStoreType>(FederationContext);
  const coordinator = federation.getCoordinator(order?.shortAlias ?? '');
  // Roblosats: each coordinator answers for its own disputes; without an email, another
  // way to reach it, and the Roblosats maintainer as the last resort.
  const contact = coordinator?.contact;
  const email =
    contact?.email ||
    contact?.telegram ||
    contact?.simplex ||
    contact?.nostr ||
    'kilombino@proton.me';

  return (
    <List dense={true}>
      <Divider />
      <ListItem>
        <Typography variant='body2'>
          {t(
            'Unfortunately you have lost the dispute. If you think this is a mistake you can ask to re-open the case by contacting your coordinator. If you think your coordinator was unfair, please fill a claim via email to {{email}}',
            { email },
          )}
        </Typography>
      </ListItem>
    </List>
  );
};

export default DisputeLoserPrompt;
