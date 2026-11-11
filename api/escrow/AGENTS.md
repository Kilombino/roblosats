# /api/escrow — onchain escrow primitives (Roblosats, agent reference)

Pure Python building blocks for the onchain deposit (plan: /mnt/datos2/roblosats/PLAN-onchain-escrow.md,
phase 2). No Django imports; everything is bytes in, bytes out.

## Contract (`taproot.EscrowContract`)
- Key path: MuSig2(seller, buyer) (BIP327 key aggregation in this order, no sorting), tweaked
  with the tap tree. The cooperative release needs both traders and never the coordinator.
- Leaves (no `OP_IF`: RDTS forbids it in tapscript; control blocks ≤ 7 nodes):
  0 `<seller> CHECKSIGVERIFY <coordinator> CHECKSIG` (dispute won by the seller / refund),
  1 `<buyer> CHECKSIGVERIFY <coordinator> CHECKSIG` (dispute won by the buyer),
  2 `<seller> CHECKSIGVERIFY <csv> CHECKSEQUENCEVERIFY` (seller rescue after ~4 weeks).
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
  the coordinator's other secrets). `ESCROW_RESCUE_CSV` (default 4480 blocks ≈ 4 weeks at 9 min),
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
