"""Per-TNUM task state and the events a controller stage ends with."""
from dataclasses import dataclass, field

from .bus import Memory, Window

# BoundaryEvent.kind values.  The first seven are hardware boundaries; the
# last two are emulator stops after which the same stage can be resumed with
# run_stage(..., resume=True).
WAIT, PARK, HANDOFF, REDISPATCH, BOUNDARY, COMPLETE = "wait", "park", "handoff", "redispatch", "boundary", "complete"
SM_RETRY, STEPS = "sm_retry", "steps"


@dataclass
class DmaRequest:
    kind: str                 # "read": external -> MURAM, "write": MURAM -> external
    external: int             # 64-bit external byte address
    muram: int                # MURAM byte address
    length: int
    data: bytes = None        # write payload when sampled at issue time


class Task:
    """State that follows a TNUM across controllers (qe-isa-research: docs/fman-task-lifecycle.md).

    General registers are *not* here: they belong to the controller.
    """

    def __init__(self, tnum, port_id=0, entry=None, ic_size=0x100):
        self.tnum, self.port_id = tnum, port_id
        self.continuation = entry          # word PC the next stage starts at (DRD0 >> 6 after a boundary)
        self.installed_cont = None         # hidden continuation of task.set_cont, published by task.handoff
        self.drd = [0, 0, 0, 0]            # FMFP_DRD0..3
        self.open_dma = 0                  # FMFP_TSn[ODC]
        self.dma_queue = []                # DmaRequest, issue order
        self.dma_count = 16                # length of `configured` transfers
        self.end_nia = 0
        self.fqid = 0
        self.order = 0                     # task.order.set channel (bit 0 = ORD)
        self.retrigger = False             # hidden latch of task.retrigger
        self.ic = Memory(ic_size, 0, f"tnum {tnum} internal context")
        # r26: internal context; r28: frame window; r29: RX controller parameter page.
        self.windows = {26: Window(self.ic, 0), 28: None, 29: None}
        self.info_context = None           # (bits 31..24, bits 15..8) of task.info; default: port id in both
        self.sm_held = []                  # semaphore regions held by the live invocation
        self.sm_epoch = 0                  # identity of the live invocation
        self.last_event = None             # kind of the boundary that ended the previous stage
        self.zero_r30 = False              # task.boundary: r30 reads zero at the continuation
        self.controller = None             # index of the controller that ran the last stage
        self.dispatches = 0

    def info(self):
        """task.info: TNUM in bits 23..16 and 7..0, source context in the other bytes."""
        high, low = (self.port_id, self.port_id) if self.info_context is None else self.info_context
        tnum = self.tnum & 0xFF
        return ((high & 0xFF) << 24) | (tnum << 16) | ((low & 0xFF) << 8) | tnum

    def __repr__(self):
        return f"<Task tnum={self.tnum} port={self.port_id:#x} cont={self.continuation} odc={self.open_dma}>"


@dataclass
class BoundaryEvent:
    kind: str
    task: Task
    controller: int
    pc: int                     # word PC of the instruction that ended the stage
    steps: int                  # instructions executed in this stage
    continuation: int = None    # where the task resumes, when the boundary defines it
    nia: int = None             # End NIA handed to FPM (redispatch, boundary)
    retrigger: bool = False     # handoff: the same TNUM is to be readmitted at `continuation`
    dma_pending: bool = False   # wait: False means the task simply stays parked
    payload: int = None         # boundary: the 16-bit DRD2/DRD3 payload, None for `preserve`
    resumable: bool = field(default=False)
