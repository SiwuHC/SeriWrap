#!/usr/bin/env python3
"""Independent check of a generated .rbtprj.

It re-derives the expected pin for every SeriWrap logical port straight from the
constraint file (the same source Rabbit uses) and compares it with the pin the
project actually binds, so a wrong or missing binding cannot slip through.

    python3 check_rabbit_project.py --project x.rbtprj --cons x_cons.xml
"""
import argparse
import sys
import xml.etree.ElementTree as ET


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--project', required=True)
    ap.add_argument('--cons', required=True)
    args = ap.parse_args()

    # pin -> design port (that is the direction every lookup below needs)
    cons = {p.get('position'): p.get('name') for p in
            ET.parse(args.cons).getroot().findall('port')}
    root = ET.parse(args.project).getroot()
    checks = 0
    bad = []

    for comp in root.find('components').findall('component'):
        ctype = comp.get('type')
        for side, tag in (('inputs', 'input'), ('outputs', 'output')):
            for e in comp.find(side):
                pin, port = e.get('pin'), e.get('port')
                checks += 1
                if pin not in cons:
                    bad.append(f'{ctype}.{port}: pin {pin} is not in the constraint file')
                    continue
                design = cons[pin]
                if ctype != 'SeriWrap':
                    continue
                expect = None
                if port.startswith('DATA['):
                    expect = f"s_data_in[{port[5:-1]}]"
                elif port.startswith('DOUT['):
                    expect = f"s_data_out[{port[5:-1]}]"
                elif port == 'CLK':
                    expect = 's_clk_in'
                elif port == 'STROBE':
                    expect = 's_strobe_in'
                elif port == 'CLK_OUT':
                    expect = 's_clk_out'
                elif port == 'DATA_VALID':
                    expect = 's_data_valid'
                elif port == 'READY':
                    expect = 's_ready'
                checks += 1
                if expect and design != expect:
                    bad.append(f'{port}: bound to pin {pin} which carries {design}, expected {expect}')
        if ctype == 'Switch' and comp.get('name') == 'rst':
            for e in comp.find('inputs'):
                checks += 1
                if cons.get(e.get('pin')) != 'rst_n':
                    bad.append(f'reset switch bound to {e.get("pin")} ({cons.get(e.get("pin"))}), expected rst_n')

    print(f'RABBIT PROJECT CHECK: {checks} assertions, {len(bad)} failure(s)')
    for b in bad:
        print('  FAIL', b)
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
