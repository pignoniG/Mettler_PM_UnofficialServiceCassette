#!/usr/bin/env python3
"""
V10.45 patch: keep weighing and the serial output running while the display is off.

On a real balance "standby" is the control bar held up (switch 70D1.6 high).  The stock main loop then
repeats the stage-1 standby code (831A -> 8395) on every pass and never weighs, and it switches the
interface off (flag 27h.0).  This patch:

  1. Hook at 831A (LJMP 8395): marks state 3, clears 27h.0 and lets the stage-1 code run; its
     SETB 27h.0 at 83AE is replaced by a NOP.
  2. Hook at 8876, the shared end of every pass: in state 3 it runs the normal weighing path
     (8314 -> 8419 ...) as state 2, with the key-event byte 2Eh hidden from it (saved, cleared,
     OR-merged back) and the standby poll's own variables (25h 26h 64h 65h 66h) saved and restored;
     when that reaches 8876 again it blanks the display as standby entry does (717A = 0, CD93(0),
     BB2B(40h)) and runs the normal tail.
  3. The bar-down poll state (29h.6, entered after the bar goes down again: wake-up or the
     configuration entry) is left exactly as in the stock ROM, because the weighing path has its
     own bar menu (calibration, vibration adapter) which would open next to the configuration.
     --poll-weigh adds weighing there too (not recommended).

--skip-startup additionally changes the wake-up: the first wake after power-on keeps the start-up
(display test, version number, switch-on tare); later wakes skip it.  Hook at 836E; hook at 8314
drops the tare-switch event (2Eh.0) for INHIB_PASSES passes after a skipped start-up, so the press
that woke the balance is not taken as a tare; the start-up's own tare request (22h.1) is not set.

Memory: code at E980 (free filler); XRAM 70F0 state, 70F1..70F5 saved poll state, 70F6 saved key
events, 70F7 first-wake flag, 70F8 inhibit counter.

Usage:  rom_standby_weigh.py IN.BIN OUT.BIN [--skip-startup] [--poll-weigh] [--diag | --diag2] [--version 10.45|10.50]
--diag   markers in standby: 'P' before the weighing part and 'W' after it through the firmware output
         routine, and 'D' written straight to the UART (to see which part of the serial path is closed)
--diag2  also a status line 'Q 29 20 2F 27 D1 F0' (hex flags and switch input) at the top of every tail
"""
import sys
import asm51
import profiles
import rom_temperature as rt

CODE_AT = 0xE980
FLAG = 0x70F0
INHIB = 0x70F8                       # passes left in which the tare switch is ignored after a skipped start-up
INHIB_PASSES = 20                    # about 3 s: covers the press that woke the balance
FLAG3 = 0x70F7                       # set after the first wake-up since power-on
CHECKSUM_AT = 0xF7FF
POLL_STATE = (0x25, 0x26, 0x64, 0x65, 0x66)   # internal RAM the standby poll keeps between passes
# the places patched and the stock bytes expected there come from profiles.py (poll, bar_up, tail, stage1_setb, wake, normal)


def encode(m, ops, pc, labels):
    o = list(ops)
    if m == 'LJMP' and isinstance(o[0], int): return [0x02, o[0] >> 8, o[0] & 0xFF]
    if m == 'CLRBIT': return [0xC2, o[0]]
    if m == 'SETBIT': return [0xD2, o[0]]
    if m == 'JNZ' and False: pass
    if m == 'ORLDA': return [0x42, o[0]]
    if m == 'DA': return [0xD4]
    if m == 'DEC' and o[0] == 'A': return [0x14]
    if m == 'SWAP': return [0xC4]
    if m == 'LCALLL': return [0x12, 0, 0] if labels is None else [0x12, labels[o[0]] >> 8, labels[o[0]] & 0xFF]
    if m == 'JNB' and isinstance(o[1], str):
        if labels is None: return [0x30, o[0], 0]
        d = labels[o[1]] - (pc + 3)
        if not -128 <= d <= 127: raise ValueError('JNB out of range')
        return [0x30, o[0], d & 0xFF]
    return rt.encode(m, ops, pc, labels)


def assemble(org, prog):
    labels, pc = {}, org
    for lab, m, *ops in prog:
        if lab: labels[lab] = pc
        pc += len(encode(m, ops, pc, None))
    code, pc = [], org
    for lab, m, *ops in prog:
        b = encode(m, ops, pc, labels); code += b; pc += len(b)
    return bytes(code), labels


def program(P, diag=False, skip_startup=False, poll_weigh=False):
    prog = []
    def I(*ins): prog.append((None,) + ins)
    def L(label, *ins): prog.append((label,) + ins)
    # ---- replaces LJMP 8395 at 831A: the bar is up (display off).  The bar stays up in standby, so this
    #      stage-1 code runs on every pass.  Mark state 3 and run it; its end reaches 8876 -> TAIL.
    L('HOOK1', 'JB', 0x7F, 'ST1')                            # 2Fh.7 set: leave it to the original code
    I('CLRBIT', 0x38)                                        # interface on while the bar is up (stage 2 / the poll set it again, as stock)
    I('MOV', 'DPTR', '#%d' % FLAG); I('MOV', 'A', '#3'); I('MOVX', '@DPTR', 'A')
    L('ST1', 'LJMP', P['stage1'])
    # ---- replaces JB 29h.6,8329 at 830E: standby poll (bar down again) -> poll first (state 1)
    L('HOOK2', 'JNB', 0x4E, 'ORIG')                          # not standby: the original next test
    I('MOV', 'DPTR', '#%d' % FLAG); I('MOV', 'A', '#1'); I('MOVX', '@DPTR', 'A')
    I('LJMP', P['poll_jump'])                                        # the standby poll sees bar events first
    L('ORIG', 'LJMP', P['poll_next'])
    # ---- replaces LCALL C464 ... at 8876 (every path ends here)
    if diag == 2:                                            # status line at the top of every tail: Q 29 20 2F 27 D1 F0
        L('TAIL', 'MOV', 'A', '#81'); I('LCALL', P['putc'])
        for kind, addr in (('r', 0x29), ('r', 0x20), ('r', 0x2F), ('r', 0x27), ('x', 0x70D1), ('x', FLAG)):
            I('MOV', 'A', '#32'); I('LCALL', P['putc'])
            if kind == 'r': I('MOV', 'A', addr)
            else: I('MOV', 'DPTR', '#%d' % addr); I('MOVX', 'A', '@DPTR')
            I('LCALLL', 'PUTHEX')
        I('MOV', 'A', '#13'); I('LCALL', P['putc']); I('MOV', 'A', '#10'); I('LCALL', P['putc'])
        I('MOV', 'DPTR', '#%d' % FLAG); I('MOVX', 'A', '@DPTR'); I('JZ', 'NORM')
    else:
        L('TAIL', 'MOV', 'DPTR', '#%d' % FLAG); I('MOVX', 'A', '@DPTR'); I('JZ', 'NORM')
    I('CJNE', 'A', '#2', 'NOT2')
    I('LJMP', 'WDONE')
    L('NOT2', 'CJNE', 'A', '#1', 'DOW')                      # state 3 (bar up) goes straight to weighing
    I('JNB', 0x4E, 'CLEAR')                                  # state 1: the poll woke the balance -> normal tail
    L('DOW', 'MOV', 'A', '#2'); I('MOVX', '@DPTR', 'A')
    if diag:
        I('MOV', 'A', '#80'); I('LCALL', P['putc'])             # 'P' = about to weigh in standby
        I('MOV', 'A', '#13'); I('LCALL', P['putc'])
        I('MOV', 'A', '#10'); I('LCALL', P['putc'])
        I('MOV', 'DPTR', '#%d' % FLAG)
    for reg in POLL_STATE:                                    # keep the poll's state: the weighing path uses
        I('INC', 'DPTR'); I('MOV', 'A', reg); I('MOVX', '@DPTR', 'A')   # 25h/26h as flags and 65h as the filter depth
    I('INC', 'DPTR'); I('MOV', 'A', 0x2E); I('MOVX', '@DPTR', 'A')           # key events (2Eh: bit 0 = tare switch pressed):
    I('MOV', 0x2E, '#0')                                      # the weighing path must not see them while the display is off
    I('MOV', 0x25, '#0'); I('MOV', 0x26, '#0')
    I('LJMP', P['normal'])
    # the weighing part is done -> blank the display as standby entry does
    L('WDONE', 'MOV', 'DPTR', '#%d' % (FLAG + 1))
    for k, reg in enumerate(POLL_STATE):
        if k: I('INC', 'DPTR')
        I('MOVX', 'A', '@DPTR'); I('MOV', reg, 'A')
    I('INC', 'DPTR'); I('MOVX', 'A', '@DPTR'); I('ORLDA', 0x2E)  # give the key events back (OR: new edges from the interrupt stay)
    I('MOV', 'DPTR', '#%d' % 0x717A); I('CLR', 'A'); I('MOVX', '@DPTR', 'A')
    I('LCALL', P['cd93'])                                       # A = 0
    I('MOV', 'A', '#64'); I('LCALL', P['bb2b'])
    if diag:
        I('MOV', 'A', '#87'); I('LCALL', P['putc'])             # 'W' = weighing part done
        I('MOV', 'A', '#13'); I('LCALL', P['putc'])
        I('MOV', 'A', '#10'); I('LCALL', P['putc'])
    L('CLEAR', 'MOV', 'DPTR', '#%d' % FLAG); I('CLR', 'A'); I('MOVX', '@DPTR', 'A')
    L('NORM', 'LCALL', P['flush']); I('CLRBIT', 0x0A); I('LJMP', P['tail_cont'])
    if skip_startup:
        # replaces SETB 29h.5 / CLR 20h.3 / LJMP 8416 at 836E (end of the wake-up init, where the start-up sequence is
        # armed).  The first wake after power-on keeps the start-up; later wakes do what its last pass does instead, without the zero setting (22h.1) that triggers the tare.
        L('HOOK3', 'MOV', 'DPTR', '#%d' % FLAG3); I('MOVX', 'A', '@DPTR'); I('JNZ', 'FAST')
        I('MOV', 'A', '#1'); I('MOVX', '@DPTR', 'A')
        I('SETBIT', 0x4D); I('CLRBIT', 0x03); I('LJMP', P['wake_exit'])
        L('FAST', 'MOV', 'DPTR', '#%d' % INHIB); I('MOV', 'A', '#%d' % INHIB_PASSES); I('MOVX', '@DPTR', 'A')
        I('LCALL', P['start_init'])                                   # end-of-start-up init (7300, 7311, 21h.7 ...)
        I('CLRBIT', 0x70); I('CLRBIT', 0x0E)                 # 2Eh.0 and 21h.6 clear, as the start-up branch does (22h.1, the
                                                             # switch-on zero request it also sets, is deliberately left alone)
        I('CLRBIT', 0x03); I('LJMP', P['wake_exit'])
    if skip_startup:
        # replaces LCALL B1A3 at 8314 (start of the normal weighing pass): for a short while after a skipped start-up
        # the tare switch event (2Eh.0) is dropped, so the press that woke the balance is not taken as a tare
        L('HOOK5', 'MOV', 'DPTR', '#%d' % INHIB); I('MOVX', 'A', '@DPTR'); I('JZ', 'H5END')
        I('DEC', 'A'); I('MOVX', '@DPTR', 'A'); I('CLRBIT', 0x70)
        L('H5END', 'LJMP', P['weigh'])
    if diag == 2:
        L('PUTHEX', 'PUSH', 0xE0); I('SWAP', 'A'); I('ANL', 'A', '#15'); I('ADD', 'A', '#144'); I('DA'); I('ADDC', 'A', '#64'); I('DA')
        I('LCALL', P['putc']); I('POP', 0xE0); I('ANL', 'A', '#15'); I('ADD', 'A', '#144'); I('DA'); I('ADDC', 'A', '#64'); I('DA')
        I('LCALL', P['putc']); I('RET')
    return prog


def build(img, diag=False, skip_startup=False, poll_weigh=False, profile=None):
    img = bytearray(img)
    if len(img) != 0x8000 or sum(img) & 0xFF: raise ValueError("expected a 32 KiB image with a good checksum")
    P = profiles.get(bytes(img), profile)
    patches = {P['bar_up']: P['bar_up_old'], P['tail']: P['tail_old'], P['stage1_setb']: bytes([0xD2, 0x38])}
    if poll_weigh: patches[P['poll']] = P['poll_old']
    if skip_startup:
        patches[P['wake']] = P['wake_old']
        patches[P['normal']] = P['normal_old']
    for a, old in patches.items():
        if img[a - 0x8000:a - 0x8000 + len(old)] != old: raise ValueError(f"unexpected bytes at {a:04X}: not the stock image of this version")
    code, lab = assemble(CODE_AT, program(P, diag, skip_startup, poll_weigh))
    o = CODE_AT - 0x8000
    if any(b != 0xFF for b in img[o:o + len(code)]) or CODE_AT + len(code) > 0xF000:
        raise ValueError("patch area is not free filler")
    img[o:o + len(code)] = code
    hooks = [(P['bar_up'], lab['HOOK1']), (P['tail'], lab['TAIL'])]
    if poll_weigh: hooks.append((P['poll'], lab['HOOK2']))
    if skip_startup:
        hooks.append((P['wake'], lab['HOOK3']))
        img[P['normal'] - 0x8000:P['normal'] - 0x8000 + 3] = bytes([0x12, lab['HOOK5'] >> 8, lab['HOOK5'] & 0xFF])   # LCALL HOOK5 (was LCALL weigh)
    for a, hook in hooks:
        img[a - 0x8000:a - 0x8000 + 3] = bytes([0x02, hook >> 8, hook & 0xFF])
    img[P['stage1_setb'] - 0x8000:P['stage1_setb'] - 0x8000 + 2] = bytes([0, 0])
    img[CHECKSUM_AT - 0x8000] = 0
    img[CHECKSUM_AT - 0x8000] = (-sum(img)) & 0xFF
    assert sum(img) & 0xFF == 0
    return bytes(img), code


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('inp'); ap.add_argument('out')
    ap.add_argument('--skip-startup', action='store_true')
    ap.add_argument('--poll-weigh', action='store_true')
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--diag', action='store_true'); g.add_argument('--diag2', action='store_true')
    ap.add_argument('--version', choices=sorted(profiles.PROFILES), help='firmware version (default: detect)')
    a = ap.parse_args()
    out, code = build(open(a.inp, 'rb').read(), 2 if a.diag2 else a.diag, a.skip_startup, a.poll_weigh, a.version)
    open(a.out, 'wb').write(out)
    print(f'patch {CODE_AT:04X}-{CODE_AT + len(code) - 1:04X} ({len(code)} bytes); written {a.out} (ROM checksum OK)')


if __name__ == '__main__':
    main()
