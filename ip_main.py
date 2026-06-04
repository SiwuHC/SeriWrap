#!/usr/bin/env python3
"""
Unified IP Generator

A unified entry point for generating BRAM, PLL, and Stream IP modules.
This reduces bundle size by sharing the Jinja2 engine.

Usage:
    # BRAM generation
    python ip_main.py bram <mif_file> [--output <file>]

    # PLL generation
    python ip_main.py pll --divide <value> --gates <30|50> [--output <file>]
    python ip_main.py pll --all [--output-dir <dir>]

    # Stream wrapper generation
    python ip_main.py stream --source <user.v> --top <module> \
        --input-port <name>[:<width>] --output-port <name>[:<width>] \
        --bram-width <W> --bram-depth <D> [--out-dir <dir>]
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
    result = bram_generator.generate_bram_from_mif(args.mif_file, args.output)
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


def handle_stream(args) -> int:
    if args.print_modules:
        result = stream_generator.parse_modules(args.source)
    else:
        # Validate required args for the generate path
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
            result = stream_generator.generate_stream_ip(
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
                debug=args.debug,
            )
    print(json.dumps(result))
    return 0 if result.get('success') else 1


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='ip_main',
        description='Unified IP Generator - Generate BRAM and PLL Verilog modules',
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
    stream_parser.add_argument('--input-port',
        help='Parallel input data port ("name" or "name:width")')
    stream_parser.add_argument('--output-port',
        help='Parallel output data port ("name" or "name:width")')
    stream_parser.add_argument('--bram-width', type=int,
        help='Width W of the dual-port BRAM')
    stream_parser.add_argument('--bram-depth', type=int,
        help='Depth D of the dual-port BRAM')
    stream_parser.add_argument('--out-dir', type=str, default='.',
        help='Output directory (default: current directory)')
    stream_parser.add_argument('--baud-div', type=int, default=2,
        help='BAUD_DIV parameter for the PISO module (default: 2 for sim)')
    stream_parser.add_argument('--idle-timeout', type=int, default=2000,
        help='IDLE_TIMEOUT parameter for the wrapper FSM (default: 2000)')
    stream_parser.add_argument('--no-piso', action='store_true',
        help='Skip the PISO + output BRAM (input-only streaming)')
    stream_parser.add_argument('--no-sipo', action='store_true',
        help='Skip the SIPO + input BRAM (output-only streaming)')
    stream_parser.add_argument('--debug', action='store_true',
        help='Include overflow/idle/busy debug status ports on the top module')
    stream_parser.add_argument('--print-modules', action='store_true',
        help='Parse --source and print discovered modules/ports as JSON, then exit')

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
