"""Run the real V10.45 cassette firmware (boot + main-loop passes) with stubbed hardware.

The interrupt-driven hardware is replaced by per-pass stubs: the A/D window sum (7042..7045), the
control-bar/jumper switches (70D1), serial transmit (drains the 7200 ring like C6D9..C714) and the
EEPROM read (the 7600 mirror is preloaded).
"""
import sys
from cpu51 import CPU51


class Sim(CPU51):
    def __init__(self, rom, eeprom_fw):
        super().__init__(rom)
        self.eeprom = bytes(eeprom_fw)
        self.sent = []                          # bytes the UART would have sent
        self.sw = 0x00                          # 70D1: bit6 control bar, bit7 jumper line
        self.sfr[0x90] = 0xFF
        self.hist = {}
        self.waits = {}
        self.idle_keys = True
        self.pc_hooks = {0xCDB7: Sim.eeprom_write, 0xCD98: Sim.eeprom_load}
        self.hold = set()

    def step_hw(self):
        """one instruction; a self-loop on a flag the interrupts would change is broken here"""
        pc = self.pc
        if pc in self.pc_hooks:
            self.pc_hooks[pc](self); return
        if pc == 0xCD2D:                       # boot timer self-test: both timers must have overflowed
            self.sfr[0x88] |= 0xA0
        op = self.code(pc)
        if op in (0x20, 0x30) and self.rel(self.code(pc + 2), pc + 3) == pc:      # JB/JNB bit,$
            bit = self.code(pc + 1)
            if (op == 0x30) == (not self.getbit(bit)):
                self.hw_wait(bit, want_set=(op == 0x30))
        if op == 0x10 and self.rel(self.code(pc + 2), pc + 3) == pc:
            self.hw_wait(self.code(pc + 1), want_set=True)
        self.step()

    def hw_wait(self, bit, want_set):
        self.waits[bit] = self.waits.get(bit, 0) + 1
        if bit in self.hold and self.pc == 0x82F3: raise StopIteration(bit)
        self.setbit(bit, 1 if want_set else 0)

    def run_until(self, stop, max_steps=3_000_000):
        n = 0
        while self.pc != stop:
            self.step_hw(); n += 1
            if n > max_steps: raise RuntimeError(f"no progress after {n} steps, pc={self.pc:04X}")
        return n

    def uart_drain(self):
        """what the transmit interrupt does with the 7200 ring (count 35h, read index 36h)"""
        while self.ram[0x35]:
            ch = self.xram.get(0x7200 + self.ram[0x36], 0)
            self.sent.append(ch & 0x7F)
            self.ram[0x36] = (self.ram[0x36] + 1) & 0x7F
            self.ram[0x35] -= 1

    def ret(self):
        hi = self.pop(); lo = self.pop(); self.pc = hi << 8 | lo

    def eeprom_read(self):
        """the interrupt-driven 93C46 driver reading R2 bytes from byte address R0 (rounded to words) into
        7600+addr; used by eeprom_load below.  Ends with R2 = 0 and R0 past the last word"""
        r0, r2 = self.r(0), self.r(2)
        a0 = r0 & ~1; nw = (r2 + 1) >> 1
        for w in range(nw):
            for k in (0, 1):
                self.xram[0x7600 + a0 + 2 * w + k] = self.eeprom[(a0 + 2 * w + k) & 0x7F]
        self.setr(0, a0 + 2 * nw); self.setr(2, 0)
        self.xram[0x7680] = (a0 + 2 * nw - 2) & 0xFF
        self.C = 0
        self.ret()

    def eeprom_load(self):
        """CD98: boot read of the whole EEPROM (R0 = 0, R2 = 80h) into the 7600 mirror"""
        self.setr(0, 0); self.setr(2, 0x80)
        self.eeprom_read()

    # ---------------- main-loop passes
    def boot(self, max_steps=3_000_000):
        self.pc = 0x8200; self.hold = {0x48}
        n = 0
        while n < max_steps:
            self.step_hw(); n += 1
            if self.pc == 0x82F3 and not self.getbit(0x48): return n
        raise RuntimeError("boot did not reach the main loop")

    def set_window(self, s_value):
        """what the A/D interrupt leaves for the main loop: the 40-bit window sum at 7041..7045"""
        for i in range(5): self.xram[0x7041 + i] = (s_value >> (8 * i)) & 0xFF

    def one_pass(self, s_value=None, sw=None, max_steps=400_000, trace=False):
        """run the main loop for one measurement window: sets the flags the ISR would, runs until the loop
        waits again; returns the visited pc list if trace"""
        if s_value is not None: self.set_window(s_value)
        if sw is not None: self.sw = sw
        self.xram[0x70D1] = self.sw
        if self.getbit(0x4E) and self.idle_keys: self.ram[0x25] = 0      # standby: the bar scan leaves 25h = 0 when untouched
        self.setbit(0x48, 1)                  # 29h.0: window ready
        self.hold = {0x48}
        seen = []
        n = 0
        while True:
            if self.pc == 0x82F3 and not self.getbit(0x48) and n > 0: break
            if trace: seen.append(self.pc)
            self.step_hw(); n += 1
            if n > max_steps: raise RuntimeError(f"pass did not finish, pc={self.pc:04X}")
        self.uart_drain()
        self.ticks = getattr(self, 'ticks', 0) + 1
        return seen if trace else n

    def eeprom_write(self):
        """CDB7: write R2 bytes (rounded to words) from the 7600 mirror at byte address R0 to the chip,
        with the read-back verify the real routine does (always passes here)"""
        r0, r2 = self.r(0), self.r(2)
        a0 = r0 & ~1; nw = (r2 + 1) >> 1
        e = bytearray(self.eeprom)
        for i in range(2 * nw):
            e[(a0 + i) & 0x7F] = self.xram.get(0x7600 + a0 + i, 0)
        self.eeprom = bytes(e)
        self.setr(0, a0 + 2 * nw); self.setr(2, 0)
        self.C = 0
        self.ret()
