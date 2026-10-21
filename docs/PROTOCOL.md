# Serial command protocol

Plain printable ASCII, lines ending CR or LF, at the balance's interface settings (9600 baud, 7E1). The balance
needs the RS-232 receive wire (pin 2) connected. Send a newline first: the stock receiver drops the first byte
after boot or wake. Avoid bytes 00, 06 and 16.

## Output (balance to PC)

* Weight lines as now: `S      294.378 g` / `SD ...`
* `T nnnnnn` temperature count (exists today).
* `R wwwwwwww rrrrrr` (implemented, `--raw`; every pass by default, `--raw-every N`), hex: `w` the 32-bit A/D window sum
  the interrupt leaves (XRAM 7045..7042, = sum >> 8), `r` the raw value that feeds the correction (24 bit, XRAM
  749C..749A; with the comparator patch this is its average). Raw per window = w * (240/n) / 256. About once a second
  (`--raw-ext-every N`) the line is longer: `R wwwwwwww rrrrrr tttttt ffffff nn`, adding `t` the unfiltered temperature
  count (RAM 16h..14h, updated when the balance measures it), `f` the filtered count the compensation uses (XRAM
  7049..7047) and `n` the window length in interrupt ticks (24 on V10.45, 20 on V10.50). The line follows the same
  gates as the T line: continuous output mode, interface free, output buffer not nearly full.

  **Why the line is short.** The balance's UART service is only about 300 characters/s (measured: a first version with
  a 37-byte R line every pass, 55 bytes per pass with the weight line, stretched the passes from 0.16 s to 0.19 s on
  average and skipped 15 % of the A/D windows, 290 bytes/s). Keep the bytes per pass below about 45 (0.16 s x 300),
  or the output blocks the main loop. The compact line averages 40 bytes per pass including the 17-byte weight line.

## Commands (PC to balance) - `--service`

Implemented by `patches/rom_service.py`, on top of the stock host-command interpreter: the letter `X` selects a
handler of the cassette (stock table entry at 8F8E + 'X'-'@'), the next letter picks the command. Lines end CR LF.
Unknown letters answer `ES`.

| Command | Answer | Meaning |
|---|---|---|
| `XV` | `XV SVC 10.45 ID 874543` | service patch present, firmware version, the three ID bytes of the EEPROM mirror |
| `XD` | 16 Intel HEX records, then `:00000001FF` | dump of the 7600 mirror (128 bytes, firmware byte order, 8 bytes per record) |
| `XA` | `XA` | arm one write that is checked against the ID of the chip |
| `XB` | `XB` | arm one write without the ID check (board swap, blank or corrupt chip) |
| `XL` | `XL`, then after the 17 records `EA` | load: the balance answers `XL` when it is armed and ready, then takes 16 data records of 8 bytes plus the EOF record. Without arming it answers `EL` and does nothing. |
| `XR` | `XR` | restart (reset vector), the new data take effect |

`XL` is the stock HEX loader with two additions: arming, and the ID guard (patched in at the loader's start): the ID
bytes 0E..10 of the new image must equal those of the chip unless armed with `XB`. The loader itself checks the three
block checksums (04..2D, 2E..31, 3E..67) and refuses a bad image with `EL` before touching the chip. Arming is
cleared after each load. After a successful write the balance restarts by itself about 3 s later, and comes up in
the `-OFF-` standby display; its serial output is silent until the control bar wakes it.

Notes from the real balance (PM4600, V10.45): the balance answers `ES` to the first newline after boot or wake (the
receiver lock), the dump takes about 1 s, and the firmware rewrites bytes 0x36 and 0x39 at every restart. A write of
the whole dump and a restore of the original worked, and a changed byte 0x7F was read back correctly.

## Boot guard (planned)

After the EEPROM load at boot, check the four block checksums and the type byte. If any fail, skip the stock
settings sanitiser (which can loop forever on random data) and go into recovery mode: either only the command
interpreter, or a built-in default image loaded into the mirror so the balance stays alive until the PC writes a
proper one.

## Safety rules

A write needs arming first and the image must pass the block checksums; the ID must match unless armed with `XB`. The
stock loader writes the chip after the EOF record, so a link that breaks before it leaves the chip unchanged. The PC
tool refuses an image whose checksums or type byte are wrong before sending anything. Always dump before a load.

## What the stock ROM already has (found 2026-10-21, V10.45)

* A host command interpreter. The first character of a line selects a handler through the jump table at 8F8E
  (index = letter - '@'; unused entries give `ES`); answers `ES` (syntax), `EL` (logical), `ET` (transmission), `SI+` /
  `SI-` (overload). On the real balance: `S`, `T`, `D` are accepted, `x`, `Y`, `A`, `I0`..`I4`, `IR` give `ES`. The
  receiver is locked after boot/wake and drops the first byte, so send a newline first. **`CA` appears to start a
  calibration; do not send it.**
* An EEPROM dump as Intel HEX (routine 90FD): 16 records `:08aa0000dd..ddcc` of 8 bytes from the 7600 mirror, then
  `:00000001FF`. And a HEX load (routine 8FCE): 16 records of 8 bytes (address 00, sequential, type 00, valid
  checksums) into a buffer at 7500..757F, then the EOF record; it checks the three block checksums
  (04..2D, 2E..31, 3E..67), copies the buffer to the 7600 mirror and writes the chip with CDB7; any error gives `EL`.
  Nothing in the V10.45 image calls either routine (no reference to 90FD or 8FCE), so they are dead code here; the
  EOF string is also present in V10.50 (C574). The command module therefore only has to call them and add the
  guards below (ID check, arming).
