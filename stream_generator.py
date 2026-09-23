#!/usr/bin/env python3
"""
stream_generator.py — Stream BRAM SeriWrap.

Given a user's Verilog source file containing a top module with parallel
I/O data ports, produces:
  - <top>__stream_bram.v     (BRAM IP, via bram_generator API)
  - <top>__stream_wrapper.v  (wraps the user module with SIPO/PISO/BRAM)
  - <top>__stream_top.v      (new top exposing 3-wire serial interface)

The wrapper's user module is treated as a passthrough function:
output = f(input). All other user ports are tied to 0 (inputs) or
ignored (outputs).
"""

import os
import re
import json
import math
from datetime import datetime
import subprocess
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field

from jinja2 import Environment, FileSystemLoader, StrictUndefined

import bram_generator


# =============================================================================
#  Constants
# =============================================================================

# Trigger key name → key_state bit index (matches device_adapter_ps2.v).
# Only keys actually mapped to a dedicated bit are allowed; space (0x29)
# is NOT in the adapter mapping table (falls into the default bucket 63).
TRIGGER_BIT = {
    'enter': 40,
    'esc':   0,
    'tab':   27,
}
VALID_TRIGGER_KEYS = list(TRIGGER_BIT.keys()) + [k.upper() for k in TRIGGER_BIT]
VALID_INPUT_SOURCES = ('rabbit', 'adapter')

# Default handshake port-name aliases. Used when no --handshake-config file
# is passed and no per-role --*-ports CLI flags are given. Override either
# wholesale (config file) or per role (CLI flags).
DEFAULT_HANDSHAKE_PORTS = {
    'clock': ['clk', 'clock', 'ap_clk'],
    'reset': ['rst_n', 'rst', 'reset_n', 'reset', 'ap_rst', 'ap_rst_n'],
    'start': ['start', 'go', 'begin', 'trigger', 'ap_start'],
    'done':  ['done', 'valid', 'finish', 'complete', 'ap_done'],
    'busy':  ['busy', 'ready', 'ap_ready', 'ap_idle'],
}


# =============================================================================
#  Verilog parser (hand-rolled, no external Verilog library)
# =============================================================================

def _strip_comments(src: str) -> str:
    """Strip // line comments and /* ... */ block comments, preserving newlines."""
    out: List[str] = []
    i, n = 0, len(src)
    while i < n:
        if src[i:i+2] == '//':
            while i < n and src[i] != '\n':
                i += 1
        elif src[i:i+2] == '/*':
            while i < n and src[i:i+2] != '*/':
                if src[i] == '\n':
                    out.append('\n')
                i += 1
            i += 2
        elif src[i] == '"':
            # Preserve string literals
            out.append(src[i])
            i += 1
            while i < n and src[i] != '"':
                if src[i] == '\\' and i + 1 < n:
                    out.append(src[i])
                    i += 1
                out.append(src[i])
                i += 1
            if i < n:
                out.append(src[i])
                i += 1
        else:
            out.append(src[i])
            i += 1
    return ''.join(out)


_MOD_HDR = re.compile(
    r'\bmodule\s+(\w+)\s*'
    r'(?:#\s*\((?P<params>[^)]*)\)\s*)?'
    r'\((?P<ports>.*?)\)\s*;',
    re.DOTALL,
)

_PARAM_DECL = re.compile(r'^\s*(?:parameter|localparam)\s+(\w+)\s*=\s*([^,;\n]+)')

# Match a single ANSI-style port declaration chunk.
# Examples matched:
#   input  wire        clk
#   input  wire [15:0] data
#   output reg  signed [WIDTH-1:0] result
#   inout  [7:0] bus
#
# Capture groups: dir, optional [hi:lo], name. Type keywords (wire|reg|
# logic|signed|unsigned) are explicitly listed so we can require at least
# one of them OR a [range] before the name, preventing the regex from
# accidentally treating a type keyword as the port name.
_PORT_DECL = re.compile(
    r'\b(?P<dir>input|output|inout)\b'
    r'(?:\s+(?:wire|reg|logic|signed|unsigned))*\s*'
    r'(?:\[(?P<hi>[^\]]*?)\s*:\s*(?P<lo>[^\]]*?)\])?\s*'
    r'(?P<name>\w+)'
    r'(?P<array_dims>(?:\s*\[\s*-?\d+\s*:\s*-?\d+\s*\])*)',
    re.DOTALL,
)


def _split_top_level_commas(s: str) -> List[str]:
    """Split a string on commas that are not inside [] brackets."""
    parts: List[str] = []
    depth = 0
    cur: List[str] = []
    for ch in s:
        if ch == '[':
            depth += 1
            cur.append(ch)
        elif ch == ']':
            depth -= 1
            cur.append(ch)
        elif ch == ',' and depth == 0:
            parts.append(''.join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur:
        last = ''.join(cur).strip()
        if last:
            parts.append(last)
    return parts


_SAFE_EXPR = re.compile(r'^[0-9A-Za-z_+\-*/() \t]+$')


def _eval_const(expr, env):
    """Evaluate a constant Verilog-ish expression against a parameter map.

    Accepts only identifiers, integer literals and + - * / ( ), so it cannot
    execute anything but arithmetic.  Returns None when unresolvable.
    """
    if expr is None:
        return None
    e = expr.strip()
    if not e or not _SAFE_EXPR.match(e):
        return None
    try:
        return int(eval(e, {"__builtins__": {}}, dict(env)))  # noqa: S307 (restricted)
    except Exception:
        return None


def _collect_params(header_params, body):
    """Resolve module parameters that have constant values.

    Hand-written RTL almost always declares "parameter W = 32;" and then uses
    [W-1:0] on its ports.  Without resolving those, every such port was
    silently treated as 1 bit wide (see _parse_width_from_range), which
    produced functionally wrong wrappers: the generated mapping packed 32-bit
    ports into single bits of a BRAM word.  Dependencies between parameters
    (parameter W2 = 2*W) are resolved by iterating to a fixpoint.
    """
    raw = {n: v for n, v in header_params}
    for m in re.finditer(r'\b(?:parameter|localparam)\b([^;]*);', body):
        for chunk in _split_top_level_commas(m.group(1)):
            mm = re.match(r'\s*(?:\w+\s+)*?(\w+)\s*=\s*(.+)$', chunk.strip(), re.DOTALL)
            if mm:
                raw.setdefault(mm.group(1), mm.group(2).strip())
    env = {}
    for _ in range(4):                      # resolve parameter dependencies
        changed = False
        for name, expr in raw.items():
            if name in env:
                continue
            val = _eval_const(expr, env)
            if val is not None:
                env[name] = val
                changed = True
        if not changed:
            break
    return env


def _parse_width_from_range(hi, lo, env=None):
    """Try to compute width from a [hi:lo] range. Returns (width, used_param)."""
    if hi is None or lo is None:
        return 1, False
    hi_s, lo_s = hi.strip(), lo.strip()
    if not hi_s or not lo_s:
        return 1, False
    # Both literals
    try:
        h, l = int(hi_s, 0), int(lo_s, 0)
        return abs(h - l) + 1, False
    except ValueError:
        pass
    # Arithmetic / parameterised range: evaluate it against the module's
    # parameter values (an empty map still covers plain arithmetic such as the
    # [32-1:0] that a preprocessed macro turns into).
    h = _eval_const(hi_s, env or {})
    l = _eval_const(lo_s, env or {})
    if h is not None and l is not None:
        return abs(h - l) + 1, False
    # Still unknown: the caller must use the CLI width override.
    return 1, True


@dataclass
class Port:
    name: str
    direction: str  # "input" | "output" | "inout"
    width: int = 1
    signed: bool = False
    used_parameter: bool = False  # width could not be resolved from source
    # For 2D array ports (e.g. `A[0:3][0:3]`), the total number of
    # elements.  For scalar ports, this is 1.  Combined with `width`
    # (which stores the *bit* width of the entire port = bit_width
    # per element × array_count), this gives the full picture.
    array_count: int = 1


@dataclass
class ModuleInfo:
    name: str
    ports: List[Port] = field(default_factory=list)
    parameters: List[Tuple[str, str]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'name': self.name,
            'ports': [
                {
                    'name': p.name,
                    'dir': p.direction,
                    'width': p.width,
                    'signed': p.signed,
                    'used_parameter': p.used_parameter,
                    'array_count': p.array_count,
                }
                for p in self.ports
            ],
            'parameters': [{'name': n, 'default': d} for n, d in self.parameters],
        }


def _parse_array_dims(dims_str: str) -> Tuple[int, bool]:
    """Parse an array-dimension suffix like ' [0:3][0:3]'.

    Returns (total_elements, used_parameter).  Literal dims multiply
    together; any non-literal dim sets used_parameter=True (and returns
    1 for the total since we can't compute it).
    """
    if not dims_str:
        return 1, False
    total = 1
    used_param = False
    for m in re.finditer(r'\[\s*(-?\d+)\s*:\s*(-?\d+)\s*\]', dims_str):
        h, l = int(m.group(1)), int(m.group(2))
        n = abs(h - l) + 1
        total *= n
    return total, used_param


def _parse_ports(port_list: str, env=None) -> List[Port]:
    """Parse a comma-separated ANSI port list into Port records.

    A single ANSI chunk can declare multiple port names sharing the same
    direction and range, e.g. `input wire [15:0] a0, a1, a2, a3,`.  We
    find the leading `dir + [range]` and then split the remainder on
    commas to enumerate all names.

    For 2D array ports like `input wire [15:0] A [0:3][0:3]`, the
    total port bit-width is `bit_width × dim0 × dim1`.  The `width`
    field in Port stores the *bit* width (per element × total elements).

    For Verilog-1995 non-ANSI port lists (e.g. Vitis HLS output) where
    the module header is `module foo (a, b, c);` followed later by
    `input [31:0] a; input b; output [3:0] c;`, no chunk has a leading
    direction keyword.  We return Port(name=...) with direction='unknown'
    in that case and the caller is expected to fill it in from the
    module-body declarations.
    """
    ports: List[Port] = []
    chunks = _split_top_level_commas(port_list)
    current_dir: Optional[str] = None
    current_width: int = 1
    current_signed: bool = False
    current_used_param: bool = False
    current_array_count: int = 1
    for chunk in chunks:
        m = _PORT_DECL.search(chunk)
        if m:
            current_dir = m.group('dir')
            hi, lo = m.group('hi'), m.group('lo')
            bit_width, bit_used_param = (
                _parse_width_from_range(hi, lo, env) if hi and lo else (1, False)
            )
            array_total, array_used_param = _parse_array_dims(m.group('array_dims') or '')
            current_width = bit_width * array_total
            current_used_param = bit_used_param or array_used_param
            current_array_count = array_total
            current_signed = 'signed' in chunk
            name = m.group('name')
        else:
            m2 = re.search(r'\b(\w+)\s*$', chunk.strip())
            if not m2:
                continue
            name = m2.group(1)
            if current_dir is None:
                # Non-ANSI: keep the name, mark direction as unknown.
                ports.append(Port(name=name, direction='unknown'))
                continue
        ports.append(Port(
            name=name,
            direction=current_dir,
            width=current_width,
            signed=current_signed,
            used_parameter=current_used_param,
            array_count=current_array_count,
        ))
    return ports


def _find_module_end(src: str, start: int) -> int:
    """Return the index just past `endmodule` for the module whose header
    ends at or after `start`. Falls back to end-of-source if `endmodule`
    isn't found.
    """
    m = re.search(r'\bendmodule\b', src[start:])
    return start + m.end() if m else len(src)


# Match a Verilog-1995 port declaration: `input [W-1:0] NAME;` or `output NAME;`
# Use \s* (not \s+) around the optional keyword group so trailing spaces are
# not consumed away from the [hi:lo] range that follows.
_DECL_LINE = re.compile(
    r'\b(?P<dir>input|output|inout)\b\s*'
    r'(?:(?:wire|reg|logic|signed|unsigned)\s+)*'
    r'(?:\[(?P<hi>[^\]]*?)\s*:\s*(?P<lo>[^\]]*?)\])?\s*'
    r'(?P<name>\w+)'
    r'\s*[,;]'
)


def _scan_declared_ports(body: str, env=None) -> Dict[str, Tuple[str, int, bool, bool]]:
    """Scan a Verilog-1995 port-declaration block. Returns
    `name -> (direction, width, signed, used_parameter)`.

    Used to recover port metadata from HLS-generated non-ANSI files.
    """
    out: Dict[str, Tuple[str, int, bool, bool]] = {}
    for m in _DECL_LINE.finditer(body):
        d = m.group('dir')
        hi, lo = m.group('hi'), m.group('lo')
        w, up = _parse_width_from_range(hi, lo, env) if hi and lo else (1, False)
        signed = bool(re.search(r'\b(signed)\b', body[max(0, m.start()-20):m.end()]))
        out[m.group('name')] = (d, w, signed, up)
    return out


def _parse_params(param_list: str) -> List[Tuple[str, str]]:
    """Parse a parameter list like 'WIDTH=16, DEPTH=256' into (name, default) pairs."""
    out: List[Tuple[str, str]] = []
    for chunk in _split_top_level_commas(param_list):
        m = re.match(r'\s*(?:parameter\s+)?(\w+)\s*=\s*(.+)$', chunk.strip(), re.DOTALL)
        if m:
            out.append((m.group(1), m.group(2).strip()))
    return out


def preprocess_source(path: str) -> Optional[str]:
    """Expand `define/`ifdef macros with Verilator's preprocessor.

    The internal parser sizes ports from the *text* of the range expression.
    A macro width (`WID-1:0) cannot be resolved that way, so the port used to
    silently default to 1 bit.  Running the file through "verilator -E" first
    turns it into ordinary arithmetic ("[32-1:0]") that the parser handles.
    Returns None when no preprocessor is available.
    """
    for cmd in (['verilator', '-E', path], ['iverilog', '-E', '-o', '-', path]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0 and out.stdout.strip():
            # Drop `line directives: they would confuse the regex parser.
            return '\n'.join(l for l in out.stdout.splitlines()
                             if not l.lstrip().startswith('`line'))
    return None


def parse_modules(path: str, handshake_ports: Optional[Dict[str, List[str]]] = None,
                  preprocess: bool = False) -> Dict[str, Any]:
    """Parse a Verilog file and return a dict of module name → ModuleInfo.

    `handshake_ports` is a role→port-names map (e.g. {'clock': ['clk','ap_clk'], ...}).
    Currently informational — the parser does not classify ports, but the
    argument is reserved for forward compatibility (e.g. annotating which
    ports are clock/reset so downstream tools can mark them).
    """

    try:
        with open(path, 'r', encoding='utf-8') as f:
            src = f.read()
    except OSError as e:
        return {'success': False, 'error': str(e), 'message': f'Cannot read {path}', 'modules': []}
    if preprocess:
        expanded = preprocess_source(path)
        if expanded is None:
            return {'success': False,
                    'error': '--preprocess requires verilator or iverilog on PATH',
                    'message': 'No preprocessor available'}
        src = expanded

    stripped = _strip_comments(src)
    modules: List[ModuleInfo] = []
    for m in _MOD_HDR.finditer(stripped):
        mod = ModuleInfo(name=m.group(1))
        params_raw = m.group('params') or ''
        mod.parameters = _parse_params(params_raw)
        # Resolve literal parameters first so [W-1:0] style ports are sized
        # correctly (hand-written RTL relies on this).
        body_end = _find_module_end(stripped, m.end())
        _env = _collect_params(mod.parameters, stripped[m.end():body_end])
        mod.ports = _parse_ports(m.group('ports'), _env)
        # Verilog-1995 non-ANSI port lists contain only names; directions and
        # widths are declared later in `input NAME;` / `output [W-1:0] NAME;`
        # form (Vitis HLS uses this style). If _parse_ports couldn't resolve
        # any direction, scan the body for those declarations and fill them in.
        if mod.ports and all(p.direction == 'unknown' for p in mod.ports):
            decls = _scan_declared_ports(stripped[m.end():body_end], _env)
            for p in mod.ports:
                if p.name in decls:
                    d, w, s, up = decls[p.name]
                    p.direction = d
                    if w != 1:
                        p.width = w
                    p.signed = s
                    p.used_parameter = up
        modules.append(mod)

    return {
        'success': True,
        'modules': [mod.to_dict() for mod in modules],
        'message': f'Parsed {len(modules)} module(s) from {os.path.basename(path)}',
    }


# =============================================================================
# =============================================================================
#  Generator
# =============================================================================

def _validate_bram(width: int, depth: int, bram_type: str = "ram4s") -> Optional[str]:
    """Return an error message if (width, depth) is not supported for the
    given BRAM primitive family, else None.
    """
    if bram_type not in bram_generator.BRAM_TYPES:
        return f"Unknown BRAM type: {bram_type}. Valid: {list(bram_generator.BRAM_TYPES.keys())}"
    valid = bram_generator.BRAM_TYPES[bram_type]["valid_combinations"]
    if valid is None:
        return None   # generic accepts any
    if (width, depth) not in valid:
        sorted_valid = sorted(valid)
        return (
            f"BRAM ({width} x {depth}) is not a supported combination for "
            f"bram_type={bram_type}. See BRAM IP docs for the list of "
            f"valid (width, depth) pairs."
        )
    return None


def _addr_w(depth: int) -> int:
    return max(1, int(math.ceil(math.log2(depth))))


def _render(template_dir: str, name: str, ctx: Dict[str, Any]) -> str:
    env = Environment(
        loader=FileSystemLoader(template_dir),
        trim_blocks=True,
        lstrip_blocks=True,
        # StrictUndefined: a template variable the generator forgot to pass used
        # to render as an empty string, i.e. silently broken or invalid Verilog.
        undefined=StrictUndefined,
    )
    env.globals['range'] = range
    env.globals['len'] = len
    return env.get_template(f'templates/{name}').render(**ctx)


def _ensure_trailing_newline(s: str) -> str:
    return s if s.endswith('\n') else s + '\n'


def _compute_chunks(data_ports: List[Dict], bram_width: int) -> Tuple[List[Dict], int]:
    """Break heterogeneous-width data ports into BRAM-width chunks.

    Each data port is a dict with keys: name, width, array_count.
    Returns (chunked_ports, total_chunks) where chunked_ports is:
        [{name, width, array_count, chunks: [{bram_idx, hi, lo}]}, ...]
    and total_chunks is the INPUT_COUNT or OUTPUT_COUNT.
    """
    chunked: List[Dict] = []
    bram_idx = 0
    for p in data_ports:
        pw = p['width']
        ac = p.get('array_count', 1)
        n_chunks = max(1, (pw + bram_width - 1) // bram_width)
        port_chunks = []
        for c in range(n_chunks):
            lo = c * bram_width
            hi = min(pw, (c + 1) * bram_width) - 1
            # bram_lo/bram_hi: where the chunk sits INSIDE its serial word.
            # The unpacked layout gives every chunk a whole word, so the field
            # starts at bit 0 -- the mapping file used to print "[?:?]" here,
            # which told the host nothing.
            port_chunks.append({'bram_idx': bram_idx, 'hi': hi, 'lo': lo,
                                'bram_lo': 0, 'bram_hi': hi - lo})
            bram_idx += 1
        chunked.append({
            'name': p['name'],
            'width': pw,
            'array_count': ac,
            'chunks': port_chunks,
        })
    return chunked, bram_idx


def _compute_chunks_binpack(data_ports: List[Dict], bram_width: int) -> Tuple[List[Dict], int]:
    """Break heterogeneous-width data ports into BRAM-width chunks using FFD bin-packing.

    Unlike the greedy sequential allocator, this packs narrow ports (width < bram_width)
    into shared BRAM entries, reducing INPUT_COUNT/OUTPUT_COUNT.

    Algorithm: First-Fit Decreasing (FFD)
      1. Split ports wider than bram_width into full-width chunks, leaving one remainder.
      2. Collect all remainder chunks and narrow ports as "items".
      3. Sort items by width descending.
      4. For each item, find the first BRAM entry with ≥ item.width free bits;
         if none, create a new entry.
    """
    # Phase 1: separate full-width chunks from remainders
    class _Item:
        __slots__ = ('port_name', 'bit_hi', 'bit_lo', 'width')
        def __init__(self, port_name, bit_hi, bit_lo):
            self.port_name = port_name
            self.bit_hi = bit_hi
            self.bit_lo = bit_lo
            self.width = bit_hi - bit_lo + 1

    full_chunks: List[Dict] = []   # port-level entries (reserve bram_idx later)
    narrow_items: List[_Item] = []

    for p in data_ports:
        pw = p['width']
        n_full = pw // bram_width
        remainder = pw % bram_width

        # Full-width chunks: each occupies exactly one BRAM entry
        for c in range(n_full):
            lo = c * bram_width
            hi = lo + bram_width - 1
            full_chunks.append({
                'port_name': p['name'],
                'port_bit_hi': hi,
                'port_bit_lo': lo,
                'width': bram_width,
                'is_full': True,
            })

        # Remainder (or the whole port if it's narrow).  A port with
        # pw < bram_width has n_full == 0 and remainder == pw > 0, so this
        # covers the entirely-narrow case too (the old "elif n_full == 0"
        # branch was unreachable).
        if remainder > 0:
            narrow_items.append(_Item(p['name'], pw - 1, pw - remainder))

    # Phase 2: sort narrow items by width descending
    narrow_items.sort(key=lambda it: it.width, reverse=True)

    # Phase 3: FFD bin-packing
    # Each bin is a BRAM entry: free_bits is the remaining capacity.
    bins: List[List[_Item]] = []

    for item in narrow_items:
        placed = False
        for b in bins:
            used = sum(it.width for it in b)
            if bram_width - used >= item.width:
                b.append(item)
                placed = True
                break
        if not placed:
            bins.append([item])

    # Phase 4: assemble chunked output
    # Full chunks get bram_idx first, then bins
    chunked: List[Dict] = []
    port_map: Dict[str, List[Dict]] = {}  # port_name → [chunks]

    bram_idx = 0

    # Full chunks: one per entry
    for fc in full_chunks:
        chunk = {
            'bram_idx': bram_idx,  'bram_hi': bram_width - 1, 'bram_lo': 0,
            'port_hi': fc['port_bit_hi'], 'port_lo': fc['port_bit_lo'],
        }
        port_map.setdefault(fc['port_name'], []).append(chunk)
        bram_idx += 1

    # Bin-packed items
    for b in bins:
        bit_cursor = 0
        for item in b:
            chunk = {
                'bram_idx': bram_idx,
                'bram_hi': bit_cursor + item.width - 1,
                'bram_lo': bit_cursor,
                'port_hi': item.bit_hi,
                'port_lo': item.bit_lo,
            }
            bit_cursor += item.width
            port_map.setdefault(item.port_name, []).append(chunk)
        bram_idx += 1

    # Reconstruct per-port chunk lists in declaration order
    for p in data_ports:
        chunks = port_map.get(p['name'], [])
        chunked.append({
            'name': p['name'],
            'width': p['width'],
            'array_count': p.get('array_count', 1),
            'chunks': chunks,
        })

    return chunked, bram_idx


def _get_pipeline_depth(bram_type: str) -> int:
    """Return the LAUNCH pipeline depth for a BRAM type.

    Every memory this tool emits has a 1-clock REGISTERED read (RAMB4_S*,
    RAMB8BWER, RAMB18E1, RAMB36E1 and the behavioral 'generic' model), so the
    3-state LAUNCH pipeline is required for all of them.  The depth is the
    NUMBER OF PIPELINE STATES, not the read latency.

    History: ram4s/ram8b/generic used to return 1, which selected a 1-state
    capture that is only valid for a *combinational* read.  With a registered
    read that path returns word k-1 for word k, i.e. every input word except
    the first was wrong (verified by simulation against a registered-read
    model).  A register-file path (depth=0) eliminates LAUNCH entirely.
    """
    return 3


_REGISTER_THRESHOLD_BITS = 384  # Max total buffered bits for register-file path.
                            # Kernels with ≤ 384 bits per side use zero-BRAM mode.
                            # 384 bits = 48 FFs at 8-bit or 12 FFs at 32-bit.


def _output_word_assigns(output_chunks_flat: List[Dict], bram_width: int):
    """Build one FULL-WIDTH assignment expression per serial output word.

    Returns [(word_index, verilog_expr), ...].  Concatenating every field of the
    word in bit order and zero-padding the gaps means the whole word is driven
    every time, so a partially filled word (or a word that several ports share
    under --binpack) can never leave undriven bits on the serial output.  The
    register-file template used to assign the covered bit ranges only.
    """
    assigns = []
    words = sorted({c.get('bram_idx', 0) for c in output_chunks_flat})
    for w in words:
        fields = sorted([c for c in output_chunks_flat if c.get('bram_idx', 0) == w],
                        key=lambda c: -c.get('bram_hi', 0))
        parts, pos = [], bram_width
        for c in fields:
            hi = c.get('bram_hi', 0)
            lo = c.get('bram_lo', 0)
            if pos > hi + 1:
                parts.append(f"{pos - hi - 1}'b0")
            if 'port_hi' in c:
                parts.append(f"user_out_w_{c['port_name']}[{c['port_hi']}:{c['port_lo']}]")
            else:
                parts.append(f"user_out_w_{c['port_name']}[{c['hi']}:{c['lo']}]")
            pos = lo
        if pos > 0:
            parts.append(f"{pos}'b0")
        if not parts:
            assigns.append((w, f"{bram_width}'b0"))
        elif len(parts) == 1:
            assigns.append((w, parts[0]))
        else:
            assigns.append((w, "{" + ", ".join(parts) + "}"))
    return assigns


def _should_use_reg_file(chunk_count: int, bram_width: int) -> bool:
    """Decide whether to use a register file instead of BRAM for the input path."""
    if _REGISTER_THRESHOLD_BITS <= 0:
        return False
    return chunk_count * bram_width <= _REGISTER_THRESHOLD_BITS


def _auto_bram_config(
    data_input_ports: List[Dict],
    data_output_ports: List[Dict],
    bram_type: str,
    use_binpack: bool = False,
) -> Tuple[int, int]:
    """Automatically select optimal (bram_width, bram_depth) from valid combinations.

    Objective: minimize INPUT_COUNT + OUTPUT_COUNT (proxy for total cycle count).
    Subject to: depth >= max(input_count, output_count), width >= max(port_width).

    Returns (width, depth). Falls back to (8, 512) if no valid combo found.
    """
    all_ports = data_input_ports + data_output_ports
    if not all_ports:
        return 8, 512

    max_port_w = max(p['width'] for p in all_ports)

    valid_set = bram_generator.BRAM_TYPES[bram_type]['valid_combinations']
    if valid_set is None:
        # generic type: accept any, choose based on port widths
        candidate_w = max(max_port_w, 8)
        return candidate_w, 512

    best_w, best_d = 8, 512
    best_cost = 10**9

    # sorted(): 'valid_set' is a set, so iterating it directly made the chosen
    # geometry depend on set ordering -- the same command could emit a different
    # wrapper on another Python build.  Ties on cost are broken towards the
    # SMALLEST depth, which also minimises the address width.
    for w, d in sorted(valid_set):
        # A port WIDER than the word is perfectly fine: the chunker splits it
        # into ceil(width/w) words.  The old 'if w < max_port_w: continue'
        # filter rejected every candidate as soon as one port exceeded 32 bits,
        # so any kernel with a wide port (aes: ctx_i[768], spmv: ...) silently
        # fell back to (8, 512) -- a legal but far from optimal geometry
        # (4x more serial words per frame, and 5 BRAMs instead of 1 for spmv).
        # Score with the marshaling that will actually be used, so the
        # selected configuration is optimal for the emitted layout.
        _chunk = _compute_chunks_binpack if use_binpack else _compute_chunks
        _, in_count = _chunk(data_input_ports, w)
        _, out_count = _chunk(data_output_ports, w)
        if max(in_count, out_count) > d:
            continue
        cost = in_count + out_count
        if cost < best_cost or (cost == best_cost and d < best_d):
            best_cost = cost
            best_w, best_d = w, d

    return best_w, best_d


def _write_mapping_doc(out_dir: str, top_module: str,
                       input_chunks_flat: List[Dict], output_chunks_flat: List[Dict],
                       bram_width: int, bram_depth: int,
                       num_in_brams: int, num_out_brams: int,
                       input_count: int, output_count: int) -> None:
    """Write a human-readable data-layout mapping file (_mapping.txt).

    Documents the serial word order that the host must follow to pack/unpack
    data for the generated wrapper.  Each line shows:
        serial_word_index  BRAM_entry_index  [BRAM_instance]  port_name  bit_range
    """
    mapping_path = os.path.join(out_dir, f"{top_module}__stream_mapping.txt")
    lines = []
    lines.append(f"Data Layout Mapping for {top_module}__stream_wrapper")
    lines.append(f"BRAM: WIDTH={bram_width}, DEPTH={bram_depth}")
    lines.append(f"Input BRAMs:  {num_in_brams}  (total entries: {input_count})")
    lines.append(f"Output BRAMs: {num_out_brams} (total entries: {output_count})")
    lines.append("")
    lines.append("=== INPUT (host → wrapper serial order) ===")

    def _bram_label(idx: int, depth: int) -> str:
        if depth <= 0:
            return ""
        bram_num = idx // depth
        intra = idx % depth
        return f"BRAM[{bram_num}][{intra}]"

    if input_chunks_flat:
        lines.append(f"{'Serial#':<8} {'Entry':<16} {'Port':<20} {'Port bits':<18} {'BRAM bits'}")
        lines.append("-" * 80)
        # Input chunks are sorted by bram_idx
        sorted_in = sorted(input_chunks_flat, key=lambda c: (c.get('bram_idx', 0), c.get('bram_lo', 0)))
        for c in sorted_in:
            # The serial word index IS the BRAM entry index, not a row counter:
            # with --binpack several chunks share one word and must therefore
            # print the SAME Serial# (a host reading distinct numbers here used
            # to send too many words).
            bi = c.get('bram_idx', 0)
            label = _bram_label(bi, bram_depth)
            if 'port_hi' in c:
                port_bits = f"[{c['port_hi']}:{c['port_lo']}]"
            else:
                port_bits = f"[{c['hi']}:{c['lo']}]"
            bram_bits = f"[{c.get('bram_hi', '?')}:{c.get('bram_lo', '?')}]"
            lines.append(f"{bi:<8} {label:<16} {c.get('port_name','?'):<20} {port_bits:<18} {bram_bits}")
    else:
        lines.append("  (no input data ports)")

    lines.append("")
    lines.append("=== OUTPUT (wrapper → host serial order) ===")
    if output_chunks_flat:
        lines.append(f"{'Serial#':<8} {'Entry':<16} {'Port':<20} {'Port bits':<18} {'BRAM bits'}")
        lines.append("-" * 80)
        sorted_out = sorted(output_chunks_flat, key=lambda c: (c.get('bram_idx', 0), c.get('bram_lo', 0)))
        for c in sorted_out:
            bi = c.get('bram_idx', 0)
            label = _bram_label(bi, bram_depth)
            if 'port_hi' in c:
                port_bits = f"[{c['port_hi']}:{c['port_lo']}]"
            else:
                port_bits = f"[{c['hi']}:{c['lo']}]"
            bram_bits = f"[{c.get('bram_hi', '?')}:{c.get('bram_lo', '?')}]"
            lines.append(f"{bi:<8} {label:<16} {c.get('port_name','?'):<20} {port_bits:<18} {bram_bits}")
    else:
        lines.append("  (no output data ports)")

    lines.append("")
    with open(mapping_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')



def _chunk_fields(chunks_flat):
    """Group flat chunk records by serial word index -> [{word, fields:[...]}]."""
    words = {}
    for c in chunks_flat:
        w = c.get('bram_idx', 0)
        # "port bits" is the slice of the user port this field carries; for the
        # unpacked layout the chunker stores hi/lo instead of port_hi/port_lo.
        if 'port_hi' in c:
            phi, plo = c['port_hi'], c['port_lo']
        else:
            phi, plo = c['hi'], c['lo']
        words.setdefault(w, []).append({
            'port': c.get('port_name', '?'),
            'port_bits': [phi, plo],
            'word_bits': [c.get('bram_hi', phi - plo), c.get('bram_lo', 0)],
        })
    out = []
    for w in sorted(words):
        fields = sorted(words[w], key=lambda x: -x['word_bits'][0])
        out.append({'word': w, 'fields': fields})
    return out


def _write_manifest(out_dir: str, top_module: str, *,
                    input_chunks_flat, output_chunks_flat,
                    bram_width: int, bram_depth: int,
                    num_in_brams: int, num_out_brams: int,
                    input_count: int, output_count: int,
                    storage_mode: str, bram_type: str,
                    sync_mode: bool, input_source: str,
                    baud_div: int, baud_div_in, baud_div_out,
                    data_input_ports, data_output_ports,
                    clock_port, reset_port, reset_polarity,
                    start_port, done_port, ready_signal: bool,
                    adapters) -> None:
    """Write the machine-readable description of the generated wrapper.

    This is the contract between SeriWrap and anything that has to drive the
    wrapper from the outside (a host GUI, a testbench, a bring-up script): it
    states the serial word width, how many words a frame is, the exact packing
    of every word, whether the wrapper expects its word boundary to be defined
    by an edge (async) or by a clock (sync), and which pins carry what.

    The human-readable *_stream_mapping.txt is the same information in tables;
    this file is the one a program should read.
    """
    def ports(lst):
        return [{'name': p['name'], 'width': p['width']} for p in lst]

    manifest = {
        'format': 'seriwrap-manifest/1',
        'generator': 'SeriWrap',
        'design': {
            'kernel': top_module,
            'wrapper': f'{top_module}__stream_wrapper',
            'top': f'{top_module}__stream_top',
        },
        'storage': {
            'mode': storage_mode,                 # bram | register_file | pingpong
            'primitive': bram_type,
            'word_width': bram_width,
            'depth': bram_depth,
            'input_banks': num_in_brams,
            'output_banks': num_out_brams,
        },
        'link': {
            'width': bram_width,
            'sync_mode': bool(sync_mode),
            # In sync mode the wrapper streams one word per system clock and
            # s_clk_out is tied low; in async mode s_clk_out toggles every
            # BAUD_DIV clocks and the word boundary is that clock edge.
            'word_boundary': 'clock' if sync_mode else 'edge',
            'baud_div': baud_div,
            'baud_div_in': baud_div_in,
            'baud_div_out': baud_div_out,
            'input_source': input_source,         # rabbit | adapter
            'adapters': [a.get('name') for a in (adapters or [])],
        },
        'frame': {
            'input_words': input_count,
            'output_words': output_count,
            'has_input': input_count > 0,
            'has_output': output_count > 0,
            'handshake': {
                'ready': bool(ready_signal),
                'start': start_port,
                'done': done_port,
            },
        },
        'signals': {
            'clock': clock_port or 'clk',
            'reset': reset_port or 'rst_n',
            'reset_polarity': reset_polarity,
            'serial_in': {'data': 's_data_in', 'clock': 's_clk_in',
                          'strobe': 's_strobe_in'},
            'serial_out': {'data': 's_data_out', 'clock': 's_clk_out',
                           'valid': 's_data_valid'},
            'ready': 's_ready' if ready_signal else None,
        },
        'ports': {
            'inputs': ports(data_input_ports),
            'outputs': ports(data_output_ports),
        },
        'packing': {
            # packing['input'][k] describes serial word k on the host->wrapper
            # link: which user port feeds it and which bits of the word carry
            # it.  Several ports can share one word (--binpack).
            'input': _chunk_fields(input_chunks_flat),
            'output': _chunk_fields(output_chunks_flat),
        },
    }
    path = os.path.join(out_dir, f'{top_module}__stream_manifest.json')
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(manifest, fh, indent=2)
        fh.write('\n')


def generate_stream_ip(
    source_file: str,
    top_module: str,
    bram_width: int,
    bram_depth: int,
    out_dir: str = '.',
    baud_div: int = 2,
    baud_div_in: Optional[int] = None,
    baud_div_out: Optional[int] = None,
    include_sipo: bool = True,
    include_piso: bool = True,
    debug: bool = False,
    width: int = 0,
    control_inputs: Optional[List[str]] = None,
    control_outputs: Optional[List[str]] = None,
    pingpong: bool = False,
    bram_type: str = "ram4s",
    adapters: Optional[List[Dict[str, Any]]] = None,
    input_source: str = 'rabbit',
    trigger_key: str = 'enter',
    numeric_width: int = 32,
    handshake_ports: Optional[Dict[str, List[str]]] = None,
    sync_mode: bool = False,
    use_binpack: bool = False,
    ready_signal: bool = True,
    reset_polarity: str = 'auto',
    preprocess: bool = False,
) -> Dict[str, Any]:
    """Generate stream wrapper Verilog files for a user module.

    Port classification (priority order):
      1. clk / rst_n           → clock/reset
      2. start / done / busy   → handshake (auto-detected by name)
      3. --control-inputs/--control-outputs → exposed on wrapper top-level
      4. everything else       → data port (serialized through BRAM)

    Heterogeneous widths are now supported: each data port can have a
    different bit width.  Ports wider than bram_width span multiple BRAM
    entries; ports narrower use 1 entry (high bits zeroed on input,
    masked on output).

    If pingpong=True, generates a double-buffered wrapper with 4 BRAM
    instances and a pipelined FSM.

    If input_source='adapter', the SIPO block is replaced with a
    "bridge" module that listens to a PS/2 adapter trigger key
    (e.g. Enter) rising edge and pulses src_done for 1 cycle.  The
    FSM is otherwise identical — the bridge is a drop-in replacement
    for SIPO.  Requires adapters to include a 'ps2' binding with
    key_state output.

    If bram_width=0, automatically selects optimal (width, depth) from
    the BRAM type's valid combinations.  use_binpack enables FFD
    bin-packing port marshaling.  baud_div_in/out allow asymmetric
    serial rates for input vs output.
    """

    # ── Resolve input_source / trigger_key syntax ────────────────────
    input_source_norm = (input_source or 'rabbit').lower()
    if input_source_norm not in VALID_INPUT_SOURCES:
        return {
            'success': False,
            'error': f"Unknown --input-source '{input_source}'. "
                     f"Valid: {list(VALID_INPUT_SOURCES)}",
            'message': 'Unknown --input-source',
        }

    trigger_key_norm = (trigger_key or 'enter').lower()
    if input_source_norm == 'adapter':
        if trigger_key_norm not in TRIGGER_BIT:
            return {
                'success': False,
                'error': f"Unknown --trigger-key '{trigger_key}'. "
                         f"Valid: {list(TRIGGER_BIT.keys())}",
                'message': 'Unknown --trigger-key',
            }
        if pingpong:
            return {
                'success': False,
                'error': "--input-source adapter is incompatible with --pingpong",
                'message': 'Bridge mode requires single-buffer wrapper',
            }
        if not include_sipo:
            return {
                'success': False,
                'error': "--input-source adapter is incompatible with --no-sipo. "
                         "Use adapter mode (which inherently skips SIPO) or "
                         "remove the --no-sipo flag.",
                'message': 'Adapter mode already replaces SIPO',
            }
        # Adapter mode requires an event-mode adapter binding.  Any
        # of ps2 / uart / gpio produces a 1-cycle src_done pulse via
        # the bridge module.
        valid_event_adapters = {'ps2', 'uart', 'gpio'}
        has_event_adapter = any(
            ad.get('name') in valid_event_adapters for ad in (adapters or [])
        )
        if not has_event_adapter:
            return {
                'success': False,
                'error': "--input-source adapter requires --adapter "
                         "ps2:.../uart:.../gpio:...",
                'message': 'Missing event-mode adapter for bridge',
            }

    # ── Divider sanity ────────────────────────────────────────────────
    for _name, _val in (('--baud-div', baud_div), ('--baud-div-in', baud_div_in),
                        ('--baud-div-out', baud_div_out)):
        if _val is not None and int(_val) < 1:
            return {
                'success': False,
                'error': f"{_name} must be >= 1 (got {_val}): a zero divider "
                         f"never produces a serial-clock tick and the PISO "
                         f"would hang.",
                'message': 'Invalid baud divider',
            }

    # ── Parse user Verilog ────────────────────────────────────────────
    parsed = parse_modules(source_file, preprocess=preprocess)
    if not parsed.get('success'):
        return parsed
    mod_dict = next((m for m in parsed['modules'] if m['name'] == top_module), None)
    if mod_dict is None:
        return {
            'success': False,
            'error': f"Module '{top_module}' not found in {source_file}",
            'message': f"Module '{top_module}' not found",
            'modules': parsed['modules'],
        }
    ports = [Port(name=p['name'], direction=p['dir'],
                  width=p['width'], signed=p['signed'],
                  used_parameter=p['used_parameter'],
                  array_count=p.get('array_count', 1)) for p in mod_dict['ports']]

    # Apply --width override: for ports with unresolved parameterized widths,
    # use the user-supplied value.  This affects only ports where the parser
    # returned width=1 with used_parameter=True.
    if width > 1:
        for p in ports:
            if p.used_parameter and p.direction in ('input', 'output'):
                p.width = width

    # A port whose range could not be evaluated would be treated as 1 bit
    # wide, which silently produces a functionally wrong wrapper (this is
    # exactly how the hand-written pilot kernels were mis-wrapped).  Fail
    # loudly and tell the user how to proceed.
    unresolved = [p.name for p in ports
                  if p.used_parameter and p.direction in ('input', 'output', 'inout')]
    if unresolved:
        return {
            'success': False,
            'error': (f"Port(s) {unresolved} have a width that could not be "
                      f"resolved from the source (parameter or macro).  They "
                      f"would default to 1 bit, producing a wrong wrapper.  "
                      f"Pass --width <W> to set the data width explicitly, or "
                      f"--preprocess to expand macros first."),
            'message': 'Unresolved port width',
        }

    if control_inputs is None:
        control_inputs = []
    if control_outputs is None:
        control_outputs = []

    if adapters is None:
        adapters = []

    ctrl_in_set  = set(control_inputs)
    ctrl_out_set = set(control_outputs)

    # Build set of port names claimed by adapters (these are routed
    # straight from adapter outputs to user module, bypassing SIPO/BRAM).
    adapter_claimed_ports: Dict[str, Dict[str, Any]] = {}
    for ad in adapters:
        if ad.get('name') not in ('ps2', 'uart', 'gpio'):
            return {
                'success': False,
                'error': f"Unknown adapter type '{ad.get('name')}'. "
                         f"Implemented: ps2, uart, gpio.",
                'message': 'Unknown adapter type',
            }
        if not ad.get('ports'):
            return {
                'success': False,
                'error': f"Adapter '{ad.get('name')}' lists no ports. "
                         f"Use --adapter {ad.get('name')}:PORT[,PORT...].",
                'message': 'Adapter without ports',
            }
        for pname in ad.get('ports', []):
            adapter_claimed_ports[pname] = ad

    # ── Auto-select BRAM config if width=0 ─────────────────────────
    # Must run BEFORE validation since auto-config produces valid values.
    if bram_width <= 0:
        # data_input_ports / data_output_ports aren't computed yet at this
        # point.  We parse the module first (port classification below),
        # then run auto-config, then validate.  Move auto-config after
        # port classification.
        pass

    # ── BRAM validation ───────────────────────────────────────────────
    if bram_width > 0:  # only validate if width is user-specified
        err = _validate_bram(bram_width, bram_depth, bram_type)
        if err:
            return {'success': False, 'error': err, 'message': err}

    # ── BRAM depth check (adapter mode) ───────────────────────────────
    if input_source_norm == 'adapter' and bram_width > 0:
        bram_depth_min = (numeric_width + bram_width - 1) // bram_width
        if bram_depth < bram_depth_min:
            return {
                'success': False,
                'error': (f"--bram-depth ({bram_depth}) is too small for "
                          f"--numeric-width ({numeric_width}) at WIDTH={bram_width}. "
                          f"Need at least ceil({numeric_width}/{bram_width}) = "
                          f"{bram_depth_min}."),
                'message': 'BRAM depth insufficient for numeric_width',
            }

    # ── Port classification ──────────────────────────────────────────
    addr_w = _addr_w(bram_depth)
    # Resolve handshake port-name aliases: defaults < file < CLI overrides.
    # Caller has already merged these; missing keys fall back to defaults.
    hp = {**DEFAULT_HANDSHAKE_PORTS, **(handshake_ports or {})}
    clock_names = set(hp.get('clock', []))
    reset_names = set(hp.get('reset', []))
    start_names = set(hp.get('start', []))
    done_names  = set(hp.get('done',  []))
    busy_names  = set(hp.get('busy',  []))

    start_port = None
    done_port = None
    busy_port = None
    clock_port = None
    reset_port = None
    has_busy_port = False

    # Vectors for template rendering
    control_input_ports = []   # exposed on wrapper top-level
    control_output_ports = []  # exposed on wrapper top-level
    data_input_ports = []      # serialized through SIPO→BRAM
    data_output_ports = []     # serialized through BRAM→PISO
    adapter_input_ports: Dict[str, List[Dict[str, Any]]] = {}  # per-adapter port lists

    for p in ports:
        lname = p.name.lower()
        if p.direction == 'input':
            if lname in clock_names:
                clock_port = p.name
                continue
            elif lname in reset_names:
                reset_port = p.name
                continue
            elif lname in start_names:
                start_port = p.name
            elif p.name in ctrl_in_set:
                control_input_ports.append({
                    'name': p.name, 'width': p.width, 'signed': p.signed,
                })
            elif p.name in adapter_claimed_ports:
                ad = adapter_claimed_ports[p.name]
                adapter_input_ports.setdefault(ad['name'], []).append({
                    'name': p.name, 'width': p.width, 'signed': p.signed,
                })
            else:
                data_input_ports.append({
                    'name': p.name, 'width': p.width,
                    'array_count': p.array_count, 'signed': p.signed,
                })
        elif p.direction == 'output':
            if lname in done_names:
                done_port = p.name
            elif lname in busy_names:
                busy_port = p.name
                has_busy_port = True
            elif p.name in ctrl_out_set:
                control_output_ports.append({
                    'name': p.name, 'width': p.width, 'signed': p.signed,
                })
            else:
                data_output_ports.append({
                    'name': p.name, 'width': p.width,
                    'array_count': p.array_count, 'signed': p.signed,
                })

    user_has_handshake = start_port is not None and done_port is not None

    # ── Interface-contract validation ───────────────────────────────
    # The wrapper drives a transaction: load inputs -> pulse start -> wait
    # for done -> dump outputs -> stream them out.  Without BOTH handshake
    # ports that sequence cannot run, and the generated (but "successful")
    # wrapper silently never transmits anything.  Reject it up front.
    if not user_has_handshake and (data_input_ports or data_output_ports):
        missing = []
        if start_port is None:
            missing.append("start")
        if done_port is None:
            missing.append("done")
        return {
            'success': False,
            'error': (
                f"Module '{top_module}' has data ports but no "
                f"{'/'.join(missing)} handshake port.  SeriWrap wraps a "
                f"transactional kernel: it needs an input 'start' and an "
                f"output 'done' (auto-detected by name, or set with "
                f"--start-ports/--done-ports).  Generating without them "
                f"would produce a wrapper that never emits data."
            ),
            'message': 'User module must expose start/done handshake',
        }

    # 2-D / unpacked array data ports cannot be wired as a flat vector.
    for p in ports:
        if p.array_count > 1 and p.direction in ('input', 'output'):
            return {
                'success': False,
                'error': (
                    f"Port '{p.name}' is an unpacked array "
                    f"({p.array_count} elements).  SeriWrap marshals each "
                    f"port as one packed vector, so array ports cannot be "
                    f"wrapped.  Flatten the port in the kernel (or wrap a "
                    f"wrapper module that packs it) and retry."
                ),
                'message': f"Array port '{p.name}' is not supported",
            }

    # ── Resolve reset polarity ─────────────────────────────────────
    _valid_polarities = ('auto', 'active_high', 'active_low')
    if reset_polarity not in _valid_polarities:
        return {'success': False, 'error': f"Invalid --reset-polarity '{reset_polarity}'. Valid: {_valid_polarities}"}
    if reset_polarity == 'auto':
        # Auto-detect from port name: trailing '_n' → active-low, else active-high
        if reset_port is None:
            reset_polarity = 'active_low'   # no reset port → irrelevant, pick safe default
        elif reset_port.lower().endswith('_n'):
            reset_polarity = 'active_low'
        else:
            reset_polarity = 'active_high'

    # Adapter mode override: force input_count = 0 (no Rabbit-fed BRAM
    # entries), so the existing `input_count == 0` path in the wrapper
    # FSM is used (pass-through-like: no C_LAUNCH, start pulses directly).
    if input_source_norm == 'adapter':
        if not user_has_handshake:
            return {
                'success': False,
                'error': "--input-source adapter requires the user module "
                         "to have both 'start' (input) and 'done' (output) "
                         "handshake ports (auto-detected by name).  Bridge "
                         "mode drives user.start/done via the C_COMPUTING FSM "
                         "state, which is only entered via handshake.",
                'message': 'Adapter mode requires user handshake',
            }
        data_input_ports = []   # ignore any data_input_ports — bridge supplies

    # Ping-pong template has no adapter support at all: reject instead of
    # silently dropping the binding and emitting undeclared nets.
    if pingpong and adapters:
        return {
            'success': False,
            'error': ("--pingpong is incompatible with --adapter: the "
                      "double-buffered wrapper has no device-adapter path. "
                      "Drop one of the two flags."),
            'message': 'ping-pong does not support adapters',
        }

    # Device adapters are only implemented in the standard (BRAM) wrapper;
    # the register-file wrapper has no adapter ports/instances, which used to
    # make the top-level fail with PINNOTFOUND.  Force the BRAM path.
    force_bram_wrapper = bool(adapters)

    # ── Auto-select BRAM config if width=0 ─────────────────────────
    if bram_width <= 0:
        bram_width, bram_depth = _auto_bram_config(
            data_input_ports, data_output_ports, bram_type,
            use_binpack=use_binpack,
        )
        # Recompute addr_w for the new depth and validate result
        addr_w = _addr_w(bram_depth)
        err = _validate_bram(bram_width, bram_depth, bram_type)
        if err:
            return {'success': False, 'error': err, 'message': err}

    # ── --no-piso / --no-sipo ─────────────────────────────────────────
    # Historically these flags were parsed and then ignored: the SIPO and the
    # PISO were always generated and always instantiated.  They are now
    # honoured, which also removes the corresponding side from the FSM.
    tied_input_ports: List[Dict[str, Any]] = []
    if not include_piso and not include_sipo:
        return {
            'success': False,
            'error': "--no-sipo and --no-piso cannot be combined: that "
                     "leaves no data path at all.",
            'message': 'Nothing left to generate',
        }
    if not include_sipo:
        if input_source_norm == 'adapter':
            return {'success': False,
                    'error': "--no-sipo cannot be combined with --input-source adapter",
                    'message': 'Conflicting options'}
        # Keep the ports (they are tied low) but drop the serialized path.
        tied_input_ports = data_input_ports
        data_input_ports = []
    if not include_piso:
        data_output_ports = []

    # ── Compute heterogeneous chunks ──────────────────────────────────
    chunk_fn = _compute_chunks_binpack if use_binpack else _compute_chunks
    input_chunked, input_count = chunk_fn(data_input_ports, bram_width)
    output_chunked, output_count = chunk_fn(data_output_ports, bram_width)

    # Flatten chunk lists for template iteration (needed by bin-packing
    # where multiple ports may share a BRAM entry).
    input_chunks_flat = []
    for p in input_chunked:
        for c in p['chunks']:
            c_flat = dict(c)
            c_flat['port_name'] = p['name']
            input_chunks_flat.append(c_flat)
    input_chunks_flat.sort(key=lambda c: c['bram_idx'])

    output_chunks_flat = []
    for p in output_chunked:
        for c in p['chunks']:
            c_flat = dict(c)
            c_flat['port_name'] = p['name']
            c_flat['port_width'] = p['width']
            output_chunks_flat.append(c_flat)
    output_chunks_flat.sort(key=lambda c: c['bram_idx'])

    # ── Build binpack output concatenation expressions ─────────────────
    # Avoids Verilog multi-driver conflicts (zero-init + partial assign).
    # Each BRAM entry gets a single concatenation expression combining all
    # pieces from potentially multiple ports, with zero-fill for unused bits.
    output_concat_exprs = []  # list of Verilog concat strings, one per out_buf entry
    if use_binpack and output_chunks_flat and output_chunks_flat[0].get('port_hi') is not None:
        # Group chunks by bram_idx
        groups: Dict[int, List[Dict]] = {}
        for c in output_chunks_flat:
            groups.setdefault(c['bram_idx'], []).append(c)
        for bi in range(output_count):
            pieces = groups.get(bi, [])
            pieces.sort(key=lambda p: p['bram_hi'], reverse=True)
            parts = []
            cursor = bram_width - 1
            for p in pieces:
                if p['bram_hi'] < cursor:
                    parts.append(f"{cursor - p['bram_hi']}'d0")
                pw = p['port_hi'] - p['port_lo'] + 1
                parts.append(f"user_out_w_{p['port_name']}[{p['port_hi']}:{p['port_lo']}]")
                cursor = p['bram_lo'] - 1
            if parts:
                if cursor >= 0:
                    parts.append(f"{cursor + 1}'d0")
                expr = "{ " + ", ".join(parts) + " }"
            else:
                expr = f"{{WIDTH{{1'b0}}}}"
            output_concat_exprs.append(expr)
    else:
        output_concat_exprs = []

    # ── Adaptive storage selection ────────────────────────────────────
    # The register-file path is one template that replaces BOTH directions,
    # so the decision is all-or-nothing (the old per-side flags let
    # pipeline_depth disagree with the template that was actually emitted).
    # Device adapters exist only in the BRAM wrapper.
    use_regfile = (
        not pingpong                  # ping-pong always instantiates BRAMs
        and not force_bram_wrapper
        and input_count > 0 and output_count > 0
        and _should_use_reg_file(input_count, bram_width)
        and _should_use_reg_file(output_count, bram_width)
    )
    use_reg_file_in = use_regfile
    use_reg_file_out = use_regfile
    pipeline_depth = _get_pipeline_depth(bram_type) if not use_regfile else 0
    # For register-file mode, SIPO/PISO address width is based on chunk count
    if use_regfile:
        regfile_max = max(input_count, output_count)
        addr_w = max(addr_w, (_addr_w(regfile_max) if regfile_max > 0 else addr_w))

    # ── Asymmetric baud rates ────────────────────────────────────────
    # When neither is specified, auto-balance based on I/O ratio so the
    # serial transfer time of each side is proportional to its data volume.
    if baud_div_in is None and baud_div_out is None:
        if input_count > 0 and output_count > 0:
            ratio = input_count / output_count
            if ratio >= 4.0:
                # Input-heavy: keep input at default rate, relax output
                baud_div_in  = baud_div
                baud_div_out = min(baud_div * max(int(ratio / 2), 1), 256)
            elif ratio <= 0.25:
                # Output-heavy: keep output at default rate, relax input
                baud_div_in  = min(baud_div * max(int(1.0 / ratio / 2), 1), 256)
                baud_div_out = baud_div
            else:
                baud_div_in = baud_div_out = baud_div
        else:
            baud_div_in = baud_div_out = baud_div
    else:
        if baud_div_in is None:
            baud_div_in = baud_div
        if baud_div_out is None:
            baud_div_out = baud_div

    # ── Multi-BRAM expansion ──────────────────────────────────────────
    # When the number of marshaled chunks exceeds a single BRAM's depth,
    # split across multiple BRAM instances.
    num_in_brams  = max(1, (input_count  + bram_depth - 1) // bram_depth) if input_count  > 0 else 0
    num_out_brams = max(1, (output_count + bram_depth - 1) // bram_depth) if output_count > 0 else 0
    max_brams = max(num_in_brams, num_out_brams, 1)
    bram_sel_w = max(1, (max_brams - 1).bit_length()) if max_brams > 1 else 0
    ext_addr_w = addr_w + bram_sel_w   # address spanning all BRAMs

    # ── Ping-pong depth check ─────────────────────────────────────────
    # The ping-pong template instantiates exactly two BRAMs per direction
    # (the two banks) and has no multi-BRAM partitioning, so it cannot hold
    # more marshaled entries than one BRAM is deep: the SIPO's address wraps
    # and silently overwrites the first entries.  Verified by simulation: 13
    # entries in an 8-deep bank corrupt 10 of 13 output words while the
    # generator still reports success.  Reject instead of corrupting data.
    if pingpong:
        need = max(input_count, output_count)
        if need > bram_depth:
            return {
                'success': False,
                'error': (f"--pingpong needs one BRAM per bank, so the marshaled "
                          f"entry count ({need}) must fit --bram-depth "
                          f"({bram_depth}).  Raise --bram-depth to at least "
                          f"{need}, or drop --pingpong (the standard wrapper "
                          f"partitions across multiple BRAMs automatically)."),
                'message': 'ping-pong requires count <= bram-depth',
            }

    # ── Index widths ──────────────────────────────────────────────────
    def _idx_w(n: int, min_w: int = 1) -> int:
        if n <= 1:
            return max(1, min_w)
        return max(min_w, int(math.ceil(math.log2(n + 1))))
    # The FSM counters only ever step over the marshaled entries, so size them
    # for INPUT_COUNT/OUTPUT_COUNT rather than for the full (possibly
    # multi-BRAM) address space.  The old ext_addr_w minimum wasted FFs on
    # every wrapper (Verilator flagged the over-wide array indices).
    input_count_idx_w  = _idx_w(input_count)  if user_has_handshake else 1
    output_count_idx_w = _idx_w(output_count) if user_has_handshake else 1

    # ── Generate BRAM IP (skip if using register-file for both sides) ─
    bram_name = f"{top_module}__stream_bram"
    bram_result = None
    # Ping-pong instantiates 4 BRAMs regardless of size, so it always needs
    # the BRAM IP (the old condition skipped it for small kernels and the
    # generated design failed with "module not found").
    need_bram = pingpong or not use_regfile
    if need_bram:
        bram_result = bram_generator.generate_bram_ip(
            module_name=bram_name,
            width_A=bram_width, depth_A=bram_depth,
            width_B=bram_width, depth_B=bram_depth,
            raw_data_array=[0] * bram_depth,
            bram_type=bram_type,
        )
    if bram_result and not bram_result.get('success'):
        return {
            'success': False,
            'error': f"BRAM generation failed: {bram_result.get('error')}",
            'message': 'BRAM generation failed',
        }

    # ── Template contexts ─────────────────────────────────────────────
    gen_date = datetime.now().strftime('%Y.%m.%d')

    # Shared context for both normal and pingpong wrappers
    shared_ctx = {
        'top_module': top_module,
        'bram_module': bram_name,
        'user_source': os.path.basename(source_file),
        'width': bram_width,
        'WIDTH': bram_width,
        'depth': bram_depth,
        'addr_w': addr_w,
        'ext_addr_w': ext_addr_w,
        'num_in_brams': num_in_brams,
        'num_out_brams': num_out_brams,
        'bram_sel_w': bram_sel_w,
        'has_multi_bram': max_brams > 1,
        'baud_div': baud_div,
        'baud_div_in': baud_div_in,
        'baud_div_out': baud_div_out,
        'user_has_handshake': user_has_handshake,
        'control_input_ports': control_input_ports,
        'control_output_ports': control_output_ports,
        'has_control_ports': bool(control_input_ports or control_output_ports),
        'data_input_ports': data_input_ports,
        'data_output_ports': data_output_ports,
        'tied_input_ports': tied_input_ports,
        'has_tied_inputs': bool(tied_input_ports),
        'include_sipo': include_sipo,
        'include_piso': include_piso,
        'input_chunked': input_chunked,
        'output_chunked': output_chunked,
        'input_chunks_flat': input_chunks_flat,
        'output_chunks_flat': output_chunks_flat,
        'output_concat_exprs': output_concat_exprs,
        # One FULL-WIDTH assignment per output word, built here because the
        # concatenation needs the field widths and their positions.  Assigning
        # only the covered bit ranges (the old bin-packing path) left the spare
        # bits of a partially filled word at X, and the wrapper drove them onto
        # the serial output; a full-width assignment pads them with zeros.
        'out_word_assigns': _output_word_assigns(output_chunks_flat, bram_width),
        'has_output_concat': len(output_concat_exprs) > 0,
        'start_port': start_port or 'start',
        'done_port': done_port or 'done',
        'busy_port': busy_port or 'busy',
        'clock_port': clock_port,
        'reset_port': reset_port,
        'has_clock_port': clock_port is not None,
        'has_reset_port': reset_port is not None,
        'reset_polarity': reset_polarity,
        'has_busy_port': has_busy_port,
        'input_count': input_count,
        'output_count': output_count,
        'input_count_idx_w': input_count_idx_w,
        'output_count_idx_w': output_count_idx_w,
        'generation_date': gen_date,
        'debug': debug,
        'input_source': input_source_norm,
        'sync_mode': sync_mode,
        'trigger_key': trigger_key_norm,
        'trigger_bit': TRIGGER_BIT.get(trigger_key_norm, 40) if input_source_norm == 'adapter' else 0,
        'numeric_width': numeric_width,
        # Optimization parameters
        'pipeline_depth': pipeline_depth,
        'use_reg_file_in': use_reg_file_in,
        'use_reg_file_out': use_reg_file_out,
        'use_binpack': use_binpack,
        'expose_ready': bool(ready_signal),
    }

    wrapper_ctx = dict(shared_ctx)
    wrapper_ctx['wrapper_module'] = f"{top_module}__stream_wrapper"
    wrapper_ctx['adapters'] = adapters
    wrapper_ctx['adapter_input_ports'] = adapter_input_ports
    # Flat list so the template's comma logic is correct with >1 adapter.
    wrapper_ctx['adapter_inputs_flat'] = [
        {'name': p['name'], 'wire': f"ad_{ad_name}_{p['name']}"}
        for ad_name, plist in adapter_input_ports.items() for p in plist
    ]

    top_ctx: Dict[str, Any] = {
        'top_module': top_module,
        'stream_module': f"{top_module}__stream_wrapper",
        'generation_date': gen_date,
        'width':  bram_width,
        'WIDTH':  bram_width,
        'depth':  bram_depth,
        'debug': debug,
        'input_count': input_count,
        'output_count': output_count,
        'control_input_ports': control_input_ports,
        'control_output_ports': control_output_ports,
        'has_control_ports': bool(control_input_ports or control_output_ports),
        'adapters': adapters,
        'expose_ready': bool(ready_signal),
    }

    src_ctx = {
        'top_module': top_module,
        'generation_date': gen_date,
        'width':  bram_width,
        'count':  input_count,
        'addr_w': ext_addr_w,
        'baud_div': baud_div_in,
        'fifo_addr_w': 3,
    }
    piso_ctx = {
        'top_module': top_module,
        'generation_date': gen_date,
        'width':    bram_width,
        'count':    output_count,
        'baud_div': baud_div_out,
        'addr_w':   ext_addr_w,
    }

    # Bridge template context: only used when input_source='adapter'.
    # USE_TRIGGER_BIT selects PS/2 mode (key_state[TRIGGER_BIT] rising edge)
    # vs. generic event mode (1-bit `event_in` pulse from UART/GPIO).
    has_ps2 = any(ad['name'] == 'ps2' for ad in adapters)
    bridge_ctx = {
        'top_module':      top_module,
        'generation_date': gen_date,
        # The wrapper declares src_addr as [EXT_ADDR_W-1:0]; keep them equal.
        'addr_w':          ext_addr_w,
        'width':           bram_width,
        'trigger_bit':     TRIGGER_BIT[trigger_key_norm],
        'trigger_key':     trigger_key_norm,
        'use_trigger_bit': 1 if has_ps2 else 0,
    }

    # ── Write files ───────────────────────────────────────────────────
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        return {'success': False, 'error': str(e), 'message': f'Cannot create {out_dir}'}

    template_dir = os.path.dirname(os.path.abspath(__file__))

    bram_path = os.path.join(out_dir, f"{bram_name}.v")
    src_path = os.path.join(out_dir, f"{top_module}__stream_sipo.v")
    bridge_path = os.path.join(out_dir, f"{top_module}__stream_bridge.v")
    piso_path = os.path.join(out_dir, f"{top_module}__stream_piso.v")

    use_regfile = (use_reg_file_in and use_reg_file_out)  # only when both sides qualify
    if pingpong:
        wrapper_path = os.path.join(out_dir, f"{top_module}__stream_wrapper.v")
        top_path     = os.path.join(out_dir, f"{top_module}__stream_top.v")
        wrapper_tmpl = 'stream_wrapper_pingpong.j2'
        top_tmpl     = 'stream_top_pingpong.j2'
    elif use_regfile:
        wrapper_path = os.path.join(out_dir, f"{top_module}__stream_wrapper.v")
        top_path     = os.path.join(out_dir, f"{top_module}__stream_top.v")
        wrapper_tmpl = 'stream_wrapper_regfile.j2'
        top_tmpl     = 'stream_top.j2'
    else:
        wrapper_path = os.path.join(out_dir, f"{top_module}__stream_wrapper.v")
        top_path     = os.path.join(out_dir, f"{top_module}__stream_top.v")
        wrapper_tmpl = 'stream_wrapper.j2'
        top_tmpl     = 'stream_top.j2'

    # Copy required adapter Verilog files into out_dir
    ADAPTERS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'adapters')
    for ad in adapters:
        ad_name = ad['name']
        adapter_src_path = os.path.join(ADAPTERS_DIR, f"device_adapter_{ad_name}.v")
        if os.path.isfile(adapter_src_path):
            adapter_dst_path = os.path.join(out_dir, f"device_adapter_{ad_name}.v")
            with open(adapter_src_path, 'r', encoding='utf-8') as fsrc, \
                 open(adapter_dst_path, 'w', encoding='utf-8') as fdst:
                fdst.write(fsrc.read())

    # Copy the async FIFO primitive (used by stream_sipo.v) into out_dir.
    # This is a hand-written Verilog module shared by all SIPO instances.
    ASYNC_FIFO_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  'templates', 'stream_async_fifo.v')
    if os.path.isfile(ASYNC_FIFO_SRC):
        async_fifo_dst = os.path.join(out_dir, 'stream_async_fifo.v')
        with open(ASYNC_FIFO_SRC, 'r', encoding='utf-8') as fsrc, \
             open(async_fifo_dst, 'w', encoding='utf-8') as fdst:
            fdst.write(fsrc.read())

    files_written = [wrapper_path, top_path]
    if include_piso:
        files_written.append(piso_path)
        files_written.append(os.path.join(out_dir, 'stream_async_fifo.v'))
    if bram_result is not None:
        files_written.insert(0, bram_path)
    if input_source_norm == 'adapter':
        files_written.append(bridge_path)
    elif include_sipo:
        files_written.append(src_path)
        if os.path.join(out_dir, 'stream_async_fifo.v') not in files_written:
            files_written.append(os.path.join(out_dir, 'stream_async_fifo.v'))
    for ad in adapters:
        files_written.append(os.path.join(out_dir, f"device_adapter_{ad['name']}.v"))

    try:
        if bram_result is not None:
            with open(bram_path, 'w', encoding='utf-8') as f:
                f.write(_ensure_trailing_newline(bram_result['verilog_code']))
        if input_source_norm == 'adapter':
            with open(bridge_path, 'w', encoding='utf-8') as f:
                f.write(_ensure_trailing_newline(_render(template_dir, 'stream_bridge.j2', bridge_ctx)))
        elif include_sipo:
            with open(src_path, 'w', encoding='utf-8') as f:
                f.write(_ensure_trailing_newline(_render(template_dir,
            'stream_sipo_sync.j2' if sync_mode else 'stream_sipo.j2', src_ctx)))
        if include_piso:
            with open(piso_path, 'w', encoding='utf-8') as f:
                f.write(_ensure_trailing_newline(_render(template_dir,
            'stream_piso_sync.j2' if sync_mode else 'stream_piso.j2', piso_ctx)))
        with open(wrapper_path, 'w', encoding='utf-8') as f:
            f.write(_ensure_trailing_newline(_render(template_dir, wrapper_tmpl, wrapper_ctx)))
        with open(top_path, 'w', encoding='utf-8') as f:
            f.write(_ensure_trailing_newline(_render(template_dir, top_tmpl, top_ctx)))

        # ── Generate data-layout mapping doc ─────────────────────────
        _write_mapping_doc(out_dir, top_module,
                           input_chunks_flat, output_chunks_flat,
                           bram_width, bram_depth,
                           num_in_brams, num_out_brams,
                           input_count, output_count)
        # ── Generate the machine-readable manifest (same information, for
        #    programs: host GUIs, testbenches, bring-up scripts) ──────────
        _write_manifest(out_dir, top_module,
                        input_chunks_flat=input_chunks_flat,
                        output_chunks_flat=output_chunks_flat,
                        bram_width=bram_width, bram_depth=bram_depth,
                        num_in_brams=num_in_brams,
                        num_out_brams=num_out_brams,
                        input_count=input_count, output_count=output_count,
                        storage_mode=('pingpong' if pingpong else
                                      ('register_file' if use_regfile else 'bram')),
                        bram_type=bram_type,
                        sync_mode=sync_mode,
                        input_source=input_source_norm,
                        baud_div=baud_div,
                        baud_div_in=baud_div_in,
                        baud_div_out=baud_div_out,
                        data_input_ports=data_input_ports,
                        data_output_ports=data_output_ports,
                        clock_port=clock_port, reset_port=reset_port,
                        reset_polarity=reset_polarity,
                        start_port=start_port, done_port=done_port,
                        ready_signal=ready_signal,
                        adapters=adapters)
        files_written.append(
            os.path.join(out_dir, f'{top_module}__stream_manifest.json'))
    except OSError as e:
        return {'success': False, 'error': str(e), 'message': f'Cannot write to {out_dir}'}

    mode_str = "ping-pong" if pingpong else "standard"
    mode_str += f" [input_source={input_source_norm}"
    if input_source_norm == 'adapter':
        mode_str += f", trigger_key={trigger_key_norm}, numeric_width={numeric_width}"
    mode_str += "]"
    result_dict = {
        'success': True,
        'wrapper_file': wrapper_path,
        'top_file': top_path,
        'bram_file': bram_path if bram_result is not None else None,
        'src_file': src_path if include_sipo else None,
        'piso_file': piso_path if include_piso else None,
        'files': files_written,
        'message': (
            f"Stream wrapper ({mode_str}) generated for {top_module} "
            f"({bram_width}x{bram_depth} BRAM, input_count={input_count}, output_count={output_count})"
        ),
    }
    if input_source_norm == 'adapter':
        result_dict['bridge_file'] = bridge_path
        result_dict['src_file'] = None
    return result_dict


# =============================================================================
#  CLI entry
# =============================================================================

def main(args_list: Optional[List[str]] = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        prog='seriwrap stream',
        description='Wrap a user module with SIPO/PISO + BRAM to expose 3-wire serial I/O',
    )
    parser.add_argument('--source', '-s', required=True,
                        help='Path to user Verilog source file')
    parser.add_argument('--top', '-t', default=None,
                        help='Top module name (required unless --print-modules)')
    parser.add_argument('--bram-width', type=int, default=None)
    parser.add_argument('--bram-depth', type=int, default=None)
    parser.add_argument('--out-dir', default='.')
    parser.add_argument('--baud-div', type=int, default=2)
    parser.add_argument('--no-sipo', action='store_true')
    parser.add_argument('--no-piso', action='store_true')
    parser.add_argument('--print-modules', action='store_true')
    parser.add_argument('--debug', action='store_true',
                        help='Include overflow/idle/busy debug status ports')
    parser.add_argument('--control-inputs', default=None,
                        help='Comma-separated control input port names to expose on wrapper')
    parser.add_argument('--control-outputs', default=None,
                        help='Comma-separated control output port names to expose on wrapper')
    parser.add_argument('--pingpong', action='store_true',
                        help='Generate double-buffered ping-pong wrapper with 4 BRAMs')
    parser.add_argument('--bram-type', type=str, default='ram4s',
                        choices=['ram4s', 'ram8b', 'ramb18e1', 'ramb36e1', 'generic'],
                        help='BRAM primitive family (default: ram4s)')
    parser.add_argument('--adapter', action='append', default=None,
                        help='Physical device adapter binding. Format: '
                             'TYPE:PORT[,PORT...] e.g. '
                             '--adapter ps2:key_state[63:0],mod_state[7:0]. '
                             'May be repeated for multiple adapters.')
    parser.add_argument('--input-source', choices=list(VALID_INPUT_SOURCES),
                        default='rabbit',
                        help='Stream input source: "rabbit" (default 3-wire '
                             'serial from host) or "adapter" (PS/2 keyboard '
                             'via the bridge module; requires --adapter ps2).')
    parser.add_argument('--trigger-key', choices=list(TRIGGER_BIT.keys()),
                        default='enter',
                        help='Which PS/2 key presses the bridge into '
                             'computing (only used with --input-source '
                             'adapter). Default: enter.')
    parser.add_argument('--numeric-width', type=int, default=32,
                        choices=[8, 16, 32, 64],
                        help='Width of the numeric accumulator (only used '
                             'with --input-source adapter for BRAM-depth '
                             'validation). Default: 32.')
    parser.add_argument('--reset-polarity', type=str, default='auto',
                        choices=['auto', 'active_high', 'active_low'],
                        help="User module reset port polarity. 'auto' (default) "
                             "detects from the port name (trailing '_n' → "
                             "active-low, otherwise active-high). Use "
                             "'active_high' for ap_rst, 'rst', etc. or "
                             "'active_low' for rst_n, reset_n, etc.")
    args = parser.parse_args(args_list)

    if args.print_modules:
        result = parse_modules(args.source)
    else:
        missing = [k for k, v in {
            'top': args.top, 'bram-width': args.bram_width,
            'bram-depth': args.bram_depth,
        }.items() if v is None]
        if missing:
            result = {
                'success': False,
                'error': f"Missing required arguments: {', '.join(missing)}",
                'message': 'Missing required arguments',
            }
        else:
            ctrl_in = [x.strip() for x in args.control_inputs.split(',') if x.strip()] \
                      if args.control_inputs else None
            ctrl_out = [x.strip() for x in args.control_outputs.split(',') if x.strip()] \
                       if args.control_outputs else None
            # Parse --adapter flags: TYPE:port1[bits],port2[bits],...
            adapters_list = []
            if args.adapter:
                for spec in args.adapter:
                    if ':' not in spec:
                        print(json.dumps({
                            'success': False,
                            'error': f"Bad --adapter spec '{spec}'. Expected "
                                     f"TYPE:PORT[,PORT...], e.g. ps2:key_state",
                            'message': 'Bad adapter spec',
                        }))
                        return 1
                    atype, ports_str = spec.split(':', 1)
                    port_names = [p.strip().split('[')[0] for p in ports_str.split(',') if p.strip()]
                    adapters_list.append({'name': atype.strip(), 'ports': port_names})
            result = generate_stream_ip(
                source_file=args.source,
                top_module=args.top,
                bram_width=args.bram_width,
                bram_depth=args.bram_depth,
                out_dir=args.out_dir,
                baud_div=args.baud_div,
                include_sipo=not args.no_sipo,
                include_piso=not args.no_piso,
                debug=args.debug,
                control_inputs=ctrl_in,
                control_outputs=ctrl_out,
                reset_polarity=args.reset_polarity,
                pingpong=args.pingpong,
                bram_type=args.bram_type,
                adapters=adapters_list,
                input_source=args.input_source,
                trigger_key=args.trigger_key,
                numeric_width=args.numeric_width,
            )

    if result.get('success') and args.bram_width == 0 and args.bram_depth is not None:
        msg = ("--bram-depth is ignored when --bram-width 0 (auto mode picks both "
               "width and depth); pass an explicit --bram-width to control depth.")
        result.setdefault('warnings', []).append(msg)
        print("WARNING: " + msg, file=sys.stderr)
    print(json.dumps(result, indent=2))
    return 0 if result.get('success') else 1


if __name__ == '__main__':
    import sys
    sys.exit(main())
