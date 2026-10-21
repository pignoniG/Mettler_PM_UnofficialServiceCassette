#!/usr/bin/env python3
"""
Service commands over the serial line: read and write the parameter EEPROM from a PC.

The stock ROM already contains a host-command interpreter and, unreferenced, an EEPROM dump and load in Intel
HEX (see docs/PROTOCOL.md).  This patch only gives them a way in:

  * the jump-table entry of the command letter X is pointed at a small handler here, so X commands arrive through
    the stock interpreter (line assembly, parity, ES reply for unknown commands);
  * the handler calls the stock dump routine (16 records of 8 bytes from the EEPROM mirror, then :00000001FF) and
    the stock load routine (16 records into a buffer; it checks the three block checksums, copies the buffer to the
    mirror and writes the chip), with two guards added: writing has to be armed first, and the ID bytes of the new
    image have to equal those in the chip unless the "board swap" arming is used.

Commands (each a line ending CR LF; send a newline first, the receiver drops the first byte after boot or wake):

    XV   version line:  XV SVC 10.45 ID 0E0F10  (the three ID bytes of the EEPROM, hex)
    XD   dump the EEPROM:  :08000000dd..ddcc (x16), :00000001FF
    XA   arm one write (the ID of the new image must equal the one in the chip)   -> XA
    XB   arm one write, ID not checked (board swap, blank chip)                    -> XB
    XL   load: the balance answers XL when it is ready, then send 16 Intel HEX records of 8 bytes and :00000001FF;
         answers EL on any error (nothing is written), otherwise the stock success reply; disarms (not armed: EL)
    XR   restart the balance (so that new settings take effect)                    -> XR

The patched stock load routine needs the full 128 bytes in order, checksums valid, and a valid checksum of the
three EEPROM blocks (04..2D, 2E..31, 3E..67); the user-calibration block 32..35 is not checked by it.

Code goes at EB00 (free filler); XRAM 70EF = arm state, 70F9..70FB scratch.
Usage:  rom_service.py IN.BIN OUT.BIN [--version 10.45|10.50]
"""
import argparse
import asm51
import profiles
import rom_temperature as rt

CODE_AT = 0xEB00
CHECKSUM_AT = 0xF7FF
ARM, SAV = 0x70EF, 0x70F9


def encode(m, ops, pc, labels):
    o = list(ops)
    if m == 'DWL':                                         # big-endian address word (the stock matcher reads hi, lo)
        t = o[0] if isinstance(o[0], int) else (0 if labels is None else labels[o[0]])
        return [t >> 8, t & 0xFF]
    if m == 'LJMP' and isinstance(o[0], int): return [0x02, o[0] >> 8, o[0] & 0xFF]
    if m == 'AJMPL': return [0x02, 0, 0] if labels is None else [0x02, labels[o[0]] >> 8, labels[o[0]] & 0xFF]
    if m == 'XRLA' : return [0x64, o[0]]
    if m == 'MOVDPH': return [0x75, 0x83, o[0]]
    if m == 'MOVDPL': return [0x75, 0x82, o[0]]
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


def text(s):                                               # inline string for the stock printer / matcher
    return [ord(c) for c in s] + [0x24]


def program(P, ver):
    prog = []
    def I(*ins): prog.append((None,) + ins)
    def L(label, *ins): prog.append((label,) + ins)
    def say(s): I('LCALL', P['cmd_print']); I('DB', *text(s))
    MATCH, DONE, ES = P['cmd_match'], P['cmd_done'], P['cmd_es']

    # ---- the handler: the stock matcher tries the alternatives in order; on a match it jumps to the address after the string
    L('HX', 'LCALL', MATCH)
    for letter, target in (('D', 'HXD'), ('L', 'HXL'), ('A', 'HXA'), ('B', 'HXB'), ('V', 'HXV'), ('R', 'HXR')):
        I('DB', *text(letter + '\r\n')); I('DWL', target)
    I('DB', 0x24); I('DWL', ES)                              # anything else: ES

    L('HXD', 'LCALL', P['hex_dump']); I('LJMP', DONE)
    L('HXA', 'MOV', 'DPTR', '#%d' % ARM); I('MOV', 'A', '#1'); I('MOVX', '@DPTR', 'A'); say('XA\r\n'); I('LJMP', DONE)
    L('HXB', 'MOV', 'DPTR', '#%d' % ARM); I('MOV', 'A', '#2'); I('MOVX', '@DPTR', 'A'); say('XB\r\n'); I('LJMP', DONE)
    L('HXL', 'MOV', 'DPTR', '#%d' % ARM); I('MOVX', 'A', '@DPTR'); I('JNZ', 'HXL2')
    say('EL\r\n'); I('LJMP', DONE)                           # not armed
    L('HXL2', 'LBL'); say('XL\r\n')                           # ready: the PC sends the records after this line
    I('LCALL', P['hex_load'])
    I('MOV', 'DPTR', '#%d' % ARM); I('CLR', 'A'); I('MOVX', '@DPTR', 'A'); I('LJMP', DONE)
    L('HXV', 'LCALL', 'PRINT0'); I('LJMP', DONE)
    L('HXR', 'LBL'); say('XR\r\n')
    I('MOV', 'R6', '#0')                                     # let the output drain, then start over
    L('HXR1', 'MOV', 'A', 0x35); I('JNZ', 'HXR1')
    L('HXR2', 'MOV', 'R7', '#0')
    L('HXR3', 'DJNZ', 'R7', 'HXR3'); I('DJNZ', 'R6', 'HXR2')
    I('LJMP', P['reset'])

    # ---- version line: XV SVC 10.45 ID 0E0F10
    L('PRINT0', 'LCALL', P['cmd_print']); I('DB', *text('XV SVC ' + ver + ' ID '))
    for a in (0x760E, 0x760F, 0x7610):
        I('MOV', 'DPTR', '#%d' % a); I('MOVX', 'A', '@DPTR'); I('LCALL', 'PUTHEX')
    I('LCALL', P['cmd_print']); I('DB', *text('\r\n')); I('RET')
    L('PUTHEX', 'PUSH', 0xE0); I('SWAP', 'A'); I('ANL', 'A', '#15'); I('ADD', 'A', '#144'); I('DA'); I('ADDC', 'A', '#64'); I('DA')
    I('LCALL', P['putc']); I('POP', 0xE0); I('ANL', 'A', '#15'); I('ADD', 'A', '#144'); I('DA'); I('ADDC', 'A', '#64'); I('DA')
    I('LCALL', P['putc']); I('RET')

    # ---- guard, called instead of the MOV DPL,#00 that starts the copy of the verified buffer to the mirror
    L('GUARD', 'MOV', 'DPTR', '#%d' % ARM); I('MOVX', 'A', '@DPTR'); I('XRLA', 2); I('JZ', 'ALLOW')       # B: no ID check
    I('MOV', 'DPTR', '#%d' % SAV)
    for r in ('R2', 'R3', 'R4'):
        I('MOV', 'A', r); I('MOVX', '@DPTR', 'A'); I('INC', 'DPTR')
    I('MOV', 'R2', '#3'); I('MOV', 'R3', '#14')
    L('GL', 'MOVDPH', 0x75); I('MOV', 'DPL', 'R3'); I('MOVX', 'A', '@DPTR'); I('MOV', 'R4', 'A')
    I('MOVDPH', 0x76); I('MOVX', 'A', '@DPTR'); I('XRL', 'A', 'R4'); I('JNZ', 'GDENY')
    I('INC', 'R3'); I('DJNZ', 'R2', 'GL')
    I('MOV', 'DPTR', '#%d' % SAV)
    for r in ('R2', 'R3', 'R4'):
        I('MOVX', 'A', '@DPTR'); I('MOV', r, 'A'); I('INC', 'DPTR')
    L('ALLOW', 'MOVDPH', 0x75); I('MOVDPL', 0); I('RET')
    L('GDENY', 'POP', 0xE0); I('POP', 0xE0); I('LJMP', P['hex_error'])      # EL, back to the command handler
    return prog


def build(img, profile=None, version_text=None):
    img = bytearray(img)
    if len(img) != 0x8000 or sum(img) & 0xFF: raise ValueError("expected a 32 KiB image with a good checksum")
    P = profiles.get(bytes(img), profile)
    name = profile if isinstance(profile, str) else (profiles.detect(bytes(img)) or '')
    for a, old in ((P['hex_guard'], P['hex_guard_old']), (P['cmd_table_x'], bytes([P['cmd_es'] >> 8, P['cmd_es'] & 0xFF]))):
        if img[a - 0x8000:a - 0x8000 + len(old)] != old: raise ValueError(f"unexpected bytes at {a:04X}: not the stock image of this version")
    code, lab = assemble(CODE_AT, program(P, version_text or name))
    o = CODE_AT - 0x8000
    if any(b != 0xFF for b in img[o:o + len(code)]) or CODE_AT + len(code) > 0xF000:
        raise ValueError("patch area is not free filler")
    img[o:o + len(code)] = code
    t = P['cmd_table_x'] - 0x8000
    img[t:t + 2] = bytes([lab['HX'] >> 8, lab['HX'] & 0xFF])
    g = P['hex_guard'] - 0x8000
    img[g:g + 3] = bytes([0x12, lab['GUARD'] >> 8, lab['GUARD'] & 0xFF])
    img[CHECKSUM_AT - 0x8000] = 0
    img[CHECKSUM_AT - 0x8000] = (-sum(img)) & 0xFF
    assert sum(img) & 0xFF == 0
    return bytes(img), code, CODE_AT


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('inp'); ap.add_argument('out')
    ap.add_argument('--version', choices=sorted(profiles.PROFILES), help='firmware version (default: detect)')
    a = ap.parse_args()
    out, code, at = build(open(a.inp, 'rb').read(), a.version)
    open(a.out, 'wb').write(out)
    print(f'patch {at:04X}-{at + len(code) - 1:04X} ({len(code)} bytes); written {a.out} (ROM checksum OK)')


if __name__ == '__main__':
    main()
