#!/usr/bin/env python3
"""
PLL SeriWrap

Generates PLL Verilog modules based on:
- Divide by Value: 2, 4, 8, 16
- FPGA Gates: 30 (30W), 50 (50W)

Usage:
    python pll_generator.py --divide <value> --gates <30|50> --output <file>
"""

from __future__ import annotations

import argparse
import sys
import json
from pathlib import Path
from typing import Optional

try:
    from jinja2 import Environment, FileSystemLoader
except ImportError:
    print("Error: jinja2 is required. Install with: pip install jinja2")
    sys.exit(1)


# Valid combinations based on src files
VALID_DIVIDE_VALUES = [2, 4, 8, 16]
VALID_FPGA_GATES = [30, 50]


def validate_inputs(divide_value: int, fpga_gates: int) -> bool:
    """
    Validate the input parameters against supported combinations.
    
    Args:
        divide_value: The divide by value (2, 4, 8, 16)
        fpga_gates: FPGA gate count (30 for 30W, 50 for 50W)
    
    Returns:
        True if valid, raises ValueError otherwise
    """
    if divide_value not in VALID_DIVIDE_VALUES:
        raise ValueError(
            f"Invalid divide value: {divide_value}. "
            f"Must be one of: {VALID_DIVIDE_VALUES}"
        )
    
    if fpga_gates not in VALID_FPGA_GATES:
        raise ValueError(
            f"Invalid FPGA gates: {fpga_gates}. "
            f"Must be one of: {VALID_FPGA_GATES} (30 for 30W, 50 for 50W)"
        )
    
    return True


def get_template_dir() -> Path:
    """Get the directory containing the Jinja2 template file."""
    script_dir = Path(__file__).parent
    return script_dir / "templates"


def generate_pll(
    divide_value: int,
    fpga_gates: int,
    output_file: Optional[str] = None
) -> dict:
    """
    Generate PLL Verilog code using Jinja2 template.
    
    Args:
        divide_value: The divide by value (2, 4, 8, 16)
        fpga_gates: FPGA gate count (30 for 30W, 50 for 50W)
        output_file: Optional output file path
    
    Returns:
        Dictionary with result information
    """
    try:
        # Validate inputs
        validate_inputs(divide_value, fpga_gates)
        
        # Setup Jinja2 environment
        template_dir = get_template_dir()
        template_path = template_dir / "pll_template.j2"
        
        if not template_path.exists():
            return {
                'success': False,
                'error': f"Template file not found: {template_path}",
                'message': f"Template file not found: {template_path}"
            }
        
        env = Environment(
            loader=FileSystemLoader(template_dir),
            trim_blocks=True,
            lstrip_blocks=True
        )
        template = env.get_template("pll_template.j2")
        
        # Render template
        verilog_code = template.render(
            divide_value=divide_value,
            fpga_gates=fpga_gates
        )
        
        # Write to file if specified
        if output_file is not None:
            output_path = Path(output_file)
            output_path.write_text(verilog_code, encoding='utf-8')
        
        return {
            'success': True,
            'output_file': output_file,
            'verilog_code': verilog_code if output_file is None else None,
            'message': f"PLL IP generated successfully: PLL_{divide_value}_{fpga_gates}.v"
        }
        
    except Exception as e:
        return {
            'success': False,
            'error': str(e),
            'message': f"Failed to generate PLL IP: {e}"
        }


def generate_all_combinations(output_dir: Optional[str] = None) -> dict:
    """
    Generate all valid PLL combinations.
    
    Args:
        output_dir: Optional output directory for generated files
    
    Returns:
        Dictionary with result information
    """
    output_path: Optional[Path] = None
    if output_dir is not None:
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
    
    generated_files: list[str] = []
    errors: list[str] = []
    
    for divide in VALID_DIVIDE_VALUES:
        for gates in VALID_FPGA_GATES:
            try:
                if output_path is not None:
                    filename = f"PLL_{divide}_{gates}.v"
                    output_file = str(output_path / filename)
                else:
                    output_file = None
                
                result = generate_pll(divide, gates, output_file)
                if result['success']:
                    generated_files.append(f"PLL_{divide}_{gates}.v")
                else:
                    errors.append(f"PLL_{divide}_{gates}: {result.get('error', 'Unknown error')}")
            except Exception as e:
                errors.append(f"PLL_{divide}_{gates}: {e}")
    
    return {
        'success': len(errors) == 0,
        'generated_files': generated_files,
        'errors': errors,
        'message': f"Generated {len(generated_files)} files" + (f", {len(errors)} errors" if errors else "")
    }


def main(args_list: Optional[list[str]] = None) -> int:
    """
    Main entry point for PLL generator.
    
    Args:
        args_list: Optional command line arguments (for programmatic use)
    
    Returns:
        Exit code (0 for success, 1 for error)
    """
    parser = argparse.ArgumentParser(
        description="PLL SeriWrap - Generate PLL Verilog modules",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python pll_generator.py --divide 2 --gates 30 --output PLL_2_30.v
    python pll_generator.py --divide 16 --gates 50 --output PLL_16_50.v
    python pll_generator.py --all --output-dir ./generated
        """
    )
    
    parser.add_argument(
        "--divide", "-d",
        type=int,
        choices=VALID_DIVIDE_VALUES,
        help=f"Divide by value. Choices: {VALID_DIVIDE_VALUES}"
    )
    
    parser.add_argument(
        "--gates", "-g",
        type=int,
        choices=VALID_FPGA_GATES,
        help=f"FPGA gate count (30 for 30W, 50 for 50W). Choices: {VALID_FPGA_GATES}"
    )
    
    parser.add_argument(
        "--output", "-o",
        type=str,
        help="Output file path"
    )
    
    parser.add_argument(
        "--all", "-a",
        action="store_true",
        help="Generate all valid combinations"
    )
    
    parser.add_argument(
        "--output-dir",
        type=str,
        default=".",
        help="Output directory for --all mode (default: current directory)"
    )
    
    args = parser.parse_args(args_list)
    
    try:
        if args.all:
            # Generate all combinations
            result = generate_all_combinations(args.output_dir)
            print(json.dumps(result))
            return 0 if result['success'] else 1
        elif args.divide is not None and args.gates is not None:
            # Generate single combination
            result = generate_pll(args.divide, args.gates, args.output)
            print(json.dumps(result))
            return 0 if result['success'] else 1
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


if __name__ == "__main__":
    sys.exit(main())
