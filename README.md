# IP-Generator

IP-Generator is a companion toolset for **UFDE+**. It generates Verilog IP cores — **Block RAM (BRAM)**, **Phase-Locked Loop (PLL)**, and **Stream Wrapper** — from high-level descriptions, which UFDE+ then instantiates in your design.

---

## What It Does

| Generator | Input | Output |
|-----------|-------|--------|
| **BRAM IP** | MIF file (memory initialization) | `test.v` — synthesizable Verilog RAM module |
| **PLL IP** | Divide ratio & gate count | `PLL_<divide>_<gates>.v` — clock multiplier module |
| **Stream IP** | User Verilog module | wrapper + SIPO + PISO + BRAM + top (5 files) |

> **Image → MIF** is a helper utility that produces initialization files for the BRAM generator. It converts PNG / JPG / BMP into the `*.mif` format consumed by `ip_main.py bram`.

The generators are used by UFDE's graphical IP Catalog. You can also run them standalone from the command line.

---

## Project Layout

```
IP-Generator/
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
│   └── stream_bridge_ps2.j2   # PS/2 bridge: SIPO drop-in for --input-source adapter
│
├── ip_generator.spec       # PyInstaller spec for ip_generator.exe
└── img2mif.spec            # PyInstaller spec for img2mif.exe
```

---

## Requirements

- **Python** ≥ 3.8
- **PyInstaller** (to build the executables UFDE consumes)
- **jinja2**, **Pillow**

```bash
$ pip install pyinstaller jinja2 Pillow
```

---

## Building the Executables

UFDE calls two bundled executables. Build them with PyInstaller using the provided `.spec` files:

```bash
# 1. Unified IP generator (BRAM + PLL + Stream)
$ pyinstaller ip_generator.spec

# 2. Image → MIF converter
$ pyinstaller img2mif.spec
```

After building you will find:

```
dist/
├── ip_generator.exe
└── img2mif.exe
```

### Integration with UFDE

Copy both executables into your `ufde-next` project directory (the exact location depends on how UFDE locates its helper tools; typically alongside the other bundled binaries). UFDE's IP Catalog will then invoke `ip_generator.exe` and `img2mif.exe` transparently when you configure BRAM, PLL, or Stream instances.

---

## Standalone Usage

You do not need UFDE to use the generators — they work perfectly from the command line.

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

To add a new IP generator or modify the template engine, see [`docs/EXTENDING_IP.md`](docs/EXTENDING_IP.md).

---

## Author

[@FrancisCYH](https://github.com/FrancisCYH)
