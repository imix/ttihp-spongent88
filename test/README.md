# Spongent-88 Verification Suite

This directory contains the verification suite for the Spongent-88/80/8 hash accelerator. It uses [cocotb](https://docs.cocotb.org/en/stable/) and [Icarus Verilog](http://iverilog.icarus.com/) to perform cycle-accurate RTL and Gate-Level simulations.

## 📁 Files

*   **`test.py`**: The main cocotb test suite (9 test cases).
*   **`spongent88_ref.py`**: A pure Python golden-reference model of the Spongent-88 algorithm.
*   **`spongent88_readable_crypto.py`**: An independent reference implementation (joostrijneveld/readable-crypto) used for cross-checking the golden model.
*   **`tb.v`**: The Verilog testbench wrapper that instantiates the TinyTapeout module.

## 🚀 How to Run

### RTL Simulation
To run the standard RTL simulation and verify the logic:
```bash
make
```

### Waveform Analysis
To generate a waveform dump (`tb.fst`) for analysis in GTKWave or Surfer:
```bash
make WAVES=1
gtkwave tb.fst tb.gtkw
```

### Gate-Level Simulation (GLS)
After hardening the design with OpenLane/LibreLane, you can verify the synthesized netlist:
```bash
make GATES=yes
```

## 🔍 Test Cases
The suite performs the following checks:
1.  **Single-byte Absorb**: Verifies hashing of standard 8-bit inputs.
2.  **Multi-byte Sequences**: Tests the sponge "absorbing" phase with variable-length messages.
3.  **Cycle-Accurate Timing**: Asserts exactly 25 cycles per absorb (1 load + 23 permutation + 1 capture).
4.  **Hardware Flags**: Verifies `busy` and `out_valid` signal transitions.
5.  **Reset Persistence**: Confirms that a hardware/software reset clears the internal state.
6.  **Collision Prevention**: Ensures that writes during a busy state are ignored.
7.  **KAT Validation**: Directly compares intermediate layers (S-Box, pLayer, LFSR) against published Known-Answer Tests.
8.  **Reference Cross-Check**: Validates the internal Python model against the `readable-crypto` implementation.
9.  **Hardware Padding**: Verifies that `CMD=2` correctly applies the `0x81` pad and auto-squeezes.

---
*Maintained by Stefan Aeschbacher.*
