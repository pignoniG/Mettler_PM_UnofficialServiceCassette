#!/usr/bin/env python3
"""
Check the correction model against the real firmware running in the simulator.

The balance turns the raw A/D value and the temperature count into a weight with the cell parameters of
the EEPROM (zero, span and linearity, each a quadratic in temperature).  The service procedures of the
planned PC tool solve that model for the parameters, so it has to be right.  This script feeds a set of loads
and a temperature offset to the simulated firmware, reads the R lines (raw value, temperature counts) and the
displayed weights, and compares the displayed weight differences with the model's prediction.

    PM_ROM=V10.45.bin PM_EEPROM=dump.BIN python3 tools/model_check.py [--dt 5000]

Needs pm_eeprom.py of Mettler_PM_UnofficialServiceTool (env PM_TOOL = its tools folder, or a sibling folder).
"""
import argparse, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path[:0] = [os.path.join(ROOT, 'sim'), os.path.join(ROOT, 'patches')]
import fwsim, profiles, rom_temperature


def find_pm_eeprom():
    cands = [os.environ.get('PM_TOOL'), os.path.join(ROOT, '..', 'Mettler_PM_UnofficialServiceTool', 'tools'),
             os.path.join(ROOT, '..', 'Mettler PM Service', 'tools')]
    for c in cands:
        if c and os.path.exists(os.path.join(c, 'pm_eeprom.py')):
            sys.path.insert(0, c); import pm_eeprom; return pm_eeprom
    sys.exit('pm_eeprom.py not found: set PM_TOOL to the tools folder of Mettler_PM_UnofficialServiceTool')


def model_weight(x, g24, raw, temp):
    """internal weight units from raw and the filtered temperature count, formula of EEPROM_MAP.md section 3"""
    t = temp - g24(x, 0x46)
    poly = lambda a, b, c: g24(x, a) + ((g24(x, b) + ((g24(x, c) * t) >> 16)) * t >> 16)
    Z, S, L = poly(0x4C, 0x4F, 0x52), poly(0x55, 0x58, 0x5B), poly(0x5E, 0x61, 0x64)
    xr = raw - g24(x, 0x40)
    W = raw + Z + (((S + ((L * xr) >> 24)) * xr) >> 24)
    return W * (g24(x, 0x2E) + g24(x, 0x32)) >> 23


def run(rom, eeprom_file, dt=5000, loads=(0, 10, 100, 500, 1000, 2000, 3000, 4000), raw_per_g=1548, raw0=1000000, version=None):
    """-> list of (load g, displayed change mg, model change mg) against the first load"""
    pm = find_pm_eeprom()
    rom = rom_temperature.build(rom, 0, version, True, 1)[0]                    # R lines on
    x = bytearray(pm.load(eeprom_file)[0]); x[0x01] |= 0xC0
    s = fwsim.Sim(rom, bytes(x)); s.boot()
    temp = pm.g24(x, 0x46) + dt
    RL = re.compile(rb'^R ([0-9A-F]{8}) ([0-9A-F]{6})(?: ([0-9A-F]{6}) ([0-9A-F]{6}) ([0-9A-F]{2}))?$')    # the long form every ~1 s
    WL = re.compile(rb'^S +(-?\d+\.\d+) g$')
    for _ in range(130): s.one_pass(s_value=s.window_sum(raw0), sw=0x80, temp=temp); s.sent.clear()
    rows = []
    for g in loads:
        last_r = last_w = None; tfilt = None
        for _ in range(70):
            s.one_pass(s_value=s.window_sum(raw0 + raw_per_g * g), sw=0x80, temp=temp)
            for l in bytes(s.sent).replace(b'\x26', b'').replace(b'\x00', b'').split(b'\r\n'):
                m = RL.match(l)
                if m:
                    last_r = [int(m.group(1), 16), int(m.group(2), 16)]
                    if m.group(4): tfilt = int(m.group(4), 16)
                m = WL.match(l)
                if m: last_w = float(m.group(1))
            s.sent.clear()
        rows.append((g, [last_r[0], last_r[1], None, tfilt], last_w))
    base = model_weight(x, pm.g24, rows[0][1][1], rows[0][1][3])
    return [(g, r[1], r[3], (w - rows[0][2]) * 1000 if w is not None else None,
             model_weight(x, pm.g24, r[1], r[3]) - base) for g, r, w in rows]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dt', type=int, default=5000, help='temperature offset from T0 in counts (about 2000 per degC)')
    ap.add_argument('--loads', default='0,10,100,500,1000,2000,3000,4000', help='loads in g')
    ap.add_argument('--raw-per-g', type=float, default=1548)
    ap.add_argument('--raw0', type=int, default=1000000, help='raw value of the empty pan (the firmware reports overload when the gross value is too large)')
    a = ap.parse_args()
    rows = run(open(os.environ['PM_ROM'], 'rb').read(), os.environ['PM_EEPROM'], a.dt,
               [float(v) for v in a.loads.split(',')], a.raw_per_g, a.raw0)
    print(f"{'load g':>8s} {'raw':>9s} {'T filt':>8s} {'displayed mg':>13s} {'model mg':>10s} {'diff mg':>8s}")
    worst = 0
    for g, raw, tf, shown, pred in rows:
        if shown is None: print(f'{g:8.1f} {raw:9d} {tf:8d}  no weight shown (overload?)'); continue
        d = shown - pred; worst = max(worst, abs(d))
        print(f'{g:8.1f} {raw:9d} {tf:8d} {shown:13.1f} {pred:10.1f} {d:8.1f}')
    print(f'largest difference between the displayed weight and the model: {worst:.1f} mg')


if __name__ == '__main__':
    main()
