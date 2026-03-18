# SPDX-FileCopyrightText: © 2024 Tiny Tapeout
# SPDX-License-Identifier: Apache-2.0
"""
Cocotb tests for tt_um_spongent88 (Spongent-88 hash accelerator)

The golden reference for all digest comparisons is spongent88_ref.py.
That Python model must be independently verified against the official
Spongent C reference implementation before tapeout.

I/O protocol recap
------------------
  ui_in[7:0]  — data byte written to chip
  uo_out[7:0] — current digest byte (LSB-first)

  uio_in[2:0] — register address
  uio_in[3]   — write strobe (rising-edge triggered)
  uio_in[4]   — read  strobe (rising-edge triggered, addr=2 advances output)

  uio_out[0]  — busy      (1 while permutation running)
  uio_out[1]  — out_valid (1 after squeeze, until next reset)

Register map:
  addr=0, data=0  → CMD reset:   zero sponge state, clear out_valid
  addr=0, data=1  → CMD squeeze: latch digest to output shift register
  addr=1, data=b  → ABSORB:      XOR b into state[7:0], run 45-round permutation
  addr=2 (rd)     → RD_ADV:      advance output shift register by one byte

Timing:
  One absorb = 47 clock cycles from the write strobe to busy going low.
  (1 to load state + 45 permutation rounds + 1 to capture result)
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

from spongent88_ref import (
    Spongent88, permute, sbox_layer, player,
    _REF_SBOX_IN, _REF_SBOX_OUT,
    _REF_PLAYER_IN, _REF_PLAYER_OUT,
    _REFERENCE_LFSR_SEQ, lfsr_sequence,
)
from spongent88_readable_crypto import SPONGENT as _RefSPONGENT

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CLOCK_PERIOD_NS = 20   # 50 MHz
ABSORB_CYCLES   = 47   # expected cycles per absorb (see project.v timing notes)

# uio_in bit positions
WR_EN = 0x08   # bit 3
RD_EN = 0x10   # bit 4

# ---------------------------------------------------------------------------
# Low-level bus helpers
# ---------------------------------------------------------------------------

def _start_clock(dut):
    clock = Clock(dut.clk, CLOCK_PERIOD_NS, units="ns")
    cocotb.start_soon(clock.start())


async def _hw_reset(dut):
    """Hold rst_n low for 5 cycles then release."""
    dut.ena.value    = 1
    dut.ui_in.value  = 0
    dut.uio_in.value = 0
    dut.rst_n.value  = 0
    await ClockCycles(dut.clk, 5)
    dut.rst_n.value  = 1
    await ClockCycles(dut.clk, 2)


async def _write_reg(dut, addr: int, data: int):
    """
    Perform one register write transaction.

    Timing (3 clock cycles consumed):
      Cycle 0: addr on bus, wr_en=0  → wr_prev latches 0
      Cycle 1: addr on bus, wr_en=1  → wr_rise=1, FSM processes command (NBAs scheduled)
      Cycle 2: settle — registered outputs (busy, out_valid, out_shreg) are now
               visible in the active region of this edge (NBAs from cycle 1 applied)
    After return: wr_en deasserted, all outputs readable.

    Note: cocotb's RisingEdge fires in the active region, before non-blocking
    assignments (NBAs) are applied.  One extra cycle is needed so that registers
    updated at cycle 1 (e.g. busy, out_shreg) are already latched when callers
    read them.
    """
    assert 0 <= addr <= 7
    assert 0 <= data <= 255
    dut.ui_in.value  = data
    dut.uio_in.value = addr           # wr_en=0: let wr_prev settle to 0
    await RisingEdge(dut.clk)
    dut.uio_in.value = addr | WR_EN   # wr_en=1: wr_rise fires at next edge
    await RisingEdge(dut.clk)         # FSM processes command here (NBAs scheduled)
    dut.uio_in.value = addr           # deassert wr_en
    await RisingEdge(dut.clk)         # settle: registered outputs now visible


async def _advance_output(dut):
    """
    Assert rd_en at addr=2 for one cycle to shift output register forward.
    Consumes 2 clock cycles (assert then deassert so rd_prev resets).
    """
    dut.uio_in.value = 2 | RD_EN     # rd_en=1, addr=2: rd_rise fires
    await RisingEdge(dut.clk)         # shift register advances
    dut.uio_in.value = 0              # rd_en=0
    await RisingEdge(dut.clk)         # rd_prev resets to 0


# ---------------------------------------------------------------------------
# Sponge-level helpers
# ---------------------------------------------------------------------------

async def _sponge_reset(dut):
    """Issue CMD=0 to zero the sponge state and clear out_valid."""
    await _write_reg(dut, 0, 0)


async def _absorb_byte(dut, byte: int):
    """
    Absorb one byte and wait for the permutation to complete.
    Returns the number of cycles waited (should be ABSORB_CYCLES).
    """
    await _write_reg(dut, 1, byte)   # busy goes high after this
    cycles = 0
    while dut.uio_out.value.integer & 0x1:   # poll busy (bit 0)
        await RisingEdge(dut.clk)
        cycles += 1
        assert cycles <= 100, f"busy stuck high after absorbing {byte:#04x}"
    return cycles


async def _squeeze(dut) -> bytes:
    """
    Issue squeeze command and read back all 11 digest bytes.
    Returns bytes in LSB-first order (digest[7:0], digest[15:8], ...).
    """
    await _write_reg(dut, 0, 1)   # CMD squeeze: out_shreg ← sponge state
    # After _write_reg the output shift register is loaded.
    # uo_out already shows byte 0.
    result = []
    for i in range(11):
        result.append(dut.uo_out.value.integer)
        if i < 10:
            await _advance_output(dut)   # advance to next byte
    return bytes(result)


def _ref_squeeze(ref: Spongent88) -> bytes:
    """Return the reference model's current state as bytes (matches _squeeze)."""
    return ref.squeeze()


# ---------------------------------------------------------------------------
# Test 1 — single-byte absorb vs reference model
# ---------------------------------------------------------------------------
@cocotb.test()
async def test_single_byte_absorb(dut):
    """Absorb one byte and verify the digest matches the Python reference."""
    _start_clock(dut)
    await _hw_reset(dut)

    test_bytes = [0x00, 0x01, 0x80, 0xFF, 0xA5, 0x5A]

    for byte in test_bytes:
        dut._log.info(f"Testing absorb of 0x{byte:02X}")

        await _sponge_reset(dut)

        # DUT
        await _absorb_byte(dut, byte)
        dut_digest = await _squeeze(dut)

        # Reference
        ref = Spongent88()
        ref.absorb_byte(byte)
        ref_digest = ref.squeeze()

        assert dut_digest == ref_digest, (
            f"Digest mismatch for byte 0x{byte:02X}:\n"
            f"  DUT: {dut_digest.hex()}\n"
            f"  REF: {ref_digest.hex()}"
        )
        dut._log.info(f"  digest = {dut_digest.hex()}  OK")


# ---------------------------------------------------------------------------
# Test 2 — multi-byte absorb vs reference model
# ---------------------------------------------------------------------------
@cocotb.test()
async def test_multi_byte_absorb(dut):
    """Absorb several byte sequences and compare to reference."""
    _start_clock(dut)
    await _hw_reset(dut)

    sequences = [
        b'\x00\x00',
        b'\x01\x02\x03',
        b'\xDE\xAD\xBE\xEF',
        b'\xAB\xCD\xEF\x01\x23\x45\x67\x89\xAB\xCD\xEF',  # 11 bytes
    ]

    for msg in sequences:
        dut._log.info(f"Testing absorb of {msg.hex()}")

        await _sponge_reset(dut)

        # DUT
        for b in msg:
            await _absorb_byte(dut, b)
        dut_digest = await _squeeze(dut)

        # Reference
        ref = Spongent88()
        ref.absorb(msg)
        ref_digest = ref.squeeze()

        assert dut_digest == ref_digest, (
            f"Digest mismatch for {msg.hex()!r}:\n"
            f"  DUT: {dut_digest.hex()}\n"
            f"  REF: {ref_digest.hex()}"
        )
        dut._log.info(f"  digest = {dut_digest.hex()}  OK")


# ---------------------------------------------------------------------------
# Test 3 — timing: absorb must complete in exactly ABSORB_CYCLES cycles
# ---------------------------------------------------------------------------
@cocotb.test()
async def test_absorb_timing(dut):
    """Verify that one absorb takes exactly 47 clock cycles."""
    _start_clock(dut)
    await _hw_reset(dut)
    await _sponge_reset(dut)

    # Issue absorb and count cycles until busy falls
    await _write_reg(dut, 1, 0x42)   # busy goes high after this edge

    cycles = 0
    while dut.uio_out.value.integer & 0x1:
        await RisingEdge(dut.clk)
        cycles += 1
        assert cycles <= 100, "busy stuck high — timing test failed"

    assert cycles == ABSORB_CYCLES, (
        f"Expected {ABSORB_CYCLES} cycles for absorb, measured {cycles}"
    )
    dut._log.info(f"Absorb timing: {cycles} cycles  OK")


# ---------------------------------------------------------------------------
# Test 4 — out_valid flag
# ---------------------------------------------------------------------------
@cocotb.test()
async def test_out_valid_flag(dut):
    """out_valid must be 0 before squeeze and 1 after."""
    _start_clock(dut)
    await _hw_reset(dut)
    await _sponge_reset(dut)

    # After reset: out_valid=0
    assert not (dut.uio_out.value.integer & 0x2), \
        "out_valid should be 0 after reset"

    # After absorb: still 0
    await _absorb_byte(dut, 0x00)
    assert not (dut.uio_out.value.integer & 0x2), \
        "out_valid should be 0 before squeeze"

    # After squeeze: 1
    await _write_reg(dut, 0, 1)
    assert dut.uio_out.value.integer & 0x2, \
        "out_valid should be 1 after squeeze"

    # After CMD reset: 0 again
    await _write_reg(dut, 0, 0)
    assert not (dut.uio_out.value.integer & 0x2), \
        "out_valid should be 0 after CMD reset"

    dut._log.info("out_valid flag behaviour OK")


# ---------------------------------------------------------------------------
# Test 5 — reset clears state
# ---------------------------------------------------------------------------
@cocotb.test()
async def test_reset_clears_state(dut):
    """CMD reset must zero the sponge state; subsequent absorbs restart clean."""
    _start_clock(dut)
    await _hw_reset(dut)

    # Absorb something, squeeze, record
    await _sponge_reset(dut)
    await _absorb_byte(dut, 0xBE)
    await _absorb_byte(dut, 0xEF)
    await _write_reg(dut, 0, 1)
    digest_a = await _squeeze(dut)

    # Reset and repeat exactly the same sequence
    await _sponge_reset(dut)
    await _absorb_byte(dut, 0xBE)
    await _absorb_byte(dut, 0xEF)
    await _write_reg(dut, 0, 1)
    digest_b = await _squeeze(dut)

    assert digest_a == digest_b, (
        f"Same input after reset gave different digest!\n"
        f"  run 1: {digest_a.hex()}\n"
        f"  run 2: {digest_b.hex()}"
    )

    # Reset and absorb only one byte — must differ from two-byte digest
    await _sponge_reset(dut)
    await _absorb_byte(dut, 0xBE)
    await _write_reg(dut, 0, 1)
    digest_c = await _squeeze(dut)

    assert digest_a != digest_c, \
        "One-byte and two-byte digests are identical — reset may not be clearing state"

    dut._log.info(f"Reset clears state OK  (one-byte={digest_c.hex()}, two-byte={digest_a.hex()})")


# ---------------------------------------------------------------------------
# Test 6 — busy ignored while permutation is running
# ---------------------------------------------------------------------------
@cocotb.test()
async def test_absorb_while_busy_ignored(dut):
    """Writing ABSORB while busy must be silently ignored."""
    _start_clock(dut)
    await _hw_reset(dut)
    await _sponge_reset(dut)

    # Start an absorb, then immediately try another one while busy
    await _write_reg(dut, 1, 0x11)   # start absorb — FSM enters ST_PERM

    # Try to write another absorb while busy (should be ignored)
    # Don't wait for busy to clear — write directly
    await _write_reg(dut, 1, 0xFF)   # must be ignored by FSM in ST_PERM

    # Wait for original absorb to complete
    for _ in range(100):
        await RisingEdge(dut.clk)
        if not (dut.uio_out.value.integer & 0x1):
            break
    else:
        assert False, "busy never cleared — DUT may have hung"

    await _write_reg(dut, 0, 1)
    dut_digest = await _squeeze(dut)

    # Reference: only 0x11 was absorbed (the 0xFF write was ignored)
    ref = Spongent88()
    ref.absorb_byte(0x11)
    ref_digest = ref.squeeze()

    assert dut_digest == ref_digest, (
        f"Absorb-while-busy was not ignored!\n"
        f"  DUT: {dut_digest.hex()}\n"
        f"  REF (only 0x11): {ref_digest.hex()}"
    )
    dut._log.info("Absorb-while-busy ignored  OK")


# ---------------------------------------------------------------------------
# Test 7 — reference KAT: sBoxLayer and pLayer intermediate vectors
#
# These are published vectors from the BenchSpongent reference implementation
# and joostrijneveld/readable-crypto.  They test the two most bug-prone
# components (S-box values and pLayer multiplier) independently of the full
# permutation, making it easy to localise failures.
#
# The DUT does not expose sBoxLayer or pLayer directly, so we verify them
# indirectly: a single absorb of a known input followed by a squeeze gives
# a digest whose value depends on every component of the round function.
# The reference Python model (already validated against these KATs) generates
# the expected digest, so any mismatch isolates a discrepancy in the Verilog.
#
# Direct KAT vectors tested here via the Python model (not via DUT I/O):
#   sBoxLayer(0x0123456789ABCDEF012345) = 0xEDB0214F7A859C36EDB021
#   pLayer   (0x0123456789ABCDEF012345) = 0x00FF003C3C333333155555
#   LFSR[0..44] matches full published 45-value sequence
# ---------------------------------------------------------------------------
@cocotb.test()
async def test_reference_kat_components(dut):
    """Verify Python model KAT vectors, then confirm DUT matches the model."""
    # ---- Part A: Python model self-checks (no DUT needed) ----------------
    # sBoxLayer KAT
    got_sbox = sbox_layer(_REF_SBOX_IN)
    assert got_sbox == _REF_SBOX_OUT, (
        f"sBoxLayer KAT FAILED in Python model!\n"
        f"  input:    0x{_REF_SBOX_IN:022X}\n"
        f"  got:      0x{got_sbox:022X}\n"
        f"  expected: 0x{_REF_SBOX_OUT:022X}\n"
        f"  (This means the S-box in spongent88_ref.py is wrong.)"
    )
    dut._log.info(f"sBoxLayer KAT  OK  0x{_REF_SBOX_OUT:022X}")

    # pLayer KAT
    got_player = player(_REF_PLAYER_IN)
    assert got_player == _REF_PLAYER_OUT, (
        f"pLayer KAT FAILED in Python model!\n"
        f"  input:    0x{_REF_PLAYER_IN:022X}\n"
        f"  got:      0x{got_player:022X}\n"
        f"  expected: 0x{_REF_PLAYER_OUT:022X}\n"
        f"  (Likely cause: wrong multiplier in P(i) = (i * M) mod 87.)"
    )
    dut._log.info(f"pLayer KAT  OK  0x{_REF_PLAYER_OUT:022X}")

    # LFSR sequence KAT (all 45 values)
    seq = lfsr_sequence(45)
    assert seq == _REFERENCE_LFSR_SEQ, (
        f"LFSR sequence KAT FAILED!\n"
        f"  got:      {[hex(v) for v in seq]}\n"
        f"  expected: {[hex(v) for v in _REFERENCE_LFSR_SEQ]}"
    )
    dut._log.info("LFSR 45-value sequence  OK")

    # ---- Part B: DUT absorbs the KAT input, must match Python model ------
    _start_clock(dut)
    await _hw_reset(dut)
    await _sponge_reset(dut)

    # Load the 88-bit KAT input (0x0123456789ABCDEF012345) byte by byte,
    # LSB first (byte 0 = 0x45, byte 1 = 0x23, ..., byte 10 = 0x01).
    kat_input = _REF_SBOX_IN  # = 0x0123456789ABCDEF012345
    kat_bytes = bytes((kat_input >> (8 * i)) & 0xFF for i in range(11))

    # We cannot absorb 11 bytes independently and get the same result as
    # a single permute() call, because absorb XORs into the rate (1 byte)
    # then permutes after each byte.  Instead, we verify using a 1-byte
    # absorb that exercises all three components together:
    test_byte = 0xA5
    await _absorb_byte(dut, test_byte)
    dut_digest = await _squeeze(dut)

    ref = Spongent88()
    ref.absorb_byte(test_byte)
    ref_digest = ref.squeeze()

    assert dut_digest == ref_digest, (
        f"DUT digest mismatch on reference-validated input 0x{test_byte:02X}!\n"
        f"  DUT: {dut_digest.hex()}\n"
        f"  REF: {ref_digest.hex()}\n"
        f"  (The Python model passed all KAT checks above, so this is a\n"
        f"   discrepancy in the Verilog S-box, pLayer, LFSR, or counter.)"
    )
    dut._log.info(f"DUT matches Python model on 0x{test_byte:02X}  OK  {dut_digest.hex()}")


# ---------------------------------------------------------------------------
# Test 8 — cross-check our Python model against joostrijneveld/readable-crypto
#
# readable-crypto/SPONGENT.py is an independent implementation of the same
# spec, vendored verbatim as spongent88_readable_crypto.py.  This test drives
# both models with identical inputs at every level (sBoxLayer, pLayer, full
# permutation, and sponge absorb/squeeze) and asserts they agree.  No DUT
# interaction is needed.
# ---------------------------------------------------------------------------
@cocotb.test()
async def test_vs_readable_crypto_reference(dut):
    """Cross-check our Python model against the readable-crypto reference implementation."""
    ref = _RefSPONGENT(n=88, c=80, r=8, R=45)

    # ---- sBoxLayer ----
    for state in [0, 0x0123456789ABCDEF012345, (1 << 88) - 1, 0xDEADBEEFCAFEBABE12345]:
        state &= (1 << 88) - 1
        ours   = sbox_layer(state)
        theirs = ref.sBoxLayer(state)
        assert ours == theirs, (
            f"sBoxLayer mismatch at state=0x{state:022X}:\n"
            f"  ours:   0x{ours:022X}\n"
            f"  theirs: 0x{theirs:022X}"
        )
    dut._log.info("sBoxLayer vs readable-crypto  OK")

    # ---- pLayer ----
    for state in [0, 0x0123456789ABCDEF012345, (1 << 88) - 1, 0xA5A5A5A5A5A5A5A5A5A5A5 & ((1 << 88) - 1)]:
        ours   = player(state)
        theirs = ref.pLayer(state)
        assert ours == theirs, (
            f"pLayer mismatch at state=0x{state:022X}:\n"
            f"  ours:   0x{ours:022X}\n"
            f"  theirs: 0x{theirs:022X}"
        )
    dut._log.info("pLayer vs readable-crypto  OK")

    # ---- full permutation ----
    test_states = [
        0,
        1,
        0xA5 ,
        0x0123456789ABCDEF012345,
        (1 << 88) - 1,
        0xDEADBEEF00000000000000 & ((1 << 88) - 1),
    ]
    for state in test_states:
        ours   = permute(state)
        theirs = ref.P(state)
        assert ours == theirs, (
            f"permute() mismatch at state=0x{state:022X}:\n"
            f"  ours:   0x{ours:022X}\n"
            f"  theirs: 0x{theirs:022X}"
        )
    dut._log.info("permute() vs readable-crypto  OK")

    # ---- sponge: byte-level absorb/squeeze (bypassing padding) ----
    sequences = [b'\x00', b'\xA5', b'\x01\x02\x03', b'\xDE\xAD\xBE\xEF']
    for msg in sequences:
        # Our model
        h = Spongent88()
        h.absorb(msg)
        ours = h.state

        # readable-crypto: manually XOR each byte into state[7:0] and call P()
        s = 0
        for byte in msg:
            s ^= byte          # XOR into rate (low 8 bits = state[7:0])
            s = ref.P(s)
        theirs = s

        assert ours == theirs, (
            f"Sponge absorb mismatch for {msg.hex()!r}:\n"
            f"  ours:   0x{ours:022X}\n"
            f"  theirs: 0x{theirs:022X}"
        )
    dut._log.info("Sponge absorb vs readable-crypto  OK")

    dut._log.info("All readable-crypto cross-checks passed")
