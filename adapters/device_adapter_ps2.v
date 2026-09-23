/*==============================================================
 *  Physical Device I/O Adapter: PS/2 Keyboard
 *  File: device_adapter_ps2.v
 *
 *  Receives PS/2 keyboard scan codes (11-bit frames: 1 start +
 *  8 data + 1 parity + 1 stop), decodes press/release sequences
 *  and F0/E0 prefixes, and exposes the current key state as
 *  a 64-bit bitmap (key_state) plus 8-bit modifier flags
 *  (mod_state).  A single-cycle key_event pulse marks every
 *  state change.
 *
 *  This is the PS/2 adapter for SeriWrap's PDIAL layer.
 *  It can be instantiated standalone or wrapped automatically
 *  by stream_generator.generate_stream_ip() when the user
 *  supplies `--adapter ps2:key_state[63:0] mod_state[7:0]`.
 *=============================================================*/
`timescale 1ns / 1ps

module device_adapter_ps2 #(
    parameter KEY_W = 64,    // number of supported keys (1-bit each)
    parameter MOD_W = 8      // modifier bits (ctrl,shift,alt,caps,...)
) (
    input  wire                 clk,
    input  wire                 rst_n,

    // Physical PS/2 pins (connect directly to FPGA board pins)
    input  wire                 ps2_clk,
    input  wire                 ps2_data,

    // Standard adapter outputs: feed these into the user module
    output reg  [KEY_W-1:0]     key_state,   // 1 = currently pressed
    output reg  [MOD_W-1:0]     mod_state,   // modifier flags
    output reg                  key_event    // 1-cycle pulse on change
);

    // ----------------------------------------------------------------
    // Scan code set 2 → index lookup (subset covering A–Z, 0–9, esc,
    // space, enter, tab, backspace, modifiers, arrows).  Codes not
    // listed map to index 6'd63 (highest bit in key_state).
    // ----------------------------------------------------------------
    reg [5:0] key_index;

    // Standard Scan Code Set 2 → key index
    function [5:0] scancode_to_index;
        input [7:0] sc;
        begin
            case (sc)
                8'h76: scancode_to_index = 6'd0;   // ESC
                8'h05: scancode_to_index = 6'd1;   // F1
                8'h06: scancode_to_index = 6'd2;   // F2
                8'h04: scancode_to_index = 6'd3;   // F3
                8'h0C: scancode_to_index = 6'd4;   // F4
                8'h03: scancode_to_index = 6'd5;   // F5
                8'h0B: scancode_to_index = 6'd6;   // F6
                8'h83: scancode_to_index = 6'd7;   // F7
                8'h0A: scancode_to_index = 6'd8;   // F8
                8'h01: scancode_to_index = 6'd9;   // F9
                8'h09: scancode_to_index = 6'd10;  // F10
                8'h78: scancode_to_index = 6'd11;  // F11
                8'h07: scancode_to_index = 6'd12;  // F12
                8'h0E: scancode_to_index = 6'd13;  // `
                8'h16: scancode_to_index = 6'd14;  // 1
                8'h1E: scancode_to_index = 6'd15;  // 2
                8'h26: scancode_to_index = 6'd16;  // 3
                8'h25: scancode_to_index = 6'd17;  // 4
                8'h2E: scancode_to_index = 6'd18;  // 5
                8'h36: scancode_to_index = 6'd19;  // 6
                8'h3D: scancode_to_index = 6'd20;  // 7
                8'h3E: scancode_to_index = 6'd21;  // 8
                8'h46: scancode_to_index = 6'd22;  // 9
                8'h45: scancode_to_index = 6'd23;  // 0
                8'h4E: scancode_to_index = 6'd24;  // -
                8'h55: scancode_to_index = 6'd25;  // =
                8'h66: scancode_to_index = 6'd26;  // backspace
                8'h0D: scancode_to_index = 6'd27;  // tab
                8'h15: scancode_to_index = 6'd28;  // Q
                8'h1D: scancode_to_index = 6'd29;  // W
                8'h24: scancode_to_index = 6'd30;  // E
                8'h2D: scancode_to_index = 6'd31;  // R
                8'h2C: scancode_to_index = 6'd32;  // T
                8'h35: scancode_to_index = 6'd33;  // Y
                8'h3C: scancode_to_index = 6'd34;  // U
                8'h43: scancode_to_index = 6'd35;  // I
                8'h44: scancode_to_index = 6'd36;  // O
                8'h4D: scancode_to_index = 6'd37;  // P
                8'h54: scancode_to_index = 6'd38;  // [
                8'h5B: scancode_to_index = 6'd39;  // ]
                8'h5A: scancode_to_index = 6'd40;  // enter
                8'h14: scancode_to_index = 6'd41;  // ctrl (left)
                8'h1C: scancode_to_index = 6'd42;  // A
                8'h1B: scancode_to_index = 6'd43;  // S
                8'h23: scancode_to_index = 6'd44;  // D
                8'h2B: scancode_to_index = 6'd45;  // F
                8'h34: scancode_to_index = 6'd46;  // G
                8'h33: scancode_to_index = 6'd47;  // H
                8'h3B: scancode_to_index = 6'd48;  // J
                8'h42: scancode_to_index = 6'd49;  // K
                8'h4B: scancode_to_index = 6'd50;  // L
                8'h4C: scancode_to_index = 6'd51;  // ;
                8'h52: scancode_to_index = 6'd52;  // '
                8'h0F: scancode_to_index = 6'd53;  // shift (left)
                8'h12: scancode_to_index = 6'd53;  // shift (left, alt code)
                8'h1A: scancode_to_index = 6'd54;  // Z
                8'h22: scancode_to_index = 6'd55;  // X
                8'h21: scancode_to_index = 6'd56;  // C
                8'h2A: scancode_to_index = 6'd57;  // V
                8'h32: scancode_to_index = 6'd58;  // B
                8'h31: scancode_to_index = 6'd59;  // N
                8'h3A: scancode_to_index = 6'd60;  // M
                8'h41: scancode_to_index = 6'd61;  // ,
                8'h49: scancode_to_index = 6'd62;  // .
                8'h4A: scancode_to_index = 6'd63;  // /
                default: scancode_to_index = 6'd63; // unmapped → last slot
            endcase
        end
    endfunction

    // Modifier-bit assignment (mod_state[7:0])
    //   [0] = left ctrl   (scan 0x14)
    //   [1] = left shift  (scan 0x12 / 0x0F)
    //   [2] = left alt    (scan 0x11)
    //   [3] = caps lock   (scan 0x58, toggle)
    //   [4:7] = reserved
    // (is_*_sc wires declared after ps2_data_byte reg below)

    // ----------------------------------------------------------------
    // PS/2 receiver: synchronise ps2_clk, sample ps2_data on falling
    // edge, shift into 10-bit buffer (start, d0..d7, parity), validate
    // and emit data_to_send on the 11th falling edge.
    // ----------------------------------------------------------------
    reg [2:0] ps2_clk_sync;
    reg [3:0] count;
    reg [9:0] buffer;
    reg       valid;
    reg       valid_delay;
    reg [7:0] data_to_send;
    reg [7:0] ps2_data_byte;

    wire is_ctrl_sc  = (ps2_data_byte == 8'h14);
    wire is_shift_sc = (ps2_data_byte == 8'h12) || (ps2_data_byte == 8'h0F);
    wire is_alt_sc   = (ps2_data_byte == 8'h11);
    wire is_caps_sc  = (ps2_data_byte == 8'h58);

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            ps2_clk_sync <= 3'b111;
        else
            ps2_clk_sync <= {ps2_clk_sync[1:0], ps2_clk};
    end

    wire sampling = ps2_clk_sync[2] & ~ps2_clk_sync[1];

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            count         <= 4'd0;
            data_to_send  <= 8'd0;
            valid         <= 1'b0;
            ps2_data_byte <= 8'd0;
        end else if (sampling) begin
            if (count == 4'd10) begin
                if ((buffer[0] == 1'b0) &&
                    (ps2_data) &&
                    (^buffer[9:1])) begin
                    data_to_send  <= buffer[8:1];
                    valid         <= 1'b1;
                    ps2_data_byte <= buffer[8:1];
                end
                count <= 4'd0;
            end else begin
                buffer[count] <= ps2_data;
                count         <= count + 4'd1;
                valid         <= 1'b0;
            end
        end
    end

    // Single-cycle pulse when a new scan code is captured
    wire valid_pulse = (~valid_delay) & valid;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            valid_delay <= 1'b0;
        else
            valid_delay <= valid;
    end

    // ----------------------------------------------------------------
    // Scan-code state machine: handles F0 (release) and E0 (extended)
    // prefix bytes, then routes the next code to press/release logic.
    // ----------------------------------------------------------------
    localparam S_IDLE      = 3'b000;
    localparam S_F0        = 3'b001;
    localparam S_E0        = 3'b010;
    localparam S_E0_F0     = 3'b011;

    reg [2:0] fsm_state;
    reg [2:0] fsm_next;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            fsm_state <= S_IDLE;
        else
            fsm_state <= fsm_next;
    end

    always @(*) begin
        case (fsm_state)
            S_IDLE: begin
                if (valid_pulse && ps2_data_byte == 8'hF0) fsm_next = S_F0;
                else if (valid_pulse && ps2_data_byte == 8'hE0) fsm_next = S_E0;
                else fsm_next = S_IDLE;
            end
            S_F0:    fsm_next = valid_pulse ? S_IDLE : S_F0;
            S_E0:    fsm_next = (valid_pulse && ps2_data_byte == 8'hF0) ? S_E0_F0 : (valid_pulse ? S_IDLE : S_E0);
            S_E0_F0: fsm_next = valid_pulse ? S_IDLE : S_E0_F0;
            default: fsm_next = S_IDLE;
        endcase
    end

    // ----------------------------------------------------------------
    // Press / release logic: maintain a 64-bit bitmap of currently
    // pressed keys and 8-bit modifier state.
    //
    // Bug fix (2026-07-05): press_event previously triggered on any
    // valid_pulse while fsm_state == S_IDLE, including F0/E0 prefix
    // bytes.  This caused key_state[63] (default-index bucket) to
    // spuriously set on every release sequence, inflating pressed_count.
    // Fix: explicitly exclude the F0 (release marker) and E0 (extended
    // marker) prefix bytes from press_event.
    // ----------------------------------------------------------------
    wire is_prefix_byte = (ps2_data_byte == 8'hF0) || (ps2_data_byte == 8'hE0);
    wire press_event   = valid_pulse && (fsm_state == S_IDLE) && !is_prefix_byte;
    wire release_event = valid_pulse && ((fsm_state == S_F0) || (fsm_state == S_E0_F0));

    // Modifier scan codes (kept out of key_state to avoid bitmap
    // collisions; tracked only in mod_state)
    // ----------------------------------------------------------------
    // Only update key_state for scan codes that really exist in the lookup
    // table.  The table's default bucket is index 63, which is ALSO the real
    // '/' key, so an unmapped code (e.g. the 0x75 of an extended arrow key)
    // used to squash '/' on press and on release.
    // ----------------------------------------------------------------
    function automatic scancode_is_mapped(input [7:0] sc);
        begin
            case (sc)
                8'h76, 8'h05, 8'h06, 8'h04, 8'h0C, 8'h03, 8'h0B, 8'h83,
                8'h0A, 8'h01, 8'h09, 8'h78, 8'h07, 8'h0E, 8'h16, 8'h1E,
                8'h26, 8'h25, 8'h2E, 8'h36, 8'h3D, 8'h3E, 8'h46, 8'h45,
                8'h4E, 8'h55, 8'h66, 8'h0D, 8'h15, 8'h1D, 8'h24, 8'h2D,
                8'h2C, 8'h35, 8'h3C, 8'h43, 8'h44, 8'h4D, 8'h54, 8'h5B,
                8'h5A, 8'h14, 8'h1C, 8'h1B, 8'h23, 8'h2B, 8'h34, 8'h33,
                8'h3B, 8'h42, 8'h4B, 8'h4C, 8'h52, 8'h0F, 8'h12, 8'h1A,
                8'h22, 8'h21, 8'h2A, 8'h32, 8'h31, 8'h3A, 8'h41, 8'h49,
                8'h4A:
                    scancode_is_mapped = 1'b1;
                default: scancode_is_mapped = 1'b0;
            endcase
        end
    endfunction
    wire mapped_sc = scancode_is_mapped(ps2_data_byte);

    wire is_modifier_sc = is_ctrl_sc || is_shift_sc || is_alt_sc || is_caps_sc;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            key_state  <= {KEY_W{1'b0}};
            mod_state  <= {MOD_W{1'b0}};
            key_event  <= 1'b0;
        end else begin
            key_event <= 1'b0;
            if (press_event) begin
                if (!is_modifier_sc && mapped_sc)
                    key_state[key_index] <= 1'b1;
                if (is_ctrl_sc)  mod_state[0] <= 1'b1;
                if (is_shift_sc) mod_state[1] <= 1'b1;
                if (is_alt_sc)   mod_state[2] <= 1'b1;
                if (is_caps_sc)  mod_state[3] <= ~mod_state[3];
                key_event <= 1'b1;
            end else if (release_event) begin
                if (!is_modifier_sc && mapped_sc)
                    key_state[key_index] <= 1'b0;
                if (is_ctrl_sc)  mod_state[0] <= 1'b0;
                if (is_shift_sc) mod_state[1] <= 1'b0;
                if (is_alt_sc)   mod_state[2] <= 1'b0;
                key_event <= 1'b1;
            end
        end
    end

    // Combinational index for the just-received byte (used on next
    // clock edge to update key_state).  When we are in a release
    // state (F0 / E0_F0) the index is computed from the previous
    // byte that is still on ps2_data_byte, which is the released key.
    always @(*) begin
        key_index = scancode_to_index(ps2_data_byte);
    end

endmodule