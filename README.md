> [!WARNING]
> **Note:** This repository consists of LLM-generated code. We make no claims about its
> correctness, completeness or fitness for any purpose. Treat all behaviour of the emulator,
> and every hardware fact it encodes, as unverified unless you have checked it yourself.

# fman-emu: LS1046A FMan controller emulator

An instruction-level emulator of the RISC controllers inside the Frame Manager (FMan) of the
NXP LS1046A, written to test FMan microcode without hardware. Pure Python 3; the only
third-party dependency is PyYAML. Instruction decoding and (for tests) assembly come from a
bundled subset of the `qeisa` package (see [Bundled code](#bundled-code)).

The core executes raw 32-bit words from an I-RAM image; it never sees assembler source.
Around it:

| Module | What it is |
| --- | --- |
| `fman_emu.core`, `bus`, `task` | The four controllers, MURAM, the external (DMA) and FMan-local (unit 12) buses, task state and stage boundaries |
| `fman_emu.qe` | Decoder, disassembler and assembler front end over the bundled `qeisa` |
| `fman_emu.hw` | Behavioural model of the FMan around the controllers: BMI Rx/Tx/offline ports creating tasks, internal buffers, FPM external requests and controller events, a scheduler for the four controllers |
| `fman_emu.board` | Behavioural models of the board devices an FMan program can reach on a Mono Gateway DK (LS1046A): I2C controllers and muxes, SFP module EEPROMs, retimer, GPIO, SerDes1 cage lanes, mEMACs, PCS and PHY MDIO |

This repository contains no microcode: no vendor images, no programs, no binaries. The tests
use only short hand-written instruction snippets.

## Running the tests

    python3 -m pip install PyYAML        # or your distribution's package (python3-pyyaml)
    python3 -m tests                     # from the repository root: all suites

Or one suite at a time, from anywhere:

    python3 tests/selftest.py [-v] [-k name] [--quick]    # core: 35 tests
    python3 tests/test_board_model.py                     # board devices: 18 tests

`pip install .` installs the `fman_emu` package with its data files.

## API

```python
from fman_emu import Cpu, Task, Window, WAIT, HANDOFF

cpu = Cpu(scramble=True)                    # Cpu(muram=None, external=None, local=None, *, dma_immediate=False,
                                            #     scramble=False, stage_limit=100000, trace=None,
                                            #     yield_on_sm_retry=True, dma_write_sample="issue")
cpu.load(0x3600, words)                     # or cpu.assemble(source, org=0x3600), cpu.write_iram(pc, word)
ram = cpu.external.add_ram(0x8000_0000, 0x1000)
task = Task(tnum=4, port_id=0x10)
task.windows[28] = Window(cpu.muram, 0x4500)        # frame window; 29 = parameter page; 26 = task.ic (preset)
event = cpu.run_stage(0, task, entry=0x3600)        # -> BoundaryEvent
while event.kind == WAIT:
    cpu.complete_dma(task)                          # apply queued DMA, open count -> 0
    event = cpu.run_stage(1, task)                  # continue at task.continuation, any controller
```

* `Cpu.controllers[0..3]`: `Controller` with `r` (32 registers), `p` (predicate bank), `pc`, `steps`,
  `csum`, `crc`, `unit12`; `reg(n)`, `set(n, v)`, `predicates()` (bit mask).
* `Cpu.muram` (`Memory`, 0x60000 bytes), `Cpu.external` (`ExternalBus`), `Cpu.local` (`LocalBus`, unit 12),
  `Cpu.iram`, `Cpu.semaphores`, `Cpu.warnings`, `Cpu.total_steps`.
* `run_stage(controller, task, entry=None, resume=False, max_steps=None)`. A fresh stage starts at `entry`
  or `task.continuation`. `max_steps` ends the stage with a resumable `STEPS` event; `resume=True` continues it.
* `complete_dma(task, count=None)`: apply the oldest `count` (default all) queued requests; returns how many.
* `Task`: `tnum`, `port_id`, `continuation`, `drd[0..3]`, `open_dma`, `dma_queue` (`DmaRequest`), `dma_count`,
  `end_nia`, `fqid`, `order`, `retrigger`, `ic` (internal context memory), `windows`, `info_context`, `dispatches`.
* `BoundaryEvent`: `kind` (`WAIT`, `PARK`, `HANDOFF`, `REDISPATCH`, `BOUNDARY`, `COMPLETE`, and the emulator
  stops `SM_RETRY`, `STEPS`), `task`, `controller`, `pc`, `steps`, `continuation`, `nia`, `retrigger`,
  `dma_pending`, `payload`, `resumable`.
* `Cpu.hooks[mnemonic] = fn(cpu, controller, ins)` replaces or supplies an instruction (for example
  `tnum.alloc`); `Cpu.boundary_hook = fn(event)` sees every hardware boundary. Install hooks before the
  affected words first execute: decoded instructions are cached per I-RAM word until it is rewritten.
* `trace=True` prints one line per instruction to stderr; `trace=callable` receives `TraceRecord`
  (`controller`, `tnum`, `pc`, `word`, `text`, `changes` (registers, `p`), `predicates`).
* Errors: `Unimplemented(word, pc, mnemonic)`, `Runaway` (more than `stage_limit` instructions in a stage),
  `BusError`, all subclasses of `EmuError`.
* `fman_emu.qe`: `decode(word, pc)`, `disassemble(word, pc)`, `assemble(source, org=None)`, `assemble_cli(path, out)`.
  The command-line assembler is `python3 -m fman_emu._vendor.qe_asm SOURCE -o OUTPUT`.

Bus interfaces. An external bus is anything with `dma_read(address, n) -> bytes` and
`dma_write(address, data)`. `ExternalBus` maps devices (`read(offset, n)`, `write(offset, data)`) at 64-bit
addresses. The board finding that a DMA read touches more of a device than it delivers is a bus property:
`ExternalBus(read_span=lambda address, n: (address2, n2))` names the span really read; the core still gets
exactly `n` bytes. `LocalBus` gives unit 12 MURAM plus registers (`register(address, value)` or `map`).

Hardware model. `fman_emu.hw.Fman(seed=None, slice=None, ports=None, ...)` wraps a `Cpu`: load an image
with `load_image(words)`, run the reset entry with `reset()`, configure and enable ports through
`host_write32`, deliver frames with `receive(port_id, frame)`, and run with `run()`; transmitted frames
appear in `fman.ports[port_id].wire`. `slice=(lo, hi)` with a seed interleaves the controllers at
instruction granularity to expose races. Behaviour known only from how existing microcode uses the
hardware, or assumed, is marked ASSUMED in the source.

Board model. `fman_emu.board.Board().attach(cpu.external)` maps the CCSR devices and the FMan MAC/MDIO
blocks for controller DMA; `attach_fman(cpu.local)` also maps the FMan blocks for unit 12. Test controls:
`insert_module(cage, a0)`, `remove_module`, `set_los`, `set_phy_link`, `set_pcs_link`, and queries such as
`mac_enabled("MAC9")`. MACs are named by hardware identity (MAC name or Rx PortID).

## What is modelled faithfully

Sources: the qe-isa-research documentation (`docs/fman-architecture-explainer.md`,
`docs/ls1046a-confirmed-isa.md`, `docs/fman-task-lifecycle.md`, `docs/hardware-csum-characterization.md`)
and its `isa/*.yaml` instruction families, of which the bundled YAML files are a subset. Doc references in
the source code ("explainer", "confirmed-isa", ...) name those files.

* Four controllers, each with its own r0..r31 and predicate bank. r31 reads zero and drops writes. Registers
  stay with the controller, not the task. `scramble=True` overwrites r1..r25 with `0xBAD0ssnn` at every stage
  start so that reliance on a register across a boundary fails visibly even on one controller.
* Predicates p0..p15 as in the explainer (carry/borrow/last shifted-out, zero, sign, halfword/byte-zero
  classes, overflow, sticky overflow, signed relations), p25 always true, p30 pending semaphore.
  Logical publication clears p0/p8/p10/p11 and keeps sticky p9. Arithmetic flags come from the untransformed
  operation, result flags from the transformed value. Branch condition: register bit `31 - code`, or
  predicate `code` when the register is r31; both selectors 31 are unconditional.
* 14-bit PC over 0x4000 words; targets wrap; links (`r30 = PC + 2`) are not reduced (0x3fff links 0x4001).
  A-family writes the link whether or not it transfers; companion forms (bit 26 clear, `2e5f`, `93`, `9b/98`)
  capture the decision, execute PC+1 once, then transfer. `jmptbl`, `ret`, `indirect`, `callind`,
  `brbitset.ind`/`brbitclr.ind`, `retry.sm`.
* ALU: 16-bit immediates (low/high, and/or/xor/andn/orn, carry/borrow, discard, publish), three-register
  matrix with result transforms (bswap, swap8, rol16, lsr8, lsr1, low16, bitrev8), increment/decrement,
  carry/borrow, predicate-selected forms, `select32`, `not32`, shifts and rotates (register count mod 32,
  reverse, rotate through carry), `bset32`/`bclr32`, `clz*`, byte-lane immediates, `merge8`, `merge32`,
  `rotmask32`, `bfins32`/`bfinsn32`, `rot{xor,and,bic,or,orn,xorn}32`. Bitfield forms are cross-checked
  against the bundled `qeisa.fman_bfins` / `qeisa.fman_circular_geometry` reference models in the self-test.
* Memory: 1/2/3/4/8-byte loads and stores, effective address `align_down(base + disp11, width)`, r31 base
  is address zero, r26/r28/r29 select per-task windows. `ld.sm`/`st.sm` semaphores per 32-byte region, shared
  by all controllers, owned by the invocation that acquired them.
* DMA: `dma.read`/`dma.write` (fixed sizes and `configured`), `dma.count`, 64-bit external address in
  `r[n]:r[n+1]`, requests queued on the task and applied by `complete_dma` (or at once with
  `dma_immediate=True`). `wait.cont` resumes with p0..p15 and p30 clear and r0 = 0.
* Task primitives: `task.set_cont[.reg]`, `task.retrigger[.default_cont]`, `task.handoff`, `park.cont`,
  `wait.cont`, `task.redispatch`, `task.boundary` (DRD0 = source << 6, payload in DRD2/DRD3, r30 zero at the
  continuation), `task.complete_cont`, `task.set_end_nia`, `task.set_fqid`, `task.order.set`. DRD0 values match
  the measurements (continuation 0x403 -> 0x100c0, default continuation -> 0x40).
* Units: `controller.id`, `regpair.init`, `task.info`, `frame.port_id`, `frame.base`, `task.pram_base.read`,
  unit 12 (`unit12.load32/store32/result`), Internet checksum and CRC-16 (`csum.init/setup/accum/result`,
  `crc16.init/result`, ascending mode 0).

## Approximations and unknowns

Anything not listed as implemented raises `Unimplemented`; nothing is a silent no-op except `nop`/`fill.ff`
and `unit0.f8.result` (writes zero).

Choices made where the documentation leaves room:

* A blocked `ld.sm` still performs its load and sets p30; the value is the current memory content.
  `yield_on_sm_retry=True` returns a resumable `SM_RETRY` event when `retry.sm` loops on a blocked semaphore,
  so the outer model can run the owner; with `False` the stage spins into `Runaway`.
* `st.sm` releases the most recently acquired region of the invocation, whatever address it stores to.
  Release order with several held regions is unknown. Ownership survives the end of the invocation
  (a later invocation of the same TNUM is not the owner).
* `task.dma_count` is reset to 16 at every stage start. This is observed after `wait.cont`; for the other
  boundaries it is an assumption.
* DMA write data is sampled from MURAM when the request is issued (`dma_write_sample="complete"` samples at
  completion instead). Requests complete in issue order and never fail.
* r0 is the controller index at every stage start except after `wait.cont`, where it is zero.
* `task.handoff` without an installed continuation leaves DRD0 and `task.continuation` unchanged.
* `task.info` returns `high << 24 | tnum << 16 | low << 8 | tnum`; the two context bytes default to the port
  id and can be set with `task.info_context` (the measured synthetic-task value 0x10040004 is `(0x10, 0)`).
* p16 (controller-0 startup flag used by `brctrl0`) is plain state set by the outer model. Predicates 17..24,
  26..29 and 31 have no documented producer and raise `Unimplemented` when consumed.
* Writes to r26/r28/r29 raise `EmuError`; reading them as scalars gives the window's `scalar`. Immediate
  writes to r27 are ordinary writes (microcode does use `lihi16 r27, ...`).
* `csum.accum` without a preceding `csum.setup` contributes nothing and records a line in `cpu.warnings`.
* A control transfer or boundary in a companion position raises `EmuError` rather than guessing.
* `clz32` is unscaled and `clz4` is four times the count, following the decoder's names.

Not implemented (raise `Unimplemented`): `tnum.alloc`, `unit0.startup2/3/4`, `dma.status`, `task.order.read`,
`task.order.mode`, `task.pram_stage.*`, `task.info.set_*`, the `s26` lifecycle forms, `task.set_cont.retrigger.*`,
`frame.base.set`/`merge_*`, `frame.error_rspid.*`, `frame.icad_a2_ops.*`, `frame.next_nia`, `framewin.fb`,
checksum copy/fixed/24-bit/descending/CRC-32 modes, `ccmatch`, `keycmp`, `match.*`, `search.*`, `scan32.*`,
`popcnt*`, `test32`, `clz4.high16`, publication from count forms, `xor16.*`, `cmpflags.result`, `imm14.eb7`,
`imm16.eblink`, `pcrel24.c3`, `pcrel16.*`, `lea14` to r31, conditional linking indirect forms, units other than
those listed, `noeffect.30/80`, all `*.raw`/`special*` words and undecodable words. `fman_emu.hw.Fman`
supplies `tnum.alloc`, `unit0.startup2/3/4`, `regpair.init` and `frame.base.set` through hooks.

## Layout

    fman_emu/              the emulator
    fman_emu/_vendor/      bundled qeisa code and data (see its README.md)
    tests/                 selftest.py (core), test_board_model.py, __main__.py

## Bundled code

`fman_emu/_vendor/` holds a minimal subset of the `qeisa` package from the qe-isa-research project
(decoder, disassembler, assembler, and two bitfield reference models used by the self-test), its
command-line assembler, and the instruction-set data those read, reduced to the fields the loaders use.
`fman_emu/_vendor/README.md` records the upstream commit and every change made.

## Licence

No licence has been chosen yet for this repository, including the bundled code in
`fman_emu/_vendor/`. Until one is added, no permission to use, copy, modify or distribute it is
granted beyond what applicable law allows.
