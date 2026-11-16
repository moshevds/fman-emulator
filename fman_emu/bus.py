"""Memory and bus plumbing.

An *address space* is any object with `read(address, n) -> bytes` and
`write(address, data)`.  The core uses three kinds:

* MURAM and per-task context windows (r26/r28/r29): plain address spaces;
* the external bus reached by controller DMA: `dma_read(address, n) -> bytes`
  and `dma_write(address, data)`;
* the FMan-local bus reached by unit 12: `read32(address)` / `write32(address, value)`.
"""
import struct


class EmuError(Exception):
    pass


class BusError(EmuError):
    pass


class Memory:
    """Flat big-endian byte memory starting at `base`."""

    def __init__(self, size, base=0, name="memory"):
        self.base, self.size, self.name = base, size, name
        self.data = bytearray(size)

    def _at(self, address, n):
        offset = address - self.base
        if offset < 0 or offset + n > self.size:
            raise BusError(f"{self.name}: access of {n} byte(s) at {address:#x} is outside {self.base:#x}..{self.base + self.size:#x}")
        return offset

    def read(self, address, n):
        offset = self._at(address, n)
        return bytes(self.data[offset:offset + n])

    def write(self, address, data):
        offset = self._at(address, len(data))
        self.data[offset:offset + len(data)] = data

    def r8(self, a): return self.read(a, 1)[0]
    def r16(self, a): return struct.unpack(">H", self.read(a, 2))[0]
    def r32(self, a): return struct.unpack(">I", self.read(a, 4))[0]
    def w8(self, a, v): self.write(a, bytes([v & 0xFF]))
    def w16(self, a, v): self.write(a, struct.pack(">H", v & 0xFFFF))
    def w32(self, a, v): self.write(a, struct.pack(">I", v & 0xFFFFFFFF))


class Window:
    """Binding of a contextual address register (r26, r28 or r29) for one task.

    Memory accesses through the register go to `space` at `base + displacement`;
    reading the register as a scalar gives `scalar` (default: `base`).  On the
    board the frame window and parameter page live in MURAM, so the usual
    binding is `Window(cpu.muram, address)`.
    """

    def __init__(self, space, base=0, scalar=None):
        self.space, self.base = space, base
        self.scalar = base if scalar is None else scalar


class ExternalBus:
    """External system memory as seen by controller DMA: 64-bit byte addresses.

    Devices are mapped with `map(base, size, device)`; a device has
    `read(offset, n) -> bytes` and `write(offset, data)`.  Unmapped accesses
    raise BusError.

    Board finding: a DMA *read* can touch more of the device than the bytes it
    delivers.  That is the bus's business, not the core's: set `read_span` to a
    function `(address, n) -> (address', n')` naming the span really read
    (it must contain the requested one).  The whole span is read from the
    device (with its side effects); the core still receives exactly `n` bytes.
    """

    def __init__(self, read_span=None):
        self.regions = []
        self.read_span = read_span
        self.log = []                      # ("r"/"w", address, length) of every bus access

    def map(self, base, size, device):
        self.regions.append((base, size, device))
        return device

    def add_ram(self, base, size, name="ram"):
        return self.map(base, size, Memory(size, 0, name))

    def _find(self, address, n):
        for base, size, device in self.regions:
            if base <= address and address + n <= base + size:
                return device, address - base
        raise BusError(f"external bus: nothing mapped for {n} byte(s) at {address:#x}")

    def dma_read(self, address, n):
        start, length = self.read_span(address, n) if self.read_span else (address, n)
        if start > address or start + length < address + n:
            raise BusError("read_span must contain the requested range")
        device, offset = self._find(start, length)
        self.log.append(("r", start, length))
        data = device.read(offset, length)
        return bytes(data[address - start:address - start + n])

    def dma_write(self, address, data):
        device, offset = self._find(address, len(data))
        self.log.append(("w", address, len(data)))
        device.write(offset, bytes(data))


class LocalBus:
    """The FMan-local 32-bit space of unit 12: MURAM at its own addresses plus
    module registers.  Registers are added with `map(base, size, device)`, the
    device having `read32(offset)` and `write32(offset, value)`, or with
    `register(address, value)` for a plain storage word.  Addresses are
    word-aligned by the core.  Unmapped addresses raise BusError (on the board
    an unmapped address such as plain DDR reads as zero; set `unmapped_reads_zero`)."""

    def __init__(self, muram, unmapped_reads_zero=False):
        self.muram, self.regions, self.words = muram, [], {}
        self.unmapped_reads_zero = unmapped_reads_zero

    def map(self, base, size, device):
        self.regions.append((base, size, device))
        return device

    def register(self, address, value=0):
        self.words[address] = value & 0xFFFFFFFF

    def _find(self, address):
        for base, size, device in self.regions:
            if base <= address < base + size:
                return device, address - base
        return None, 0

    def read32(self, address):
        if self.muram.base <= address < self.muram.base + self.muram.size:
            return self.muram.r32(address)
        if address in self.words:
            return self.words[address]
        device, offset = self._find(address)
        if device is not None:
            return device.read32(offset) & 0xFFFFFFFF
        if self.unmapped_reads_zero:
            return 0
        raise BusError(f"FMan-local bus: nothing mapped at {address:#x}")

    def write32(self, address, value):
        if self.muram.base <= address < self.muram.base + self.muram.size:
            return self.muram.w32(address, value)
        if address in self.words:
            self.words[address] = value & 0xFFFFFFFF
            return
        device, offset = self._find(address)
        if device is None:
            raise BusError(f"FMan-local bus: nothing mapped at {address:#x}")
        device.write32(offset, value & 0xFFFFFFFF)
