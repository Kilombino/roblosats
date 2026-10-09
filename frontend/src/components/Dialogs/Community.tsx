import React from 'react';
import { useTranslation } from 'react-i18next';
import {
  Dialog,
  DialogContent,
  Divider,
  List,
  ListItemText,
  ListItemIcon,
  ListItemButton,
  Typography,
} from '@mui/material';
import GitHubIcon from '@mui/icons-material/GitHub';
import { Telegram } from '@mui/icons-material';
import { NostrIcon } from '../Icons';

interface Props {
  open: boolean;
  onClose: () => void;
}

const CommunityDialog = ({ open = false, onClose }: Props): React.JSX.Element => {
  const { t } = useTranslation();

  return (
    <Dialog
      open={open}
      onClose={onClose}
      aria-labelledby='community-dialog-title'
      aria-describedby='community-description'
    >
      <DialogContent>
        <Typography component='h5' variant='h5'>
          {t('Community')}
        </Typography>

        <Typography component='div' variant='body2'>
          <p>
            {t(
              'Support is only offered via SimpleX. Join our community if you have questions or want to hang out with other cool robots. Please, use our Github Issues if you find a bug or want to see new features!',
            )}
          </p>
        </Typography>

        <List dense>
          <Divider />

          <ListItemButton
            component='a'
            target='_blank'
            href='https://t.me/roblosats'
            rel='noreferrer'
          >
            <ListItemIcon sx={{ minWidth: 56 }}>
              <Telegram color='primary' sx={{ height: 32, width: 32 }} />
            </ListItemIcon>

            <ListItemText
              primary={t('Join RoboSats SimpleX group')}
              secondary={t('RoboSats main public support')}
            />
          </ListItemButton>

          <Divider />

          <ListItemButton
            component='a'
            onClick={() => {
              window.open(
                'https://njump.me/npub1vlq05j6wcadtc5fele3k9nwg3u573et965sj9prp9u8mesxd0udsyqqnqw',
                '_blank',
                'noopener,noreferrer',
              );
            }}
          >
            <ListItemIcon sx={{ minWidth: 56 }}>
              <NostrIcon color='primary' sx={{ height: 32, width: 32 }} />
            </ListItemIcon>

            <ListItemText
              primary={t('Follow RoboSats in Nostr')}
              secondary={t('Nostr Official Account')}
            />
          </ListItemButton>

          <Divider />

          <ListItemButton
            component='a'
            target='_blank'
            href='https://github.com/Kilombino/roblosats/issues'
            rel='noreferrer'
          >
            <ListItemIcon sx={{ minWidth: 56 }}>
              <GitHubIcon color='primary' sx={{ height: 32, width: 32 }} />
            </ListItemIcon>

            <ListItemText
              primary={t('Tell us about a new feature or a bug')}
              secondary={t('Github Issues - The Robotic Satoshis Open Source Project')}
            />
          </ListItemButton>
        </List>
      </DialogContent>
    </Dialog>
  );
};

export default CommunityDialog;
