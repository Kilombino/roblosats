import { sha256 } from 'js-sha256';
import { sha256 as sha256Hash, sha512 } from '@noble/hashes/sha2.js';
import Robot from './Robot.model';
import Order from './Order.model';
import type Federation from './Federation.model';
import { roboidentitiesClient } from '../services/Roboidentities/Web';
import hexToBase91 from '../utils/hexToBase91';
import { validateTokenEntropy } from '../utils/token';
import { getPublicKey } from 'nostr-tools';

/** Roblosats onchain escrow: the steps inside one order status (bond seen, deposit built,
 * signed, release nonces…) must repaint the order too, not only a change of status. */
const ONCHAIN_KEYS = [
  'escrow_status_code',
  'bond_status_code',
  'escrow_deposit_tx',
  'escrow_deposit_signed',
  'escrow_release_tx',
  'escrow_release_aggnonce',
  'escrow_release_my_nonce',
  'escrow_release_my_partial',
  'escrow_release_leaf_ok',
  'escrow_release_txid',
  'escrow_funding_txid',
  'escrow_waiting_for',
  'escrow_waiting_what',
  'escrow_my_payout_address',
  'bond_mempool_txid',
  'escrow_funding_mempool_txid',
  'bond_return_tx',
];
const onchainSignature = (order: Order): string => {
  const o = order as unknown as Record<string, unknown>;
  if (o.escrow_type !== 'onchain' && o.escrow_type !== 1) return '';
  return JSON.stringify(ONCHAIN_KEYS.map((k) => o[k] ?? null));
};

/** Statuses after which the app stops following an order (it becomes the slot's last order). */
export const FINISHED_STATUSES = [4, 5, 12, 14, 17, 18];

/** 0.9.10 (#26): an onchain order that already finished (collaborative cancel, success, dispute
 * resolved) may still have a step on chain: the escrow release in the mempool or being signed
 * together, or a bond signature this app must make. Keep following it as the active order (at
 * the normal pace) until that clears; then it is finished like before. */
export const onchainStillPending = (order: Order | null | undefined): boolean => {
  if (!order) return false;
  const o = order as unknown as Record<string, unknown>;
  if (o.escrow_type !== 'onchain' && o.escrow_type !== 1) return false;
  if (![12, 14, 17, 18].includes(order.status)) return false;
  return o.escrow_status_code === 2 || o.escrow_status_code === 4 || o.bond_status_code === 1;
};

/** Finished for good: nothing left to follow. */
export const orderFinished = (order: Order): boolean =>
  FINISHED_STATUSES.includes(order.status) && !onchainStillPending(order);

class Slot {
  constructor(
    token: string,
    shortAliases: string[],
    robotAttributes: object,
    onSlotUpdate: () => void,
  ) {
    this.onSlotUpdate = onSlotUpdate;
    this.token = token;

    this.hashId = sha256(sha256(this.token));
    this.nickname = null;
    void roboidentitiesClient.generateRoboname(this.hashId).then((nickname) => {
      this.nickname = nickname;
      onSlotUpdate();
    });
    void roboidentitiesClient.generateRobohash(this.hashId, 'small');
    void roboidentitiesClient.generateRobohash(this.hashId, 'large');

    const { hasEnoughEntropy, bitsEntropy, shannonEntropy } = validateTokenEntropy(token);

    const tokenSHA256 = hexToBase91(sha256(token));

    const tokenBytes = new TextEncoder().encode(this.token ?? '');
    const nostrSecKey = sha256Hash(sha512(tokenBytes));
    this.nostrSecKey = nostrSecKey;
    const nostrPubKey = getPublicKey(this.nostrSecKey);
    this.nostrPubKey = nostrPubKey;

    this.robots = shortAliases.reduce((acc: Record<string, Robot>, shortAlias: string) => {
      acc[shortAlias] = new Robot({
        ...robotAttributes,
        token,
        shortAlias,
        hasEnoughEntropy,
        bitsEntropy,
        shannonEntropy,
        tokenSHA256,
        nostrPubKey,
      });
      this.updateSlotFromRobot(acc[shortAlias]);
      return acc;
    }, {});

    this.loading = true;

    this.onSlotUpdate();
  }

  token: string | null;
  hashId: string | null;
  nickname: string | null;
  robots: Record<string, Robot>;
  activeOrder: Order | null = null;
  lastOrder: Order | null = null;
  lastOrderStatusKnown: boolean = false;
  nostrSecKey?: Uint8Array;
  nostrPubKey?: string;
  availableRewards: string | null = null;
  loading: boolean;

  onSlotUpdate: () => void;

  // Robots
  getRobot = (shortAlias?: string): Robot | null => {
    if (shortAlias) {
      return this.robots[shortAlias];
    } else if (this.activeOrder?.id) {
      return this.robots[this.activeOrder.shortAlias];
    } else if (this.lastOrder?.id && this.robots[this.lastOrder.shortAlias]) {
      return this.robots[this.lastOrder.shortAlias];
    } else if (Object.values(this.robots).length > 0) {
      return Object.values(this.robots)[0];
    }
    return null;
  };

  fetchRobot = async (federation: Federation): Promise<void> => {
    this.loading = true;

    await Promise.all(
      Object.values(this.robots).map(async (robot) => {
        const fetchedRobot = await robot.fetch(federation);
        this.loading = Object.values(this.robots).some((r) => r.loading);
        this.updateSlotFromRobot(fetchedRobot);
      }),
    );
    this.loading = Object.values(this.robots).some((r) => r.loading);
    this.onSlotUpdate();
  };

  updateSlotFromRobot = (robot: Robot | null): void => {
    if (!robot) return;

    let changed = false;

    if (robot.activeOrderId && this.activeOrder?.id !== robot.activeOrderId) {
      this.activeOrder = new Order({
        id: robot.activeOrderId,
        shortAlias: robot.shortAlias,
      });
      changed = true;
    } else if (
      !robot.activeOrderId &&
      this.activeOrder?.shortAlias === robot.shortAlias &&
      this.lastOrder?.id !== this.activeOrder.id &&
      // #26: finished for the coordinator, but its release is still on its way: keep it
      !(robot.lastOrderId === this.activeOrder.id && onchainStillPending(this.activeOrder))
    ) {
      this.activeOrder = null;
      changed = true;
    }

    if (robot.lastOrderId && this.lastOrder?.id !== robot.lastOrderId) {
      if (robot.lastOrderId === robot.activeOrderId) {
        // The robot reports the same id for both last and active — it is still
        // active, so leave activeOrder in place and skip updating lastOrder.
      } else if (
        this.activeOrder?.id === robot.lastOrderId &&
        this.activeOrder?.shortAlias === robot.shortAlias &&
        onchainStillPending(this.activeOrder)
      ) {
        // #26: still following it until its onchain step clears (updateSlotFromOrder parks it).
      } else if (this.activeOrder?.id === robot.lastOrderId) {
        // The previously-active order is now the last order: preserve the full
        // object so that status details are not lost.
        this.lastOrder = this.activeOrder;
        this.lastOrderStatusKnown = this.hasOrderDetails(this.lastOrder);
        this.activeOrder = null;
        changed = true;
      } else {
        // New last order with minimal data, status must be resolved before reusability checks.
        this.lastOrder = new Order({ id: robot.lastOrderId, shortAlias: robot.shortAlias });
        this.lastOrderStatusKnown = false;
        changed = true;
      }
    }

    const previousRewards = this.availableRewards;
    this.availableRewards =
      robot.earnedRewards != undefined && robot.earnedRewards > 0
        ? robot.shortAlias
        : this.availableRewards === robot.shortAlias
          ? null
          : this.availableRewards;
    if (this.availableRewards !== previousRewards) {
      changed = true;
    }

    if (changed) {
      this.onSlotUpdate();
    }
  };

  // Orders
  fetchActiveOrder = async (federation: Federation): Promise<void> => {
    if (this.activeOrder) {
      const order = this.activeOrder;
      const previousStatus = order.status;
      const previousOnchain = onchainSignature(order);
      await order.fecth(federation, this);
      this.loading = false;
      if (this.activeOrder !== order) return;
      this.updateSlotFromOrder(order);
      if (
        this.activeOrder &&
        (order.status !== previousStatus || onchainSignature(order) !== previousOnchain)
      )
        this.onSlotUpdate();
    }
  };

  takeOrder = async (
    federation: Federation,
    order: Order,
    takeAmount: string,
    lightning = false,
    onchain = false,
  ): Promise<Order> => {
    await order.take(federation, this, takeAmount, lightning, onchain);
    this.updateSlotFromOrder(order);
    return order;
  };

  makeOrder = async (federation: Federation, attributes: object): Promise<Order> => {
    const order = new Order(attributes);
    await order.make(federation, this);
    if (order.id > 0 && !order.bad_request) {
      if (this.activeOrder) {
        this.lastOrder = this.activeOrder;
        this.lastOrderStatusKnown = this.hasOrderDetails(this.lastOrder);
      }
      this.activeOrder = order;
      this.onSlotUpdate();
    }
    return order;
  };

  private resolveBadRequestStatus = (order: Order): boolean => {
    if (order.bad_request?.includes('expired')) {
      order.status = 5;
      return true;
    }
    if (order.bad_request?.includes('collaborativelly')) {
      order.status = 12;
      return true;
    }
    return false;
  };

  updateSlotFromOrder: (newOrder: Order | null) => void = (newOrder) => {
    if (newOrder) {
      this.resolveBadRequestStatus(newOrder);
      if (
        newOrder.id === this.activeOrder?.id &&
        newOrder.shortAlias === this.activeOrder?.shortAlias
      ) {
        const previousStatus = this.activeOrder?.status;
        const previousBadRequest = this.activeOrder?.bad_request;
        this.activeOrder?.update(newOrder);
        const changed =
          this.activeOrder?.status !== previousStatus ||
          this.activeOrder?.bad_request !== previousBadRequest;
        if (
          this.activeOrder?.bad_request ||
          orderFinished(this.activeOrder) ||
          // 0.9.8: lost the race for this order and its bond is settled: nothing left to follow
          this.activeOrder?.lost_take_done === true
        ) {
          this.lastOrder = this.activeOrder;
          this.lastOrderStatusKnown = this.hasOrderDetails(this.lastOrder);
          this.activeOrder = null;
        }
        if (changed || this.activeOrder === null) {
          this.onSlotUpdate();
        }
      } else if (
        newOrder?.is_participant &&
        !newOrder.bad_request &&
        this.lastOrder?.id === newOrder.id &&
        this.lastOrder?.shortAlias === newOrder.shortAlias &&
        !orderFinished(newOrder)
      ) {
        // Parked as the last order by an error that is gone now and still running: follow it
        // again as the active order (otherwise nobody refreshes it any more).
        this.activeOrder = newOrder;
        this.onSlotUpdate();
      } else if (
        newOrder?.is_participant &&
        (this.lastOrder?.id !== newOrder.id || this.lastOrder?.shortAlias !== newOrder.shortAlias)
      ) {
        if (orderFinished(newOrder)) {
          this.lastOrder = newOrder;
          this.lastOrderStatusKnown = this.hasOrderDetails(newOrder);
        } else {
          this.activeOrder = newOrder;
        }
        this.onSlotUpdate();
      }
    }
  };

  private hasOrderDetails = (order: Order | null): boolean => {
    if (!order) return false;

    return (
      order.maker > 0 ||
      order.taker > 0 ||
      order.payment_method !== '' ||
      order.maker_nick !== '' ||
      order.status_message !== '' ||
      order.bond_size !== '' ||
      Boolean(order.bad_request)
    );
  };

  ensureLastOrderStatus = async (federation: Federation): Promise<void> => {
    if (
      !this.lastOrder ||
      this.lastOrderStatusKnown ||
      this.activeOrder?.id === this.lastOrder.id
    ) {
      return;
    }

    await this.lastOrder.fecth(federation, this);
    const statusResolved =
      !this.lastOrder.bad_request || this.resolveBadRequestStatus(this.lastOrder);
    this.lastOrderStatusKnown = this.hasOrderDetails(this.lastOrder) && statusResolved;
    // #26: its onchain release is still on its way (app restarted meanwhile): follow it again
    if (!this.activeOrder && !this.lastOrder.bad_request && onchainStillPending(this.lastOrder)) {
      this.activeOrder = this.lastOrder;
    }
    this.onSlotUpdate();
  };

  // 0.9.17 signed actions: the robot of this coordinator (made from the default one if the slot
  // has none yet), so a request is signed with the key that coordinator knows.
  ensureRobot: (federation: Federation, shortAlias: string) => Robot | null = (
    federation,
    shortAlias,
  ) => {
    if (!this.robots[shortAlias]) this.syncCoordinator(federation, shortAlias);
    return this.robots[shortAlias] ?? null;
  };

  syncCoordinator: (federation: Federation, shortAlias: string) => void = (
    federation,
    shortAlias,
  ) => {
    const defaultRobot = this.getRobot();
    if (defaultRobot?.token) {
      this.robots[shortAlias] = new Robot({
        shortAlias,
        hasEnoughEntropy: defaultRobot.hasEnoughEntropy,
        bitsEntropy: defaultRobot.bitsEntropy,
        shannonEntropy: defaultRobot.shannonEntropy,
        token: defaultRobot.token,
        pubKey: defaultRobot.pubKey,
        encPrivKey: defaultRobot.encPrivKey,
        nostrPubKey: defaultRobot.nostrPubKey,
      });
      // Roblosats: repaint once it answers (its onchain escrow permission arrives with it;
      // 17 nov 2026 the phone's maker form never showed the onchain switch).
      void this.robots[shortAlias].fetch(federation).then((robot) => {
        this.updateSlotFromRobot(robot);
        this.onSlotUpdate();
      });
      this.updateSlotFromRobot(this.robots[shortAlias]);
    }
  };

  isReusable = (): boolean => {
    if (this.loading) return false;

    if (this.activeOrder) return false;

    // Roblosats: a robot the coordinator lets into the onchain escrow beta stays usable for
    // more orders (a new robot would not be on its list and would lose the onchain option).
    if (Object.values(this.robots).some((robot) => robot?.onchainEscrow)) return true;

    if (!this.lastOrder) {
      return true;
    }

    if (!this.lastOrderStatusKnown) {
      return false;
    }

    const reusableStatuses = [4, 5];

    return reusableStatuses.includes(this.lastOrder.status);
  };
}

export default Slot;
