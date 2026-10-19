# Serial command protocol (DRAFT, not implemented)

Plain printable ASCII, lines ending CR or LF, at the balance's interface settings (9600 baud, 7E1). The balance
needs the RS-232 receive wire (pin 2) connected. Send a newline first: the stock receiver drops the first byte
after boot or wake. Avoid bytes 00, 06 and 16.

## Output (balance to PC)

* Weight lines as now: `S      294.378 g` / `SD ...`
* `T nnnnnn` temperature count (exists today).
* `R wwwwwwww rrrrrr tttttt ffffff nn` (implemented, `--raw`; every pass by default, `--raw-every N`), all hex:
  `w` the 32-bit A/D window sum the interrupt leaves (XRAM 7045..7042, = sum >> 8), `r` the raw value that feeds the
  correction (24 bit, XRAM 749C..749A; with the comparator patch this is its average), `t` the unfiltered temperature
  count (RAM 16h..14h, updated when the balance measures it), `f` the filtered count the compensation uses (XRAM
  7049..7047), `n` the window length in interrupt ticks (24 on V10.45, 20 on V10.50). Raw per window = w * (240/n) / 256.
  The line follows the same gates as the T line: continuous output mode, interface free, output buffer not nearly full.
* Answers to commands start with `=`.

## Commands (PC to balance)

| Command | Meaning |
|---|---|
| `~V` | version line: patch revision, ROM checksum, model code and ID from the EEPROM |
| `~R aa nn` | read nn bytes of the EEPROM mirror from address aa, hex, in chunks over a few passes |
| `~A code` | arm writing (a magic code; disarms after the next write or a timeout) |
| `~S aa hh hh ...` | stage bytes into a 128-byte buffer (XRAM 7700..777F), not written yet |
| `~C` | commit: check the four block checksums and the ID of the staged image (refuse on a mismatch unless `~C!`), write data words first and checksum words last through `CDB7`, read back, report |
| `~X` | restart (jump to the reset code) so the new data take effect |

## Boot guard (planned)

After the EEPROM load at boot, check the four block checksums and the type byte. If any fail, skip the stock
settings sanitiser (which can loop forever on random data) and go into recovery mode: either only the command
interpreter, or a built-in default image loaded into the mirror so the balance stays alive until the PC writes a
proper one.

## Safety rules

Writes only through arm + stage + commit; a failed or interrupted commit leaves the previous image; the PC tool
saves a timestamped dump before every write and on every connection.
