#!/usr/bin/env python3

from PIL import Image
import argparse
import os


def threshold_transform(x):
    return 0 if x < 128 else 255


def generate_preview(data, output_path):
    if len(data) != 1024:
        raise ValueError(f"Data size must be 1024 bytes, got {len(data)}")
    
    preview = Image.new('1', (128, 64), 1)
    pixels = preview.load()
    
    if pixels is None:
        raise RuntimeError("Failed to create preview pixel buffer")
    
    for tile_x in range(16):
        base_x = tile_x * 8
        base_addr = tile_x * 64
        
        for row in range(64):
            addr = base_addr + row
            byte_val = data[addr]
            y = row
            
            for col in range(8):
                x = base_x + col
                if byte_val & (1 << (7 - col)):
                    pixels[x, y] = 0
    
    preview.save(output_path)
    print(f"Preview saved: {output_path}")


def generate_preview_from_mif(mif_path, output_path):
    data = parse_mif_file(mif_path)

    if len(data) < 1024:
        print(f"Warning: MIF data size is {len(data)}, expected at least 1024 bytes")
        data = data + [0] * (1024 - len(data))
    elif len(data) > 1024:
        print(f"Warning: MIF has {len(data)} entries; preview uses the first 1024")

    generate_preview(data[:1024], output_path)


def _radix_to_base(radix):
    return {'HEX': 16, 'DEC': 10, 'UNS': 10, 'BIN': 2, 'OCT': 8}.get(radix, 16)


def parse_mif_file(mif_path):
    """Parse a MIF into an address-indexed byte list (DEPTH entries).

    The old version appended values in file order with a hard-coded base 16,
    so it ignored ADDRESS_RADIX/DATA_RADIX, could not handle "[a..b] : v"
    ranges (the file's own test MIFs use them) and silently produced a
    one-element list.  Ranges are expanded and every value is placed at its
    declared address now.
    """
    import re as _re
    addr_radix, data_radix, depth = 'DEC', 'HEX', None
    entries = []          # (start_addr, end_addr, value)
    in_content = False
    with open(mif_path, 'r') as fh:
        for raw in fh:
            line = raw.split('--')[0].strip()
            if not line:
                continue
            up = line.upper()
            if up.startswith('CONTENT BEGIN'):
                in_content = True
                continue
            if up.startswith('END'):
                break
            if not in_content:
                for part in line.split(';'):
                    if '=' in part:
                        k, v = [x.strip() for x in part.split('=', 1)]
                        k = k.upper()
                        if k == 'DEPTH':
                            depth = int(v, 0)
                        elif k == 'ADDRESS_RADIX':
                            addr_radix = v.upper()
                        elif k == 'DATA_RADIX':
                            data_radix = v.upper()
                continue
            if ':' not in line:
                continue
            lhs, rhs = line.split(':', 1)
            lhs = lhs.strip()
            rhs = rhs.strip().rstrip(';').strip()
            base_a, base_d = _radix_to_base(addr_radix), _radix_to_base(data_radix)
            try:
                value = int(rhs, base_d)
            except ValueError:
                continue
            m = _re.match(r'^\[\s*([0-9A-Fa-f]+)\s*\.\.\s*([0-9A-Fa-f]+)\s*\]$', lhs)
            if m:
                entries.append((int(m.group(1), base_a), int(m.group(2), base_a), value))
            else:
                try:
                    a = int(lhs, base_a)
                except ValueError:
                    continue
                entries.append((a, a, value))
    size = depth if depth else (max((e for _, e, _ in entries), default=-1) + 1)
    size = max(size, 1024)          # the graphic-LCD preview wants 1024 bytes
    data = [0] * size
    for a, b, v in entries:
        for addr in range(a, min(b, size - 1) + 1):
            data[addr] = v
    return data


def resize_keep_aspect(img, target_size):
    target_w, target_h = target_size
    img_w, img_h = img.size
    
    scale_w = target_w / img_w
    scale_h = target_h / img_h
    scale = min(scale_w, scale_h)
    
    new_w = int(img_w * scale)
    new_h = int(img_h * scale)
    
    resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    
    result = Image.new('L', target_size, 255)
    offset_x = (target_w - new_w) // 2
    offset_y = (target_h - new_h) // 2
    result.paste(resized, (offset_x, offset_y))
    
    return result


def image_to_mif(image_path, output_path, invert_color=False):
    img = Image.open(image_path)
    img = img.convert('L')
    img = resize_keep_aspect(img, (128, 64))
    img = img.point(threshold_transform, '1')
    
    pixels = img.load()
    if pixels is None:
        raise RuntimeError("Failed to load pixel data from image")
    
    data = []
    
    print(f"Processing image: {image_path}")
    print(f"Size: {img.size}")
    print("Storage mode: 8-column vertical strip (bit-reversed: bit7=left, bit0=right)")
    
    for tile_x in range(16):
        base_x = tile_x * 8
        
        for row in range(64):
            byte_val = 0
            y = row
            
            for col in range(8):
                x = base_x + col
                
                pixel = pixels[x, y]
                if pixel is None:
                    pixel = 255
                
                is_black = (pixel == 0)
                
                if invert_color:
                    is_black = not is_black
                
                if is_black:
                    byte_val |= (1 << (7 - col))
            
            data.append(byte_val)
    
    depth = len(data)
    print(f"\nGenerating MIF file: {output_path}")
    print(f"Total depth: {depth} bytes (0x{depth:04X})")
    
    with open(output_path, 'w') as f:
        f.write("-- GraphicLCD 8-Column Vertical Strip MIF File\n")
        f.write(f"-- Source image: {os.path.basename(image_path)}\n")
        f.write("-- Format: 128x64 pixels, 8-column vertical strip mode\n")
        f.write("-- Each strip: 64 bytes (64 rows, each row = 8 horizontal pixels)\n")
        f.write("-- Bit7 = leftmost pixel in row, Bit0 = rightmost pixel in row\n")
        f.write("-- Address order: 8-column strip (left-to-right), then row top-to-bottom\n\n")
        
        f.write("WIDTH=8;\n")
        f.write(f"DEPTH={depth};\n\n")
        f.write("ADDRESS_RADIX=HEX;\n")
        f.write("DATA_RADIX=HEX;\n\n")
        f.write("CONTENT BEGIN\n")
        
        for i, val in enumerate(data):
            f.write(f"    {i:04X} : {val:02X};\n")
        
        f.write("END;\n")
    
    print("MIF generation done!")
    return output_path


def generate_test_pattern(output_path, pattern='checker'):
    print(f"Generating test pattern: {pattern}")
    
    data = []
    
    for tile_x in range(16):
        base_x = tile_x * 8
        
        for row in range(64):
            byte_val = 0
            y = row
            
            for col in range(8):
                x = base_x + col
                
                if pattern == 'checker':
                    is_black = ((x // 8) + (y // 8)) % 2 == 0
                elif pattern == 'vertical':
                    is_black = (x % 2) == 0
                elif pattern == 'horizontal':
                    is_black = (y % 2) == 0
                elif pattern == 'gradient':
                    is_black = x < y
                else:
                    is_black = False
                
                if is_black:
                    byte_val |= (1 << (7 - col))
            
            data.append(byte_val)
    
    with open(output_path, 'w') as f:
        f.write("WIDTH=8;\n")
        f.write(f"DEPTH={len(data)};\n")
        f.write("ADDRESS_RADIX=HEX;\n")
        f.write("DATA_RADIX=HEX;\n")
        f.write("CONTENT BEGIN\n")
        for i, val in enumerate(data):
            f.write(f"    {i:04X} : {val:02X};\n")
        f.write("END;\n")
    
    print(f"Test pattern saved: {output_path}")
    return output_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Convert image to GraphicLCD MIF file with preview')
    parser.add_argument('input', nargs='?', help='Input image path (PNG/JPG/BMP)')
    parser.add_argument('-o', '--output', help='Output MIF filename (default: input name with .mif extension)')
    parser.add_argument('-p', '--preview', help='Generate preview image from MIF (specify output PNG path)')
    parser.add_argument('-i', '--invert', action='store_true', help='Invert color (black to white)')
    parser.add_argument('-t', '--test', choices=['checker', 'vertical', 'horizontal', 'gradient'], 
                       help='Generate test pattern instead of converting from image')
    
    args = parser.parse_args()
    
    if args.test:
        mif_path = args.output if args.output else f'{args.test}.mif'
        generate_test_pattern(mif_path, args.test)
        
        if args.preview:
            generate_preview_from_mif(mif_path, args.preview)
    elif args.input:
        mif_path = args.output if args.output else args.input.rsplit('.', 1)[0] + '.mif'
        image_to_mif(args.input, mif_path, args.invert)
        
        if args.preview:
            print()
            generate_preview_from_mif(mif_path, args.preview)
    else:
        parser.print_help()
        print("\nExamples:")
        print("  python img2mif.py image.png                    # Generate MIF only")
        print("  python img2mif.py image.png -o out.mif         # Specify output name")
        print("  python img2mif.py image.png -p preview.png     # Generate MIF + preview from MIF")
        print("  python img2mif.py -t checker -o test.mif -p preview.png")
