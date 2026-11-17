import {
  individualPk,
  nonceGen,
  partialSigVerify,
  sign as musigSign,
  type Session,
} from './musig2';
import {
  BondContract,
  EscrowContract,
  Transaction,
  bytesToHex,
  fundingScript,
  hexToBytes,
  p2trAddress,
  pubkey,
  signSchnorr,
  tokenKey,
  xonly,
  type TxOut,
} from './taproot';

export interface EscrowKeys {
  escrow: Uint8Array;
  funding: Uint8Array;
  bond: Uint8Array;
}

export interface EscrowOrderData {
  id: number;
  status?: number;
  is_maker?: boolean;
  escrow_type?: string | number;
  escrow_alias?: string;
  escrow_status_code?: number;
  escrow_my_keys_set?: boolean;
  escrow_deposit_signed?: boolean;
  escrow_release_my_nonce?: string;
  escrow_release_my_partial?: boolean;
  bond_status_code?: number;
  escrow_role?: 'seller' | 'buyer';
  escrow_coordinator_pubkey?: string;
  escrow_seller_pubkey?: string;
  escrow_buyer_pubkey?: string;
  escrow_rescue_csv?: number;
  escrow_address?: string;
  escrow_hrp?: string;
  escrow_sats?: number;
  escrow_output_sats?: number;
  escrow_funding_address?: string;
  escrow_funding_owed?: number;
  escrow_funding_recommended?: number;
  escrow_deposit_tx?: string;
  escrow_deposit_spent?: Array<[number, string]>;
  escrow_deposit_my_inputs?: number[];
  escrow_deposit_my_bond_input?: number | null;
  escrow_release_tx?: string;
  escrow_release_kind?: string;
  escrow_release_aggnonce?: string;
  escrow_my_payout_address?: string;
  bond_role?: 'maker' | 'taker';
  bond_address?: string;
  bond_required_sats?: number;
  bond_refund_csv?: number;
  bond_coordinator_pubkey?: string;
  bond_hrp?: string;
  bond_txid?: string;
  bond_vout?: number;
  bond_value?: number;
  bond_penalty_tx?: string;
  bond_return_tx?: string;
  bond_spend_txid?: string;
  bond_mempool_txid?: string;
  bond_mempool_sats?: number;
  escrow_fee_from_bond?: boolean;
  escrow_fee_at_release?: boolean;
  escrow_fee_at_release_sats?: number;
  take_lightning?: boolean;
  taker_lightning?: boolean;
  hybrid_onchain_maker_bond?: boolean;
  escrow_funding_mempool_txid?: string;
  escrow_funding_mempool_sats?: number;
  escrow_funding_txid?: string;
  escrow_leftover_sats?: number;
  escrow_sweep_tx?: string;
  escrow_sweep_spent?: Array<[number, string]>;
  escrow_sweep_txid?: string;
  escrow_release_txid?: string;
  escrow_waiting_for?: '' | 'you' | 'peer' | 'confirmation' | 'coordinator';
  escrow_waiting_what?: string;
  onchain_summary?: OnchainSummary;
  escrow_release_cancel?: boolean;
  escrow_trade_sats?: number;
  escrow_bond_in_escrow?: { seller: number; buyer: number };
  escrow_release_coordinator_sats?: number;
  /** 2 (rl31-beta15): the coordinator fees ride in the escrow output and only the release of
   * a completed trade pays them; a collaborative cancel gives each trader its fee back. */
  escrow_fee_plan?: number;
  escrow_fees_in_escrow?: { seller: number; buyer: number };
}

export interface OnchainSummary {
  role: 'seller' | 'buyer';
  is_buyer: boolean;
  trade_sats: number;
  received_sats: number;
  bond_sats: number;
  bond_back_sats: number;
  coordinator_fee_sats: number;
  fee_from_bond: boolean;
  fee_at_release?: boolean;
  lightning_bond?: boolean;
  deposit_mining_sats?: number | null;
  release_mining_sats: number;
  deposit_txid?: string;
  release_txid?: string;
  release_kind?: string;
}

export class EscrowCheckError extends Error {}

const fail = (why: string): never => {
  throw new EscrowCheckError(why);
};

export const escrowKeys = (token: string, coordinator: string, orderId: number): EscrowKeys => ({
  escrow: tokenKey(token, coordinator, orderId, 'escrow'),
  funding: tokenKey(token, coordinator, orderId, 'funding'),
  bond: tokenKey(token, coordinator, orderId, 'bond'),
});

export const publicKeys = (
  keys: EscrowKeys,
): { escrow: string; funding: string; bond: string } => ({
  escrow: bytesToHex(pubkey(keys.escrow)),
  funding: bytesToHex(pubkey(keys.funding)),
  bond: bytesToHex(pubkey(keys.bond)),
});

const spentOf = (data: EscrowOrderData): TxOut[] =>
  (data.escrow_deposit_spent ?? []).map(([v, s]) => ({ value: BigInt(v), script: hexToBytes(s) }));

export const contractOf = (data: EscrowOrderData): EscrowContract => {
  if (!data.escrow_seller_pubkey || !data.escrow_buyer_pubkey || !data.escrow_coordinator_pubkey)
    fail('the escrow keys are not complete yet');
  return new EscrowContract(
    hexToBytes(data.escrow_seller_pubkey!),
    hexToBytes(data.escrow_buyer_pubkey!),
    hexToBytes(data.escrow_coordinator_pubkey!),
    data.escrow_rescue_csv!,
    data.escrow_hrp ?? 'bc',
  );
};

/** The coordinator's escrow address must be the one our own key and the peer's give. */
export const checkEscrowAddress = (data: EscrowOrderData, keys: EscrowKeys): EscrowContract => {
  const c = contractOf(data);
  const mine = bytesToHex(pubkey(keys.escrow));
  const role = data.escrow_role;
  if ((role === 'seller' ? data.escrow_seller_pubkey : data.escrow_buyer_pubkey) !== mine)
    fail('the coordinator does not have our escrow key');
  if (c.address !== data.escrow_address) fail('the escrow address does not match our keys');
  return c;
};

export const checkFundingAddress = (data: EscrowOrderData, keys: EscrowKeys): string => {
  const addr = p2trAddress(xonly(pubkey(keys.funding)), data.escrow_hrp ?? 'bc');
  if (addr !== data.escrow_funding_address) fail('the funding address is not ours');
  return addr;
};

export const bondContractOf = (data: EscrowOrderData, keys: EscrowKeys): BondContract => {
  const c = new BondContract(
    pubkey(keys.bond),
    hexToBytes(data.bond_coordinator_pubkey!),
    data.bond_refund_csv!,
    data.bond_hrp ?? 'bc',
  );
  if (c.address !== data.bond_address) fail('the bond address does not match our key');
  return c;
};

const spendsOnly = (tx: Transaction, txid: string, vout: number): void => {
  if (tx.inputs.length !== 1 || tx.inputs[0].txid !== txid || tx.inputs[0].vout !== vout)
    fail('this transaction does not spend our bond');
};

/** Signature of the penalty given in advance: it only ever spends this bond. */
export const signBondPenalty = (data: EscrowOrderData, keys: EscrowKeys): string => {
  const c = bondContractOf(data, keys);
  const tx = Transaction.parse(data.bond_penalty_tx!);
  spendsOnly(tx, data.bond_txid!, data.bond_vout!);
  if (tx.outputs.length !== 1) fail('the penalty must have a single output');
  const msg = tx.sighashUnified(
    0,
    [{ value: BigInt(data.bond_value!), script: c.scriptPubkey }],
    c.leafHashes[BondContract.COOPERATIVE],
  );
  return bytesToHex(signSchnorr(keys.bond, msg));
};

/** Return of a freed bond: everything (minus the fee) to our own address. */
export const signBondReturn = (
  data: EscrowOrderData,
  keys: EscrowKeys,
  myScript: Uint8Array,
  maxFeeSats: number,
): string => {
  const c = bondContractOf(data, keys);
  const tx = Transaction.parse(data.bond_return_tx!);
  spendsOnly(tx, data.bond_txid!, data.bond_vout!);
  if (tx.outputs.length !== 1 || bytesToHex(tx.outputs[0].script) !== bytesToHex(myScript))
    fail('the return does not pay our address');
  if (BigInt(data.bond_value!) - tx.outputs[0].value > BigInt(maxFeeSats))
    fail('the return fee is too high');
  const msg = tx.sighashUnified(
    0,
    [{ value: BigInt(data.bond_value!), script: c.scriptPubkey }],
    c.leafHashes[BondContract.COOPERATIVE],
  );
  return bytesToHex(signSchnorr(keys.bond, msg));
};

/** Most the deposit may take from us: the recommended funding (which the coordinator
 * recomputes from the live fee rate, so it drifts by a few sats after we paid: order #21 on
 * 17 nov 2026 failed by 1 sat) plus a small tolerance, plus our bond. Never unlimited. */
export const depositSpendLimit = (data: EscrowOrderData): number => {
  const recommended = data.escrow_funding_recommended ?? 0;
  const tolerance = Math.max(546, Math.ceil(recommended * 0.01));
  return recommended + tolerance + (data.bond_value ?? 0);
};

export interface DepositLimits {
  maxSpendSats: number;
  maxFeeRate: number;
}

/** Checks the joint deposit and signs our funding inputs (and our bond input). */
export const signDeposit = (
  data: EscrowOrderData,
  keys: EscrowKeys,
  bondData: EscrowOrderData | null,
  limits: DepositLimits,
): { signatures: string[]; bond_signature?: string } => {
  const c = checkEscrowAddress(data, keys);
  const tx = Transaction.parse(data.escrow_deposit_tx!);
  const spent = spentOf(data);
  if (spent.length !== tx.inputs.length) fail('inputs and spent outputs do not match');
  const myFunding = fundingScript(pubkey(keys.funding));
  const mine = data.escrow_deposit_my_inputs ?? [];
  const bondIndex = data.escrow_deposit_my_bond_input;
  let bondValue = BigInt(0);
  let bond: BondContract | null = null;
  if (bondIndex !== null && bondIndex !== undefined) {
    if (!bondData) fail('missing our bond');
    bond = bondContractOf(bondData!, keys);
    if (bytesToHex(spent[bondIndex].script) !== bytesToHex(bond.scriptPubkey))
      fail('the bond input is not our bond');
    bondValue = spent[bondIndex].value;
  }
  for (const i of mine)
    if (bytesToHex(spent[i].script) !== bytesToHex(myFunding))
      fail('an input marked as ours is not our funding');
  if (bytesToHex(tx.outputs[0].script) !== bytesToHex(c.scriptPubkey))
    fail('the first output is not the escrow');
  if (tx.outputs[0].value < BigInt(data.escrow_sats!) + bondValue)
    fail('the escrow output is too small');
  const totalIn = spent.reduce((a, s) => a + s.value, BigInt(0));
  const totalOut = tx.outputs.reduce((a, o) => a + o.value, BigInt(0));
  if (totalOut > totalIn) fail('outputs exceed inputs');
  const myIn = mine.reduce((a, i) => a + spent[i].value, BigInt(0)) + bondValue;
  const myBack = tx.outputs
    .filter((o) => bytesToHex(o.script) === bytesToHex(myFunding))
    .reduce((a, o) => a + o.value, BigInt(0));
  if (myIn - myBack > BigInt(limits.maxSpendSats))
    fail('this deposit takes more than agreed from us');
  const vbytes =
    11 + tx.inputs.length * 110 + tx.outputs.reduce((a, o) => a + 9 + o.script.length, 0);
  if (Number(totalIn - totalOut) > vbytes * limits.maxFeeRate) fail('the mining fee is too high');
  const signatures = mine.map((i) =>
    bytesToHex(signSchnorr(keys.funding, tx.sighashUnified(i, spent))),
  );
  const out: { signatures: string[]; bond_signature?: string } = { signatures };
  if (bond && bondIndex !== null && bondIndex !== undefined)
    out.bond_signature = bytesToHex(
      signSchnorr(
        keys.bond,
        tx.sighashUnified(bondIndex, spent, bond.leafHashes[BondContract.COOPERATIVE]),
      ),
    );
  return out;
};

export interface ReleaseCheck {
  myScript: Uint8Array;
  minToMe: number;
  /** When set, the release must pay us exactly this (less the mining fee if we pay it). */
  owed?: { due: number; paysFee: boolean };
}

// Most the release may leave to miners when we pay it: ~200 vB at 25 sat/vB.
export const MAX_RELEASE_FEE_SATS = 5000;

/** What a cooperative release (trade or collaborative cancel) owes us, to the sat, from the
 * amounts the coordinator states, after checking them against what we know ourselves. Null when
 * the coordinator does not state them (older servers) or for dispute releases. */
export const owedInRelease = (data: EscrowOrderData): { due: number; paysFee: boolean } | null => {
  const bonds = data.escrow_bond_in_escrow;
  const trade = data.escrow_trade_sats;
  if (
    !bonds ||
    trade === undefined ||
    !(data.escrow_release_kind ?? '').startsWith('Seller and buyer')
  )
    return null;
  const role = data.escrow_role;
  // fee plan 2: each trader's fee is inside the escrow output until the release
  const held =
    (data.escrow_fee_plan ?? 1) >= 2
      ? (data.escrow_fees_in_escrow ?? { seller: 0, buyer: 0 })
      : { seller: 0, buyer: 0 };
  if (held.seller < 0 || held.buyer < 0)
    fail('the amounts of the release do not add up to the escrow');
  if (trade + bonds.seller + bonds.buyer + held.seller + held.buyer !== data.escrow_output_sats)
    fail('the amounts of the release do not add up to the escrow');
  const myHeld = role === 'seller' ? held.seller : held.buyer;
  // our fee inside the escrow is what we agreed to pay: what we owed beyond the trade amount
  if ((data.escrow_fee_plan ?? 1) >= 2 && data.escrow_funding_owed !== undefined) {
    const agreed =
      role === 'seller'
        ? data.escrow_funding_owed - (data.escrow_sats ?? trade)
        : data.escrow_funding_owed;
    if (myHeld !== agreed) fail('the release does not give our fee back');
  }
  if (role === 'buyer' && data.escrow_fee_at_release && held.buyer !== 0)
    fail('the amounts of the release do not add up to the escrow');
  if (data.escrow_sats !== undefined && trade !== data.escrow_sats)
    fail('the release does not keep the trade amount');
  const myBond = role === 'seller' ? bonds.seller : bonds.buyer;
  if (data.bond_value !== undefined && data.bond_txid) {
    if (myBond > data.bond_value) fail('the release counts more bond than we posted');
    const mayLose = data.escrow_fee_from_bond ? (data.escrow_funding_recommended ?? 0) : 0;
    if (myBond < data.bond_value - mayLose) fail('the release does not give our bond back');
    if (data.escrow_fee_from_bond && myBond + myHeld > data.bond_value)
      fail('the release counts more bond than we posted');
  }
  const atRelease = data.escrow_release_coordinator_sats ?? 0;
  if (atRelease && atRelease !== (data.escrow_fee_at_release_sats ?? -1))
    fail('the release charges a fee we did not agree');
  if (data.escrow_release_cancel)
    return role === 'seller'
      ? { due: trade + bonds.seller + held.seller, paysFee: true }
      : { due: bonds.buyer + held.buyer, paysFee: false };
  return role === 'buyer'
    ? { due: trade + bonds.buyer - atRelease, paysFee: true }
    : { due: bonds.seller, paysFee: false };
};

const checkRelease = (
  data: EscrowOrderData,
  check: ReleaseCheck,
): { tx: Transaction; c: EscrowContract } => {
  const c = contractOf(data);
  const tx = Transaction.parse(data.escrow_release_tx!);
  const toMe = tx.outputs
    .filter((o) => bytesToHex(o.script) === bytesToHex(check.myScript))
    .reduce((a, o) => a + o.value, BigInt(0));
  if (check.owed) {
    const out = tx.outputs.reduce((a, o) => a + o.value, BigInt(0));
    const fee = BigInt(data.escrow_output_sats!) - out;
    if (fee < BigInt(0) || fee > BigInt(MAX_RELEASE_FEE_SATS))
      fail('the release mining fee is too high');
    const expect = BigInt(check.owed.due) - (check.owed.paysFee ? fee : BigInt(0));
    if (toMe !== expect) fail('the release does not pay us exactly what we are owed');
    return { tx, c };
  }
  if (toMe < BigInt(check.minToMe)) fail('the release pays us less than agreed');
  return { tx, c };
};

const escrowSpent = (data: EscrowOrderData, c: EscrowContract): TxOut[] => [
  { value: BigInt(data.escrow_output_sats!), script: c.scriptPubkey },
];

const releaseSession = (
  data: EscrowOrderData,
  tx: Transaction,
  c: EscrowContract,
  aggnonce: Uint8Array,
): Session => ({
  aggnonce,
  pubkeys: c.musigPubkeys(),
  tweaks: [c.tweak],
  isXonly: [true],
  msg: tx.sighashUnified(0, escrowSpent(data, c)),
});

/** Round 1 of the cooperative release. Keep the secret nonce in memory only. */
export const releaseNonce = (
  data: EscrowOrderData,
  keys: EscrowKeys,
  check: ReleaseCheck,
): { secnonce: Uint8Array; pubnonce: string } => {
  const { tx, c } = checkRelease(data, check);
  const msg = tx.sighashUnified(0, escrowSpent(data, c));
  const n = nonceGen(keys.escrow, individualPk(keys.escrow), c.outputKey, msg);
  return { secnonce: n.secnonce, pubnonce: bytesToHex(n.pubnonce) };
};

/** Round 2: our partial signature over the same release, once both nonces are in. */
export const releasePartial = (
  data: EscrowOrderData,
  keys: EscrowKeys,
  check: ReleaseCheck,
  secnonce: Uint8Array,
): string => {
  const { tx, c } = checkRelease(data, check);
  const session = releaseSession(data, tx, c, hexToBytes(data.escrow_release_aggnonce!));
  return bytesToHex(musigSign(secnonce, keys.escrow, session));
};

/** Dispute won: our signature on the leaf with the coordinator. */
export const signDisputeRelease = (
  data: EscrowOrderData,
  keys: EscrowKeys,
  check: ReleaseCheck,
): string => {
  const { tx, c } = checkRelease(data, check);
  const leaf =
    data.escrow_role === 'buyer'
      ? EscrowContract.BUYER_COORDINATOR
      : EscrowContract.SELLER_COORDINATOR;
  const msg = tx.sighashUnified(0, escrowSpent(data, c), c.leafHashes[leaf]);
  return bytesToHex(signSchnorr(keys.escrow, msg));
};

export { partialSigVerify };

/** Sweep of what is left on our own funding address: every input must be our funding, the only
 * output our address, and the fee at most maxFeeSats. One signature per input. */
export const signSweep = (
  data: EscrowOrderData,
  keys: EscrowKeys,
  myScript: Uint8Array,
  maxFeeSats: number,
): string[] => {
  const tx = Transaction.parse(data.escrow_sweep_tx!);
  const spent: TxOut[] = (data.escrow_sweep_spent ?? []).map(([v, spk]) => ({
    value: BigInt(v),
    script: hexToBytes(spk),
  }));
  if (spent.length !== tx.inputs.length) fail('inputs and spent outputs do not match');
  const myFunding = bytesToHex(fundingScript(pubkey(keys.funding)));
  for (const sp of spent)
    if (bytesToHex(sp.script) !== myFunding) fail('an input is not our funding');
  if (tx.outputs.length !== 1 || bytesToHex(tx.outputs[0].script) !== bytesToHex(myScript))
    fail('the sweep does not pay our address');
  const totalIn = spent.reduce((a, sp) => a + sp.value, BigInt(0));
  if (totalIn - tx.outputs[0].value > BigInt(maxFeeSats)) fail('the sweep fee is too high');
  return tx.inputs.map((_, i) =>
    bytesToHex(signSchnorr(keys.funding, tx.sighashUnified(i, spent))),
  );
};
