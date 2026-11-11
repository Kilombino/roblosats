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
