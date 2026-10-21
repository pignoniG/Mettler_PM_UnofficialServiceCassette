"""
Addresses of the places the patches hook and the firmware routines they call, per firmware version.

They were found by matching instruction patterns (absolute addresses masked) between the versions, so
every entry is the same code in a different place; see docs/PATCHES.md.  RAM, XRAM and bit addresses
(flags 27h.0, 29h.6, 2Eh.0 ..., the temperature count at XRAM 7047, the EEPROM mirror at 7600 ...) are
the same in all versions and are not listed here.

  window          A/D window length in interrupt ticks (150 ticks/s): 24 = 0.160 s, 20 = 0.133 s per pass
  comp_*          comparator: the raw-value normaliser CC11 and where its stock code resumes
  cal_timeouts    MOV 26h,#n countdowns (passes) of the calibration / stability waits
  temp_*          the call of the temperature routine in the common pass tail
  putc            the firmware's character output routine
  bar_up .. weigh the standby / dispatch / tail places of the standby-weighing patch
  cmd_*, hex_*, reset   the stock host-command interpreter (matcher, inline printer, end of a command, `ES` reply, jump-table
                  entry of the command letter X) and the stock EEPROM Intel-HEX dump / load routines the service patch calls
  main_wait .. timer_test  only for the simulator
"""
PROFILES = {
    '10.45': dict(
        window=24, signature={0x8020: b'105'},
        comp_hook=0xCC11, comp_hook_old=bytes([0x85, 0x32, 0xF0]), comp_resume=0xCC14,
        cal_timeouts={0x93B6: 0x30, 0x9AB0: 0x48},
        temp_call=0x827B, temp_call_old=bytes([0x12, 0xC8, 0x87]), temp_routine=0xC887,
        putc=0xD6FC,
        bar_up=0x831A, bar_up_old=bytes([0x02, 0x83, 0x95]), stage1=0x8395, stage1_setb=0x83AE,
        poll=0x830E, poll_old=bytes([0x20, 0x4E, 0x18]), poll_next=0x8311, poll_jump=0x8329,
        normal=0x8314, normal_old=bytes([0x12, 0xB1, 0xA3]), weigh=0xB1A3,
        tail=0x8876, tail_old=bytes([0x12, 0xC4, 0x64, 0xC2, 0x0A, 0x02, 0x82, 0x78]), flush=0xC464, tail_cont=0x8278,
        wake=0x836E, wake_old=bytes([0xD2, 0x4D, 0xC2, 0x03, 0x02, 0x84, 0x16]), wake_exit=0x8416, start_init=0x8AA3,
        cd93=0xCD93, bb2b=0xBB2B,
        cmd_match=0xB7EB, cmd_print=0xCD82, cmd_done=0x8F0F, cmd_es=0x8F3B, cmd_table_x=0x8FBE,
        hex_dump=0x90FD, hex_load=0x8FCE, hex_guard=0x9080, hex_guard_old=bytes([0x75, 0x82, 0x00]), hex_error=0x902D, hex_reset_wait=0x90A1, reset=0x8200,
        main_wait=0x82F3, ee_load=0xCD98, ee_write=0xCDB7, timer_test=0xCD2D),
    '10.50': dict(
        window=20, signature={0x8020: b'100'},
        comp_hook=0xCB22, comp_hook_old=bytes([0x85, 0x32, 0xF0]), comp_resume=0xCB25,
        cal_timeouts={0x91AE: 0x40, 0x99CD: 0x60},
        temp_call=0x8275, temp_call_old=bytes([0x12, 0xC7, 0xE7]), temp_routine=0xC7E7,
        putc=0xD6B9,
        bar_up=0x830E, bar_up_old=bytes([0x02, 0x83, 0x89]), stage1=0x8389, stage1_setb=0x83A2,
        poll=0x8302, poll_old=bytes([0x20, 0x4E, 0x18]), poll_next=0x8305, poll_jump=0x831D,
        normal=0x8308, normal_old=bytes([0x12, 0xB0, 0xFE]), weigh=0xB0FE,
        tail=0x886A, tail_old=bytes([0x12, 0xC3, 0xC7, 0xC2, 0x0A, 0x02, 0x82, 0x72]), flush=0xC3C7, tail_cont=0x8272,
        wake=0x8362, wake_old=bytes([0xD2, 0x4D, 0xC2, 0x03, 0x02, 0x84, 0x0A]), wake_exit=0x840A, start_init=0x8AB2,
        cd93=0xCC9F, bb2b=0xBA8E,
        cmd_match=0xB742, cmd_print=0xCC8E, cmd_done=0x8F1F, cmd_es=0x8F4B, cmd_table_x=0x8FD4,
        hex_dump=0xC535, hex_load=0xC3FC, hex_guard=0xC4AE, hex_guard_old=bytes([0x75, 0x82, 0x00]), hex_error=0xC45B, hex_reset_wait=0xC4CF, reset=0x8200,
        main_wait=0x82E6, ee_load=0xCCA4, ee_write=0xCCC3, timer_test=0xCC3E),
}


def detect(img):
    """name of the firmware version of a 32 KiB image, from the version digits at 8020 (no patch touches them;
    every patch still checks the exact bytes it replaces)"""
    for name, p in PROFILES.items():
        if all(img[a - 0x8000:a - 0x8000 + len(b)] == b for a, b in p['signature'].items()):
            return name
    return None


def get(img, profile=None):
    """the profile dict for an image: a name, a dict, or None to detect"""
    if isinstance(profile, dict): return profile
    name = profile or detect(img)
    if name not in PROFILES:
        raise ValueError('unrecognised firmware: expected the unmodified V10.45 or V10.50 standard cassette image '
                         '(patches can only be combined in the documented order)' if name is None else f'unknown profile {name}')
    return PROFILES[name]
