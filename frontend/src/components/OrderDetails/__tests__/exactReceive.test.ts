import { exactReceive } from '../exactReceive';
import { type Order } from '../../../models';
import en from '../../../../static/locales/en.json';
import es from '../../../../static/locales/es.json';
import ca from '../../../../static/locales/ca.json';

const order = (o: object): Order => ({ is_buyer: true, ...o }) as unknown as Order;

// 0.9.14 (Kilombino TG 15656, order #34)
describe("the buyer's exact amount once it has a payout", () => {
  it('a Lightning payout: its invoice amount (the trade minus the routing budget it chose)', () => {
    expect(
      exactReceive(order({ payout_sats: 95_820_974, payout_routing_budget_sats: 95_917, payout_layer: 'lightning' })),
    ).toEqual({ sats: 95_820_974, routingBudget: 95_917, onchain: false });
  });
  it('an onchain payout', () => {
    expect(exactReceive(order({ payout_sats: 1_000_000, payout_layer: 'onchain' }))).toEqual({
      sats: 1_000_000,
      routingBudget: 0,
      onchain: true,
    });
  });
  it('no payout yet, or the seller: approximate as before', () => {
    expect(exactReceive(order({}))).toBeNull();
    expect(exactReceive(order({ payout_sats: 0 }))).toBeNull();
    expect(exactReceive(order({ is_buyer: false, payout_sats: 5 }))).toBeNull();
  });
  it('has its texts in en, es and ca', () => {
    for (const k of ['You receive {{amount}} Sats', 'You receive onchain {{amount}} Sats', '(routing budget {{sats}} Sats)'])
      for (const d of [en, es, ca] as Array<Record<string, string>>) expect(d[k]).toBeTruthy();
  });
});
