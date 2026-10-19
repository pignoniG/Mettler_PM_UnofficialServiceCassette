#!/usr/bin/env python3
"""
Apply the cassette patches to YOUR OWN dump of the Mettler V10.45 standard program cassette.

    python3 build.py V10.45.bin OUT.bin --comparator --temperature --standby-weigh --skip-startup

The input is the 32 KiB image of the 27C256 (ME-34172 "Standard" cassette, version V10.45).  This
repository does not contain it.  Every patch checks the exact bytes it replaces and refuses to apply
to anything else, fixes the ROM checksum, and refuses to touch space that is not free filler.  The
patches can be combined; they are applied in the order below and use separate code and RAM areas
(see docs/PATCHES.md).  Never publish or share OUT.bin: it contains Mettler's firmware.
"""
import argparse, hashlib, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, 'patches'))
import rom_comparator, rom_temperature, rom_standby_weigh

# sha256 of the unmodified V10.45 image this was developed on (a hint only; the patches check the bytes)
STOCK_SHA256 = '265470d391c798148a564b95cb7e698ab1d69b5bcd04660808e7ac10699c4778'


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('rom'); ap.add_argument('out')
    ap.add_argument('--comparator', action='store_true',
                    help='long A/D averaging at normal display speed (mass-comparator style)')
    ap.add_argument('--table', default=','.join(f'{k}x{n}' for k, n in rom_comparator.DEFAULT_TABLE),
                    help='comparator averaging per vibration-adapter setting 0..3: KxN (see docs/PATCHES.md)')
    ap.add_argument('--temperature', action='store_true', help='send "T nnnnnn" (temperature count) lines')
    ap.add_argument('--every', type=int, default=62, help='temperature line every N passes (~0.16 s each)')
    ap.add_argument('--standby-weigh', action='store_true', help='keep weighing and the serial output on with the display off')
    ap.add_argument('--skip-startup', action='store_true', help='with --standby-weigh: wake without start-up sequence and tare')
    a = ap.parse_args()
    img = open(a.rom, 'rb').read()
    h = hashlib.sha256(img).hexdigest()
    print('input sha256', h, '(matches the image this was developed on)' if h == STOCK_SHA256 else
          '(differs from the development image: the patches still check every byte they replace)')
    if a.skip_startup and not a.standby_weigh:
        sys.exit('--skip-startup needs --standby-weigh')
    if a.comparator:
        table = tuple(tuple(int(v) for v in e.split('x')) for e in a.table.split(','))
        img, lab, code, ru = rom_comparator.build(img, table)
        print(f'comparator: code {rom_comparator.CODE_AT:04X}+{len(code)} bytes, table {a.table}')
    if a.temperature:
        img, code = rom_temperature.build(img, a.every)
        print(f'temperature: code {rom_temperature.CODE_AT:04X}+{len(code)} bytes, a T line every {a.every} passes')
    if a.standby_weigh:
        img, code = rom_standby_weigh.build(img, False, a.skip_startup)
        print(f'standby-weigh: code {rom_standby_weigh.CODE_AT:04X}+{len(code)} bytes' + (' with --skip-startup' if a.skip_startup else ''))
    if sum(img) & 0xFF:
        sys.exit('internal error: ROM checksum is wrong')
    open(a.out, 'wb').write(img)
    print('written', a.out, '(ROM checksum OK)')


if __name__ == '__main__':
    main()
