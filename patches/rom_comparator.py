#!/usr/bin/env python3
"""
V10.45 cassette ROM: "comparator" averaging - long A/D averaging at stock loop speed.

Why: a bench test (2026-10-14) showed the A/D resolves about 3.2 mg per 240 ticks on the user's
PM4600, i.e. ~32 mg per stock 24-tick window.  A real 1 mg needs roughly 8 s of counting.  The
PM5003 Comparator data sheet (1 mg on 5100 g, display update 0.13 s, settling 9/12/15 s) shows
Mettler did exactly that in software: fast display loop, long averaging.

What it does (hook on CC11, the raw-value normaliser, only caller C919 on the weighing path):
  - each pass, the finished window sum w = S>>8 (XRAM 7042..7045) is stored as a 16-bit
    difference from a BASE value in a ring of up to N slots at 7682..76FF;
  - the output is the average of the newest half of the slots filled since the last restart, so
    readings taken while a new load was still creeping in drop out as the average grows; scaled
    exactly as stock CC11
    scales a single window: raw = bytes 1..3 of avg * (240/32h);
  - the average restarts (BASE = w, one slot) when w differs from the current average by more
    than --reset-mg, or when w leaves the 16-bit range around BASE.  So a new load is shown at
    single-window precision at once, and the reading then refines over ~N passes.
The main loop, 32h (24) and the stock 16-sample adaptive filter are untouched; the patch
variables live in the unused XRAM after that filter's ring (7090..70A3).

Default: always on.  --jumper: on only when the cassette's configuration-lock jumper ("24" in the
user manual; fitted = line 70D1.7 low) is fitted at power-on.

Usage:  rom_comparator.py IN.BIN OUT.BIN [-n 63] [--reset-mg 120] [--raw-per-g 1548] [--jumper]
"""
import argparse
import profiles

CODE_AT = 0xF000
CHECKSUM_AT = 0xF7FF
# the hook (MOV B,32h -> LJMP HOOK) and the calibration/stability timeouts (MOV 26h,#n counted down once per
# display pass, Err 1 at zero) are at version-specific addresses: see profiles.py

RING = 0x7682                     # 63 slots x 2 bytes up to 76FF (7680 is the EEPROM pointer)
NMAX = (0x7700 - RING) // 2
W, AVG, BASE = 0x7090, 0x7094, 0x7098
CNT, IDX, SAVE_R0, MODE, MARK, SIGN = 0x709C, 0x709D, 0x709E, 0x709F, 0x70A0, 0x70A3
PART, J, KV, NV, ENDV, SETV, RQ, CNTW = 0x70A4, 0x70A7, 0x70A8, 0x70A9, 0x70AA, 0x70AB, 0x70AC, 0x70AD
KSH, M = 0x70AE, 0x70AF
# vibration-adapter setting 0..3 -> (windows per slot, slots)
DEFAULT_TABLE = ((2, 38), (2, 63), (4, 50), (4, 50))   # averages the newest half: 38, 64, 100 windows
MARKER = (0x5A, 0xC3, 0x96)
DIRECT = {'B': 0xF0, 'DPL': 0x82, 'DPH': 0x83, 'ACC': 0xE0}
RN = ('R0', 'R1', 'R2', 'R3', 'R4', 'R5', 'R6', 'R7')


# ---------------------------------------------------------------- assembler
def encode(m, ops, pc, labels):
    def tgt(t): return labels[t] if labels is not None else pc
    def rel(t, size):
        d = tgt(t) - (pc + size)
        if labels is not None and not -128 <= d <= 127:
            raise ValueError(f"{m} to {t} out of range at {pc:04X} ({d})")
        return d & 0xFF
    imm = lambda o: isinstance(o, str) and o.startswith('#')
    iv = lambda o: int(o[1:], 0) & 0xFF
    def dirv(o):
        if isinstance(o, int): return o
        return DIRECT.get(o)
    o = list(ops)
    if m == 'MOV':
        d, s = o
        if d == 'A':
            if s in RN: return [0xE8 + RN.index(s)]
            if imm(s): return [0x74, iv(s)]
            if dirv(s) is not None: return [0xE5, dirv(s)]
        if s == 'A':
            if d in RN: return [0xF8 + RN.index(d)]
            if d == '@R0': return [0xF6]
            if dirv(d) is not None: return [0xF5, dirv(d)]
        if d == 'DPTR' and imm(s):
            v = int(s[1:], 0); return [0x90, v >> 8, v & 0xFF]
        if d == 'DPTR': return [0x90, tgt(s) >> 8, tgt(s) & 0xFF]
        if d in RN and imm(s): return [0x78 + RN.index(d), iv(s)]
        if d in RN and dirv(s) is not None: return [0xA8 + RN.index(d), dirv(s)]
        if dirv(d) is not None and s in RN: return [0x88 + RN.index(s), dirv(d)]
        if dirv(d) is not None and imm(s): return [0x75, dirv(d), iv(s)]
        if dirv(d) is not None and dirv(s) is not None: return [0x85, dirv(s), dirv(d)]
    if m == 'MOVX' and o == ['A', '@DPTR']: return [0xE0]
    if m == 'MOVX' and o == ['@DPTR', 'A']: return [0xF0]
    if m == 'INC':
        if o == ['DPTR']: return [0xA3]
        if o[0] in RN: return [0x08 + RN.index(o[0])]
        if o == ['A']: return [0x04]
    if m in ('ADD', 'ADDC', 'SUBB') and o[0] == 'A':
        base = {'ADD': 0x20, 'ADDC': 0x30, 'SUBB': 0x90}[m]
        s = o[1]
        if s in RN: return [base + 8 + RN.index(s)]
        if imm(s): return [base + 4, iv(s)]
        if dirv(s) is not None: return [base + 5, dirv(s)]
    if m == 'XRL' and o[0] == 'A' and imm(o[1]): return [0x64, iv(o[1])]
    if m == 'XRL' and o[0] == 'A' and o[1] in RN: return [0x68 + RN.index(o[1])]
    if m == 'ANL' and o[0] == 'A' and imm(o[1]): return [0x54, iv(o[1])]
    if m == 'CPL' and o == ['A']: return [0xF4]
    if m == 'RL' and o == ['A']: return [0x23]
    if m == 'RR' and o == ['A']: return [0x03]
    if m == 'RLC' and o == ['A']: return [0x33]
    if m == 'RRC' and o == ['A']: return [0x13]
    if m == 'MOVC' and o == ['A', '@A+DPTR']: return [0x93]
    if m == 'CLR' and o == ['A']: return [0xE4]
    if m == 'CLR' and o == ['C']: return [0xC3]
    if m == 'SETB' and o == ['C']: return [0xD3]
    if m == 'MUL': return [0xA4]
    if m == 'DIV': return [0x84]
    if m == 'CJNE' and o[0] == 'A' and imm(o[1]): return [0xB4, iv(o[1]), rel(o[2], 3)]
    if m == 'CJNE' and o[0] == 'A': return [0xB5, dirv(o[1]), rel(o[2], 3)]
    if m in ('JZ', 'JNZ', 'JC', 'JNC', 'SJMP'):
        return [{'JZ': 0x60, 'JNZ': 0x70, 'JC': 0x40, 'JNC': 0x50, 'SJMP': 0x80}[m], rel(o[0], 2)]
    if m in ('JB', 'JNB') and o[0] == 'ACC.7':
        return [0x20 if m == 'JB' else 0x30, 0xE7, rel(o[1], 3)]
    if m == 'DJNZ' and o[0] in RN: return [0xD8 + RN.index(o[0]), rel(o[1], 2)]
    if m == 'DJNZ' and dirv(o[0]) is not None: return [0xD5, dirv(o[0]), rel(o[1], 3)]
    if m == 'LJMP': return [0x02, tgt(o[0]) >> 8, tgt(o[0]) & 0xFF]
    if m == 'RET': return [0x22]
    if m == 'DB': return list(o)
    raise ValueError(f"cannot encode {m} {ops}")


def assemble(org, prog, external):
    labels, pc = dict(external), org
    for lab, m, *ops in prog:
        if lab:
            if lab in labels: raise ValueError(f"duplicate label {lab}")
            labels[lab] = pc
        pc += len(encode(m, ops, pc, None))
    code, pc = [], org
    for lab, m, *ops in prog:
        b = encode(m, ops, pc, labels)
        code += b; pc += len(b)
    return bytes(code), labels


# ---------------------------------------------------------------- the patch
def program(table, reset_units, jumper):
    """table: 4 x (k, n) indexed by the vibration-adapter setting (EEPROM byte 0, bits 2-3).
    k = windows per slot (1, 2 or 4), n = slots kept (1..NMAX). The output averages the newest
    half of the slots collected since the last restart (so readings taken while the load was still
    settling drop out as the average grows), i.e. k * ceil(n/2) windows once settled."""
    for k, n in table:
        assert k in (1, 2, 4) and 1 <= n <= NMAX and k * ((n + 1) // 2) + k - 1 <= 127
    lim = 2 * reset_units + 1
    assert lim < 0x10000
    P = []
    def L(label, *ins): P.append((label,) + ins)
    def I(*ins): P.append((None,) + ins)
    def dptr(v, label=None): P.append((label, 'MOV', 'DPTR', '#%d' % v))
    def load4(addr, label=None):  # XRAM addr -> B,R1,R2,R3
        dptr(addr, label)
        I('MOVX', 'A', '@DPTR'); I('MOV', 'B', 'A'); I('INC', 'DPTR')
        I('MOVX', 'A', '@DPTR'); I('MOV', 'R1', 'A'); I('INC', 'DPTR')
        I('MOVX', 'A', '@DPTR'); I('MOV', 'R2', 'A'); I('INC', 'DPTR')
        I('MOVX', 'A', '@DPTR'); I('MOV', 'R3', 'A')
    def store4(addr):             # B,R1,R2,R3 -> XRAM addr
        dptr(addr)
        I('MOV', 'A', 'B'); I('MOVX', '@DPTR', 'A'); I('INC', 'DPTR')
        I('MOV', 'A', 'R1'); I('MOVX', '@DPTR', 'A'); I('INC', 'DPTR')
        I('MOV', 'A', 'R2'); I('MOVX', '@DPTR', 'A'); I('INC', 'DPTR')
        I('MOV', 'A', 'R3'); I('MOVX', '@DPTR', 'A')
    def sub4(addr):               # B,R1,R2,R3 -= XRAM addr  (x + ~y + 1)
        dptr(addr)
        I('MOVX', 'A', '@DPTR'); I('CPL', 'A'); I('SETB', 'C'); I('ADDC', 'A', 'B'); I('MOV', 'B', 'A'); I('INC', 'DPTR')
        I('MOVX', 'A', '@DPTR'); I('CPL', 'A'); I('ADDC', 'A', 'R1'); I('MOV', 'R1', 'A'); I('INC', 'DPTR')
        I('MOVX', 'A', '@DPTR'); I('CPL', 'A'); I('ADDC', 'A', 'R2'); I('MOV', 'R2', 'A'); I('INC', 'DPTR')
        I('MOVX', 'A', '@DPTR'); I('CPL', 'A'); I('ADDC', 'A', 'R3'); I('MOV', 'R3', 'A')
    def neg3():
        I('MOV', 'A', 'R1'); I('CPL', 'A'); I('ADD', 'A', '#1'); I('MOV', 'R1', 'A')
        I('MOV', 'A', 'R2'); I('CPL', 'A'); I('ADDC', 'A', '#0'); I('MOV', 'R2', 'A')
        I('MOV', 'A', 'R3'); I('CPL', 'A'); I('ADDC', 'A', '#0'); I('MOV', 'R3', 'A')
    def marker_check(fail):
        dptr(MARK)
        for i, m in enumerate(MARKER):
            if i: I('INC', 'DPTR')
            I('MOVX', 'A', '@DPTR'); I('CJNE', 'A', '#%d' % m, fail)
    def setx(addr, value):        # XRAM addr = #value
        dptr(addr); I('MOV', 'A', '#%d' % value); I('MOVX', '@DPTR', 'A')

    # ---- entry (LCALL CC11 from C91B, R0 = result address 6Bh)
    L('HOOK', 'MOV', 'A', 'R0'); dptr(SAVE_R0); I('MOVX', '@DPTR', 'A')
    marker_check('INIT')
    dptr(MODE); I('MOVX', 'A', '@DPTR'); I('JNZ', 'RUN')
    L('STOCK', 'MOV', 'DPTR', '#%d' % SAVE_R0); I('MOVX', 'A', '@DPTR'); I('MOV', 'R0', 'A')
    I('MOV', 'B', 0x32); I('LJMP', 'RESUME')
    if jumper:
        L('INIT', 'MOV', 'DPTR', '#%d' % 0x70D1); I('MOVX', 'A', '@DPTR')
        I('CPL', 'A'); I('RL', 'A'); I('ANL', 'A', '#1')
    else:
        L('INIT', 'MOV', 'A', '#1')
    dptr(MODE); I('MOVX', '@DPTR', 'A')
    for m in MARKER:
        I('INC', 'DPTR'); I('MOV', 'A', '#%d' % m); I('MOVX', '@DPTR', 'A')
    setx(SETV, 0xFF)                                   # no setting yet: forces a restart
    dptr(MODE); I('MOVX', 'A', '@DPTR'); I('JZ', 'STOCK')

    # ---- adapter setting -> KV, NV, ENDV; a change of setting requests a restart
    L('RUN', 'MOV', 'DPTR', '#%d' % 0x7600); I('MOVX', 'A', '@DPTR')
    I('ANL', 'A', '#12'); I('RR', 'A'); I('RR', 'A'); I('MOV', 'R0', 'A')       # R0 = setting
    I('MOV', 'B', '#4'); I('MUL', 'AB'); I('MOV', 'R2', 'A')
    I('MOV', 'DPTR', 'TABLE'); I('MOVC', 'A', '@A+DPTR'); I('MOV', 'R1', 'A')    # k
    I('MOV', 'A', 'R2'); I('INC', 'A'); I('MOVC', 'A', '@A+DPTR'); I('MOV', 'R3', 'A')  # n
    I('MOV', 'A', 'R2'); I('ADD', 'A', '#2'); I('MOVC', 'A', '@A+DPTR'); I('MOV', 'B', 'A')  # end
    I('MOV', 'A', 'R2'); I('ADD', 'A', '#3'); I('MOVC', 'A', '@A+DPTR')                       # log2 k
    dptr(KSH); I('MOVX', '@DPTR', 'A')
    dptr(KV); I('MOV', 'A', 'R1'); I('MOVX', '@DPTR', 'A')
    I('INC', 'DPTR'); I('MOV', 'A', 'R3'); I('MOVX', '@DPTR', 'A')              # NV = KV+1
    I('INC', 'DPTR'); I('MOV', 'A', 'B'); I('MOVX', '@DPTR', 'A')               # ENDV = KV+2
    I('INC', 'DPTR'); I('MOVX', 'A', '@DPTR'); I('XRL', 'A', 'R0'); I('JZ', 'GET')  # SETV = KV+3
    I('MOV', 'A', 'R0'); I('MOVX', '@DPTR', 'A')
    setx(RQ, 1)
    # ---- new window -> B,R1,R2,R3 and W (re-read if a tick changed 31h meanwhile, as CC18 does)
    L('GET', 'MOV', 'R0', 0x31)
    load4(0x7042)
    I('MOV', 'A', 'R0'); I('CJNE', 'A', 0x31, 'GET')
    store4(W)
    dptr(RQ); I('MOVX', 'A', '@DPTR'); I('JZ', 'C1'); I('LJMP', 'REFILL')
    # ---- |W - AVG| <= reset ?   y = W - AVG + T must be 0..2T
    load4(W, 'C1')
    sub4(AVG)
    I('MOV', 'A', 'B'); I('ADD', 'A', '#%d' % (reset_units & 0xFF)); I('MOV', 'B', 'A')
    I('MOV', 'A', 'R1'); I('ADDC', 'A', '#%d' % (reset_units >> 8)); I('MOV', 'R1', 'A')
    I('MOV', 'A', 'R2'); I('ADDC', 'A', '#0'); I('JNZ', 'J1')
    I('MOV', 'A', 'R3'); I('ADDC', 'A', '#0'); I('JNZ', 'J1')
    I('CLR', 'C'); I('MOV', 'A', 'B'); I('SUBB', 'A', '#%d' % (lim & 0xFF))
    I('MOV', 'A', 'R1'); I('SUBB', 'A', '#%d' % (lim >> 8)); I('JC', 'C2')
    L('J1', 'LJMP', 'REFILL')
    # ---- d = W - BASE must fit 16 bits signed: W - BASE + 8000h in 0..FFFFh
    load4(W, 'C2')
    sub4(BASE)
    I('MOV', 'A', 'R1'); I('ADD', 'A', '#128'); I('MOV', 'R1', 'A')
    I('MOV', 'A', 'R2'); I('ADDC', 'A', '#0'); I('JNZ', 'J1')
    I('MOV', 'A', 'R3'); I('ADDC', 'A', '#0'); I('JNZ', 'J1')
    I('MOV', 'A', 'R1'); I('XRL', 'A', '#128'); I('MOV', 'R1', 'A')             # d = R1:B
    I('SJMP', 'ADDWIN')
    # ---- restart: BASE = W, empty ring, empty partial slot; the window enters with d = 0
    load4(W, 'REFILL')
    store4(BASE)
    setx(CNT, 0); setx(IDX, RING & 0xFF); setx(J, 0); setx(RQ, 0)
    dptr(PART); I('CLR', 'A'); I('MOVX', '@DPTR', 'A'); I('INC', 'DPTR'); I('MOVX', '@DPTR', 'A')
    I('INC', 'DPTR'); I('MOVX', '@DPTR', 'A')
    I('MOV', 'B', 'A'); I('MOV', 'R1', 'A')
    # ---- PART += d (24-bit); J += 1; when J == k the slot is complete
    L('ADDWIN', 'MOV', 'DPTR', '#%d' % PART)
    I('MOVX', 'A', '@DPTR'); I('ADD', 'A', 'B'); I('MOVX', '@DPTR', 'A'); I('INC', 'DPTR')
    I('MOVX', 'A', '@DPTR'); I('ADDC', 'A', 'R1'); I('MOVX', '@DPTR', 'A'); I('INC', 'DPTR')
    I('MOV', 'A', 'R1'); I('JB', 'ACC.7', 'SX1'); I('MOV', 'A', '#0'); I('SJMP', 'SX2')
    L('SX1', 'MOV', 'A', '#255')
    L('SX2', 'MOV', 'R2', 'A'); I('MOVX', 'A', '@DPTR'); I('ADDC', 'A', 'R2'); I('MOVX', '@DPTR', 'A')
    dptr(J); I('MOVX', 'A', '@DPTR'); I('INC', 'A'); I('MOVX', '@DPTR', 'A'); I('MOV', 'R2', 'A')
    dptr(KV); I('MOVX', 'A', '@DPTR'); I('XRL', 'A', 'R2'); I('JZ', 'SLOT'); I('LJMP', 'COMPUTE')
    # slot value: PART / k, floored (arithmetic shift right log2 k times) -> R1:B
    L('SLOT', 'MOV', 'DPTR', '#%d' % PART)
    I('MOVX', 'A', '@DPTR'); I('MOV', 'B', 'A'); I('INC', 'DPTR')
    I('MOVX', 'A', '@DPTR'); I('MOV', 'R1', 'A'); I('INC', 'DPTR')
    I('MOVX', 'A', '@DPTR'); I('MOV', 'R2', 'A')
    dptr(KSH); I('MOVX', 'A', '@DPTR'); I('JZ', 'NOSH'); I('MOV', 'R3', 'A')
    L('SHR', 'MOV', 'A', 'R2'); I('RLC', 'A')                                   # C = sign
    I('MOV', 'A', 'R2'); I('RRC', 'A'); I('MOV', 'R2', 'A')
    I('MOV', 'A', 'R1'); I('RRC', 'A'); I('MOV', 'R1', 'A')
    I('MOV', 'A', 'B'); I('RRC', 'A'); I('MOV', 'B', 'A')
    I('DJNZ', 'R3', 'SHR')
    L('NOSH', 'MOV', 'DPTR', '#%d' % IDX); I('MOVX', 'A', '@DPTR')
    I('MOV', 'DPL', 'A'); I('MOV', 'DPH', '#%d' % (RING >> 8))
    I('MOV', 'A', 'B'); I('MOVX', '@DPTR', 'A'); I('INC', 'DPTR')
    I('MOV', 'A', 'R1'); I('MOVX', '@DPTR', 'A'); I('INC', 'DPTR')
    I('MOV', 'A', 'DPL'); I('MOV', 'R3', 'A')
    dptr(ENDV); I('MOVX', 'A', '@DPTR'); I('XRL', 'A', 'R3'); I('JNZ', 'NOWRAP')
    I('MOV', 'R3', '#%d' % (RING & 0xFF))
    L('NOWRAP', 'MOV', 'DPTR', '#%d' % IDX); I('MOV', 'A', 'R3'); I('MOVX', '@DPTR', 'A')
    dptr(CNT); I('MOVX', 'A', '@DPTR'); I('MOV', 'R3', 'A')
    dptr(NV); I('MOVX', 'A', '@DPTR'); I('XRL', 'A', 'R3'); I('JZ', 'FULL')
    dptr(CNT); I('MOV', 'A', 'R3'); I('INC', 'A'); I('MOVX', '@DPTR', 'A')
    L('FULL', 'MOV', 'DPTR', '#%d' % PART); I('CLR', 'A'); I('MOVX', '@DPTR', 'A')
    I('INC', 'DPTR'); I('MOVX', '@DPTR', 'A'); I('INC', 'DPTR'); I('MOVX', '@DPTR', 'A')
    setx(J, 0)

    # ---- total = k * (sum of the newest M = ceil(CNT/2) slots) + PART  -> R1,R2,R3 (24-bit signed)
    L('COMPUTE', 'MOV', 'DPTR', '#%d' % CNT); I('MOVX', 'A', '@DPTR')
    I('INC', 'A'); I('CLR', 'C'); I('RRC', 'A'); I('MOV', 'R0', 'A')
    dptr(M); I('MOVX', '@DPTR', 'A'); I('JZ', 'NOSUM0')
    I('ADD', 'A', 'R0'); I('MOV', 'B', 'A')                                     # 2M bytes back
    dptr(IDX); I('MOVX', 'A', '@DPTR'); I('CLR', 'C'); I('SUBB', 'A', 'B'); I('MOV', 'R1', 'A')
    I('CJNE', 'A', '#%d' % (RING & 0xFF), 'CHK')
    L('CHK', 'JNC', 'STOK')                                                     # below the ring: wrap
    dptr(NV); I('MOVX', 'A', '@DPTR'); I('ADD', 'A', 'ACC'); I('ADD', 'A', 'R1'); I('MOV', 'R1', 'A')
    L('STOK', 'MOV', 'DPTR', '#%d' % ENDV); I('MOVX', 'A', '@DPTR'); I('MOV', 'B', 'A')
    I('MOV', 'A', 'R1'); I('MOV', 'DPL', 'A'); I('MOV', 'DPH', '#%d' % (RING >> 8))
    I('CLR', 'A'); I('MOV', 'R1', 'A'); I('MOV', 'R2', 'A'); I('MOV', 'R3', 'A')
    L('SUM', 'MOVX', 'A', '@DPTR'); I('ADD', 'A', 'R1'); I('MOV', 'R1', 'A'); I('INC', 'DPTR')
    I('MOVX', 'A', '@DPTR'); I('JB', 'ACC.7', 'SN')
    I('ADDC', 'A', 'R2'); I('MOV', 'R2', 'A'); I('CLR', 'A'); I('SJMP', 'SA')
    L('SN', 'ADDC', 'A', 'R2'); I('MOV', 'R2', 'A'); I('MOV', 'A', '#255')
    L('SA', 'ADDC', 'A', 'R3'); I('MOV', 'R3', 'A'); I('INC', 'DPTR')
    I('MOV', 'A', 'DPL'); I('CJNE', 'A', 'B', 'NW2')
    dptr(RING)
    L('NW2', 'DJNZ', 'R0', 'SUM')
    dptr(KSH); I('MOVX', 'A', '@DPTR'); I('JZ', 'NOSUM'); I('MOV', 'R0', 'A')
    L('SHL', 'CLR', 'C')
    I('MOV', 'A', 'R1'); I('RLC', 'A'); I('MOV', 'R1', 'A')
    I('MOV', 'A', 'R2'); I('RLC', 'A'); I('MOV', 'R2', 'A')
    I('MOV', 'A', 'R3'); I('RLC', 'A'); I('MOV', 'R3', 'A')
    I('DJNZ', 'R0', 'SHL')
    I('SJMP', 'NOSUM')
    L('NOSUM0', 'CLR', 'A'); I('MOV', 'R1', 'A'); I('MOV', 'R2', 'A'); I('MOV', 'R3', 'A')
    L('NOSUM', 'MOV', 'DPTR', '#%d' % PART)
    I('MOVX', 'A', '@DPTR'); I('ADD', 'A', 'R1'); I('MOV', 'R1', 'A'); I('INC', 'DPTR')
    I('MOVX', 'A', '@DPTR'); I('ADDC', 'A', 'R2'); I('MOV', 'R2', 'A'); I('INC', 'DPTR')
    I('MOVX', 'A', '@DPTR'); I('ADDC', 'A', 'R3'); I('MOV', 'R3', 'A')
    # ---- count = (M << log2 k) + J  -> DPL later
    dptr(M); I('MOVX', 'A', '@DPTR'); I('MOV', 'R0', 'A')
    dptr(KSH); I('MOVX', 'A', '@DPTR'); I('JZ', 'K1'); I('MOV', 'B', 'A')
    L('KL', 'MOV', 'A', 'R0'); I('ADD', 'A', 'R0'); I('MOV', 'R0', 'A'); I('DJNZ', 'B', 'KL')
    L('K1', 'MOV', 'DPTR', '#%d' % J); I('MOVX', 'A', '@DPTR'); I('ADD', 'A', 'R0')
    dptr(CNTW); I('MOVX', '@DPTR', 'A')
    # ---- q = total / count, signed (restoring division, divisor in DPL)
    I('MOV', 'A', 'R3'); I('RLC', 'A'); I('CLR', 'A'); I('SUBB', 'A', '#0')   # FF if negative
    dptr(SIGN); I('MOVX', '@DPTR', 'A'); I('JZ', 'POS')
    neg3()
    L('POS', 'MOV', 'DPTR', '#%d' % CNTW); I('MOVX', 'A', '@DPTR'); I('MOV', 'DPL', 'A')
    I('MOV', 'B', '#0'); I('MOV', 'R0', '#24')
    L('DIVL', 'CLR', 'C')
    I('MOV', 'A', 'R1'); I('RLC', 'A'); I('MOV', 'R1', 'A')
    I('MOV', 'A', 'R2'); I('RLC', 'A'); I('MOV', 'R2', 'A')
    I('MOV', 'A', 'R3'); I('RLC', 'A'); I('MOV', 'R3', 'A')
    I('MOV', 'A', 'B'); I('RLC', 'A'); I('MOV', 'B', 'A')
    I('CLR', 'C'); I('SUBB', 'A', 'DPL'); I('JC', 'DSK')
    I('MOV', 'B', 'A'); I('INC', 'R1')
    L('DSK', 'DJNZ', 'R0', 'DIVL')
    dptr(SIGN); I('MOVX', 'A', '@DPTR'); I('JZ', 'POS2')
    neg3()
    L('POS2', 'MOV', 'A', 'R3'); I('RLC', 'A'); I('CLR', 'A'); I('SUBB', 'A', '#0'); I('MOV', 'B', 'A')
    # ---- AVG = BASE + q
    dptr(BASE)
    I('MOVX', 'A', '@DPTR'); I('ADD', 'A', 'R1'); I('MOV', 'R1', 'A'); I('INC', 'DPTR')
    I('MOVX', 'A', '@DPTR'); I('ADDC', 'A', 'R2'); I('MOV', 'R2', 'A'); I('INC', 'DPTR')
    I('MOVX', 'A', '@DPTR'); I('ADDC', 'A', 'R3'); I('MOV', 'R3', 'A'); I('INC', 'DPTR')
    I('MOVX', 'A', '@DPTR'); I('ADDC', 'A', 'B'); I('MOV', 'B', 'A')
    dptr(AVG + 3); I('MOVX', '@DPTR', 'A')
    dptr(AVG)
    I('MOV', 'A', 'R1'); I('MOVX', '@DPTR', 'A'); I('INC', 'DPTR')
    I('MOV', 'A', 'R2'); I('MOVX', '@DPTR', 'A'); I('INC', 'DPTR')
    I('MOV', 'A', 'R3'); I('MOVX', '@DPTR', 'A')
    # ---- raw = bytes 1..3 of AVG * (240/32h), exactly as CC14..CC4F do for one window
    I('MOV', 'A', '#240'); I('MOV', 'B', 0x32); I('DIV', 'AB'); I('MOV', 'R0', 'A')
    dptr(AVG)
    I('MOVX', 'A', '@DPTR'); I('MOV', 'B', 'R0'); I('MUL', 'AB'); I('MOV', 'R2', 'B'); I('INC', 'DPTR')
    I('MOVX', 'A', '@DPTR'); I('MOV', 'B', 'R0'); I('MUL', 'AB'); I('ADD', 'A', 'R2'); I('MOV', 'R1', 'A')
    I('MOV', 'A', 'B'); I('ADDC', 'A', '#0'); I('MOV', 'R2', 'A'); I('INC', 'DPTR')
    I('MOVX', 'A', '@DPTR'); I('MOV', 'B', 'R0'); I('MUL', 'AB'); I('ADD', 'A', 'R2'); I('MOV', 'R3', 'A')
    I('MOV', 'A', 'B'); I('ADDC', 'A', '#0'); I('MOV', 'R2', 'A'); I('INC', 'DPTR')
    I('MOVX', 'A', '@DPTR'); I('MOV', 'B', 'R0'); I('MUL', 'AB'); I('ADD', 'A', 'R2'); I('MOV', 'B', 'A')
    dptr(SAVE_R0); I('MOVX', 'A', '@DPTR'); I('MOV', 'R0', 'A')
    I('MOV', 'A', 'R1'); I('MOV', '@R0', 'A'); I('INC', 'R0')
    I('MOV', 'A', 'R3'); I('MOV', '@R0', 'A'); I('INC', 'R0')
    I('MOV', 'A', 'B'); I('MOV', '@R0', 'A')
    I('RET')
    # ---- per-setting table: k, n, low byte of the ring end, log2 k
    L('TABLE', 'DB', *[v for k, n in table for v in (k, n, (RING + 2 * n) & 0xFF, k.bit_length() - 1)])
    return P


def units_per_mg(raw_per_g, window=24):
    # raw = (S>>8) * 10 / 256  ->  one raw unit is 25.6 units of S>>8
    return raw_per_g / 1000 * 256 * window / 240          # 25.6 for the 24-tick window of V10.45


def build(img, table=DEFAULT_TABLE, reset_mg=120, raw_per_g=1548, jumper=False, cal_passes=150, profile=None):
    img = bytearray(img)
    if len(img) != 0x8000: raise ValueError("expected a 32 KiB ROM image")
    if sum(img) & 0xFF: raise ValueError("ROM checksum of the input is already wrong")
    P = profiles.get(bytes(img), profile)
    hook, resume = P['comp_hook'], P['comp_resume']
    if img[hook - 0x8000:hook - 0x8000 + 3] != P['comp_hook_old']:
        raise ValueError(f"unexpected bytes at {hook:04X}: not the stock image of this version")
    reset_units = round(reset_mg * units_per_mg(raw_per_g, P['window']))
    code, lab = assemble(CODE_AT, program(table, reset_units, jumper), {'RESUME': resume})
    o = CODE_AT - 0x8000
    if any(b != 0xFF for b in img[o:o + len(code)]) or CODE_AT + len(code) > CHECKSUM_AT:
        raise ValueError("patch area is not free filler")
    img[o:o + len(code)] = code
    for at, n0 in P['cal_timeouts'].items():
        o2 = at - 0x8000
        if img[o2:o2 + 3] != bytes([0x75, 0x26, n0]):
            raise ValueError(f"unexpected bytes at {at:04X}")
        if cal_passes:
            assert 1 <= cal_passes <= 255
            img[o2 + 2] = max(n0, cal_passes)
    h = lab['HOOK']
    img[hook - 0x8000:hook - 0x8000 + 3] = bytes([0x02, h >> 8, h & 0xFF])
    img[CHECKSUM_AT - 0x8000] = 0
    img[CHECKSUM_AT - 0x8000] = (-sum(img)) & 0xFF
    assert sum(img) & 0xFF == 0
    return bytes(img), lab, code, reset_units


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('inp'); p.add_argument('out')
    p.add_argument('--table', default=','.join(f'{k}x{n}' for k, n in DEFAULT_TABLE),
                   help='per vibration-adapter setting 0..3: KxN = K windows per slot, N slots')
    p.add_argument('--reset-mg', type=float, default=120, help='restart the average above this change')
    p.add_argument('--raw-per-g', type=float, default=1548, help='raw units per gram (PM4600: 1548)')
    p.add_argument('--version', choices=sorted(profiles.PROFILES), help='firmware version (default: detect)')
    p.add_argument('--jumper', action='store_true', help='on only with the cassette jumper fitted')
    p.add_argument('--cal-passes', type=int, default=150,
                   help='calibration/stability timeouts in display passes (stock 48 and 72; 0 = stock)')
    a = p.parse_args()
    table = tuple(tuple(int(v) for v in e.split('x')) for e in a.table.split(','))
    assert len(table) == 4, 'four settings'
    out, lab, code, ru = build(open(a.inp, 'rb').read(), table, a.reset_mg, a.raw_per_g, a.jumper, a.cal_passes, a.version)
    open(a.out, 'wb').write(out)
    print(f"patch {CODE_AT:04X}-{CODE_AT + len(code) - 1:04X} ({len(code)} bytes), table {a.table}, "
          f"restart above {a.reset_mg:g} mg ({ru} units), {'jumper' if a.jumper else 'always on'}, "
          f"calibration timeouts {a.cal_passes or 'stock'} passes")
    print(f"written {a.out} (ROM checksum OK)")


if __name__ == '__main__':
    main()
