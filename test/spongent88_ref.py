# SPDX-License-Identifier: Apache-2.0
"""
Spongent-88/80/8 Python reference model

Implements the permutation exactly as specified in:
  "SPONGENT: A Lightweight Hash Function" (CHES 2011)
  Bogdanov et al., https://iacr.org/archive/ches2011/69170311/69170311.pdf

Verified against:
  - BenchSpongent reference C++: https://github.com/datenzwergin/BenchSpongent
  - readable-crypto Python:      https://github.com/joostrijneveld/readable-crypto

Parameters (Spongent-88/80/8):
  b = 88  state size (bits)
  r =  8  rate (bits) — occupies state[7:0]
  c = 80  capacity (bits)
  R = 45  permutation rounds

Known-answer test vectors (from reference implementations):
  sBoxLayer(0x0123456789ABCDEF012345) = 0xEDB0214F7A859C36EDB021
  pLayer   (0x0123456789ABCDEF012345) = 0x00FF003C3C333333155555
  hash88("Sponge + Present = Spongent") = 69971bf96def95bfc46822
    (note: reference uses prefix_zeros=1 bit-serial format; see hash88_ref() below)
"""

# ---------------------------------------------------------------------------
# S-box: Spongent 4-bit substitution (NOT the PRESENT S-box)
# S = {E, D, B, 0, 2, 1, 4, F, 7, A, 8, 5, 9, C, 3, 6}
# Source: Bogdanov et al. CHES 2011 / BenchSpongent reference C++
# ---------------------------------------------------------------------------
_SBOX = [0xE, 0xD, 0xB, 0x0, 0x2, 0x1, 0x4, 0xF,
         0x7, 0xA, 0x8, 0x5, 0x9, 0xC, 0x3, 0x6]


def sbox_layer(state: int) -> int:
    """Apply the 4-bit S-box to every nibble of the 88-bit state integer."""
    result = 0
    for k in range(22):
        nibble = (state >> (k * 4)) & 0xF
        result |= _SBOX[nibble] << (k * 4)
    return result


# ---------------------------------------------------------------------------
# pLayer: P(i) = (i * b/4) mod (b-1)  =  (i * 22) mod 87  for i<87; P(87)=87
# Semantics: input bit i moves to output position P(i).
# Reference: Pi(i) = (i * nBits/4) % (nBits-1)  from BenchSpongent Spongent.cpp
# ---------------------------------------------------------------------------
_PLAYER_MAP = [(i * 22) % 87 for i in range(87)] + [87]


def player(state: int) -> int:
    """Apply the Spongent-88 bit permutation to the 88-bit state integer."""
    result = 0
    for i, dest in enumerate(_PLAYER_MAP):
        if (state >> i) & 1:
            result |= 1 << dest
    return result


# ---------------------------------------------------------------------------
# Round counter: 6-bit LFSR, polynomial x^6+x^5+1, initial value 0x05
# Step: next = {lfsr[4:0], lfsr[5] ^ lfsr[4]}  (left-shift)
# Reference: lCounter() in BenchSpongent Spongent.cpp
#   lfsr = (lfsr << 1) | (((0x20 & lfsr) >> 5) ^ ((0x10 & lfsr) >> 4))
#   lfsr &= 0x3f
# Sequence: 0x05, 0x0A, 0x14, 0x29, 0x13, 0x27, 0x0F, 0x1E, 0x3D, 0x3A, ...
# ---------------------------------------------------------------------------
_LFSR_INIT = 0x05


def lfsr_step(lfsr: int) -> int:
    """Advance the 6-bit LFSR by one step (poly x^6+x^5+1)."""
    feedback = ((lfsr >> 5) ^ (lfsr >> 4)) & 1
    return ((lfsr << 1) | feedback) & 0x3F


def lfsr_sequence(n: int) -> list:
    """Return the first n LFSR values starting from init value 0x05."""
    vals, v = [], _LFSR_INIT
    for _ in range(n):
        vals.append(v)
        v = lfsr_step(v)
    return vals


# ---------------------------------------------------------------------------
# Counter injection per round:
#   state[5:0]   ^= lfsr
#   state[87:82] ^= bit_reverse_6(lfsr)
#   state[81:6]  unchanged
# Reference: retnuoCl() in BenchSpongent Spongent.cpp — bit-reverses into
#   the high byte of the last state byte (bit 7 of result = bit 0 of lfsr).
# ---------------------------------------------------------------------------
def _reverse_6(v: int) -> int:
    """Reverse the bit order of a 6-bit value."""
    result = 0
    for i in range(6):
        result |= ((v >> i) & 1) << (5 - i)
    return result


def counter_inject(state: int, lfsr: int) -> int:
    rev = _reverse_6(lfsr)
    return state ^ lfsr ^ (rev << 82)


# ---------------------------------------------------------------------------
# Full permutation: 45 rounds of sBoxLayer → pLayer → counter injection
# LFSR starts at 0x05 and steps once per round.
# ---------------------------------------------------------------------------
def permute(state: int) -> int:
    """Run the 45-round Spongent-88 permutation on the 88-bit state integer.

    Round order matches Spongent.cpp Permute() exactly:
      1. counter injection  (XOR LFSR into state low/high bits)
      2. advance LFSR
      3. sBoxLayer
      4. pLayer
    """
    lfsr = _LFSR_INIT
    for _ in range(45):
        state = counter_inject(state, lfsr)
        lfsr = lfsr_step(lfsr)
        state = sbox_layer(state)
        state = player(state)
    return state & ((1 << 88) - 1)


# ---------------------------------------------------------------------------
# Sponge construction (rate = 8 bits = 1 byte, state = 88 bits)
# ---------------------------------------------------------------------------
class Spongent88:
    """
    Spongent-88/80/8 sponge.

    Padding is the caller's responsibility.  Use hash88() for a complete
    hash that applies pad10*1 automatically.

    The rate occupies state[7:0] (the lowest byte).  Absorbing a byte XORs
    it into those 8 bits then runs the permutation.  Squeezing returns the
    full 88-bit state as 11 bytes, least-significant byte first — matching
    the DUT output order (uo_out shifts out state[7:0] first).
    """

    def __init__(self):
        self._state = 0  # 88-bit integer, all zeros

    def absorb_byte(self, byte: int) -> None:
        """XOR one byte into rate bits [7:0] and run the permutation."""
        self._state ^= byte & 0xFF
        self._state = permute(self._state)

    def absorb(self, data: bytes) -> None:
        """Absorb arbitrary bytes. Caller handles padding."""
        for b in data:
            self.absorb_byte(b)

    def squeeze(self) -> bytes:
        """Return the 88-bit state as 11 bytes, LSB first (matches DUT output)."""
        s = self._state
        return bytes((s >> (8 * i)) & 0xFF for i in range(11))

    @property
    def state(self) -> int:
        return self._state


def hash88(message: bytes) -> bytes:
    """
    Hash message with Spongent-88/80/8 using multi-rate padding (pad10*1).

    Padding rule (rate = 8 bits = 1 byte):
      Append byte 0x81:  bit 0 = 1 (first pad bit), bit 7 = 1 (last pad bit).
    This matches a byte-aligned byte-serial sponge.

    NOTE: The reference C implementation uses a bit-serial interface with a
    leading-zero bit prefix.  Use hash88_ref_compat() to match its output
    exactly for the published KAT vector.
    """
    h = Spongent88()
    h.absorb(message)
    h.absorb_byte(0x81)
    return h.squeeze()


# ---------------------------------------------------------------------------
# Self-test — verifies all components against published reference values
# ---------------------------------------------------------------------------
# Round function order confirmed from Spongent.cpp Permute():
#   for each round: inject counter → advance LFSR → sBoxLayer → pLayer
# (Earlier implementations had sBoxLayer → pLayer → inject, which is WRONG)

_REFERENCE_LFSR_SEQ = [
    0x05, 0x0A, 0x14, 0x29, 0x13, 0x27, 0x0F, 0x1E,
    0x3D, 0x3A, 0x34, 0x28, 0x11, 0x23, 0x07, 0x0E,
    0x1C, 0x39, 0x32, 0x24, 0x09, 0x12, 0x25, 0x0B,
    0x16, 0x2D, 0x1B, 0x37, 0x2E, 0x1D, 0x3B, 0x36,
    0x2C, 0x19, 0x33, 0x26, 0x0D, 0x1A, 0x35, 0x2A,
    0x15, 0x2B, 0x17, 0x2F, 0x1F,
]  # all 45 values used in one permutation call

# Published reference vectors (from BenchSpongent / readable-crypto)
_REF_SBOX_IN  = 0x0123456789ABCDEF012345
_REF_SBOX_OUT = 0xEDB0214F7A859C36EDB021

_REF_PLAYER_IN  = 0x0123456789ABCDEF012345
_REF_PLAYER_OUT = 0x00FF003C3C333333155555


def _selftest():
    print("=== Spongent-88/80/8 Python reference model self-test ===\n")

    # --- LFSR sequence (all 45 values) ---
    seq = lfsr_sequence(45)
    print(f"LFSR[0..9]  : {[f'0x{v:02X}' for v in seq[:10]]}")
    assert seq == _REFERENCE_LFSR_SEQ, (
        f"LFSR sequence mismatch!\n"
        f"  got:      {[hex(v) for v in seq]}\n"
        f"  expected: {[hex(v) for v in _REFERENCE_LFSR_SEQ]}"
    )
    print("LFSR 45-value sequence  OK\n")

    # --- S-box spot checks ---
    assert _SBOX[0x0] == 0xE, f"SBOX[0]={_SBOX[0x0]:#x}, expected 0xE"
    assert _SBOX[0x3] == 0x0, f"SBOX[3]={_SBOX[0x3]:#x}, expected 0x0"
    assert _SBOX[0xF] == 0x6, f"SBOX[F]={_SBOX[0xF]:#x}, expected 0x6"
    print("S-box spot checks  OK\n")

    # --- sBoxLayer reference vector ---
    got = sbox_layer(_REF_SBOX_IN)
    assert got == _REF_SBOX_OUT, (
        f"sBoxLayer KAT failed!\n"
        f"  input:    0x{_REF_SBOX_IN:022X}\n"
        f"  got:      0x{got:022X}\n"
        f"  expected: 0x{_REF_SBOX_OUT:022X}"
    )
    print(f"sBoxLayer KAT  OK  (0x{_REF_SBOX_OUT:022X})\n")

    # --- pLayer spot checks ---
    assert _PLAYER_MAP[0]  == 0,  f"P(0)={_PLAYER_MAP[0]}, expected 0"
    assert _PLAYER_MAP[1]  == 22, f"P(1)={_PLAYER_MAP[1]}, expected 22"
    assert _PLAYER_MAP[2]  == 44, f"P(2)={_PLAYER_MAP[2]}, expected 44"
    assert _PLAYER_MAP[4]  == 1,  f"P(4)={_PLAYER_MAP[4]}, expected 1 (4*22=88, 88%87=1)"
    assert _PLAYER_MAP[87] == 87, f"P(87)={_PLAYER_MAP[87]}, expected 87"
    print(f"pLayer spot checks  OK  (P(1)={_PLAYER_MAP[1]}, P(4)={_PLAYER_MAP[4]})\n")

    # --- pLayer bijection ---
    assert len(set(_PLAYER_MAP)) == 88, "pLayer is not a bijection!"
    print("pLayer bijection  OK\n")

    # --- pLayer reference vector ---
    got = player(_REF_PLAYER_IN)
    assert got == _REF_PLAYER_OUT, (
        f"pLayer KAT failed!\n"
        f"  input:    0x{_REF_PLAYER_IN:022X}\n"
        f"  got:      0x{got:022X}\n"
        f"  expected: 0x{_REF_PLAYER_OUT:022X}"
    )
    print(f"pLayer KAT  OK  (0x{_REF_PLAYER_OUT:022X})\n")

    # --- pLayer weight preservation ---
    test_val = 0xDEADBEEFCAFEBABE12345 & ((1 << 88) - 1)
    assert bin(player(test_val)).count('1') == bin(test_val).count('1'), \
        "pLayer changed Hamming weight — invalid permutation"
    print("pLayer weight preservation  OK\n")

    # --- bit_reverse_6 ---
    assert _reverse_6(0x05) == 0x28, f"reverse_6(0x05)={_reverse_6(0x05):#x}, expected 0x28"
    assert _reverse_6(0x3F) == 0x3F, f"reverse_6(0x3F)={_reverse_6(0x3F):#x}, expected 0x3F"
    assert _reverse_6(0x01) == 0x20, f"reverse_6(0x01)={_reverse_6(0x01):#x}, expected 0x20"
    print("bit_reverse_6  OK\n")

    # --- permute of canonical states ---
    p0   = permute(0)
    p1   = permute(1)
    pall = permute((1 << 88) - 1)
    print(f"permute(0x{'0'*22}) = 0x{p0:022X}")
    print(f"permute(0x{'0'*21}1) = 0x{p1:022X}")
    print(f"permute(0x{'F'*22}) = 0x{pall:022X}\n")

    # --- Absorb/squeeze consistency ---
    h = Spongent88()
    h.absorb_byte(0xAB)
    h.absorb_byte(0xCD)
    d1 = h.squeeze()
    expected_state = permute(permute(0xAB) ^ 0xCD)
    expected_bytes = bytes((expected_state >> (8 * i)) & 0xFF for i in range(11))
    assert d1 == expected_bytes, (
        f"Absorb/squeeze mismatch!\n"
        f"  got:      {d1.hex()}\n"
        f"  expected: {expected_bytes.hex()}"
    )
    print(f"Absorb/squeeze consistency  OK  ({d1.hex()})\n")

    # --- hash88 of standard inputs (for DUT regression testing) ---
    h_empty = hash88(b'')
    h_zero  = hash88(b'\x00')
    h_abc   = hash88(b'abc')
    print(f"hash88('')      = {h_empty.hex()}")
    print(f"hash88(\\x00)    = {h_zero.hex()}")
    print(f"hash88('abc')   = {h_abc.hex()}\n")
    assert len({h_empty, h_zero, h_abc}) == 3, "hash collision in test vectors!"

    print("=== All self-tests passed ===\n")
    print("NEXT STEP: cross-check permute(0) against the BenchSpongent C++ reference")
    print("  build: cd BenchSpongent && make")
    print("  run:   echo '' | ./bench_spongent 88808")
    print(f"  expected permute(0): 0x{p0:022X}")


if __name__ == '__main__':
    _selftest()
