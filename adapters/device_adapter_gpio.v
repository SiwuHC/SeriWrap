/*==============================================================
 *  Physical Device I/O Adapter: GPIO Edge Detector
 *  File: device_adapter_gpio.v
 *
 *  Rising-edge (or falling-edge) detector on a single GPIO pin.
 *  Exposes a 1-cycle pulse on every detected transition.
 *
 *  Instantiation: stream_generator generates this when the user
 *  passes `--adapter gpio:gpio_event`.  The wrapper's L1 FSM treats
 *  `gpio_event` as a source event in Bridge-style use.
 *
 *  Use cases: button presses, optical interrupter transitions, IRQ
 *  lines from external peripherals, etc.  When EDGE=1 the adapter
 *  pulses on a 0→1 transition; when EDGE=0 it pulses on 1→0.
 *=============================================================*/
`timescale 1ns / 1ps

module device_adapter_gpio #(
    parameter EDGE = 1   // 1 = rising edge, 0 = falling edge
) (
    input  wire clk,
    input  wire rst_n,

    // Physical GPIO pin
    input  wire gpio_in,

    // Standard adapter output: 1-cycle pulse on detected edge
    output reg  gpio_event
);

    reg [2:0] gpio_sync;
    reg       prev;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            gpio_sync <= 3'b000;
            prev      <= 1'b0;
            gpio_event <= 1'b0;
        end else begin
            gpio_sync <= {gpio_sync[1:0], gpio_in};
            gpio_event <= 1'b0;
            prev <= gpio_sync[2];
            if (EDGE == 1)
                gpio_event <= gpio_sync[2] & ~prev;
            else
                gpio_event <= ~gpio_sync[2] & prev;
        end
    end

endmodule