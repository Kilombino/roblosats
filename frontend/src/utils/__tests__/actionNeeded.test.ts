import { actionNeeded } from '../actionNeeded';

const seller = { is_seller: true, is_buyer: false };
const buyer = { is_seller: false, is_buyer: true };

describe('actionNeeded (0.9.4)', () => {
  it('asks for the bonds', () => {
    expect(actionNeeded({ status: 0, is_maker: true })).toBe('Lock your bond to publish the order');
    expect(actionNeeded({ status: 1, is_maker: true })).toBeNull();
    expect(actionNeeded({ status: 1, is_maker: false })).toBeNull();
    expect(actionNeeded({ status: 1, is_maker: false, bond_invoice: 'lnbc' })).toBe(
      'Lock your bond to take the order',
    );
  });

  it('Lightning escrow and payout', () => {
    expect(actionNeeded({ status: 6, ...seller })).toBe('Lock the trade escrow');
    expect(actionNeeded({ status: 6, ...buyer })).toBe('Send your payout invoice');
    expect(actionNeeded({ status: 7, ...buyer })).toBeNull();
    expect(actionNeeded({ status: 8, ...seller })).toBeNull();
    expect(actionNeeded({ status: 8, ...buyer })).toBe('Send your payout invoice');
  });

  it('chat, fiat and dispute', () => {
    expect(actionNeeded({ status: 9, ...buyer })).toBe('Send the payment and press "Confirm sent"');
    expect(actionNeeded({ status: 9, ...seller })).toBeNull();
    expect(actionNeeded({ status: 10, ...seller })).toBe(
      'Check your account: confirm only when the money has arrived',
    );
    expect(actionNeeded({ status: 10, ...buyer })).toBeNull();
    expect(actionNeeded({ status: 9, ...seller, pending_cancel: true })).toBe(
      'Your counterpart asked to cancel by agreement: accept it or keep trading',
    );
    expect(actionNeeded({ status: 11, statement_submitted: false })).toBe(
      'Send your dispute statement',
    );
    expect(actionNeeded({ status: 11, statement_submitted: true })).toBeNull();
    expect(actionNeeded({ status: 15, ...buyer })).toBe('Send a new invoice');
  });

  it('onchain steps follow the escrow', () => {
    const oc = { escrow_type: 'onchain' };
    expect(
      actionNeeded({
        status: 6,
        ...oc,
        ...buyer,
        escrow_waiting_for: 'you',
        escrow_waiting_what: 'address',
      }),
    ).toBe('Give the address that will receive the sats');
    expect(
      actionNeeded({
        status: 7,
        ...oc,
        ...seller,
        escrow_status_code: 0,
        escrow_waiting_for: 'you',
        escrow_waiting_what: 'funding',
      }),
    ).toBe('Fund the onchain escrow');
    expect(
      actionNeeded({
        status: 7,
        ...oc,
        ...seller,
        escrow_status_code: 0,
        escrow_waiting_for: 'peer',
        escrow_waiting_what: 'funding',
      }),
    ).toBeNull();
    expect(
      actionNeeded({
        status: 7,
        ...oc,
        ...seller,
        escrow_status_code: 1,
        escrow_waiting_for: 'you',
        escrow_waiting_what: 'deposit_signature',
      }),
    ).toBe('Keep this order open: your app has to sign');
    expect(actionNeeded({ status: 0, ...oc, is_maker: true, bond_status_code: 1 })).toBe(
      'Keep this order open: your app has to sign',
    );
    expect(actionNeeded({ status: 14, ...oc, bond_status_code: 5 })).toBe('Get your sats back');
    expect(actionNeeded({ status: 14, ...oc, escrow_leftover_sats: 500 })).toBe(
      'Get your sats back',
    );
    expect(
      actionNeeded({ status: 14, ...oc, escrow_leftover_sats: 500, escrow_sweep_txid: 'ab' }),
    ).toBeNull();
    expect(actionNeeded({ status: 14, ...oc })).toBeNull();
  });

  it('nothing without an order', () => {
    expect(actionNeeded(null)).toBeNull();
    expect(actionNeeded(undefined)).toBeNull();
  });
});
