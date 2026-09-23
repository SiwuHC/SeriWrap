/*==============================================================
 *  Demo user module: numeric_input
 *
 *  Accepts PS/2 keyboard events (via device_adapter_ps2) and
 *  accumulates the typed digits into an internal buffer:
 *    - digits 0..9  (scan codes 0x45,0x16,0x1E,...,0x26,0x25,0x2E,
 *                     0x36,0x3D,0x3E,0x46) → bits 14..23 of key_state
 *    - backspace    (0x66)                          → bit 26
 *    - enter        (0x5A)                          → bit 40  (the trigger)
 *
 *  When the wrapper FSM pulses `start` (because the bridge detected
 *  the Enter rising edge), the buffer is latched into `value` and
 *  `done` is asserted for 1 cycle.  The wrapper's PISO then ships
 *  `value` out the 3-wire serial port to a downstream compute stage.
 *
 *  Pair with: --input-source adapter --trigger-key enter
 *
 *  Note: value width is hardcoded 32 so SeriWrap's Verilog
 *  parser can resolve the bit-width (parameter-driven ranges are
 *  treated as 1-bit by the parser).  If you change this, also
 *  update --bram-width/depth to keep ceil(32 / WIDTH) ≤ depth.
 *=============================================================*/
`timescale 1ns / 1ps


module numeric_input (
    input  wire                    clk,
    input  wire                    rst_n,
    // From PS/2 adapter (device_adapter_ps2)
    input  wire [63:0]             key_state,
    input  wire [7:0]              mod_state,
    input  wire                    key_event,
    // Handshake from wrapper FSM (driven by --input-source adapter)
    input  wire                    start,
    output reg                     done,
    // Latched accumulator, shipped downstream via PISO
    output reg  [31:0]             value
);

    // ---------------------------------------------------------
    // State (note: "accum" not "buf"; buf is a SV reserved token)
    // ---------------------------------------------------------
    reg [63:0]        prev_key_state;
    reg [31:0]        accum;

    // ---------------------------------------------------------
    // Rising-edge decode of newly-pressed keys
    // ---------------------------------------------------------
    wire [63:0] new_presses = key_state & ~prev_key_state;

    reg [3:0]  new_digit;
    reg        new_backspace;
    reg        new_press_valid;

    integer    i;
    always @(*) begin
        new_digit       = 4'd0;
        new_backspace   = 1'b0;
        new_press_valid = 1'b0;
        // Scan bits 14..22 for digits 1..9
        for (i = 14; i <= 22; i = i + 1) begin
            if (new_presses[i]) begin
                new_digit       = (i - 13);    // 1..9
                new_press_valid = 1'b1;
            end
        end
        // Bit 23 → digit 0
        if (new_presses[23]) begin
            new_digit       = 4'd0;
            new_press_valid = 1'b1;
        end
        // Bit 26 → backspace
        if (new_presses[26]) begin
            new_backspace   = 1'b1;
            new_press_valid = 1'b1;
        end
    end

    // ---------------------------------------------------------
    // Accumulator + latched output
    // ---------------------------------------------------------
    // Saturating arithmetic: if accum*10+digit would overflow 32 bits,
    // hold at 2^32-1.
    wire [32:0] next_accum_ext = {1'b0, accum} * 33'd10 + {29'd0, new_digit};

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            prev_key_state <= 64'd0;
            accum          <= 32'd0;
            value          <= 32'd0;
            done           <= 1'b0;
        end else begin
            prev_key_state <= key_state;
            done           <= 1'b0;

            // Apply key event to accumulator.  Enter (bit 40) is the
            // trigger key — its rising edge is consumed by the
            // bridge to start the FSM, so we skip it here.
            if (new_press_valid && !new_presses[40]) begin
                if (new_backspace) begin
                    accum <= accum / 10;
                end else begin
                    if (next_accum_ext[32]) begin
                        accum <= 32'hFFFF_FFFF;        // overflow → saturate
                    end else begin
                        accum <= next_accum_ext[31:0];
                    end
                end
            end

            // On start pulse: latch accumulator into value (so PISO
            // can ship it to the downstream stage).
            if (start) begin
                value <= accum;
            end

            // Done is asserted one cycle after start.  The wrapper
            // FSM in C_COMPUTING checks user_done_wire on the cycle
            // following the start pulse, so a 1-cycle latency here
            // matches its expectation.
            done <= start;
        end
    end

endmodule
