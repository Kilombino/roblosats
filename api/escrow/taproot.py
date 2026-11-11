import hashlib
import hmac
import struct
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from api.escrow import bip327 as M

SIGHASH_ALL = 0x01
SIGHASH_UNIFIED = 0x20
SIGHASH_ALL_UNIFIED = SIGHASH_ALL | SIGHASH_UNIFIED
LEAF_VERSION = 0xC0
OP_CHECKSIG = b"\xac"
OP_CHECKSIGVERIFY = b"\xad"
OP_CSV = b"\xb2"
SEQUENCE_RBF = 0xFFFFFFFD
BECH32M_CONST = 0x2BC830A3
CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
KEY_INFO = b"roblosats/escrow/v1/"


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def tagged_hash(tag: str, data: bytes) -> bytes:
    return M.tagged_hash(tag, data)


def ser_compact(n: int) -> bytes:
    if n < 0xFD:
        return bytes([n])
    if n <= 0xFFFF:
        return b"\xfd" + struct.pack("<H", n)
    if n <= 0xFFFFFFFF:
        return b"\xfe" + struct.pack("<I", n)
    return b"\xff" + struct.pack("<Q", n)


def ser_bytes(data: bytes) -> bytes:
    return ser_compact(len(data)) + data


def push(data: bytes) -> bytes:
    if len(data) < 0x4C:
        return bytes([len(data)]) + data
    if len(data) <= 0xFF:
        return b"\x4c" + bytes([len(data)]) + data
    return b"\x4d" + struct.pack("<H", len(data)) + data


def script_num(n: int) -> bytes:
    if n == 0:
        return b"\x00"
    if 1 <= n <= 16:
        return bytes([0x50 + n])
    if n < 0:
        raise ValueError("negative script numbers are not used here")
    out = n.to_bytes((n.bit_length() + 8) // 8, "little")
    return push(out)


def hkdf_sha256(secret: bytes, info: bytes, length: int = 32) -> bytes:
    prk = hmac.new(b"\x00" * 32, secret, hashlib.sha256).digest()
    okm, block, counter = b"", b"", 1
    while len(okm) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        okm += block
        counter += 1
    return okm[:length]


def derive_secret(secret: bytes, coordinator: str, order_id: int, role: str) -> bytes:
    info = KEY_INFO + f"{coordinator}/{order_id}/{role}".encode()
    for attempt in range(256):
        sk = hkdf_sha256(secret, info + (bytes([attempt]) if attempt else b""))
        if 0 < int.from_bytes(sk, "big") < M.n:
            return sk
    raise RuntimeError("could not derive a valid key")


def pubkey(sk: bytes) -> bytes:
    return M.individual_pk(sk)


def xonly(pk33: bytes) -> bytes:
    if len(pk33) != 33 or pk33[0] not in (2, 3):
        raise ValueError("expected a compressed public key")
    return pk33[1:]


def _polymod(values: List[int]) -> int:
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (top >> i) & 1 else 0
    return chk


def _hrp_expand(hrp: str) -> List[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convertbits(data: bytes, frombits: int, tobits: int) -> List[int]:
    acc = bits = 0
    out = []
    maxv = (1 << tobits) - 1
    for v in data:
        acc = (acc << frombits) | v
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            out.append((acc >> bits) & maxv)
    if bits:
        out.append((acc << (tobits - bits)) & maxv)
    return out


def p2tr_address(output_key: bytes, hrp: str = "bc") -> str:
    data = [1] + _convertbits(output_key, 8, 5)
    polymod = _polymod(_hrp_expand(hrp) + data + [0] * 6) ^ BECH32M_CONST
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(CHARSET[d] for d in data + checksum)


def tapleaf_hash(script: bytes) -> bytes:
    return tagged_hash("TapLeaf", bytes([LEAF_VERSION]) + ser_bytes(script))


def tapbranch_hash(a: bytes, b: bytes) -> bytes:
    return tagged_hash("TapBranch", min(a, b) + max(a, b))


@dataclass
class EscrowContract:
    """Taproot escrow output: key path MuSig2(seller, buyer); leaves for disputes and rescue."""

    seller_pk: bytes
    buyer_pk: bytes
    coordinator_pk: bytes
    seller_rescue_csv: int
    hrp: str = "bc"
    leaves: List[bytes] = field(init=False)
    internal_key: bytes = field(init=False)
    merkle_root: bytes = field(init=False)
    tweak: bytes = field(init=False)
    output_key: bytes = field(init=False)
    parity: int = field(init=False)

    SELLER_COORDINATOR = 0
    BUYER_COORDINATOR = 1
    SELLER_RESCUE = 2

    def __post_init__(self):
        if not 0 < self.seller_rescue_csv < 0xFFFF:
            raise ValueError("seller_rescue_csv must be a block count below 65535")
        s, b, c = (xonly(k) for k in (self.seller_pk, self.buyer_pk, self.coordinator_pk))
        if len({s, b, c}) != 3:
            raise ValueError("seller, buyer and coordinator keys must differ")
        self.leaves = [
            push(s) + OP_CHECKSIGVERIFY + push(c) + OP_CHECKSIG,
            push(b) + OP_CHECKSIGVERIFY + push(c) + OP_CHECKSIG,
            push(s) + OP_CHECKSIGVERIFY + script_num(self.seller_rescue_csv) + OP_CSV,
        ]
        h = [tapleaf_hash(leaf) for leaf in self.leaves]
        b01 = tapbranch_hash(h[0], h[1])
        self.merkle_root = tapbranch_hash(b01, h[2])
        self._paths = {0: [h[1], h[2]], 1: [h[0], h[2]], 2: [b01]}
        self._leaf_hashes = h
        keyagg = M.key_agg(self.musig_pubkeys())
        self.internal_key = M.get_xonly_pk(keyagg)
        self.tweak = tagged_hash("TapTweak", self.internal_key + self.merkle_root)
        tweaked = M.apply_tweak(keyagg, self.tweak, True)
        self.output_key = M.get_xonly_pk(tweaked)
        self.parity = 0 if M.has_even_y(tweaked.Q) else 1

    def musig_pubkeys(self) -> List[bytes]:
        return [self.seller_pk, self.buyer_pk]

    @property
    def script_pubkey(self) -> bytes:
        return b"\x51\x20" + self.output_key

    @property
    def address(self) -> str:
        return p2tr_address(self.output_key, self.hrp)

    def leaf_hash(self, index: int) -> bytes:
        return self._leaf_hashes[index]

    def control_block(self, index: int) -> bytes:
        return bytes([LEAF_VERSION | self.parity]) + self.internal_key + b"".join(
            self._paths[index]
        )

    def descriptor_address_check(self) -> Tuple[str, str]:
        return self.address, self.script_pubkey.hex()


@dataclass
class TxIn:
    txid: str
    vout: int
    sequence: int = SEQUENCE_RBF
    witness: List[bytes] = field(default_factory=list)

    def outpoint(self) -> bytes:
        return bytes.fromhex(self.txid)[::-1] + struct.pack("<I", self.vout)


@dataclass
class TxOut:
    value: int
    script_pubkey: bytes

    def serialize(self) -> bytes:
        return struct.pack("<q", self.value) + ser_bytes(self.script_pubkey)


@dataclass
class Transaction:
    inputs: List[TxIn]
    outputs: List[TxOut]
    version: int = 2
    locktime: int = 0

    def serialize(self, with_witness: bool = True) -> bytes:
        has_witness = with_witness and any(i.witness for i in self.inputs)
        out = struct.pack("<I", self.version)
        if has_witness:
            out += b"\x00\x01"
        out += ser_compact(len(self.inputs))
        for i in self.inputs:
            out += i.outpoint() + b"\x00" + struct.pack("<I", i.sequence)
        out += ser_compact(len(self.outputs)) + b"".join(o.serialize() for o in self.outputs)
        if has_witness:
            for i in self.inputs:
                out += ser_compact(len(i.witness)) + b"".join(ser_bytes(w) for w in i.witness)
        return out + struct.pack("<I", self.locktime)

    def txid(self) -> str:
        return hashlib.sha256(hashlib.sha256(self.serialize(False)).digest()).digest()[::-1].hex()

    def vsize(self) -> int:
        base = len(self.serialize(False))
        total = len(self.serialize(True))
        return (base * 3 + total + 3) // 4

    def sighash_unified(
        self,
        index: int,
        spent: List[TxOut],
        hashtype: int = SIGHASH_ALL_UNIFIED,
        leaf_hash: Optional[bytes] = None,
    ) -> bytes:
        """Knots' SignatureHashUnified for a Taproot input, SIGHASH_ALL|UNIFIED only."""
        if hashtype != SIGHASH_ALL_UNIFIED:
            raise ValueError("only SIGHASH_ALL|SIGHASH_UNIFIED is supported")
        if len(spent) != len(self.inputs):
            raise ValueError("one spent output per input is required")
        sha_prevouts = sha256(b"".join(i.outpoint() for i in self.inputs))
        sha_amounts = sha256(b"".join(struct.pack("<q", o.value) for o in spent))
        sha_scripts = sha256(b"".join(ser_bytes(o.script_pubkey) for o in spent))
        sha_sequences = sha256(b"".join(struct.pack("<I", i.sequence) for i in self.inputs))
        sha_outputs = sha256(b"".join(o.serialize() for o in self.outputs))
        msg = bytes([0, hashtype]) + struct.pack("<I", self.version)
        msg += struct.pack("<I", self.locktime) + b"\x00"
        msg += sha_prevouts + sha_amounts + sha_scripts + sha_sequences + sha_outputs
        msg += bytes([3 if leaf_hash else 2])
        msg += struct.pack("<I", index)
        msg += b"\x00"
        if leaf_hash:
            msg += leaf_hash + b"\x00" + struct.pack("<I", 0xFFFFFFFF)
        return tagged_hash("UnifiedSighash", msg)


def schnorr_sign(sk: bytes, msg: bytes) -> bytes:
    import secp256k1

    key = secp256k1.PrivateKey(sk, raw=True)
    sig = key.schnorr_sign(msg, None, raw=True)
    if not M.schnorr_verify(msg, xonly(key.pubkey.serialize()), sig):
        raise RuntimeError("signature does not verify")
    return sig


def schnorr_verify(msg: bytes, xonly_pk: bytes, sig: bytes) -> bool:
    return M.schnorr_verify(msg, xonly_pk, sig)


def with_hashtype(sig: bytes, hashtype: int = SIGHASH_ALL_UNIFIED) -> bytes:
    if len(sig) != 64:
        raise ValueError("a Schnorr signature is 64 bytes")
    return sig + bytes([hashtype])


def keypath_witness(aggregate_sig: bytes) -> List[bytes]:
    return [with_hashtype(aggregate_sig)]


def leaf_witness(contract: EscrowContract, index: int, sigs: List[bytes]) -> List[bytes]:
    """sigs in the order the script consumes them from the stack top: the second key first."""
    return [with_hashtype(s) for s in sigs] + [contract.leaves[index], contract.control_block(index)]
