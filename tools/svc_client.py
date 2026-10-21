#!/usr/bin/env python3
"""
PC side of the service commands (patches/rom_service.py): read and write the balance's parameter EEPROM over the
serial line.  9600 baud, 7 data bits, even parity, 1 stop bit (the balance's I-FACE settings), RS-232 levels.

    svc_client.py [-p PORT] version
    svc_client.py [-p PORT] dump  out.BIN [--firmware-order]
    svc_client.py [-p PORT] load  in.BIN  [--override-id] [--no-verify]
    svc_client.py [-p PORT] restart

dump writes the 128 bytes the way an EEPROM programmer saves a 93C46 in x16 mode (the two bytes of every word
swapped, the format of the dumps of Mettler_PM_UnofficialServiceTool), or in the balance's own order with
--firmware-order.  load accepts either order, checks the type-check byte and the three block checksums itself
before sending anything, arms the write on the balance (XA, or XB with --override-id for a board swap or a blank
chip), streams the 16 Intel HEX records and, unless --no-verify, reads the chip back afterwards.  The balance
restarts by itself after a successful write, into the -OFF- standby display: wake it with the bar before talking to it again.  A write replaces the whole EEPROM including the user calibration:
dump first.
"""
import argparse, re, sys, time

STREAM = re.compile(rb'^(R [0-9A-F]{8} [0-9A-F]{6}( [0-9A-F]{6} [0-9A-F]{6} [0-9A-F]{2})?|T \d+|S[DI]? +[-+]?\d*\.?\d* ?g?|S[DI][+-]|SI[+-])$')
PAUSE = bytes([0x00, 0x16, 0x1A, 0x26, 0x4B])          # pause / sync characters the interface adds between lines


class ServiceError(Exception):
    pass


class Client:
    def __init__(self, port, log=None):
        self.p = port; self.buf = b''; self.log = log or (lambda *a: None)

    # ---- line level
    def _fill(self, timeout):
        t0 = time.time()
        while time.time() - t0 < timeout:
            d = self.p.read(256)
            if d:
                self.buf += d; return True
        return False

    def lines(self, timeout=2.0, until=None):
        """collect lines for up to timeout seconds (or until a line satisfies `until`), dropping the weight / R / T stream"""
        out = []; t0 = time.time()
        while time.time() - t0 < timeout:
            while b'\n' in self.buf:
                raw, self.buf = self.buf.split(b'\n', 1)
                l = raw.replace(b'\r', b'').strip(PAUSE + b' ')
                if l and not STREAM.match(l):
                    out.append(l); self.log('<', l)
                    if until and until(l): return out
            self._fill(0.2)
        return out

    def send(self, data):
        self.log('>', data); self.p.write(data)

    def unlock(self):
        """the receiver is locked after boot or wake and drops the first byte: send a newline and let things settle"""
        self.send(b'\n'); time.sleep(0.4); self.buf = b''; self.lines(0.6)

    # ---- commands
    def command(self, cmd, expect, timeout=3.0):
        self.send(cmd + b'\r\n')
        got = self.lines(timeout, until=lambda l: l.startswith(expect))
        for l in got:
            if l.startswith(expect): return l
        raise ServiceError(f'no answer {expect!r} to {cmd!r}; got {got[:4]}')

    def version(self):
        return self.command(b'XV', b'XV SVC').decode()

    def dump(self):
        """-> 128 bytes in the balance's order"""
        self.send(b'XD\r\n'); data = bytearray(); seen_eof = False
        for l in self.lines(8.0, until=lambda l: l.startswith(b':00000001FF')):
            if l == b':00000001FF': seen_eof = True; break
            if not l.startswith(b':'): continue
            try: raw = bytes.fromhex(l[1:].decode())
            except ValueError: raise ServiceError(f'garbled record {l!r}')
            if sum(raw) & 0xFF or raw[0] != 8 or raw[3] != 0 or (raw[1] << 8 | raw[2]) != len(data):
                raise ServiceError(f'bad record {l!r}')
            data += raw[4:12]
        if not seen_eof or len(data) != 128: raise ServiceError(f'incomplete dump ({len(data)} bytes)')
        return bytes(data)

    def load(self, img, override_id=False, verify=True):
        if len(img) != 128: raise ServiceError('need 128 bytes')
        check_image(img)
        self.command(b'XB' if override_id else b'XA', b'XB' if override_id else b'XA')
        self.send(b'XL\r\n')
        got = self.lines(3.0, until=lambda l: l == b'XL' or l.startswith(b'EL'))
        if b'XL' not in got: raise ServiceError(f'the balance did not accept the load: {got[:3]} (not armed?)')
        for l in hex_records(img):
            self.send(l); time.sleep(0.03)                    # 29 characters at 9600 baud take 30 ms
        res = self.lines(8.0, until=lambda l: l.startswith(b'E'))
        if not res or res[-1].startswith(b'EL'): raise ServiceError(f'the balance refused the image: {res[-2:]} (checksum, ID or timing)')
        self.log('load accepted; the balance restarts about 3 s after the write:', res)
        if verify:
            time.sleep(6.0); self.buf = b''
            try:
                self.unlock(); back = self.dump()
            except ServiceError:
                return 'asleep'                                 # the balance restarts into -OFF- and is silent until the bar wakes it
            diff = [i for i in range(128) if back[i] != img[i]]
            return diff                                         # bytes the balance changed on its own (boot normalises some settings)
        return None

    def restart(self):
        self.command(b'XR', b'XR')


# ---------------------------------------------------------------- image helpers
def hex_records(img):
    out = []
    for a in range(0, 128, 8):
        b = bytes([8, a >> 8, a & 255, 0]) + bytes(img[a:a + 8])
        out.append((':' + b.hex().upper() + '%02X' % ((-sum(b)) & 255) + '\r\n').encode())
    return out + [b':00000001FF\r\n']


def block_ok(img, lo, hi):
    return (0xA6 + sum(img[lo:hi + 1])) & 0xFF == 0


def check_image(img):
    if (img[0x68] & 0xF8) != 0x60: raise ServiceError('byte 0x68 is not a type-check byte (0x60..0x67): wrong byte order?')
    for lo, hi, name in ((0x04, 0x2D, 'type'), (0x2E, 0x31, 'span'), (0x3E, 0x67, 'cell')):
        if not block_ok(img, lo, hi): raise ServiceError(f'{name} block checksum ({lo:02X}..{hi:02X}) is wrong')


def to_firmware_order(raw):
    """a file of 128 bytes: firmware order, or programmer order (two bytes of each word swapped)"""
    for cand in (bytes(raw), bytes(raw[i ^ 1] for i in range(128))):
        if (cand[0x68] & 0xF8) == 0x60 and block_ok(cand, 0x04, 0x2D): return cand
    raise ServiceError('not a recognisable PM EEPROM image in either byte order')


def to_programmer_order(img):
    return bytes(img[i ^ 1] for i in range(128))


def open_port(name=None):
    import serial, serial.tools.list_ports
    if not name:
        c = [p.device for p in serial.tools.list_ports.comports() if any(k in p.device.lower() for k in ('usb', 'ttyacm', 'com'))]
        if not c: sys.exit('no serial port found: give one with -p')
        name = c[0]
    return serial.Serial(name, 9600, bytesize=7, parity='E', stopbits=1, timeout=0.2)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('-p', '--port'); ap.add_argument('-v', '--verbose', action='store_true')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('version'); sub.add_parser('restart')
    d = sub.add_parser('dump'); d.add_argument('out'); d.add_argument('--firmware-order', action='store_true')
    l = sub.add_parser('load'); l.add_argument('inp'); l.add_argument('--override-id', action='store_true'); l.add_argument('--no-verify', action='store_true')
    a = ap.parse_args()
    c = Client(open_port(a.port), (lambda *x: print(*x)) if a.verbose else None)
    try:
        c.unlock()
        if a.cmd == 'version': print(c.version())
        elif a.cmd == 'restart': c.restart(); print('restarting')
        elif a.cmd == 'dump':
            img = c.dump(); check_image(img)
            open(a.out, 'wb').write(img if a.firmware_order else to_programmer_order(img))
            print(f'EEPROM dumped to {a.out} ({"firmware" if a.firmware_order else "programmer"} order), ID bytes {img[0x0E]:02X}{img[0x0F]:02X}{img[0x10]:02X}')
        elif a.cmd == 'load':
            img = to_firmware_order(open(a.inp, 'rb').read())
            diff = c.load(img, a.override_id, not a.no_verify)
            if diff is None: print('image sent and accepted (not verified)')
            elif diff == 'asleep': print('image accepted; the balance restarted into -OFF- and does not talk until woken: press the control bar, then run `dump` to verify')
            elif not diff: print('image written and verified: the chip now equals the file')
            else: print('image written; bytes the balance normalised itself:', [hex(i) for i in diff])
    except ServiceError as ex:
        sys.exit(f'error: {ex}')


if __name__ == '__main__':
    main()
