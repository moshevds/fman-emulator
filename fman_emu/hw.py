"""Behavioural model of the FMan hardware around the controllers.

BMI receive, transmit and offline ports creating tasks, the internal 256-byte
buffers, the port registers the controller uses through unit 12, the FPM
external-request lines and controller events, and a scheduler for the four
controllers.  Register names and offsets follow the LS1046A DPAA reference
manual (BMI, FPM); task behaviour follows the qe-isa-research measurements
(docs/fman-task-lifecycle.md, hardware-task-scheduling.md) and measurements on
an LS1046A board.  Behaviour that is known only from how existing microcode
uses the hardware, or that is simply assumed, is marked ASSUMED; microcode
tested here must not be considered proven on those points until real hardware
agrees.

Hardware facts the model relies on:

* Reset: each of the four controllers runs I-RAM word 0 once, as its own
  context (TNUMs 0..3 stand for them here).  `regpair.init` on r26/r28 has a
  contextual effect that is not observable; it is ignored.
* Concurrency: up to four stages run at once, one per controller.  A
  constrained PortID (FMFP_PRC) has at most one executing stage at a time,
  on whichever controller; the model treats every PortID as constrained.
"""
import random
import struct
from collections import deque

from .bus import Memory, Window, EmuError
from .core import Cpu
from .task import Task, WAIT, PARK, HANDOFF, REDISPATCH, BOUNDARY, COMPLETE, SM_RETRY, STEPS

BMI, FPM = 0x80000, 0xC3000
FPM_TNC, FPM_PRC, FPM_FCEV, FPM_CEE, FPM_TSP = 0x00, 0x04, 0x20, 0x40, 0x68
FPM_EXTC, FPM_EXT1, FPM_EXT2, FPM_CEV = 0x74, 0x78, 0x7C, 0xE0
NIA_RELEASE, NIA_DISCARD, NIA_TX, NIA_TX_STOP = 0x5000C0, 0x5000C1, 0x5000D0, 0x500274
EN = 0x80000000


class ModelError(EmuError):
    """The microcode did something the hardware model has no behaviour for."""


class Port:
    def __init__(self, pid, kind):
        self.id, self.kind = pid, kind            # kind: "rx", "tx", "oh"
        self.regs = {}                            # BMI block offset -> word
        self.tasks = set()                        # live TNUMs
        self.wire = []                            # tx: transmitted frames; rx: not used
        self.rx_dropped = 0                       # frames BMI could not stage
        self.cursor = None                        # +0x38c protocol latch
        self.stopped = False
        self.held = False                         # tx: the MAC cannot send (a slow or paused link)
        self.waiting = False                      # tx: BMI owes the controller a task once it can

    @property
    def enabled(self):
        return bool(self.regs.get(0, 0) & EN)

    def nia_reg(self):
        return {"rx": 0x20, "tx": 0x18, "oh": 0x10}[self.kind]

    def page_reg(self):
        return {"rx": 0x60, "tx": 0x20, "oh": 0x60}[self.kind]


def gross_sum(data):
    """Folded, not complemented, one's-complement sum of `data` as big-endian 16-bit words
    (an odd last byte is the high half of a word)."""
    if len(data) & 1:
        data = bytes(data) + b"\0"
    total = sum(int.from_bytes(data[n:n + 2], "big") for n in range(0, len(data), 2))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return total


class Fman:
    """FMan with `cpu` (emu.core.Cpu) inside.

    slice=(lo, hi) with a seed interleaves the controllers at instruction
    granularity (random slices of lo..hi instructions) to expose races;
    without it every stage runs to its boundary.
    """

    def __init__(self, *, seed=None, slice=None, fifo=(0x1000, 0x20000), ddr=(0x80000000, 32 << 20),
                 scramble=True, trace=None, ports=None):
        self.cpu = Cpu(scramble=scramble, trace=trace)
        self.muram = self.cpu.muram
        self.ddr_base = ddr[0]
        self.ddr = self.cpu.external.add_ram(ddr[0], ddr[1], "DDR")
        self.rng = random.Random(seed)
        self.slice = slice
        self.free_buffers = deque(range(fifo[0], fifo[1], 0x100))
        self.buffer_owner = {}                    # buffer address -> tnum
        self.links = {}                           # buffer -> next buffer (internal frame chains)
        self.free_tnums = deque(range(8, 128))
        self.tasks = {}                           # tnum -> Task
        self.ports = {}
        self.ready = deque()
        self.parked = {}                          # tnum -> Task
        self.running = [None] * 4
        self.current = None
        self.fpm = {FPM_EXTC: 0, FPM_EXT1: 0, FPM_EXT2: 0, FPM_PRC: 0, FPM_TNC: 0}
        self.fcev, self.cee = [0] * 4, [0] * 4
        self.time = 0                             # instructions executed
        self.clock = 0                            # FMFP_TSP; advanced by the test, so runs are repeatable
        self.last_idle = False
        self.stats = {"stages": 0, "idle_waits": 0, "rx_released": 0, "tx_sent": 0, "wakes": 0}
        self.common = {0x04: 0x01FF0010, 0x08: 0x007B0000}
        self.cpu.local.map(BMI, 0x40000, _Bmi(self))
        self.cpu.local.map(FPM, 0x200, _Fpm(self))
        hooks = self.cpu.hooks
        for name in ("unit0.startup2", "unit0.startup3", "unit0.startup4"):
            hooks[name] = lambda cpu, c, i: None
        hooks["tnum.alloc"] = self._tnum_alloc
        hooks["regpair.init"] = self._regpair_init
        hooks["frame.base.set"] = self._frame_base_set
        hooks["task.boundary"] = self._boundary
        for pid, kind in (ports or DEFAULT_PORTS).items():
            self.ports[pid] = Port(pid, kind)

    # ---- host view ------------------------------------------------------------
    def host_read32(self, address):
        return self.cpu.local.read32(address)

    def host_write32(self, address, value):
        if BMI <= address < BMI + 0x40000 and (address & 0xFFF) == 0 and address >> 12 != BMI >> 12:
            port = self.ports[(address - BMI) >> 12]
            was = port.enabled
            port.regs[0] = value
            if port.enabled and not was:
                self._enable(port)
            return
        if address == FPM + FPM_EXTC:             # the host raises request lines
            self.fpm[FPM_EXTC] |= value & 0xFF000000
            self._poll_requests()
            return
        if FPM + FPM_CEE <= address < FPM + FPM_CEE + 16:
            self.cee[(address - FPM - FPM_CEE) >> 2] = value
            return
        if FPM + FPM_CEV <= address < FPM + FPM_CEV + 16:   # acknowledge events
            self.fcev[(address - FPM - FPM_CEV) >> 2] &= ~value
            return
        self.cpu.local.write32(address, value)

    def irq(self):
        """Level of the FMan interrupt line as far as controller events go."""
        return any(e & m for e, m in zip(self.fcev, self.cee))

    def load_image(self, words, origin=0):
        self.cpu.load(origin, words)

    def reset(self):
        """Run the reset entry (I-RAM word 0) on each controller."""
        for index in range(4):
            task = Task(index, 0, entry=0)        # TNUMs 0..3 stand for the reset contexts
            task.windows[28] = task.windows[29] = None
            self.current = task
            event = self.cpu.run_stage(index, task)
            self.current = None
            if event.kind == PARK:
                self.parked[task.tnum] = self.tasks[task.tnum] = task
            elif event.kind != HANDOFF:
                raise ModelError(f"reset stage of controller {index} ended with {event.kind}")

    # ---- task creation ----------------------------------------------------------
    def _alloc_buffer(self, tnum):
        if not self.free_buffers:
            return None
        address = self.free_buffers.popleft()
        self.buffer_owner[address] = tnum
        self.muram.write(address, bytes(0x100))
        return address

    def _free_buffer(self, address):
        if self.buffer_owner.pop(address, None) is None:
            raise ModelError(f"internal buffer {address:#x} freed twice or never allocated")
        self.links.pop(address, None)
        self.free_buffers.append(address)

    def _new_task(self, port):
        if not self.free_tnums:
            return None
        tnum = self.free_tnums.popleft()
        ic = self._alloc_buffer(tnum)
        if ic is None:
            self.free_tnums.appendleft(tnum)
            return None
        task = Task(tnum, port.id, entry=port.regs.get(port.nia_reg(), 0) & 0x3FFF)
        task.ic_address = ic
        task.windows = {26: Window(self.muram, ic), 28: None, 29: None}
        self.muram.w32(ic + 0x10, port.regs.get(port.page_reg(), 0))
        task.boundary_nia = None
        self.tasks[tnum] = task
        port.tasks.add(tnum)
        return task

    def _enable(self, port):
        port.stopped = False
        nia = port.regs.get(port.nia_reg(), 0)
        if port.kind == "rx":
            return
        if nia & 0xFF0000:
            raise ModelError(f"port {port.id:#x}: dequeue NIA {nia:#x} is not a controller action (QMI is not modelled)")
        pp = self.common.get(0x104 + (port.id - 1) * 4, 0)
        count = ((pp >> 24) & 0x3F) + 1 if port.kind == "oh" else ((port.regs.get(0x0C, 0) >> 12) & 0xF) + 1
        for _ in range(count):
            task = self._new_task(port)
            if task is None:
                raise ModelError("out of TNUMs or internal buffers while enabling a port")
            self.ready.append(task)

    def hold(self, pid, held):
        """Test hook: a transmit port whose link cannot take frames (held) gets no new
        transmit task after its current one; released, it gets one again."""
        port = self.ports[pid]
        port.held = held
        if not held and port.waiting:
            port.waiting = False
            if port.enabled and not port.stopped:
                fresh = self._new_task(port)
                if fresh is None:
                    raise ModelError("no TNUM or buffer for the next transmit task")
                self.ready.append(fresh)

    def receive(self, pid, frame, status=0):
        """A frame arrives on Rx port `pid`.  Returns False if BMI had to drop it."""
        port = self.ports[pid]
        if not port.enabled:
            return False
        need = (len(frame) + 255) // 256 + 1
        if len(self.free_buffers) < need:
            port.rx_dropped += 1
            return False
        task = self._new_task(port)
        if task is None:
            port.rx_dropped += 1
            return False
        buffers = [self._alloc_buffer(task.tnum) for _ in range(need - 1)]
        for index, address in enumerate(buffers):
            self.muram.write(address, frame[index * 256:index * 256 + 256])
            if index:
                self.links[buffers[index - 1]] = address
        page = port.regs.get(0x60, 0)
        self.muram.w32(task.ic_address + 0x08, len(frame))
        self.muram.w32(task.ic_address + 0x0C, status)
        # Parse result running sum (board, qe-isa-research FHW-223): Rx BMI's folded one's-complement
        # sum of the frame as 16-bit big-endian words from its first byte (FCS not forwarded, CSI 0).
        self.muram.w32(task.ic_address + 0x28, gross_sum(frame))
        task.windows[28] = Window(self.muram, buffers[0])
        task.windows[29] = Window(self.muram, page)
        task.frame_buffers = buffers
        task.entry_r27 = self.muram.r32(page)      # hardware loads r27 from the page's first word
        self.ready.append(task)
        return True

    # ---- controller hooks ---------------------------------------------------------
    def _tnum_alloc(self, cpu, c, i):
        if self.free_tnums:
            tnum = self.free_tnums.popleft()
            c.set(i.a if hasattr(i, "a") else 0, 0x80000000 | tnum)
        else:
            c.set(i.a, 0)

    def _regpair_init(self, cpu, c, i):
        # On r26/r28 the effect is contextual and not observable: ignored here.
        if i.a in (26, 28):
            return
        c.set(i.a, c.id)
        if i.a + 1 < 31:
            c.set(i.a + 1, 0x41)

    def _frame_base_set(self, cpu, c, i):
        value = c.reg(i.a)
        c.task.windows[28] = Window(self.muram, value & 0x00FFFF00, scalar=value)
        cpu.bind(c, c.task)

    def _boundary(self, cpu, c, i):
        c.task.boundary_nia = c.reg(i.b)
        return Cpu._task(cpu, c, i)

    # ---- scheduling -----------------------------------------------------------------
    def _port_busy(self, task):
        """Constrained PortID: one executing stage per port at a time."""
        return any(other is not None and other is not task and other.port_id == task.port_id and task.port_id
                   for other in self.running)

    def step(self):
        """Advance one controller by one stage (or one slice).  False when nothing can run."""
        order = list(range(4))
        if self.slice:
            self.rng.shuffle(order)
        for index in order:
            task = self.running[index]
            resume = task is not None
            if not resume:
                for candidate in self.ready:        # first admissible task; the others keep their order
                    if not self._port_busy(candidate):
                        task = candidate
                        break
                if task is not None:
                    self.ready.remove(task)
                if task is None:
                    continue
            budget = self.rng.randint(*self.slice) if self.slice else None
            controller = self.cpu.controllers[index]
            if not resume and getattr(task, "entry_r27", None) is not None and task.dispatches == 0:
                controller.r[27] = task.entry_r27
            before = controller.steps if resume else 0
            self.current = task
            event = self.cpu.run_stage(index, task, resume=resume, max_steps=budget)
            self.current = None
            self.time += controller.steps - before
            if event.kind in (STEPS, SM_RETRY):
                self.running[index] = task
                if event.kind == SM_RETRY and not self.slice:
                    # Nobody else can release it while this stage holds the controller model.
                    if not any(t is not None and t is not task for t in self.running) and not self.ready:
                        raise ModelError(f"{task!r} waits for a semaphore nobody will release (pc {event.pc:#x})")
                    self.ready.rotate(-1)
                return True
            self.running[index] = None
            self.stats["stages"] += 1
            self._boundary_event(task, event)
            return True
        return False

    def run(self, max_stages=400000, until=None):
        """Run until nothing is ready (or `until()` is true).  Returns stages run.

        A task that waits with nothing outstanding is a poller and never leaves
        the ready queue.  The model is quiescent when only such tasks are left,
        each of them last stopped at such a wait (not at one with DMA
        outstanding or another boundary), and a full round of them changed
        nothing in MURAM or the FPM state.
        """
        import zlib
        count = streak = 0
        mark = None
        while count < max_stages:
            if until is not None and until():
                break
            if not self.step():
                break
            count += 1
            if any(self.running):
                continue
            if not self.last_idle:
                streak, mark = 0, None
                continue
            if not all(getattr(task, "idle_wait", False) for task in self.ready):
                streak, mark = 0, None      # a task in the middle of a sequence is waiting for its turn
                continue
            streak += 1
            if streak >= len(self.ready) + 1:
                state = (zlib.crc32(self.muram.read(0, self.muram.size)), tuple(self.fcev), self.fpm[FPM_EXTC])
                if state == mark:
                    break
                streak, mark = 0, state
        else:
            raise ModelError(f"still busy after {max_stages} stages (a task polling without parking?)")
        return count

    def _boundary_event(self, task, event):
        kind = event.kind
        self.last_idle = kind == WAIT and not event.dma_pending
        task.idle_wait = self.last_idle
        if kind == WAIT:
            if event.dma_pending:
                self.cpu.complete_dma(task)
            else:
                self.stats["idle_waits"] += 1       # board: resumes at once, a busy poll
            self.ready.append(task)
        elif kind == PARK:
            self.parked[task.tnum] = task
            self._poll_requests()
        elif kind == HANDOFF:
            if event.retrigger:
                self.ready.append(task)
            else:
                self.parked[task.tnum] = task       # dormant: only an external request brings it back
        elif kind in (BOUNDARY, REDISPATCH):
            nia = (task.boundary_nia if kind == BOUNDARY else event.nia) & 0x7FFFFF
            self._nia(task, nia)
        elif kind == COMPLETE:
            self.parked[task.tnum] = task
        else:
            raise ModelError(f"unexpected stage end {kind}")

    def _nia(self, task, nia):
        if not nia & 0x7F0000:                      # controller action: same task continues there
            task.continuation = nia & 0x3FFF
            self.ready.append(task)
            return
        if task.open_dma:
            raise ModelError(f"{task!r} handed to BMI with {task.open_dma} DMA request(s) open")
        port = self.ports.get(task.port_id)
        if port is None:
            raise ModelError(f"{task!r} has no port to hand NIA {nia:#x} to")
        if nia in (NIA_RELEASE, NIA_DISCARD):
            if port.kind == "rx":
                self.stats["rx_released"] += 1
            self._retire(task, port)
        elif nia == NIA_TX and port.kind == "tx":
            self._transmit(task, port)
        elif nia == NIA_TX_STOP and port.kind == "tx":
            port.stopped = True
            self._retire(task, port)
        else:
            raise ModelError(f"{task!r}: NIA {nia:#x} is not modelled for a {port.kind} port")

    def _retire(self, task, port):
        for address in [a for a, owner in self.buffer_owner.items() if owner == task.tnum]:
            self._free_buffer(address)
        if task.sm_held:
            raise ModelError(f"{task!r} ended holding a semaphore")
        port.tasks.discard(task.tnum)
        del self.tasks[task.tnum]
        self.free_tnums.append(task.tnum)

    def _transmit(self, task, port):
        """ASSUMED (NXP-code evidence): frame = chain from the frame base, length at IC+8."""
        window = task.windows[28]
        if window is None:
            raise ModelError(f"{task!r} transmits without a frame base")
        length = self.muram.r32(task.ic_address + 0x08)
        if self.muram.r32(task.ic_address + 0x0C):
            raise ModelError("transmit command word at IC+0x0c is not zero (other values are untested on hardware)")
        data, address = b"", window.base
        while len(data) < length:
            if self.buffer_owner.get(address) != task.tnum:
                raise ModelError(f"{task!r}: transmit chain reaches buffer {address:#x} it does not own")
            data += self.muram.read(address, 0x100)
            following = self.links.get(address, address)
            if following == address and len(data) < length:
                raise ModelError(f"{task!r}: transmit chain ends after {len(data)} of {length} bytes")
            address = following
        if not 14 <= length <= 9600:
            raise ModelError(f"{task!r}: transmit length {length}")
        port.wire.append(data[:length])
        self.stats["tx_sent"] += 1
        self._retire(task, port)
        if port.held:                                # its FIFO is full: no next task until it drains
            port.waiting = True
        elif port.enabled and not port.stopped:      # BMI asks the controller for the next frame
            fresh = self._new_task(port)
            if fresh is None:
                raise ModelError("no TNUM or buffer for the next transmit task")
            self.ready.append(fresh)

    # ---- external requests ---------------------------------------------------------------
    def _line_tnum(self, line):
        word = self.fpm[FPM_EXT1] if line < 4 else self.fpm[FPM_EXT2]
        return (word >> (24 - 8 * (line & 3))) & 0xFF

    def _poll_requests(self):
        """A raised line re-admits its parked TNUM.  ASSUMED level-sensitive: a line
        raised while the task runs wakes it as soon as it parks (to be confirmed)."""
        for line in range(8):
            if self.fpm[FPM_EXTC] & (0x80000000 >> line):
                task = self.parked.pop(self._line_tnum(line), None)
                if task is not None:
                    self.stats["wakes"] += 1
                    self.ready.append(task)


class _Bmi:
    """BMI register space as unit 12 sees it."""

    def __init__(self, fman):
        self.fman = fman

    def read32(self, offset):
        fman = self.fman
        pid, reg = offset >> 12, offset & 0xFFF
        if pid == 0:
            return fman.common.get(reg, 0)
        port = fman.ports.get(pid)
        if port is None:
            raise ModelError(f"BMI port {pid:#x} does not exist on this board")
        task = self._current()
        if reg == 0x380:                              # ASSUMED: a free internal buffer, bit 31 = none
            address = fman._alloc_buffer(task.tnum)
            return 0x80000000 if address is None else address
        if reg == 0x38C:                              # ASSUMED: next buffer of the chain
            current = port.cursor
            following = fman.links.get(current, current)
            return ((current >> 8) << 16) | (following >> 8)
        if reg == 0x004:
            return (0x80000000 if port.tasks else 0) | (pid << 16)
        return port.regs.get(reg, 0)

    def write32(self, offset, value):
        fman = self.fman
        pid, reg = offset >> 12, offset & 0xFFF
        if pid == 0:
            fman.common[reg] = value
            return
        port = fman.ports.get(pid)
        if port is None:
            raise ModelError(f"BMI port {pid:#x} does not exist on this board")
        if reg == 0x388:                              # ASSUMED: link buffer A -> B (A == B ends the chain)
            first, second = (value >> 16) << 8, (value & 0xFFFF) << 8
            tnum = self._current().tnum
            for address in (first, second):
                if fman.buffer_owner.get(address) != tnum:
                    raise ModelError(f"link of buffer {address:#x} the task does not own")
            if first == second:
                fman.links.pop(first, None)
            else:
                fman.links[first] = second
        elif reg == 0x38C:
            if value & 0xFF or value >> 8 not in fman.buffer_owner:
                raise ModelError(f"next-buffer cursor {value:#x} is not an internal buffer (a margin top byte hangs the board)")
            port.cursor = value >> 8
        elif reg == 0:
            raise ModelError("the controller must not write a port's configuration register")
        else:
            port.regs[reg] = value

    def _current(self):
        if self.fman.current is None:
            raise ModelError("BMI access outside a stage")
        return self.fman.current


class _Fpm:
    def __init__(self, fman):
        self.fman = fman

    def read32(self, offset):
        fman = self.fman
        if FPM_FCEV <= offset < FPM_FCEV + 16:
            return fman.fcev[(offset - FPM_FCEV) >> 2]
        if FPM_CEE <= offset < FPM_CEE + 16:
            return fman.cee[(offset - FPM_CEE) >> 2]
        if offset == FPM_TSP:
            return fman.clock & 0xFFFFFFFF
        return fman.fpm.get(offset, 0)

    def write32(self, offset, value):
        fman = self.fman
        if FPM_FCEV <= offset < FPM_FCEV + 16:        # the controller raises events
            fman.fcev[(offset - FPM_FCEV) >> 2] |= value
        elif offset == FPM_EXTC:                      # raise (bits 31..24) or clear (bits 23..16) lines
            fman.fpm[FPM_EXTC] |= value & 0xFF000000
            fman.fpm[FPM_EXTC] &= ~((value & 0x00FF0000) << 8)
            fman._poll_requests()
        else:
            fman.fpm[offset] = value


# Rx and Tx PortIDs of the MACs a Mono Gateway DK populates (MAC2, MAC5, MAC6 at 1G;
# MAC9, MAC10 at 10G behind the SFP+ cages; Rx = 0x08 + MAC - 1 for MAC1..8, 0x10/0x11
# for MAC9/10, Tx = Rx + 0x20), and the six offline/host-command PortIDs 0x02..0x07 of
# the LS1046A FMan (Linux qoriq-fman3-0-6oh.dtsi).  Pass `ports=` for another set.
DEFAULT_PORTS = {0x09: "rx", 0x0C: "rx", 0x0D: "rx", 0x10: "rx", 0x11: "rx",
                 0x29: "tx", 0x2C: "tx", 0x2D: "tx", 0x30: "tx", 0x31: "tx",
                 **{pid: "oh" for pid in range(0x02, 0x08)}}
