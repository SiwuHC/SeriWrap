#!/usr/bin/env python3
"""
stream_generator.py — Stream BRAM IP generator.

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
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field

from jinja2 import Environment, FileSystemLoader

import bram_generator


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


def _parse_width_from_range(hi: str, lo: str) -> Tuple[Optional[int], bool]:
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
        # Parameter-driven. Caller must use CLI override.
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


def _parse_ports(port_list: str) -> List[Port]:
    """Parse a comma-separated ANSI port list into Port records.

    A single ANSI chunk can declare multiple port names sharing the same
    direction and range, e.g. `input wire [15:0] a0, a1, a2, a3,`.  We
    find the leading `dir + [range]` and then split the remainder on
    commas to enumerate all names.

    For 2D array ports like `input wire [15:0] A [0:3][0:3]`, the
    total port bit-width is `bit_width × dim0 × dim1`.  The `width`
    field in Port stores the *bit* width (per element × total elements).
    """
    ports: List[Port] = []
    # First split on top-level commas to identify "header" chunks (those
    # starting with a direction keyword) vs "tail" chunks (just a name
    # continuing the previous header).
    chunks = _split_top_level_commas(port_list)
    current_dir: Optional[str] = None
    current_width: int = 1
    current_signed: bool = False
    current_used_param: bool = False
    current_array_count: int = 1
    for chunk in chunks:
        m = _PORT_DECL.search(chunk)
        if m:
            # New header chunk: dir + optional range + optional array dims
            current_dir = m.group('dir')
            hi, lo = m.group('hi'), m.group('lo')
            bit_width, bit_used_param = (
                _parse_width_from_range(hi, lo) if hi and lo else (1, False)
            )
            array_total, array_used_param = _parse_array_dims(m.group('array_dims') or '')
            # Total port bit width = bit_width per element * array elements
            current_width = bit_width * array_total
            current_used_param = bit_used_param or array_used_param
            current_array_count = array_total
            current_signed = 'signed' in chunk
            name = m.group('name')
        else:
            # Tail chunk: just a port name (continuation of previous header)
            if current_dir is None:
                continue
            m2 = re.search(r'\b(\w+)\s*$', chunk.strip())
            if not m2:
                continue
            name = m2.group(1)
        ports.append(Port(
            name=name,
            direction=current_dir,
            width=current_width,
            signed=current_signed,
            used_parameter=current_used_param,
            array_count=current_array_count,
        ))
    return ports


def _parse_params(param_list: str) -> List[Tuple[str, str]]:
    """Parse a parameter list like 'WIDTH=16, DEPTH=256' into (name, default) pairs."""
    out: List[Tuple[str, str]] = []
    for chunk in _split_top_level_commas(param_list):
        m = re.match(r'\s*(?:parameter\s+)?(\w+)\s*=\s*(.+)$', chunk.strip(), re.DOTALL)
        if m:
            out.append((m.group(1), m.group(2).strip()))
    return out


def parse_modules(path: str) -> Dict[str, Any]:
    """Parse a Verilog file and return a dict of module name → ModuleInfo."""

    try:
        with open(path, 'r', encoding='utf-8') as f:
            src = f.read()
    except OSError as e:
        return {'success': False, 'error': str(e), 'message': f'Cannot read {path}', 'modules': []}

    stripped = _strip_comments(src)
    modules: List[ModuleInfo] = []
    for m in _MOD_HDR.finditer(stripped):
        mod = ModuleInfo(name=m.group(1))
        params_raw = m.group('params') or ''
        mod.parameters = _parse_params(params_raw)
        mod.ports = _parse_ports(m.group('ports'))
        modules.append(mod)

    return {
        'success': True,
        'modules': [mod.to_dict() for mod in modules],
        'message': f'Parsed {len(modules)} module(s) from {os.path.basename(path)}',
    }


# =============================================================================
#  Port-name parsing (CLI side: "name" or "name:width")
# =============================================================================

def _parse_port_spec(spec: str) -> Tuple[str, Optional[int]]:
    """Parse a 'name' or 'name:width' CLI argument."""
    if ':' in spec:
        name, width_s = spec.split(':', 1)

        try:
            return name.strip(), int(width_s)
        except ValueError:
            raise ValueError(f"Invalid port spec '{spec}': width must be an integer")
    return spec.strip(), None


# =============================================================================
#  Generator
# =============================================================================

def _validate_bram(width: int, depth: int) -> Optional[str]:
    """Return an error message if (width, depth) is not supported, else None."""
    if (width, depth) not in bram_generator._VALID_COMBINATIONS:
        valid = sorted(bram_generator._VALID_COMBINATIONS)
        return (
            f"BRAM ({width} x {depth}) is not a supported combination. "
            f"See BRAM IP docs for the list of valid (width, depth) pairs."
        )
    return None


def _addr_w(depth: int) -> int:
    return max(1, int(math.ceil(math.log2(depth))))


def _render(template_dir: str, name: str, ctx: Dict[str, Any]) -> str:
    env = Environment(
        loader=FileSystemLoader(template_dir),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return env.get_template(f'templates/{name}').render(**ctx)


def _ensure_trailing_newline(s: str) -> str:
    return s if s.endswith('\n') else s + '\n'


def _port_connection(port: Port, width: int) -> str:
    """Return the Verilog expression to connect an unused port."""
    if port.width > 1:
        return f"{{{port.width}{{1'b0}}}}"
    return "1'b0"


def generate_stream_ip(
    source_file: str,
    top_module: str,
    input_port: str,
    output_port: str,
    bram_width: int,
    bram_depth: int,
    out_dir: str = '.',
    baud_div: int = 2,
    idle_timeout: int = 2000,
    include_sipo: bool = True,
    include_piso: bool = True,
) -> Dict[str, Any]:
    """Generate the three .v files for the stream wrapper.

    Returns the standard {success, message, ...} dict. On success, also
    includes 'wrapper_file', 'top_file', 'bram_file', and 'files'.
    """

    try:
        in_name, in_width_override = _parse_port_spec(input_port)
        out_name, out_width_override = _parse_port_spec(output_port)
        if not include_sipo and in_width_override is None:
            return {
                'success': False,
                'error': 'When --no-sipo is set, --input-port must include ":width"',
                'message': 'Cannot determine input port width without SIPO source',
            }
        if not include_piso and out_width_override is None:
            return {
                'success': False,
                'error': 'When --no-piso is set, --output-port must include ":width"',
                'message': 'Cannot determine output port width without PISO source',
            }
    except ValueError as e:
        return {'success': False, 'error': str(e), 'message': str(e)}

    # Parse the user's Verilog
    parsed = parse_modules(source_file)
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

    # Resolve port widths (CLI override wins, then parsed, then default 1)
    in_port = next((p for p in ports if p.name == in_name), None)
    out_port = next((p for p in ports if p.name == out_name), None)
    if in_port is None:
        return {
            'success': False,
            'error': f"Input port '{in_name}' not found in module '{top_module}'",
            'message': f"Input port '{in_name}' not found",
        }
    if out_port is None:
        return {
            'success': False,
            'error': f"Output port '{out_name}' not found in module '{top_module}'",
            'message': f"Output port '{out_name}' not found",
        }

    in_port.width = in_width_override if in_width_override is not None else in_port.width
    out_port.width = out_width_override if out_width_override is not None else out_port.width

    # Width consistency: both data ports must match BRAM width
    if include_sipo and include_piso:
        if in_port.width != out_port.width:
            return {
                'success': False,
                'error': f"Input port width ({in_port.width}) != output port width ({out_port.width}); both must match BRAM width",
                'message': 'Input/output port width mismatch',
            }
        if in_port.width != bram_width:
            return {
                'success': False,
                'error': f"Port width ({in_port.width}) != BRAM width ({bram_width})",
                'message': 'Port/BRAM width mismatch',
            }
    elif include_sipo:
        if in_port.width != bram_width:
            return {
                'success': False,
                'error': f"Input port width ({in_port.width}) != BRAM width ({bram_width})",
                'message': 'Input port/BRAM width mismatch',
            }
    elif include_piso:
        if out_port.width != bram_width:
            return {
                'success': False,
                'error': f"Output port width ({out_port.width}) != BRAM width ({bram_width})",
                'message': 'Output port/BRAM width mismatch',
            }

    # BRAM validation
    err = _validate_bram(bram_width, bram_depth)
    if err:
        return {'success': False, 'error': err, 'message': err}

    # Generate the BRAM IP via the existing bram_generator API
    bram_name = f"{top_module}__stream_bram"
    bram_result = bram_generator.generate_bram_ip(
        module_name=bram_name,
        width_A=bram_width, depth_A=bram_depth,
        width_B=bram_width, depth_B=bram_depth,
        raw_data_array=[0] * bram_depth,
    )
    if not bram_result.get('success'):
        return {
            'success': False,
            'error': f"BRAM generation failed: {bram_result.get('error')}",
            'message': 'BRAM generation failed',
        }

    # Build the wrapper template context.
    # For each user port, classify it:
    #   - clk / rst_n → top-level clock/reset
    #   - start / done / busy (when present) → handshake
    #   - all other inputs → tie to 0
    #   - all other outputs → leave dangling
    addr_w = _addr_w(bram_depth)
    clock_names = {'clk', 'clock'}
    reset_names = {'rst_n', 'rst', 'reset_n', 'reset'}
    start_names = {'start', 'go', 'begin', 'trigger'}
    done_names  = {'done', 'valid', 'finish', 'complete'}
    busy_names  = {'busy', 'ready'}

    user_input_ports = []   # all input ports that the wrapper must wire
    user_output_ports = []  # all output ports that the wrapper must wire
    user_input_data_ports = []   # inputs OTHER than clk/rst_n/start, in port-list order
    user_output_data_ports = []  # outputs OTHER than done/busy, in port-list order

    start_port = None
    done_port = None
    busy_port = None
    has_busy_port = False

    for p in ports:
        lname = p.name.lower()
        if p.direction == 'input':
            if lname in clock_names:
                user_input_ports.append({
                    'name': p.name, 'width': p.width,
                    'connection': 'clk', 'is_clock': True,
                })
            elif lname in reset_names:
                user_input_ports.append({
                    'name': p.name, 'width': p.width,
                    'connection': 'rst_n', 'is_reset': True,
                })
            elif lname in start_names:
                start_port = p.name
            else:
                user_input_ports.append({
                    'name': p.name, 'width': p.width,
                    'connection': _port_connection(p, p.width),
                })
                user_input_data_ports.append({'name': p.name, 'width': p.width})
        elif p.direction == 'output':
            if lname in done_names:
                done_port = p.name
            elif lname in busy_names:
                busy_port = p.name
                has_busy_port = True
            else:
                user_output_ports.append({
                    'name': p.name, 'width': p.width,
                    'connection': '', 'lint_off': True,
                })
                user_output_data_ports.append({'name': p.name, 'width': p.width})

    # Handshake detection
    user_has_handshake = start_port is not None and done_port is not None
    if user_has_handshake:
        # For handshake mode, each data port has `array_count` elements
        # of `bram_width` bits each.  The wrapper's INPUT_COUNT and
        # OUTPUT_COUNT are the *total number of elements* across all
        # data ports of the user module.
        for p in user_input_data_ports:
            if p['width'] != bram_width:
                return {
                    'success': False,
                    'error': f"User input port '{p['name']}' is {p['width']} bits but BRAM is {bram_width} bits",
                    'message': 'Port width mismatch',
                }
        for p in user_output_data_ports:
            if p['width'] != bram_width:
                return {
                    'success': False,
                    'error': f"User output port '{p['name']}' is {p['width']} bits but BRAM is {bram_width} bits",
                    'message': 'Port width mismatch',
                }

    # Compute total element counts across all data ports of each direction.
    # For matrix_mult_4x4, A[0:3][0:3]=16 + B[0:3][0:3]=16 → 32 inputs.
    def _port_total_elements(p) -> int:
        # p is a port-dict {'name', 'width', 'array_count'}; the
        # original Port object's array_count is what we need.
        return p.get('array_count', 1)

    # Re-iterate to populate array_count on the data-port dicts (since
    # the input parsing kept it on the Port object).
    for i, p in enumerate(user_input_data_ports):
        for orig in ports:
            if orig.name == p['name']:
                user_input_data_ports[i]['array_count'] = orig.array_count
                break
    for i, p in enumerate(user_output_data_ports):
        for orig in ports:
            if orig.name == p['name']:
                user_output_data_ports[i]['array_count'] = orig.array_count
                break

    input_count  = sum(_port_total_elements(p) for p in user_input_data_ports)  if user_has_handshake else 0
    output_count = sum(_port_total_elements(p) for p in user_output_data_ports) if user_has_handshake else 0

    # Build flat ordered port-name lists with their target index in
    # user_in_reg[] / user_out_wire[].  For matrix_mult_4x4, the
    # input data ports A[0:3][0:3] (16 elements) and B[0:3][0:3]
    # (16 elements) flatten to A0..A15, B0..B15 in row-major order.
    # For scalar ports, each name maps to one element.
    user_input_data_port_names = []   # [{'name', 'index'}]
    user_output_data_port_names = []  # [{'name', 'index'}]
    user_input_data_port_names_grouped = []   # [{'name','index','array_count'}]
    user_output_data_port_names_grouped = []  # [{'name','index','array_count'}]
    if user_has_handshake:
        idx = 0
        for p in user_input_data_ports:
            ac = p.get('array_count', 1)
            user_input_data_port_names.append({'name': p['name'], 'index': idx})
            user_input_data_port_names_grouped.append({
                'name': p['name'], 'index': idx, 'array_count': ac,
            })
            idx += ac
        idx = 0
        for p in user_output_data_ports:
            ac = p.get('array_count', 1)
            user_output_data_port_names.append({'name': p['name'], 'index': idx})
            user_output_data_port_names_grouped.append({
                'name': p['name'], 'index': idx, 'array_count': ac,
            })
            idx += ac

    # Index widths (ceil(log2(n+1))) for launch/dump counters.
    # We use n+1 so the counter can also equal n+1 (e.g. dump_idx==OUTPUT_COUNT).
    def _idx_w(n: int) -> int:
        if n <= 1:
            return 1
        return max(1, int(math.ceil(math.log2(n + 1))))
    input_count_idx_w  = _idx_w(input_count)  if user_has_handshake else 1
    output_count_idx_w = _idx_w(output_count) if user_has_handshake else 1

    # PISO result count: in handshake mode, it's output_count; in
    # pass-through, it tracks sipo_cnt (data words received from Rabbit).
    piso_result_count = (
        f'8\'d{output_count}' if user_has_handshake else 'sipo_cnt[7:0]'
    )

    wrapper_ctx: Dict[str, Any] = {
        'top_module': top_module,
        'wrapper_module': f"{top_module}__stream_wrapper",
        'bram_module': bram_name,
        'user_source': os.path.basename(source_file),
        'in_port': in_name,
        'out_port': out_name,
        'width': bram_width,
        'WIDTH': bram_width,
        'depth': bram_depth,
        'addr_w': addr_w,
        'baud_div': baud_div,
        'idle_timeout': idle_timeout,
        'user_input_ports': user_input_ports,
        'user_output_ports': user_output_ports,
        'user_has_handshake': user_has_handshake,
        'user_input_data_ports': user_input_data_ports,
        'user_output_data_ports': user_output_data_ports,
        'user_input_data_port_names': user_input_data_port_names,
        'user_output_data_port_names': user_output_data_port_names,
        'user_input_data_port_names_grouped': user_input_data_port_names_grouped,
        'user_output_data_port_names_grouped': user_output_data_port_names_grouped,
        'start_port': start_port or 'start',
        'done_port': done_port or 'done',
        'busy_port': busy_port or 'busy',
        'has_busy_port': has_busy_port,
        'input_count': input_count,
        'output_count': output_count,
        'input_count_idx_w': input_count_idx_w,
        'output_count_idx_w': output_count_idx_w,
        'piso_result_count': piso_result_count,
        'generation_date': datetime.now().strftime('%Y.%m.%d'),
    }

    top_ctx: Dict[str, Any] = {
        'top_module': top_module,
        'stream_module': f"{top_module}__stream_wrapper",
        'generation_date': wrapper_ctx['generation_date'],
        'width':  bram_width,
        'WIDTH':  bram_width,
        'depth':  bram_depth,
    }

    # Render and write

    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        return {'success': False, 'error': str(e), 'message': f'Cannot create {out_dir}'}

    template_dir = os.path.dirname(os.path.abspath(__file__))

    bram_path = os.path.join(out_dir, f"{bram_name}.v")
    wrapper_path = os.path.join(out_dir, f"{top_module}__stream_wrapper.v")
    top_path = os.path.join(out_dir, f"{top_module}__stream_top.v")
    sipo_path = os.path.join(out_dir, f"{top_module}__stream_sipo.v")
    piso_path = os.path.join(out_dir, f"{top_module}__stream_piso.v")

    sipo_ctx = {
        'top_module': top_module,
        "generation_date": wrapper_ctx["generation_date"],
        "width":  bram_width,
        "count":  input_count,
        "addr_w": addr_w,
    }
    piso_ctx = {
        'top_module': top_module,
        "generation_date": wrapper_ctx["generation_date"],
        "width":    bram_width,
        "count":    output_count,
        "baud_div": baud_div,
        "addr_w":   addr_w,
    }

    try:
        with open(bram_path, 'w', encoding='utf-8') as f:
            f.write(_ensure_trailing_newline(bram_result['verilog_code']))
        with open(sipo_path, 'w', encoding='utf-8') as f:
            f.write(_ensure_trailing_newline(_render(template_dir, 'stream_sipo.j2', sipo_ctx)))
        with open(piso_path, 'w', encoding='utf-8') as f:
            f.write(_ensure_trailing_newline(_render(template_dir, 'stream_piso.j2', piso_ctx)))
        with open(wrapper_path, 'w', encoding='utf-8') as f:
            f.write(_ensure_trailing_newline(_render(template_dir, 'stream_wrapper.j2', wrapper_ctx)))
        with open(top_path, 'w', encoding='utf-8') as f:
            f.write(_ensure_trailing_newline(_render(template_dir, 'stream_top.j2', top_ctx)))
    except OSError as e:
        return {'success': False, 'error': str(e), 'message': f'Cannot write to {out_dir}'}

    return {
        'success': True,
        'wrapper_file': wrapper_path,
        'top_file': top_path,
        'bram_file': bram_path,
        'sipo_file': sipo_path,
        'piso_file': piso_path,
        'files': [bram_path, sipo_path, piso_path, wrapper_path, top_path],
        'message': (
            f"Stream wrapper generated for {top_module} "
            f"({bram_width}x{bram_depth} BRAM, "
            f"in={in_name}, out={out_name})"
        ),
    }


# =============================================================================
#  CLI entry
# =============================================================================

def main(args_list: Optional[List[str]] = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        prog='ip_generator stream',
        description='Wrap a user module with SIPO/PISO + BRAM to expose 3-wire serial I/O',
    )
    parser.add_argument('--source', '-s', required=True,
                        help='Path to user Verilog source file')
    parser.add_argument('--top', '-t', default=None,
                        help='Top module name (required unless --print-modules)')
    parser.add_argument('--input-port', default=None,
                        help='Parallel input data port ("name" or "name:width")')
    parser.add_argument('--output-port', default=None,
                        help='Parallel output data port ("name" or "name:width")')
    parser.add_argument('--bram-width', type=int, default=None)
    parser.add_argument('--bram-depth', type=int, default=None)
    parser.add_argument('--out-dir', default='.')
    parser.add_argument('--baud-div', type=int, default=2)
    parser.add_argument('--idle-timeout', type=int, default=2000)
    parser.add_argument('--no-sipo', action='store_true')
    parser.add_argument('--no-piso', action='store_true')
    parser.add_argument('--print-modules', action='store_true')
    args = parser.parse_args(args_list)

    if args.print_modules:
        result = parse_modules(args.source)
    else:
        # Validate required args
        missing = [k for k, v in {
            'top': args.top, 'input-port': args.input_port,
            'output-port': args.output_port, 'bram-width': args.bram_width,
            'bram-depth': args.bram_depth,
        }.items() if v is None]
        if missing:
            result = {
                'success': False,
                'error': f"Missing required arguments: {', '.join(missing)}",
                'message': 'Missing required arguments',
            }
        else:
            result = generate_stream_ip(
                source_file=args.source,
                top_module=args.top,
                input_port=args.input_port,
                output_port=args.output_port,
                bram_width=args.bram_width,
                bram_depth=args.bram_depth,
                out_dir=args.out_dir,
                baud_div=args.baud_div,
                idle_timeout=args.idle_timeout,
                include_sipo=not args.no_sipo,
                include_piso=not args.no_piso,
            )

    print(json.dumps(result, indent=2))
    return 0 if result.get('success') else 1


if __name__ == '__main__':
    import sys
    sys.exit(main())
