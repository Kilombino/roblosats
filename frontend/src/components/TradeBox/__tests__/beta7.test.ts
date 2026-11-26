import {
  feeAtReleaseKey,
  hasOnchainSteps,
  peerFundingSeen,
  tradeBoxNoOverflowSx,
  tradeBoxWidthEm,
} from '../onchainEscrowSteps';
import { type Order } from '../../../models';
import { type EscrowOrderData } from '../../../utils/escrow/client';
import en from '../../../../static/locales/en.json';
import es from '../../../../static/locales/es.json';
import ca from '../../../../static/locales/ca.json';

const dicts = [en, es, ca] as Array<Record<string, string>>;

// 0.9.14-beta7 (Kilombino TG 15645, #38)
describe('the buyer whose fee comes out of the release', () => {
  it('mixta D maker: its order is Lightning (it never took anything); hybrid A taker: took with Lightning', () => {
    expect(feeAtReleaseKey(true)).toMatch(/^Your order is Lightning/);
    expect(feeAtReleaseKey(false)).toMatch(/^You took with Lightning/);
    for (const k of [feeAtReleaseKey(true), feeAtReleaseKey(false)]) for (const d of dicts) expect(d[k]).toBeTruthy();
  });
});

describe("the other trader's funding payment in the mempool", () => {
  const tx = '78'.repeat(32);
  const data = { escrow_peer_funding_mempool_txids: [tx, 'junk'] } as unknown as EscrowOrderData;
  it('the buyer sees the seller funding, with its own txid', () => {
    const p = peerFundingSeen(data, true, 7)!;
    expect(p.txids).toEqual([tx]);
    expect(p.key).toMatch(/^The seller is funding the escrow onchain/);
    for (const d of dicts) expect(d[p.key]).toBeTruthy();
    for (const d of dicts) expect(d["Peer's payment"]).toBeTruthy();
  });
  it("the seller sees the buyer's payment; nothing outside funding or without txids", () => {
    expect(peerFundingSeen(data, false, 7)!.key).toMatch(/buyer's payment/);
    expect(peerFundingSeen(data, true, 9)).toBeNull();
    expect(peerFundingSeen({} as EscrowOrderData, true, 7)).toBeNull();
  });
});

// 0.9.14-beta7 (TG 15643): no horizontal scroll in the onchain prompts
describe('trade box width and overflow', () => {
  it('onchain prompts get up to 26em where the window has room, 21em otherwise', () => {
    expect(tradeBoxWidthEm(false, 120, true)).toBe(21);
    expect(tradeBoxWidthEm(true, 120, true)).toBe(26); // 1920 px ≈ 120em
    expect(tradeBoxWidthEm(true, 80, true)).toBe(26); // 1280 px ≈ 80em
    expect(tradeBoxWidthEm(true, 23, false)).toBe(22); // a phone: the window
    expect(tradeBoxWidthEm(true, 20, false)).toBe(21);
  });
  it('knows an order with onchain steps', () => {
    expect(hasOnchainSteps({ escrow_type: 'onchain' } as unknown as Order)).toBe(true);
    expect(hasOnchainSteps({ maker_lightning: true } as unknown as Order)).toBe(true);
    expect(hasOnchainSteps({ take_onchain: true } as unknown as Order)).toBe(true);
    expect(hasOnchainSteps({ escrow_type: 'lightning' } as unknown as Order)).toBe(false);
    expect(hasOnchainSteps(null)).toBe(false);
  });
  it('wraps buttons and long words and hides sideways overflow', () => {
    expect(tradeBoxNoOverflowSx.overflowX).toBe('hidden');
    expect(tradeBoxNoOverflowSx['& .MuiButton-root'].whiteSpace).toBe('normal');
    expect(tradeBoxNoOverflowSx['& .MuiGrid-root'].maxWidth).toBe('100%');
  });
});
