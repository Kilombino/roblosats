import { schnorr } from '@noble/curves/secp256k1.js';
import { hexToBytes, bytesToHex } from '@noble/hashes/utils.js';

import {
  applyTweak,
  keyAgg,
  nonceAgg,
  nonceGenInternal,
  partialSigAgg,
  partialSigVerify,
  sign,
  xonlyAggKey,
  individualPk,
  nonceGen,
} from '../musig2';
import {
  BondContract,
  EscrowContract,
  Transaction,
  deriveSecret,
  pubkey,
  tokenKey,
} from '../taproot';
import keyAggV from './vectors/key_agg_vectors.json';
import nonceGenV from './vectors/nonce_gen_vectors.json';
import nonceAggV from './vectors/nonce_agg_vectors.json';
import signV from './vectors/sign_verify_vectors.json';
import tweakV from './vectors/tweak_vectors.json';
import sigAggV from './vectors/sig_agg_vectors.json';

const h = (s: string): Uint8Array => hexToBytes(s);
const H = (b: Uint8Array): string => bytesToHex(b).toUpperCase();
const enc = new TextEncoder();

describe('BIP327 vectors', () => {
  test('key aggregation', () => {
    for (const t of keyAggV.valid_test_cases) {
      const pks = t.key_indices.map((i: number) => h(keyAggV.pubkeys[i]));
      expect(H(xonlyAggKey(keyAgg(pks)))).toBe(t.expected);
    }
  });

  test('nonce generation', () => {
    for (const t of nonceGenV.test_cases) {
      const r = nonceGenInternal(
        h(t.rand_),
        t.sk ? h(t.sk) : null,
        h(t.pk),
        t.aggpk ? h(t.aggpk) : null,
        t.msg !== null ? h(t.msg) : null,
        t.extra_in ? h(t.extra_in) : null,
      );
      expect(H(r.secnonce)).toBe(t.expected_secnonce);
      expect(H(r.pubnonce)).toBe(t.expected_pubnonce);
    }
  });

  test('nonce aggregation', () => {
    for (const t of nonceAggV.valid_test_cases) {
      expect(H(nonceAgg(t.pnonce_indices.map((i: number) => h(nonceAggV.pnonces[i]))))).toBe(
        t.expected,
      );
    }
  });

  test('sign and verify', () => {
    for (const t of signV.valid_test_cases) {
      const pubkeys = t.key_indices.map((i: number) => h(signV.pubkeys[i]));
      const pnonces = t.nonce_indices.map((i: number) => h(signV.pnonces[i]));
      const session = {
        aggnonce: h(signV.aggnonces[t.aggnonce_index]),
        pubkeys,
        tweaks: [],
        isXonly: [],
        msg: h(signV.msgs[t.msg_index]),
      };
      const psig = sign(h(signV.secnonces[0]), h(signV.sk), session);
      expect(H(psig)).toBe(t.expected);
      expect(partialSigVerify(psig, pnonces, session, t.signer_index)).toBe(true);
    }
  });

  test('tweaks', () => {
    for (const t of tweakV.valid_test_cases) {
      const session = {
        aggnonce: h(tweakV.aggnonce),
        pubkeys: t.key_indices.map((i: number) => h(tweakV.pubkeys[i])),
        tweaks: t.tweak_indices.map((i: number) => h(tweakV.tweaks[i])),
        isXonly: t.is_xonly,
        msg: h(tweakV.msg),
      };
      expect(H(sign(h(tweakV.secnonce), h(tweakV.sk), session))).toBe(t.expected);
    }
  });

  test('signature aggregation', () => {
    for (const t of sigAggV.valid_test_cases) {
      const session = {
        aggnonce: h(t.aggnonce),
        pubkeys: t.key_indices.map((i: number) => h(sigAggV.pubkeys[i])),
        tweaks: t.tweak_indices.map((i: number) => h(sigAggV.tweaks[i])),
        isXonly: t.is_xonly,
        msg: h(sigAggV.msg),
      };
      const psigs = t.psig_indices.map((i: number) => h(sigAggV.psigs[i]));
      expect(H(partialSigAgg(psigs, session))).toBe(t.expected);
    }
  });
});

describe('same keys, contracts and sighash as the coordinator (api/tests/test_escrow_taproot.py)', () => {
  const sk = (secret: string, role: string): Uint8Array =>
    deriveSecret(enc.encode(secret), 'kilobot', 4242, role);
  const S = pubkey(sk('token-del-vendedor', 'seller'));
  const B = pubkey(sk('token-del-comprador', 'buyer'));
  const C = pubkey(sk('secreto-del-coordinador', 'coordinator'));

  test('keys', () => {
    expect(bytesToHex(S)).toBe(
      '03c94775090f921d1865978e21ccf6d0d9d9cd877d71fbb7f93479807adbdf3982',
    );
    expect(bytesToHex(B)).toBe(
      '02f83c9a54f0e918d68b4b9f6a3b3fe4f573007effd865892807916103e37e326e',
    );
    expect(bytesToHex(C)).toBe(
      '020764e0f847d501bfa14e542f49d919f7ad308751f45fb45f5d9b094cc763f032',
    );
    expect(bytesToHex(pubkey(tokenKey('Kx1ToKeN', 'Kilobot', 77, 'funding')))).toBe(
      '035278cce6fd04ad4fe80656b8a53af3a5d14189d3fea3ae830b1f0be650d53d42',
    );
  });

  test('escrow contract', () => {
    const c = new EscrowContract(S, B, C, 4480);
    expect(c.address).toBe('bc1p9jhzqnjypwjcwvleg862n460lgdmqrvkh98rzefgedum6yrk7z8qp8xsxd');
    expect(bytesToHex(c.internalKey)).toBe(
      '2098b16fde9f60657ddaa1a777ecbbf40eb579612f84d358831b14dab49b06ac',
    );
    expect(bytesToHex(c.merkleRoot)).toBe(
      '5811bfb9b566b41985bb3f96b59fe5c53d3d14cbd787bcc08285d0c8ac50ac98',
    );
  });

  test('bond contract', () => {
    const b = new BondContract(pubkey(sk('token-del-vendedor', 'bond')), C, 432);
    expect(b.address).toBe('bc1puz53mcxycuqjexuzyh3xkgrvlzl2f8tduzmzrjrg75acxvqyptyqd5qfrz');
  });

  test('unified sighash, key path, leaf and bond leaf', () => {
    const c = new EscrowContract(S, B, C, 4480);
    const tx = new Transaction(
      [{ txid: '11'.repeat(32), vout: 0, sequence: 0xfffffffd }],
      [{ value: BigInt(99000), script: h('5120' + '22'.repeat(32)) }],
    );
    const spent = [{ value: BigInt(100000), script: c.scriptPubkey }];
    expect(bytesToHex(tx.sighashUnified(0, spent))).toBe(
      '3171975a2f622d452db5a19239652d22d3142aeef865fd54c645f3bbc51a993c',
    );
    expect(
      bytesToHex(tx.sighashUnified(0, spent, c.leafHashes[EscrowContract.BUYER_COORDINATOR])),
    ).toBe('9c3276df71449904047e7525dec7f1b2a6c437339089f639c8620dc3413a9d65');
    const b = new BondContract(pubkey(sk('token-del-vendedor', 'bond')), C, 432);
    expect(
      bytesToHex(
        tx.sighashUnified(
          0,
          [{ value: BigInt(100000), script: b.scriptPubkey }],
          b.leafHashes[BondContract.COOPERATIVE],
        ),
      ),
    ).toBe('f2a9616bc5c87861adc8398f1b0a1fc2aa1895413ff2af091895edc6b78523f1');
  });

  test('a two-party MuSig2 key-path signature for the escrow output verifies', () => {
    const sks = [sk('token-del-vendedor', 'seller'), sk('token-del-comprador', 'buyer')];
    const c = new EscrowContract(S, B, C, 4480);
    const msg = new Uint8Array(32).fill(7);
    const pks = c.musigPubkeys();
    const n1 = nonceGen(sks[0], individualPk(sks[0]), c.outputKey, msg);
    const n2 = nonceGen(sks[1], individualPk(sks[1]), c.outputKey, msg);
    const session = {
      aggnonce: nonceAgg([n1.pubnonce, n2.pubnonce]),
      pubkeys: pks,
      tweaks: [c.tweak],
      isXonly: [true],
      msg,
    };
    const sig = partialSigAgg(
      [sign(n1.secnonce, sks[0], session), sign(n2.secnonce, sks[1], session)],
      session,
    );
    expect(schnorr.verify(sig, msg, c.outputKey)).toBe(true);
    expect(xonlyAggKey(applyTweak(keyAgg(pks), c.tweak, true))).toEqual(c.outputKey);
  });

  test('transaction parse roundtrip of the outpoints and outputs', () => {
    const tx = Transaction.parse(
      '020000000111111111111111111111111111111111111111111111111111111111111111110000000000fdffffff01b882010000000000225120' +
        '22'.repeat(32) +
        '00000000',
    );
    expect(tx.inputs[0].txid).toBe('11'.repeat(32));
    expect(tx.outputs[0].value).toBe(BigInt(99000));
  });
});
