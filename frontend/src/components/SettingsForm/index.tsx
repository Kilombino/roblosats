import React, { useContext, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { type UseAppStoreType, AppContext } from '../../contexts/AppContext';
import {
  Paper,
  Switch,
  useTheme,
  FormControlLabel,
  List,
  ListItem,
  ListItemIcon,
  Slider,
  Typography,
  ToggleButtonGroup,
  ToggleButton,
  Button,
} from '@mui/material';
import SelectLanguage from './SelectLanguage';
import {
  Translate,
  Palette,
  LightMode,
  DarkMode,
  SettingsOverscan,
  Link,
  QrCode,
  SettingsInputAntenna,
  NotificationsActive,
  Vibration,
  SystemUpdate,
  BugReport,
  BatteryChargingFull,
  Public,
} from '@mui/icons-material';
import { CHECK_UPDATE_EVENT, BACKGROUND_EVENT, RecentLogsDialog } from '../Dialogs';
import { systemClient } from '../../services/System';
import Tor from '../Icons/Tor';
import { UseFederationStoreType, FederationContext } from '../../contexts/FederationContext';

interface SettingsFormProps {
  dense?: boolean;
}

const SettingsForm = ({ dense = false }: SettingsFormProps): React.JSX.Element => {
  const { updateConnection } = useContext<UseFederationStoreType>(FederationContext);
  const { settings, setSettings, client } = useContext<UseAppStoreType>(AppContext);
  const theme = useTheme();
  const { t } = useTranslation();
  const [openLogs, setOpenLogs] = useState<boolean>(false);
  // 0.9.11: Tor (built-in / Orbot) and Clearnet change in place: the app used to restart (a
  // black screen). The federation reconnects through the settings effect.
  const setNetworkMode = (useProxy: boolean, clearnet: boolean): void => {
    systemClient.setItem('settings_use_proxy', String(useProxy));
    systemClient.setItem('settings_clearnet', String(clearnet));
    window.AndroidAppRobosats?.setNetworkMode?.(useProxy, clearnet);
    setSettings({ ...settings, useProxy, clearnet });
  };
  const [checkUpdates, setCheckUpdates] = useState<boolean>(true);
  React.useEffect(() => {
    void systemClient.getItem('settings_check_updates').then((v) => {
      setCheckUpdates(v !== 'false');
    });
  }, []);
  const fontSizes = [
    { label: 'XS', value: { basic: 12, pro: 10 } },
    { label: 'S', value: { basic: 13, pro: 11 } },
    { label: 'M', value: { basic: 14, pro: 12 } },
    { label: 'L', value: { basic: 15, pro: 13 } },
    { label: 'XL', value: { basic: 16, pro: 14 } },
  ];

  return (
    <List dense={dense} style={{ width: '100%', padding: 16 }}>
      <ListItem>
        <ListItemIcon sx={{ minWidth: 56 }}>
          <Translate />
        </ListItemIcon>
        <SelectLanguage
          language={settings.language ?? 'en'}
          setLanguage={(language) => {
            setSettings({ ...settings, language });
            systemClient.setItem('settings_language', language);
          }}
        />
      </ListItem>

      <ListItem>
        <ListItemIcon sx={{ minWidth: 56 }}>
          <Palette />
        </ListItemIcon>
        <FormControlLabel
          labelPlacement='end'
          label={settings.mode === 'dark' ? t('Dark') : t('Light')}
          control={
            <Switch
              checked={settings.mode === 'dark'}
              checkedIcon={
                <Paper
                  elevation={3}
                  sx={{
                    width: '1.2em',
                    height: '1.2em',
                    borderRadius: '0.4em',
                    backgroundColor: 'white',
                    position: 'relative',
                    top: `${7 - 0.5 * theme.typography.fontSize}px`,
                  }}
                >
                  <DarkMode sx={{ width: '0.8em', height: '0.8em', color: '#666' }} />
                </Paper>
              }
              icon={
                <Paper
                  elevation={3}
                  sx={{
                    width: '1.2em',
                    height: '1.2em',
                    borderRadius: '0.4em',
                    backgroundColor: 'white',
                    padding: '0.07em',
                    position: 'relative',
                    top: `${7 - 0.5 * theme.typography.fontSize}px`,
                  }}
                >
                  <LightMode sx={{ width: '0.67em', height: '0.67em', color: '#666' }} />
                </Paper>
              }
              onChange={(e) => {
                const mode = e.target.checked ? 'dark' : 'light';
                setSettings({ ...settings, mode });
                systemClient.setItem('settings_mode', mode);
              }}
            />
          }
        />
        {settings.mode === 'dark' ? (
          <>
            <ListItemIcon sx={{ minWidth: 56 }}>
              <QrCode />
            </ListItemIcon>
            <FormControlLabel
              labelPlacement='end'
              label={settings.lightQRs ? t('Light') : t('Dark')}
              control={
                <Switch
                  checked={!settings.lightQRs}
                  checkedIcon={
                    <Paper
                      elevation={3}
                      sx={{
                        width: '1.2em',
                        height: '1.2em',
                        borderRadius: '0.4em',
                        backgroundColor: 'white',
                        position: 'relative',
                        top: `${7 - 0.5 * theme.typography.fontSize}px`,
                      }}
                    >
                      <DarkMode sx={{ width: '0.8em', height: '0.8em', color: '#666' }} />
                    </Paper>
                  }
                  icon={
                    <Paper
                      elevation={3}
                      sx={{
                        width: '1.2em',
                        height: '1.2em',
                        borderRadius: '0.4em',
                        backgroundColor: 'white',
                        padding: '0.07em',
                        position: 'relative',
                        top: `${7 - 0.5 * theme.typography.fontSize}px`,
                      }}
                    >
                      <LightMode sx={{ width: '0.67em', height: '0.67em', color: '#666' }} />
                    </Paper>
                  }
                  onChange={(e) => {
                    const lightQRs = !e.target.checked;
                    setSettings({ ...settings, lightQRs });
                    systemClient.setItem('settings_light_qr', String(lightQRs));
                  }}
                />
              }
            />
          </>
        ) : (
          <></>
        )}
      </ListItem>

      <ListItem>
        <ListItemIcon sx={{ minWidth: 56 }}>
          <SettingsOverscan />
        </ListItemIcon>
        <Slider
          value={settings.fontSize}
          min={settings.frontend === 'basic' ? 12 : 10}
          max={settings.frontend === 'basic' ? 16 : 14}
          step={1}
          onChange={(_e, newValue) => {
            const fontSize = Array.isArray(newValue) ? newValue[0] : newValue;
            setSettings({ ...settings, fontSize });
            systemClient.setItem(`settings_fontsize_${settings.frontend}`, fontSize.toString());
          }}
          valueLabelDisplay='off'
          marks={fontSizes.map(({ label, value }) => ({
            label: <Typography variant='caption'>{t(label)}</Typography>,
            value: settings.frontend === 'basic' ? value.basic : value.pro,
          }))}
          track={false}
        />
      </ListItem>

      <ListItem>
        <ListItemIcon sx={{ minWidth: 56 }}>
          <SettingsInputAntenna />
        </ListItemIcon>
        <ToggleButtonGroup
          sx={{ width: '100%' }}
          exclusive={true}
          value={settings.connection}
          onChange={(_e, connection) => {
            setSettings({ ...settings, connection });
            systemClient.setItem('settings_connection', connection);
          }}
        >
          <ToggleButton value='api' color='primary' sx={{ flexGrow: 1 }}>
            {t('API')}
          </ToggleButton>
          <ToggleButton value='nostr' color='secondary' sx={{ flexGrow: 1 }}>
            {t('nostr')}
          </ToggleButton>
        </ToggleButtonGroup>
      </ListItem>

      <ListItem>
        <ListItemIcon sx={{ minWidth: 56 }}>
          <Link />
        </ListItemIcon>
        <ToggleButtonGroup
          sx={{ width: '100%' }}
          exclusive={true}
          value={settings.network}
          onChange={(_e, network) => {
            const newSetting = { ...settings, network };
            updateConnection(newSetting);
            setSettings({ ...settings, network });
            systemClient.setItem('settings_network', network);
          }}
        >
          <ToggleButton value='mainnet' color='primary' sx={{ flexGrow: 1 }}>
            {t('Mainnet')}
          </ToggleButton>
          <ToggleButton value='testnet' color='secondary' sx={{ flexGrow: 1 }}>
            {t('Testnet')}
          </ToggleButton>
        </ToggleButtonGroup>
      </ListItem>

      <ListItem>
        <ListItemIcon sx={{ minWidth: 56 }}>
          <Vibration />
        </ListItemIcon>
        <ToggleButtonGroup
          exclusive={true}
          sx={{ width: '100%' }}
          value={settings.actionAlerts}
          onChange={(_e, actionAlerts) => {
            if (actionAlerts === null) return;
            setSettings({ ...settings, actionAlerts });
            systemClient.setItem('settings_action_alerts', String(actionAlerts));
          }}
        >
          <ToggleButton value={true} color='primary' sx={{ flexGrow: 1 }}>
            {t('Sound when an order needs you')}
          </ToggleButton>
          <ToggleButton value={false} color='secondary' sx={{ flexGrow: 1 }}>
            {t('Off')}
          </ToggleButton>
        </ToggleButtonGroup>
      </ListItem>

      {client == 'mobile' && (
        <ListItem>
          <ListItemIcon sx={{ minWidth: 56 }}>
            <NotificationsActive />
          </ListItemIcon>
          <ToggleButtonGroup
            exclusive={true}
            sx={{ width: '100%' }}
            value={settings.androidNotifications}
            onChange={(_e, androidNotifications) => {
              setSettings({ ...settings, androidNotifications });
              systemClient.setItem('settings_notifications', String(androidNotifications));
            }}
          >
            <ToggleButton value={true} color='primary' sx={{ flexGrow: 1 }}>
              {t('On')}
            </ToggleButton>
            <ToggleButton value={false} color='secondary' sx={{ flexGrow: 1 }}>
              {t('Off')}
            </ToggleButton>
          </ToggleButtonGroup>
        </ListItem>
      )}

      {client == 'mobile' && (
        <ListItem>
          <ListItemIcon sx={{ minWidth: 56 }}>
            <Tor />
          </ListItemIcon>
          <ToggleButtonGroup
            exclusive={true}
            sx={{ width: '100%' }}
            value={settings.useProxy}
            onChange={(_e, useProxy: boolean | null) => {
              if (useProxy === null) return;
              setNetworkMode(useProxy, settings.clearnet);
            }}
          >
            <ToggleButton value={false} color='primary' sx={{ flexGrow: 1 }}>
              {t('Orbot')}
            </ToggleButton>
            <ToggleButton value={true} color='secondary' sx={{ flexGrow: 1 }}>
              {t('Build-in')}
            </ToggleButton>
          </ToggleButtonGroup>
        </ListItem>
      )}
      {client == 'mobile' && (
        <ListItem>
          <ListItemIcon sx={{ minWidth: 56 }}>
            <Public />
          </ListItemIcon>
          <div style={{ width: '100%' }}>
            <ToggleButtonGroup
              exclusive={true}
              sx={{ width: '100%' }}
              value={settings.clearnet}
              onChange={(_e, clearnet: boolean | null) => {
                if (clearnet === null) return;
                setNetworkMode(settings.useProxy, clearnet);
              }}
            >
              <ToggleButton value={false} color='primary' sx={{ flexGrow: 1 }}>
                {t('Tor only')}
              </ToggleButton>
              <ToggleButton value={true} color='secondary' sx={{ flexGrow: 1 }}>
                {t('Clearnet (beta)')}
              </ToggleButton>
            </ToggleButtonGroup>
            {settings.clearnet ? (
              <Typography
                variant='caption'
                color='text.secondary'
                sx={{ display: 'block', mt: 0.5 }}
              >
                {t(
                  'Coordinators with a clearnet address are reached directly, faster but without Tor: they see your IP address. The others still go through Tor.',
                )}
              </Typography>
            ) : null}
          </div>
        </ListItem>
      )}
      {/* 0.9.6: update notice, recent logs, background reliability */}
      <ListItem>
        <ListItemIcon sx={{ minWidth: 56 }}>
          <SystemUpdate />
        </ListItemIcon>
        <ToggleButtonGroup
          exclusive={true}
          sx={{ width: '100%' }}
          value={checkUpdates}
          onChange={(_e, value) => {
            if (value === null) return;
            setCheckUpdates(value);
            systemClient.setItem('settings_check_updates', String(value));
          }}
        >
          <ToggleButton value={true} color='primary' sx={{ flexGrow: 1 }}>
            {t('Check for updates')}
          </ToggleButton>
          <ToggleButton value={false} color='secondary' sx={{ flexGrow: 1 }}>
            {t('Off')}
          </ToggleButton>
        </ToggleButtonGroup>
        <Button
          size='small'
          sx={{ ml: 1, whiteSpace: 'nowrap' }}
          onClick={() => window.dispatchEvent(new Event(CHECK_UPDATE_EVENT))}
        >
          {t('Check now')}
        </Button>
      </ListItem>

      <ListItem>
        <ListItemIcon sx={{ minWidth: 56 }}>
          <BugReport />
        </ListItemIcon>
        <Button
          variant='outlined'
          sx={{ width: '100%' }}
          onClick={() => {
            setOpenLogs(true);
          }}
        >
          {t('Recent logs')}
        </Button>
      </ListItem>

      {client == 'mobile' && window.AndroidAppRobosats?.backgroundStatus ? (
        <ListItem>
          <ListItemIcon sx={{ minWidth: 56 }}>
            <BatteryChargingFull />
          </ListItemIcon>
          <Button
            variant='outlined'
            sx={{ width: '100%' }}
            onClick={() => window.dispatchEvent(new Event(BACKGROUND_EVENT))}
          >
            {t('Notifications when the app is closed')}
          </Button>
        </ListItem>
      ) : null}
      <RecentLogsDialog
        open={openLogs}
        onClose={() => {
          setOpenLogs(false);
        }}
      />
    </List>
  );
};

export default SettingsForm;
