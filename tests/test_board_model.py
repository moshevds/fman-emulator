"""Self-test of fman_emu/board.py, driven through bus accesses with the register
sequences the LS1046A RM, the Linux drivers and the device data sheets describe.

    python3 tests/test_board_model.py
"""
import os
import sys
import traceback

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fman_emu.bus import BusError, EmuError, ExternalBus, LocalBus, Memory
from fman_emu.board import (Board, BoardModelError, I2C1_BASE, I2C2_BASE, GPIO3_BASE, SERDES1_BASE, FMAN_CCSR,
                       SCFG_ALTCBAR, DCSR_RCWCR5, EMI2, MACS, RX_PORT_IDS)

IBAD, IBFD, IBCR, IBSR, IBDR = range(5)
GPDAT = GPIO3_BASE + 8


def eq(got, want, what):
    if got != want:
        raise AssertionError(f"{what}: got {got!r}, want {want!r}")


def raises(kind, fn, what):
    try:
        fn()
    except kind:
        return
    raise AssertionError(f"{what}: no {kind.__name__}")


class Timeout(Exception):
    pass


class Dma:
    """Controller-DMA-like accessor on an ExternalBus: 8- and 32-bit accesses, RMW,
    and a tick counter (each access ~36 ticks, `delay` = 256 steps of 256 ticks)."""

    def __init__(self, bus):
        self.bus, self.ticks = bus, 0

    def r8(self, a):
        self.ticks += 36
        return self.bus.dma_read(a, 1)[0]

    def w8(self, a, v):
        self.ticks += 36
        self.bus.dma_write(a, bytes([v]))

    def r32(self, a):
        self.ticks += 36
        return int.from_bytes(self.bus.dma_read(a, 4), "big")

    def w32(self, a, v):
        self.ticks += 36
        self.bus.dma_write(a, v.to_bytes(4, "big"))

    def rmw(self, a, m, v):
        new = (self.r32(a) & ~m & 0xFFFFFFFF) | v
        self.w32(a, new)
        return new

    def delay(self):
        self.ticks += 65536


class I2cOps:
    """I2C master operations (LS1046A RM I2C chapter, as Linux i2c-imx.c drives them) on one controller."""

    def __init__(self, dma, base):
        self.d, self.base = dma, base

    def r(self, reg): return self.d.r8(self.base + reg)
    def w(self, reg, v): self.d.w8(self.base + reg, v)

    def wait(self, reg, m, v, limit=1000):
        for _ in range(limit):
            if self.r(reg) & m == v:
                return
        raise Timeout(f"wait({reg}, {m:#x}, {v:#x})")

    def begin(self):
        self.w(IBCR, 0x00); self.w(IBSR, 0x12); self.d.delay(); self.wait(IBSR, 0x20, 0x00)

    def start(self, a):
        self.w(IBCR, 0x30); self.wait(IBSR, 0x20, 0x20); self.tx(a)

    def tx(self, b):
        self.w(IBDR, b); self.wait(IBSR, 0x02, 0x02); self.w(IBSR, 0x02); self.wait(IBSR, 0x01, 0x00)

    def restart(self, a):
        self.w(IBCR, 0x34); self.tx(a)

    def stop(self):
        self.w(IBCR, 0x00); self.wait(IBSR, 0x20, 0x00)

    def rx(self, n, poll_ibsr=False):
        assert n >= 2
        self.w(IBCR, 0x20)
        out = [self.r(IBDR)]                      # dummy
        for _ in range(n - 2):
            self.d.delay()
            if poll_ibsr:
                self.r(IBSR)                      # the forbidden poll (caveat 1)
            out.append(self.r(IBDR))
        self.d.delay(); self.w(IBCR, 0x28); out.append(self.r(IBDR))
        self.d.delay(); self.w(IBCR, 0x08); out.append(self.r(IBDR))
        self.w(IBSR, 0x02); self.wait(IBSR, 0x20, 0x00)
        return out

    def end(self):
        self.w(IBCR, 0x80)

    def mux_read(self):
        self.start(0xE1); self.w(IBCR, 0x28)
        dummy = self.r(IBDR); self.d.delay(); self.w(IBCR, 0x08)
        value = self.r(IBDR); self.w(IBSR, 0x02); self.wait(IBSR, 0x20, 0)
        return dummy, value

    def mux_select(self, control):
        self.start(0xE0); self.tx(control); self.stop()

    def eeprom_read(self, cage, offset, count, mux_check=True, dev=0xA0, poll_ibsr=False):
        """Mux check, channel select, EEPROM random read, deselect -> (mux dummy, mux value, read bytes incl. the dummy)."""
        self.begin()
        mux = self.mux_read() if mux_check else (None, None)
        self.mux_select(1 << cage)
        self.start(dev); self.tx(offset); self.restart(dev | 1)
        data = self.rx(count, poll_ibsr)
        self.mux_select(0x00)
        self.end()
        return mux + (data,)


def a0_image(part):
    a0 = bytearray(256)
    a0[0], a0[2], a0[3], a0[8], a0[12] = 0x03, 0x21, 0x00, 0x04, 0x67
    a0[20:36] = b"FS" + b" " * 14
    a0[37:40] = bytes([0x64, 0x9D, 0x99])
    a0[40:56] = part.ljust(16).encode()
    a0[56:60] = b"A   "
    a0[63] = sum(a0[:63]) & 0xFF
    return bytes(a0)


def setup(**kw):
    board = Board(**kw)
    bus = ExternalBus()
    bus.add_ram(0x80000000, 0x1000, "DDR")
    board.attach(bus)
    return board, Dma(bus)


# ---------------------------------------------------------------- I2C

def test_i2c_boot_registers():
    board, d = setup()
    eq(d.r32(I2C2_BASE), 0x003E8080, "I2C2 IBAD..IBSR boot word (board readback)")
    eq(d.r8(I2C2_BASE + IBSR), 0x80, "IBSR reset")
    raises(BusError, lambda: board.i2c2.read(1, 2), "unaligned 16-bit access (device level; DMA widens it)")
    raises(BusError, lambda: board.i2c2.write(0, 8, 0), "64-bit write")
    raises(BusError, lambda: d.bus.dma_read(I2C2_BASE + 8, 1), "offset beyond the register block")


def test_eeprom_read_both_cages():
    board, d = setup()
    board.insert_module(0, a0_image("SFP-10G-PC03"))
    board.insert_module(1, a0_image("SFP-10G-PC005"))
    ops = I2cOps(d, I2C2_BASE)
    for cage, part in ((0, b"SFP-10G-PC03    "), (1, b"SFP-10G-PC005   ")):
        mux_dummy, mux, data = ops.eeprom_read(cage, 20, 40)
        eq((mux_dummy, mux), (0xE1, 0x30), "mux read: dummy 0xE1 then control 0x30")
        eq(data[0], 0xA1, "rx dummy returns the address byte")
        want = b"FS" + b" " * 14 + bytes([0, 0x64, 0x9D, 0x99]) + part + b"A   "
        eq(bytes(data[1:]), want, f"cage {cage} bytes 20..59")
    eq(board.sfp_mux.control, 0, "mux deselected")
    eq(d.r8(I2C2_BASE + IBCR), 0x80, "module left disabled")
    log = [e for e in board.i2c2.log if e[0] == "rx"]
    eq([ack for *_, ack in log[-40:]], [True] * 39 + [False], "last byte NACKed by the master")


def test_eeprom_offset0_checksum():
    board, d = setup()
    image = a0_image("SFP-10G-PC03")
    board.insert_module(0, image)
    _, _, data = I2cOps(d, I2C2_BASE).eeprom_read(0, 0, 64, mux_check=False)
    eq(bytes(data[1:]), image[:64], "offset 0..63 read")
    eq(sum(data[1:64]) & 0xFF, data[64], "CC_BASE checks")


def test_polling_ibsr_while_receiving_loses_bytes():
    board, d = setup()
    image = bytes(range(256))
    board.insert_module(0, image)
    _, _, data = I2cOps(d, I2C2_BASE).eeprom_read(0, 20, 8, mux_check=False, poll_ibsr=True)
    eq(data[1:7], [21, 23, 25, 27, 29, 31], "every other byte, as observed on the board")


def test_absent_and_not_ready_nack():
    board, d = setup()
    ops = I2cOps(d, I2C2_BASE)
    ops.begin(); ops.mux_select(0x01)
    ops.w(IBCR, 0x30); ops.wait(IBSR, 0x20, 0x20); ops.w(IBDR, 0xA0); ops.wait(IBSR, 0x02, 0x02)
    eq(ops.r(IBSR) & 0x01, 1, "empty cage: address NACK (RXAK=1)")
    raises(Timeout, lambda: ops.wait(IBSR, 0x01, 0x00, limit=50), "tx macro would time out")
    ops.stop()
    board.insert_module(0, a0_image("X"), ready=False)
    eq(d.r32(GPDAT) & 0x00080000, 0, "MOD_ABS shows present")
    raises(Timeout, lambda: ops.start(0xA0), "not ready: A0 NACK")
    ops.stop()
    raises(Timeout, lambda: ops.start(0xA2), "not ready: A2 NACK")
    ops.stop()
    board.set_module_ready(0)
    ops.start(0xA0); ops.stop()
    raises(Timeout, lambda: ops.start(0xA2), "module without A2: NACK on 0x51")
    ops.stop(); ops.mux_select(0); ops.end()


def test_a2_write_and_read():
    board, d = setup()
    board.insert_module(1, a0_image("X"), a2=bytes(256))
    ops = I2cOps(d, I2C2_BASE)
    ops.begin(); ops.mux_select(0x02)
    ops.start(0xA2); ops.tx(0x7F); ops.tx(0x11); ops.tx(0x22); ops.stop()
    ops.start(0xA2); ops.tx(0x7F); ops.restart(0xA3)
    eq(ops.rx(2)[1:], [0x11, 0x22], "A2 bytes written")
    ops.mux_select(0); ops.end()
    eq(bytes(board.modules[1].a2.data[0x7F:0x81]), b"\x11\x22", "stored")


def test_mux_selection_effective_at_stop_and_channels_isolated():
    board, d = setup()
    board.insert_module(0, a0_image("LEFT"))
    ops = I2cOps(d, I2C2_BASE)
    ops.begin()
    ops.start(0xE0); ops.tx(0x01)
    ops.w(IBCR, 0x34); ops.w(IBDR, 0xA0); ops.wait(IBSR, 0x02, 0x02)
    eq(ops.r(IBSR) & 1, 1, "channel not active before STOP")
    ops.stop()
    ops.mux_select(0x02)
    raises(Timeout, lambda: ops.start(0xA0), "left module not visible on channel 1")
    ops.stop(); ops.mux_select(0); ops.end()


def test_i2c_strictness():
    board, d = setup()
    ops = I2cOps(d, I2C2_BASE)
    raises(BoardModelError, lambda: ops.w(IBCR, 0x30), "MDIS cleared and MSSL set together")


def test_i2c_timing_model():
    board, d = setup(clock=lambda: d.ticks, i2c_byte_ticks=640)
    board.insert_module(0, a0_image("SFP-10G-PC03"))
    ops = I2cOps(d, I2C2_BASE)
    _, _, data = ops.eeprom_read(0, 40, 4)
    eq(bytes(data[1:]), b"SFP-", "with delays the sequence works")
    ops.begin(); ops.mux_select(1)
    ops.start(0xA0); ops.tx(0); ops.restart(0xA1); ops.w(IBCR, 0x20); ops.r(IBDR)
    raises(BoardModelError, lambda: ops.r(IBDR), "IBDR read before the byte arrived")


def test_retimer():
    board, d = setup()
    ops = I2cOps(d, I2C1_BASE)
    eq(d.r32(I2C1_BASE), 0x003E8080, "I2C1 boot word")

    def write(reg, value):
        ops.start(0x30); ops.tx(reg); ops.tx(value); ops.stop()

    def read(reg):
        ops.start(0x30); ops.tx(reg); ops.restart(0x31)
        return ops.rx(2)[1]                        # read 2, discard the second

    ops.begin(); ops.mux_select(0x01)
    eq(read(0x01), 0xD0, "ID register")
    saved = {}
    for page in (0x04, 0x07):                      # right cage
        write(0xFF, page)
        eq(read(0xFF), page, "page register")
        saved[page] = {r: read(r) for r in (0x09, 0x14, 0x1E)}
    eq(saved[4], {0x09: 0, 0x14: 0, 0x1E: 0x09}, "baseline page 4")
    eq(saved[7], {0x09: 0, 0x14: 0, 0x1E: 0xE9}, "baseline page 7")
    for page in (0x04, 0x07):                      # -> 1000BASE-X
        write(0xFF, page)
        for reg, value in ((0x09, saved[page][0x09] | 0x20), (0x1E, saved[page][0x1E] & ~0xE0 & 0xFF),
                           (0x14, saved[page][0x14])):
            write(reg, value)
            eq(read(reg), value, f"readback page {page} reg {reg:#x}")
    write(0xFF, 0x00)
    ops.mux_select(0); ops.end()
    eq(board.retimer.reg(7, 0x1E), 0x09, "page 7 0x1e cleared bits 7:5")
    eq(board.retimer.reg(5, 0x09), 0x00, "left cage page untouched")
    eq(board.retimer.page, 0, "page reset")


# ---------------------------------------------------------------- GPIO

def test_gpio():
    board, d = setup()
    eq(d.r32(GPIO3_BASE), 0x2207E4A0, "GPDIR boot")
    eq(d.r32(GPDAT) & 0x00780000, 0x00780000, "empty cages: MOD_ABS and LOS high")
    board.insert_module(0, a0_image("L")); board.insert_module(1, a0_image("R"))
    eq(d.r32(GPDAT), 0xB18009E0, "both present, gates low (board readback)")
    raises(BusError, lambda: d.w8(GPDAT + 2, 0x49), "byte write")
    raises(BusError, lambda: d.r8(GPDAT + 3), "byte read")
    eq(d.rmw(GPDAT, 0x00040000, 0x00040000), 0xB18409E0, "right gate on")
    eq((board.tx_enabled(0), board.tx_enabled(1)), (False, True), "tx_enabled")
    board.set_los(1, True)
    eq(d.r32(GPDAT) & 0x00400000, 0x00400000, "LOS right")
    d.w32(GPDAT, 0)                                # inputs keep their levels
    eq(d.r32(GPDAT) & 0x00780000, 0x00400000, "input pins not overwritten")
    board.remove_module(0)
    eq(d.r32(GPDAT) & 0x00180000, 0x00180000, "left absent")


# ---------------------------------------------------------------- SerDes

def test_serdes_power_down_up_right_cage():
    board, d = setup()
    board.insert_module(1, a0_image("R"))
    d.rmw(GPDAT, 0x00040000, 0x00040000)
    g = SERDES1_BASE + 0x880
    eq(d.r32(GPDAT), 0xB18609E0 & ~0x00020000 | 0x00080000 | 0x00100000, "before (left empty)")
    d.rmw(GPDAT, 0x00040000, 0)
    eq(d.r32(g), 0x33651500, "before")
    eq(d.rmw(g, 0x00500000, 0x00100000), 0x33351500, "RX_PD=1 RRST_B=0")
    eq(d.rmw(g, 0x00280000, 0x00080000), 0x331D1500, "TX_PD=1 TRST_B=0")
    eq(board.lane_powered(1), False, "powered down")
    eq(d.rmw(g, 0x00180000, 0), 0x33051500, "clear PDs")
    eq(d.rmw(g, 0x00600000, 0x00600000), 0x33651500, "release resets")
    d.rmw(GPDAT, 0x00040000, 0x00040000)
    eq((board.lane_powered(1), board.tx_enabled(1), board.lane_mode(1)), (True, True, "10g"), "back up")


def lane(cage):
    base = SERDES1_BASE + 0x800 + 0x40 * (3, 2)[cage]
    sg = SERDES1_BASE + 0x1800 + 0x10 * (3, 2)[cage]
    return dict(GCR0=base, GCR1=base + 4, RECR0=base + 0x10, TECR0=base + 0x18, TTLCR0=base + 0x20,
                CR0=sg, CR1=sg + 4)


ROUTES = {0: ((0x00070000, 0x00010000), (0x07000000, 0x01000000)),
          1: ((0x00700000, 0x00100000), (0x70000000, 0x20000000))}


def rcwcr5(d, proto):
    saved = d.r32(SCFG_ALTCBAR)
    d.w32(SCFG_ALTCBAR, 0x00002000)
    d.w32(DCSR_RCWCR5, proto << 16 | 0x5A06)
    d.w32(SCFG_ALTCBAR, saved)


def to_1g(d, cage):
    L, ((sm, sv), (xm, xv)) = lane(cage), ROUTES[cage]
    P8, PB = SERDES1_BASE + 0x220, SERDES1_BASE + 0x22C
    for addr, m, v in ((L["GCR0"], 0x00600000, 0), (PB, xm, 0), (P8, sm, 0), (L["CR1"], 0x800, 0),
                       (L["CR0"], 0xC0000000, 0x40000000), (L["GCR1"], 0x007FC033, 0x001C4011),
                       (L["RECR0"], 0x1F0FCC00, 0x0F0FC000), (L["TECR0"], 0x37FF3F3F, 0x00003006),
                       (L["TTLCR0"], 0x3F000000, 0x39000000), (L["GCR0"], 0xBB040F80, 0xAA000080),
                       (L["CR1"], 0x800, 0x800), (L["CR0"], 0xC0000000, 0x80000000), (P8, sm, sv)):
        d.rmw(addr, m, v)
    rcwcr5(d, 0x1333 if cage == 1 else 0x3133)
    d.rmw(L["GCR0"], 0x00600000, 0x00600000)


def to_10g(d, cage):
    L, ((sm, sv), (xm, xv)) = lane(cage), ROUTES[cage]
    P8, PB = SERDES1_BASE + 0x220, SERDES1_BASE + 0x22C
    for addr, m, v in ((L["GCR0"], 0x00600000, 0), (P8, sm, 0), (L["CR1"], 0x800, 0),
                       (L["CR0"], 0xC0000000, 0x40000000), (L["GCR0"], 0x00040F80, 0x00040500),
                       (L["GCR1"], 0x007FC033, 0x00000022), (L["RECR0"], 0x1F0FCC00, 0x10000400),
                       (L["TECR0"], 0x37FF3F3F, 0x10233007), (L["TTLCR0"], 0x3F000000, 0),
                       (L["GCR0"], 0xBB000000, 0x33000000), (PB, xm, xv)):
        d.rmw(addr, m, v)
    rcwcr5(d, 0x1133)
    d.rmw(L["GCR0"], 0x00600000, 0x00600000)


def test_serdes_lane_transitions():
    board, d = setup()
    for cage in (1, 0):
        to_1g(d, cage)
        eq(d.r32(lane(cage)["GCR0"]), 0xAA611080, f"cage {cage} 1G GCR0 (board readback, SerDes protocol 0x1133)")
        eq(d.r32(lane(cage)["TTLCR0"]), 0x39000400, "TTLCR0")
        eq(board.lane_mode(cage), "1g", "1g")
        eq(board.lane_mode(1 - cage), "10g" if cage == 1 else "1g", "companion untouched")
    eq(d.r32(SERDES1_BASE + 0x220), 0x11110000, "PCCR8 both 1G (board readback, SerDes protocol 0x1133)")
    eq(d.r32(SERDES1_BASE + 0x22C), 0, "PCCRB both 1G")
    eq(board.rcwcr5.value, 0x31335A06, "last RCWCR5 written")
    eq(board.altcbar.value, 0, "ALTCBAR restored")
    for cage in (0, 1):
        to_10g(d, cage)
        eq(d.r32(lane(cage)["GCR0"]), 0x33651500, f"cage {cage} XFI GCR0")
        eq(board.lane_mode(cage), "10g", "10g")
    eq(d.r32(SERDES1_BASE + 0x22C), 0x21000000, "PCCRB back to boot")
    eq(d.r32(SERDES1_BASE + 0x24), 0x20860008, "PLL2 CR0 boot")
    d.rmw(SERDES1_BASE + 0x22C, 0x70000000, 0x10000000)
    eq(board.lane_mode(1), None, "lane C with XFIA=1 is not a valid 10G route")
    raises(BusError, lambda: d.r32(SERDES1_BASE + 0x900), "unmodelled SerDes register")
    raises(BusError, lambda: d.r8(SERDES1_BASE + 0x880), "byte read of SerDes")


# ---------------------------------------------------------------- MDIO

class Mdio:
    """DPAA RM MDIO procedures (clause 22 and 45) through 32-bit accesses at CCSR FMan base + B."""

    def __init__(self, d, block, base=FMAN_CCSR):
        self.d, self.b = d, base + block

    def poll(self):
        for _ in range(1000):
            if not self.d.r32(self.b + 0x30) & 0x80000001 and not self.d.r32(self.b + 0x38) & 0x80000000:
                return
        raise Timeout("MDIO busy")

    def _mode(self, c45):
        c = self.d.r32(self.b + 0x30)
        if bool(c & 0x40) != c45:
            self.d.w32(self.b + 0x30, (c & 0x0080FF9C & ~0x40) | (0x40 if c45 else 0))
        self.poll()

    def c22_read(self, p, r):
        self._mode(False)
        self.d.w32(self.b + 0x34, (p << 5) | r)
        self.d.w32(self.b + 0x34, (p << 5) | r | 0x8000)
        self.poll()
        if self.d.r32(self.b + 0x30) & 0x2:
            return None
        return self.d.r32(self.b + 0x38) & 0xFFFF

    def c22_write(self, p, r, v):
        self._mode(False)
        self.d.w32(self.b + 0x34, (p << 5) | r)
        self.d.w32(self.b + 0x38, v)
        self.poll()

    def c45_read(self, p, dev, reg):
        self._mode(True)
        self.d.w32(self.b + 0x34, (p << 5) | dev)
        self.d.w32(self.b + 0x3C, reg)
        self.poll()
        self.d.w32(self.b + 0x34, (p << 5) | dev | 0x8000)
        self.poll()
        return None if self.d.r32(self.b + 0x30) & 0x2 else self.d.r32(self.b + 0x38) & 0xFFFF


def test_phys_on_emi2():
    board, d = setup()
    m = Mdio(d, EMI2)
    for p in (0, 1, 2):
        eq((m.c22_read(p, 2), m.c22_read(p, 3)), (0x67C9, 0xDF10), f"PHY {p} ID")
    eq(m.c22_read(5, 2), None, "RD_ER at an empty address")
    eq(m.c22_read(0, 2), 0x67C9, "RD_ER cleared by the next good read")
    board.set_phy_link("MAC5", True, speed=1000, duplex=1)
    eq(m.c22_read(0, 0x18), 0x040A, "MIISTAT link/full/1000")
    eq(m.c22_read(0, 1) & 4, 4, "BMSR link")
    board.set_phy_link(0, False); board.set_phy_link(0, True, speed=100, duplex=0)
    eq((m.c22_read(0, 1) & 4, m.c22_read(0, 1) & 4), (0, 4), "BMSR latched low, then current")
    eq(m.c22_read(0, 0x18), 0x0401, "MIISTAT 100/half")
    eq(m.c22_read(1, 0x18) & 0x400, 0, "PHY 1 down")
    m.c22_write(2, 0x1B, 0x0F00)
    eq(m.c22_read(2, 0x1B), 0x0F00, "LED register")
    m.c22_write(2, 0x00, 0x8000)
    eq((m.c22_read(2, 0), m.c22_read(2, 0x1B), board.phys[2].resets), (0x1040, 0, 1), "BMCR reset self-clears")
    eq(m.c45_read(2, 0x1E, 0x01), 0, "MMD read")
    m._mode(True); d.w32(m.b + 0x34, (2 << 5) | 0x1E); d.w32(m.b + 0x3C, 1); m.poll(); d.w32(m.b + 0x38, 0x2040); m.poll()
    eq(m.c45_read(2, 0x1E, 0x01), 0x2040, "MMD VEND1 0x01 write")


def test_mdio_busy_and_strictness():
    board, d = setup(mdio_busy_polls=3)
    b = FMAN_CCSR + EMI2
    d.w32(b + 0x30, d.r32(b + 0x30) & ~0x40)
    d.w32(b + 0x34, 0x8002)
    raises(BoardModelError, lambda: d.w32(b + 0x34, 0x8003), "new transaction while BSY")
    eq([d.r32(b + 0x30) >> 31 for _ in range(4)], [1, 1, 1, 0], "BSY for 3 polls (DRM bit 0)")
    eq(d.r32(b + 0x30) & 0x40000000, 0x40000000, "CMP set")
    d.w32(b + 0x30, d.r32(b + 0x30))
    eq(d.r32(b + 0x30) & 0x40000000, 0, "CMP w1c")
    eq(d.r32(b + 0x38), 0x67C9, "data")
    raises(BusError, lambda: d.r8(b + 0x30), "byte read of MDIO")
    raises(BusError, lambda: d.r32(b + 0x00), "reserved MDIO offset")
    board2, d2 = setup(emi2_cfg=0x00800048)            # reset value: MDC off
    b2 = FMAN_CCSR + EMI2
    d2.w32(b2 + 0x34, 0x8002)
    raises(Timeout, lambda: Mdio(d2, EMI2).poll(), "CLK_DIV=0 never completes")


def test_pcs_link_and_writes():
    board, d = setup()
    m = Mdio(d, MACS["MAC10"] + 0x1000)
    eq(d.r32(FMAN_CCSR + 0xF3030), 0x00001448, "internal MDIO CFG reset (board read 0x40001448 incl. CMP)")
    eq(m.c45_read(0, 3, 1) & 4, 0, "10G PCS down")
    board.set_pcs_link(0x11, True)                 # by Rx PortID: MAC10
    eq(m.c45_read(0, 3, 1) & 4, 4, "10G PCS up")
    board.set_pcs_link("MAC10", False); board.set_pcs_link("MAC10", True)
    eq((m.c45_read(0, 3, 1) & 4, m.c45_read(0, 3, 1) & 4), (0, 4), "latched low, read twice")
    for reg, value in ((0x14, 0x0000), (0x04, 0x01A0), (0x13, 0x0013), (0x12, 0x12D0), (0x00, 0x0140)):
        m.c22_write(0, reg, value)
        eq(m.c22_read(0, reg), value, f"PCS reg {reg:#x}")
    eq(m.c22_read(0, 1) & 4, 4, "BMSR link via clause 22 (own latch, cleared by its first read)")
    eq(m.c22_read(1, 1), None, "only port address 0 answers")
    m1 = Mdio(d, MACS["MAC2"] + 0x1000)
    for reg, value in ((0x12, 0x0D40), (0x13, 0x0003), (0x14, 0x0001), (0x04, 0x4001)):
        m1.c22_write(0, reg, value)
    eq(board.pcs["MAC2"].regs[0x12], 0x0D40, "SGMII PCS storage")


# ---------------------------------------------------------------- mEMAC

def test_memac():
    board, d = setup()
    for name, rx in RX_PORT_IDS.items():
        base = FMAN_CCSR + MACS[name]
        eq(d.r32(base + 0x008), 0x00000840, f"{name} COMMAND_CONFIG reset")
        eq((board.mac_enabled(name), board.mac_enabled(rx)), (False, False), "disabled")
    cc = FMAN_CCSR + MACS["MAC9"] + 0x008
    d.w32(cc, 0x00020953)
    eq(board.mac_enabled("MAC9"), True, "MAC9 enabled")
    d.rmw(cc, 0x3, 0)
    d.w32(cc, d.r32(cc) | 0x1000)
    polls = 0
    while d.r32(cc) & 0x1000:
        polls += 1
    eq((polls, board.memacs["MAC9"].resets, d.r32(cc)), (1, 1, 0x00020950), "SW_RESET self-clears")
    d.w32(FMAN_CCSR + MACS["MAC9"] + 0x300, 2)
    d.w32(FMAN_CCSR + MACS["MAC9"] + 0x020, 0x00100020)
    eq(board.fman_read32(MACS["MAC9"] + 0x300), 2, "IF_MODE via the FMan-offset path")
    eq(board.fman_read32(MACS["MAC9"] + 0x014), 0x600, "MAXFRM reset")
    eq(board.fman_read32(0x80000), None, "not a board block")
    eq(board.fman_write32(0x80000, 1), None, "not a board block")
    raises(BusError, lambda: d.r8(cc), "byte read of mEMAC")


def test_fman_local_path():
    from fman_emu.hw import Fman
    fman = Fman()
    board = Board().attach(fman.cpu.external).attach_fman(fman.cpu.local)
    eq(fman.cpu.local.read32(0xF3030), 0x00001448, "MDIO CFG via unit 12 path")
    fman.cpu.local.write32(0xF2008, 0x00020953)
    eq(board.mac_enabled("MAC10"), True, "MAC10 via unit 12 path")
    eq(int.from_bytes(fman.cpu.external.dma_read(FMAN_CCSR + 0xF2008, 4), "big"), 0x00020953, "same register via DMA")
    eq(fman.ddr.r32(0), 0, "DDR mapping still works")


def main():
    tests = [(k, v) for k, v in globals().items() if k.startswith("test_")]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"ok {name}")
        except Exception:
            failed.append(name)
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"board model: {len(tests) - len(failed)}/{len(tests)} passed" + (f", failed: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
