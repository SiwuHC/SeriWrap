# SeriWrap

SeriWrap is a Verilog IP generator: it emits synthesizable Verilog for a block RAM,
a PLL, or a wrapper that puts a user module behind a serial link, from a short
description (a memory image, a divide ratio, a plain user module).  Each generator is
a plain command line tool that prints one JSON object on stdout, so it can be driven
by a shell, a Makefile, a GUI or a CI job.

This document describes the generators and their options.

## Contents

1. [Generators](#1-generators)
2. [Requirements and build](#2-requirements-and-build)
3. [Repository layout](#3-repository-layout)
4. [Command line interface](#4-command-line-interface)
5. [BRAM generator](#5-bram-generator)
6. [PLL generator](#6-pll-generator)
7. [Image to MIF helper](#7-image-to-mif-helper)
8. [Stream wrapper](#8-stream-wrapper)
9. [Supported BRAM configurations](#9-supported-bram-configurations)
10. [Contributing](#10-contributing)
11. [Author](#11-author)
12. [Host integration](#12-host-integration)

---

## 1. Generators

| Generator | Command | Input | Output |
|-----------|---------|-------|--------|
| Block RAM | `ip_main.py bram` | MIF memory image | `test.v` — synthesizable RAM module |
| PLL | `ip_main.py pll` | divide ratio, FPGA gate count | `PLL_<divide>_<gates>.v` |
| Stream wrapper | `ip_main.py stream` | user Verilog module | wrapper + SIPO + PISO + BRAM + top (plus a manifest with `--emit-manifest`) |
| Image to MIF | `img2mif.py` | PNG / JPG / BMP | `*.mif` for the BRAM generator |

A stream wrapper exposes a ready/valid outer interface: `s_ready` tells the host
when a new frame may be driven, a frame is exactly `INPUT_COUNT` words on
`s_data_in` and `OUTPUT_COUNT` words on `s_data_out`, and the wrapped module only
has to provide `start` / `done`.

## 2. Requirements and build

- **Python** ≥ 3.8
- **PyInstaller** (optional — only to build standalone executables)
- **jinja2**, **Pillow**

```bash
$ pip install pyinstaller jinja2 Pillow
```

### Building the executables (optional)

On a machine without a Python environment you can freeze both entry points into
standalone executables with PyInstaller:

```bash
# 1. Unified SeriWrap (BRAM + PLL + Stream)
$ pyinstaller seriwrap.spec

# 2. Image → MIF converter
$ pyinstaller img2mif.spec
```

Release builds for Linux and Windows are produced by
`.github/workflows/release.yml`: pushing a tag `v*` builds both with PyInstaller on their
own runner (PyInstaller cannot cross compile) and attaches
`seriwrap-linux-x86_64.tar.gz` / `seriwrap-windows-x86_64.zip` to the GitHub release.  A
manual run of the workflow builds the artifacts without publishing a release.

After a local build you will find:

```
dist/
├── seriwrap.exe
└── img2mif.exe
```

## 3. Repository layout

```
SeriWrap/
│
│  === IP Generation Core ===
│
├── ip_main.py              # Unified CLI entry (bram / pll / stream sub-commands)
├── bram_generator.py       # BRAM generator engine
├── img2mif.py              # Image → MIF converter (feeds BRAM generator)
├── pll_generator.py        # PLL generator engine
├── stream_generator.py     # Stream wrapper generator (SIPO/PISO/BRAM glue)
│
├── templates/              # Jinja2 Verilog templates
│   ├── bram_template.j2
│   ├── pll_template.j2
│   ├── stream_sipo.j2
│   ├── stream_piso.j2
│   ├── stream_wrapper.j2
│   ├── stream_top.j2
│   ├── stream_piso_sync.j2 / stream_sipo_sync.j2   # single-clock variants
│   └── stream_bridge.j2    # internal: event/PS-2 input bridge (experimental)
│
├── seriwrap.spec       # PyInstaller spec for seriwrap.exe
└── img2mif.spec            # PyInstaller spec for img2mif.exe
```

## 4. Command line interface

```bash
python3 ip_main.py bram   <mif> [options]        # block RAM
python3 ip_main.py pll    [options]              # PLL
python3 ip_main.py stream --source <file.v> --top <module> [options]
```

* every subcommand prints **one JSON object** on stdout
  (`success`, `files`, `message`, and `error` when it fails) and exits non-zero on
  failure, so callers never have to parse prose;
* `--help` lists the options of a subcommand; they are also tabulated in the
  sections below;
* `ip_main.py stream --print-modules` only lists the modules and ports found in a
  Verilog file and exits — the quickest way to see what the parser understands.

## 5. BRAM generator

```bash
$ python ip_main.py bram input.mif --output bram.v
```

Supported MIF parameters: `WIDTH=`, `DEPTH=`, `WIDTHA=`, `DEPTHA=`, `WIDTHB=`, `DEPTHB=`, `ADDRESS_RADIX=`, `DATA_RADIX=`. Single-port and dual-port modes are both supported.

**Single-port example**

```
DEPTH = 256;           -- memory depth (number of addresses)
WIDTH = 8;             -- data width in bits
ADDRESS_RADIX = DEC;   -- address radix (HEX/DEC/BIN/OCT/UNS)
DATA_RADIX = HEX;      -- data radix (HEX/DEC/BIN/OCT/UNS)
CONTENT BEGIN
    0 : 00;            -- data at address 0
    1 : 0F;            -- data at address 1
    [2..9] : FF;       -- addresses 2 through 9
    10 : A5;           -- data at address 10
    [11..255] : 00;    -- remaining addresses filled with 00
END;
```

**Dual-port example**

```
DEPTHA = 256;          -- port A memory depth (number of addresses)
WIDTHA = 8;            -- port A data width in bits
DEPTHB = 128;          -- port B memory depth (number of addresses)
WIDTHB = 16;           -- port B data width in bits
ADDRESS_RADIX = DEC;   -- address radix (HEX/DEC/BIN/OCT/UNS)
DATA_RADIX = HEX;      -- data radix (HEX/DEC/BIN/OCT/UNS)
CONTENT BEGIN
    0 : 00;            -- data at address 0
    1 : 0F;            -- data at address 1
    [2..9] : FF;       -- addresses 2 through 9
    10 : A5;           -- data at address 10
    [11..255] : 00;    -- remaining addresses filled with 00
END;
```

## 6. PLL generator

```bash
# Single configuration
$ python ip_main.py pll --divide 2 --gates 30 --output PLL_2_30.v

# All combinations (4 divide values × 2 gate counts = 8 files)
$ python ip_main.py pll --all --output-dir ./generated
```

| Parameter | Values | Description |
|-----------|--------|-------------|
| `divide` | 2, 4, 8, 16 | Clock divide ratio |
| `gates` | 30, 50 | 30 = 30W (DLL primitive), 50 = 50W (DCM primitive) |

## 7. Image to MIF helper
### Preparing a MIF from an image

If your BRAM will store graphic data, use `img2mif.py` to create the initialization file first:

```bash
# Convert a picture to GraphicLCD MIF (128×64, 8-column vertical strip)
$ python img2mif.py image.png -o output.mif

# With preview
$ python img2mif.py image.png -o output.mif -p preview.png

# Invert colours
$ python img2mif.py image.png -o output.mif --invert

# Built-in test patterns
$ python img2mif.py -t checker -o checker.mif -p preview.png
```

Then pass the resulting `*.mif` to `ip_main.py bram` as shown above.

## 8. Stream wrapper

`ip_main.py stream` turns a plain Verilog module (your *kernel*) into an IP core
with a serial outer interface: SIPO + PISO + dual-port BRAM around the kernel, so
a host that can only change a few pins per transaction can still deliver a whole
input frame and read a whole output frame.

#### 8.1 Quick start

```bash
# what is in my file?  (modules, ports, widths, handshake ports)
python3 ip_main.py stream --source my_kernel.v --print-modules

# generate the wrapper: 8-bit words, 512-deep BRAM, sync link
python3 ip_main.py stream \
    --source my_kernel.v --top my_kernel \
    --bram-width 8 --bram-depth 512 --bram-type ram4s \
    --binpack --sync-mode \
    --out-dir ./out/          #  add --emit-manifest if the host should read
                              #  the link description instead of hard-coding it
```

The command prints one JSON object on stdout (`success`, `files`, `message`, ...),
so a Makefile, a GUI or a CI job can drive it just as well as a shell.

#### 8.2 What the kernel has to look like

* a clock / reset pair (`clk`, `rst_n` by default — names are configurable),
* `start` in, `done` out (and optionally `busy`),
* data inputs and one or more data outputs; every input port becomes part of the
  input frame and every output port part of the output frame,
* port widths written as literal ranges (`[31:0]`).  Parameterised widths are
  treated as 1 bit by the parser: pass `--width`, or `--preprocess` to expand
  macros with `verilator -E` first.

```verilog
module my_kernel (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        start,
    output reg         done,
    input  wire [31:0] a0, a1, a2, a3,
    output reg  [31:0] y0
);
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin done <= 1'b0; y0 <= 32'h0; end
    else begin done <= start; if (start) y0 <= a0 + a1 + a2 + a3; end
  end
endmodule
```

The wrapper holds `start` for one cycle as soon as the whole input frame has
arrived, waits for `done`, dumps the outputs into the output buffer and shifts them
out; `s_ready` stays low while that is happening.

#### 8.3 What gets generated

| file | content |
|------|---------|
| `<top>__stream_top.v` | top level: wrapper + serial pins |
| `<top>__stream_wrapper.v` | frame FSM (receive → start → wait `done` → dump → shift out) |
| `<top>__stream_sipo.v` | input side: serial words → BRAM |
| `<top>__stream_piso.v` | output side: BRAM → serial words |
| `<top>__stream_bram.v` | BRAM primitive instances |
| `stream_async_fifo.v` | only for async wrappers: the clock-domain crossing FIFO the async SIPO instantiates |
| `<top>__stream_manifest.json` | **only with `--emit-manifest`**: machine-readable description of the link |
| `<top>__stream_mapping.txt` | human-readable port ↔ word map |

#### 8.4 Optional: the machine-readable manifest

`--emit-manifest` also writes `<top>__stream_manifest.json`, the mapping document in JSON form: `link` (word width, sync/async), `frame` (words per frame, handshake), `ports` (kernel ports and widths) and `packing` (which bits of which port each word carries).  It is off by default: a host that hard-codes the frame size does not need it, a GUI or bring-up script that would rather read the description does.

#### 8.5 Words, ports and widths

A kernel *port* is not the same thing as a serial *word*.  With `--bram-width 8`,
a 32-bit `a0` spans four words, so sixteen such ports make a 64-word frame; with a
16-bit link each port is two words.  Packing is one-to-one — one port, one word —
only when the port width equals the word width.  `--binpack` packs several narrow
ports into one BRAM entry when their bits fit together.

#### 8.6 Choosing the link

| option | effect |
|--------|--------|
| `--bram-width` | word width; required, or `0` to auto-select a valid pair |
| `--bram-depth` | BRAM depth (auto with `--bram-width 0`); the frame must fit, so depth ≥ words per frame |
| `--bram-type` | `ram4s` (Spartan-6 / FDP3P7 style), `ram8b`, `ramb18e1`, `ramb36e1`, `generic` (behavioural, for simulation) |
| `--sync-mode` | lightweight SIPO/PISO: no async FIFO, no `s_clk_in` baud clock, `CLK_OUT` tied low, word boundary = `STROBE` edge |
| `--binpack` | FFD bin-packing of the port marshalling |
| `--baud-div`, `--baud-div-in`, `--baud-div-out` | serial clock dividers (async mode) |
| `--pingpong` | two buffers, so a frame can be loaded while the previous one is computed |
| `--no-sipo`, `--no-piso` | build a single direction only |

#### 8.7 Driving it from a host

* **sync mode** — put the word on the data pins, raise `STROBE` for one system
  clock, drop it again, and leave at least one clock before the next word.  The
  sync SIPO writes on the **rising edge** of `STROBE`, so a host whose pins only
  change once per transaction (a USB frame, a GUI tick) may hold the strobe as
  long as it likes: the pulse, not the level, defines the word.  `s_clk_out` is
  tied low and `s_data_valid` marks each output word.
* **async mode** — three phases per word (data + strobe high, `s_clk_in` high,
  everything low), held for `--baud-div` (or `--baud-div-in/-out`) clocks; the
  word boundary is the host's own serial clock.
* wait for `s_ready` before starting the next frame — the wrapper drops it while
  it computes.

#### 8.8 All `stream` options

| option | default | description |
|--------|---------|-------------|
| `--source`, `-s` | *(required)* | user Verilog source |
| `--top`, `-t` | *(required)* | module to wrap |
| `--out-dir` | `.` | output directory |
| `--width` | 0 (auto) | data port width override |
| `--bram-width`, `-w` | *(required, or 0)* | BRAM / word width; `0` lets the tool pick a valid (width, depth) pair for the chosen `--bram-type` |
| `--bram-depth` | *(with a width)* | BRAM depth; auto-selected when `--bram-width 0` |
| `--bram-type` | `ram4s` | `ram4s`, `ram8b`, `ramb18e1`, `ramb36e1`, `generic` |
| `--emit-manifest` | off | also write `<top>__stream_manifest.json` (8.4) |
| `--force-bram` | off | use the BRAM wrapper even for small frames (see 8.6) |
| `--binpack` | off | pack narrow ports into shared BRAM entries |
| `--sync-mode` | off | sync SIPO/PISO (see 8.7) |
| `--baud-div` / `--baud-div-in` / `--baud-div-out` | 2 | serial clock dividers |
| `--pingpong` | off | double-buffered wrapper |
| `--no-sipo` / `--no-piso` | off | omit one direction |
| `--no-ready` | off | omit `s_ready` (reproduces the old 21/69-pin interface; not recommended) |
| `--debug` | off | extra overflow/idle/busy ports |
| `--print-modules` | — | list modules/ports and exit |
| `--preprocess` | off | expand ``define`/``ifdef` with `verilator -E` first |
| `--control-inputs` / `--control-outputs` | — | ports to route through the control path instead of the frame |
| `--handshake-config` | — | JSON file mapping clock/reset/start/done/busy roles to port names |
| `--clock-ports`, `--reset-ports`, `--start-ports`, `--done-ports`, `--busy-ports` | — | override one role (comma separated names) |

---

## 9. Supported BRAM configurations

Based on the 4Kb `RAMB4_Sx` primitive. Up to 16 primitives can be combined in parallel.

### Single-Port (25 configurations)

| Capacity | width × depth |
|----------|---------------|
| 4Kb  | 1×4096, 2×2048, 4×1024, 8×512, 16×256 |
| 8Kb  | 2×4096, 4×2048, 8×1024, 16×512, 32×256 |
| 16Kb | 4×4096, 8×2048, 16×1024, 32×512, 64×256 |
| 32Kb | 8×4096, 16×2048, 32×1024, 64×512, 128×256 |
| 64Kb | 16×4096, 32×2048, 64×1024, 128×512, 256×256 |

### Dual-Port (75 configurations)

Symmetric and asymmetric dual-port are supported; total capacity of Port A must equal Port B.

## 10. Contributing

To add a new SeriWrap or change the template engine, start from the templates
in [`templates/`](templates) and the generator in `stream_generator.py`.

## 11. Author

* [@FrancisCYH](https://github.com/FrancisCYH) — IP-Generator: BRAM, PLL.
* [@SiwuHC](https://github.com/SiwuHC) — SeriWrap: stream wrapper.

## 12. Host integration

A generated wrapper is ordinary synthesizable Verilog, so any host that can drive its
pins will do: with `--emit-manifest` it can read the frame size, packing and mode from
`<top>__stream_manifest.json` instead of hard-coding them (`<top>__stream_mapping.txt` is
the same information for a human); the sync word boundary is a `STROBE` pulse, the async one
is the `s_clk_in` period, and the host waits for `s_ready` before the next frame.

`tools/gen_rabbit_project.py` and `tools/check_rabbit_project.py` turn such a manifest plus a
pin-constraint file into a bound project for the
[Rabbit](https://github.com/0xtaruhi/Rabbit) virtual-component platform and verify the
bindings afterwards.
