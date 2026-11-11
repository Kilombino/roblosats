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
