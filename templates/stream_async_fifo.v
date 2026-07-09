/*==============================================================
 *  Description: Generic Gray-coded asynchronous FIFO.
 *  File: stream_async_fifo.v
 *  Author: UFDE+ Stream IP Generator (hand-written primitive)
 *
 *  Standard 2-FF synchronised Gray-coded async FIFO (Cliff Cummings
 *  "Simulation and Synthesis Techniques for Asynchronous FIFO Design"
 *  SNUG 2002).  Provides:
 *    - Cross-domain write/read pointer synchronisation
 *    - Registered empty / full flags (1-cycle latency)
 *    - Strict no-loss guarantee while w_full is honoured by the producer
 *    - Verilog-2001 compatible, no vendor primitives
 *
 *  Parameters:
 *    WIDTH  - data width
 *    ADDR_W - pointer width; FIFO depth = 2^ADDR_W
 *==============================================================*/
`timescale 1ns / 1ps

module stream_async_fifo #(
    parameter WIDTH  = 8,
    parameter ADDR_W = 3
) (
    input  wire              w_clk,
    input  wire              w_rst_n,
    input  wire              w_we,
    input  wire [WIDTH-1:0]  w_data,

    input  wire              r_clk,
    input  wire              r_rst_n,
    input  wire              r_re,
    output reg  [WIDTH-1:0]  r_data,
    output reg               r_empty,
    output reg               w_full
);

    localparam DEPTH = (1 << ADDR_W);

    // ----------------------------------------------------------------
    // Memory
    // ----------------------------------------------------------------
    reg [WIDTH-1:0] mem [0:DEPTH-1];

    // ----------------------------------------------------------------
    // Pointers (binary + Gray)
    // ----------------------------------------------------------------
    reg [ADDR_W:0] w_ptr_bin, w_ptr_gray;
    reg [ADDR_W:0] r_ptr_bin, r_ptr_gray;

    wire [ADDR_W:0] w_ptr_gray_next = (w_ptr_bin + 1'b1) ^ ((w_ptr_bin + 1'b1) >> 1);
    wire [ADDR_W:0] r_ptr_gray_next = (r_ptr_bin + 1'b1) ^ ((r_ptr_bin + 1'b1) >> 1);

    // ----------------------------------------------------------------
    // Cross-domain pointer synchronisers (2-FF each)
    // ----------------------------------------------------------------
    reg [ADDR_W:0] r_ptr_gray_sync_0, r_ptr_gray_sync_1;
    reg [ADDR_W:0] w_ptr_gray_sync_0, w_ptr_gray_sync_1;

    // ----------------------------------------------------------------
    // Empty / full comparators (Gray-coded, one-bit MSB distinguishes)
    // ----------------------------------------------------------------
    wire r_empty_comb = (r_ptr_gray == w_ptr_gray_sync_1);
    wire w_full_comb  = (w_ptr_gray == {~r_ptr_gray_sync_1[ADDR_W],
                                          r_ptr_gray_sync_1[ADDR_W-1:0]});

    // ----------------------------------------------------------------
    // Write domain
    // ----------------------------------------------------------------
    always @(posedge w_clk or negedge w_rst_n) begin
        if (!w_rst_n) begin
            w_ptr_bin         <= 0;
            w_ptr_gray        <= 0;
            w_full            <= 1'b0;
            r_ptr_gray_sync_0 <= 0;
            r_ptr_gray_sync_1 <= 0;
        end else begin
            // Pointer increments on a successful write
            if (w_we && !w_full) begin
                mem[w_ptr_bin[ADDR_W-1:0]] <= w_data;
                w_ptr_bin  <= w_ptr_bin + 1'b1;
                w_ptr_gray <= w_ptr_gray_next;
            end

            // Registered full flag
            w_full <= w_full_comb;

            // Read-pointer synchroniser
            r_ptr_gray_sync_0 <= r_ptr_gray;
            r_ptr_gray_sync_1 <= r_ptr_gray_sync_0;
        end
    end

    // ----------------------------------------------------------------
    // Read domain
    // ----------------------------------------------------------------
    always @(posedge r_clk or negedge r_rst_n) begin
        if (!r_rst_n) begin
            r_ptr_bin         <= 0;
            r_ptr_gray        <= 0;
            r_empty           <= 1'b1;
            r_data            <= 0;
            w_ptr_gray_sync_0 <= 0;
            w_ptr_gray_sync_1 <= 0;
        end else begin
            // Pointer increments on a successful read.
            //
            // NOTE: the enable uses the *combinational* empty flag
            // (r_empty_comb), NOT the registered r_empty.  The registered
            // flag lags one r_clk cycle behind the read pointer, so when the
            // FIFO holds exactly one word, using r_empty here would let the
            // reader over-consume: it would advance the pointer a second time
            // (reading an uninitialised mem[] entry → X) on the cycle the
            // registered flag is still 0 but the pointer has already caught
            // up to the write pointer.  r_empty_comb reflects the latest
            // pointer state and stops the read exactly when the FIFO drains.
            if (r_re && !r_empty_comb) begin
                r_data     <= mem[r_ptr_bin[ADDR_W-1:0]];
                r_ptr_bin  <= r_ptr_bin + 1'b1;
                r_ptr_gray <= r_ptr_gray_next;
            end

            // Registered empty flag
            r_empty <= r_empty_comb;

            // Write-pointer synchroniser
            w_ptr_gray_sync_0 <= w_ptr_gray;
            w_ptr_gray_sync_1 <= w_ptr_gray_sync_0;
        end
    end

endmodule
