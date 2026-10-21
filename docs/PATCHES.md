# Patches for V10.45 and V10.50 (ME-34172 standard program cassette)

All patches are applied to a 32 KiB image with a valid checksum (the byte sum is 0 mod 256). Each one checks
the bytes it replaces, uses only free filler (0xFF) and fixes the checksum at F7FF. They can be combined.

## Firmware versions

`patches/profiles.py` lists, per version, the addresses of every hook site and every firmware routine the
patches call. They were found by matching instruction patterns between the images with the absolute
addresses masked; every site matched uniquely (12 instructions). RAM, XRAM and bit addresses (flags, the
temperature count at XRAM 7047, the EEPROM mirror at 7600) are the same in both versions, and the XRAM and
ROM areas the patches use are unused in both.

| | V10.45 | V10.50 |
|---|---|---|
| A/D window (interrupt ticks, per weighing pass) | 24 (0.160 s) | 20 (0.133 s) |
| comparator hook `CC11` (normaliser) | CC11 | CB22 |
| temperature call / routine | 827B / C887 | 8275 / C7E7 |
| pass tail | 8876 | 886A |
| bar-up dispatch / stage 1 | 831A / 8395 | 830E / 8389 |
| wake: arms start-up | 836E | 8362 |
| calibration timeouts (passes) | 93B6: 48, 9AB0: 72 | 91AE: 64, 99CD: 96 |
| free ROM | E72E..FF7F | E6C6..FF7F |

Differences that matter:

* The window is shorter in V10.50. The comparator converts its restart threshold (120 mg) with the window
  length, and the temperature line's default interval is 75 passes against 62, both about 10 s. The comparator
  ring holds the same number of windows, so its averaging time is 0.83 times as long as in V10.45.
* The V10.50 temperature routine sets error code 4 on a range failure without raising the error display flag.
* V10.50 has the resolution-aware interrupt filter ladder. It is not part of the patches and the simulator does
  not model the interrupt filter, so the settling behaviour of the combination is untested.

## Memory map used

| Area | Used by |
|---|---|
| ROM E800..E97C (E730..E94A with `--raw`) | `--temperature` / `--raw` |
| ROM E980..EA38 | `--standby-weigh` (and `--skip-startup`) |
| ROM F000..F368 | `--comparator` |
| XRAM 7090..70AF, ring 7682..76FF | `--comparator` |
| XRAM 70E8..70EE | `--temperature` (T counter 70E8, register save 70E9..70EC, R counter 70ED, long-R counter 70EE) |
| XRAM 70F0..70F8 | `--standby-weigh` (state, saved poll state, key events, first-wake flag, inhibit counter) |

Still free: about 5 KB of ROM, XRAM 7700..77EF.

## --comparator

Hook on `CC11` (the raw-value normaliser, called once per pass from the weighing path). Each pass the finished
window sum (7042..7045, 0.16 s of A/D counts) is stored as a 16-bit difference from a base value in a ring.
The output is the average of the newest half of the slots collected since the last restart, scaled exactly as
the stock routine. The average restarts when a window differs from it by more than 120 mg, so a new load shows at
single-window precision at once and refines over seconds. Windows per slot and slot count depend on the vibration
adapter setting (`--table`). The calibration and stability timeouts (26h countdowns at 93B6 and 9AB0) are raised
to 150 passes so calibration does not give error 1.

## --temperature

(Since the `--raw` change the T-line code is laid out differently, so a rebuild is no longer byte-identical to the v6 image that was first flashed and tested; that image is what commit `c8b0800` builds. The simulator tests cover both layouts.)

Hook at 827B (`LCALL C887`, the temperature routine, called once per pass from the common tail). In continuous
output mode, when the interface is free and the output buffer is not nearly full, it sends `T nnnnnn` + CR LF
every N passes through the firmware's own character routine `D6FC` (so line ending and parity follow the
settings). The number is the 24-bit filtered count at XRAM 7047..7049 (about 2000 counts per degree C; the filter
is a first-order low-pass with factor 1/4 per update).

## --raw

An addition to the temperature patch (same hook, same gates): an `R` line every pass (or every N) with the
window sum and the normaliser output, and about once a second also both temperature counts and the window
length, in hex; see PROTOCOL.md. The line is short on purpose: the UART serves about 300 characters/s. The
patch grows from 381 to 539 bytes and then starts at E730 instead of E800 (it still ends before
the standby-weigh patch at E980). The hook is in the common pass tail, so the line is also produced with the
display off.

`tools/model_check.py` feeds raw values and temperature offsets to the simulated firmware, reads the R lines and
the displayed weights and compares the weight differences with the correction formula of EEPROM_MAP.md section 3
(zero, span and linearity as quadratics of temperature): the difference is 0.0 mg for 0..4000 g at temperature
offsets of -20000, +5000 and +24000 counts on both V10.45 and V10.50. One thing the check showed: the firmware
reports overload (`SI+`) when the *gross* value, including the pan, is too large, so the raw value of the empty
pan matters in a simulation (X0 is the reference point of the linearity term, not the zero load).

## --standby-weigh

See the header of `patches/rom_standby_weigh.py`. In short: the real standby (control bar up) repeats the
stage-1 code at 831A->8395 every pass and sets flag 27h.0, which turns the serial output and the RS-232 driver
off. The patch lets stage 1 run with 27h.0 clear, then runs the normal weighing path in the common pass tail
(8876), hides the key events from it, restores the standby poll's own variables and re-blanks the display. The
bar-down poll state is untouched.

## --service

`patches/rom_service.py`, code at EB00 (about 290 bytes), XRAM 70EF (arming state) and 70F9 (saved value). The
dead stock routines are reused: the HEX dump (V10.45 90FD, V10.50 C535) and HEX load (8FCE, C3FC). Hooks: the
jump-table entry for `X` of the stock host-command interpreter (8F8E table) points to the cassette's handler, which
matches `D L A B V R` + CRLF by calling the stock matcher (B7EB / B742) and prints replies with the stock print
routine; and the three bytes at the loader's start (hex_guard, 9080 / C4AE, `75 82 00`) are replaced by a call that
compares the ID bytes 0E..10 of the incoming image with the chip unless armed with `XB`, and on a mismatch leaves
through the loader's error exit (902D / C45B, `EL`). `XR` waits for the output buffer to empty and jumps to the
reset vector. Tested in the simulator on both versions (arming, bad checksum, ID mismatch, override, persistence
across a simulated power cycle) and on a PM4600 (V10.45) with a full dump, write and restore.

## --skip-startup

Hook at 836E (end of the wake-up init, where the start-up sequence is armed), at 8314 (start of the normal
weighing pass, to drop the tare-switch event 2Eh.0 for 20 passes after a skipped start-up). The first wake after
power-on keeps the stock start-up. The skipped sequence's end-of-start-up init (8AA3) is run directly; its
switch-on zero request (22h.1) is deliberately not set.

## Findings that matter for further patches

* Key events are edges accumulated by the interrupt in 2Eh (bit 0 = tare switch pressed, a falling edge of 34h.0);
  2Ch holds the previous state. A press seen by the weighing path is a tare.
* The standby poll (E54F..) keeps its state in 25h, 26h, 64h, 65h, 66h; the weighing path uses the same
  locations for display flags and the filter depth.
* The stock RX path locks the receiver after boot/wake until the first character passes a line-idle check
  (27h.5, the first byte is dropped); bytes 00, 06 and 16 are special.
* The EEPROM write routine is `CDB7` (words from the 7600 mirror, with read-back verify, error 6 on failure);
  `CD98` loads the whole chip at boot.
* A good-checksum but random settings area hangs the boot in the sanitiser loop at AC4F; empty or bad-checksum
  EEPROMs only give error 8/6 and the main loop keeps running.
