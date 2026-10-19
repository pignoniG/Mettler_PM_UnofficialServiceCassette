# Patches for V10.45 (ME-34172 standard program cassette)

All patches are applied to a 32 KiB image with a valid checksum (the byte sum is 0 mod 256). Each one checks
the bytes it replaces, uses only free filler (0xFF) and fixes the checksum at F7FF. They can be combined.

## Memory map used

| Area | Used by |
|---|---|
| ROM E800..E957 | `--temperature` |
| ROM E980..EA38 | `--standby-weigh` (and `--skip-startup`) |
| ROM F000..F368 | `--comparator` |
| XRAM 7090..70AF, ring 7682..76FF | `--comparator` |
| XRAM 70E8..70ED | `--temperature` (counter, register save) |
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

Hook at 827B (`LCALL C887`, the temperature routine, called once per pass from the common tail). In continuous
output mode, when the interface is free and the output buffer is not nearly full, it sends `T nnnnnn` + CR LF
every N passes through the firmware's own character routine `D6FC` (so line ending and parity follow the
settings). The number is the 24-bit filtered count at XRAM 7047..7049 (about 2000 counts per degree C; the filter
is a first-order low-pass with factor 1/4 per update).

## --standby-weigh

See the header of `patches/rom_standby_weigh.py`. In short: the real standby (control bar up) repeats the
stage-1 code at 831A->8395 every pass and sets flag 27h.0, which turns the serial output and the RS-232 driver
off. The patch lets stage 1 run with 27h.0 clear, then runs the normal weighing path in the common pass tail
(8876), hides the key events from it, restores the standby poll's own variables and re-blanks the display. The
bar-down poll state is untouched.

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
