import React, {
  createContext,
  type Dispatch,
  useState,
  type SetStateAction,
  useEffect,
  type ReactNode,
  useContext,
  useRef,
} from 'react';

import { defaultMaker, type Maker, Garage } from '../models';
import { activeOrderDelay, defaultDelay } from './orderRefreshDelay';
import { type UseAppStoreType, AppContext } from './AppContext';
import { type UseFederationStoreType, FederationContext } from './FederationContext';

export interface GarageContextProviderProps {
  children: ReactNode;
}

export interface UseGarageStoreType {
  garage: Garage;
  maker: Maker;
  setMaker: Dispatch<SetStateAction<Maker>>;
  setDelay: Dispatch<SetStateAction<number>>;
  fetchSlotActiveOrder: () => void;
  recoverAccountFromRelays: () => Promise<void>;
}

export const initialGarageContext: UseGarageStoreType = {
  garage: new Garage(),
  maker: defaultMaker,
  setMaker: () => {},
  setDelay: () => {},
  fetchSlotActiveOrder: () => {},
  recoverAccountFromRelays: async () => {},
};

export const GarageContext = createContext<UseGarageStoreType>(initialGarageContext);

export const GarageContextProvider = ({
  children,
}: GarageContextProviderProps): React.JSX.Element => {
  // All garage data structured
  const { settings, torStatus, page, setSlotUpdatedAt } = useContext<UseAppStoreType>(AppContext);
  const pageRef = useRef(page);
  const { federation } = useContext<UseFederationStoreType>(FederationContext);
  const [garage] = useState<Garage>(initialGarageContext.garage);
  const [maker, setMaker] = useState<Maker>(initialGarageContext.maker);
  const [lastOrderCheckAt] = useState<number>(+new Date());
  const lastOrderCheckAtRef = useRef(lastOrderCheckAt);
  const [delay, setDelay] = useState<number>(defaultDelay);
  const [timer, setTimer] = useState<ReturnType<typeof setTimeout> | undefined>(() =>
    setInterval(() => null, delay),
  );

  const onSlotUpdated = (): void => {
    setSlotUpdatedAt(new Date().toISOString());
  };

  const recoverAccountFromRelays = async (): Promise<void> => {
    await garage.getGarageKey()?.recoverAccount(federation.roboPool);
  };

  useEffect(() => {
    setMaker((maker) => {
      return { ...maker, coordinator: federation.getCoordinatorsAlias()[0] };
    }); // default MakerForm coordinator is decided via sorted lottery
    garage.registerHook('onSlotUpdate', onSlotUpdated);
    clearInterval(timer);
    fetchSlotActiveOrder();

    // Wait for garage mode, key and slots before any auto-switch evaluation.
    void Promise.all([garage.loadGarageKey(), garage.loadMode(), garage.waitForSlotsLoaded()]).then(
      async () => {
        // Roblosats (17 nov 2026): always load the current robot once the garage is read.
        // The page effect below only fetches it when the page changes, and the slots are not
        // loaded yet on the first run: opening /order/... directly left the robot unloaded
        // forever (no /api/robot, no /api/order, endless spinner) since robots stopped being
        // rotated at start-up.
        const slot = garage.getSlot();
        if (slot?.token) {
          // A slot saved before a coordinator joined has no robot for it: add it.
          for (const alias of federation.getCoordinatorsAlias()) {
            if (!slot.robots[alias]) slot.syncCoordinator(federation, alias);
          }
          await garage.fetchRobot(federation, slot.token).catch(() => {});
        }
        if (garage.getMode() === 'garageKey' && garage.getGarageKey()) {
          garage.resetManualNavigation();
          void garage.ensureReusableSlot(federation, { source: 'auto' });
        }
      },
    );

    return () => {
      clearTimeout(timer);
    };
  }, []);

  useEffect(() => {
    const token = garage.getSlot()?.token;

    if (pageRef.current !== page) {
      pageRef.current = page;
      if (token && page === 'garage') {
        void garage.fetchRobot(federation, token).then(() => {
          if (garage.getMode() === 'garageKey' && garage.getGarageKey()) {
            void garage.ensureReusableSlot(federation, { source: 'auto' });
          }
        });
      }
    } else if (token) {
      void garage.fetchRobot(federation, token);
    }
  }, [settings.network, settings.useProxy, torStatus, page]);

  const fetchSlotActiveOrder: () => void = () => {
    const slot = garage?.getSlot();
    if (slot?.activeOrder?.id) {
      let delay = activeOrderDelay(slot.activeOrder);
      if (pageRef.current !== 'order') delay = delay * 5;
      // 0.9.6: the app in the background (or a hidden tab) asks much less often; the Android
      // notification service tells about new steps meanwhile
      if (typeof document !== 'undefined' && document.hidden) delay = delay * 10;
      if (+new Date() - lastOrderCheckAtRef.current >= delay) {
        void slot.fetchActiveOrder(federation).finally(() => {
          const order = slot.activeOrder ?? slot.lastOrder;
          if (order && [14, 17, 18].includes(order.status)) {
            garage.resetManualNavigation();
          }

          if (
            pageRef.current === 'garage' &&
            garage.getMode() === 'garageKey' &&
            garage.getGarageKey()
          ) {
            void garage.ensureReusableSlot(federation, { source: 'auto' });
          }
          lastOrderCheckAtRef.current = +new Date();
          resetInterval();
        });
      } else {
        resetInterval();
      }
    } else {
      resetInterval();
    }
  };

  const resetInterval = (): void => {
    clearInterval(timer);
    setDelay(defaultDelay);
    setTimer(
      setTimeout(() => {
        fetchSlotActiveOrder();
      }, defaultDelay),
    );
  };

  return (
    <GarageContext.Provider
      value={{
        garage,
        maker,
        setMaker,
        setDelay,
        fetchSlotActiveOrder,
        recoverAccountFromRelays,
      }}
    >
      {children}
    </GarageContext.Provider>
  );
};
