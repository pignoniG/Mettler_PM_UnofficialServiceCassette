# Mettler AM/PM - unofficial service cassette

A patch kit, a firmware simulator and a serial protocol draft for turning the **standard program cassette
(V10.45)** of Mettler AM/PM balances into a PC-assisted service cassette: a balance that streams its raw
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
| `--comparator` | long A/D averaging at the normal display speed (mass-comparator style: ring buffer, newest-half average, longer calibration timeouts) | running on a PM4600 |
| `--temperature` | extra line `T 297904` every ~10 s: the balance's filtered temperature count (the number the service keypad shows) | running on a PM4600 |
| `--standby-weigh` | with the display off (control bar up) the balance keeps weighing and sending its serial output; the bar-down poll and the configuration entry behave as stock | running on a PM4600 (overnight logs) |
| `--skip-startup` | wake from standby without the display test, version number and switch-on tare (the first wake after power-on keeps them) | tested in the simulator, being tested on the balance |

Details, hook addresses and the memory map are in [docs/PATCHES.md](docs/PATCHES.md). The planned command
module (EEPROM read/write, raw A/D line, boot guard for a bad EEPROM) is described in
[docs/PROTOCOL.md](docs/PROTOCOL.md); it is not implemented yet.

## Use

```bash
python3 build.py V10.45.bin out.bin --comparator --temperature --standby-weigh --skip-startup
```

`V10.45.bin` is your unmodified 32 KiB image. Burn `out.bin` into a 27C256 and put it in the cassette. The
serial interface is configured on the balance (configuration file, sector I-FACE: continuous transmission,
9600 baud, even parity, Pause 0); wiring and settings are in the characterisation tool's README.

## Simulator

`sim/cpu51.py` is a complete 8051 core and `sim/fwsim.py` runs the real firmware on it with the hardware
stubbed (A/D window sums, control-bar switches, UART, EEPROM). It reproduces the start-up messages, tare,
weight lines and the standby behaviour, and lets patches be tested before an EPROM is burnt.

```bash
PM_ROM=V10.45.bin PM_EEPROM=dump.BIN python3 -m unittest discover -s tests
```

The tests are skipped without your files. `sim/dis51.py` is a small 8051 disassembler.

## Roadmap

1. `R` line with the raw A/D window sum and both temperature counts; check the correction formula against
   the displayed weight.
2. Command module: `~V` version/ID, `~R` EEPROM read.
3. `~W` write with arming, whole-image checksum check, ID guard, restart; boot guard for a corrupt EEPROM
   (recovery mode).
4. PC-side service procedures (sticker check, rebuild, linearity, calibration, TC fit).

## Licence

MIT, see [LICENSE](LICENSE). The analysis and code were developed with the help of Claude (Anthropic).
