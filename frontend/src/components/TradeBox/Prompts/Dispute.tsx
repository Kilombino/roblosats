import React, { useContext, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Checkbox,
  FormControlLabel,
  Grid,
  MenuItem,
  Select,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material';
import { LoadingButton } from '@mui/lab';
import { Contact, Order } from '../../../models';
import { UseFederationStoreType, FederationContext } from '../../../contexts/FederationContext';
import { SubmitActionProps } from '../index';
import { useDisputeChat } from './useDisputeChat';
import { type DisputeForm, statementWithChat } from './disputeStatement';

export { type DisputeForm, defaultDispute } from './disputeStatement';

interface DisputePromptProps {
  submitAction: (propr: SubmitActionProps) => void;
  order: Order;
  dispute: DisputeForm;
  shortAlias: string;
  setDispute: (state: DisputeForm) => void;
}

export const DisputePrompt = ({
  submitAction,
  order,
  dispute,
  shortAlias,
  setDispute,
}: DisputePromptProps): React.JSX.Element => {
  const { t } = useTranslation();
  const { federation } = useContext<UseFederationStoreType>(FederationContext);

  const contactMethods: Contact = federation.getCoordinator(shortAlias)?.contact ?? {};

  const { messages, loading: loadingChat } = useDisputeChat(order, shortAlias);
  const [sending, setSending] = useState<boolean>(false);
  const loading = loadingChat || sending;

  const submitStatement = function (): void {
    let statement = dispute.statement;
    if (!statement || statement.trim() === '' || statement.length < 100) {
      setDispute({
        ...dispute,
        badStatement: t('The statement is too short. Make sure to be thorough.'),
      });
    } else if (!dispute.contact || dispute.contact.trim() === '') {
      setDispute({ ...dispute, badContact: t('A contact method is required') });
    } else {
      const { contactMethod, contact } = dispute;
      statement = `${contactMethod ?? ''}: ${contact ?? ''} \n\n ${statement}`;
      // 26 nov 2026: a long chat no longer makes the statement too long: it goes apart
      const sent = statementWithChat(statement, messages, dispute.attachLogs);
      statement = sent.statement;
      const chatLog = sent.chatLog;

      setSending(true);
      submitAction({ action: 'submit_statement', statement, chat_log: chatLog });
    }
  };

  return (
    <Grid container style={{ width: '100%', padding: 16 }} sx={{ flexDirection: 'column' }}>
      <Grid>
        <Typography variant='body2'>
          {t(
            'Please, submit your statement. Be clear and specific about what happened and provide the necessary evidence. You MUST provide a contact method: burner email, SimpleX incognito link or telegram (make sure to create a searchable username) to follow up with the dispute solver (your trade host/coordinator). Disputes are solved at the discretion of real robots (aka humans), so be as helpful as possible to ensure a fair outcome.',
          )}
        </Typography>
      </Grid>
      <Grid>
        <Grid size={12}>
          <TextField
            error={dispute.badStatement !== ''}
            helperText={dispute.badStatement}
            label={t('Submit dispute statement')}
            required
            fullWidth
            multiline
            rows={4}
            onChange={(e) => {
              setDispute({ ...dispute, statement: e.target.value });
            }}
          />
        </Grid>
        <Grid size={12}>
          <Select
            variant='standard'
            required
            fullWidth
            displayEmpty
            value={dispute.contactMethod}
            onChange={(e) => {
              setDispute({ ...dispute, contactMethod: String(e.target.value) });
            }}
            renderValue={(value: unknown) => {
              const v = value as string;
              if (!v) {
                return <span style={{ color: 'gray' }}>{t('Select a contact method')}</span>;
              }
              return v.charAt(0).toUpperCase() + v.slice(1);
            }}
          >
            <MenuItem value='' disabled>
              {t('Select a contact method')}
            </MenuItem>
            {Object.keys(contactMethods).map((contact) => {
              if (
                !(contactMethods as Record<string, string>)[contact] ||
                (contactMethods as Record<string, string>)[contact] === ''
              )
                return <></>;
              if (['pgp', 'fingerprint', 'website'].includes(contact)) return <></>;

              return (
                <MenuItem value={contact as string} key={contact}>
                  <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap' }}>
                    {contact.charAt(0).toUpperCase() + contact.slice(1)}
                  </div>
                </MenuItem>
              );
            })}
            <MenuItem value={'other'} key='other'>
              <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap' }}>
                {t('Other')}
              </div>
            </MenuItem>
          </Select>
        </Grid>
        <Grid size={12}>
          <TextField
            error={dispute.badContact !== ''}
            helperText={dispute.badContact}
            fullWidth
            required
            size='small'
            onChange={(e) => {
              setDispute({ ...dispute, contact: e.target.value });
            }}
          />
        </Grid>
        <Grid>
          <Tooltip
            enterTouchDelay={0}
            placement='top'
            title={t(
              'Attaching chat logs helps the dispute resolution process and adds transparency. However, it might compromise your privacy.',
            )}
          >
            <FormControlLabel
              control={
                <Checkbox
                  checked={dispute.attachLogs}
                  onChange={(_e, attachLogs) => {
                    setDispute({ ...dispute, attachLogs });
                  }}
                />
              }
              label={t('Attach chat logs')}
            />
          </Tooltip>
        </Grid>
        <Grid container sx={{ width: '100%', justifyContent: 'center' }}>
          <LoadingButton
            onClick={submitStatement}
            variant='contained'
            color='primary'
            loading={loading}
          >
            {t('Submit')}
          </LoadingButton>
        </Grid>
      </Grid>
    </Grid>
  );
};

export default DisputePrompt;
