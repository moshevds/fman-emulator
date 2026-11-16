"""Behavioural models of the board devices around the FMan on a Mono Gateway DK (LS1046A).

Modelled: the two I2C controllers with the SFP-cage and retimer multiplexers,
SFP module EEPROMs (SFF-8472 A0h/A2h), the DS100DF410 retimer, the GPIO3 cage
signals, the SerDes1 registers of the cage lanes, the mEMACs, their internal
PCS MDIO blocks, and EMI2 with three GPY115C PHYs.

Sources: the LS1046A reference manual (RM), the DPAA reference manual, the
Linux drivers (i2c-imx.c, gpio-mpc8xxx.c, fsl_memac.c / fman_memac.c,
xgmac_mdio.c) and the board's device tree, the device data sheets, and register
values read on the board (marked "board").  Where these disagree the models
follow the manual or the driver (listed at the end of this docstring); choices
no source supports are marked ASSUMED.

Every device has a mount-independent interface

    dev.read(offset, size) -> int          # big-endian value of `size` bytes
    dev.write(offset, size, value)

with `offset` relative to the device's own base.  Accesses the real device
does not support (width, alignment, unmapped offset) raise `BusError`;
protocol misuse the model has no defined behaviour for raises
`BoardModelError`.  Both derive from `EmuError`.

`Board` builds the whole set:

    board = Board()
    board.attach(cpu.external)       # CCSR devices + FMan blocks at 0x01a00000 + offset (controller DMA)
    board.attach_fman(cpu.local)     # optional: FMan blocks at their FMan offsets (unit 12 path)
    board.fman_read32(0xf3030)       # direct FMan-offset access; None if not a board block

Ports are named by their hardware identity (board device tree):
MAC2 (Rx PortID 0x09, EMI2 PHY address 2), MAC5 (0x0c, PHY 0), MAC6 (0x0d, PHY 1),
MAC9 (0x10, left SFP+ cage 0, SerDes1 lane D), MAC10 (0x11, right cage 1, lane C).
Methods that take a MAC accept its name ("MAC9") or its Rx PortID (0x10).

Points where the model had to choose between sources, or assumes (ASSUMED)
what no source gives:

* I2C IBSR bit 4: RM 23.5.5 lists bits 4-3 as reserved; Linux (i2c-imx.c IAL)
  calls it IBAL (w1c).  Modelled as IBAL, never set (single master).
* I2C registers accept 8-, 16- and 32-bit aligned accesses (RM 23.5), plus an
  aligned 64-bit read.  Board: an FMan DMA read of any byte of an I2C block
  reads all 8 bytes, IBDR included, so it consumes a received byte in master
  receive mode (`Board.attach` installs this read widening).
* IBCR: clearing MDIS and setting MSSL in one write raises: RM "you must clear
  this field before any other IBCR fields have any effect".
* PCA9545: a new channel selection takes effect at the STOP (PCA9545 data
  sheet).  INT bits read as 0x3 on the SFP mux (board read 0x30), 0 on I2C1.
* MDIO_CFG bit numbering follows the DPAA RM: BSY = 0x80000000, CMP =
  0x40000000 (w1c), RD_ER = 0x2, ENC45 = 0x40.  Linux's BSY = BIT(0) is a
  reserved bit here and reads 0.  MDIO_DATA bit 31 (sometimes described as
  "DATA busy") is reserved and reads 0.
* External MDIO with MDIO_CLK_DIV = 0 (its reset value: MDC off) never
  completes a transaction (BSY stays set).  The default EMI2 CFG is an ASSUMED
  hand-off value with CLK_DIV = 0x28.
* Absent SFP: LOS input reads 1 (ASSUMED, pull-up); MOD_ABS reads 1.
* Retimer: ID 0xd0 is on page 0 (U-Boot reads it with page 0); pages are plain
  256-byte stores; register pointer auto-increments (ASSUMED).
"""
from .bus import BusError, EmuError

M32 = 0xFFFFFFFF

FMAN_CCSR = 0x01A00000
I2C1_BASE, I2C2_BASE = 0x02180000, 0x02190000
GPIO3_BASE = 0x02320000
SERDES1_BASE = 0x01EA0000
SCFG_ALTCBAR = 0x01570158
DCSR_RCWCR5 = 0x20140110
EMI2 = 0xFD000

MACS = {"MAC2": 0xE2000, "MAC5": 0xE8000, "MAC6": 0xEA000, "MAC9": 0xF0000, "MAC10": 0xF2000}
RX_PORT_IDS = {"MAC2": 0x09, "MAC5": 0x0C, "MAC6": 0x0D, "MAC9": 0x10, "MAC10": 0x11}   # MAC -> Rx PortID
CAGE_MACS = ("MAC9", "MAC10")                              # cage 0 = left, 1 = right
CAGE_LANES = (3, 2)                                        # SerDes1 register block: lane D, lane C
PHY_ADDRESS = {"MAC5": 0, "MAC6": 1, "MAC2": 2}            # GPY115C on EMI2

# GPIO3 cage signals (pin n = bit 1 << (31 - n))
TX_GATE = (0x00020000, 0x00040000)                         # 1 = transmitter enabled
MOD_ABS = (0x00080000, 0x00200000)                         # 1 = absent
RX_LOS = (0x00100000, 0x00400000)                          # 1 = loss of signal
GPDIR_BOOT, GPDAT_BOOT = 0x2207E4A0, 0xB18009E0   # as U-Boot leaves them
GPDIR_RESET = 0x00000000                           # as EDK2 leaves it (board): every pin an input


class BoardModelError(EmuError):
    """The microcode did something the board model has no defined behaviour for."""


def _check(name, offset, size, sizes, limit):
    if size not in sizes or offset < 0 or offset % size or offset + size > limit:
        raise BusError(f"{name}: unsupported {size}-byte access at offset {offset:#x}")


class WordDevice:
    """Base for blocks of 32-bit big-endian registers: whole aligned words only."""
    name, span = "device", 0x1000

    def read(self, offset, size):
        _check(self.name, offset, size, (4,), self.span)
        return self.read32(offset) & M32

    def write(self, offset, size, value):
        _check(self.name, offset, size, (4,), self.span)
        self.write32(offset, value & M32)


class Word(WordDevice):
    """One plain-storage 32-bit register."""
    span = 4

    def __init__(self, name, value=0):
        self.name, self.value = name, value

    def read32(self, offset): return self.value
    def write32(self, offset, value): self.value = value


# --------------------------------------------------------------------------- I2C

class I2cBus:
    """One I2C bus: (7-bit address, target) pairs; muxes expose `downstream()`.

    Target interface: address(read) -> ack, write(byte) -> ack, read() -> byte,
    ack(master_acked), stop().  Several responders are wired-AND."""

    def __init__(self, name):
        self.name, self.devices, self.active = name, [], []

    def attach(self, address, target):
        self.devices.append((address, target))
        return target

    def _reachable(self):
        for address, target in self.devices:
            yield address, target
            for sub in getattr(target, "downstream", lambda: ())():
                yield sub

    def _all(self):
        for address, target in self.devices:
            yield target
            for channel in getattr(target, "channels", ()):
                for _, sub in channel:
                    yield sub

    def address(self, address7, read):
        self.active = [t for a, t in self._reachable() if a == address7 and t.address(read)]
        return bool(self.active)

    def write_byte(self, byte):
        acks = [t.write(byte) for t in self.active]
        return any(acks)

    def read_byte(self):
        value = 0xFF
        for t in self.active:
            value &= t.read()
        return value

    def master_ack(self, ack):
        for t in self.active:
            t.ack(ack)

    def restart(self):
        self.active = []

    def stop(self):
        self.active = []
        for t in list(self._all()):
            t.stop()


class I2cController:
    """LS1046A I2C (VF610 flavour): IBAD/IBFD/IBCR/IBSR/IBDR/IBIC/IBDBG at +0..+6.

    `clock` (callable -> ticks) and `byte_ticks` make each byte take time:
    TCF reads 0 and IBIF stays clear until clock() >= start + byte_ticks; an
    IBDR access or a START/STOP during that time raises BoardModelError.
    Without a clock every byte completes at once."""
    MDIS, IBIE, MSSL, TXRX, NOACK, RSTA = 0x80, 0x40, 0x20, 0x10, 0x08, 0x04
    TCF, IBB, IBAL, IBIF, RXAK = 0x80, 0x20, 0x10, 0x02, 0x01
    span = 0x10000

    def __init__(self, name, bus, ibfd=0x3E, clock=None, byte_ticks=0):
        self.name, self.bus, self.clock, self.byte_ticks = name, bus, clock, byte_ticks
        self.ibad, self.ibfd, self.ibic, self.ibdbg = 0, ibfd, 0, 0
        self.ibcr, self.data = self.MDIS, 0
        self._reset_status()
        self.log = []                                       # (kind, byte, ack) per bus byte

    def _reset_status(self):
        self.ibb = self.ibal = self.ibif = False
        self.rxak = False
        self.phase = None             # None, 'addr', 'write', 'read', 'read_done', 'nack'
        self.done_at = None
        self.pending_rx = None        # byte being received (clock mode)

    # ---- timing
    def _busy(self):
        return self.done_at is not None and self.clock() < self.done_at

    def _settle(self):
        if self.done_at is not None and self.clock() >= self.done_at:
            self.done_at = None
            self._finish()

    def _begin(self, receiving):
        if self.clock is None:
            self._finish()
        else:
            self.done_at = self.clock() + self.byte_ticks

    def _finish(self):
        if self.pending_rx is not None:
            ack = not self.ibcr & self.NOACK              # the ACK bit is driven at the end of the byte
            if self.phase == "read":
                self.bus.master_ack(ack)
                if not ack:
                    self.phase = "read_done"
            self.rxak = not ack
            self.data, self.pending_rx = self.pending_rx, None
            self.log.append(("rx", self.data, ack))
        self.ibif = True

    @property
    def enabled(self):
        return not self.ibcr & self.MDIS

    @property
    def master(self):
        return self.enabled and bool(self.ibcr & self.MSSL)

    # ---- register access
    def read(self, offset, size):
        _check(self.name, offset, size, (1, 2, 4, 8), 8)
        value = 0
        for i in range(size):
            value = (value << 8) | self._read8(offset + i)
        return value

    def write(self, offset, size, value):
        _check(self.name, offset, size, (1, 2, 4), 8)
        for i in range(size):
            self._write8(offset + i, (value >> (8 * (size - 1 - i))) & 0xFF)

    def _read8(self, reg):
        if self.clock is not None:
            self._settle()
        if reg == 0: return self.ibad
        if reg == 1: return self.ibfd
        if reg == 2: return self.ibcr
        if reg == 3: return self.status()
        if reg == 4:
            value = self.data
            if self.master and not self.ibcr & self.TXRX:
                if self._busy():
                    raise BoardModelError(f"{self.name}: IBDR read while a byte is still being received")
                self._receive()
            return value
        if reg == 5: return self.ibic
        if reg == 6: return self.ibdbg
        return 0                                           # +7 reserved, no bus error (RM 23.5)

    def status(self):
        if not self.enabled:
            return self.TCF                                # reset value; "not valid" while disabled
        busy = self._busy()
        return ((0 if busy else self.TCF) | (self.IBB if self.ibb else 0) | (self.IBAL if self.ibal else 0)
                | (self.IBIF if self.ibif and not busy else 0) | (self.RXAK if self.rxak else 0))

    def _write8(self, reg, value):
        if self.clock is not None:
            self._settle()
        if reg == 0: self.ibad = value
        elif reg == 1: self.ibfd = value
        elif reg == 2: self._set_ibcr(value)
        elif reg == 3:                                     # w1c IBIF, IBAL
            if value & self.IBIF: self.ibif = False
            if value & self.IBAL: self.ibal = False
        elif reg == 4:
            self.data = value
            if self.master and self.ibcr & self.TXRX:
                if self._busy():
                    raise BoardModelError(f"{self.name}: IBDR written while a byte is still in flight")
                self._transmit(value)
        elif reg == 5: self.ibic = value
        elif reg == 6: self.ibdbg = value

    def _set_ibcr(self, value):
        old, new = self.ibcr, value & ~self.RSTA
        if old & self.MDIS:
            if not value & self.MDIS and value & self.MSSL:
                raise BoardModelError(f"{self.name}: IBCR {value:#04x} clears MDIS and sets MSSL in one write")
        if value & self.MSSL and not old & self.MSSL and self.ibfd == 0:
            raise BoardModelError(f"{self.name}: START with IBFD = 0 (reset value): no divider was set, nothing would complete")
            self.ibcr = new
            return
        if value & self.MDIS:                              # module reset; slaves just see the end
            if self.ibb:
                self.bus.stop()
            self._reset_status()
            self.ibcr = new
            return
        was, now = bool(old & self.MSSL), bool(value & self.MSSL)
        if was != now and self._busy():
            raise BoardModelError(f"{self.name}: START/STOP while a byte is in flight")
        self.ibcr = new
        if now and not was:
            if self.ibb:
                self.ibal = self.ibif = True
            else:
                self.ibb, self.phase = True, "addr"
                self.log.append(("start", None, None))
        elif was and not now:
            self.bus.stop()
            self.ibb, self.phase = False, None
            self.log.append(("stop", None, None))
            if self.ibic & 0x80:                           # BIIE: bus-idle interrupt
                self.ibif = True
        elif now and value & self.RSTA:
            if self._busy():
                raise BoardModelError(f"{self.name}: repeated START while a byte is in flight")
            self.bus.restart()
            self.phase = "addr"
            self.log.append(("restart", None, None))

    def _transmit(self, byte):
        if self.phase == "addr":
            ack = self.bus.address(byte >> 1, bool(byte & 1))
            self.phase = ("read" if byte & 1 else "write") if ack else "nack"
            self.log.append(("addr", byte, ack))
        elif self.phase == "write":
            ack = self.bus.write_byte(byte)
            self.log.append(("tx", byte, ack))
        else:                                              # nobody is listening
            ack = False
            self.log.append(("tx", byte, ack))
        self.rxak = not ack
        self._begin(False)

    def _receive(self):
        self.pending_rx = self.bus.read_byte() if self.phase == "read" else 0xFF
        self._begin(True)


class Pca9545:
    """PCA9545 4-channel mux: control register bits 3:0 = channel enables, 7:4 = INT3..0."""

    def __init__(self, int_bits=0):
        self.control, self.active, self.int_bits = 0, 0, int_bits
        self.channels = [[], [], [], []]

    def downstream(self):
        for c, channel in enumerate(self.channels):
            if self.active & (1 << c):
                yield from channel

    def address(self, read): return True
    def write(self, byte):
        self.control = byte & 0x0F                         # the last byte written wins
        return True
    def read(self): return ((self.int_bits & 0xF) << 4) | self.control
    def ack(self, ack): pass
    def stop(self): self.active = self.control              # selection takes effect at STOP


class Eeprom:
    """256-byte I2C EEPROM page: pointer write, sequential read (wraps), optional writes."""

    def __init__(self, present, writable):
        self.data, self.present, self.writable = bytearray(256), present, writable
        self.pointer, self.expect_pointer, self.nacked = 0, False, False

    def load(self, data):
        if len(data) > 256:
            raise ValueError("an EEPROM page holds 256 bytes")
        self.data = bytearray(data) + bytearray(256 - len(data))

    def address(self, read):
        if not self.present():
            return False
        self.expect_pointer, self.nacked = not read, False
        return True

    def write(self, byte):
        if not self.present():
            return False
        if self.expect_pointer:
            self.pointer, self.expect_pointer = byte, False
        else:
            if self.writable:
                self.data[self.pointer] = byte
            self.pointer = (self.pointer + 1) & 0xFF
        return True

    def read(self):
        if self.nacked or not self.present():
            return 0xFF
        value = self.data[self.pointer]
        self.pointer = (self.pointer + 1) & 0xFF
        return value

    def ack(self, ack):
        if not ack:
            self.nacked = True

    def stop(self):
        self.expect_pointer = False


class SfpModule:
    """The I2C side of one cage: A0h (0x50, read-only) and A2h (0x51, writable)."""

    def __init__(self):
        self.inserted, self.ready, self.has_a2 = False, False, False
        self.a0 = Eeprom(lambda: self.inserted and self.ready, writable=False)
        self.a2 = Eeprom(lambda: self.inserted and self.ready and self.has_a2, writable=True)


class Ds100df410:
    """DS100DF410 retimer: register 0xff selects the page; other registers are per-page bytes."""

    BASELINE = {4: {0x1E: 0x09}, 5: {0x1E: 0x09}, 6: {0x1E: 0xE9}, 7: {0x1E: 0xE9}}

    def __init__(self):
        self.page, self.pages = 0, {}
        self.pointer, self.expect_pointer, self.nacked = 0, False, False
        self.store(0)[0x01] = 0xD0
        for page, regs in self.BASELINE.items():
            for reg, value in regs.items():
                self.store(page)[reg] = value

    def store(self, page):
        return self.pages.setdefault(page, bytearray(256))

    def reg(self, page, reg):
        return self.page if reg == 0xFF else self.store(page)[reg]

    def address(self, read):
        self.expect_pointer, self.nacked = not read, False
        return True

    def write(self, byte):
        if self.expect_pointer:
            self.pointer, self.expect_pointer = byte, False
            return True
        if self.pointer == 0xFF:
            self.page = byte
        elif not (self.page == 0 and self.pointer == 0x01):  # device ID is read-only
            self.store(self.page)[self.pointer] = byte
        self.pointer = (self.pointer + 1) & 0xFF
        return True

    def read(self):
        if self.nacked:
            return 0xFF
        value = self.reg(self.page, self.pointer)
        self.pointer = (self.pointer + 1) & 0xFF
        return value

    def ack(self, ack):
        if not ack:
            self.nacked = True

    def stop(self):
        self.expect_pointer = False


# --------------------------------------------------------------------------- GPIO

class Gpio(WordDevice):
    """QorIQ GPIO block (GPIO3): GPDIR +0, GPODR +4, GPDAT +8, GPIER +0xc, GPIMR +0x10,
    GPICR +0x14, GPIBE +0x18.  GPDAT reads output latches for output pins and the
    pin levels for input pins."""
    name, span = "GPIO3", 0x1C

    def __init__(self, gpdir=GPDIR_BOOT, gpdat=GPDAT_BOOT):
        self.regs = {0x00: gpdir, 0x04: 0, 0x08: gpdat, 0x0C: 0, 0x10: 0, 0x14: 0, 0x18: 0}
        self.pins = gpdat                                  # externally driven levels

    def read(self, offset, size):
        _check(self.name, offset, size, (4,), 0x10000)
        if offset >= self.span:
            raise BusError(f"{self.name}: nothing at offset {offset:#x}")
        return self.read32(offset)

    def write(self, offset, size, value):
        _check(self.name, offset, size, (4,), 0x10000)
        if offset >= self.span:
            raise BusError(f"{self.name}: nothing at offset {offset:#x}")
        self.write32(offset, value & M32)

    def read32(self, offset):
        if offset == 0x08:
            direction = self.regs[0x00]
            return (self.regs[0x08] & direction) | (self.pins & ~direction & M32)
        return self.regs[offset]

    def write32(self, offset, value):
        if offset == 0x0C:
            self.regs[0x0C] &= ~value                      # GPIER w1c
        else:
            self.regs[offset] = value

    def drive(self, mask, level):
        self.pins = (self.pins | mask) if level else (self.pins & ~mask)

    def output(self, mask):
        """Level driven by the SoC on an output pin (None if the pin is an input)."""
        if not self.regs[0x00] & mask:
            return None
        return bool(self.regs[0x08] & mask)


# --------------------------------------------------------------------------- SerDes

GCR0_RRST_B, GCR0_TRST_B, GCR0_RX_PD, GCR0_TX_PD = 0x00400000, 0x00200000, 0x00100000, 0x00080000
GCR0_PROTS, PROTS_SGMII, PROTS_XFI = 0x00000F80, 0x00000080, 0x00000500
PCCR8, PCCRB, PLL2_CR0 = 0x220, 0x22C, 0x024
PCCR8_ROUTE = ((0x00070000, 0x00010000), (0x00700000, 0x00100000))   # per cage: (mask, 1G value)
PCCRB_ROUTE = ((0x07000000, 0x01000000), (0x70000000, 0x20000000))   # per cage: (mask, 10G value)


def lane_regs(lane):
    g = 0x800 + 0x40 * lane
    return {"GCR0": g, "GCR1": g + 4, "RECR0": g + 0x10, "TECR0": g + 0x18, "TTLCR0": g + 0x20,
            "SGMII_CR0": 0x1800 + 0x10 * lane, "SGMII_CR1": 0x1804 + 0x10 * lane}


class Serdes1(WordDevice):
    """SerDes1 registers of the two cage lanes (LS1046A RM, SerDes chapter): protocol
    configuration PCCR8/PCCRB, PLL2_CR0, per-lane GCR0/GCR1/RECR0/TECR0/TTLCR0 and
    SGMII control.  Plain storage with the boot snapshot of SerDes protocol 0x1133.
    Unlisted offsets raise BusError."""
    name, span = "SerDes1", 0x10000
    BOOT_LANE = {"GCR0": 0x33651500,                       # BOARD
                 "GCR1": 0x00000022, "RECR0": 0x10000400, "TECR0": 0x10233007,   # ASSUMED: 10G profile values
                 "TTLCR0": 0x00000400,                     # BOARD-HOST: 1G readback 0x39000400 minus FLT_SEL
                 "SGMII_CR0": 0x40000000, "SGMII_CR1": 0x00000000}               # ASSUMED: SGMII powered down

    def __init__(self):
        self.regs = {PCCR8: 0x11000000, PCCRB: 0x21000000, PLL2_CR0: 0x20860008}
        for lane in CAGE_LANES:
            for name, offset in lane_regs(lane).items():
                self.regs[offset] = self.BOOT_LANE[name]

    def read32(self, offset):
        if offset not in self.regs:
            raise BusError(f"{self.name}: register {offset:#x} not modelled")
        return self.regs[offset]

    def write32(self, offset, value):
        if offset not in self.regs:
            raise BusError(f"{self.name}: register {offset:#x} not modelled")
        self.regs[offset] = value

    def lane(self, cage, name):
        return self.regs[lane_regs(CAGE_LANES[cage])[name]]

    def lane_mode(self, cage):
        gcr0 = self.lane(cage, "GCR0")
        smask, sval = PCCR8_ROUTE[cage]
        xmask, xval = PCCRB_ROUTE[cage]
        sgmii, xfi = self.regs[PCCR8] & smask, self.regs[PCCRB] & xmask
        if gcr0 & GCR0_PROTS == PROTS_XFI and xfi == xval and sgmii == 0:
            return "10g"
        if (gcr0 & GCR0_PROTS == PROTS_SGMII and sgmii == sval and xfi == 0
                and self.lane(cage, "SGMII_CR1") & 0x800 and self.lane(cage, "SGMII_CR0") & 0xC0000000 == 0x80000000):
            return "1g"
        return None

    def lane_powered(self, cage):
        gcr0 = self.lane(cage, "GCR0")
        return gcr0 & (GCR0_RRST_B | GCR0_TRST_B | GCR0_RX_PD | GCR0_TX_PD) == GCR0_RRST_B | GCR0_TRST_B


# --------------------------------------------------------------------------- mEMAC

class Memac(WordDevice):
    """mEMAC register page (4 KiB).  COMMAND_CONFIG SW_RESET (0x1000) reads back set
    `swr_reads` times, then clears; everything else is plain storage."""
    COMMAND_CONFIG, MAXFRM, TX_FIFO_SECTIONS, IF_MODE = 0x008, 0x014, 0x020, 0x300
    RX_EN, TX_EN, SW_RESET = 0x2, 0x1, 0x1000

    def __init__(self, name, ten_g, swr_reads=1):
        self.name, self.swr_reads, self.swr_left = name, swr_reads, 0
        self.regs = {self.COMMAND_CONFIG: 0x00000840, self.MAXFRM: 0x00000600,
                     self.TX_FIFO_SECTIONS: 0x00400019 if ten_g else 0x00100020,   # ASSUMED (Linux values)
                     self.IF_MODE: 0x00000000 if ten_g else 0x00000002}           # ASSUMED (Linux values)
        self.resets = 0

    def read32(self, offset):
        value = self.regs.get(offset, 0)
        if offset == self.COMMAND_CONFIG and value & self.SW_RESET:
            if self.swr_left:
                self.swr_left -= 1
            else:
                value &= ~self.SW_RESET
                self.regs[offset] = value
        return value

    def write32(self, offset, value):
        if offset == self.COMMAND_CONFIG and value & self.SW_RESET:
            self.resets += 1
            self.swr_left = self.swr_reads
            if not self.swr_left:
                value &= ~self.SW_RESET
        self.regs[offset] = value

    @property
    def enabled(self):
        return self.regs[self.COMMAND_CONFIG] & 3 == 3


# --------------------------------------------------------------------------- MDIO

class LatchedLink:
    """Link state with IEEE latched-low status bits (one latch per register)."""

    def __init__(self):
        self.up, self.latched = False, {}

    def set(self, up):
        if self.up and not up:
            for key in self.latched:
                self.latched[key] = True
        self.up = up

    def read(self, key):
        value = self.up and not self.latched.get(key, False)
        self.latched[key] = False
        return value


class Gpy115c:
    """MaxLinear GPY115C: Clause 22 registers, MMD via Clause 45 or regs 0x0d/0x0e."""
    DEFAULTS = {0x00: 0x1040, 0x02: 0x67C9, 0x03: 0xDF10, 0x04: 0x01E1, 0x09: 0x0300}

    def __init__(self):
        self.link = LatchedLink()
        self.speed, self.duplex = 1000, 1
        self.mmd = {}
        self.reset()

    def reset(self):
        self.regs = dict(self.DEFAULTS)
        self.resets = getattr(self, "resets", -1) + 1

    @property
    def powered_down(self):
        """BMCR power-down (bit 11): no link pulses, the partner sees no link."""
        return bool(self.regs[0x00] & 0x0800)

    @property
    def partner_link(self):
        """What the link partner sees: link only while a cable is there and the PHY is powered."""
        return self.link.up and not self.powered_down

    def miistat(self):
        speed = {10: 0, 100: 1, 1000: 2, 2500: 4}[self.speed]
        return (0x0400 if self.partner_link else 0) | (0x0008 if self.duplex else 0) | speed

    def c22_read(self, reg):
        if reg == 0x01:
            latched = self.link.read("bmsr") and not self.powered_down
            return 0x7949 | (0x0004 if latched else 0) | (0x0020 if self.partner_link else 0)
        if reg == 0x18:
            return self.miistat()
        if reg == 0x0E:
            return self._mmd_indirect(None)
        return self.regs.get(reg, 0)

    def c22_write(self, reg, value):
        if reg == 0x00 and value & 0x8000:
            self.reset()                                   # BMCR reset self-clears
        elif reg in (0x01, 0x02, 0x03, 0x18):
            pass                                           # read-only
        elif reg == 0x0E:
            self._mmd_indirect(value)
        else:
            self.regs[reg] = value

    def _mmd_indirect(self, value):
        ctl = self.regs.get(0x0D, 0)
        dev, function = ctl & 0x1F, ctl >> 14
        if function == 0:
            if value is None:
                return self.regs.get(0x0E, 0)
            self.regs[0x0E] = value
            return None
        address = self.regs.get(0x0E, 0)
        if value is None:
            result = self.c45_read(dev, address)
        else:
            self.c45_write(dev, address, value)
            result = None
        if function == 2:                                  # post-increment
            self.regs[0x0E] = (address + 1) & 0xFFFF
        return result

    def c45_read(self, dev, reg):
        return self.mmd.get((dev, reg), 0)

    def c45_write(self, dev, reg, value):
        self.mmd[(dev, reg)] = value


class Pcs:
    """Internal PCS at port address 0 of a MAC's MDIO block: Clause 22 plain storage
    with BMSR link (latched low); Clause 45 device 3 register 1 link bit (latched low)."""

    def __init__(self):
        self.link = LatchedLink()
        self.regs = {0x00: 0x1140}
        self.mmd = {}

    def c22_read(self, reg):
        if reg == 0x01:
            return 0x0109 | (0x0004 if self.link.read("bmsr") else 0) | (0x0020 if self.link.up else 0)
        return self.regs.get(reg, 0)

    def c22_write(self, reg, value):
        if reg != 0x01:
            self.regs[reg] = value & ~0x8000 if reg == 0 else value

    def c45_read(self, dev, reg):
        if (dev, reg) == (3, 0x0001):
            return 0x0004 if self.link.read("pcs_status1") else 0
        if (dev, reg) == (3, 0x0020):
            return 0x1001 if self.link.up else 0
        return self.mmd.get((dev, reg), 0)

    def c45_write(self, dev, reg, value):
        self.mmd[(dev, reg)] = value


class MdioController(WordDevice):
    """FMan MDIO block (DPAA RM 6.4.3.4): CFG +0x30, CTL +0x34, DATA +0x38, ADDR +0x3c.

    A transaction keeps CFG[BSY] (0x80000000) set for `busy_polls` CFG reads
    (forever when MDIO_CLK_DIV is 0), then sets CMP and RD_ER and latches the
    read data.  Starting a transaction while busy raises BoardModelError."""
    CFG, CTL, DATA, ADDR = 0x30, 0x34, 0x38, 0x3C
    BSY, CMP, CIM, NEG, EHOLD = 0x80000000, 0x40000000, 0x20000000, 0x00800000, 0x00400000
    CLK_DIV, ENC45, PRE, HOLD, RD_ER = 0x0000FF80, 0x40, 0x20, 0x1C, 0x2
    READ, POST_INC = 0x8000, 0x4000

    def __init__(self, name, devices, external, cfg=None, busy_polls=1):
        self.name, self.devices, self.external, self.busy_polls = name, devices, external, busy_polls
        reset = 0x00800048 if external else 0x00001448
        self.cfg = reset if cfg is None else cfg
        self.ctl = self.data = self.addr = 0
        self.busy, self.pending = False, None
        self.log = []

    def read32(self, offset):
        if offset == self.CFG:
            value = self.cfg | (self.BSY if self.busy else 0)
            if self.busy and self.cfg & self.CLK_DIV:
                self.busy_left -= 1
                if self.busy_left <= 0:
                    self._complete()
            return value
        if offset == self.CTL: return self.ctl
        if offset == self.DATA: return self.data
        if offset == self.ADDR: return 0                   # write-only
        raise BusError(f"{self.name}: nothing at offset {offset:#x}")

    def write32(self, offset, value):
        if offset == self.CFG:
            writable = self.CIM | self.CLK_DIV | self.ENC45 | self.PRE | self.HOLD
            if self.external:
                writable |= self.NEG | self.EHOLD
            keep = self.cfg & (self.CMP | self.RD_ER) & ~(value & self.CMP)
            self.cfg = (value & writable) | keep
            return
        if offset not in (self.CTL, self.DATA, self.ADDR):
            raise BusError(f"{self.name}: nothing at offset {offset:#x}")
        if self.busy:
            raise BoardModelError(f"{self.name}: write of {value:#x} to +{offset:#x} while MDIO_CFG[BSY]")
        port, devreg = (self.ctl >> 5) & 0x1F, self.ctl & 0x1F
        c45 = bool(self.cfg & self.ENC45)
        if offset == self.CTL:
            self.ctl = value & 0x3FF
            port, devreg = (self.ctl >> 5) & 0x1F, self.ctl & 0x1F
            if value & (self.READ | self.POST_INC):
                dev = self.devices.get(port)
                if dev is None:
                    result = None
                elif c45:
                    result = dev.c45_read(devreg, self.addr)
                else:
                    result = dev.c22_read(devreg)
                if c45 and value & self.POST_INC:
                    self.addr = (self.addr + 1) & 0xFFFF
                self.log.append(("c45" if c45 else "c22", "read", port, devreg, self.addr if c45 else None, result))
                self._start(("read", result))
        elif offset == self.DATA:
            dev = self.devices.get(port)
            if dev is not None:
                if c45:
                    dev.c45_write(devreg, self.addr, value & 0xFFFF)
                else:
                    dev.c22_write(devreg, value & 0xFFFF)
            self.log.append(("c45" if c45 else "c22", "write", port, devreg, self.addr if c45 else None, value & 0xFFFF))
            self._start(("write", None))
        else:
            self.addr = value & 0xFFFF
            if c45:
                self._start(("address", None))

    def _start(self, pending):
        self.pending, self.busy = pending, True
        self.busy_left = self.busy_polls
        if not self.busy_polls and self.cfg & self.CLK_DIV:
            self._complete()

    def _complete(self):
        kind, result = self.pending
        self.busy, self.pending = False, None
        self.cfg |= self.CMP
        if kind == "read":
            if result is None:
                self.cfg |= self.RD_ER
                self.data = 0xFFFF
            else:
                self.cfg &= ~self.RD_ER
                self.data = result & 0xFFFF


# --------------------------------------------------------------------------- board

class _ExternalView:
    """Adapter to ExternalBus devices: read(offset, n) -> bytes, write(offset, data)."""

    def __init__(self, device):
        self.device = device

    def read(self, offset, n):
        return self.device.read(offset, n).to_bytes(n, "big")

    def write(self, offset, data):
        self.device.write(offset, len(data), int.from_bytes(data, "big"))


class _LocalView:
    """Adapter to LocalBus devices: read32(offset), write32(offset, value)."""

    def __init__(self, device):
        self.device = device

    def read32(self, offset): return self.device.read(offset, 4)
    def write32(self, offset, value): self.device.write(offset, 4, value)


class Board:
    """All modelled board devices.  Cages start empty (MOD_ABS = 1);
    insert both modules to get the boot GPDAT 0xb18009e0."""

    def __init__(self, *, clock=None, i2c_byte_ticks=0, mdio_busy_polls=1, emi2_cfg=0x00801448,
                 sfp_mux_int=0x3, i2c_dma_granule=8, cold=False):
        # cold: registers as EDK2 leaves them (board, 2026-11-15) instead of as U-Boot does:
        # no I2C divider, every GPIO3 pin an input, no MDIO clock divider on EMI2
        self.i2c_dma_granule = i2c_dma_granule
        self.i2c1_bus, self.i2c2_bus = I2cBus("I2C1"), I2cBus("I2C2")
        self.sfp_mux = self.i2c2_bus.attach(0x70, Pca9545(sfp_mux_int))
        self.retimer_mux = self.i2c1_bus.attach(0x70, Pca9545(0))
        self.modules = [SfpModule(), SfpModule()]
        for cage, module in enumerate(self.modules):
            self.sfp_mux.channels[cage] += [(0x50, module.a0), (0x51, module.a2)]
        self.retimer = Ds100df410()
        self.retimer_mux.channels[0].append((0x18, self.retimer))
        self.i2c1 = I2cController("I2C1", self.i2c1_bus, ibfd=0 if cold else 0x3E, clock=clock, byte_ticks=i2c_byte_ticks)
        self.i2c2 = I2cController("I2C2", self.i2c2_bus, ibfd=0 if cold else 0x3E, clock=clock, byte_ticks=i2c_byte_ticks)
        self.gpio = Gpio(gpdir=GPDIR_RESET if cold else GPDIR_BOOT)
        self.serdes = Serdes1()
        self.altcbar = Word("SCFG_ALTCBAR", 0)             # ASSUMED boot value
        self.rcwcr5 = Word("DCSR_RCWCR5", 0x11335A06)
        self.phys = {address: Gpy115c() for address in (0, 1, 2)}
        self.emi2 = MdioController("EMI2", self.phys, external=True, cfg=0x00800008 if cold else emi2_cfg, busy_polls=mdio_busy_polls)
        self.pcs = {name: Pcs() for name in MACS}
        self.pcs_mdio = {name: MdioController(f"{name} PCS MDIO", {0: self.pcs[name]}, external=False,
                                              busy_polls=mdio_busy_polls) for name in MACS}
        self.memacs = {name: Memac(name, name in CAGE_MACS) for name in MACS}
        self.fman_blocks = {EMI2: self.emi2}
        for name, base in MACS.items():
            self.fman_blocks[base] = self.memacs[name]
            self.fman_blocks[base + 0x1000] = self.pcs_mdio[name]
        for cage in (0, 1):
            self._cage_pins(cage, present=False)

    # ---- mounting
    def ccsr_regions(self):
        """[(CCSR base, size, device)] of every device, FMan blocks included."""
        regions = [(I2C1_BASE, 0x10000, self.i2c1), (I2C2_BASE, 0x10000, self.i2c2),
                   (GPIO3_BASE, 0x10000, self.gpio), (SERDES1_BASE, 0x10000, self.serdes),
                   (SCFG_ALTCBAR, 4, self.altcbar), (DCSR_RCWCR5, 4, self.rcwcr5)]
        regions += [(FMAN_CCSR + base, 0x1000, dev) for base, dev in sorted(self.fman_blocks.items())]
        return regions

    def attach(self, external_bus):
        """Map everything on an emu.bus.ExternalBus and install the I2C read widening
        (a DMA read of any byte of an I2C block reads all 8 bytes, IBDR included)."""
        for base, size, device in self.ccsr_regions():
            external_bus.map(base, size, _ExternalView(device))
        if self.i2c_dma_granule:
            previous, granule = external_bus.read_span, self.i2c_dma_granule

            def read_span(address, n):
                for base in (I2C1_BASE, I2C2_BASE):
                    if base <= address and address + n <= base + granule:
                        return base, granule
                return previous(address, n) if previous else (address, n)
            external_bus.read_span = read_span
        return self

    def attach_fman(self, local_bus):
        """Map the FMan blocks at their FMan offsets on an emu.bus.LocalBus (unit 12)."""
        for base, device in self.fman_blocks.items():
            local_bus.map(base, 0x1000, _LocalView(device))
        return self

    def _fman_block(self, offset):
        device = self.fman_blocks.get(offset & ~0xFFF)
        return device, offset & 0xFFF

    def fman_read32(self, offset):
        device, local = self._fman_block(offset)
        return None if device is None else device.read(local, 4)

    def fman_write32(self, offset, value):
        device, local = self._fman_block(offset)
        if device is None:
            return None
        device.write(local, 4, value)
        return True

    # ---- test-facing controls
    def _cage_pins(self, cage, present):
        self.gpio.drive(MOD_ABS[cage], not present)
        self.gpio.drive(RX_LOS[cage], not present)

    def insert_module(self, cage, a0, a2=None, ready=True):
        module = self.modules[cage]
        module.a0.load(a0)
        module.has_a2 = a2 is not None
        if a2 is not None:
            module.a2.load(a2)
        module.inserted, module.ready = True, ready
        self._cage_pins(cage, present=True)

    def set_module_ready(self, cage, ready=True):
        self.modules[cage].ready = ready

    def remove_module(self, cage):
        self.modules[cage].inserted = self.modules[cage].ready = False
        self._cage_pins(cage, present=False)
        self.set_pcs_link(CAGE_MACS[cage], False)

    def set_los(self, cage, los):
        self.gpio.drive(RX_LOS[cage], los)

    def tx_enabled(self, cage):
        return bool(self.gpio.output(TX_GATE[cage]))

    @staticmethod
    def mac_name(mac):
        """A MAC name ("MAC9") or its Rx PortID (0x10) -> the MAC name."""
        if isinstance(mac, int):
            for name, port_id in RX_PORT_IDS.items():
                if port_id == mac:
                    return name
            raise ValueError(f"no MAC with Rx PortID {mac:#x} on this board")
        if mac not in MACS:
            raise ValueError(f"unknown MAC {mac!r}")
        return mac

    def phy_powered(self, index):
        return not self.phys[PHY_ADDRESS[index] if isinstance(index, str) else index].powered_down

    def set_phy_link(self, index, up, speed=1000, duplex=1):
        """index = EMI2 PHY address 0..2, or a MAC name ('MAC5' -> 0, 'MAC6' -> 1, 'MAC2' -> 2)."""
        phy = self.phys[PHY_ADDRESS[index] if isinstance(index, str) else index]
        phy.speed, phy.duplex = speed, duplex
        phy.link.set(up)

    def set_pcs_link(self, mac, up):
        self.pcs[self.mac_name(mac)].link.set(up)

    def lane_mode(self, cage):
        return self.serdes.lane_mode(cage)

    def lane_powered(self, cage):
        return self.serdes.lane_powered(cage)

    def mac_enabled(self, mac):
        return self.memacs[self.mac_name(mac)].enabled
