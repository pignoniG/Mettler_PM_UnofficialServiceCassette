#!/usr/bin/env python3
"""
V10.45 cassette ROM: send the balance's temperature count over the serial interface.

The balance measures a temperature count continuously for its temperature compensation
(the number the service keypad shows as "297904 T").  The routine at C887 (called once per
weighing pass from 827B) low-pass filters it into XRAM 7047..7049 (24 bit, little endian);
the compensation reads it from there.

This patch hooks that call.  In continuous-output mode (I-FACE "S" = cONT) and when the
interface is free, it sends one extra line every --every passes:

    T 297904<CR><LF>

through the firmware's own character routine D6FC, so the line ending, pause bytes and parity
follow the balance's interface settings.  The weight lines are unchanged.  Other modes
(stable only / all / auto) send nothing extra.

Goes at E800 (free filler) and can be applied on top of any V10.45-based image, e.g. the
comparator ROM.  Counter at XRAM 70E8, register save area 70E9..70ED.

Usage:  rom_temperature.py IN.BIN OUT.BIN [--every N] [--version 10.45|10.50]
"""
import argparse
import asm51
import profiles

CODE_AT = 0xE800
CODE_AT_BIG = 0xE730
CHECKSUM_AT = 0xF7FF
TEMP = 0x7047                                              # 24 bit filtered temperature count
CNT, SAVE, RCNT = 0x70E8, 0x70E9, 0x70ED
PSW = 0xD0


def encode(m, ops, pc, labels):
    o = list(ops)
    def rel(t, size):
        if labels is None: return 0
        d = labels[t] - (pc + size)
        if not -128 <= d <= 127: raise ValueError(f"{m} to {t} out of range at {pc:04X}")
        return d & 0xFF
    if m == 'LCALL':
        t = o[0] if isinstance(o[0], int) else (0 if labels is None else labels[o[0]])
        return [0x12, t >> 8, t & 0xFF]
    if m == 'LJMP' and isinstance(o[0], str): return asm51.encode(m, ops, pc, labels)
    if m in ('JB', 'JNB'): return [0x20 if m == 'JB' else 0x30, o[0], rel(o[1], 3)]
    if m == 'LBL': return []                               # label only, no code
    if m == 'SWAP': return [0xC4]
    if m == 'DA': return [0xD4]
    if m == 'PUSH': return [0xC0, o[0]]
    if m == 'POP': return [0xD0, o[0]]
    if m == 'SJMP' and False: pass
    return asm51.encode(m, ops, pc, labels)


def assemble(org, prog):
    labels, pc = {}, org
    for lab, m, *ops in prog:
        if lab: labels[lab] = pc
        pc += len(encode(m, ops, pc, None))
    code, pc = [], org
    for lab, m, *ops in prog:
        b = encode(m, ops, pc, labels); code += b; pc += len(b)
    return bytes(code), labels


def program(every, putc, temp_routine, raw_every=0, window_reg=0x32, raw_xram=0x749A):
    """every: T line every N passes (0 = none); raw_every: R line every N passes (0 = none)"""
    P = []
    def I(*ins): P.append((None,) + ins)
    def L(label, *ins): P.append((label,) + ins)
    def dptr(v): I('MOV', 'DPTR', '#%d' % v)
    def put(ch): I('MOV', 'A', '#%d' % ch); I('LCALL', putc)
    def hexbyte_x(addr): dptr(addr); I('MOVX', 'A', '@DPTR'); I('LCALL', 'PUTHEX')
    def hexbyte_r(addr): I('MOV', 'A', addr); I('LCALL', 'PUTHEX')

    I('LCALL', temp_routine)                                # what the original call did
    dptr(0x7601); I('MOVX', 'A', '@DPTR'); I('ANL', 'A', '#192'); I('CJNE', 'A', '#192', 'DONE')   # cONT only
    for bit in (0x38, 0x7D, 0x78, 0x4B):                    # 27h.0 2Fh.5 2Fh.0 29h.3: output off / busy
        I('JB', bit, 'DONE')
    I('MOV', 'A', 0x35); I('CLR', 'C'); I('SUBB', 'A', '#64'); I('JNC', 'DONE')   # output buffer nearly full
    I('LJMP', 'BODY')
    L('DONE', 'RET')
    L('BODY', 'PUSH', 0xF0); I('PUSH', PSW)                         # B, PSW
    dptr(SAVE)
    for r in ('R0', 'R1', 'R2', 'R3'):
        I('MOV', 'A', r); I('MOVX', '@DPTR', 'A'); I('INC', 'DPTR')
    if raw_every:                                           # R line every raw_every passes
        dptr(RCNT); I('MOVX', 'A', '@DPTR'); I('INC', 'A'); I('MOVX', '@DPTR', 'A'); I('CJNE', 'A', '#%d' % raw_every, 'NOTRAW')
        I('CLR', 'A'); I('MOVX', '@DPTR', 'A'); I('LJMP', 'DORAW')
    L('NOTRAW', 'LBL'); L('AFTERRAW', 'LBL')
    if every:                                               # T line every N passes
        dptr(CNT); I('MOVX', 'A', '@DPTR'); I('INC', 'A'); I('MOVX', '@DPTR', 'A'); I('CJNE', 'A', '#%d' % every, 'NOTT')
        I('CLR', 'A'); I('MOVX', '@DPTR', 'A'); I('LJMP', 'DOT')
    L('NOTT', 'LJMP', 'EXIT')
    L('DOT', 'LBL')
    dptr(TEMP)
    I('MOVX', 'A', '@DPTR'); I('MOV', 'R1', 'A'); I('INC', 'DPTR')
    I('MOVX', 'A', '@DPTR'); I('MOV', 'R2', 'A'); I('INC', 'DPTR')
    I('MOVX', 'A', '@DPTR'); I('MOV', 'R3', 'A')
    put(ord('T')); put(0x20)
    for k in (100000, 10000, 1000, 100, 10, 1):
        kl, km, kh = k & 255, (k >> 8) & 255, k >> 16
        tag = 'D%d' % k
        I('MOV', 'R0', '#0')
        L(tag, 'CLR', 'C')
        I('MOV', 'A', 'R1'); I('SUBB', 'A', '#%d' % kl); I('MOV', 'R1', 'A')
        I('MOV', 'A', 'R2'); I('SUBB', 'A', '#%d' % km); I('MOV', 'R2', 'A')
        I('MOV', 'A', 'R3'); I('SUBB', 'A', '#%d' % kh); I('MOV', 'R3', 'A')
        I('JC', 'B%d' % k); I('INC', 'R0'); I('SJMP', tag)
        L('B%d' % k, 'MOV', 'A', 'R1'); I('ADD', 'A', '#%d' % kl); I('MOV', 'R1', 'A')
        I('MOV', 'A', 'R2'); I('ADDC', 'A', '#%d' % km); I('MOV', 'R2', 'A')
        I('MOV', 'A', 'R3'); I('ADDC', 'A', '#%d' % kh); I('MOV', 'R3', 'A')
        I('MOV', 'A', 'R0'); I('ADD', 'A', '#48'); I('LCALL', putc)
    put(0x0D); put(0x0A)
    I('LJMP', 'EXIT')
    if raw_every:
        # R wwwwwwww rrrrrr tttttt ffffff nn : window sum (7045..7042), raw value after the normaliser (749C..749A),
        # unfiltered temperature (16h 15h 14h), filtered temperature (7049..7047), window length (32h); hex
        L('DORAW', 'LBL')
        put(ord('R')); put(0x20)
        for a in (0x7045, 0x7044, 0x7043, 0x7042): hexbyte_x(a)
        put(0x20)
        for a in (raw_xram + 2, raw_xram + 1, raw_xram): hexbyte_x(a)
        put(0x20)
        for a in (0x16, 0x15, 0x14): hexbyte_r(a)
        put(0x20)
        for a in (TEMP + 2, TEMP + 1, TEMP): hexbyte_x(a)
        put(0x20)
        hexbyte_r(window_reg)
        put(0x0D); put(0x0A)
        I('LJMP', 'AFTERRAW')
    L('EXIT', 'LBL')
    dptr(SAVE)
    for r in ('R0', 'R1', 'R2', 'R3'):
        I('MOVX', 'A', '@DPTR'); I('MOV', r, 'A'); I('INC', 'DPTR')
    I('POP', PSW); I('POP', 0xF0)
    I('RET')
    # hex byte in A -> two ASCII characters through the firmware's output routine
    L('PUTHEX', 'PUSH', 0xE0); I('SWAP', 'A'); I('ANL', 'A', '#15'); I('ADD', 'A', '#144'); I('DA'); I('ADDC', 'A', '#64'); I('DA')
    I('LCALL', putc); I('POP', 0xE0); I('ANL', 'A', '#15'); I('ADD', 'A', '#144'); I('DA'); I('ADDC', 'A', '#64'); I('DA')
    I('LCALL', putc); I('RET')
    return P


def build(img, every=None, profile=None, raw=False, raw_every=None):
    """every: a T line every N passes, 0 for none; the default is about 10 s (62 passes of 0.16 s, 75 of 0.133 s).
    raw: also an R line every raw_every passes (default 1).  Returns (image, code, address of the code)."""
    img = bytearray(img)
    if len(img) != 0x8000: raise ValueError("expected a 32 KiB ROM image")
    if sum(img) & 0xFF: raise ValueError("ROM checksum of the input is already wrong")
    P = profiles.get(bytes(img), profile)
    if every is None: every = round(10 * 150 / P['window'])
    if raw and raw_every is None: raw_every = 1
    hook = P['temp_call']
    if img[hook - 0x8000:hook - 0x8000 + 3] != P['temp_call_old']:
        raise ValueError(f"unexpected bytes at {hook:04X}: not the stock image of this version")
    prog = program(every, P['putc'], P['temp_routine'], raw_every or 0)
    code_at = CODE_AT                                          # E800 for the T line alone: E800..E97F
    code, lab = assemble(code_at, prog)
    if len(code) > 0x180:                                      # the R line needs more room: start earlier, E730..E97F
        code_at = CODE_AT_BIG
        code, lab = assemble(code_at, prog)
    if code_at + len(code) > 0xE980:
        raise ValueError("code does not fit before the standby-weigh patch at E980")
    o = code_at - 0x8000
    if any(b != 0xFF for b in img[o:o + len(code)]):
        raise ValueError("patch area is not free filler")
    img[o:o + len(code)] = code
    img[hook - 0x8000:hook - 0x8000 + 3] = bytes([0x12, code_at >> 8, code_at & 0xFF])
    img[CHECKSUM_AT - 0x8000] = 0
    img[CHECKSUM_AT - 0x8000] = (-sum(img)) & 0xFF
    assert sum(img) & 0xFF == 0
    return bytes(img), code, code_at


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('inp'); p.add_argument('out')
    p.add_argument('--every', type=int, help='send a T line every N weighing passes (default: about every 10 s; 0 = none)')
    p.add_argument('--raw', action='store_true', help='also send an R line with the raw A/D and temperature counts')
    p.add_argument('--raw-every', type=int, help='R line every N passes (default 1)')
    p.add_argument('--version', choices=sorted(profiles.PROFILES), help='firmware version (default: detect)')
    a = p.parse_args()
    assert a.every is None or 0 <= a.every <= 255
    out, code, at = build(open(a.inp, 'rb').read(), a.every, a.version, a.raw, a.raw_every)
    open(a.out, 'wb').write(out)
    print(f"patch {at:04X}-{at + len(code) - 1:04X} ({len(code)} bytes)")
    print(f"written {a.out} (ROM checksum OK)")


if __name__ == '__main__':
    main()
