"""Instruction-level core of the LS1046A FMan controller RISC.

Doc references ("explainer", "confirmed-isa", ...) are files under
qe-isa-research/docs; yaml names are files under qe-isa-research/isa.
"""
import sys

from . import qe
from .bus import BusError, EmuError, ExternalBus, LocalBus, Memory
from .task import (BOUNDARY, COMPLETE, HANDOFF, PARK, REDISPATCH, SM_RETRY, STEPS, WAIT, BoundaryEvent,
                   DmaRequest)

M32 = 0xFFFFFFFF
PCMASK = 0x3FFF                # 14-bit word PC ring (explainer, "Program and instruction representation")
IRAM_WORDS = 0x4000
MURAM_SIZE = 0x60000
SM_REGION_SHIFT = 5            # semaphore key is effective_address >> 5 (confirmed-isa, suboperation 4)


class Unimplemented(EmuError):
    """An instruction (or one of its forms) with no faithful model here."""

    def __init__(self, word, pc, mnemonic, why=""):
        self.word, self.pc, self.mnemonic, self.why = word, pc, mnemonic, why
        super().__init__(f"unimplemented {mnemonic} (word {word:#010x} at pc {pc:#06x})" + (f": {why}" if why else ""))


class Runaway(EmuError):
    pass


def sx32(v): return v - (1 << 32) if v & 0x80000000 else v
def sx11(w): return (w & 0x7FF) - 0x800 if w & 0x400 else w & 0x7FF
def rol32(v, n): return ((v << n) | (v >> (32 - n))) & M32 if n else v
def clz32(v): return 32 - v.bit_length()


def circular_mask(low, high):
    """Inclusive circular bit interval; low > high wraps through bits 31 and 0
    (confirmed-isa "C8-CF value transforms", qeisa/fman_circular_geometry.py)."""
    if low <= high:
        return (((1 << (high - low + 1)) - 1) << low) & M32
    return (((1 << (high + 1)) - 1) | (M32 ^ ((1 << low) - 1))) & M32


def _bitrev8(v):
    return int.from_bytes(bytes(int(f"{b:08b}"[::-1], 2) for b in v.to_bytes(4, "big")), "big")


# Result transforms of the D-family, selector bits 5..3 (addition-family.yaml "transforms").
TRANSFORMS = {
    "bitrev8": _bitrev8,
    "swap8": lambda v: ((v & 0x00FF00FF) << 8) | ((v >> 8) & 0x00FF00FF),
    "rol16": lambda v: rol32(v, 16),
    "lsr8": lambda v: v >> 8,
    "lsr1": lambda v: v >> 1,
    "low16": lambda v: v & 0xFFFF,
    "bswap": lambda v: int.from_bytes(v.to_bytes(4, "big"), "little"),
}
LOGIC = {
    "and": lambda l, r: l & r, "or": lambda l, r: l | r, "xor": lambda l, r: l ^ r,
    "andn": lambda l, r: l & ~r, "orn": lambda l, r: l | ~r, "xnor": lambda l, r: ~(l ^ r),
    "load": lambda l, r: r, "loadn": lambda l, r: ~r,
}


class Controller:
    """One of the four controller RISCs: registers, predicates and unit state.

    r[1..25], r[27], r[30] and r[0] are plain controller-local storage.  r[31]
    always reads zero.  r[26], r[28] and r[29] hold the scalar view of the
    current task's context windows (None when nothing is bound).
    """

    def __init__(self, index):
        self.id = index
        self.r = [0] * 32
        self.p = [False] * 32          # hidden predicate bank, p[n] = r31 selector n
        self.p[25] = True              # functionally always true (alu-predicate-effects.yaml)
        self.pc = self.npc = 0
        self.comp = None               # target pending while a companion executes
        self.task = None               # task of the current or most recent stage
        self.in_stage = False          # between stage entry and its hardware boundary
        self.steps = 0                 # instructions in the current stage
        self.csum = self.crc = 0       # Internet-checksum and CRC-16 state, controller-local
        self.csum_armed = False
        self.unit12 = 0                # unit12.result latch

    def reg(self, n):
        v = self.r[n]
        if v is None:
            raise EmuError(f"r{n} used at pc {self.pc:#06x} but the task has no context bound to it")
        return v

    def set(self, n, value):
        if n == 31:                    # constructing r31 reads back as zero (explainer, "Registers")
            return
        if n in (26, 28, 29):
            raise EmuError(f"write to contextual selector r{n} at pc {self.pc:#06x} is not modelled")
        self.r[n] = value & M32

    def predicates(self):
        return sum(1 << n for n in range(32) if self.p[n])

    # Predicate publication.  Bit layout and equations: explainer "Conditions",
    # encodings.yaml `condition_models`, alu-predicate-effects.yaml `selectors`.
    def _reduce(self, res):
        p = self.p
        z = res == 0
        p[1], p[2], p[3] = z, bool(res >> 31), z or bool(res >> 31)
        p[4] = (res >> 16) == 0 or (res & 0xFFFF) == 0
        p[5], p[6], p[7] = (res & 0xFFFF) == 0, bool(res & 0x8000), (res & 0xFF) == 0
        p[12] = not (res & 0xFF and res & 0xFF00 and res & 0xFF0000 and res & 0xFF000000)
        p[13], p[14], p[15] = bool(res & 1), False, res == 0x80000000

    def publish_logic(self, res, p0=False):
        p = self.p                     # p9 (sticky overflow) is retained
        p[0], p[8], p[10], p[11] = bool(p0), False, False, False
        self._reduce(res)

    def publish_arith(self, res, carry, overflow, le=False, lt=False):
        p = self.p
        p[0], p[8], p[9], p[10], p[11] = bool(carry), overflow, p[9] or overflow, le, lt
        self._reduce(res)


class TraceRecord:
    __slots__ = ("controller", "tnum", "pc", "word", "text", "changes", "predicates")

    def __str__(self):
        changed = " ".join(f"{k}={v:#x}" for k, v in self.changes.items())
        return f"c{self.controller} t{self.tnum} {self.pc:04x}: {self.word:08x}  {self.text:<44} {changed}"


class Cpu:
    """Four controllers sharing one I-RAM, MURAM, the semaphore table and the buses."""

    def __init__(self, muram=None, external=None, local=None, *, dma_immediate=False, scramble=False,
                 stage_limit=100000, trace=None, yield_on_sm_retry=True, dma_write_sample="issue"):
        self.iram = [0xFFFFFFFF] * IRAM_WORDS
        self._decoded = [None] * IRAM_WORDS
        self.muram = muram or Memory(MURAM_SIZE, 0, "MURAM")
        self.external = external or ExternalBus()
        self.local = local or LocalBus(self.muram)
        self.controllers = [Controller(n) for n in range(4)]
        self.semaphores = {}           # (space id, address >> 5) -> (task, invocation epoch)
        self.hooks = {}                # mnemonic -> fn(cpu, controller, ins) -> None | BoundaryEvent
        self.dma_immediate = dma_immediate
        self.dma_write_sample = dma_write_sample     # "issue" or "complete"
        self.scramble = scramble
        self.stage_limit = stage_limit
        self.trace = trace             # None, True (print to stderr) or a callable(TraceRecord)
        self.yield_on_sm_retry = yield_on_sm_retry
        self.boundary_hook = None      # callable(BoundaryEvent), called at every hardware boundary
        self.warnings = []
        self.total_steps = 0
        self._serial = 0

    # ---- program -----------------------------------------------------------
    def load(self, origin, words):
        for n, word in enumerate(words):
            self.write_iram(origin + n, word)

    def write_iram(self, pc, word):
        self.iram[pc & PCMASK] = word & M32
        self._decoded[pc & PCMASK] = None

    def assemble(self, source, org=None):
        origin, words = qe.assemble(source, org)
        self.load(origin, words)
        return origin, words

    def _fetch(self, pc):
        ins = qe.decode(self.iram[pc], pc)
        ins.fn = self._bind(ins)
        self._decoded[pc] = ins
        return ins

    def _bind(self, ins):
        if ins.m in self.hooks:
            hook = self.hooks[ins.m]
            return lambda cpu, c, i: hook(cpu, c, i)
        major = ins.word >> 26
        if 0x28 <= major <= 0x2F:                      # A0..BF: one register-bit transfer family
            return Cpu._ab
        if major in (0x21, 0x22, 0x23):                # 84..8F rotate-mask-combine
            return Cpu._rotcombine
        if major in (0x3E, 0x3F) and ins.m in ("bfins32", "bfinsn32", "bset32", "bclr32"):
            return Cpu._bfins
        fn = OPS.get(ins.m)
        if fn is None and ins.m.startswith("jmptbl"):
            fn = Cpu._jmptbl
        if fn is None:
            return Cpu._unimplemented
        ins.x = next((TRANSFORMS[k] for k in ins.decs if k in TRANSFORMS), None)
        return fn

    def _unimplemented(self, c, i, why=""):
        raise Unimplemented(i.word, i.pc, i.m, why)

    # ---- stages ------------------------------------------------------------
    def run_stage(self, controller, task, entry=None, resume=False, max_steps=None):
        """Run `task` on `controller` until a boundary and return the BoundaryEvent.

        A fresh stage starts at `entry` or else at `task.continuation`.  With
        resume=True execution continues where an SM_RETRY/STEPS stop left it.
        """
        c = controller if isinstance(controller, Controller) else self.controllers[controller]
        if resume:
            if c.task is not task or not c.in_stage:
                raise EmuError("resume: the controller is not in a stage of this task")
        else:
            self._enter(c, task, entry)
        return self._run(c, max_steps)

    def _enter(self, c, task, entry):
        pc = task.continuation if entry is None else entry
        if pc is None:
            raise EmuError(f"{task!r} has neither an entry nor a continuation")
        self._serial += 1
        c.task, c.in_stage, task.controller = task, True, c.id
        task.dispatches += 1
        c.pc, c.comp, c.steps = pc & PCMASK, None, 0
        for n in (*range(16), 30):       # explainer: readmission starts with selectors 0-13, 15 and 30 clear
            c.p[n] = False
        if self.scramble:                # registers belong to the controller, not the TNUM
            for n in range(1, 26):
                c.r[n] = 0xBAD00000 | ((self._serial & 0xFF) << 8) | n
        # r0 = controller ID at dispatch/handoff/boundary/redispatch; zero after wait.cont (explainer).
        c.r[0] = 0 if task.last_event == WAIT else c.id
        if task.zero_r30:
            c.r[30], task.zero_r30 = 0, False
        task.dma_count = 16              # observed after wait.cont; assumed for every stage start
        self.bind(c, task)

    def bind(self, c, task):
        """(Re)load the scalar views of r26/r28/r29 from the task's windows."""
        for n in (26, 28, 29):
            window = task.windows.get(n)
            c.r[n] = None if window is None else window.scalar & M32

    def _run(self, c, max_steps):
        decoded, trace, limit = self._decoded, self.trace, self.stage_limit
        budget = max_steps
        while True:
            pc = c.pc
            i = decoded[pc] or self._fetch(pc)
            if c.steps >= limit:
                raise Runaway(f"stage of {c.task!r} on controller {c.id} exceeded {limit} instructions at pc {pc:#06x}")
            if budget is not None:
                if budget == 0:
                    return self._event(STEPS, c, i, resumable=True)
                budget -= 1
            c.steps += 1
            self.total_steps += 1
            comp, c.comp = c.comp, None
            c.npc = (pc + 1) & PCMASK
            if trace:
                before, flags = c.r[:], c.predicates()
            event = i.fn(self, c, i)
            if trace:
                self._trace(c, i, before, flags)
            if comp is not None:         # that was the companion of the preceding transfer
                if event is not None or c.comp is not None or c.npc != (pc + 1) & PCMASK:
                    raise EmuError(f"control transfer or boundary in a companion position at pc {pc:#06x}")
                c.pc = comp
            else:
                c.pc = c.npc
            if event is not None:
                return event

    def _trace(self, c, i, before, flags):
        record = TraceRecord()
        record.controller, record.tnum, record.pc, record.word, record.text = c.id, c.task.tnum, i.pc, i.word, i.text
        record.changes = {f"r{n}": c.r[n] for n in range(32) if c.r[n] != before[n] and c.r[n] is not None}
        record.predicates = c.predicates()
        if record.predicates != flags:
            record.changes["p"] = record.predicates
        if self.trace is True:
            print(record, file=sys.stderr)
        else:
            self.trace(record)

    def _event(self, kind, c, i, resumable=False, **fields):
        task = c.task
        event = BoundaryEvent(kind, task, c.id, i.pc, c.steps, resumable=resumable, **fields)
        if not resumable:
            task.last_event = kind
            c.in_stage = False
            if self.boundary_hook:
                self.boundary_hook(event)
        return event

    # ---- register-bit / predicate tests -----------------------------------
    def _pred(self, c, n, i):
        if n < 17 or n == 25 or n == 30:   # p16: set by the outer model (controller-0 startup predicate)
            return c.p[n]
        raise Unimplemented(i.word, i.pc, i.m, f"predicate p{n} has no documented producer")

    def _selected(self, c, i):
        """True when a `select=pN` / `select=!pN` decorator lets the operation through."""
        s = i.decs.get("select")
        if s is None:
            return True
        return self._pred(c, int(s.lstrip("!p")), i) != s.startswith("!")

    def _bit_test(self, c, i):
        """A/B and 98-9F condition: bit (31 - code) of a register, or predicate `code` of r31.
        Both selectors 31 bypass the condition (explainer, "Control flow")."""
        reg, code = i.a, (i.word >> 21) & 31
        if reg == 31:
            return None if code == 31 else self._pred(c, code, i)
        return bool((c.reg(reg) >> (31 - code)) & 1)

    # ---- control flow ------------------------------------------------------
    def _go(self, c, target, direct):
        if direct:
            c.npc = target & PCMASK
        else:                           # companion form: execute PC+1 once, then transfer
            c.comp = target & PCMASK

    def _ab(self, c, i):
        """A0-BF.  bit 28: 0 = A (writes r30 = PC+2 whenever executed), 1 = B; bit 27: branch on
        clear; bit 26: 0 = companion, 1 = direct.  target = (PC + raw[13:0]) mod 0x4000."""
        w = i.word
        state = self._bit_test(c, i)    # captured before the link write and the companion
        taken = state is None or state != bool(w & 0x08000000)
        if not w & 0x10000000:
            c.set(30, i.pc + 2)         # not reduced to 14 bits: 0x3fff stores 0x4001
        if taken:
            self._go(c, i.pc + (w & PCMASK), w & 0x04000000)
            if self.yield_on_sm_retry and w & 0x04000000 and i.a == 31 and (w >> 21) & 31 == 30 and state:
                return self._event(SM_RETRY, c, i, resumable=True)

    def _xfer_2e5f(self, c, i):
        if i.word >> 16 != 0x2E5F:
            return self._unimplemented(c, i)
        self._go(c, i.pc + (i.word & PCMASK), False)

    def _jmptbl(self, c, i):
        """93 (companion) / 97 (direct): table + 2 * (index & (entries - 1)), entries = 2 << mode."""
        w = i.word
        if w >> 26 not in (0x24, 0x25) or (w >> 24) & 3 != 3:
            return self._unimplemented(c, i)
        index = c.reg(i.a) & ((2 << ((w >> 21) & 7)) - 1)
        self._go(c, i.pc + (w & PCMASK) + 2 * index, w & 0x04000000)

    def _indirect(self, c, i):
        """98-9F register-indirect forms: 9B/98 companion, 9F/9C direct; low bit 10 clear links
        (callind), low bit 0 selects branch-on-clear.  PC = r[target] & 0x3fff."""
        w, low = i.word, i.word & 0x7FF
        if low & ~0x501 or i.b == 31:
            return self._unimplemented(c, i)
        state = self._bit_test(c, i)
        if not low & 0x400:
            if state is not None:
                return self._unimplemented(c, i, "conditional linking indirect")
        taken = state is None or state != bool(low & 1)
        target = c.reg(i.b)
        if not low & 0x400:
            c.set(30, i.pc + 2)
        if taken:
            self._go(c, target, w & 0x04000000)

    # ---- ALU ---------------------------------------------------------------
    def _binop(self, c, kind, l, r, cin, i):
        """One scalar operation with optional result transform and publication.
        Arithmetic predicates (carry/borrow, overflow, signed relations) come from the
        untransformed operation, the result predicates from the transformed value
        (addition-family.yaml / subtraction-family.yaml `predicate_publication`)."""
        publish = "publish" in i.decs
        if kind == "add":
            raw = l + r + cin
            res = raw & M32
            if publish:
                s = sx32(l) + sx32(r) + cin
        elif kind == "sub":
            raw = l - r - cin
            res = raw & M32
            if publish:
                s = sx32(l) - sx32(r) - cin
        else:
            res = LOGIC[kind](l, r) & M32
        if i.x:
            res = i.x(res) & M32
        if publish:
            if kind == "add":
                c.publish_arith(res, raw > M32, not -0x80000000 <= s <= 0x7FFFFFFF)
            elif kind == "sub":
                c.publish_arith(res, raw < 0, not -0x80000000 <= s <= 0x7FFFFFFF, s <= 0, s < 0)
            else:
                c.publish_logic(res)
        return res

    def _alu3(self, c, i):
        """D0-DF three-register forms: left bits 20..16, right 15..11, destination 10..6."""
        d, kind = i.decs, i.m[:-2]
        l, r = c.reg(i.a), c.reg(i.b)
        cin = 0
        if kind == "add":
            cin = int(c.p[0]) if "carry" in d else int("increment" in d)
        elif kind == "sub":
            cin = int(c.p[0]) if "borrow" in d else int("decrement" in d)
        if not self._selected(c, i):   # pass the left input through (assembly-language.md, select forms)
            if kind in ("add", "sub"):
                r = cin = 0            # D0: left + (condition ? right : 0)
            else:
                kind, r = "load", l
        res = self._binop(c, kind, l, r, cin, i)
        if "discard" not in d:
            c.set(i.c, res)

    def _not32(self, c, i):
        top = i.word >> 24
        if top == 0xD7:                 # D78 conditional: ~right when selected, else left
            taken = self._selected(c, i)
            res, dest = self._binop(c, "loadn" if taken else "load", 0, c.reg(i.b if taken else i.a), 0, i), i.c
        elif top == 0xDD:               # DDDF: destination 10..6 = ~source 15..11
            res, dest = self._binop(c, "loadn", 0, c.reg(i.b), 0, i), i.c
        elif top == 0xF6:               # F64: destination 15..11 = ~source 20..16
            res, dest = self._binop(c, "loadn", 0, c.reg(i.a), 0, i), i.b
        elif top == 0xEE:               # EE4: in place
            res, dest = self._binop(c, "loadn", 0, c.reg(i.a), 0, i), i.a
        else:
            return self._unimplemented(c, i)
        if "discard" not in i.decs:
            c.set(dest, res)

    def _select32(self, c, i):
        res = self._binop(c, "load", 0, c.reg(i.b) if self._selected(c, i) else c.reg(i.a), 0, i)
        if "discard" not in i.decs:
            c.set(i.c, res)

    def _imm16(self, c, i):
        """E0-EF immediate forms (alu-matrix-families.yaml, docs/hardware-immediate-survey.md)."""
        kind, high = IMM16[i.m]
        d = i.decs
        l = 0 if kind in ("load", "loadn") else c.reg(i.a)
        cin = int(c.p[0]) if "carry" in d or "borrow" in d else 0
        res = self._binop(c, kind, l, i.imm << 16 if high else i.imm, cin, i)
        if "discard" not in d:
            c.set(i.a, res)

    def _lane8(self, c, i):
        """F0-F5 byte-lane immediates: constant = imm8 << (8 * (3 - lane)); lane 0 is bits 31..24."""
        d, w = i.decs, i.word
        cin = int(c.p[0]) if "carry" in d or "borrow" in d else 0
        res = self._binop(c, i.m[:-5], c.reg(i.a), (w & 0xFF) << (8 * (3 - ((w >> 8) & 3))), cin, i)
        if "discard" not in d:
            c.set(i.b, res)

    def _shift(self, c, i):
        """DB/DF shifts and rotates (shift-rotate-families.yaml).  Bit 0: immediate count in
        bits 15..11, else the low five bits of that register.  Shifts publish the last
        shifted-out bit in p0 (clear for count 0); plain rotates publish p0 clear."""
        d, m, v = i.decs, i.m, c.reg(i.a)
        n = i.b if i.word & 1 else c.reg(i.b) & 31
        if "reverse" in d:
            n = 31 - n
        if "increment_count" in d:
            n = (n + 1) & 31
        out = False
        if m == "lsl32":
            res, out = (v << n) & M32, bool(n and (v >> (32 - n)) & 1)
        elif m == "lsr32":
            res, out = v >> n, bool(n and (v >> (n - 1)) & 1)
        elif m == "asr32":
            res, out = (sx32(v) >> n) & M32, bool(n and (v >> (n - 1)) & 1)
        elif m == "rol32":
            res = rol32(v, n)
        elif "carry" in d:              # ror33: rotate the 33-bit {p0, source} pair right
            x = (int(c.p[0]) << 32) | v
            x = ((x >> n) | (x << (33 - n))) & 0x1FFFFFFFF
            res, out = x & M32, bool(x >> 32)
        else:
            res = rol32(v, (32 - n) & 31)
        if "publish" in d:
            c.publish_logic(res, out)
        if "discard" not in d:
            c.set(i.c, res)

    def _bit(self, c, i):
        """bset32/bclr32 (bit-update-families.yaml; D68 conditional form in scalar-matrix-families.yaml)."""
        d, w, v = i.decs, i.word, c.reg(i.a)
        if (w >> 23) == 0x1AD:          # D68: MSB-first immediate index under a predicate
            bit = 0x80000000 >> i.b
        elif w & 0x3F == 0x3E:          # D28/D30: numerically indexed immediate
            bit = 1 << i.b
        else:
            n = c.reg(i.b) & 31
            bit = 0x80000000 >> n if "reverse" in d else 1 << n
        res = v if not self._selected(c, i) else v | bit if i.m == "bset32" else v & ~bit
        if "publish" in d:
            c.publish_logic(res)
        if "discard" not in d:
            c.set(i.c, res)

    def _clz(self, c, i):
        if "publish" in i.decs:
            return self._unimplemented(c, i, "count-form predicate publication")
        v = c.reg(i.b)
        m = i.m
        if m == "clz32":                # unscaled, field 20..16 fixed to 31 (docs/hardware-alu-special.md)
            res = clz32(v)
        elif m == "clzclear32":         # 4 * clz, and clear the source's most significant set bit
            res = 4 * clz32(v)
            c.set(i.b, v & ~(1 << (v.bit_length() - 1)) if v else 0)
        else:                           # count-matrix-families.yaml
            res = 4 * clz32({"clz4": v, "clznot4": ~v & M32, "clz4.low16": v & 0xFFFF, "clz4.low8": v & 0xFF}[m])
        if "discard" not in i.decs:
            c.set(i.c, res)

    def _merge8(self, c, i):
        c.set(i.c, (c.reg(i.b) & 0xFFFFFF00) | (c.reg(i.a) & 0xFF))

    def _merge32(self, c, i):
        """C8-CB: interval from bits 20..16's register, complement from bits 15..11's register."""
        w = i.word
        mask = circular_mask((w >> 1) & 31, 31 - ((w >> 21) & 31))
        c.set(i.c, (c.reg(i.a) & mask) | (c.reg(i.b) & ~mask))

    def _rotmask32(self, c, i):
        """CC-CF: destination = rol32(source, count) & circular_mask(low_bound, 31 - high_trim);
        bit 0 = 1 selects the immediate count."""
        w = i.word
        count = i.b if w & 1 else c.reg(i.b) & 31
        c.set(i.c, rol32(c.reg(i.a), count) & circular_mask((w >> 1) & 31, 31 - ((w >> 21) & 31)))

    def _bfins(self, c, i):
        """F8-FB bfins32 and FC-FF bfinsn32 (qeisa/fman_bfins.py, docs/hardware-unused-opcode-families.md)."""
        w = i.word
        mask = circular_mask((w >> 1) & 31, 31 - ((w >> 21) & 31))
        source = i.a if w & 1 else c.reg(i.a)
        old = c.reg(i.b)
        if w >> 26 == 0x3F:
            old = ~old
        c.set(i.b, (old & ~mask) | (rol32(source, i.c) & mask))

    def _rotcombine(self, c, i):
        """84-8F (docs/hardware-unused-opcode-families.md): value = (bit0 ? rotated : ~rotated) & mask."""
        w = i.word
        value = rol32(c.reg(i.a), i.c)
        value = (value if w & 1 else ~value) & circular_mask((w >> 1) & 31, 31 - ((w >> 21) & 31))
        old = c.reg(i.b)
        c.set(i.b, {0x21: old ^ value, 0x22: old & ~value, 0x23: old | value}[w >> 26])

    def _lea(self, c, i):
        m, pc, w = i.m, i.pc, i.word
        offset = w & 0xFFFF
        signed = offset - 0x10000 if offset & 0x8000 else offset
        if m == "li24":
            c.set(30, w & 0xFFFFFF)
        elif m == "lea16.lo":
            c.set(i.a, (c.reg(i.a) & 0xFFFF0000) | ((pc + offset) & 0xFFFF))
        elif m == "lea24":
            c.set(i.a, (pc + signed) & 0xFFFFFF)
        elif m == "lea24.c5":
            total = (pc + signed) & 0xFFFFFF
            c.set(i.a, total & 0xFF7FFF if signed >= 0 else total)
        elif m == "lea14.c1":           # modulo-0x4000 code coordinate
            if i.a == 31:
                return self._unimplemented(c, i, "destination r31")
            c.set(i.a, (pc + (w & PCMASK)) & PCMASK)
        else:                           # C2: signed 24-bit reference into r30; only the coordinate is confirmed
            c.set(30, (pc + (offset if m == "pcrel24.c2pos" else offset - 0x10000)) & 0xFFFFFF)

    # ---- direct memory -----------------------------------------------------
    def _space(self, c, reg):
        if reg in (26, 28, 29):
            window = c.task.windows.get(reg)
            if window is None:
                raise EmuError(f"memory access through r{reg} at pc {c.pc:#06x} but the task has no window bound to it")
            return window.space, window.base
        return self.muram, c.reg(reg)

    def _mem(self, c, i):
        """00-17 direct memory.  effective = align_down(r[base] + sign_extend(disp11), width)
        (confirmed-isa "Direct-memory reads"); big-endian; narrow loads zero-extend."""
        width, store, flavor = MEM[i.m]
        if width is None:               # ld.sm / st.sm: shape code in bits 26..25
            width = 1 << ((i.word >> 25) & 3)
        align = 4 if flavor == "24" else 8 if flavor == "h4" else width
        space, base = self._space(c, i.b)
        ea = ((base + sx11(i.word)) & M32) & ~(align - 1)
        if store:
            if width == 8:
                data = c.reg(i.a).to_bytes(4, "big") + c.reg((i.a + 1) & 31).to_bytes(4, "big")
            elif flavor in ("24", "sm24"):     # keep the leading byte of the word
                data = space.read(ea, 1) + (c.reg(i.a) & 0xFFFFFF).to_bytes(3, "big")
            else:
                data = (c.reg(i.a) & ((1 << (8 * width)) - 1)).to_bytes(width, "big")
            space.write(ea, data)
            if flavor in ("sm", "sm24") and c.task.sm_held:
                # st.sm releases the live context's current acquisition, whatever its address
                del self.semaphores[c.task.sm_held.pop()]
            return
        if flavor == "h4":              # four zero-extended halfwords into four registers
            data = space.read(ea, 8)
            for n in range(4):
                c.set((i.a + n) & 31, int.from_bytes(data[2 * n:2 * n + 2], "big"))
            return
        data = space.read(ea, width)
        if width == 8:
            c.set(i.a, int.from_bytes(data[:4], "big"))
            c.set((i.a + 1) & 31, int.from_bytes(data[4:], "big"))
            value = None
        else:
            value = int.from_bytes(data, "big")
            if flavor == "24":
                value &= 0xFFFFFF
            elif flavor == "s8":
                value = (value - 0x100 if value & 0x80 else value) & M32
            c.set(i.a, value)
        if flavor in ("pub", "s8", "sm5"):
            c.publish_logic(value)
        if flavor in ("sm", "sm5"):     # acquire; selector 30 = acquisition pending
            key, me = (id(space), ea >> SM_REGION_SHIFT), (c.task, c.task.sm_epoch)
            owner = self.semaphores.setdefault(key, me)
            c.p[30] = owner != me
            if owner == me and key not in c.task.sm_held:
                c.task.sm_held.append(key)

    # ---- DMA ---------------------------------------------------------------
    def _dma(self, c, i):
        """5x: bits 20..16 = first register of the 64-bit external address pair, bits 15..11 =
        MURAM byte-address register.  Exact unaligned byte addresses on both sides."""
        size = i.ops[2]
        length = c.task.dma_count if size == "configured" else int(size, 0)
        external = (c.reg(i.a) << 32) | c.reg((i.a + 1) & 31)
        muram = c.reg(i.b)
        if len(i.ops) == 4:             # dma.read pair, base, 8, displacement
            muram = (muram + i.f["displacement"]) & M32
        request = DmaRequest("read" if i.m == "dma.read" else "write", external, muram, length)
        if request.kind == "write" and self.dma_write_sample == "issue":
            request.data = self.muram.read(muram, length)
        if self.dma_immediate:
            self._apply_dma(request)
        else:
            c.task.dma_queue.append(request)
            c.task.open_dma += 1

    def _apply_dma(self, request):
        if request.kind == "read":
            data = self.external.dma_read(request.external, request.length)
            if len(data) != request.length:
                raise BusError("dma_read returned the wrong number of bytes")
            self.muram.write(request.muram, data)
        else:
            data = request.data if request.data is not None else self.muram.read(request.muram, request.length)
            self.external.dma_write(request.external, data)

    def complete_dma(self, task, count=None):
        """Apply the oldest `count` (default: all) queued DMA requests of `task`."""
        done = 0
        while task.dma_queue and (count is None or done < count):
            self._apply_dma(task.dma_queue.pop(0))
            task.open_dma -= 1
            done += 1
        return done

    def _dma_count(self, c, i):
        """Immediate 6bNNfd00: NN + 1 bytes; register form: (r[count] & 0xff) + 1."""
        c.task.dma_count = i.f["byte_count"] if "byte_count" in i.f else (c.reg(i.b) & 0xFF) + 1

    # ---- 70-7F units -------------------------------------------------------
    def _unit(self, c, i):
        m, task = i.m, c.task
        if m == "controller.id":
            c.set(i.a, c.id)
        elif m == "regpair.init":       # (controller_id, 0x41) into an adjacent pair
            c.set(i.a, c.id)
            c.set((i.a + 1) & 31, 0x41)
        elif m == "task.info":
            c.set(i.a, task.info())
        elif m == "frame.port_id":
            c.set(i.a, task.port_id)
        elif m == "frame.base":         # equals scalar r28; zero without a frame
            c.set(i.a, 0 if task.windows.get(28) is None else task.windows[28].scalar)
        elif m == "task.pram_base.read":
            if task.windows.get(29) is None:
                return self._unimplemented(c, i, "no parameter page bound to r29")
            c.set(i.a, task.windows[29].scalar)
        elif m == "unit0.f8.result":    # zero in every tested context
            c.set(i.a, 0)
        elif m == "unit12.load32":      # bits 20..16 select the address; word-aligned
            c.unit12 = self.local.read32(c.reg(i.a) & ~3) & M32
        elif m == "unit12.store32":     # data bits 20..16, address bits 15..11; publishes the stored value
            c.unit12 = c.reg(i.a)
            self.local.write32(c.reg(i.b) & ~3, c.unit12)
        elif m == "unit12.result":
            c.set(i.a, c.unit12)
        elif m == "task.set_end_nia":   # low 24 bits of the bits-15..11 register
            task.end_nia = c.reg(i.b) & 0xFFFFFF
        elif m == "task.set_fqid":
            task.fqid = c.reg(i.a) & 0xFFFFFF
        elif m == "task.order.set":     # retains the source's low two bits; bit 0 is ORD
            task.order = c.reg(i.b) & 3
        elif m == "csum.init":
            c.csum = 0
        elif m == "crc16.init":
            c.crc = 0
        elif m == "csum.setup":
            if "descending" in i.decs:
                return self._unimplemented(c, i, "descending traversal")
            c.csum_armed = True
        elif m == "csum.result":        # folded, not complemented
            c.set(i.a, c.csum)
        elif m == "crc16.result":
            c.set(i.a, c.crc)

    def _csum_accum(self, c, i):
        """Mode 0 (docs/hardware-csum-characterization.md): Internet sum over (range | 1) + 1 bytes
        at r[address] + unsigned11(offset); CRC-16 (reflected, 0x8408, no final XOR) over range + 1."""
        if not c.csum_armed:            # documented: a contribution without csum.setup adds nothing
            self.warnings.append(f"csum.accum without csum.setup at pc {i.pc:#06x}")
            return
        c.csum_armed = False
        count = c.reg(i.a)
        space, base = self._space(c, i.b)
        data = space.read((base + (i.word & 0x7FF)) & M32, (count | 1) + 1)
        total = c.csum + sum(int.from_bytes(data[n:n + 2], "big") for n in range(0, len(data), 2))
        while total >> 16:
            total = (total & 0xFFFF) + (total >> 16)
        c.csum = total
        crc = c.crc
        for byte in data[:count + 1]:
            crc ^= byte
            for _ in range(8):
                crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
        c.crc = crc

    # ---- task control ------------------------------------------------------
    def _end_invocation(self, task):
        """Completion ends the live acquisition context; semaphores it held stay owned."""
        task.sm_held, task.sm_epoch = [], task.sm_epoch + 1

    def _task(self, c, i):
        m, task = i.m, c.task
        if m == "task.set_cont":        # 261f: install (PC + disp) mod 0x4000, fall through
            task.installed_cont = i.target & PCMASK
        elif m == "task.set_cont.reg":  # 241f: install code_coordinate(rN)
            task.installed_cont = c.reg(i.b) & PCMASK
        elif m == "task.retrigger":     # 203ff800: arm readmission at the next handoff
            task.retrigger = True
        elif m == "task.retrigger.default_cont":   # 263f003d: also replace the continuation with word one
            task.retrigger, task.installed_cont = True, 1
        elif m == "wait.cont":          # 2e3f: resume at target once the open-DMA count is zero
            task.continuation = i.target & PCMASK
            return self._event(WAIT, c, i, continuation=task.continuation, dma_pending=task.open_dma > 0)
        elif m == "park.cont":          # 2e1f: DRD0 = target << 6, park
            task.continuation = i.target & PCMASK
            task.drd[0] = task.continuation << 6
            return self._event(PARK, c, i, continuation=task.continuation)
        elif m == "task.handoff":       # 281ff800: publish the installed continuation in DRD0[31:6]
            if task.installed_cont is not None:
                task.continuation = task.installed_cont
                task.drd[0] = task.continuation << 6
            retrigger, task.retrigger = task.retrigger, False
            if not retrigger:
                self._end_invocation(task)
            return self._event(HANDOFF, c, i, continuation=task.continuation, retrigger=retrigger)
        elif m == "task.redispatch":    # 283ff800: live task goes to FPM through its End NIA
            return self._event(REDISPATCH, c, i, nia=task.end_nia)
        elif m == "task.boundary":      # 2c2: payload bits 20..16 (31 = preserve), continuation bits 15..11
            payload = None
            if i.a != 31:
                payload = c.reg(i.a) & 0xFFFF
                task.drd[2] = (task.drd[2] & ~3) | (payload >> 14)
                task.drd[3] = (task.drd[3] & 0x3FFFF) | ((payload & 0x3FFF) << 18)
            task.drd[0] = (c.reg(i.b) << 6) & M32
            task.continuation = (task.drd[0] >> 6) & PCMASK
            task.zero_r30 = True        # r30 is zero at the continuation on every controller
            c.r[30] = 0
            return self._event(BOUNDARY, c, i, continuation=task.continuation, nia=task.end_nia, payload=payload)
        elif m == "task.complete_cont":  # 2c1f: DRD0 = code_coordinate(rN) << 6, complete the task
            task.continuation = c.reg(i.b) & PCMASK
            task.drd[0] = task.continuation << 6
            self._end_invocation(task)
            return self._event(COMPLETE, c, i, continuation=task.continuation)

    def _nop(self, c, i):
        pass


IMM16 = {  # mnemonic: (operation, immediate in the high half)
    "addhi16": ("add", True), "andhi16": ("and", True), "orhi16": ("or", True), "subhi16": ("sub", True),
    "andnhi16": ("andn", True), "ornhi16": ("orn", True), "andnlo16": ("andn", False), "ornlo16": ("orn", False),
    "addi16": ("add", False), "andi16": ("and", False), "ori16": ("or", False), "xori16": ("xor", False),
    "subi16": ("sub", False), "li16": ("load", False), "lihi16": ("load", True), "linv16": ("loadn", False),
    "linvhi16": ("loadn", True), "testi16": ("load", False),
}
MEM = {  # mnemonic: (bytes, store, flavour)
    "memb.read.s0": (1, False, None), "memh.read.s0": (2, False, None), "memw.read.s0": (4, False, None),
    "memd.read.s0": (8, False, None), "memb.write.s0": (1, True, None), "memh.write.s0": (2, True, None),
    "memw.write.s0": (4, True, None), "memd.write.s0": (8, True, None), "memd.write.s2": (8, True, None),
    "membu.readz": (1, False, "pub"), "memhu.readz": (2, False, "pub"), "memw.readz": (4, False, "pub"),
    "membs.readz": (1, False, "s8"), "mem24.read": (4, False, "24"), "mem24.write": (4, True, "24"),
    "memh4.read": (8, False, "h4"), "ld.sm": (None, False, "sm"), "st.sm": (None, True, "sm"),
    "ld.sm.s5": (4, False, "sm5"), "st.sm.low24": (4, True, "sm24"),
}
OPS = {m: Cpu._mem for m in MEM}
OPS.update({m: Cpu._imm16 for m in IMM16})
OPS.update({m: Cpu._alu3 for m in ("add32", "sub32", "and32", "or32", "xor32", "andn32", "orn32", "xnor32")})
OPS.update({m: Cpu._lane8 for m in ("addlane8", "sublane8", "andlane8", "orlane8", "andnlane8")})
OPS.update({m: Cpu._shift for m in ("lsl32", "lsr32", "asr32", "rol32", "ror32")})
OPS.update({m: Cpu._clz for m in ("clz32", "clz4", "clznot4", "clz4.low16", "clz4.low8", "clzclear32")})
OPS.update({m: Cpu._lea for m in ("li24", "lea16.lo", "lea24", "lea24.c5", "lea14.c1", "pcrel24.c2pos", "coord24.c2neg")})
OPS.update({m: Cpu._indirect for m in ("ret", "indirect", "callind", "brbitset.ind", "brbitclr.ind")})
OPS.update({m: Cpu._unit for m in (
    "controller.id", "regpair.init", "task.info", "frame.port_id", "frame.base", "task.pram_base.read",
    "unit0.f8.result", "unit12.load32", "unit12.store32", "unit12.result", "task.set_end_nia", "task.set_fqid",
    "task.order.set", "csum.init", "crc16.init", "csum.setup", "csum.result", "crc16.result")})
OPS.update({m: Cpu._task for m in (
    "task.set_cont", "task.set_cont.reg", "task.retrigger", "task.retrigger.default_cont", "wait.cont",
    "park.cont", "task.handoff", "task.redispatch", "task.boundary", "task.complete_cont")})
OPS.update({
    "not32": Cpu._not32, "select32": Cpu._select32, "bset32": Cpu._bit, "bclr32": Cpu._bit,
    "merge8": Cpu._merge8, "merge32": Cpu._merge32, "rotmask32": Cpu._rotmask32, "bfins32": Cpu._bfins,
    "bfinsn32": Cpu._bfins, "xfer14": Cpu._xfer_2e5f, "dma.read": Cpu._dma, "dma.write": Cpu._dma,
    "dma.count": Cpu._dma_count, "csum.accum": Cpu._csum_accum, "fill.ff": Cpu._nop,
})
IMPLEMENTED = sorted(OPS) + ["jmptbl", "(all A0-BF transfers: xfer14, call, cbrz14, cbrnz14, cbrnn14, brbitset14, "
                             "brbitclr14, callbitset14, callbitclr14, retry.sm, brctrl0, brnotctrl0)",
                             "rotxor32/rotxorn32/rotbic32/rotand32/rotor32/rotorn32/xorrol3"]
