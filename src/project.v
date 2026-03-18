/*
 * Copyright (c) 2024 Your Name
 * SPDX-License-Identifier: Apache-2.0
 *
 * Spongent-88 hash accelerator — TinyTapeout top module
 *
 * Implements the Spongent-88/80/8 sponge construction as a byte-serial
 * hardware accelerator.  The host XORs message bytes into the rate and
 * triggers the permutation one byte at a time; after all bytes (including
 * any padding) have been absorbed the host issues a squeeze command to
 * latch the 88-bit digest and then reads it back one byte at a time.
 *
 * I/O pin assignment
 * ------------------
 *  ui_in[7:0]   data byte written to the chip (latched on wr_rise)
 *  uo_out[7:0]  current digest byte (LSB-first; advances on rd_rise/addr=2)
 *
 *  uio_in[2:0]  register address
 *                 0 = CMD    write 0→reset sponge  write 1→squeeze
 *                 1 = ABSORB write ui_in byte into rate[7:0], run permutation
 *                 2 = RD_ADV read-strobe: advance output shift-register by 1 byte
 *  uio_in[3]    write strobe  (rising-edge triggered)
 *  uio_in[4]    read  strobe  (rising-edge triggered, addr must be 2)
 *  uio_in[7:5]  unused
 *
 *  uio_out[0]   busy      — 1 while permutation is running
 *  uio_out[1]   out_valid — 1 after squeeze, until next reset
 *  uio_out[7:2] driven 0
 *
 * Typical host sequence
 * ---------------------
 *  1. wr addr=0, data=0          → reset sponge state
 *  2. for each message byte b:
 *       wr addr=1, data=b        → absorb (XOR + permute, 45 cycles)
 *       poll uio_out[0] until 0  → wait for busy to clear
 *  3. (host appends padding bytes the same way, per sponge spec)
 *  4. wr addr=0, data=1          → squeeze: latch 88-bit digest
 *  5. read uo_out                → byte 0 of digest (state[7:0])
 *     wr addr=2, data=x          → advance to byte 1
 *     read uo_out                → byte 1 of digest (state[15:8])
 *     ... repeat 9 more times for bytes 2–10
 *
 * Timing (50 MHz clock)
 *   One absorb = 1 (load) + 45 (permutation) + 1 (capture) = 47 cycles ≈ 940 ns
 */

`default_nettype none

module tt_um_winternitz_ots (
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);

    // uio[1:0] are driven outputs; uio[7:2] are inputs
    assign uio_oe = 8'b0000_0011;

    // ------------------------------------------------------------------
    // Bus decode
    // ------------------------------------------------------------------
    wire [2:0] addr  = uio_in[2:0];
    wire       wr_en = uio_in[3];
    wire       rd_en = uio_in[4];

    // Rising-edge detection (registered to avoid glitches)
    reg wr_prev, rd_prev;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            wr_prev <= 1'b0;
            rd_prev <= 1'b0;
        end else begin
            wr_prev <= wr_en;
            rd_prev <= rd_en;
        end
    end

    wire wr_rise = wr_en & ~wr_prev;
    wire rd_rise = rd_en & ~rd_prev;

    // ------------------------------------------------------------------
    // Spongent-88 core
    // ------------------------------------------------------------------
    reg  [87:0] core_in;
    wire [87:0] core_out;
    reg         core_start;
    wire        core_busy;

    spongent88_core u_spongent (
        .clk      (clk),
        .rst_n    (rst_n),
        .start    (core_start),
        .busy     (core_busy),
        .state_in (core_in),
        .state_out(core_out)
    );

    // ------------------------------------------------------------------
    // Sponge state and output buffer
    // ------------------------------------------------------------------
    reg [87:0] sponge;     // current sponge state (all-zero after reset)
    reg [87:0] out_shreg;  // digest shift register loaded at squeeze time
    reg        out_valid;  // set by squeeze, cleared by reset
    reg        busy;       // reflects ongoing permutation

    // perm_active: prevents spurious early exit from ST_PERM.
    // core_busy goes high one cycle after core_start; this flag ensures we
    // stay in ST_PERM until core_busy has been observed high at least once.
    reg perm_active;

    // ------------------------------------------------------------------
    // FSM
    // ------------------------------------------------------------------
    localparam ST_IDLE = 1'b0;
    localparam ST_PERM = 1'b1;
    reg fsm;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            sponge      <= 88'b0;
            out_shreg   <= 88'b0;
            out_valid   <= 1'b0;
            busy        <= 1'b0;
            perm_active <= 1'b0;
            core_start  <= 1'b0;
            core_in     <= 88'b0;
            fsm         <= ST_IDLE;

        end else begin
            core_start <= 1'b0;  // default: no start pulse this cycle

            case (fsm)

                ST_IDLE: begin
                    if (wr_rise) begin
                        case (addr)

                            3'd0: begin
                                // CMD register
                                case (ui_in[1:0])
                                    2'd0: begin  // reset: zero the sponge state
                                        sponge    <= 88'b0;
                                        out_valid <= 1'b0;
                                    end
                                    2'd1: begin  // squeeze: latch digest to output buffer
                                        out_shreg <= sponge;
                                        out_valid <= 1'b1;
                                    end
                                    default: ;
                                endcase
                            end

                            3'd1: begin
                                // ABSORB: XOR ui_in into rate bits [7:0], start permutation
                                core_in     <= sponge ^ {80'b0, ui_in};
                                core_start  <= 1'b1;
                                busy        <= 1'b1;
                                perm_active <= 1'b0;
                                fsm         <= ST_PERM;
                            end

                            default: ;
                        endcase
                    end
                end

                ST_PERM: begin
                    // Phase 1: wait for core_busy to go high (one cycle after start)
                    // Phase 2: wait for core_busy to fall (45 rounds later)
                    if (!perm_active) begin
                        if (core_busy)
                            perm_active <= 1'b1;
                    end else if (!core_busy) begin
                        sponge      <= core_out;
                        busy        <= 1'b0;
                        perm_active <= 1'b0;
                        fsm         <= ST_IDLE;
                    end
                end

            endcase

            // Advance output shift register whenever host reads addr=2
            // Shifts right by 8: next read returns the next digest byte
            if (rd_rise && (addr == 3'd2) && out_valid)
                out_shreg <= {8'b0, out_shreg[87:8]};
        end
    end

    // ------------------------------------------------------------------
    // Output assignments
    // ------------------------------------------------------------------
    assign uo_out  = out_shreg[7:0];          // current digest byte (LSB first)
    assign uio_out = {6'b0, out_valid, busy}; // status: bit1=out_valid, bit0=busy

    wire _unused = &{ena, uio_in[7:5], 1'b0};

endmodule
