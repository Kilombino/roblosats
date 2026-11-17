# /api/escrow — onchain escrow primitives (Roblosats, agent reference)

Pure Python building blocks for the onchain deposit (plan: /mnt/datos2/roblosats/PLAN-onchain-escrow.md,
phase 2). No Django imports; everything is bytes in, bytes out.

## Contract (`taproot.EscrowContract`)
- Key path: MuSig2(seller, buyer) (BIP327 key aggregation in this order, no sorting), tweaked
  with the tap tree. The cooperative release needs both traders and never the coordinator.
- Leaves (no `OP_IF`: RDTS forbids it in tapscript; control blocks ≤ 7 nodes):
  0 `<seller> CHECKSIGVERIFY <coordinator> CHECKSIG` (dispute won by the seller / refund),
  1 `<buyer> CHECKSIGVERIFY <coordinator> CHECKSIG` (dispute won by the buyer),
  2 rescue plan 1 (escrows up to rl31-beta15): `<seller> CHECKSIGVERIFY <csv> CHECKSEQUENCEVERIFY`
    (the seller alone takes everything); rescue plan 2 (rl31-beta16):
    `<seller> CHECKSIGVERIFY <buyer> CHECKSIGVERIFY <csv> CHECKSEQUENCEVERIFY` (fair rescue, below).
- Witness for a leaf: signatures in reverse key order (`leaf_witness(c, i, [sig_second_key, sig_first_key])`),
  then the script and the control block.

## Signature hash
`Transaction.sighash_unified` reimplements Knots' `SignatureHashUnified` (interpreter.cpp) for
Taproot with `SIGHASH_ALL|SIGHASH_UNIFIED` (0x21) only: tag `UnifiedSighash`, locktime as 5
bytes, script type 2 (key path) / 3 (tapscript) where BIP341 has its spend type, annex byte,
then leaf hash, key version and codeseparator position for tapscript. Every escrow signature
must use 0x21: a plain BIP341 signature is also valid on the SHA-256 chain (replay). Checked
end to end against a regtest node (lab-onchain/py/lab_escrow.py, 9/9).

## Keys
`derive_secret(secret, coordinator, order_id, role)` is HKDF-SHA256 with info
`roblosats/escrow/v1/<coordinator>/<order_id>/<role>`. Traders derive theirs from the robot token
in the client (the server only ever sees public keys); the coordinator derives its key from a
server secret. The JS client must produce the same keys: see the vectors in
`api/tests/test_escrow_taproot.py`.

## Which code signs
- Coordinator signatures (dispute leaves) use libsecp256k1 (`secp256k1.PrivateKey.schnorr_sign`,
  BIP340, constant time), checked with the reference verifier before returning.
- `bip327.py` is the BIP327 reference implementation, vendored. Its own header warns it is not
  constant time: the server uses it only for public operations (key aggregation, nonce
  aggregation, partial signature verification and aggregation) and tests use it to play the
  traders. Never sign with a secret key through it on the server.

## Deposit and release (`transactions.py`)
- Funding address per trader and order: `rawtr(<funding key>)` (no tweak), key derived from the
  token with role `funding`. The trader pays it from any wallet; the coordinator watches it
  read-only (`funding_descriptor`).
- `plan_deposit` builds the joint deposit: escrow output first, coordinator fee, dev fee (only
  if ≥ 330 sats and a dev script is set; it comes out of the trading fees), then each side's
  change. Each side pays the mining fee of its own inputs and change plus half of the shared
  part; change below dust is left as fee. Short funding raises `DepositError` with the amount
  missing.
- `check_deposit` is what a client verifies before signing its inputs: escrow output and
  amount, what it is charged at most, and a fee-rate ceiling.
- `release_tx`: one escrow input, payouts adding up to the escrow, `fee_payer` (the buyer by
  default) pays the mining fee; outputs below dust are dropped.
- Size constants match the real transactions on regtest (key-path release 112 vB, two-signature
  leaf 170 vB, 2-in/5-out deposit 341 vB); sizes are rounded up before applying the fee rate so
  the requested rate is always reached (lab-onchain/py/lab_flow.py, 16/16).

## Service (`service.py`, Django glue)
- Off unless `ONCHAIN_ESCROW=True` and `ESCROW_SECRET` (the coordinator's escrow keys are
  HKDF of it per order; losing it loses the ability to sign dispute leaves, so back it up with
  the coordinator's other secrets). `ESCROW_RESCUE_CSV` (default 2016 blocks ≈ 2 weeks; it was 4480 before rl31-beta16, new escrows only),
  `ESCROW_WATCH_WALLET` (default `roblosats-escrow`, a watch-only descriptor wallet created on
  first use in the coordinator's bitcoind), `ESCROW_DEPOSIT_TARGET_BLOCKS`, `ESCROW_FALLBACK_FEE_RATE`.
- Flow: `create_escrow` → `submit_keys` (each trader: escrow key + funding key; when both are
  in, the address is fixed and the funding addresses and the escrow are imported read-only) →
  traders pay their funding addresses → `build_deposit` (status FUNDING) →
  `submit_funding_signatures` (each signature checked against the trader's funding key; broadcast
  when both are in; MEMPOOL) → `refresh_confirmation` (LOCKED at 1 confirmation) →
  `start_release` (fixes the release: the buyer's address, next-block fee paid by the buyer) →
  cooperative: `submit_release_nonce` ×2, `aggregate_nonce`, `submit_release_partial` ×2 (each
  partial verified; broadcast when both verify; RELEASED) — dispute: `submit_leaf_signature` by
  the winner only, the coordinator adds its own and broadcasts.
- Row locks (`select_for_update`) guard every state change; the broadcast happens after the
  commit and the status moves only after `sendrawtransaction` returned.
- Tested end to end against the regtest lab: `api/tests/test_escrow_service_regtest.py`
  (opt-in, `ESCROW_REGTEST=1`).

## Order flow (`flow.py`, hooks in logics/views/admin/follow_invoices)
- `Order.escrow_type` (`LIGHTNING`=0, `ONCHAIN`=1), chosen at creation (`escrow_type` in the
  make-order request; refused unless `service.enabled()`). `/api/info` says `onchain_escrow`;
  the book lists `escrow_type`; the Nostr order event carries `layer=onchain`.
- No Lightning at all: bonds are onchain too (`bonds.py`, see below). States: WFB (maker bond
  locked → PUB), TAK (taker bond locked → contract), WF2 (keys + both release addresses via
  `escrow_*` actions) → WFE (funding; `ESCROW_FUNDING_SECONDS`, default 3 h) → CHA when the
  deposit has 1 confirmation → FSE → seller `confirm` starts the cooperative release and the
  bonds are returned → PAY → SUC when the release is broadcast (proceeds = the coordinator fee
  inside the deposit).
- `follow_invoices.follow_onchain_escrows` runs `flow.advance` under the order lock for onchain
  orders in WF2/WFE/PAY: builds the deposit when both funding addresses hold enough (needs
  `ESCROW_COORDINATOR_ADDRESS`), confirms it, completes the order after the release.
- Expiry in WF2/WFE: `flow.expire_funding` blames whoever is missing (keys, address, funding,
  deposit signature): that bond is settled and the other returned with the usual reward. A
  deposit already broadcast pushes the expiry back an hour instead.
- Disputes: `open_dispute` settles only the bonds (no LN escrow) and also accepts PAY (seller
  confirmed but does not sign). `maker_wins`/`taker_wins` call `flow.resolve_dispute` (the
  winner's release address must be set) and credit only the winner's bond; the escrow moves
  when the winner signs the leaf.
- Views: GET order adds `escrow_type` and, for traders, `flow.order_data` (funding address and
  amounts, the deposit to sign, the release and the aggregate nonce); POST actions
  `escrow_keys`, `escrow_address` (PGP signed like `update_address`), `escrow_funding_signatures`,
  `escrow_nonce`, `escrow_partial`, `escrow_leaf_signature` with their data in `escrow`.
- Regtest: `EscrowOrderFlowRegtest` (WF2 → SUC, and a dispute solved from the admin).

## Onchain bonds (`taproot.BondContract`, no Lightning at all)
- No key path: internal key is BIP341's NUMS point H, so nobody moves a bond alone before the
  timelock. Leaf 0 `<owner> CHECKSIGVERIFY <coordinator> CHECKSIG` (into the deposit, early
  return, or the penalty), leaf 1 `<owner> CHECKSIGVERIFY <refund_csv> CSV` (owner alone later).
- Penalty: the owner signs `bond_spend_tx(..., coordinator_spk)` through leaf 0 when posting the
  bond; the coordinator keeps that signature and adds its own only if the owner misbehaves.
- `plan_deposit(..., seller_bond=BondInput, buyer_bond=BondInput)`: both bonds become inputs of
  the deposit (leaf 0, owner + coordinator sign) and the escrow output holds amount + bonds; each
  side's funding pays the mining fee of its own bond input. Release: cooperative pays the buyer
  amount + its bond and the seller its bond; a dispute gives the loser's bond to the coordinator
  with a reward share for the winner.
- Regtest: lab-onchain/py/lab_bonds.py 10/10.

## Onchain bonds in the order flow (`bonds.py`, `OnchainBond`)
- `bond_key` (owner's bond key) → `OnchainBond` FUNDING with its address and `required_sats`
  (bond_size % of the amount); `refund_csv` = the hold-invoice CLTV the LN bond would have had.
  `bonds.refresh` (from `flow.advance`) waits for a confirmed output, fixes the penalty to
  `ESCROW_COORDINATOR_ADDRESS` (`ESCROW_PENALTY_FEE_RATE`, default 10 sat/vB) → SIGN;
  `bond_penalty_signature` → LOCKED. Maker LOCKED in WFB → `publish_order`; pretaker LOCKED in
  PUB → `finalize_contract`.
- The coordinator's key per bond is HKDF of `ESCROW_SECRET` with order, role and the owner's
  bond key (stable whatever happens to the TakeOrder row); checked against the stored public key
  before every signature.
- Into the deposit as inputs; the trader signs its bond input together with its funding
  inputs (`escrow_funding_signatures` with `bond_signature`); the coordinator adds its own.
- Penalty (`bonds.penalize`): expiry in WF2/WFE (whoever is missing), unilateral cancel in
  WF2/WFE (`flow.cancel_before_escrow`; taker cancelling sends the order back to the book with the
  maker's bond still locked). `bonds.release` → RETURNABLE: public order cancelled or expired,
  pretaker gone, the other party's bond. RETURNABLE bonds come back with `bond_return {address}`
  + `bond_return_signature` (owner + coordinator), or alone after `refund_csv`.
- After the deposit the bonds are inside the escrow output: cooperative release gives each bond
  back, a collaborative cancel returns amount + bond to the seller and its bond to the buyer
  (order → CCA once broadcast), a dispute gives the winner amount + its bond + a
  `SLASHED_BOND_REWARD_SPLIT` share of the loser's bond and the rest to the coordinator (nothing
  is credited to the robot: all paid on chain).
- LN paths that touch `order.maker_bond`/`taker_bond` are guarded (`settle_bond(None)` is a
  no-op; views skip hold-invoice generation and the LN lock checks for onchain orders).
- Regtest: `EscrowOrderFlowRegtest` — WFB → PUB → take → WF2 → WFE → CHA → PAY → SUC, a
  collaborative cancel, a dispute from the admin, and a taker that never funds (penalty mined).

## rl31-beta9 (16 nov 2026, after the first real onchain trade, order #20 on Kilobot)
- Buyer fee out of its bond (`flow.buyer_fee_from_bond`, `ESCROW_BUYER_FEE_FROM_BOND`, default
  on): the buyer funds nothing; `plan_deposit(..., buyer_fee_from_bond=True)` takes its fee and
  its mining share (bond input + half the shared part) from its bond and leaves the rest in the
  escrow output. The plan stores `bond_in_escrow` (what each bond left in the escrow), and the
  release/cancel/dispute payouts use it (`_bond_values`). The order data tells the buyer's
  client `escrow_fee_from_bond` and the most it may lose (`escrow_funding_recommended`).
- Mempool: `mempool_seen` (bonds still FUNDING, funding addresses while KEYS) → order data
  `bond_mempool_txid/sats`, `escrow_funding_mempool_txid/sats`; `notify_mempool` (from
  `advance`) logs it and notifies the payer once per txid (Redis cache key).
- `waiting_for` → `escrow_waiting_for` (`you`/`peer`/`confirmation`/`coordinator`) and
  `escrow_waiting_what` so the app names who it waits for.
- `trade_summary` → `onchain_summary` once released (the Lightning `summarize_trade` is skipped
  for onchain orders since beta7).
- `/api/book/?onchain=1`: the onchain public orders, only for an authenticated allowed robot
  while `ESCROW_ALLOWED_ROBOTS` is set (never cached).

## Hybrid: a Lightning taker on an onchain order (rl31-beta10)
- Take with `take_lightning: true` (only on onchain orders, `ESCROW_LIGHTNING_TAKER`, default
  on): `TakeOrder.lightning`; the taker gets a normal Lightning hold invoice for its bond; on
  lock `finalize_contract` sets `Order.taker_lightning`.
- Case A (taker = buyer): the escrow stays onchain (seller funds it, only the seller's onchain
  bond goes into the deposit). The buyer funds nothing: its coordinator fee comes out of its
  payout in the cooperative release (`plan.buyer_fee_at_release`, coordinator output in the
  release, `release_coordinator_sats` counted in `proceeds`). Its Lightning bond is returned
  on success / collaborative cancel, settled on expiry-at-fault / cancel / dispute open
  (`penalize_side` / `free_side`); a dispute credits it back or rewards the maker
  (`ln_bond_dispute_credit`, ledger).
- Case B (taker = seller): `flow.to_lightning` turns the order into a Lightning order. The
  maker's onchain bond is represented by a proxy `LNPayment` (`payment_hash =
  "onchain-bond-<OnchainBond.pk>"`) in `order.maker_bond`, so the Lightning machinery runs
  unchanged: `Logics.settle_bond` on the proxy penalizes the onchain bond,
  `return_bond`/`cancel_bond` free it (the maker takes it back with the usual bond_return
  actions, allowed on such orders), `reward_source_problem` accepts a penalized proxy without
  asking LND, `follow_invoices` never looks proxies up. The buyer gets the onchain swap down to
  `ESCROW_MIN_SATS`. Back in the book (`publish_order` → `to_onchain`) it is onchain again.
- Case C (buyer of an onchain escrow paid by Lightning through the coordinator) is NOT
  implemented: `ln_payout_enabled()` is hard-wired False.
- Tests: `api/tests/test_roblosats_hybrid.py` (mocked) and `EscrowHybridRegtest` (regtest).

## Coordinator fee plans (rl31-beta15)
- `funding_signatures["fee_plan"]` is set to 2 by `service.create_escrow` and never changes;
  escrows without it (created up to rl31-beta14) are plan 1 and keep the old rules everywhere
  (`flow.fee_plan`).
- Plan 1: the fees are an output of the deposit (kept by the coordinator even on a cancel);
  hybrid A adds the buyer's fee as a second output in the release, each output at least the
  minimum.
- Plan 2 (Kilombino, 17 nov 2026: a collaborative cancel refunds the fee, as with Lightning):
  the deposit has no fee nor dev output; both fees ride in the escrow output
  (`plan.fees_in_escrow`, `flow.fees_held`). `seller_confirms` pays them (plus hybrid A's
  `buyer_fee_at_release`, out of the buyer's payout) in ONE coordinator output of at least
  `ESCROW_MIN_COORDINATOR_FEE_SATS` for the whole order, with the optional dev share split off
  in the release (`release_fee_sats`, `release_dev_sats`). `collaborative_cancel` and
  `return_everything` give each trader its fee back (no coordinator output); `resolve_dispute`
  pays the winner as before and the coordinator the penalty plus the fees.
- `proceeds` counts only what reaches the coordinator on chain: plan 2 `release_fee_sats` (0 on
  a cancel); plan 1 the deposit's fee output plus `release_coordinator_sats`.
- Client data: `escrow_fee_plan`, `escrow_fees_in_escrow` (`owedInRelease` adds them to the
  escrow total and to a cancel's refunds, and checks ours against `escrow_funding_owed`);
  `escrow_release_coordinator_sats` stays the buyer's fee taken out of its payout (hybrid A).
- Summary: `charged_fees` (0 for both after a plan-2 cancel), `fee_refunded_sats`.
- Tests: `api/tests/test_escrow_fee_plan2.py`, regtest `EscrowOrderFlowRegtest` (exact outputs
  of trade, collaborative cancel, dispute won, return everything) and
  `EscrowHybridRegtest.test_case_a_small_order_one_minimum_in_total`; client
  `owed.test.ts`, e2e trade + cancel.

## Fair rescue (rescue plan 2, rl31-beta16)
- `funding_signatures["rescue_plan"]` = 2 from `service.create_escrow` (`service.RESCUE_PLAN`);
  escrows without it keep plan 1 (`service.rescue_plan`, `EscrowContract(rescue_plan=...)`, JS
  `new EscrowContract(..., rescuePlan)`): their address and the seller-alone rescue are unchanged.
- Right after `build_deposit` (deposit txid known, nothing signed), `flow.advance` calls
  `service.build_rescue(escrow, *_returns(escrow))`: one input (escrow outpoint, sequence =
  rescue_csv), seller output = trade + its bond + its held fee, buyer output = its bond + its
  held fee (none in hybrid A), each paying half the mining fee at `rescue_fee_rate()`
  (estimate clamped to ESCROW_RESCUE_MIN/MAX_FEE_RATE, 2–20 sat/vB). A buyer output that would
  fall below dust is dropped (to the miners, never to the seller).
- Each trader signs it (leaf 2, 0x21) with `escrow_rescue_signature`; `submit_funding_signatures`
  refuses deposit signatures until both rescue signatures are in, and the client keeps the
  complete rescue (`completeRescue`, both signatures checked) in its encrypted rescue record
  before it signs the deposit. Whoever withholds its rescue signature is at fault on expiry.
- Without the coordinator, after rescue_csv blocks either side broadcasts the stored rescue
  ("Recover without the coordinator" → "Your part of the escrow"). No key path or leaf lets one
  side take the other's part; the coordinator is not needed and cannot change it.
- Limits: the fee is fixed at deposit time (CPFP from one's own output if it falls short); the
  rescue returns the pre-trade split, so a buyer who already paid fiat gets only its bond back
  if the coordinator vanishes before the release (as with plan 1, but now it keeps its bond);
  the coordinator does not notice a mined rescue (the escrow stays LOCKED in its database).
- Tests: api/tests/test_escrow_rescue_plan2.py (unit), EscrowOrderFlowRegtest
  `test_fair_rescue_without_the_coordinator` / `test_rescue_plan_1_kept_for_old_escrows`, JS
  e2e `fair rescue without the coordinator`.
