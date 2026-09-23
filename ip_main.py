#!/usr/bin/env python3
"""
SeriWrap

A unified entry point for generating BRAM, PLL, and Stream IP modules.
This reduces bundle size by sharing the Jinja2 engine.

Usage:
    # BRAM generation
    python ip_main.py bram <mif_file> [--output <file>]

    # PLL generation
    python ip_main.py pll --divide <value> --gates <30|50> [--output <file>]
    python ip_main.py pll --all [--output-dir <dir>]

    # Stream wrapper generation (auto BRAM config: --bram-width 0)
    python ip_main.py stream --source <user.v> --top <module> \
        --bram-width <W> --bram-depth <D> [--out-dir <dir>] [--binpack]
    python ip_main.py stream --source <user.v> --top <module> --bram-width 0
    python ip_main.py stream --source <user.v> --print-modules

    # Show help
    python ip_main.py --help
    python ip_main.py bram --help
    python ip_main.py pll --help
    python ip_main.py stream --help
"""

import sys
import json
import argparse
from typing import Optional

import bram_generator
import pll_generator
import stream_generator


def handle_bram(args) -> int:
    result = bram_generator.generate_bram_from_mif(args.mif_file, args.output,
                                                   bram_type=args.bram_type)
    print(json.dumps(result))
    return 0 if result['success'] else 1


def handle_pll(args) -> int:
    if args.all:
        result = pll_generator.generate_all_combinations(args.output_dir)
        print(json.dumps(result))
        return 0 if result['success'] else 1
    else:
        result = pll_generator.generate_pll(args.divide, args.gates, args.output)
        print(json.dumps(result))
        return 0 if result['success'] else 1


def _load_handshake_ports(args):
    """Resolve handshake port-name aliases from defaults + file + CLI flags.

    Priority (lowest to highest): built-in defaults < --handshake-config file <
    per-role --*-ports CLI flags. Returns a dict of role → list[str].
    """
    import os
    # 1. Built-in defaults ship with the package.
    defaults = stream_generator.DEFAULT_HANDSHAKE_PORTS.copy()
    # 2. Optional config file replaces the whole map.
    if args.handshake_config:
        path = args.handshake_config
        if not os.path.isfile(path):
            raise FileNotFoundError(f"--handshake-config not found: {path}")
        with open(path) as f:
            data = json.load(f)
        for k in defaults.keys():
            if k in data:
                defaults[k] = list(data[k])
    # 3. Per-role CLI flags override individual roles.
    overrides = {
        'clock': args.clock_ports,
        'reset': args.reset_ports,
        'start': args.start_ports,
        'done':  args.done_ports,
        'busy':  args.busy_ports,
    }
    for role, raw in overrides.items():
        if raw is not None:
            defaults[role] = [p.strip() for p in raw.split(',') if p.strip()]
    return defaults


def handle_stream(args) -> int:
    if args.print_modules:
        # Even --print-modules can use custom aliases (e.g. to inspect a
        # Vitis-HLS file with ap_* ports).
        result = stream_generator.parse_modules(
            args.source,
            handshake_ports=_load_handshake_ports(args),
        )
    else:
        # bram-depth is optional when bram-width=0 (auto mode)
        auto_bram = (args.bram_width is not None and args.bram_width == 0)
        missing = [k for k, v in {
            'top': args.top, 'bram-width': args.bram_width,
        }.items() if v is None]
        if not auto_bram and args.bram_depth is None:
            missing.append('bram-depth')
        if missing:
            result = {
                'success': False,
                'error': f"Missing required arguments: {', '.join(missing)}",
                'message': 'Missing required arguments',
            }
        elif args.bram_width is not None and args.bram_width < 0:
            result = {
                'success': False,
                'error': (f"--bram-width must be >= 0 (0 means auto, got "
                          f"{args.bram_width})"),
                'message': 'Invalid --bram-width',
            }
        elif (args.bram_depth is not None and args.bram_depth < 1
              and not auto_bram):
            # 0 used to reach math.log2() and die with "math domain error"
            result = {
                'success': False,
                'error': (f"--bram-depth must be >= 1 (got {args.bram_depth}); "
                          f"omit it, or use --bram-width 0, to let the tool pick "
                          f"the geometry"),
                'message': 'Invalid --bram-depth',
            }
        else:
            ctrl_in = [x.strip() for x in args.control_inputs.split(',') if x.strip()] \
                      if args.control_inputs else None
            ctrl_out = [x.strip() for x in args.control_outputs.split(',') if x.strip()] \
                       if args.control_outputs else None
            adapters_list = []
            if args.adapter:
                for spec in args.adapter:
                    if ':' not in spec:
                        result = {'success': False, 'error': f"Bad --adapter spec: {spec}",
                                  'message': 'Bad adapter spec'}
                        print(json.dumps(result))
                        return 1
                    atype, ports_str = spec.split(':', 1)
                    port_names = [p.strip().split('[')[0] for p in ports_str.split(',') if p.strip()]
                    adapters_list.append({'name': atype.strip(), 'ports': port_names})
            result = stream_generator.generate_stream_ip(
                source_file=args.source,
                top_module=args.top,
                bram_width=args.bram_width if args.bram_width is not None else 0,
                bram_depth=(args.bram_depth
                            if (args.bram_depth is not None
                                and not (auto_bram and args.bram_depth < 1))
                            else 512),
                out_dir=args.out_dir,
                baud_div=args.baud_div,
                width=args.width,          # was parsed but never forwarded
                baud_div_in=getattr(args, 'baud_div_in', None),
                baud_div_out=getattr(args, 'baud_div_out', None),
                include_sipo=not args.no_sipo,
                include_piso=not args.no_piso,
                debug=args.debug,
                control_inputs=ctrl_in,
                control_outputs=ctrl_out,
                pingpong=args.pingpong,
                bram_type=args.bram_type,
                adapters=adapters_list,
                input_source=args.input_source,
                trigger_key=args.trigger_key,
                numeric_width=args.numeric_width,
                handshake_ports=_load_handshake_ports(args),
                sync_mode=args.sync_mode,
                ready_signal=not getattr(args, 'no_ready', False),
                preprocess=getattr(args, 'preprocess', False),
                use_binpack=getattr(args, 'binpack', False),
                reset_polarity=getattr(args, 'reset_polarity', 'auto'),
            )
    # --bram-width 0 selects width AND depth from the primitive's valid set, so
    # an explicit --bram-depth would be silently ignored: say so.
    if result.get('success') and getattr(args, 'bram_width', None) == 0 and args.bram_depth is not None:
        msg = ("--bram-depth is ignored when --bram-width 0 (auto mode picks both "
               "width and depth from the valid combinations); pass an explicit "
               "--bram-width to control the depth.")
        result.setdefault('warnings', []).append(msg)
        print("WARNING: " + msg, file=sys.stderr)
    print(json.dumps(result))
    return 0 if result.get('success') else 1


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='ip_main',
        description='SeriWrap - Generate BRAM and PLL Verilog modules',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Generate BRAM from MIF file
    python ip_main.py bram input.mif --output bram_output.v
    
    # Generate single PLL
    python ip_main.py pll --divide 2 --gates 30 --output PLL_2_30.v
    
    # Generate all PLL combinations
    python ip_main.py pll --all --output-dir ./generated
        """
    )
    
    subparsers = parser.add_subparsers(dest='command', help='IP type to generate')
    
    bram_parser = subparsers.add_parser(
        'bram',
        help='Generate BRAM IP from MIF file',
        description='Generate BRAM (Block RAM) IP Verilog code from a Memory Initialization File (.mif)'
    )
    bram_parser.add_argument(
        'mif_file',
        type=str,
        help='Path to the input MIF file'
    )
    bram_parser.add_argument(
        '--output', '-o',
        type=str,
        help='Output Verilog file path (default: <mif_name>.v)'
    )
    bram_parser.add_argument(
        '--bram-type', '-t',
        type=str,
        choices=list(bram_generator.BRAM_TYPES.keys()),
        default='ram4s',
        help='BRAM primitive family (default: ram4s). Determines which (width, depth) '
             'combinations are accepted.'
    )
    
    pll_parser = subparsers.add_parser(
        'pll',
        help='Generate PLL IP',
        description='Generate PLL (Phase-Locked Loop) IP Verilog code'
    )
    
    pll_group = pll_parser.add_mutually_exclusive_group(required=True)
    pll_group.add_argument(
        '--divide', '-d',
        type=int,
        choices=pll_generator.VALID_DIVIDE_VALUES,
        help=f"Divide by value. Choices: {pll_generator.VALID_DIVIDE_VALUES}"
    )
    pll_group.add_argument(
        '--all', '-a',
        action='store_true',
        help='Generate all valid PLL combinations'
    )
    
    pll_parser.add_argument(
        '--gates', '-g',
        type=int,
        choices=pll_generator.VALID_FPGA_GATES,
        help=f"FPGA gate count (30 for 30W, 50 for 50W). Choices: {pll_generator.VALID_FPGA_GATES}"
    )
    
    pll_parser.add_argument(
        '--output', '-o',
        type=str,
        help='Output file path (for single PLL generation)'
    )
    
    pll_parser.add_argument(
        '--output-dir',
        type=str,
        default='.',
        help='Output directory for --all mode (default: current directory)'
    )

    # ----- stream subcommand -----
    stream_parser = subparsers.add_parser(
        'stream',
        help='Wrap a user module with SIPO/PISO + BRAM to expose 3-wire serial I/O',
        description=(
            'Generate a stream-wrapped Verilog IP from a user module. '
            'Produces <top>__stream_wrapper.v and <top>__stream_top.v '
            'in --out-dir.'
        ),
    )
    stream_parser.add_argument('--source', '-s', required=True,
        help='Path to the user Verilog source file')
    stream_parser.add_argument('--top', '-t',
        help='Top module name inside the source file')
    stream_parser.add_argument('--width', type=int, default=0,
        help='Data port width (0 = auto-detect from module ports)')
    stream_parser.add_argument('--out-dir', type=str, default='.',
        help='Output directory (default: current directory)')
    stream_parser.add_argument('--baud-div', type=int, default=2,
        help='BAUD_DIV parameter for the PISO module (default: 2 for sim)')
    stream_parser.add_argument('--no-piso', action='store_true',
        help='Skip the PISO + output BRAM (input-only streaming)')
    stream_parser.add_argument('--no-sipo', action='store_true',
        help='Skip the SIPO + input BRAM (output-only streaming)')
    stream_parser.add_argument('--debug', action='store_true',
        help='Include overflow/idle/busy debug status ports on the top module')
    stream_parser.add_argument('--print-modules', action='store_true',
        help='Parse --source and print discovered modules/ports as JSON, then exit')
    stream_parser.add_argument('--control-inputs', type=str, default=None,
        help='Comma-separated control input port names to expose on wrapper top-level')
    stream_parser.add_argument('--control-outputs', type=str, default=None,
        help='Comma-separated control output port names to expose on wrapper top-level')
    stream_parser.add_argument('--pingpong', action='store_true',
        help='Generate double-buffered (ping-pong) wrapper with 4 BRAM instances')
    stream_parser.add_argument('--bram-type', type=str, default='ram4s',
        choices=['ram4s', 'ram8b', 'ramb18e1', 'ramb36e1', 'generic'],
        help='BRAM primitive family (default: ram4s = Xilinx Spartan-6 / Fudan FDP3P7)')
    stream_parser.add_argument('--adapter', action='append', default=None,
        help='Physical device adapter binding. Format: TYPE:PORT[,PORT...] '
             'e.g. --adapter ps2:key_state[63:0],mod_state[7:0]. '
             'May be repeated for multiple adapters.')
    stream_parser.add_argument('--input-source',
        choices=list(stream_generator.VALID_INPUT_SOURCES),
        default='rabbit',
        help='Stream input source: "rabbit" (default 3-wire serial from host) '
             'or "adapter" (PS/2 keyboard via the bridge module; requires '
             '--adapter ps2).')
    stream_parser.add_argument('--trigger-key',
        choices=list(stream_generator.TRIGGER_BIT.keys()),
        default='enter',
        help='Which PS/2 key triggers the bridge into computing '
             '(only used with --input-source adapter). Default: enter.')
    stream_parser.add_argument('--numeric-width', type=int, default=32,
        choices=[8, 16, 32, 64],
        help='Width of the numeric accumulator (only used with '
             '--input-source adapter for BRAM-depth validation). '
             'Default: 32.')
    stream_parser.add_argument('--sync-mode', action='store_true',
        help='Use lightweight synchronous SIPO/PISO (no async FIFO, no baud-div '
             'serial clock). Reduces LUT overhead by ~50%% for single-clock-domain '
             'designs. The wrapper top-level port interface is unchanged.')
    stream_parser.add_argument('--handshake-config', type=str, default=None,
        help='Path to a JSON file with role→port-name mappings. '
             'Each key (clock/reset/start/done/busy) maps to a list of '
             'Verilog port names that satisfy that role. Replaces the '
             'built-in defaults wholesale; use --*-ports flags to override '
             'individual roles on top of the file.')
    stream_parser.add_argument('--clock-ports', type=str, default=None,
        help='Comma-separated clock port names (overrides --handshake-config).')
    stream_parser.add_argument('--reset-ports', type=str, default=None,
        help='Comma-separated reset port names (overrides --handshake-config).')
    stream_parser.add_argument('--start-ports', type=str, default=None,
        help='Comma-separated start port names (overrides --handshake-config).')
    stream_parser.add_argument('--done-ports', type=str, default=None,
        help='Comma-separated done port names (overrides --handshake-config).')
    stream_parser.add_argument('--busy-ports', type=str, default=None,
        help='Comma-separated busy port names (overrides --handshake-config).')
    stream_parser.add_argument('--baud-div-in', type=int, default=None,
        help='SIPO input baud divider (default: same as --baud-div). '
             'Lower values increase input bandwidth.')
    stream_parser.add_argument('--baud-div-out', type=int, default=None,
        help='PISO output baud divider (default: same as --baud-div). '
             'Higher values reduce output pin toggling.')
    stream_parser.add_argument('--binpack', action='store_true',
        help='Use FFD bin-packing port marshaling to reduce BRAM entry count '
             'by packing narrow ports into shared entries.')
    stream_parser.add_argument('--preprocess', action='store_true',
        help=r'Expand \`define and \`ifdef macros with verilator -E before parsing '
             '(needed when port widths are written with macros).')
    stream_parser.add_argument('--no-ready', action='store_true',
        help='Omit the s_ready back-pressure output (bit-exact reproduction of '
             'the original 21/69-pin interface; NOT recommended: a producer '
             'that starts the next frame early then silently loses words).')
    stream_parser.add_argument('--reset-polarity', type=str, default='auto',
        choices=['auto', 'active_high', 'active_low'],
        help="User module reset port polarity. 'auto' (default) detects from "
             "the port name (trailing '_n' → active-low, otherwise active-high). "
             "Use 'active_high' for ap_rst, 'rst', etc. or 'active_low' for "
             "rst_n, reset_n, etc.")
    stream_parser.add_argument('--bram-width', '-w', type=int, default=None,
        help='BRAM data width (REQUIRED unless 0 = auto-select). Set to 0 to '
             'let the tool pick a valid (width,depth) pair for --bram-type.')
    stream_parser.add_argument('--bram-depth', type=int, default=None,
        help='BRAM depth (optional with --bram-width 0, auto-selected).')

    return parser


def main(args_list: Optional[list[str]] = None) -> int:
    parser = create_parser()
    args = parser.parse_args(args_list)
    
    if args.command is None:
        parser.print_help()
        return 1
    
    try:
        if args.command == 'bram':
            return handle_bram(args)
        elif args.command == 'pll':
            return handle_pll(args)
        elif args.command == 'stream':
            return handle_stream(args)
        else:
            parser.print_help()
            return 1
    except Exception as e:
        result = {
            'success': False,
            'error': str(e),
            'message': f"Unexpected error: {e}"
        }
        print(json.dumps(result))
        return 1


if __name__ == '__main__':
    sys.exit(main())
