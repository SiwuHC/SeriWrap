/*==============================================================
 *  Physical Device I/O Adapter: UART Receiver
 *  File: device_adapter_uart.v
 *
 *  8N1 UART receiver for IP-Generator's PDIAL layer.
 *  Receives serial frames at a fixed baud rate (BAUD_DIV system
 *  clocks per bit), recovers the byte, and exposes it as
 *    data_byte[7:0]   - the most recently received byte (LSB-first frame)
 *    data_valid       - 1-cycle pulse when a new byte is latched
 *
 *  Instantiation: stream_generator generates this when the user
 *  passes `--adapter uart:data_byte[7:0]`.  In Bridge-style use the
 *  downstream wrapper's L1 FSM treats `data_valid` as a source event.
 *
 *  Defaults assume a 100 MHz system clock and a 921 600 baud link
 *  (BAUD_DIV=108).  Override BAUD_DIV for other baud rates:
 *    BAUD_DIV = f_clk / baud_rate
 *=============================================================*/
`timescale 1ns / 1ps

module device_adapter_uart #(
    parameter [15:0] BAUD_DIV = 16'd108   // system clocks per UART bit
) (
    input  wire        clk,
    input  wire        rst_n,

    // Physical UART pin (idle high)
    input  wire        uart_rx,

    // Standard adapter outputs
    output reg  [7:0]  data_byte,
    output reg         data_valid
);

    // ----------------------------------------------------------------
    // 3-stage synchroniser on uart_rx (async pin → system clock domain)
    // ----------------------------------------------------------------
    reg [2:0] rx_sync;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) rx_sync <= 3'b111;
        else        rx_sync <= {rx_sync[1:0], uart_rx};
    end

    // ----------------------------------------------------------------
    // Receiver FSM: IDLE → START → DATA → STOP → IDLE
    //   START:  detect falling edge, then wait BAUD_DIV/2 to land
    //           in the middle of the start bit for re-sampling.
    //   DATA:   sample 8 data bits at BAUD_DIV intervals (LSB first).
    //   STOP:   wait one full bit, then pulse data_valid.
    // ----------------------------------------------------------------
    localparam S_IDLE  = 2'd0;
    localparam S_START = 2'd1;
    localparam S_DATA  = 2'd2;
    localparam S_STOP  = 2'd3;

    reg [1:0]  state;
    reg [15:0] baud_cnt;
    reg [3:0]  bit_idx;
    reg [7:0]  shift_reg;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state      <= S_IDLE;
            baud_cnt   <= 16'd0;
            bit_idx    <= 4'd0;
            shift_reg  <= 8'd0;
            data_byte  <= 8'd0;
            data_valid <= 1'b0;
        end else begin
            data_valid <= 1'b0;     // default: clear pulse
            case (state)
                S_IDLE: begin
                    baud_cnt <= 16'd0;
                    if (rx_sync[2] == 1'b0) begin    // start bit (falling edge)
                        state    <= S_START;
                        baud_cnt <= 16'd0;
                    end
                end

                S_START: begin
                    // Wait half a bit to land mid-start-bit (re-sample).
                    baud_cnt <= baud_cnt + 16'd1;
                    if (baud_cnt == (BAUD_DIV >> 1)) begin
                        baud_cnt <= 16'd0;
                        bit_idx  <= 4'd0;
                        state    <= S_DATA;
                    end
                end

                S_DATA: begin
                    baud_cnt <= baud_cnt + 16'd1;
                    if (baud_cnt == BAUD_DIV - 1) begin
                        baud_cnt <= baud_cnt + 16'd1;   // wraps on next cycle
                        shift_reg[bit_idx] <= rx_sync[2];
                        if (bit_idx == 4'd7)
                            state <= S_STOP;
                        else
                            bit_idx <= bit_idx + 4'd1;
                    end
                end

                S_STOP: begin
                    baud_cnt <= baud_cnt + 16'd1;
                    if (baud_cnt == BAUD_DIV - 1) begin
                        // Stop bit sampled.  We do NOT gate on stop-bit
                        // polarity here so the adapter remains useful
                        // for framings without a stop bit; the wrapper's
                        // L1 FSM still treats `data_valid` as a 1-cycle
                        // event pulse.
                        data_byte  <= shift_reg;
                        data_valid <= 1'b1;
                        state      <= S_IDLE;
                    end
                end

                default: state <= S_IDLE;
            endcase
        end
    end

endmodule