import { apiClient } from '../services/api';
import type Federation from './Federation.model';
import type Slot from './Slot.model';

export interface SubmitActionProps {
  action:
    | 'cancel'
    | 'dispute'
    | 'pause'
    | 'confirm'
    | 'undo_confirm'
    | 'update_invoice'
    | 'update_address'
    | 'submit_statement'
    | 'rate_platform'
    | 'take'
    | 'bond_key'
    | 'bond_penalty_signature'
    | 'bond_return'
    | 'bond_return_signature'
    | 'bond_leftover_return'
    | 'bond_leftover_signatures'
    | 'escrow_keys'
    | 'escrow_address'
    | 'escrow_funding_signatures'
    | 'escrow_rescue_signature'
    | 'escrow_nonce'
    | 'escrow_partial'
    | 'escrow_leaf_signature'
    | 'escrow_sweep'
    | 'escrow_sweep_signatures';
  escrow?: object;
  invoice?: string;
  routing_budget_ppm?: number;
  address?: string;
  mining_fee_rate?: number;
  statement?: string;
  rating?: number;
  amount?: number;
  cancel_status?: number;
  password?: string;
  take_lightning?: boolean;
}

export interface TradeRobotSummary {
  is_buyer: boolean;
  sent_fiat: number;
  received_sats: number;
  is_swap: boolean;
  received_onchain_sats: number;
  mining_fee_sats: number;
  swap_fee_sats: number;
  swap_fee_percent: number;
  sent_sats: number;
  received_fiat: number;
  trade_fee_sats: number;
  trade_fee_percent: number;
  bond_size_sats: number;
  bond_size_percent: number;
  payment_hash?: string;
  preimage?: string;
  address?: string;
  txid?: string;
}

export interface TradeCoordinatorSummary {
  contract_timestamp: Date;
  contract_total_time: number;
  contract_exchange_rate: number;
  routing_budget_sats: number;
  trade_revenue_sats: number;
}

class Order {
  constructor(attributes: object) {
    Object.assign(this, attributes);
  }

  id: number = 0;
  status: number = 0;
  created_at: Date = new Date();
  expires_at: Date = new Date();
  type: number = 0;
  currency: number = 0;
  amount: number | null = null;
  has_range: boolean = false;
  min_amount: number = 0;
  max_amount: number = 0;
  payment_method: string = '';
  is_explicit: boolean = false;
  premium: number = 0;
  // Roblosats rl31-beta17: fixed price per BTC in the order's currency (null = market + premium)
  fixed_price: number | string | null = null;
  // Roblosats rl31-beta19: "丰 sat" order. The coordinator sends the amounts in sats plus
  // exchange_* in the currency; update() keeps the sats in sats_* and puts what is paid in
  // exchange in amount/min_amount/max_amount, the same shape as the book, so everything that
  // reads amount (rate, Spamcoin note, fees) works in the currency as for any other order.
  sats_denominated: boolean = false;
  sats_amount: number | null = null;
  sats_min_amount: number | null = null;
  sats_max_amount: number | null = null;
  satoshis: number = 0;
  maker: number = 0;
  taker: number = 0;
  escrow_duration: number = 0;
  total_secs_exp: number = 0;
  penalty: Date | undefined = undefined;
  is_maker: boolean = false;
  is_taker: boolean = false;
  is_participant: boolean = false;
  has_password: boolean = false;
  maker_status: 'Active' | 'Seen recently' | 'Inactive' = 'Active';
  taker_status: 'Active' | 'Seen recently' | 'Inactive' = 'Active';
  price_now: number | undefined = undefined;
  satoshis_now: number = 0;
  latitude: number = 0;
  longitude: number = 0;
  password: string | undefined = undefined;
  description: string | undefined = undefined;
  premium_now: number | null | undefined = undefined; // null: fixed price without a market price
  tg_enabled: boolean = false; // deprecated
  tg_token: string = '';
  tg_bot_name: string = '';
  is_buyer: boolean = false;
  is_seller: boolean = false;
  maker_nick: string = '';
  maker_hash_id: string = '';
  maker_nostr_pubkey: string = '';
  taker_nick: string = '';
  taker_hash_id: string = '';
  taker_nostr_pubkey: string = '';
  status_message: string = '';
  is_fiat_sent: boolean = false;
  is_disputed: boolean = false;
  ur_nick: string = '';
  maker_locked: boolean = false;
  taker_locked: boolean = false;
  escrow_locked: boolean = false;
  trade_satoshis: number = 0;
  spamcoin_sats: number | null = null;
  bond_invoice: string = '';
  bond_satoshis: number = 0;
  escrow_invoice: string = '';
  escrow_satoshis: number = 0;
  invoice_amount: number = 0;
  swap_allowed: boolean = false;
  swap_failure_reason: string = '';
  suggested_mining_fee_rate: number = 0;
  swap_fee_rate: number = 0;
  pending_cancel: boolean = false;
  asked_for_cancel: boolean = false;
  statement_submitted: boolean = false;
  retries: number = 0;
  next_retry_time: Date = new Date();
  failure_reason: string = '';
  invoice_expired: boolean = false;
  onchain_rescue: boolean = false;
  public_duration: number = 0;
  bond_size: string = '';
  trade_fee_percent: number = 0;
  bond_size_sats: number = 0;
  bond_size_percent: number = 0;
  chat_last_index: number = 0;
  maker_summary: TradeRobotSummary = {
    is_buyer: false,
    sent_fiat: 0,
    received_sats: 0,
    is_swap: false,
    received_onchain_sats: 0,
    mining_fee_sats: 0,
    swap_fee_sats: 0,
    swap_fee_percent: 0,
    sent_sats: 0,
    received_fiat: 0,
    trade_fee_sats: 0,
    trade_fee_percent: 0,
    bond_size_sats: 0,
    bond_size_percent: 0,
  };

  taker_summary: TradeRobotSummary = {
    is_buyer: false,
    sent_fiat: 0,
    received_sats: 0,
    is_swap: false,
    received_onchain_sats: 0,
    mining_fee_sats: 0,
    swap_fee_sats: 0,
    swap_fee_percent: 0,
    sent_sats: 0,
    received_fiat: 0,
    trade_fee_sats: 0,
    trade_fee_percent: 0,
    bond_size_sats: 0,
    bond_size_percent: 0,
  };

  platform_summary: TradeCoordinatorSummary = {
    contract_timestamp: new Date(),
    contract_total_time: 0,
    contract_exchange_rate: 0,
    routing_budget_sats: 0,
    trade_revenue_sats: 0,
  };

  expiry_reason: number = 0;
  expiry_message: string = '';
  num_satoshis: number = 0;
  sent_satoshis: number = 0;
  txid: string = '';
  tx_queued: boolean = false;
  address: string = '';
  network: 'mainnet' | 'testnet' = 'mainnet';
  shortAlias: string = '';
  escrow_type?: number | string;
  bad_request?: string = '';
  bad_address?: string = '';
  bad_invoice?: string = '';
  bad_statement?: string = '';

  /** Keys the last full order page from the coordinator brought (rl31-beta20). */
  private serverKeys: string[] = [];

  update = (attributes: object): Order => {
    const a = attributes as Record<string, unknown>;
    // A full order page replaces the previous one: a key the coordinator stopped sending (a
    // leftover already sent back, a return already signed...) goes away instead of staying with
    // its old value (18 nov 2026: "Recover your sats" stayed after the leftover went back).
    // Only keys the coordinator sent before are dropped, never what the app set itself.
    if (typeof a.id === 'number' && 'status' in a && !a.bad_request) {
      const self = this as unknown as Record<string, unknown>;
      for (const k of this.serverKeys) {
        if (!(k in a) && /^(bond_|escrow_|onchain_)/.test(k)) delete self[k];
      }
      this.serverKeys = Object.keys(a);
    }
    Object.assign(this, attributes);
    if (
      a.sats_denominated === true &&
      ('exchange_amount' in a || 'exchange_min_amount' in a || 'exchange_max_amount' in a)
    ) {
      const num = (v: unknown): number | null =>
        v === null || v === undefined || v === '' ? null : Number(v);
      this.sats_amount = num(a.amount);
      this.sats_min_amount = num(a.min_amount);
      this.sats_max_amount = num(a.max_amount);
      this.amount = num(a.exchange_amount);
      this.min_amount = num(a.exchange_min_amount) ?? 0;
      this.max_amount = num(a.exchange_max_amount) ?? 0;
    }
    return this;
  };

  /** A good reply from the coordinator drops an old error: Object.assign alone never removes
   * it, and the order stayed stuck on it (16 nov 2026: "You cannot confirm the fiat payment
   * at this stage" after a retried confirm, while the order itself was fine). */
  private updateFromReply = (data: object): void => {
    if (!(data as { bad_request?: string }).bad_request) this.bad_request = '';
    this.update(data);
  };

  make: (federation: Federation, slot: Slot) => Promise<this> = async (federation, slot) => {
    const body = {
      type: this.type,
      currency: this.currency,
      amount: this.has_range
        ? null
        : this.sats_denominated
          ? (this.sats_amount ?? this.amount)
          : this.amount,
      has_range: this.has_range,
      min_amount: this.sats_denominated
        ? (this.sats_min_amount ?? this.min_amount)
        : this.min_amount,
      max_amount: this.sats_denominated
        ? (this.sats_max_amount ?? this.max_amount)
        : this.max_amount,
      sats_denominated: this.sats_denominated,
      payment_method: this.payment_method,
      is_explicit: this.is_explicit,
      premium: this.is_explicit ? null : this.premium,
      satoshis: this.is_explicit ? this.satoshis : null,
      fixed_price: this.fixed_price ?? null,
      public_duration: this.public_duration,
      escrow_duration: this.escrow_duration,
      bond_size: this.bond_size,
      latitude: this.latitude,
      longitude: this.longitude,
      password: this.password,
      description: this.description,
      escrow_type: this.escrow_type,
    };

    if (slot) {
      const coordinator = federation.getCoordinator(this.shortAlias);
      if (!coordinator || !coordinator.url) return this;
      const authHeaders = slot.getRobot()?.getAuthHeaders();
      if (!authHeaders) return this;
      const data = await apiClient
        .post(coordinator.url, '/api/make/', body, authHeaders)
        .catch((e) => {
          console.log(e);
        });
      if (data) this.update(data);
    }

    return this;
  };

  take: (
    federation: Federation,
    slot: Slot,
    takeAmount: string,
    lightning?: boolean,
  ) => Promise<this> = async (federation, slot, takeAmount, lightning = false) => {
    return await this.submitAction(federation, slot, {
      action: 'take',
      password: this?.password,
      amount: this.sats_denominated
        ? Math.round(Number(takeAmount))
        : this?.currency === 1000
          ? Number(takeAmount) / 100000000
          : Number(takeAmount),
      // Roblosats hybrid: take an onchain order with Lightning
      ...(lightning ? { take_lightning: true } : {}),
    });
  };

  submitAction: (federation: Federation, slot: Slot, action: SubmitActionProps) => Promise<this> =
    async (federation, slot, action) => {
      if (this.id < 1) return this;

      if (slot) {
        const coordinator = federation.getCoordinator(this.shortAlias);
        if (!coordinator || !coordinator.url) return this;
        const data = await apiClient
          .post(coordinator.url, `/api/order/?order_id=${Number(this.id)}`, action, {
            tokenSHA256: slot?.getRobot()?.tokenSHA256 ?? '',
          })
          .catch((e) => {
            console.log(e);
          });
        if (data) this.updateFromReply(data);
      }
      return this;
    };

  fecth: (federation: Federation, slot: Slot) => Promise<this> = async (federation, slot) => {
    if (this.id < 1) return this;
    if (!slot) return this;

    const coordinator = federation.getCoordinator(this.shortAlias);
    if (!coordinator || !coordinator.url) return this;
    // Roblosats: the robot of this order's coordinator, not whatever robot comes first.
    const authHeaders = (slot.getRobot(this.shortAlias) ?? slot.getRobot())?.getAuthHeaders();
    if (!authHeaders) return this;
    const data = await apiClient
      .get(coordinator.url, `/api/order/?order_id=${this.id}`, authHeaders)
      .catch((e) => {
        console.log(e);
      });

    if (data) this.updateFromReply(data);

    return this;
  };
}

export default Order;
