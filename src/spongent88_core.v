/*
 * Copyright (c) 2026 Stefan Aeschbacher
 * SPDX-License-Identifier: Apache-2.0
 *
 * spongent88_core.v — Spongent-88/80/8 permutation (2-rounds-per-cycle)
 *
 * Spongent-88/80/8 parameters (CHES 2011):
 *   State b  = 88 bits
 *   Rate  r  =  8 bits  (rate portion = state[7:0])
 *   Cap   c  = 80 bits
 *   Rounds   = 45
 *
 * Round function (applied 45×) — order matches Spongent.cpp reference:
 *   1. Counter    — 6-bit LFSR value injected at both ends of state:
 *                     state[5:0]   ^= lfsr
 *                     state[87:82] ^= bit_reverse_6(lfsr)
 *   2. sBoxLayer  — Spongent 4-bit S-box on every nibble in parallel
 *   3. pLayer     — bit permutation P(i) = (i * b/4) mod (b-1)
 *                                        = (i * 22) mod 87  for i<87; P(87)=87
 *
 * LFSR: 6-bit, polynomial x^6+x^5+1, left-shift, initial value 6'b000_101 (=0x05)
 *   next = {lfsr[4:0], lfsr[5] ^ lfsr[4]}
 *   Sequence: 0x05, 0x0A, 0x14, 0x29, 0x13, 0x27, 0x0F, ...  (period 63)
 *
 * Timing (2-rounds-per-cycle):
 *   45 rounds are computed as 22 double-round cycles (rounds 0-1 .. 42-43)
 *   plus 1 single-round cycle (round 44).
 *   Latency = 23 cycles.  busy falls on the same edge the final state is
 *   written, so state_out is valid combinationally after that edge.
 *
 * Interface:
 *   start     — assert for one cycle (while busy=0) to load state_in and begin
 *   busy      — high while computing; falls when result is ready
 *   state_out — final permuted state, stable as long as busy=0 and start=0
 *
 * Verified against:
 *   - Reference C implementation: https://github.com/datenzwergin/BenchSpongent
 *   - Reference Python: https://github.com/joostrijneveld/readable-crypto
 *   - sBoxLayer(0x0123456789ABCDEF012345) = 0xEDB0214F7A859C36EDB021
 *   - pLayer  (0x0123456789ABCDEF012345) = 0x00FF003C3C333333155555
 */

`default_nettype none

module spongent88_core (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        start,
    output reg         busy,
    input  wire [87:0] state_in,
    output wire [87:0] state_out
);

    // ----------------------------------------------------------------
    // S-box: Spongent 4-bit substitution (NOT the PRESENT S-box)
    // S = {E, D, B, 0, 2, 1, 4, F, 7, A, 8, 5, 9, C, 3, 6}
    // Source: Bogdanov et al. CHES 2011 / BenchSpongent reference
    // ----------------------------------------------------------------
    function [3:0] sbox;
        input [3:0] x;
        case (x)
            4'h0: sbox = 4'hE;  4'h1: sbox = 4'hD;
            4'h2: sbox = 4'hB;  4'h3: sbox = 4'h0;
            4'h4: sbox = 4'h2;  4'h5: sbox = 4'h1;
            4'h6: sbox = 4'h4;  4'h7: sbox = 4'hF;
            4'h8: sbox = 4'h7;  4'h9: sbox = 4'hA;
            4'hA: sbox = 4'h8;  4'hB: sbox = 4'h5;
            4'hC: sbox = 4'h9;  4'hD: sbox = 4'hC;
            4'hE: sbox = 4'h3;  4'hF: sbox = 4'h6;
            default: sbox = 4'h0;
        endcase
    endfunction

    // Apply sbox to every nibble of an 88-bit value.
    // Nibble 0 = bits [3:0], nibble 1 = bits [7:4], …, nibble 21 = bits [87:84].
    function [87:0] sbox_layer;
        input [87:0] x;
        integer k;
        begin
            for (k = 0; k < 22; k = k + 1)
                sbox_layer[k*4 +: 4] = sbox(x[k*4 +: 4]);
        end
    endfunction

    // ----------------------------------------------------------------
    // pLayer: P(i) = (i * b/4) mod (b-1)  =  (i * 22) mod 87  for i<87
    //         P(87) = 87
    // Semantics: input bit i moves to output position P(i).
    //   out[P(i)] = in[i]   →   out[(i*22) % 87] = in[i]   for i < 87
    //   out[87]              = in[87]
    // Bijection: gcd(22, 87) = 1.  Zero gates (wire renaming at elaboration).
    // ----------------------------------------------------------------
    function [87:0] player;
        input [87:0] x;
        integer k;
        begin
            player = 88'b0;
            for (k = 0; k < 87; k = k + 1)
                player[(k * 22) % 87] = x[k];
            player[87] = x[87];
        end
    endfunction

    // ----------------------------------------------------------------
    // Internal state
    // ----------------------------------------------------------------
    reg [87:0] state;
    reg [5:0]  lfsr;   // 6-bit round counter; poly x^6+x^5+1, init 0x05
    reg [5:0]  round;  // next round index to apply: 0, 2, 4, ..., 42, 44

    assign state_out = state;

    // ----------------------------------------------------------------
    // Round 1 — uses current lfsr (applies round `round`)
    // ----------------------------------------------------------------
    wire [5:0]  lfsr_rev1  = {lfsr[0], lfsr[1], lfsr[2], lfsr[3], lfsr[4], lfsr[5]};
    wire [87:0] r_count1   = state ^ {lfsr_rev1, 76'b0, lfsr};
    wire [87:0] r_sbox1    = sbox_layer(r_count1);
    wire [87:0] r_round1   = player(r_sbox1);
    wire [5:0]  lfsr_next1 = {lfsr[4:0], lfsr[5] ^ lfsr[4]};

    // ----------------------------------------------------------------
    // Round 2 — uses lfsr after one step (applies round `round + 1`)
    // Only used when round < 44 (i.e., we have a pair to process).
    // ----------------------------------------------------------------
    wire [5:0]  lfsr2      = lfsr_next1;
    wire [5:0]  lfsr_rev2  = {lfsr2[0], lfsr2[1], lfsr2[2], lfsr2[3], lfsr2[4], lfsr2[5]};
    wire [87:0] r_count2   = r_round1 ^ {lfsr_rev2, 76'b0, lfsr2};
    wire [87:0] r_sbox2    = sbox_layer(r_count2);
    wire [87:0] r_round2   = player(r_sbox2);
    wire [5:0]  lfsr_next2 = {lfsr2[4:0], lfsr2[5] ^ lfsr2[4]};

    // ----------------------------------------------------------------
    // Control FSM
    // ----------------------------------------------------------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            busy  <= 1'b0;
            state <= 88'b0;
            lfsr  <= 6'b000_101;  // 0x05
            round <= 6'd0;

        end else if (!busy && start) begin
            // Load initial state and start the permutation
            state <= state_in;
            lfsr  <= 6'b000_101;  // 0x05
            round <= 6'd0;
            busy  <= 1'b1;

        end else if (busy) begin
            if (round == 6'd44) begin
                // Last round: apply single round 44, then done
                state <= r_round1;
                lfsr  <= lfsr_next1;
                busy  <= 1'b0;
                round <= 6'd0;
            end else begin
                // Double round: apply rounds `round` and `round+1` in one cycle
                state <= r_round2;
                lfsr  <= lfsr_next2;
                round <= round + 6'd2;
            end
        end
    end

endmodule
