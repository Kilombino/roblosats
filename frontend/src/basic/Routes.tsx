import React, { useContext, useEffect } from 'react';
import { Routes as DomRoutes, Route, useNavigate, Navigate } from 'react-router-dom';
import { Fade } from '@mui/material';
import { type UseAppStoreType, AppContext, Page } from '../contexts/AppContext';
import useLegacyMode from '../hooks/useLegacyMode';

import { RobotPage, GaragePage, MakerPage, BookPage, OrderPage, SettingsPage } from '.';
import { GarageContext, type UseGarageStoreType } from '../contexts/GarageContext';

const Routes: React.FC = () => {
  const navigate = useNavigate();
  const { garage } = useContext<UseGarageStoreType>(GarageContext);
  const { page, navigateToPage } = useContext<UseAppStoreType>(AppContext);

  const { isLegacyMode } = useLegacyMode();

  // 0.9.7: an order notification tapped on Android: open that order with its robot. The app
  // keeps it until the page takes it (cold start: once the garage is read; app already open:
  // the app calls onNavigationIntent).
  const openFromNotification = (): void => {
    const raw = window.AndroidAppRobosats?.takePendingNavigation?.() ?? '';
    if (!raw) return;
    let nav: { path?: string; robot?: string } = {};
    try {
      nav = JSON.parse(raw) as { path?: string; robot?: string };
    } catch {
      return;
    }
    const [coordinator, orderId] = (nav.path ?? '').split('/');
    if (!coordinator || !orderId) return;
    const byRobot = Object.values(garage.slots).find(
      (s) => nav.robot && s.nostrPubKey === nav.robot,
    );
    const slot = byRobot ?? garage.getSlotByOrder(coordinator, parseInt(orderId, 10));
    if (slot?.token) garage.setCurrentSlot(slot.token);
    console.info(
      `notification: open order/${coordinator}/${orderId}${slot ? ' with its robot' : ''}`,
    );
    navigateToPage(`order/${coordinator}/${orderId}`, navigate);
  };

  useEffect(() => {
    if (window.AndroidRobosats) window.AndroidRobosats.onNavigationIntent = openFromNotification;
    return () => {
      if (window.AndroidRobosats) window.AndroidRobosats.onNavigationIntent = undefined;
    };
  });

  useEffect(() => {
    // the garage is read: a notification that started the app can be opened now
    if (garage.currentSlot) openFromNotification();
  }, [garage.currentSlot]);

  useEffect(() => {
    // change tab (page) into the current route
    const pathPage: Page | string = location.pathname.split('/')[1];
    if (pathPage === 'index.html') {
      navigateToPage('garage', navigate);
    }
  }, [location]);

  const GarageComponent = garage.mode === 'garageKey' ? GaragePage : RobotPage;

  return (
    <DomRoutes>
      {['/garage/:token?', '/garage', '/', ''].map((path, index) => {
        return (
          <Route
            path={path}
            element={
              <Fade in={page === 'garage'} appear>
                <div>
                  <GarageComponent />
                </div>
              </Fade>
            }
            key={index}
          />
        );
      })}

      <Route
        path={'/offers'}
        element={
          isLegacyMode ? (
            <Navigate to='/garage' replace />
          ) : (
            <Fade in={page === 'offers'} appear>
              <div>
                <BookPage />
              </div>
            </Fade>
          )
        }
      />

      <Route
        path='/create'
        element={
          isLegacyMode ? (
            <Navigate to='/garage' replace />
          ) : (
            <Fade in={page === 'create'} appear>
              <div>
                <MakerPage />
              </div>
            </Fade>
          )
        }
      />

      <Route
        path='/order/:shortAlias/:orderId'
        element={
          <Fade in={page === 'order'} appear>
            <div>
              <OrderPage />
            </div>
          </Fade>
        }
      />

      <Route
        path='/settings'
        element={
          <Fade in={page === 'settings'} appear>
            <div>
              <SettingsPage />
            </div>
          </Fade>
        }
      />
    </DomRoutes>
  );
};

export default Routes;
