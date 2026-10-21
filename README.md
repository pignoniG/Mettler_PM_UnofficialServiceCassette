# Mettler AM/PM - unofficial service cassette

A patch kit, a firmware simulator and a serial protocol draft for turning the **standard program cassette
(V10.45 or V10.50)** of Mettler AM/PM balances into a PC-assisted service cassette: a balance that streams its raw
A/D and temperature counts, keeps measuring with the display off, and (planned) lets a PC read and write
its parameter EEPROM, so the adjustment steps of the service manual can be done from a computer.

The PC side lives in [Mettler_PM_UnofficialServiceTool](https://github.com/pignoniG/Mettler_PM_UnofficialServiceTool)
(EEPROM map and editor). Measurement and logging tools are in
[Precision-Scale-Caracterisation-Tool](https://github.com/pignoniG/Precision-Scale-Caracterisation-Tool).

> **This repository contains no Mettler-Toledo firmware.** You need your own dump of the 27C256 in your
> cassette. `build.py` patches *that* image, checks every byte it replaces, and writes a new file for you
> to burn. **Do not publish or share the patched image**: it contains Mettler's code. Unofficial project,
> not affiliated with or endorsed by Mettler-Toledo. Burning a bad image into a cassette can leave a
> balance not working until you reprogram it; keep the original EPROM. Use at your own risk.

## What it can do today

| Patch | What it does | Status |
|---|---|---|
| `--comparator` | long A/D averaging at the normal display speed (mass-comparator style: ring buffer, newest-half average, longer calibration timeouts) | running on a PM4600 (V10.45) |
| `--temperature` | extra line `T 297904` every ~10 s: the balance's filtered temperature count (the number the service keypad shows) | running on a PM4600 |
| `--raw` | an `R` line every pass: the 32-bit A/D window sum and the raw value that feeds the correction, and about once a second the temperature counts and the window length (hex), see [docs/PROTOCOL.md](docs/PROTOCOL.md) | on a PM4600 (V10.45): works, and the correction model matched the displayed weight of a 1 kg step to a few mg; the first, longer line format was too slow for the UART, this one is compact |
| `--standby-weigh` | with the display off (control bar up) the balance keeps weighing and sending its serial output; the bar-down poll and the configuration entry behave as stock | running on a PM4600 (overnight logs) |
| `--service` | EEPROM read and write over serial: `XV` version and ID, `XD` dump, `XA`/`XB` arm, `XL` load, `XR` restart, with checksum, ID and arming guards (`tools/svc_client.py` is the PC side; the GUI of Mettler_PM_UnofficialServiceTool uses the same commands for live read/write and the adjustment procedures) | dump, write, restore, the refusals (unarmed, bad checksum, ID mismatch), the `XB` override and restart tested on a PM4600 (V10.45) |
| `--skip-startup` | wake from standby without the display test, version number and switch-on tare (the first wake after power-on keeps them) | tested in the simulator, being tested on the balance |

**Firmware versions.** V10.45 is what the patches were developed and tested on (PM4600). V10.50 is supported through an address table (`patches/profiles.py`): every hook site and firmware routine was found by instruction-pattern matching, the patched images build and pass the same simulator tests, but **V10.50 has not been tried on a real balance yet**. `build.py` detects the version from the image.

Details, hook addresses and the memory map are in [docs/PATCHES.md](docs/PATCHES.md). The serial protocol (output
lines and the service commands) is in [docs/PROTOCOL.md](docs/PROTOCOL.md). The boot guard for a corrupt EEPROM is
still planned.

## Use

```bash
python3 build.py V10.45.bin out.bin --comparator --temperature --raw --standby-weigh --skip-startup --service
```

`V10.45.bin` (or a V10.50 image) is your unmodified 32 KiB image. Burn `out.bin` into a 27C256 and put it in the cassette. The
serial interface is configured on the balance (configuration file, sector I-FACE: continuous transmission,
9600 baud, even parity, Pause 0); wiring and settings are in the characterisation tool's README.

### Reading and writing the EEPROM

With a `--service` image in the cassette and the balance connected (RS-232 receive wire connected as well):

```bash
python3 tools/svc_client.py version
python3 tools/svc_client.py dump backup.BIN          # programmer byte order, like the EEPROM tool's dumps
python3 tools/svc_client.py load new.BIN             # checks the file, arms, writes; add --override-id for a board swap or blank chip
```

`load` replaces the whole EEPROM including the user calibration: dump first. After a successful write the balance
restarts into the `-OFF-` standby display and is silent: press the control bar, then `dump` to verify. The firmware
rewrites a few bytes itself on every restart (0x36 and 0x39, their sum stays constant), so a dump after a write is
not bit-identical in those two bytes. Needs pyserial. Never send `CA` (stock calibration command).

## Simulator

`sim/cpu51.py` is a complete 8051 core and `sim/fwsim.py` runs the real firmware on it with the hardware
stubbed (A/D window sums, control-bar switches, UART, EEPROM). It reproduces the start-up messages, tare,
weight lines and the standby behaviour, and lets patches be tested before an EPROM is burnt.

```bash
PM_ROM=V10.45.bin PM_ROM_1050=V10.50.bin PM_EEPROM=dump.BIN python3 -m unittest discover -s tests
```

The tests are skipped without your files. `tools/model_check.py` compares the displayed weights of the simulated
firmware with the correction model (it needs `pm_eeprom.py` of the EEPROM tool: `PM_TOOL=.../tools`). `sim/dis51.py` is a
small 8051 disassembler.

## Roadmap

1. ~~`R` line with the raw A/D window sum and both temperature counts; check the correction formula against
   the displayed weight.~~ Done in the simulator: the R line works on both versions and `tools/model_check.py` finds the
   model of `EEPROM_MAP.md` section 3 identical to the firmware (0.0 mg at 0..4000 g and three temperatures). Still to do:
   the same check against a log from a real balance.
2. ~~Command module: version/ID, EEPROM read.~~ Done (`--service`, `XV`, `XD`).
3. ~~Write with arming, checksum check, ID guard, restart.~~ Done (`XA`/`XB`/`XL`/`XR`), tested on a PM4600 with a
   write and restore of the chip. Refusals and `XR` also checked on the balance (chip unchanged after each refusal). The `XB` override (a changed ID written, then the original restored) was checked too. Still to do: V10.50 on a real balance;
   boot guard for a corrupt EEPROM (recovery mode).
4. PC-side service procedures (sticker check, rebuild, linearity, calibration, TC fit).

## Licence

MIT, see [LICENSE](LICENSE). The analysis and code were developed with the help of Claude (Anthropic).
