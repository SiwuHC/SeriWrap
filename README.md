# SeriWrap

SeriWrap is a standalone Verilog IP-generation toolset. It produces synthesizable
Verilog IP cores — **Block RAM (BRAM)**, **Phase-Locked Loop (PLL)**, and the
**SeriWrap stream wrapper** — from high-level descriptions: a memory image, a divide
ratio, or a plain user Verilog module.

It runs entirely from the command line and has no dependency on any IDE, vendor
toolchain or project file format.

---

## What It Does

| Generator | Input | Output |
|-----------|-------|--------|
| **BRAM IP** | MIF file (memory initialization) | `test.v` — synthesizable Verilog RAM module |
| **PLL IP** | Divide ratio & gate count | `PLL_<divide>_<gates>.v` — clock multiplier module |
| **Stream IP** | User Verilog module | wrapper + SIPO + PISO + BRAM + top (5 files) |

> The stream wrapper exposes a **ready/valid style outer interface**: `s_ready`
> tells the host when a new frame may be driven, and each frame is exactly
> `INPUT_COUNT` words on `s_data_in` and `OUTPUT_COUNT` words on `s_data_out`.
> The kernel must provide `start`/`done`; everything else is generated.

> **Image → MIF** is a helper utility that produces initialization files for the BRAM generator. It converts PNG / JPG / BMP into the `*.mif` format consumed by `ip_main.py bram`.

Every generator is a plain CLI that prints a single JSON object on stdout, so it can
equally be driven by a GUI, a Makefile or a CI script.

---

## Project Layout

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

---

## Requirements

- **Python** ≥ 3.8
- **PyInstaller** (optional — only to build standalone executables)
- **jinja2**, **Pillow**

```bash
$ pip install pyinstaller jinja2 Pillow
```

---

## Building the Executables (optional)

On a machine without a Python environment you can freeze both entry points into
standalone executables with PyInstaller:

```bash
# 1. Unified SeriWrap (BRAM + PLL + Stream)
$ pyinstaller seriwrap.spec

# 2. Image → MIF converter
$ pyinstaller img2mif.spec
```

After building you will find:

```
dist/
├── seriwrap.exe
└── img2mif.exe
```

---

## Rabbit integration

A SeriWrap wrapper is meant to be driven by a host.  Two things make that
turn-key with the [Rabbit](https://github.com/0xtaruhi/Rabbit) virtual-component
platform (a copy lives in `BRAM_Test/Rabbit`):

* every `stream` run also writes **`<top>__stream_manifest.json`** next to the
  wrapper.  It is the machine-readable description of the link: serial word
  width, words per frame, the exact packing of every word, async vs sync, and
  which pins carry what.  A host GUI, a testbench or a bring-up script should
  read this file instead of guessing;
* `tools/gen_rabbit_project.py` turns that manifest plus the pin-constraint file
  into a fully bound Rabbit project (`.rbtprj`), and
  `tools/check_rabbit_project.py` re-derives every expected pin from the
  constraint file to prove the bindings are right:

  ```bash
  python3 tools/gen_rabbit_project.py --manifest mac13__stream_manifest.json \
      --cons mac13_cons.xml --out mac13.rbtprj --name mac13 --bit mac13_bit.bit
  python3 tools/check_rabbit_project.py --project mac13.rbtprj --cons mac13_cons.xml
  ```

Rabbit's matching host-side component is `SeriWrap` (see
`Rabbit/doc/SeriWrapComponent.md`); it speaks the protocol described by the
manifest, including the `s_ready` back-pressure handshake.

---

## Usage

### BRAM from MIF

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

#### Preparing a MIF from an image

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

### PLL (limited testing)

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

### Stream IP

Wraps a user Verilog module with SIPO + PISO + dual-port BRAM, exposing a parallel N-bit serial interface (DATA + CLK + STROBE / DATA + CLK + DATA_VALID). Auto-detects data port width, input/output counts, and `start` / `done` / `busy` handshake ports. Use `--width` only if the module uses parameterized port widths that cannot be auto-detected.

```bash
# List modules and ports
$ python ip_main.py stream --source *.v --print-modules

# Generate wrapper (width auto-detected from first data port)
$ python ip_main.py stream \
    --source *.v \
    --top matrix_mult_3x3 \
    --bram-width 8 \
    --bram-depth 512 \
    --out-dir ./generated/
```

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--source` | *(required)* | Path to user Verilog source |
| `--top` | *(required)* | Top module name to wrap |
| `--bram-width` | 16 | BRAM data width |
| `--bram-depth` | 256 | BRAM depth |
| `--width` | 0 (auto) | Data port width override (0 = auto-detect from module) |
| `--baud-div` | 2 | System clocks per PISO CLK half-period |
| `--out-dir` | `.` | Output directory |
| `--debug` | — | Include overflow/idle/busy debug ports |
| `--print-modules` | — | Print module list and exit |

For a complete guide including protocol specification and simulation, see [STREAM_IP_GUIDE.md](STREAM_IP_GUIDE.md).

---

## Supported BRAM Configurations

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

---

## Contributing

To add a new SeriWrap or change the template engine, start from the templates
in [`templates/`](templates) and the generator in `stream_generator.py`.

---

## Author

[@FrancisCYH](https://github.com/FrancisCYH)
