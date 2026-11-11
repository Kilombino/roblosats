# frontend/src/utils/escrow — onchain escrow client crypto (Roblosats, agent reference)

Client side of the onchain escrow and bonds (server: `api/escrow`, plan:
/mnt/datos2/roblosats/PLAN-onchain-escrow.md). Built only on `@noble/curves`, `@noble/hashes` and
`@scure/base`, already in the bundle (no new dependency).

- `musig2.ts`: BIP327 (key aggregation in the given order, x-only tweak, nonce generation,
  nonce aggregation, partial sign/verify, aggregation). Passes every official BIP327 vector in
  `__tests__/vectors` (copied from bitcoin/bips). `sign` wipes the secret nonce so it cannot be
  reused; never persist a secret nonce.
- `taproot.ts`: keys from the robot token (`tokenKey`: HKDF-SHA256, info
  `roblosats/escrow/v1/<coordinator alias lowercase>/<order id>/<role>`, roles `escrow`,
  `funding`, `bond`), the escrow and bond contracts, Knots' unified sighash
  (SIGHASH_ALL|UNIFIED, 0x21) and minimal transaction parsing. `__tests__/escrow.test.ts` pins the
  same values as `api/tests/test_escrow_taproot.py`: client and coordinator must agree byte for
  byte, and the client must recompute every address the coordinator shows before paying it.
- `client.ts`: what the app checks and signs, step by step: bond address and penalty
  (`signBondPenalty` only signs a transaction that spends just our bond), escrow address
  recomputed from both keys (`checkEscrowAddress`), our funding address, the joint deposit
  (`signDeposit`: escrow output and amount, what it takes from us at most, fee ceiling; signs our
  funding inputs and our bond input), the two MuSig2 rounds of the cooperative release
  (`releaseNonce`/`releasePartial`, refusing a release that pays us less than agreed; keep the
  secret nonce in memory only), the dispute leaf and a bond return.
- `__tests__/e2e.test.ts`: opt-in cross test (`ESCROW_E2E_URL`) that plays both robots with
  `client.ts` against the real coordinator through the lab harness
  (/mnt/datos2/roblosats/lab-onchain/py/harness.py): bonds, take, keys, funding, deposit,
  confirmation and the cooperative release, ending in a successful order.
