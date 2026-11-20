"""Receive scripts as the coordinator shows them (0.9.9): the address of a scriptPubKey and its
type, for the admin panel and the Telegram alerts, without asking the node. P2PKH ("1…"),
P2SH ("3…"), segwit v0 ("bc1q…") and taproot ("bc1p…"); anything else is shown as hex."""

import hashlib

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _b58check(payload: bytes) -> str:
    data = payload + hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    pad = len(data) - len(data.lstrip(b"\x00"))
    return "1" * pad + out


def _polymod(values):
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (b >> i) & 1 else 0
    return chk


def _convertbits(data, frombits, tobits):
    acc, bits, ret, maxv = 0, 0, [], (1 << tobits) - 1
    for value in data:
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if bits:
        ret.append((acc << (tobits - bits)) & maxv)
    return ret


def _segwit(hrp: str, version: int, program: bytes) -> str:
    const = 1 if version == 0 else 0x2BC830A3
    data = [version] + _convertbits(program, 8, 5)
    hrp_exp = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    pm = _polymod(hrp_exp + data + [0] * 6) ^ const
    checksum = [(pm >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_BECH32[d] for d in data + checksum)


def script_type(spk: bytes) -> str:
    if len(spk) == 25 and spk[:3] == b"\x76\xa9\x14" and spk[23:] == b"\x88\xac":
        return "P2PKH"
    if len(spk) == 23 and spk[:2] == b"\xa9\x14" and spk[22:] == b"\x87":
        return "P2SH"
    if len(spk) == 22 and spk[:2] == b"\x00\x14":
        return "P2WPKH"
    if len(spk) == 34 and spk[:2] == b"\x00\x20":
        return "P2WSH"
    if len(spk) == 34 and spk[:2] == b"\x51\x20":
        return "P2TR"
    return ""


def script_to_address(spk: bytes, hrp: str = "bc") -> str:
    """The address of a receive script on the network of 'hrp' ("bc": mainnet versions 0x00 /
    0x05; any other: testnet and regtest 0x6f / 0xc4); "" when it is no standard address."""
    t = script_type(spk)
    main = hrp == "bc"
    if t == "P2PKH":
        return _b58check(bytes([0x00 if main else 0x6F]) + spk[3:23])
    if t == "P2SH":
        return _b58check(bytes([0x05 if main else 0xC4]) + spk[2:22])
    if t in ("P2WPKH", "P2WSH"):
        return _segwit(hrp, 0, spk[2:])
    if t == "P2TR":
        return _segwit(hrp, 1, spk[2:])
    return ""
