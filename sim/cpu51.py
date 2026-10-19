"""Complete 8051/8052 core for running the V10.45 cassette firmware in a simulation.

Direct addresses 00-7F are RAM, 80-FF are SFRs; indirect addresses 80-FF reach the upper 128 bytes of
internal RAM (8052).  External RAM is a dict; hooks may intercept MOVX and SFR reads/writes.
No timers or interrupts are modelled here: the caller raises them (see fwsim.py).
"""
PSW, ACC, B, SP, DPL, DPH = 0xD0, 0xE0, 0xF0, 0x81, 0x82, 0x83


class CPU51:
    def __init__(self, rom, base=0x8000):
        self.rom, self.base = rom, base
        self.ram = bytearray(256)      # 00-FF; 80-FF only via @Ri
        self.sfr = bytearray(256)      # indices 80-FF used
        self.xram = {}
        self.pc = 0
        self.cycles = 0
        self.xread_hook = None         # f(addr) -> value or None
        self.xwrite_hook = None        # f(addr, value) -> True if handled
        self.sfr_read_hook = None      # f(addr, current) -> value
        self.sfr_write_hook = None     # f(addr, value)
        self.sfr[SP] = 7
        self.sfr[0x90] = self.sfr[0xA0] = self.sfr[0xB0] = self.sfr[0x80] = 0xFF

    # ---------------- memory
    def code(self, a):
        a &= 0xFFFF
        i = a - self.base
        if 0 <= i < len(self.rom): return self.rom[i]
        if a < self.base and 0 <= a + 0x8000 - self.base < len(self.rom): return self.rom[a + 0x8000 - self.base]
        return 0xFF
    def rd(self, a):                                  # direct read
        if a < 0x80: return self.ram[a]
        v = self.sfr[a]
        if self.sfr_read_hook: v = self.sfr_read_hook(a, v)
        return v
    def wr(self, a, v):
        v &= 0xFF
        if a < 0x80: self.ram[a] = v
        else:
            self.sfr[a] = v
            if self.sfr_write_hook: self.sfr_write_hook(a, v)
    def ird(self, a): return self.ram[a & 0xFF]       # indirect read
    def iwr(self, a, v): self.ram[a & 0xFF] = v & 0xFF
    @property
    def A(self): return self.sfr[ACC]
    @A.setter
    def A(self, v): self.sfr[ACC] = v & 0xFF
    @property
    def B(self): return self.sfr[B]
    @B.setter
    def B(self, v): self.sfr[B] = v & 0xFF
    @property
    def DPTR(self): return self.sfr[DPH] << 8 | self.sfr[DPL]
    @DPTR.setter
    def DPTR(self, v): v &= 0xFFFF; self.sfr[DPH], self.sfr[DPL] = v >> 8, v & 0xFF
    def R(self, n): return (self.sfr[PSW] & 0x18) + n
    def r(self, n): return self.ram[self.R(n)]
    def setr(self, n, v): self.ram[self.R(n)] = v & 0xFF
    @property
    def C(self): return (self.sfr[PSW] >> 7) & 1
    @C.setter
    def C(self, v): self.sfr[PSW] = (self.sfr[PSW] & 0x7F) | (0x80 if v else 0)
    def parity(self):
        self.sfr[PSW] = (self.sfr[PSW] & 0xFE) | (bin(self.A).count('1') & 1)
    def xr(self, a):
        a &= 0xFFFF
        if self.xread_hook:
            v = self.xread_hook(a)
            if v is not None: return v & 0xFF
        return self.xram.get(a, 0)
    def xw(self, a, v):
        a &= 0xFFFF
        if self.xwrite_hook and self.xwrite_hook(a, v & 0xFF): return
        self.xram[a] = v & 0xFF

    # ---------------- bits
    def bitaddr(self, b):
        return (0x20 + (b >> 3), b & 7) if b < 0x80 else (b & 0xF8, b & 7)
    def getbit(self, b):
        byte, bit = self.bitaddr(b); return (self.rd(byte) >> bit) & 1
    def setbit(self, b, v):
        byte, bit = self.bitaddr(b); cur = self.rd(byte) if byte < 0x80 else self.sfr[byte]
        self.wr(byte, (cur & ~(1 << bit)) | (int(bool(v)) << bit))

    # ---------------- stack
    def push(self, v):
        sp = (self.sfr[SP] + 1) & 0xFF; self.sfr[SP] = sp; self.ram[sp] = v & 0xFF
    def pop(self):
        sp = self.sfr[SP]; v = self.ram[sp]; self.sfr[SP] = (sp - 1) & 0xFF; return v

    # ---------------- ALU helpers
    def add(self, y, c=0):
        a = self.A; r = a + y + c
        ac = ((a & 15) + (y & 15) + c) > 15
        c6 = ((a & 127) + (y & 127) + c) > 127
        self.C = r > 255
        ov = (r > 255) != c6
        self.sfr[PSW] = (self.sfr[PSW] & ~0x44) | (0x40 if ov else 0) | (0x40 if False else 0)
        self.sfr[PSW] = (self.sfr[PSW] & ~0x04) | (0x04 if ov else 0)
        self.sfr[PSW] = (self.sfr[PSW] & ~0x40) | (0x40 if ac else 0)
        self.A = r; self.parity()
    def subb(self, y, c):
        a = self.A; r = a - y - c
        ac = (a & 15) - (y & 15) - c < 0
        c6 = (a & 127) - (y & 127) - c < 0
        self.C = r < 0
        ov = (r < 0) != c6
        self.sfr[PSW] = (self.sfr[PSW] & ~0x04) | (0x04 if ov else 0)
        self.sfr[PSW] = (self.sfr[PSW] & ~0x40) | (0x40 if ac else 0)
        self.A = r; self.parity()

    def rel(self, x, nxt):
        return (nxt + (x - 256 if x > 127 else x)) & 0xFFFF

    # ---------------- one instruction
    def step(self):
        pc = self.pc; op = self.code(pc); b1 = self.code(pc + 1); b2 = self.code(pc + 2)
        nxt = pc + 1
        rn = op & 7; hi = op >> 4
        def src_rn(): return self.r(rn)
        def indir(i): return self.ird(self.r(i))
        # grouped by low nibble pattern (columns 4-F) --------------------------------
        if op == 0x00: pass
        elif op & 0x1F == 0x01:                       # AJMP / ACALL
            nxt = pc + 2; addr = (nxt & 0xF800) | ((op >> 5) << 8) | b1
            if op & 0x10: self.push(nxt & 0xFF); self.push(nxt >> 8)
            nxt = addr
        elif op == 0x02: nxt = b1 << 8 | b2
        elif op == 0x12: self.push((pc + 3) & 0xFF); self.push((pc + 3) >> 8); nxt = b1 << 8 | b2
        elif op == 0x22 or op == 0x32:
            hi_ = self.pop(); lo_ = self.pop(); nxt = hi_ << 8 | lo_
        elif op == 0x80: nxt = self.rel(b1, pc + 2)
        elif op == 0x40: nxt = self.rel(b1, pc + 2) if self.C else pc + 2
        elif op == 0x50: nxt = self.rel(b1, pc + 2) if not self.C else pc + 2
        elif op == 0x60: nxt = self.rel(b1, pc + 2) if self.A == 0 else pc + 2
        elif op == 0x70: nxt = self.rel(b1, pc + 2) if self.A != 0 else pc + 2
        elif op == 0x10:                               # JBC bit,rel
            nxt = pc + 3
            if self.getbit(b1): self.setbit(b1, 0); nxt = self.rel(b2, pc + 3)
        elif op == 0x20: nxt = self.rel(b2, pc + 3) if self.getbit(b1) else pc + 3
        elif op == 0x30: nxt = self.rel(b2, pc + 3) if not self.getbit(b1) else pc + 3
        elif op == 0x03: self.A = (self.A >> 1) | ((self.A & 1) << 7); nxt = pc + 1
        elif op == 0x13: c = self.A & 1; self.A = (self.A >> 1) | (self.C << 7); self.C = c
        elif op == 0x23: self.A = (self.A << 1) | (self.A >> 7)
        elif op == 0x33: c = self.A >> 7; self.A = (self.A << 1) | self.C; self.C = c
        elif op == 0x04: self.A = self.A + 1
        elif op == 0x14: self.A = self.A - 1
        elif op == 0x05: self.wr(b1, self.rd(b1) + 1); nxt = pc + 2
        elif op == 0x15: self.wr(b1, self.rd(b1) - 1); nxt = pc + 2
        elif op in (0x06, 0x07): i = op & 1; self.iwr(self.r(i), self.ird(self.r(i)) + 1)
        elif op in (0x16, 0x17): i = op & 1; self.iwr(self.r(i), self.ird(self.r(i)) - 1)
        elif 0x08 <= op <= 0x0F: self.setr(rn, self.r(rn) + 1)
        elif 0x18 <= op <= 0x1F: self.setr(rn, self.r(rn) - 1)
        elif op == 0xA3: self.DPTR = self.DPTR + 1
        elif op == 0xE4: self.A = 0
        elif op == 0xF4: self.A = ~self.A
        elif op == 0xC4: self.A = ((self.A << 4) | (self.A >> 4))
        elif op == 0xD4:                               # DA A
            a = self.A; c = self.C
            if (a & 15) > 9 or (self.sfr[PSW] & 0x40): 
                a += 6
                if a > 255: c = 1
            if (a >> 4) > 9 or c: a += 0x60; c = 1
            self.A = a; self.C = c
        elif op == 0xA4: p = self.A * self.B; self.A = p & 0xFF; self.B = p >> 8; self.C = 0; self.sfr[PSW] = (self.sfr[PSW] & ~4) | (4 if p > 255 else 0)
        elif op == 0x84:
            self.C = 0
            if self.B == 0: self.sfr[PSW] |= 4
            else:
                q, rm = divmod(self.A, self.B); self.A = q; self.B = rm; self.sfr[PSW] &= ~4
        elif op == 0xC3: self.C = 0
        elif op == 0xD3: self.C = 1
        elif op == 0xB3: self.C = 1 - self.C
        elif op == 0xC2: self.setbit(b1, 0); nxt = pc + 2
        elif op == 0xD2: self.setbit(b1, 1); nxt = pc + 2
        elif op == 0xB2: self.setbit(b1, 1 - self.getbit(b1)); nxt = pc + 2
        elif op == 0x92: self.setbit(b1, self.C); nxt = pc + 2
        elif op == 0xA2: self.C = self.getbit(b1); nxt = pc + 2
        elif op == 0x72: self.C = self.C | self.getbit(b1); nxt = pc + 2
        elif op == 0xA0: self.C = self.C | (1 - self.getbit(b1)); nxt = pc + 2
        elif op == 0x82: self.C = self.C & self.getbit(b1); nxt = pc + 2
        elif op == 0xB0: self.C = self.C & (1 - self.getbit(b1)); nxt = pc + 2
        elif op == 0x73: nxt = (self.A + self.DPTR) & 0xFFFF
        elif op == 0x93: self.A = self.code(self.A + self.DPTR)
        elif op == 0x83: self.A = self.code(self.A + pc + 1)
        elif op == 0xE0: self.A = self.xr(self.DPTR)
        elif op == 0xF0: self.xw(self.DPTR, self.A)
        elif op in (0xE2, 0xE3): self.A = self.xr((self.sfr[0xA0] << 8) | self.r(op & 1))
        elif op in (0xF2, 0xF3): self.xw((self.sfr[0xA0] << 8) | self.r(op & 1), self.A)
        elif op == 0xC0: self.push(self.rd(b1)); nxt = pc + 2
        elif op == 0xD0: self.wr(b1, self.pop()); nxt = pc + 2
        elif op == 0x90: self.DPTR = b1 << 8 | b2; nxt = pc + 3
        elif op == 0x74: self.A = b1; nxt = pc + 2
        elif op == 0x75: self.wr(b1, b2); nxt = pc + 3
        elif op in (0x76, 0x77): self.iwr(self.r(op & 1), b1); nxt = pc + 2
        elif 0x78 <= op <= 0x7F: self.setr(rn, b1); nxt = pc + 2
        elif op == 0x85: self.wr(b2, self.rd(b1)); nxt = pc + 3
        elif op in (0x86, 0x87): self.wr(b1, self.ird(self.r(op & 1))); nxt = pc + 2
        elif 0x88 <= op <= 0x8F: self.wr(b1, self.r(rn)); nxt = pc + 2
        elif op in (0xA6, 0xA7): self.iwr(self.r(op & 1), self.rd(b1)); nxt = pc + 2
        elif 0xA8 <= op <= 0xAF: self.setr(rn, self.rd(b1)); nxt = pc + 2
        elif op == 0xE5: self.A = self.rd(b1); nxt = pc + 2; self.parity()
        elif op in (0xE6, 0xE7): self.A = self.ird(self.r(op & 1))
        elif 0xE8 <= op <= 0xEF: self.A = self.r(rn)
        elif op == 0xF5: self.wr(b1, self.A); nxt = pc + 2
        elif op in (0xF6, 0xF7): self.iwr(self.r(op & 1), self.A)
        elif 0xF8 <= op <= 0xFF: self.setr(rn, self.A)
        elif op == 0xC5: t = self.rd(b1); self.wr(b1, self.A); self.A = t; nxt = pc + 2
        elif op in (0xC6, 0xC7): i = self.r(op & 1); t = self.ird(i); self.iwr(i, self.A); self.A = t
        elif 0xC8 <= op <= 0xCF: t = self.r(rn); self.setr(rn, self.A); self.A = t
        elif op in (0xD6, 0xD7):
            i = self.r(op & 1); t = self.ird(i); self.iwr(i, (t & 0xF0) | (self.A & 0x0F)); self.A = (self.A & 0xF0) | (t & 0x0F)
        # arithmetic / logic with the 4 operand forms
        elif 0x24 <= op <= 0x2F or 0x34 <= op <= 0x3F or 0x94 <= op <= 0x9F:
            base = op & 0xF0; form = op & 0x0F
            if form == 4: y = b1; nxt = pc + 2
            elif form == 5: y = self.rd(b1); nxt = pc + 2
            elif form in (6, 7): y = self.ird(self.r(form & 1))
            else: y = self.r(form - 8)
            if base == 0x20: self.add(y)
            elif base == 0x30: self.add(y, self.C)
            else: self.subb(y, self.C)
        elif (0x42 <= op <= 0x4F) or (0x52 <= op <= 0x5F) or (0x62 <= op <= 0x6F):
            base = op & 0xF0; form = op & 0x0F
            fn = {0x40: lambda a, b: a | b, 0x50: lambda a, b: a & b, 0x60: lambda a, b: a ^ b}[base]
            if form == 2: self.wr(b1, fn(self.rd(b1), self.A)); nxt = pc + 2
            elif form == 3: self.wr(b1, fn(self.rd(b1), b2)); nxt = pc + 3
            elif form == 4: self.A = fn(self.A, b1); nxt = pc + 2
            elif form == 5: self.A = fn(self.A, self.rd(b1)); nxt = pc + 2
            elif form in (6, 7): self.A = fn(self.A, self.ird(self.r(form & 1)))
            else: self.A = fn(self.A, self.r(form - 8))
        elif op == 0xB4: self.C = self.A < b1; nxt = self.rel(b2, pc + 3) if self.A != b1 else pc + 3
        elif op == 0xB5: v = self.rd(b1); self.C = self.A < v; nxt = self.rel(b2, pc + 3) if self.A != v else pc + 3
        elif op in (0xB6, 0xB7): v = self.ird(self.r(op & 1)); self.C = v < b1; nxt = self.rel(b2, pc + 3) if v != b1 else pc + 3
        elif 0xB8 <= op <= 0xBF: v = self.r(rn); self.C = v < b1; nxt = self.rel(b2, pc + 3) if v != b1 else pc + 3
        elif op == 0xD5:
            v = (self.rd(b1) - 1) & 0xFF; self.wr(b1, v); nxt = self.rel(b2, pc + 3) if v else pc + 3
        elif 0xD8 <= op <= 0xDF:
            v = (self.r(rn) - 1) & 0xFF; self.setr(rn, v); nxt = self.rel(b1, pc + 2) if v else pc + 2
        else:
            raise NotImplementedError(f"opcode {op:02X} at {pc:04X}")
        # instruction lengths for the ones that fall through with default nxt
        if nxt == pc + 1:
            nxt = pc + self.length(op)
        self.pc = nxt & 0xFFFF
        self.cycles += 1

    LEN = {}
    def length(self, op):
        if op in (0x02, 0x12, 0x10, 0x20, 0x30, 0x43, 0x53, 0x63, 0x75, 0x85, 0x90, 0xB4, 0xB5, 0xB6, 0xB7, 0xD5) or 0xB8 <= op <= 0xBF: return 3
        if op & 0x1F == 0x01: return 2
        if op in (0x05, 0x15, 0x24, 0x25, 0x34, 0x35, 0x40, 0x42, 0x44, 0x45, 0x50, 0x52, 0x54, 0x55, 0x60, 0x62, 0x64, 0x65, 0x70, 0x72,
                  0x74, 0x76, 0x77, 0x80, 0x82, 0x86, 0x87, 0x92, 0x94, 0x95, 0xA0, 0xA2, 0xA6, 0xA7, 0xB0, 0xB2, 0xC0, 0xC2, 0xC5, 0xD0,
                  0xD2, 0xE5, 0xF5) or 0x78 <= op <= 0x7F or 0x88 <= op <= 0x8F or 0xA8 <= op <= 0xAF or 0xD8 <= op <= 0xDF: return 2
        return 1
