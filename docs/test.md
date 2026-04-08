# Testing

## Simulation (cocotb + Icarus Verilog)

```bash
cd test
pip install -r requirements.txt
make          # RTL simulation
make WAVES=1  # also generate FST waveform (open with GTKWave or Surfer)
```

Nine test cases execute automatically.  All pass against the Python reference
model (`spongent88_ref.py`) which has been independently verified against the
BenchSpongent C reference and joostrijneveld/readable-crypto.

---

## Test sequences

### Core functional tests (1–6)

| # | Test name | What it checks | Pass criterion |
|---|-----------|----------------|----------------|
| 1 | `test_single_byte_absorb` | Absorb each of `0x00 0x01 0x80 0xFF 0xA5 0x5A`, then squeeze | DUT digest matches Python reference for all 6 inputs |
| 2 | `test_multi_byte_absorb` | Absorb byte sequences of 2, 3, 4, and 11 bytes | DUT digest matches reference after each multi-byte absorb |
| 3 | `test_absorb_timing` | Measure clock cycles from write strobe to `busy` falling | Exactly **25 cycles** (1 load + 23 permutation rounds + 1 capture) |
| 4 | `test_out_valid_flag` | Monitor `out_valid` through reset → absorb → squeeze → reset | `out_valid=0` after reset; `out_valid=0` after absorb; `out_valid=1` after squeeze; `out_valid=0` after CMD reset |
| 5 | `test_reset_clears_state` | Absorb `0xBE 0xEF`, reset, absorb same sequence again | Both runs produce identical digests; one-byte absorb gives different digest |
| 6 | `test_absorb_while_busy_ignored` | Issue second ABSORB while `busy=1` | Second write is silently dropped; digest equals absorb of `0x11` only |

### KAT and reference cross-check tests (7–9)

| # | Test name | What it checks | Pass criterion |
|---|-----------|----------------|----------------|
| 7 | `test_reference_kat_components` | Python model S-box, pLayer and LFSR against published vectors; then DUT against validated model | All three KAT checks pass; DUT digest matches model on `0xA5` |
| 8 | `test_vs_readable_crypto_reference` | Our Python model against joostrijneveld/readable-crypto at every level | `sBoxLayer`, `pLayer`, `permute()`, and sponge absorb/squeeze all agree |
| 9 | `test_hash_command` | CMD=2 absorbs pad `0x81` and auto-squeezes for empty, 1-byte, 3-byte and 4-byte messages | `out_valid` set; digest matches `hash88()` from reference model |

---

## Known-answer test vectors

### Primitive component KATs

| Operation | Input (88-bit) | Expected output (88-bit) |
|-----------|---------------|--------------------------|
| `sBoxLayer` | `0x0123456789ABCDEF012345` | `0xEDB0214F7A859C36EDB021` |
| `pLayer` | `0x0123456789ABCDEF012345` | `0x00FF003C3C333333155555` |
| LFSR[0..4] | — | `0x05, 0x0A, 0x14, 0x29, 0x13` |

### Hash KAT vectors

Single-byte absorb, no padding, squeeze full 88-bit state (LSB-first, 11 bytes):

| Input byte | Digest (hex) |
|-----------|--------------|
| `0x00` | `82f3cecf167feb3981c07c` |
| `0x01` | `0842dc1b6c7399eb92f540` |
| `0x80` | `a0623e32cd5a6bba0b304f` |
| `0xFF` | `fe511649a2fa375bf97aa3` |
| `0xA5` | `82b032622cbefe65b01911` |

---

## Standalone reference model

Run without a simulator to print LFSR sequence, S-box checks, pLayer KAT and
digest values for standard inputs — useful for catching spec mismatches before
simulation:

```bash
cd test
python3 spongent88_ref.py
```

---

## Hardware test (TinyTapeout demo board)

Connect the demo board.  The chip runs at 50 MHz.

**Register interface:**

| `uio[2:0]` addr | Write data | Effect |
|-----------------|-----------|--------|
| `0` | `0x00` | CMD reset — zero sponge state, clear `out_valid` |
| `0` | `0x01` | CMD squeeze — latch 88-bit digest into output shift register |
| `0` | `0x02` | CMD hash — absorb pad `0x81`, auto-squeeze |
| `1` | byte | ABSORB — XOR byte into `state[7:0]`, run 45-round permutation |
| `2` | (read strobe) | RD\_ADV — advance output shift register by one byte |

**Status flags** (`uio_out`)

| Bit | Signal | Description |
|-----|--------|-------------|
| 0 | `busy` | High while permutation is running; host must poll before next command |
| 1 | `out_valid` | High after squeeze until next reset |

**Example — hash `0xAB 0xCD 0xEF` with hardware padding:**

```python
write_reg(0, 0)   # reset
absorb(0xAB)      # poll busy after each absorb
absorb(0xCD)
absorb(0xEF)
write_reg(0, 2)   # CMD hash: absorbs 0x81, auto-squeezes
while read_busy():
    pass

digest = []
for i in range(11):
    digest.append(read_uo_out())
    if i < 10:
        advance_output()   # RD_ADV
print(bytes(digest).hex())
```

**Manual squeeze (no padding):**

```python
write_reg(0, 0)   # reset
absorb(byte)      # absorb one or more bytes
write_reg(0, 1)   # CMD squeeze

digest = []
for i in range(11):
    digest.append(read_uo_out())
    if i < 10:
        advance_output()
print(bytes(digest).hex())
```

---

## Waveform inspection

```bash
cd test
make WAVES=1
# open test/tb.fst in GTKWave using the provided view file:
gtkwave tb.fst tb.gtkw
```

Signals of interest:

| Signal | Purpose |
|--------|---------|
| `uio_out[0]` (`busy`) | Goes high for 23 cycles during each permutation |
| `uio_out[1]` (`out_valid`) | Set by squeeze, cleared by reset |
| `uo_out[7:0]` | Current digest byte on the output shift register |
| `spongent88_core.state` | Internal 88-bit permutation state |
| `spongent88_core.round` | Round counter (counts down from 44 to 0) |

---

## Performance summary

| Parameter | Value |
|-----------|-------|
| Clock frequency | 50 MHz |
| Cycles per permutation | 23 (2-round unrolled) |
| Cycles per absorb | 25 (1 load + 23 rounds + 1 capture) |
| Throughput | ~2.0 MB/s (1 byte per 25 cycles at 50 MHz) |
| W-OTS signature time | ~190 µs (375 permutations for 25 chains × 15 steps) |
