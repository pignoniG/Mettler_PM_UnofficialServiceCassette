"""Run the real V10.45 cassette firmware (boot + main-loop passes) with stubbed hardware.

The interrupt-driven hardware is replaced by per-pass stubs: the A/D window sum (7042..7045), the
control-bar/jumper switches (70D1), serial transmit (drains the 7200 ring like C6D9..C714) and the
EEPROM read (the 7600 mirror is preloaded).
"""
import sys
from cpu51 import CPU51


import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'patches'))
import profiles                      # per-version addresses (main loop wait, EEPROM routines, ...)


class Sim(CPU51):
    def __init__(self, rom, eeprom_fw, version=None):
        super().__init__(rom)
        self.prof = profiles.get(bytes(rom), version)
        self.window = self.prof['window']
        self.eeprom = bytes(eeprom_fw)
        self.sent = []                          # bytes the UART would have sent
        self.sw = 0x00                          # 70D1: bit6 control bar, bit7 jumper line
        self.sfr[0x90] = 0xFF
        self.hist = {}
        self.waits = {}
        self.idle_keys = True
        self.rx_queue = []
        self.pc_hooks = {self.prof['ee_write']: Sim.eeprom_write, self.prof['ee_load']: Sim.eeprom_load}
        self.hold = set()

    def step_hw(self):
        """one instruction; a self-loop on a flag the interrupts would change is broken here"""
        pc = self.pc
        if self.rx_queue and self.cycles % 1000 == 0:          # the UART delivers about one character per 1000 machine cycles (9600 baud)
            self.rx(bytes([self.rx_queue.pop(0)]))
        if self.cycles % 3300 == 0 and self.ram[0x35]:        # transmit: the real UART service is about 300 characters/s
            self.uart_one()
        if pc == self.prof['hex_reset_wait']:                  # after a successful EEPROM load the stock code waits for a
            self.ram[0x1E] = 0                                  # countdown (1Eh, run by the interrupt) and then restarts: time passes
        if pc in self.pc_hooks:
            self.pc_hooks[pc](self); return
        if pc == self.prof['timer_test']:                       # boot timer self-test: both timers must have overflowed
            self.sfr[0x88] |= 0xA0
        op = self.code(pc)
        if op in (0x20, 0x30) and self.code(pc + 1) == 0x48 and pc != self.prof['main_wait']:
            # a loop outside the main loop that polls the window flag 29h.0: the interrupt would set it every 0.16 s
            self.poll29 = getattr(self, 'poll29', 0) + 1
            if self.poll29 % 40 == 0: self.setbit(0x48, 1)
        if op in (0x20, 0x30) and self.rel(self.code(pc + 2), pc + 3) == pc:      # JB/JNB bit,$
            bit = self.code(pc + 1)
            if (op == 0x30) == (not self.getbit(bit)):
                self.hw_wait(bit, want_set=(op == 0x30))
        if op == 0x10 and self.rel(self.code(pc + 2), pc + 3) == pc:
            self.hw_wait(self.code(pc + 1), want_set=True)
        self.step()

    def hw_wait(self, bit, want_set):
        self.waits[bit] = self.waits.get(bit, 0) + 1
        if bit in self.hold and self.pc == self.prof['main_wait']: raise StopIteration(bit)
        self.setbit(bit, 1 if want_set else 0)

    def run_until(self, stop, max_steps=3_000_000):
        n = 0
        while self.pc != stop:
            self.step_hw(); n += 1
            if n > max_steps: raise RuntimeError(f"no progress after {n} steps, pc={self.pc:04X}")
        return n

    def uart_one(self):
        ch = self.xram.get(0x7200 + self.ram[0x36], 0)
        self.sent.append(ch & 0x7F)
        self.ram[0x36] = (self.ram[0x36] + 1) & 0x7F
        self.ram[0x35] -= 1

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
            if self.pc == self.prof['main_wait'] and not self.getbit(0x48): return n
        raise RuntimeError("boot did not reach the main loop")

    def window_sum(self, raw):
        """the 40-bit window sum the A/D interrupt leaves for a given raw value (raw = (sum >> 8) * (240 / window) / 256)"""
        return int(raw * 256 * self.window / 240) << 8

    def set_window(self, s_value):
        """what the A/D interrupt leaves for the main loop: the 40-bit window sum at 7041..7045"""
        for i in range(5): self.xram[0x7041 + i] = (s_value >> (8 * i)) & 0xFF

    def one_pass(self, s_value=None, sw=None, max_steps=400_000, trace=False, temp=None):
        """run the main loop for one measurement window: sets the flags the ISR would, runs until the loop
        waits again; returns the visited pc list if trace"""
        if s_value is not None: self.set_window(s_value)
        if temp is not None:                  # a new temperature result as the interrupt leaves it: 14h..16h, flag 2Ah.5
            self.ram[0x14], self.ram[0x15], self.ram[0x16] = temp & 0xFF, (temp >> 8) & 0xFF, (temp >> 16) & 0xFF
            self.setbit(0x55, 1)
        if sw is not None: self.sw = sw
        self.xram[0x70D1] = self.sw
        if self.getbit(0x4E) and self.idle_keys: self.ram[0x25] = 0      # standby: the bar scan leaves 25h = 0 when untouched
        self.setbit(0x48, 1)                  # 29h.0: window ready
        self.hold = {0x48}
        seen = []
        n = 0
        while True:
            if self.pc == self.prof['main_wait'] and not self.getbit(0x48) and n > 0: break
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

    # ---------------- serial receive (what the interrupt does with a character, C768..C7B6)
    def rx(self, data):
        """feed bytes from the PC: the receiver is locked after boot/wake (27h.5) and the first byte unlocks it
        and is dropped (the ring is seeded with 96h); then characters go into the ring at 7280 (38h count,
        39h read index, 3Ah write index).  06h (ACK) and 16h (SYN) have their own meaning.  Parity is assumed good."""
        for b in data:
            if self.getbit(0x3D):                                   # 27h.5: locked
                self.setbit(0x4B, 0)                                # CLR 29h.3
                self.setbit(0x3D, 0)                                # P3.0 (RXD) high: unlock, drop this byte
                self.xram[0x7280] = 0x96; self.ram[0x38] = 1; self.ram[0x39] = 0; self.ram[0x3A] = 1
                continue
            if b == 0: self.setbit(0x3D, 1); continue
            c = b & 0x7F
            if c == 0x06:
                if self.getbit(0x0F): self.setbit(0x54, 1); self.setbit(0x4B, 1)
                continue
            if c == 0x16:
                self.xram[0x7280] = 0x96; self.ram[0x38] = 1; self.ram[0x39] = 0; self.ram[0x3A] = 1; continue
            if self.ram[0x38] < 0x3F:
                self.xram[0x7280 + self.ram[0x3A]] = c
                self.ram[0x3A] = (self.ram[0x3A] + 1) & 0x3F; self.ram[0x38] += 1

    def rx_async(self, data):
        """queue bytes that arrive at line speed while the firmware runs (a long command loop reads them as they come)"""
        self.rx_queue += list(data)
