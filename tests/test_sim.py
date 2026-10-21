"""
Simulator tests: run the real firmware (your own dump) with stubbed hardware.

    PM_ROM=/path/V10.45.bin PM_ROM_1050=/path/V10.50.bin PM_EEPROM=/path/dump.BIN python3 -m unittest discover -s tests

PM_ROM      the unmodified V10.45 image (32 KiB)
PM_ROM_1050 the unmodified V10.50 image (optional; its tests are skipped without it)
PM_EEPROM a 128-byte 93C46 dump of a balance (programmer byte order is detected; byte 0x01 gets the
          continuous-output bit set so the simulated balance streams its readings)
Without these the tests are skipped: the repository contains no Mettler data.
"""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [os.path.join(ROOT, 'sim'), os.path.join(ROOT, 'patches'), ROOT]
import fwsim, profiles, rom_comparator, rom_temperature, rom_standby_weigh, rom_service

EEP = os.environ.get('PM_EEPROM')


def load_eeprom(path):
    """128 bytes in firmware order.  Dumps from an x16 programmer have the two bytes of every word swapped;
    the right order is the one with the type-check byte (0x60..0x67) at address 0x68 and a valid
    type-block checksum (0xA6 + sum of 0x04..0x2D = 0 mod 256)."""
    b = open(path, 'rb').read()
    assert len(b) == 128, 'expected a 128-byte dump'
    for e in (bytes(b), bytes(b[i ^ 1] for i in range(128))):
        if (e[0x68] & 0xF8) == 0x60 and (0xA6 + sum(e[0x04:0x2E])) & 0xFF == 0:
            return e
    raise AssertionError('not a recognisable PM EEPROM dump in either byte order')


def lines(sent):
    return [l for l in bytes(sent).replace(b'\x26', b'').replace(b'\x00', b'').split(b'\r\n') if l]     # pause bytes out


class SimTests(object):                       # run once per firmware version, see the subclasses at the end
    ROM_ENV = None; VERSION = None

    @classmethod
    def setUpClass(cls):
        cls.stock = open(os.environ[cls.ROM_ENV], 'rb').read()
        assert profiles.detect(cls.stock) == cls.VERSION, 'wrong firmware version in ' + cls.ROM_ENV
        e = bytearray(load_eeprom(EEP)); e[0x01] |= 0xC0           # interface: continuous
        cls.eeprom = bytes(e)
        cls.x0 = e[0x40] | e[0x41] << 8 | e[0x42] << 16            # raw reference point

    def sim(self, rom, eeprom=None):
        s = fwsim.Sim(rom, eeprom or self.eeprom); s.boot(); return s

    def pass_(self, s, grams=0.0, sw=0x80, raw_per_g=1548):
        s.one_pass(s_value=s.window_sum(self.x0 + raw_per_g * grams), sw=sw)
        out = lines(s.sent); s.sent.clear(); return out

    def settle(self, s, n=130):
        out = []
        for _ in range(n): out += self.pass_(s)
        return out

    def test_stock_boots_and_prints_version(self):
        out = self.settle(self.sim(self.stock))
        self.assertTrue(any(l.startswith(b'STANDARD') for l in out), out[:3])
        self.assertTrue(any(l.startswith(b'S') and l.endswith(b' g') for l in out))

    def test_patched_normal_mode_matches_stock(self):
        a, b = self.sim(self.stock), self.sim(rom_standby_weigh.build(self.stock, False, True)[0])
        self.settle(a); self.settle(b)
        for g in (0, 10, 25):
            la = [self.pass_(a, g) for _ in range(40)][-1]
            lb = [self.pass_(b, g) for _ in range(40)][-1]
            self.assertEqual(la, lb, g)

    def test_stock_is_silent_with_the_bar_up_patched_is_not(self):
        for rom, want in ((self.stock, 0), (rom_standby_weigh.build(self.stock, False, True)[0], 40)):
            s = self.sim(rom); self.settle(s)
            n = sum(1 for _ in range(40) for l in self.pass_(s, 10, 0xC0) if l.startswith(b'S'))
            self.assertEqual(n, want)

    def test_bar_up_reading_equals_normal_reading(self):
        s = self.sim(rom_standby_weigh.build(self.stock, False, True)[0]); self.settle(s)
        normal = [l for _ in range(40) for l in self.pass_(s, 10)][-1]
        up = [l for _ in range(40) for l in self.pass_(s, 10, 0xC0)][-1]
        self.assertEqual(normal, up)

    def test_wake_without_tare_when_start_up_is_skipped(self):
        s = self.sim(rom_standby_weigh.build(self.stock, False, True)[0]); self.settle(s)
        for _ in range(2): self.pass_(s, 10, 0xC0)
        out = []
        for _ in range(300): out += self.pass_(s, 10)           # the bar-down poll times out in the simulator: a wake
        self.assertFalse(any(l.startswith(b'TA') for l in out))
        self.assertTrue([l for l in out if l.startswith(b'S')][-1].strip().endswith(b'g'))
        self.assertNotIn(b'0.000', [l for l in out if l.startswith(b'S')][-1])

    def test_temperature_and_comparator_images_run(self):
        rom = rom_comparator.build(self.stock)[0]
        rom = rom_temperature.build(rom, 10)[0]                  # a T line every 10 passes keeps the test short
        rom = rom_standby_weigh.build(rom, False, True)[0]
        s = self.sim(rom); out = self.settle(s, 220)
        self.assertTrue(any(l.startswith(b'T ') for l in out), 'no T line')
        self.assertTrue(any(l.startswith(b'S') for l in out))

    def test_error_states_keep_the_main_loop_running(self):
        rom = rom_standby_weigh.build(self.stock, False, True)[0]
        good = bytearray(self.eeprom)
        for name, mutate in (('all FF', lambda b: bytearray([0xFF] * 128)),
                             ('all 00', lambda b: bytearray(128)),
                             ('bad cell checksum', lambda b: (b.__setitem__(0x67, b[0x67] ^ 0x55), b)[1]),
                             ('bad type checksum', lambda b: (b.__setitem__(0x2D, b[0x2D] ^ 0x55), b)[1])):
            s = fwsim.Sim(rom, bytes(mutate(bytearray(good)))); s.boot()
            visits = [0]; step = s.step_hw
            def counted():
                if s.pc == s.prof['tail']: visits[0] += 1
                step()
            s.step_hw = counted
            for _ in range(30): self.pass_(s)
            self.assertGreaterEqual(visits[0], 30, name)               # the pass-tail hook is reached on every pass
            self.assertNotEqual(s.ram[0x3D], 0, name)                  # and the error code is set

    def test_raw_line_reports_what_the_interrupt_left(self):
        import re
        rom = rom_temperature.build(self.stock, 0, None, True, 1)[0]
        s = self.sim(rom); w = s.window; ext = round(150 / w)
        RL = re.compile(rb'^R ([0-9A-F]{8}) ([0-9A-F]{6})(?: ([0-9A-F]{6}) ([0-9A-F]{6}) ([0-9A-F]{2}))?$')
        for _ in range(130): s.one_pass(s_value=s.window_sum(self.x0), sw=0x80, temp=300000); s.sent.clear()
        got = []; per_pass = []
        for i in range(36):
            s.one_pass(s_value=s.window_sum(self.x0 + 1548 * 10), sw=0x80, temp=300000 + i)
            ls = lines(s.sent); s.sent.clear()
            ms = [RL.match(l) for l in ls if l.startswith(b'R')]
            per_pass.append(len(ms)); got += ms
        self.assertEqual(per_pass, [1] * 36)                              # exactly one R line per pass
        longs = [m for m in got if m.group(3)]
        self.assertEqual(len(longs), 36 // ext)                           # the temperature fields every ext-th line
        wsum, raw = int(got[-1].group(1), 16), int(got[-1].group(2), 16)
        self.assertEqual(wsum, s.window_sum(self.x0 + 1548 * 10) >> 8)    # the 32-bit window sum
        self.assertEqual(raw, (wsum * (240 // w)) >> 8)                   # the normaliser's output
        tun, tfi, win = (int(longs[-1].group(i), 16) for i in (3, 4, 5))
        self.assertTrue(300000 <= tun < 300036)                           # unfiltered temperature
        self.assertTrue(tun - 12 < tfi <= tun)                            # filtered temperature follows it
        self.assertEqual(win, w)
        self.assertLessEqual(max(len(l) for l in ls), 40)                 # lines stay short: the UART does ~300 characters/s

    def test_model_matches_the_firmware(self):
        sys.path.insert(0, os.path.join(ROOT, 'tools'))
        import model_check
        try: model_check.find_pm_eeprom()
        except SystemExit: self.skipTest('pm_eeprom.py of the EEPROM tool not found (set PM_TOOL)')
        for dt in (5000, -20000):
            rows = model_check.run(self.stock, EEP, dt, (0, 100, 2000, 4000))
            for g, raw, tf, shown, pred in rows:
                self.assertIsNotNone(shown, (dt, g))
                self.assertLessEqual(abs(shown - pred), 1.0, (dt, g))

    # ---- service commands over the serial line (stock Intel-HEX dump and load behind XD / XL)
    def _service(self):
        rom = rom_service.build(self.stock)[0]
        def new(chip=None):
            s = fwsim.Sim(rom, chip or self.eeprom); s.boot()
            for _ in range(130): s.one_pass(s_value=s.window_sum(self.x0), sw=0x80, temp=300000); s.sent.clear()
            s.rx(b'\n')                                              # unlock the receiver (this byte is dropped)
            for _ in range(14): s.one_pass(s_value=s.window_sum(self.x0), sw=0x80, temp=300000); s.sent.clear()
            return s
        return new

    @staticmethod
    def _records(img):
        def rec(a, d):
            b = bytes([len(d), a >> 8, a & 255, 0]) + bytes(d)
            return (':' + b.hex().upper() + '%02X' % ((-sum(b)) & 255) + '\r\n').encode()
        return b''.join(rec(a, img[a:a + 8]) for a in range(0, 128, 8)) + b':00000001FF\r\n'

    @staticmethod
    def _fix(img):
        img = bytearray(img)
        for lo, hi in ((0x04, 0x2D), (0x2E, 0x31), (0x3E, 0x67)):
            img[hi] = 0; img[hi] = (-(0xA6 + sum(img[lo:hi]))) & 255
        return bytes(img)

    def _talk(self, s, cmd, passes=14, feed=b'', stop=None):
        s.sent.clear(); s.rx(cmd)
        if feed: s.rx_async(feed)
        out = b''; chip0 = bytes(s.eeprom)
        for _ in range(passes):
            try:
                s.one_pass(s_value=s.window_sum(self.x0), sw=0x80, temp=300000, max_steps=3_000_000)
            except RuntimeError:                                       # after a successful load the stock code restarts the
                if bytes(s.eeprom) != chip0: break                     # firmware; the simulator does not model what follows
                raise
            out += bytes(s.sent); s.sent.clear()
            if stop and not s.rx_queue and stop(out, s): break
        txt = out.replace(b'\x26', b'').replace(b'\x00', b'').replace(b'\x4b', b'')           # pause characters
        return [l for l in txt.split(b'\r\n') if l and not l.startswith(b'S ')]

    def _dump(self, s):
        img = bytearray()
        for l in self._talk(s, b'XD\r\n'):
            if l.startswith(b':') and l != b':00000001FF': img += bytes.fromhex(l[9:25].decode())
        return bytes(img)

    def _load(self, s, img):
        return self._talk(s, b'XL\r\n', 14, self._records(img), lambda o, s_: b'EL' in o or s_.eeprom != self.eeprom)

    def test_service_version_and_dump(self):
        new = self._service(); s = new()
        v = self._talk(s, b'XV\r\n')
        self.assertTrue(v and v[0].startswith(b'XV SVC ' + self.VERSION.encode()), v)
        dump = self._dump(s)
        self.assertEqual(len(dump), 128)
        # the boot normalises a few user-setting bytes (36h..39h here), everything else is the chip
        diff = [i for i in range(128) if dump[i] != self.eeprom[i]]
        self.assertTrue(set(diff) <= {0x00, 0x01, 0x02, 0x03, 0x32, 0x33, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39}, diff)
        self.assertEqual(self._talk(new(), b'XQ\r\n'), [b'ES'])

    def test_service_write_needs_arming_checksums_and_id(self):
        new = self._service()
        s = new(); img = self._dump(s)
        mod = bytearray(img); mod[0x20] ^= 1; mod = self._fix(mod)
        # not armed: refused, chip untouched
        s = new(); self.assertEqual(self._load(s, mod)[:1], [b'EL']); self.assertEqual(s.eeprom, self.eeprom)
        # armed, valid: written
        s = new(); self.assertEqual(self._talk(s, b'XA\r\n'), [b'XA'])
        self._load(s, mod); self.assertEqual(s.eeprom[0x20], mod[0x20]); self.assertNotEqual(s.eeprom[0x20], self.eeprom[0x20])
        # armed, bad block checksum: refused
        bad = bytearray(mod); bad[0x40] ^= 1
        s = new(); self._talk(s, b'XA\r\n'); self.assertEqual(self._load(s, bad)[-1:], [b'EL']); self.assertEqual(s.eeprom, self.eeprom)
        # armed, another balance's ID: refused with XA, accepted with XB
        other = bytearray(mod); other[0x0E] ^= 0x55; other = self._fix(other)
        s = new(); self._talk(s, b'XA\r\n'); self.assertEqual(self._load(s, other)[-1:], [b'EL']); self.assertEqual(s.eeprom, self.eeprom)
        s = new(); self._talk(s, b'XB\r\n'); self._load(s, other); self.assertEqual(s.eeprom[0x0E], other[0x0E])


def _skip(env):
    return unittest.skipUnless(os.environ.get(env) and os.path.exists(os.environ[env]) and EEP and os.path.exists(EEP),
                               f'set {env} and PM_EEPROM')


@_skip('PM_ROM')
class SimTestsV1045(SimTests, unittest.TestCase):
    ROM_ENV = 'PM_ROM'; VERSION = '10.45'


@_skip('PM_ROM_1050')
class SimTestsV1050(SimTests, unittest.TestCase):
    ROM_ENV = 'PM_ROM_1050'; VERSION = '10.50'


if __name__ == '__main__':
    unittest.main()
