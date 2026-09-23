#!/usr/bin/env python3
import os
import json
import sys
import re
import math
import argparse
from datetime import datetime
from typing import Dict, List, Tuple, Optional, Any
from jinja2 import Environment, FileSystemLoader, StrictUndefined

_RADIX_MAP = {'DEC': 'DEC', 'HEX': 'HEX', 'BIN': 'BIN', 'OCT': 'OCT', 'UNS': 'UNS'}

# =============================================================================
#  BRAM primitive type registry
# =============================================================================
#  Each entry describes one supported BRAM primitive family.  The external
#  port interface (ADDRA, CLKA, ENA, WEA, DOA, ADDRB, DIB, CLKB, ENB, WEB,
#  DOB) is identical across types; only the *internal* primitive
#  instantiation and INIT layout differ.
#
#  Schema:
#    primitive_size_bits  — total bits per physical primitive instance
#    init_lines           — number of INIT_xx parameters per primitive
#    init_bits_per_line   — bits per INIT parameter
#    valid_combinations   — set of (width, depth) tuples that the primitive
#                           family supports natively; None for "any"
#    template             — Jinja2 template filename (without .j2)
#    description          — human-readable label
# =============================================================================
def _ram8b_combinations():
    """(width, depth) pairs the RAMB8BWER template can actually build.

    RAMB8BWER is 1 Kb with a 9-bit address bus, and the template slices the
    data width across parallel primitives, so:
      * depth <= 512                (9 address bits on the primitive)
      * width_one = 1024/depth      (bits per primitive) must be >= 1
      * width must be a multiple of width_one
    Combinations outside this set used to either crash the generator or
    silently drop the top address bit.
    """
    out = set()
    for depth in (32, 64, 128, 256, 512):
        width_one = 1024 // depth
        for width in (1, 2, 4, 8, 16, 32):
            if width_one >= 1 and width % width_one == 0:
                out.add((width, depth))
    return out


BRAM_TYPES = {
    "ram4s": {
        "description": "Xilinx Spartan-6 / Fudan FDP3P7 (RAMB4_S*)",
        "primitive_size_bits": 4096,
        "init_lines": 16,
        "init_bits_per_line": 256,
        "valid_combinations": {
            (1, 4096), (2, 2048), (4, 1024), (8, 512), (16, 256),
            (2, 4096), (4, 2048), (8, 1024), (16, 512), (32, 256),
            (4, 4096), (8, 2048), (16, 1024), (32, 512), (64, 256),
            (8, 4096), (16, 2048), (32, 1024), (64, 512), (128, 256),
            (16, 4096), (32, 2048), (64, 1024), (128, 512), (256, 256),
        },
        "template": "bram_template.j2",
    },
    "ram8b": {
        "description": "Xilinx Spartan-6 (RAMB8BWER)",
        "primitive_size_bits": 1024,
        "init_lines": 16,
        "init_bits_per_line": 64,
        # RAMB8BWER is a 1 Kb primitive with a 9-bit address bus: at most 512
        # locations, and width x depth must be (primitive count) x 1024 with
        # at least one data bit per primitive.  The old table advertised
        # depths up to 65536, which either crashed the generator
        # (ZeroDivisionError, width_A_one became 0) or silently dropped the
        # top address bit.  Only combinations this template can actually
        # build are listed now.
        "valid_combinations": _ram8b_combinations(),
        "template": "bram_template_ram8b.j2",
    },
    "ramb18e1": {
        "description": "Xilinx 7-series (RAMB18E1, 18Kb TDP)",
        "primitive_size_bits": 16384,   # 16,384 data bits (64 lines * 256)
        "init_lines": 64,
        "init_bits_per_line": 256,
        # TDP mode, widths 1/2/4/8/16 (no parity). 18/36 modes omitted.
        "valid_combinations": {
            # 1-bit
            (1, 16384), (1, 8192), (1, 4096), (1, 2048), (1, 1024), (1, 512),
            # 2-bit
            (2, 8192), (2, 4096), (2, 2048), (2, 1024), (2, 512),
            # 4-bit
            (4, 4096), (4, 2048), (4, 1024), (4, 512), (4, 256),
            # 8-bit
            (8, 2048), (8, 1024), (8, 512), (8, 256), (8, 128),
            # 16-bit
            (16, 1024), (16, 512), (16, 256), (16, 128), (16, 64),
        },
        "template": "bram_template_ramb18e1.j2",
    },
    "ramb36e1": {
        "description": "Xilinx 7-series / UltraScale (RAMB36E1, 36Kb TDP)",
        "primitive_size_bits": 32768,   # 32,768 data bits
        # RAMB36E1 exposes INIT_00..INIT_7F = 128 lines x 256 bits = 32,768
        # bits.  The old value (64) silently truncated the upper half of the
        # memory for every full-capacity configuration.
        "init_lines": 128,
        "init_bits_per_line": 256,
        # TDP mode, widths 1/2/4/8/16/32 (no parity). 18/36 modes omitted.
        "valid_combinations": {
            # 1-bit
            (1, 32768), (1, 16384), (1, 8192), (1, 4096), (1, 2048), (1, 1024),
            # 2-bit
            (2, 16384), (2, 8192), (2, 4096), (2, 2048), (2, 1024),
            # 4-bit
            (4, 8192), (4, 4096), (4, 2048), (4, 1024), (4, 512),
            # 8-bit
            (8, 4096), (8, 2048), (8, 1024), (8, 512), (8, 256), (8, 128),
            # 16-bit
            (16, 2048), (16, 1024), (16, 512), (16, 256), (16, 128),
            # 32-bit
            (32, 1024), (32, 512), (32, 256), (32, 128), (32, 64),
        },
        "template": "bram_template_ramb36e1.j2",
    },
    "generic": {
        "description": "Behavioral (pure Verilog, portable, no vendor IP)",
        "primitive_size_bits": None,   # N/A
        "init_lines": 0,
        "init_bits_per_line": 0,
        "valid_combinations": None,     # any (width, depth) accepted
        "template": "bram_template_generic.j2",
    },
}

# Xilinx 7-series RAMB18E1/RAMB36E1 only accept data-width values from the
# parity-mode set {0,1,2,4,9,18,36}.  The framework exposes "naked" widths
# {1,2,4,8,16,32} to users; we map them to the nearest valid primitive width
# by reserving the top bits for parity (which we tie to 0).  This mapping is
# used only for the defparam values (READ/WRITE_WIDTH_A/B) — the external
# data port widths stay at the user-requested value.
_PARITY_MODE_WIDTHS = {1: 1, 2: 2, 4: 4, 8: 9, 16: 18, 32: 36}


def _map_to_prim_width(user_width: int) -> int:
    """Map a user-facing width to the nearest valid RAMB18E1/36E1 width.

    The user-facing widths we support are 1, 2, 4, 8, 16, 32.  The
    Xilinx 7-series primitives reject 8/16/32 directly and require
    the corresponding parity-mode values 9/18/36.
    """
    if user_width in _PARITY_MODE_WIDTHS:
        return _PARITY_MODE_WIDTHS[user_width]
    raise ValueError(
        f"Width {user_width} not supported by ramb18e1/ramb36e1. "
        f"Supported: {sorted(_PARITY_MODE_WIDTHS)}"
    )

def _xilinx_prim_addr(name: str, addr_bits: int, prim_bits: int,
                      port_width: int) -> str:
    """Address expression for RAMB36E1 / RAMB18E1 that matches Xilinx's own
    BRAM_TDP_MACRO pattern.

    The 7-series BRAM primitives do NOT take a plain word address.  The word
    address sits in the TOP address bits and the bits below it select the
    column inside the 36/18/9-bit word, so for a 32-bit port the memory index
    is ADDR[14:5] -- not ADDR[9:0].  Xilinx's macro fills the column bits with
    1s and sets ADDR[15] high for the 36Kb primitive:

        ADDRA_WIDTH == 10  ->  {1'b1, ADDRA, 5'b11111}      (36Kb)
        ADDRA_WIDTH == 10  ->  {ADDRA, 4'b1111}             (18Kb)

    Feeding a zero-extended word address instead aliases every word below
    2**ceil(log2(word_width)) onto word 0, i.e. the whole buffer collapses to
    a single word.  Verified against unisims_ver 2022.2 (see
    benchmark/xsim_check/).
    """
    low = int(round(math.log2(port_width))) if port_width > 1 else 0   # column bits under the word address
    if prim_bits == 16:                       # RAMB36E1
        head, pad = "1'b1", 15 - low - addr_bits
    else:                                     # RAMB18E1
        head, pad = None, 14 - low - addr_bits
    if addr_bits >= prim_bits or pad < 0:     # depth too large for this primitive
        return f"{name}[{prim_bits - 1}:0]"
    parts = []
    if head:
        parts.append(head)
    if pad:
        parts.append(f"{pad}'b" + "0" * pad)
    parts.append(f"{name}[{addr_bits - 1}:0]")
    if low:
        parts.append(f"{low}'b" + "1" * low)
    if len(parts) == 1:
        return parts[0]
    return "{" + ", ".join(parts) + "}"


# Backward-compat: keep module-level _VALID_COMBINATIONS for old callers
# (defaults to ram4s set)
_VALID_COMBINATIONS = BRAM_TYPES["ram4s"]["valid_combinations"]

def _validate_combination(width: int, depth: int, port_name: str = "port",
                          bram_type: str = "ram4s") -> bool:
    if bram_type not in BRAM_TYPES:
        raise ValueError(f"Unknown BRAM type: {bram_type}. "
                         f"Valid: {list(BRAM_TYPES.keys())}")
    valid = BRAM_TYPES[bram_type]["valid_combinations"]
    if valid is None:
        return True   # generic: any combo
    return (width, depth) in valid

def _parse_data(data_str: str, data_radix: str) -> int:
    data_str = data_str.strip()
    if data_radix == 'HEX':
        return int(data_str, 16) if not data_str.startswith('0x') else int(data_str, 16)
    elif data_radix == 'BIN':
        return int(data_str, 2) if not data_str.startswith('0b') else int(data_str, 2)
    elif data_radix == 'OCT':
        return int(data_str, 8) if not data_str.startswith('0o') else int(data_str, 8)
    elif data_radix == 'UNS':
        return int(data_str)
    else:
        return int(data_str)

def read_mif(path: str, bram_type: str = "ram4s") -> Dict[str, Any]:
    result = {
        'mode': 'unknown', 'width': 0, 'depth': 0, 'widthA': 0, 'widthB': 0,
        'depthA': 0, 'depthB': 0, 'address_radix': 'DEC', 'data_radix': 'HEX',
        'data_dict': {}, 'data_array': [], 'hex_strings': [], 'success': False, 'error': None
    }
    try:
        if not os.path.exists(path):
            raise FileNotFoundError(f"not found: {path}")
        with open(path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        in_content = False
        for line in lines:
            line = line.strip()
            if not line or line.startswith('--'):
                continue
            if 'CONTENT BEGIN' in line.upper():
                in_content = True
                continue
            elif 'END;' in line.upper():
                break
            if not in_content:
                # One line may carry several assignments ("WIDTH=8; DEPTH=256;").
                # Parse them separately instead of failing on int('8; DEPTH').
                line = line.rstrip(';').strip()
                if ';' in line:
                    for _part in [p for p in line.split(';') if p.strip()]:
                        _m = re.match(r'\s*([A-Za-z_]+)\s*=\s*(\S+)', _part)
                        if _m:
                            _key = _m.group(1).strip().upper()
                            _field = {'WIDTH': 'width', 'DEPTH': 'depth',
                                      'WIDTHA': 'widthA', 'DEPTHA': 'depthA',
                                      'WIDTHB': 'widthB', 'DEPTHB': 'depthB',
                                      'ADDRESS_RADIX': 'address_radix',
                                      'DATA_RADIX': 'data_radix'}.get(_key)
                            if _field:
                                if 'RADIX' in _key:
                                    result[_field] = _m.group(2).strip().upper()
                                else:
                                    result[_field] = int(_m.group(2).strip(), 0)
                    if result['mode'] == 'unknown':
                        result['mode'] = ('dual' if result['widthA'] and result['depthA']
                                          else 'single')
                    continue
                line_upper = line.upper()
                if result['mode'] == 'unknown':
                    if 'WIDTHA=' in line_upper and 'DEPTHA=' in ''.join(lines).upper():
                        result['mode'] = 'dual'
                    elif 'WIDTH=' in line_upper and 'DEPTH=' in ''.join(lines).upper():
                        result['mode'] = 'single'
                for param, key in [('WIDTH=', 'width'), ('DEPTH=', 'depth'), ('WIDTHA=', 'widthA'),
                                   ('WIDTHB=', 'widthB'), ('DEPTHA=', 'depthA'), ('DEPTHB=', 'depthB')]:
                    if line_upper.startswith(param):
                        result[key] = int(line.split('=')[1].strip())
                if line_upper.startswith('ADDRESS_RADIX='):
                    result['address_radix'] = _RADIX_MAP.get(line.split('=')[1].strip().upper(), 'DEC')
                elif line_upper.startswith('DATA_RADIX='):
                    result['data_radix'] = _RADIX_MAP.get(line.split('=')[1].strip().upper(), 'HEX')
            else:
                line = line.split('--')[0].strip() if '--' in line else line.strip()
                if not (line := line.rstrip(';').strip()):
                    continue
                if range_match := re.match(r'\[(.+?)\s*\.\.\s*(.+?)\]\s*:\s*([^;]+);?', line):
                    start_addr_str, end_addr_str, data_str = range_match.group(1), range_match.group(2), range_match.group(3).strip()
                    start_addr = _parse_data(start_addr_str, result['address_radix'])
                    end_addr = _parse_data(end_addr_str, result['address_radix'])
                    data_value = _parse_data(data_str, result['data_radix'])
                    for addr in range(start_addr, end_addr + 1):
                        result['data_dict'][addr] = data_value
                elif single_match := re.match(r'(.+?)\s*:\s*([^;]+);?', line):
                    addr_str, data_str = single_match.group(1), single_match.group(2).strip()
                    addr = _parse_data(addr_str, result['address_radix'])
                    data_value = _parse_data(data_str, result['data_radix'])
                    result['data_dict'][addr] = data_value
        if result['mode'] == 'unknown':
            result['mode'] = 'dual' if (result['widthA'] > 0 or result['widthB'] > 0) else 'single' if result['width'] > 0 else None
        if not result['mode']:
            raise ValueError("Width and Depth parameters not found in MIF file")
        
        if result['mode'] == 'single':
            if result['width'] <= 0 or result['depth'] <= 0:
                raise ValueError(f"Single-port mode parameters invalid: width={result['width']}, depth={result['depth']}")
            if not _validate_combination(result['width'], result['depth'], bram_type=bram_type):
                valid_set = BRAM_TYPES[bram_type]["valid_combinations"]
                raise ValueError(f"Invalid single-port combination for {bram_type}: width={result['width']}, depth={result['depth']}. Must be one of: {sorted(valid_set) if valid_set else 'any'}")
            depth = result['depth']
            width = result['width']
        else:
            if result['widthA'] <= 0 or result['depthA'] <= 0 or result['widthB'] <= 0 or result['depthB'] <= 0:
                raise ValueError(f"Dual-port mode parameters invalid: A(w={result['widthA']},d={result['depthA']}), B(w={result['widthB']},d={result['depthB']})")
            if not _validate_combination(result['widthA'], result['depthA'], bram_type=bram_type):
                valid_set = BRAM_TYPES[bram_type]["valid_combinations"]
                raise ValueError(f"Invalid dual-port combination for port A ({bram_type}): width={result['widthA']}, depth={result['depthA']}. Must be one of: {sorted(valid_set) if valid_set else 'any'}")
            if not _validate_combination(result['widthB'], result['depthB'], bram_type=bram_type):
                valid_set = BRAM_TYPES[bram_type]["valid_combinations"]
                raise ValueError(f"Invalid dual-port combination for port B ({bram_type}): width={result['widthB']}, depth={result['depthB']}. Must be one of: {sorted(valid_set) if valid_set else 'any'}")
            if (result['widthA'] * result['depthA']) != (result['widthB'] * result['depthB']):
                raise ValueError("Dual-port capacity mismatch")
            depth = result['depthA']
            width = result['widthA']
        
        data_array = [0] * depth
        skipped, clamped = [], []
        for addr, value in result['data_dict'].items():
            if 0 <= addr < depth:
                data_array[addr] = value
            else:
                skipped.append(addr)
        if skipped:
            raise ValueError(
                f"{len(skipped)} MIF address(es) are outside the declared depth "
                f"({depth}): e.g. {sorted(skipped)[:4]}.  Fix the MIF instead of "
                f"silently losing that data.")
        hex_chars = (width + 3) // 4
        max_val = (1 << width) - 1
        for value in data_array:
            if value > max_val:
                clamped.append(value)
        if clamped:
            raise ValueError(
                f"{len(clamped)} MIF value(s) do not fit WIDTH={width} "
                f"(e.g. {clamped[0]:#x} > {max_val:#x}).  Values would be "
                f"silently truncated.")
        result['data_array'] = data_array
        result['hex_strings'] = [format(value, f'0{hex_chars}x') for value in data_array]
        result['success'] = True
    except Exception as e:
        result['error'] = str(e)
    return result

def _compute_init_data(module_number: int, width_A_one: int, depth_A: int,
                       init_lines_per_primitive: int, init_bits_per_line: int,
                       raw_data_array: list) -> List[List[str]]:
    """Compute INIT parameter strings for one or more BRAM primitives.

    Splits the data array across `module_number` primitives, each
    holding `width_A_one` bits × `depth_A` addresses.  Each INIT
    line covers (init_bits_per_line / width_A_one) addresses.

    Returns: list of `module_number` lists, each of `init_lines_per_primitive`
             hex strings of the form "{init_bits_per_line}'h<hex>".
    """
    init_data = []
    addresses_per_init = init_bits_per_line // width_A_one

    for module_idx in range(module_number):
        module_init_strings = []
        bit_start = module_idx * width_A_one

        for group_idx in range(init_lines_per_primitive):
            start_addr = group_idx * addresses_per_init
            end_addr = min(start_addr + addresses_per_init, depth_A)

            # Collect data for this group, from highest address to lowest (for INIT format)
            group_binary_str = ''
            for addr in range(end_addr - 1, start_addr - 1, -1):
                if addr < len(raw_data_array):
                    data_val = (raw_data_array[addr] >> bit_start) & ((1 << width_A_one) - 1)
                else:
                    data_val = 0
                group_binary_str += format(data_val, f'0{width_A_one}b')

            # Pad to init_bits_per_line if necessary
            if end_addr - start_addr < addresses_per_init:
                # The string is assembled highest-address-first, so the unused
                # slots of a partial line are at the MSB end.
                padding_bits = (addresses_per_init - (end_addr - start_addr)) * width_A_one
                group_binary_str = '0' * padding_bits + group_binary_str

            # Convert binary string to hex
            hex_str = ''.join(
                format(int(group_binary_str[i:i+4], 2), 'x')
                for i in range(0, init_bits_per_line, 4)
            )
            module_init_strings.append(f"{init_bits_per_line}'h{hex_str}")

        init_data.append(module_init_strings)
    return init_data


def generate_bram_ip(module_name: str, width_A: int, depth_A: int, width_B: int, depth_B: int, raw_data_array: list,
                     bram_type: str = "ram4s") -> dict:
    """
    Generate BRAM IP Verilog code for the given primitive family.

    Args:
        module_name:     Name of the module
        width_A:         Width of port A
        depth_A:         Depth of port A
        width_B:         Width of port B (0 for single-port)
        depth_B:         Depth of port B (1 for single-port)
        raw_data_array:  Initial data array
        bram_type:       One of BRAM_TYPES keys: 'ram4s' | 'ram8b' | 'ramb18e1' |
                         'ramb36e1' | 'generic'

    Returns:
        Dictionary with result information including 'verilog_code'.
    """
    try:
        if bram_type not in BRAM_TYPES:
            return {
                'success': False,
                'error': f"Unknown BRAM type '{bram_type}'. Valid: {list(BRAM_TYPES.keys())}",
                'message': 'Invalid bram_type',
            }

        type_cfg = BRAM_TYPES[bram_type]
        template_dir = os.path.dirname(os.path.abspath(__file__))
        # StrictUndefined: a template variable the generator forgot to pass used to
        # render as an empty string, i.e. silently broken or invalid Verilog.
        env = Environment(loader=FileSystemLoader(template_dir), trim_blocks=True,
                          lstrip_blocks=True, undefined=StrictUndefined)
        env.filters['format_hex'] = lambda x: f"{x:02X}"
        template = env.get_template(f"templates/{type_cfg['template']}")

        gen_date = datetime.now().strftime("%Y.%m.%d")

        # ---- Generic (behavioral) path: no INIT, simple param-driven ----
        if bram_type == "generic":
            # The behavioral model has one shared array: both ports must have
            # identical geometry.  (It used to declare port B with port A's
            # widths and silently truncate the top address/data bits.)
            if width_B and (width_B != width_A or depth_B != depth_A):
                return {
                    'success': False,
                    'error': (f"The generic behavioral BRAM is symmetric: port A "
                              f"({width_A}x{depth_A}) and port B "
                              f"({width_B}x{depth_B}) must match.  Use a vendor "
                              f"primitive family for an asymmetric dual-port "
                              f"memory."),
                    'message': 'Asymmetric geometry not supported by generic BRAM',
                }
            addr_w = max(1, int(math.ceil(math.log2(depth_A))))
            # Port B may be asymmetric: give it its own address/data width
            # (the template used to declare both ports with port A's sizes,
            # truncating port B's top address bit and half of its data).
            addr_w_b = max(1, int(math.ceil(math.log2(depth_B)))) if depth_B > 0 else 1
            # The behavioural model has no INIT_xx parameters, so the initial
            # content is emitted as an initial block (the portable way to
            # preload a RAM; every synthesizer supports it).  Only the non-zero
            # words are listed to keep the file small.
            init_words = [(a, v) for a, v in enumerate(raw_data_array[:depth_A]) if v]
            template_params = {
                'module_name': module_name,
                'width_A': width_A, 'width_B': width_B,
                'addr_w': addr_w, 'addr_w_b': addr_w_b,
                'depth': depth_A, 'depth_b': depth_B if depth_B > 0 else depth_A,
                'init_words': init_words,
                'generation_date': gen_date,
            }
            verilog_code = template.render(**template_params)
            return {
                'success': True,
                'verilog_code': verilog_code,
                'message': f"BRAM IP (generic behavioral) generated: {module_name}.v"
            }

        # ---- Primitive-based path (ram4s, ram8b, ramb18e1, ramb36e1, ...) ----
        prim_size = type_cfg['primitive_size_bits']
        init_lines_max = type_cfg['init_lines']
        init_bits = type_cfg['init_bits_per_line']

        # If the requested (width × depth) is smaller than one primitive, we
        # still use a single primitive (with partial utilization).  This is
        # the case for our 8×512 tests against ramb18e1 (16Kb) / ramb36e1
        # (32Kb), which are much larger than the requested 4Kb.
        # Parallel primitive count.  Each primitive owns width_A_one data
        # bits, so it can never exceed the requested width; capping here
        # keeps width_A_one >= 1 (it used to become 0 for combinations such
        # as (1, 2048) and blew up in the division below).
        module_number = max(1, min(width_A, int(width_A * depth_A / prim_size)))
        width_A_one = int(width_A / module_number)
        if width_A_one < 1:
            return {
                'success': False,
                'error': (f"Cannot build {width_A}x{depth_A} from "
                          f"{bram_type} primitives: capacity or address range "
                          f"exceeds what this primitive family supports."),
                'message': 'Unsupported BRAM geometry',
            }
        # Each primitive holds depth_A addresses, so depth must fit its
        # address bus (RAMB4 = 4096 max at 1 bit, RAMB8BWER = 512).
        if depth_A > prim_size // width_A_one:
            return {
                'success': False,
                'error': (f"Cannot build {width_A}x{depth_A} from {bram_type}: "
                          f"a primitive sliced to {width_A_one} bit(s) only "
                          f"addresses {prim_size // width_A_one} locations."),
                'message': 'Depth exceeds primitive capacity',
            }
        width_B_one = int(width_B / module_number) if width_B > 0 else 0

        # For primitives with large INIT space (ramb18e1=64, ramb36e1=64), we
        # only need the lines that actually cover the requested depth.
        addresses_per_init = init_bits // width_A_one
        init_lines_per_instance = (depth_A + addresses_per_init - 1) // addresses_per_init
        # Never silently drop initialisation data: if the requested geometry
        # needs more INIT lines than the primitive has, the generated memory
        # would come up half/partially initialised.
        if init_lines_per_instance > init_lines_max:
            return {
                'success': False,
                'error': (f"{width_A}x{depth_A} needs {init_lines_per_instance} "
                          f"INIT lines but {bram_type} only provides "
                          f"{init_lines_max}."),
                'message': 'INIT data does not fit the primitive',
            }

        # Compute INIT data layout
        init_data = _compute_init_data(
            module_number=module_number,
            width_A_one=width_A_one,
            depth_A=depth_A,
            init_lines_per_primitive=init_lines_per_instance,
            init_bits_per_line=init_bits,
            raw_data_array=raw_data_array,
        )

        # For 7-series BRAMs, the defparam values for READ/WRITE_WIDTH must
        # come from the parity-mode set {1,2,4,9,18,36}.  Map 8/16/32 → 9/18/36.
        # External data-port widths stay at the user-requested value.
        if bram_type in ("ramb18e1", "ramb36e1"):
            prim_width_A = _map_to_prim_width(width_A)
            prim_width_B = _map_to_prim_width(width_B) if width_B > 0 else 0
        else:
            prim_width_A = width_A
            prim_width_B = width_B

        # RAMB8BWER has a 9-bit address bus.  Zero-extend (or slice) the
        # module's address port for it.  Built here rather than in the
        # template: the "{{N{1'b0}}, ADDR...}" form needs nested braces that
        # are easy to get wrong in Jinja (a previous attempt emitted
        # "{3{1'b0}, ADDRA[5:0]}" which is not legal Verilog).
        def _addr_expr(name: str, bits: int, prim_bits: int = 9) -> str:
            pad = prim_bits - bits
            if pad > 0:
                return "{{" + str(pad) + "{1'b0}}, " + name + "[" + str(bits - 1) + ":0]}"
            return name + "[" + str(prim_bits - 1) + ":0]"

        addr_bits_A = int(math.log2(depth_A))
        addr_bits_B = int(math.log2(depth_B)) if depth_B > 1 else 1
        prim_addr_bits = {"ramb36e1": 16, "ramb18e1": 14}.get(bram_type, 0)
        if prim_addr_bits:
            addr_a_expr = _xilinx_prim_addr('ADDRA', addr_bits_A, prim_addr_bits, width_A)
            addr_b_expr = _xilinx_prim_addr('ADDRB', addr_bits_B, prim_addr_bits, width_B or width_A)
        else:
            addr_a_expr = _addr_expr('ADDRA', addr_bits_A)
            addr_b_expr = _addr_expr('ADDRB', addr_bits_B)

        template_params = {
            'module_name': module_name,
            'width_A': width_A, 'depth_A': addr_bits_A,
            'addr_a_expr': addr_a_expr,
            'addr_b_expr': addr_b_expr,
            'width_A_one': width_A_one,
            'width_B': width_B, 'depth_B': int(math.log2(depth_B)) if depth_B > 1 else 0,
            'width_B_one': width_B_one,
            'prim_width_A': prim_width_A,
            'prim_width_B': prim_width_B,
            'init_data': init_data, 'module_number': module_number,
            'generation_date': gen_date,
            'init_bits_per_line': init_bits,
            'init_lines_per_instance': init_lines_per_instance,
            'init_lines_max': init_lines_max,
        }

        verilog_code = template.render(**template_params)

        return {
            'success': True,
            'verilog_code': verilog_code,
            'message': f"BRAM IP ({bram_type}) generated: {module_name}.v"
        }

    except Exception as e:
        return {
            'success': False,
            'error': str(e),
            'message': f"Failed to generate BRAM IP: {e}"
        }

def generate_bram_from_mif(mif_file: str, output_file: Optional[str] = None,
                           bram_type: str = "ram4s") -> dict:
    """
    Generate BRAM IP from MIF file.

    Args:
        mif_file: Path to MIF file
        output_file: Optional output file path
        bram_type: BRAM primitive family (used for (width, depth) validation
                   against the primitive's native valid combinations)

    Returns:
        Dictionary with result information
    """
    mif_result = read_mif(mif_file, bram_type=bram_type)
    module_name = os.path.splitext(os.path.basename(mif_file))[0]
    
    if not mif_result['success']:
        return {
            'success': False,
            'error': mif_result['error'],
            'message': f"Failed to read MIF file: {mif_result['error']}"
        }
    
    try:
        width_A = mif_result['width'] if mif_result['mode'] == 'single' else mif_result['widthA']
        depth_A = mif_result['depth'] if mif_result['mode'] == 'single' else mif_result['depthA']
        width_B = 0 if mif_result['mode'] == 'single' else mif_result['widthB']
        depth_B = 1 if mif_result['mode'] == 'single' else mif_result['depthB']
        
        result = generate_bram_ip(module_name, width_A, depth_A, width_B, depth_B, mif_result['data_array'], bram_type=bram_type)
        
        if result['success']:
            output_path = output_file or f"{module_name}.v"
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(result['verilog_code'])
            
            return {
                'success': True,
                'output_file': output_path,
                'message': f"BRAM IP generated successfully: {output_path}"
            }
        else:
            return result
            
    except Exception as e:
        return {
            'success': False,
            'error': str(e),
            'message': f"Failed to generate BRAM IP: {e}"
        }

def main(args_list: Optional[list[str]] = None) -> int:
    """
    Main entry point for BRAM generator.
    
    Args:
        args_list: Optional command line arguments (for programmatic use)
    
    Returns:
        Exit code (0 for success, 1 for error)
    """
    parser = argparse.ArgumentParser(description='Generate BRAM IP Verilog code')
    
    parser.add_argument('mif_file', type=str, help='Input MIF file path')
    parser.add_argument('--output', type=str, help='Output Verilog file')
    parser.add_argument('--bram-type', '-t', type=str, default='ram4s',
                        choices=list(BRAM_TYPES.keys()),
                        help='BRAM primitive family (default: ram4s)')
    
    args = parser.parse_args(args_list)
    
    result = generate_bram_from_mif(args.mif_file, args.output,
                                   bram_type=getattr(args, 'bram_type', 'ram4s'))
    print(json.dumps(result))
    
    return 0 if result['success'] else 1

if __name__ == '__main__':
    sys.exit(main())
