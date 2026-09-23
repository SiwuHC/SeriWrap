# Extending SeriWrap

This guide is for developers who want to add a new IP type or modify the existing BRAM / PLL generators.

---

## Architecture Overview

```
┌─────────────┐     sub-command dispatch      ┌──────────────────┐
│  ip_main.py │  ───────────────────────────► │ xxx_generator.py │
│   (CLI)     │         JSON stdout           │  (logic + Jinja2)│
└─────────────┘                               └──────────────────┘
                                                        │
                                                        ▼
                                               ┌──────────────────┐
                                               │ templates/xxx.j2 │
                                               │  (Verilog shell) │
                                               └──────────────────┘
```

- **`ip_main.py`** — Unified entry point. Registers sub-commands (`bram`, `pll`, …) via `argparse`. Dispatches to the corresponding generator module and prints a JSON result to stdout.
- **`xxx_generator.py`** — Domain-specific logic (parameter validation, data transformation, INIT string calculation, etc.). Loads a Jinja2 template and renders it into Verilog code.
- **`templates/xxx.j2`** — Jinja2 template that defines the Verilog module skeleton. All hardware-specific details (primitive names, port lists, defparams) live here.

Every generator is reachable through the single `ip_main.py` CLI, which prints one JSON
object on stdout so that any caller (a shell script, a Makefile, a GUI) can tell success
from failure without parsing free-form text.

---

## Modifying `img2mif.py`

`img2mif.py` is **not** part of the unified `ip_main.py` pipeline. It is a standalone utility that produces MIF initialization files for the BRAM generator. Its architecture is deliberately simple.

### Architecture

```
Input image (PNG/JPG/BMP)
        │
        ▼
   PIL Image.open()
        │
        ├──► resize_keep_aspect()  ──► 128×64 grayscale
        │
        ├──► point(threshold_transform)  ──► 1-bit black/white
        │
        └──► 8-column vertical strip packing ──► MIF file
```

**Key functions**

| Function | Purpose |
|----------|---------|
| `image_to_mif()` | Main conversion pipeline: open → resize → threshold → pack → write MIF |
| `resize_keep_aspect()` | Scales the image to fit 128×64 while preserving aspect ratio (letterboxed on white) |
| `threshold_transform()` | Simple 128-level threshold: `0` for dark, `255` for light |
| `generate_test_pattern()` | Produces built-in patterns (`checker`, `vertical`, `horizontal`, `gradient`) without an input image |
| `generate_preview()` / `generate_preview_from_mif()` | Reverse operation: reads a MIF back and renders a 128×64 PNG for visual verification |

### Common Modifications

Because `img2mif.py` has no template engine and no sub-command registration, changes are usually limited to:

1. **Resolution**  
   The target size `128×64` is hard-coded in `image_to_mif()` and `generate_preview()`. If your GraphicLCD panel has a different resolution, change the `(128, 64)` tuple in both functions.

2. **Storage mode / bit mapping**  
   The current format is an **8-column vertical strip**: the image is divided into 16 tiles of 8×64 pixels, each tile stored row-by-row with `bit7 = leftmost`. If you need a different memory layout (e.g. horizontal scan-line), modify the nested loops inside `image_to_mif()` and keep `generate_preview()` symmetrical so the preview still matches.

3. **Threshold or dithering**  
   Replace `threshold_transform()` with Floyd-Steinberg dithering or an adaptive threshold if the output looks too noisy on the physical LCD.

4. **New test patterns**  
   Add a new branch in `generate_test_pattern()` and extend the `choices=` list in the CLI parser:
   ```python
   parser.add_argument('-t', '--test', choices=['checker', 'vertical', 'horizontal', 'gradient', 'diagonal'],
                      help='Generate test pattern instead of converting from image')
   ```

5. **MIF metadata**  
   The header comments inside `image_to_mif()` describe the format to human readers. Update them if you change the packing scheme.

### Build Note

`img2mif.py` is bundled separately via `img2mif.spec`. After modifying it, rebuild with:

```bash
$ pyinstaller img2mif.spec
```

---

## Adding a New SeriWrap

The fastest way is to copy the PLL generator (it is the simpler of the two) and adapt it.

### 1. Create the Generator Module

Create `<ip_name>_generator.py` next to the existing generators. It must expose at least one public function with this signature:

```python
def generate_<ip_name>(..., output_file: Optional[str] = None) -> dict:
    """
    Returns a dict with keys:
        success: bool
        output_file: str | None      # path written to disk
        verilog_code: str | None     # raw code when output_file is None
        message: str
        error: str | None
    """
```

**Mandatory conventions**

| Convention | Why it matters |
|------------|----------------|
| Return a `dict` with `'success'` (bool) | `ip_main.py` decides the exit code from this field. |
| Print **only** JSON to stdout when called from `ip_main.py` | Callers parse stdout as JSON. Extra print statements break every automated caller. |
| Raise `ValueError` for invalid parameters | The CLI wrapper in `ip_main.py` catches exceptions and turns them into JSON error responses automatically. |
| Use `templates/<ip_name>_template.j2` | Keeps logic and presentation separated; matches the existing pattern. |

### 2. Write the Jinja2 Template

Create `templates/<ip_name>_template.j2`.

**Template rules**

- Use `trim_blocks=True, lstrip_blocks=True` when constructing the Jinja2 `Environment` (this removes the extra whitespace that Jinja2 tags normally leave behind).
- Keep the file header consistent with existing templates:
  ```
  /*==============================================================
   *  Description: Generated by SeriWrap (<IP Name>)
   *  File: {{ output_file_name }}
   *  Author: SeriWrap
   *  Date: {{ generation_date }}
   *=============================================================*/
  ```
- All FPGA-primitive-specific details (primitive names, pin mappings, `defparam` blocks) belong **inside the template**, not in Python. This makes future porting to a different FPGA family a matter of editing templates rather than rewriting Python logic.

### 3. Register the Sub-command in `ip_main.py`

Open `ip_main.py` and make three changes.

**a) Import the new generator**

```python
import <ip_name>_generator
```

**b) Add a handler**

```python
def handle_<ip_name>(args) -> int:
    result = <ip_name>_generator.generate_<ip_name>(...)
    print(json.dumps(result))
    return 0 if result['success'] else 1
```

**c) Register the parser**

```python
<ip_name>_parser = subparsers.add_parser(
    '<ip_name>',
    help='Generate <IP Name> IP',
    description='Generate ...'
)
# add arguments ...
```

Then wire it into `main()`:

```python
elif args.command == '<ip_name>':
    return handle_<ip_name>(args)
```

### 4. Update the PyInstaller Spec

If `ip_main.py` gains new dependencies (additional generator modules or data files), edit `seriwrap.spec` so PyInstaller bundles them. The existing spec already includes `templates/`, so if you follow the naming conventions above no changes are usually needed.

Rebuild:

```bash
$ pyinstaller seriwrap.spec
```

### 5. Test Standalone First

Always test the new generator from the command line first:

```bash
$ python ip_main.py <ip_name> ... --output test.v
```

Check that:
1. `test.v` is syntactically valid Verilog (run through your synthesizer or a linter).
2. stdout contains **only** a single JSON object.
3. The JSON object has `"success": true` on success and `"success": false` plus an `"error"` message on failure.

---

## How the Existing Generators Work

### BRAM Generator (`bram_generator.py`)

**Input**: MIF file → `read_mif()` parses width, depth, address/data radix, and initial data.

**Validation**: `_VALID_COMBINATIONS` defines the 25 single-port and 75 dual-port configurations based on the 4 Kb `RAMB4_Sx` primitive. Port A and Port B must have equal total capacity (`width × depth`).

**Data transformation**: `generate_bram_ip()` slices the flat data array into `INIT_XX` strings:
- Each `RAMB4_Sx` primitive stores 256 bits per INIT parameter.
- For multi-module configurations (e.g. 32 Kb needs 8 primitives), the data bus is bit-sliced across modules.
- Binary data is collected high-address-first, padded to 256 bits, then converted to a 64-digit hex string.

**Template variables passed to Jinja2**:

| Variable | Meaning |
|----------|---------|
| `module_name` | Output file stem (from MIF name) |
| `width_A` / `depth_A` | Port A width and depth |
| `width_B` / `depth_B` | Port B width and depth (`0` / `1` for single-port) |
| `width_A_one` / `width_B_one` | Width per primitive (= total width ÷ `module_number`) |
| `depth_A` / `depth_B` | Passed as `int(math.log2(depth))` for address bus sizing |
| `module_number` | Number of `RAMB4_Sx` primitives in parallel |
| `init_data` | 2-D list: `init_data[module_idx][init_line]` → `256'h...` hex string |
| `generation_date` | `YYYY.MM.DD` timestamp |

### PLL Generator (`pll_generator.py`)

**Input**: `--divide` (2 / 4 / 8 / 16) and `--gates` (30 / 50).

**Validation**: `VALID_DIVIDE_VALUES` and `VALID_FPGA_GATES` are module-level constants. The CLI reads them dynamically so the help text stays in sync.

**Template variables**:

| Variable | Meaning |
|----------|---------|
| `divide_value` | Clock divide ratio |
| `fpga_gates` | 30 (DLL primitive) or 50 (DCM primitive) |

The template uses an `{% if fpga_gates == 30 %}` / `{% elif fpga_gates == 50 %}` branch because the two primitives have different pin lists and `defparam` sets.

---

## Automation Contract

Anything that drives SeriWrap (a shell script, a Makefile, a GUI front end) relies on
this contract:

1. **Entry point**: `ip_main.py` (or a frozen `seriwrap` executable built from `seriwrap.spec`).
2. **Arguments**: whatever sub-command and flags are registered in `ip_main.py`.
3. **stdout**: a single line of JSON.
   - Success: `{"success": true, "output_file": "...", "message": "..."}`
   - Failure: `{"success": false, "error": "...", "message": "..."}`
4. **Exit code**: `0` on success, `1` on failure.
5. **Output file**: the caller reads the path from `output_file` and copies or
   references it in its own project directory.

If you change this JSON schema, every caller has to be updated with it.

---

## Checklist for a New IP

- [ ] Generator module created with standard return dict
- [ ] Jinja2 template placed in `templates/`
- [ ] Sub-command registered in `ip_main.py`
- [ ] PyInstaller spec verified (templates/ included)
- [ ] Standalone CLI tested: output file generated, stdout is valid JSON
- [ ] Verilog output passes synthesis / lint
- [ ] Driven end-to-end through `ip_main.py` (JSON on stdout, exit code 0)
