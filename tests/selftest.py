#!/usr/bin/env python3
"""Self-test of the core: small hand-written programs, assembled with the bundled
assembler and run on the emulator.

    python3 tests/selftest.py [-v] [-k substring] [--quick]

Expected values are computed here, independently of fman_emu.core.
"""
import os, random, struct, sys, tempfile, time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fman_emu import (BOUNDARY, COMPLETE, HANDOFF, PARK, REDISPATCH, SM_RETRY, STEPS, WAIT, BusError, Cpu, EmuError,
                      ExternalBus, Memory, Runaway, Task, Unimplemented, Window, qe)

M = 0xFFFFFFFF
ORG = 0x100
BASE = 0x1000                      # MURAM scratch used by the tests
TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def run(body, regs=None, cpu=None, task=None, ctl=0, end="task.handoff", org=ORG, **options):
    """Assemble `body` (+ a terminating boundary) at `org` and run one stage."""
    cpu = cpu or Cpu(**options)
    cpu.assemble(body + ("\n" + end + "\n" if end else "\n"), org=org)
    c = cpu.controllers[ctl]
    for n, value in (regs or {}).items():
        c.r[n] = value & M
    task = task or Task(4, 0x10)
    event = cpu.run_stage(ctl, task, entry=org)
    return cpu, c, task, event


def regs_after(body, regs=None, **kw):
    return run(body, regs, **kw)[1].r


def eq(got, want, what=""):
    if got != want:
        show = lambda v: hex(v) if isinstance(v, int) and not isinstance(v, bool) else repr(v)
        raise AssertionError(f"{what}: got {show(got)}, want {show(want)}")


def raises(kind, fn):
    try:
        fn()
    except kind as error:
        return error
    raise AssertionError(f"expected {kind.__name__}")


def sx(v):
    return struct.unpack(">i", struct.pack(">I", v & M))[0]


def result_bits(res):
    """p1..p7, p12, p13, p15 of a 32-bit result (explainer 'Conditions')."""
    b = res.to_bytes(4, "big")
    z, n = res == 0, res >> 31
    return {1: z, 2: bool(n), 3: z or bool(n), 4: res >> 16 == 0 or res & 0xFFFF == 0, 5: res & 0xFFFF == 0,
            6: bool(res & 0x8000), 7: res & 0xFF == 0, 12: 0 in b, 13: bool(res & 1), 14: False, 15: res == 0x80000000}


def cmp_bits(l, r):
    res = (l - r) & M
    overflow = not -2**31 <= sx(l) - sx(r) < 2**31
    return {0: l < r, 8: overflow, 10: sx(l) <= sx(r), 11: sx(l) < sx(r), **result_bits(res)}, overflow


def pmask(bits, extra=0):
    return sum(1 << n for n, v in bits.items() if v) | (1 << 25) | extra


# ---------------------------------------------------------------- immediates
IMMEDIATES = """
        lihi16 r1, 0x1234
        ori16  r1, 0x5678
        li16   r2, 0xffff
        addi16 r2, 0x0001
        li16   r3, 0x0010
        subi16 r3, 0x0011
        lihi16 r4, 0xffff
        ori16  r4, 0xf0f0
        andi16 r4, 0x0ff0
        li16   r5, 0x00ff
        xori16 r5, 0x0f0f
        li24   r30, 0xc3074
        linv16 r6, 0x00ff
        linvhi16 r7, 0x00ff
        lihi16 r8, 0x0001
        addhi16 r8, 0xffff
        orhi16 r8, 0x8000
        lihi16 r9, 0x00ff
        subhi16 r9, 0x0100
        lihi16 r10, 0xffff
        ori16  r10, 0xffff
        andnhi16 r10, 0x0ff0
        andnlo16 r10, 0x000f
        li16   r11, 0x1
        ornlo16 r11, 0x00ff
        li16   r12, 0x1
        ornhi16 r12, 0xfff0
        lihi16 r13, 0xabcd
        ori16  r13, 0x1234
        andhi16 r13, 0xff00
        li16   r14, 7
        addi16 r14, 1 {discard}
        andi16 r14, 0 {discard, publish}
    """


@test
def immediates():
    r = regs_after(IMMEDIATES)
    eq(r[1], 0x12345678, "lihi16/ori16"); eq(r[2], 0x10000, "addi16 carries into the high half")
    eq(r[3], 0xFFFFFFFF, "subi16 wraps"); eq(r[4], 0x00F0, "andi16 zero-extends its immediate")
    eq(r[5], 0x0FF0, "xori16"); eq(r[30], 0xC3074, "li24 r30")
    eq(r[6], 0xFFFFFF00, "linv16"); eq(r[7], 0xFF00FFFF, "linvhi16")
    eq(r[8], 0x80000000, "addhi16/orhi16"); eq(r[9], 0xFFFF0000, "subhi16")
    eq(r[10], 0xF00FFFF0, "andnhi16/andnlo16"); eq(r[11], 0xFFFFFF01, "ornlo16"); eq(r[12], 0x000FFFFF, "ornhi16")
    eq(r[13], 0xAB000000, "andhi16"); eq(r[14], 7, "discard forms leave the register")


@test
def immediate_predicates_and_carry():
    # 64-bit add of 1 to 0x00000001_ffffffff through p0, then a borrow chain.
    cpu, c, _, _ = run("""
        addi16 r3, 1 {publish}
        addi16 r2, 0 {carry}
        subi16 r5, 1 {publish}
        subhi16 r4, 0 {borrow}
        testi16 0 {discard, publish, raw20_16=3}
    """, {2: 1, 3: 0xFFFFFFFF, 4: 5, 5: 0})
    eq((c.r[2], c.r[3]), (2, 0), "addi16 {carry} consumes p0")
    eq((c.r[4], c.r[5]), (4, 0xFFFFFFFF), "subhi16 {borrow} consumes p0")
    eq(c.predicates(), pmask(result_bits(0)), "testi16 publishes from the immediate, carry clear")
    c = run("addi16 r3, 1 {publish}", {3: 0x7FFFFFFF})[1]
    eq(c.predicates(), pmask({**result_bits(0x80000000), 8: True, 9: True}), "addi16 signed overflow")
    c = run("addi16 r3, 1 {publish}\nori16 r4, 1 {publish}", {3: 0xFFFFFFFF, 4: 0})[1]
    eq(c.predicates(), pmask(result_bits(1)), "ori16 {publish} clears carry")
    c = run("xori16 r3, 0x1234 {discard, publish}", {3: 0x1234})[1]
    eq((c.r[3], c.p[1]), (0x1234, True), "xori16 {discard, publish} is an equality test")


# ---------------------------------------------------------------- 3-register ALU
@test
def alu_three_register():
    a, b = 0x89ABCDEF, 0x13570246
    r = regs_after("""
        add32  r3, r1, r2
        sub32  r4, r1, r2
        and32  r5, r1, r2
        or32   r6, r1, r2
        xor32  r7, r1, r2
        andn32 r8, r1, r2
        orn32  r9, r1, r2
        xnor32 r10, r1, r2
        add32  r11, r1, r31
        sub32  r12, r31, r1
        add32  r13, r1, r2 {increment}
        sub32  r14, r1, r2 {decrement}
        not32  r15, r1
        not32  r16, r1 {encoding=f64}
        add32  r1, r2 {discard, raw10_6=17}
    """, {1: a, 2: b, 17: 0x55})
    want = {3: a + b, 4: a - b, 5: a & b, 6: a | b, 7: a ^ b, 8: a & ~b, 9: a | ~b, 10: ~(a ^ b), 11: a, 12: -a,
            13: a + b + 1, 14: a - b - 1, 15: ~a, 16: ~a, 17: 0x55, 31: 0}
    for n, v in want.items():
        eq(r[n], v & M, f"r{n}")


@test
def alu_result_transforms():
    a, b = 0x01020304, 0x10203040
    s = (a + b) & M                              # 0x11223344
    r = regs_after("""
        add32 r3, r1, r2 {bswap}
        add32 r4, r1, r2 {swap8}
        add32 r5, r1, r2 {rol16}
        add32 r6, r1, r2 {lsr8}
        add32 r7, r1, r2 {lsr1}
        add32 r8, r1, r2 {low16}
        add32 r9, r1, r2 {bitrev8}
        xor32 r10, r1, r2 {rol16}
        sub32 r11, r2, r1 {bswap}
    """, {1: a, 2: b})
    eq(r[3], 0x44332211, "bswap"); eq(r[4], 0x22114433, "swap8"); eq(r[5], 0x33441122, "rol16")
    eq(r[6], s >> 8, "lsr8"); eq(r[7], s >> 1, "lsr1"); eq(r[8], 0x3344, "low16")
    eq(r[9], 0x8844CC22, "bitrev8"); eq(r[10], 0x33441122, "xor32 {rol16}"); eq(r[11], 0x3C2D1E0F, "sub32 {bswap}")


@test
def alu_carry_borrow_chains():
    # r2:r3 += r4:r5 and r6:r7 -= r8:r9 through p0.
    r = regs_after("""
        add32 r3, r3, r5 {publish}
        add32 r2, r2, r4 {carry}
        sub32 r7, r7, r9 {publish}
        sub32 r6, r6, r8 {borrow}
    """, {2: 1, 3: 0xFFFFFFF0, 4: 2, 5: 0x20, 6: 5, 7: 1, 8: 1, 9: 2})
    eq((r[2], r[3]), (4, 0x10), "add32 {carry}"); eq((r[6], r[7]), (3, 0xFFFFFFFF), "sub32 {borrow}")


@test
def cmp32_predicate_bank():
    # cmp32 is `sub32 l, r {discard, publish}` (word DC20_0038 | fields).
    pairs = [(5, 5), (3, 9), (9, 3), (0x80000000, 1), (1, 0x80000000), (0x7FFFFFFF, 0xFFFFFFFF), (0, 0x80000000),
             (0x80000000, 0), (0x12340000, 0x1234), (0x00010100, 0x00000100), (0xFFFFFFFF, 0), (0, 0xFFFFFFFF)]
    for l, r in pairs:
        cpu, c, _, _ = run("sub32 r3, r4 {discard, publish, raw10_6=0}", {3: l, 4: r})
        bits, overflow = cmp_bits(l, r)
        eq(c.predicates(), pmask(bits, (1 << 9) if overflow else 0), f"cmp32 {l:#x}, {r:#x}")
        eq((c.r[3], c.r[4]), (l, r), "cmp32 writes nothing")
    eq(qe.disassemble(0xDC232038), "sub32 r3, r4 {discard, publish, raw10_6=0x0}", "cmp32 spelling")
    # r31 supplies constant zero in either position: compare-to-zero.
    c = run("sub32 r3, r31 {discard, publish, raw10_6=0}", {3: 0})[1]
    eq((c.p[0], c.p[1]), (False, True), "cmp32 r3, zero")


@test
def predicates_consumed_by_branches():
    l, r = 0x80000003, 0x00008009            # borrow clear, signed overflow, signed less
    bits, overflow = cmp_bits(l, r)
    assert overflow
    body = "    sub32 r3, r4 {discard, publish, raw10_6=0}\n"
    for n in range(16):
        body += f"    brbitset14 r31, {31 - n}, set{n}\n    xfer14 next{n} {{encoding=b7}}\nset{n}:\n"
        body += f"    ori16 r20, {1 << n:#x}\nnext{n}:\n"
    got = regs_after(body, {3: l, 4: r, 20: 0})[20]
    eq(got, pmask(bits, 1 << 9) & 0xFFFF, "predicate n is r31 selector n = bit code n")


@test
def sticky_overflow_and_logical_publication():
    cpu, c, _, _ = run("""
        sub32 r3, r4 {discard, publish, raw10_6=0}
        and32 r5, r6 {discard, publish, raw10_6=0}
    """, {3: 0x80000000, 4: 1, 5: 0xF0F0, 6: 0x0FF0})
    eq(c.predicates(), pmask(result_bits(0x00F0), 1 << 9), "logical test clears p0/p8/p10/p11, keeps sticky p9")
    eq(c.r[5], 0xF0F0, "test form stores nothing")
    c = run("or32 r5, r3, r4 {publish}\nadd32 r6, r3, r4 {publish, bswap}", {3: 0x00FF0000, 4: 0xFF00FF00})[1]
    eq(c.r[5], 0xFFFFFF00); eq(c.r[6], 0x00FFFFFF, "raw 0xffffff00 byte-swapped")
    eq(c.predicates(), pmask(result_bits(0x00FFFFFF)), "result predicates follow the transformed value, no carry")


@test
def predicate_select_forms():
    body = """
        sub32 r3, r4 {discard, publish, raw10_6=0}
        add32 r7, r5, r6 {select=p1}
        add32 r8, r5, r6 {select=!p1}
        select32 r9, r5, r6 {select=p1}
        select32 r10, r5, r6 {select=!p1}
        sub32 r11, r5, r6 {select=p0}
        not32 r12, r5, r6 {select=p1}
        andn32 r13, r5, r6 {select=!p0}
        add32 r14, r5, r6 {select=p25}
    """
    r = regs_after(body, {3: 7, 4: 7, 5: 0x100, 6: 0x0F})          # equal: p1 set, p0 clear
    eq([r[n] for n in range(7, 15)], [0x10F, 0x100, 0x100, 0x0F, 0x100, 0xFFFFFFF0, 0x100, 0x10F], "p1 set")
    r = regs_after(body, {3: 1, 4: 7, 5: 0x100, 6: 0x0F})          # below: p1 clear, p0 set
    eq([r[n] for n in range(7, 15)], [0x100, 0x10F, 0x0F, 0x100, 0xF1, 0x100, 0x100, 0x10F], "p0 set")
    error = raises(Unimplemented, lambda: run("add32 r7, r5, r6 {select=p20}"))
    eq(error.mnemonic, "add32", "undocumented predicate selector")


# ---------------------------------------------------------------- shifts, bits, counts
@test
def shifts_and_rotates():
    v = 0x80000F01
    r = regs_after("""
        lsl32 r3, r1, 4
        lsr32 r4, r1, 4
        asr32 r5, r1, 4
        rol32 r6, r1, 8
        ror32 r7, r1, 8
        lsl32 r8, r1, r2
        lsr32 r9, r1, r2
        asr32 r10, r1, r2
        rol32 r11, r1, r2
        ror32 r12, r1, r2
        lsl32 r13, r1, 0
        lsr32 r14, r1, r2 {reverse}
        lsl32 r15, r1, r2 {reverse}
        ror32 r16, r1, r2 {increment_count}
        lsr32 r17, r1, 31
    """, {1: v, 2: 0xFFFFFF29})                                      # register count uses the low five bits: 9
    rol = lambda x, n: ((x << n) | (x >> (32 - n))) & M if n else x
    want = [v << 4, v >> 4, sx(v) >> 4, rol(v, 8), rol(v, 24), v << 9, v >> 9, sx(v) >> 9, rol(v, 9), rol(v, 23), v,
            v >> 22, v << 22, rol(v, 22), 1]
    for n, w in zip(range(3, 18), want):
        eq(r[n], w & M, f"r{n}")


@test
def shift_predicates_and_rotate_through_carry():
    c = run("lsl32 r3, r1, 1 {publish}", {1: 0x80000000})[1]
    eq((c.r[3], c.predicates()), (0, pmask({**result_bits(0), 0: True})), "lsl32 publishes the shifted-out bit")
    c = run("lsr32 r3, r1, 4 {publish}", {1: 0x18})[1]
    eq((c.r[3], c.p[0], c.p[1]), (1, True, False), "lsr32 p0 = bit count-1")
    c = run("lsr32 r3, r1, 0 {publish}", {1: 0xFFFFFFFF})[1]
    eq(c.p[0], False, "count 0 clears p0")
    c = run("asr32 r3, r1, 4 {publish}", {1: 0x80000008})[1]
    eq((c.r[3], c.p[0], c.p[2]), (0xF8000000, True, True), "asr32")
    c = run("rol32 r3, r1, r2 {publish}", {1: 0x80000000, 2: 1})[1]
    eq((c.r[3], c.p[0]), (1, False), "rotates publish p0 clear")
    # ror33: {p0, r1} rotated right by 1: p0 enters bit 31, bit 0 leaves into p0.
    c = run("subi16 r9, 1 {publish}\nror32 r3, r1, 1 {publish, carry}", {1: 0x00000003, 9: 0})[1]
    eq((c.r[3], c.p[0]), (0x80000001, True), "ror32 {carry}")
    c = run("ror32 r3, r1, 4 {carry}", {1: 0x0000001F})[1]
    eq(c.r[3], 0xE0000001, "33-bit rotate by 4 with p0 clear")


@test
def bit_set_clear_and_counts():
    r = regs_after("""
        bset32 r3, r1, 31
        bset32 r4, r1, 0
        bclr32 r5, r2, 4
        bset32 r6, r1, r10
        bclr32 r7, r2, r10
        bset32 r8, r1, r10 {reverse}
        bclr32 r9, r2, r10 {reverse}
        clz32  r11, r12
        clz4   r13, r12 {raw20_16=0}
        clz32  r14, r1
        clznot4 r15, r2 {raw20_16=0}
        clz4.low16 r16, r17 {raw20_16=0}
        clzclear32 r18, r19 {raw20_16=0}
    """, {1: 0, 2: 0xFFFFFFFF, 10: 0x45, 12: 0x00010000, 17: 0xFFFF0100, 18: 0x00500000})
    eq(r[3], 0x80000000, "bset32 immediate index is numeric"); eq(r[4], 1); eq(r[5], 0xFFFFFFEF)
    eq(r[6], 0x20, "register index uses the low five bits"); eq(r[7], 0xFFFFFFDF)
    eq(r[8], 0x04000000, "reverse index counts from bit 31"); eq(r[9], 0xFBFFFFFF)
    eq(r[11], 15, "clz32 is unscaled"); eq(r[13], 60, "clz4 is 4 * clz32"); eq(r[14], 32, "clz32(0)")
    eq(r[15], 128, "clznot4"); eq(r[16], 4 * 23, "clz4.low16 masks first")
    eq((r[19], r[18]), (36, 0x00100000), "clzclear32")
    c = run("bclr32 r5, r6, 31 {publish}", {6: 0x80000000})[1]
    eq((c.r[5], c.p[1]), (0, True), "bclr32 {publish}")


@test
def byte_lanes():
    r = regs_after("""
        addlane8 r3, r1, 0, 0x01
        addlane8 r4, r1, 3, 0xff
        sublane8 r5, r1, 2, 0x01
        andlane8 r6, r2, 1, 0xf0
        orlane8  r7, r1, 0, 0x81
        andnlane8 r8, r2, 3, 0x0f
        addlane8 r9, r2, 3, 0x01 {publish}
        addlane8 r10, r1, 2, 0x00 {carry}
        sublane8 r2, 3, 0xff {discard, publish, raw15_11=0}
        sublane8 r11, r1, 3, 0x00 {borrow}
    """, {1: 0x10203040, 2: 0xFFFFFFFF})
    eq(r[3], 0x11203040, "lane 0 is bits 31..24"); eq(r[4], 0x1020313F, "lane 3 is bits 7..0, scalar carry")
    eq(r[5], 0x10202F40); eq(r[6], 0x00F00000); eq(r[7], 0x91203040); eq(r[8], 0xFFFFFFF0)
    eq(r[9], 0); eq(r[10], 0x10203041, "addlane8 {carry}: scalar carry-in at bit 0, whatever the lane")
    eq(r[11], 0x10203040, "no borrow after 0xffffffff - 0xff")


# ---------------------------------------------------------------- bitfields
@test
def merge_and_rotmask():
    a, b = 0x12345678, 0xABCDEF01
    r = regs_after("""
        merge8  r3, r1, r2
        merge32 r4, r1, r2, 16, 8
        merge32 r5, r1, r2, 28, 28
        merge32 r6, r1, r2, 0, 0
        merge32 r7, r1, r2, 8, 24
        merge32 r8, r1, r31, 24, 0
        merge32 r9, r31, r2, 24, 0
        rotmask32 r10, r1, 8, 16, 8
        rotmask32 r11, r1, r12, 16, 8
        rotmask32 r13, r1, 4, 0, 0
        rotmask32 r14, r1, 0, 28, 3
        rotmask32 r15, r1, 0, 8, 24
    """, {1: a, 2: b, 12: 0xDEADBE08})
    eq(r[3], 0x12345601, "merge8: high 24 of the first source, low 8 of the second")
    eq(r[4], (a & 0xFF00) | (b & ~0xFF00 & M), "merge32 bits 15..8 from source_a")
    eq(r[5], (a & 0xF000000F) | (b & 0x0FFFFFF0), "low 28 above high 3: the interval wraps through bit 31")
    eq(r[6], a, "full interval"); eq(r[7], a, "low == high + 1 wraps to all 32 bits")
    eq(r[8], a & 0xFF, "r31 is zero as source_b"); eq(r[9], b & ~0xFF & M, "r31 is zero as source_a")
    rot8 = ((a << 8) | (a >> 24)) & M
    eq(r[10], rot8 & 0xFF00, "rotmask32 immediate"); eq(r[11], rot8 & 0xFF00, "rotmask32 register count & 31")
    eq(r[13], ((a << 4) | (a >> 28)) & M, "rotmask32 as a plain rotate"); eq(r[14], a & 0x8, "one bit")
    eq(r[15], a, "wrapped full-width mask")


@test
def bitfield_insert_matches_research_model():
    from fman_emu._vendor.qeisa.fman_bfins import execute_bfins
    from fman_emu._vendor.qeisa.fman_circular_geometry import circular_mask as reference_mask
    from fman_emu.core import circular_mask
    for low in range(32):
        for high in range(32):
            eq(circular_mask(low, high), reference_mask(low, high), f"circular_mask({low}, {high})")
    rng = random.Random(7)
    for _ in range(60):
        trim, rot, low, dest, src = (rng.randrange(32) for _ in range(5))
        old, value = rng.getrandbits(32), rng.getrandbits(32)
        for body, source in ((f"bfins32 r5, r6, {rot}, {trim}, {low}", value), (f"bfins32 r5, {src}, {rot}, {trim}, {low}", None)):
            cpu, c, _, _ = run(body, {5: old, 6: value})
            eq(c.r[5], execute_bfins(cpu.iram[ORG], old, source), body)
    mask, rot = 0x000000F0, lambda x, n: ((x << n) | (x >> (32 - n))) & M
    r = regs_after("""
        bfins32  r5, r6, 4, 24, 4
        bfinsn32 r7, r6, 4, 24, 4
        rotxor32 r8, r6, 4, 24, 4
        rotand32 r9, r6, 4, 24, 4
        rotbic32 r10, r6, 4, 24, 4
        rotor32  r11, r6, 4, 24, 4
        bfins32  r12, 0x1f, 4, 24, 4
    """, {5: 0x11111111, 6: 0x0000000A, 7: 0x11111111, 8: 0xFF, 9: 0xFF, 10: 0xFF, 11: 0, 12: 0})
    eq(r[5], 0x111111A1, "insert 4 bits at bit 4"); eq(r[7], 0xEEEEEEAE, "bfinsn32 complements the rest")
    eq(r[8], 0xFF ^ 0xA0); eq(r[9], 0xFF & ~(~0xA0 & mask), "rotand32 = AND inside the interval")
    eq(r[10], 0xFF & ~0xA0); eq(r[11], 0xA0); eq(r[12], 0xF0, "immediate source, 5 bits rotated then masked")


# ---------------------------------------------------------------- memory
@test
def memory_loads_and_stores():
    cpu = Cpu()
    cpu.muram.write(BASE, bytes(range(0x10, 0x30)))
    cpu, c, _, _ = run("""
        memb.read.s0 0x2, r1, 0x5
        memh.read.s0 0x3, r1, 0x5
        memw.read.s0 0x4, r1, 0x7
        memd.read.s0 0x6, r1, 0xf
        memw.read.s0 0x8, r1, -1
        memw.read.s0 0x9, r31, 0x10
        memb.write.s0 0x10, r1, 0x103
        memh.write.s0 0x10, r1, 0x105
        memw.write.s0 0x10, r1, 0x10b
        memd.write.s0 0x10, r1, 0x117
        memw.read.s0 0xc, r13, 0x3
        membs.readz 0xe, r1, 0x20
        mem24.read 0xf, r1, 0x6
        memh4.read 0x14, r1, 0x13
    """, {1: BASE, 13: BASE + 3, 16: 0xA1B2C3D4, 17: 0xE5F60718}, cpu=cpu)
    mem = cpu.muram
    eq(c.r[2], 0x15, "byte load zero-extends"); eq(c.r[3], 0x1415, "halfword aligns down")
    eq(c.r[4], 0x14151617, "word aligns down"); eq((c.r[6], c.r[7]), (0x18191A1B, 0x1C1D1E1F), "8 bytes into r6:r7")
    eq(c.r[8], 0, "negative displacement"); eq(c.r[9], 0, "r31 base is address zero")
    eq(mem.read(BASE + 0x100, 8).hex(), "000000d4c3d40000", "byte store; halfword store at 0x105 aligns down")
    eq(mem.r32(BASE + 0x108), 0xA1B2C3D4, "word store aligned down")
    eq(mem.read(BASE + 0x110, 8).hex(), "a1b2c3d4e5f60718", "8-byte store takes r16:r17, aligned to 8")
    eq(c.r[12], 0x14151617, "base + displacement is added before alignment")
    mem.w8(BASE + 0x20, 0x80)
    eq(run("membs.readz 0xe, r1, 0x20", {1: BASE}, cpu=cpu)[1].r[14], 0xFFFFFF80, "signed byte load")
    eq(c.r[15], 0x151617, "mem24.read drops the leading byte")
    eq([c.r[n] for n in range(20, 24)], [0x2021, 0x2223, 0x2425, 0x2627], "memh4.read")
    c = run("mem24.write 0x10, r1, 0x122\nmemd.write.s2 r16, r1, 0x128", {1: BASE, 16: 0x99ABCDEF, 17: 0x01020304}, cpu=cpu)[1]
    eq(mem.r32(BASE + 0x120), 0x00ABCDEF, "mem24.write keeps the leading byte")
    eq(mem.read(BASE + 0x128, 8).hex(), "99abcdef01020304", "memd.write.s2")
    c = run("membu.readz 0x2, r1, 0x0\nmemhu.readz 0x3, r1, 0x0\nmemw.readz 0x4, r1, 0x200", {1: BASE}, cpu=cpu)[1]
    eq((c.r[2], c.r[3], c.r[4], c.p[1]), (0x10, 0x1011, 0, True), "readz forms publish from the loaded value")
    raises(BusError, lambda: run("memw.read.s0 0x2, r1, 0x0", {1: 0x60000}))
    raises(BusError, lambda: run("memw.read.s0 0x2, r31, -4"))


@test
def context_windows():
    cpu = Cpu()
    task = Task(9, 0x0C)
    frame = Memory(0x100, 0, "frame")
    frame.write(0, bytes(range(0x40, 0x60)))
    task.windows[28] = Window(frame, 0, scalar=0x4500)               # frame in its own space
    cpu.muram.w32(0x21A00, 0x20000000)
    cpu.muram.w32(0x21A04, 0x00021C20)
    task.windows[29] = Window(cpu.muram, 0x21A00)                    # parameter page in MURAM
    task.ic.w32(0x18, 0x21C00)
    cpu, c, _, _ = run("""
        memw.read.s0 0x2, r28, 0x3
        memh.read.s0 0x3, r28, 0x3
        memb.read.s0 0x4, r28, 0x9
        memw.read.s0 0x5, r26, 0x18
        memw.read.s0 0x6, r29, 0x4
        add32 r7, r28, r31
        add32 r8, r29, r31
        memw.write.s0 0x5, r28, 0x40
        frame.base r9
        task.pram_base.read r10
        memd.read.s0 0xc, r28, 0xc
    """, cpu=cpu, task=task)
    eq(c.r[2], 0x40414243, "offsets 0..3 select the word at byte 0"); eq(c.r[3], 0x4243); eq(c.r[4], 0x49)
    eq(c.r[5], 0x21C00, "r26 reaches the internal context"); eq(c.r[6], 0x21C20, "r29 reaches the parameter page")
    eq((c.r[7], c.r[8], c.r[9], c.r[10]), (0x4500, 0x21A00, 0x4500, 0x21A00), "scalar views")
    eq(frame.r32(0x40), 0x21C00, "store through r28"); eq((c.r[12], c.r[13]), (0x48494A4B, 0x4C4D4E4F))
    # merge8 turns the frame base and a byte offset into an ordinary address when the frame lives in MURAM.
    cpu.muram.write(0x4500, bytes(range(0x80, 0xC0)))
    task.windows[28] = Window(cpu.muram, 0x4500)
    c = run("merge8 r17, r28, r18\nmemb.read.s0 0x2, r17, 0x0\nmemb.read.s0 0x3, r28, 0x9e",
            {18: 0xFFFFFF1E}, cpu=cpu, task=task)[1]
    eq((c.r[17], c.r[2]), (0x451E, 0x9E), "merge8 bridge")
    raises(EmuError, lambda: run("memw.read.s0 0x2, r28, 0x0"))      # no frame bound
    raises(EmuError, lambda: run("add32 r28, r1, r2"))               # contextual selector as destination


# ---------------------------------------------------------------- control flow
@test
def direct_and_companion_transfers():
    r = regs_after("""
        xfer14 a {encoding=b7}
        ori16 r1, 0x1
a:      xfer14 b {next}
        ori16 r1, 0x2
        ori16 r1, 0x4
b:      xfer14 c
        ori16 r1, 0x8
        ori16 r1, 0x10
c:      call d
        ori16 r1, 0x20
        ori16 r1, 0x40
        xfer14 e {encoding=b7}
d:      add32 r2, r30, r31
        ret
e:      call f {next}
        ori16 r1, 0x80
        ori16 r1, 0x100
        xfer14 g {encoding=b7}
f:      add32 r3, r30, r31
        ret {next}
        ori16 r1, 0x200
        ori16 r1, 0x400
g:
    """.replace("a:      ", "a:\n").replace("b:      ", "b:\n").replace("c:      ", "c:\n").replace("d:      ", "d:\n")
       .replace("e:      ", "e:\n").replace("f:      ", "f:\n"), {1: 0})
    # B7 skips PC+1; B3 and 2E5F execute it once; A7 skips it and links PC+2; A3 executes it and links PC+2.
    eq(r[1], 0x2 | 0x8 | 0x40 | 0x80 | 0x100 | 0x200, "which words executed")
    eq((r[2], r[3]), (ORG + 10, ORG + 16), "r30 = PC + 2")


@test
def register_bit_branches():
    # All eight A/B forms on r5 bit 7 with the bit set and clear.  Each case marks: 1 = companion ran,
    # 2 = fell through to PC+2, 4 = reached the target.
    def case(mnemonic, next_, value):
        r = regs_after(f"""
            {mnemonic} r5, 7, target{' {next}' if next_ else ''}
            ori16 r1, 0x1
            ori16 r1, 0x2
            xfer14 done {{encoding=b7}}
        target:
            ori16 r1, 0x4
        done:
        """, {1: 0, 5: value, 30: 0x7777})
        return r[1], r[30]
    for mnemonic, on_set, link in (("brbitset14", True, False), ("brbitclr14", False, False),
                                   ("callbitset14", True, True), ("callbitclr14", False, True)):
        for value in (0x80, 0xFFFFFF7F):
            taken = bool(value & 0x80) == on_set
            want_link = ORG + 2 if link else 0x7777
            eq(case(mnemonic, False, value), (4 if taken else 3, want_link), f"{mnemonic} direct, r5={value:#x}")
            eq(case(mnemonic, True, value), (5 if taken else 3, want_link), f"{mnemonic} companion, r5={value:#x}")
    # The decision of a companion form is captured before its companion executes.
    r = regs_after("""
        brbitset14 r5, 0, target {next}
        li16 r5, 0
        xfer14 done {encoding=b7}
    target:
        ori16 r1, 0x4
    done:
    """, {1: 0, 5: 1})
    eq((r[1], r[5]), (4, 0), "captured decision")
    eq(regs_after("brbitset14 r5, 31, t\nori16 r1, 1\nt:", {1: 0, 5: 0x80000000})[1], 0, "bit 31")
    # r31 with selector 31 bypasses the condition even in the branch-on-clear encoding (BFFF).
    cpu = Cpu()
    cpu.load(ORG, [0xBFFF0002, 0xEBC10001, 0x281FF800])
    eq(cpu.run_stage(0, Task(1), entry=ORG).steps, 2, "BFFF always transfers")


@test
def predicate_branch_aliases():
    body = """
        subi16 r3, 1 {publish}
        %s t
        ori16 r1, 1
    t:
    """
    for mnemonic, value, taken in (("cbrz14", 1, True), ("cbrz14", 2, False), ("cbrnz14", 2, True), ("cbrnz14", 1, False),
                                   ("cbrnn14", 2, True), ("cbrnn14", 0, False)):
        suffix = " {next}" if mnemonic == "cbrnn14" else ""
        got = regs_after(body % mnemonic + suffix if False else (body % mnemonic).replace(" t\n", f" t{suffix}\n"), {1: 0, 3: value})[1]
        eq(got, 1 if (not taken or suffix) else 0, f"{mnemonic} after result {value - 1}")
    eq(qe.disassemble(0xB43F0000 | 5), "cbrz14 0x5", "B43F is brbitset14 r31, selector 1")
    # p16 is the controller-0 startup predicate; the outer model owns it.
    cpu = Cpu()
    cpu.controllers[0].p[16] = True
    eq(run("brctrl0 t {next}\nnop\nori16 r1, 1\nt:", {1: 0}, cpu=cpu)[1].r[1], 0, "brctrl0")


@test
def indirect_calls_and_tables():
    r = regs_after("""
        lea14.c1? 0x4, 0x0, sub
        callind r4
        ori16 r1, 0x1
        ori16 r1, 0x2
        lea14.c1? 0x6, 0x0, hop
        indirect r6 {next}
        ori16 r1, 0x4
        ori16 r1, 0x8
    hop:
        li16 r9, 0
        brbitclr.ind r9, 31, r4
        ori16 r1, 0x10
        xfer14 done {encoding=b7}
    sub:
        add32 r2, r30, r31
        add32 r3, r3, r4 {increment}
        brbitset.ind r3, 0, r30
        ori16 r1, 0x20
    done:
    """, {1: 0, 3: 0})
    # callind skips PC+1 and links; indirect {next} runs PC+1; brbitclr.ind r9, 31, r4 re-enters `sub`
    # without linking or running its companion; the second time r3 is even and brbitset.ind falls through.
    eq(r[1], 0x2 | 0x4 | 0x20, "path"); eq(r[2], ORG + 3, "brbitclr.ind does not write r30")
    eq(r[4], ORG + 12, "lea14.c1 builds a code coordinate")
    for index, want in ((0, 1), (1, 2), (2, 4), (3, 8), (0x47, 8)):
        r = regs_after("""
            jmptbl r5, table, 4
            ori16 r1, 0x100
        table:
            ori16 r1, 1
            xfer14 done {encoding=b7}
            ori16 r1, 2
            xfer14 done {encoding=b7}
            ori16 r1, 4
            xfer14 done {encoding=b7}
            ori16 r1, 8
        done:
        """, {1: 0, 5: index})
        eq(r[1], want, f"jmptbl index {index}")
    r = regs_after("jmptbl r5, table, 2 {next}\nori16 r1, 0x100\ntable:\nori16 r1, 1\nnop\nori16 r1, 2", {1: 0, 5: 3})
    eq(r[1], 0x102, "jmptbl {next} runs PC+1 once; 2-entry table masks the index")


@test
def pc_ring_and_link_width():
    cpu = Cpu()
    cpu.assemble("nop\nnop\nt:\ntask.handoff\n", org=0x0)       # target 2 = 0x3fff + 3 mod 0x4000
    cpu.write_iram(0x3FFF, 0xA7FF0003)
    event = cpu.run_stage(0, Task(1), entry=0x3FFF)
    eq((event.pc, cpu.controllers[0].r[30]), (2, 0x4001), "target wraps, the link is not reduced to 14 bits")
    cpu.assemble("ret\n", org=0x10)
    cpu.controllers[0].r[30] = 0x4002                           # ret applies code_coordinate(r30)
    eq(cpu.run_stage(0, Task(1), entry=0x10).pc, 2, "ret uses the low 14 bits")
    r = regs_after("lea24 r5, 0x12 {raw23_21=0}\nlea16.lo r6, 0x12 {raw23_21=0}\npcrel24.c2pos? t\nnop\nt:", {6: 0xABCD0000})
    eq((r[5], r[6], r[30]), (ORG + 0x12, 0xABCD0000 | (ORG + 1 + 0x12), ORG + 4), "lea24, lea16.lo, C2")
    raises(EmuError, lambda: run("xfer14 a {next}\nxfer14 b {encoding=b7}\nnop\na:\nb:"))


# ---------------------------------------------------------------- semaphores
SM_BODY = """
    lihi16 r1, 0
    ori16  r1, 0x540
acquire:
    ld.sm 0x2, 0x2, r1, {load}
    retry.sm acquire
    addi16 r2, 1
    {store} 0x2, r1, {disp}
    {end}
"""


@test
def semaphores_across_two_controllers():
    cpu = Cpu()
    cpu.muram.w32(0x540, 100)
    cpu.assemble(SM_BODY.format(load="0x0", store="memw.write.s0", disp="0x0", end="task.handoff"), org=0x100)
    cpu.assemble(SM_BODY.format(load="0x0", store="st.sm 0x2,", disp="0x4", end="task.handoff"), org=0x200)
    cpu.assemble(SM_BODY.format(load="0x1c", store="st.sm 0x2,", disp="0x1c", end="task.handoff"), org=0x300)
    cpu.assemble(SM_BODY.format(load="0x20", store="st.sm 0x2,", disp="0x20", end="task.handoff"), org=0x400)
    a, b, d = Task(5), Task(6), Task(7)
    # Task 5 acquires on controller 0 and stops inside its critical section.
    eq(cpu.run_stage(0, a, entry=0x200, max_steps=5).kind, STEPS, "stopped after the load")
    eq(cpu.controllers[0].p[30], False, "acquired: selector 30 clear")
    # Task 6 on controller 1 contends anywhere in the same 32-byte region ...
    event = cpu.run_stage(1, b, entry=0x300)
    eq((event.kind, event.resumable, cpu.controllers[1].p[30]), (SM_RETRY, True, True), "pending acquisition")
    eq(cpu.run_stage(1, b, resume=True).kind, SM_RETRY, "still blocked")
    # ... while the next region is free.
    eq(cpu.run_stage(2, d, entry=0x400).kind, HANDOFF, "0x560 is another semaphore")
    # st.sm at 0x544 writes there but releases the region acquired at 0x540.
    eq(cpu.run_stage(0, a, resume=True).kind, HANDOFF)
    eq((cpu.muram.r32(0x540), cpu.muram.r32(0x544)), (100, 101), "st.sm stores at its own address")
    eq(cpu.run_stage(1, b, resume=True).kind, HANDOFF, "contender acquires after the release")
    eq(cpu.semaphores, {}, "everything released")
    # An ordinary store does not release, and ownership survives task completion.
    eq(cpu.run_stage(0, a, entry=0x100).kind, HANDOFF)
    eq(len(cpu.semaphores), 1, "semaphore still owned after the owner completed")
    eq(cpu.run_stage(1, b, entry=0x300).kind, SM_RETRY, "another task stays blocked")
    eq(cpu.run_stage(0, a, entry=0x200).kind, SM_RETRY, "a later invocation of the same TNUM is not the owner")
    cpu.yield_on_sm_retry = False
    raises(Runaway, lambda: cpu.run_stage(1, Task(8), entry=0x300))


@test
def semaphore_load_variants():
    cpu = Cpu()
    cpu.muram.w32(0x600, 0)
    cpu.muram.w32(0x604, 0xAA000000)
    cpu, c, task, _ = run("""
        li16 r9, 5
        subi16 r9, 1 {publish}
        ld.sm 0x2, 0x2, r1, 0x0
        add32 r10, r31, r31 {select=!p1}
        ld.sm.s5 r3, r1, 0x0
        st.sm.low24? r4, r1, 0x4
    """, {1: 0x600, 4: 0x12345678, 10: 1}, cpu=cpu)
    eq(c.r[10], 0, "ld.sm preserves the predicates (p1 still clear)")
    eq(c.p[1], True, "ld.sm.s5 publishes from the loaded zero")
    eq(cpu.muram.r32(0x604), 0xAA345678, "st.sm.low24 keeps the high byte")
    eq(cpu.semaphores, {}, "st.sm.low24 releases")
    cpu.muram.write(0x620, bytes(range(1, 9)))
    c = run("ld.sm 0x0, 0x2, r1, 0x21\nld.sm 0x1, 0x3, r1, 0x21\nld.sm 0x3, 0x4, r1, 0x21\nst.sm 0x0, 0x2, r1, 0x27",
            {1: 0x600}, cpu=cpu)[1]
    eq((c.r[2], c.r[3], c.r[4], c.r[5]), (2, 0x0102, 0x01020304, 0x05060708), "ld.sm shapes 1/2/8 bytes")


# ---------------------------------------------------------------- DMA and wait.cont
DMA_BODY = """
    li16 r4, 0
    lihi16 r5, 0x8000
    ori16 r5, 0x0101
    li16 r6, 0x1003
    dma.read r4, r6, 8, 0x10
    addi16 r5, 0x20
    addi16 r6, 0x40
    dma.count 0xd
    dma.read r4, r6, configured
    li16 r7, 0xff0c
    dma.count r7 {raw20_16=0}
    addi16 r5, 0x40
    dma.write r4, r6, configured
    sub32 r8, r8 {discard, publish, raw10_6=0}
    wait.cont resume
    nop
resume:
    li16 r11, 0x1014
    memw.read.s0 0x9, r11, 0x0
    li16 r4, 0
    lihi16 r5, 0x8000
    li16 r6, 0x1043
    dma.write r4, r6, configured
    park.cont resume
"""


def dma_setup(**options):
    cpu = Cpu(**options)
    ram = cpu.external.add_ram(0x80000000, 0x1000)
    ram.write(0x100, bytes(range(0x100, 0x200, 1)[n] & 0xFF for n in range(0x100)))
    cpu.muram.write(0x1043, b"0123456789abcdef")
    return cpu, ram


@test
def dma_queueing_and_wait_cont():
    cpu, ram = dma_setup(scramble=True)
    cpu, c, task, event = run(DMA_BODY, cpu=cpu, end=None)
    eq((event.kind, event.dma_pending, event.continuation, task.open_dma), (WAIT, True, ORG + 16, 3), "wait.cont")
    eq(cpu.muram.read(0x1013, 8), bytes(8), "a queued read is not visible yet")
    eq(ram.read(0x161, 13), bytes(range(0x61, 0x6E)), "a queued write has not reached the bus")
    eq(cpu.complete_dma(task), 3, "three requests before one wait")
    eq(task.open_dma, 0)
    eq(cpu.muram.read(0x1013, 8), bytes(range(1, 9)), "fixed 8-byte read at base + displacement, unaligned")
    eq(cpu.muram.read(0x1043, 14), bytes(range(0x21, 0x2E)) + b"d", "configured read: dma.count 13")
    eq(ram.read(0x161, 14), b"0123456789abc" + bytes([0x6E]), "write samples MURAM at issue; (r7 & 0xff) + 1 = 13")
    event = cpu.run_stage(1, task)                               # resumes on another controller
    eq((event.kind, event.pc), (PARK, ORG + 22), "continuation")
    c1 = cpu.controllers[1]
    eq(c1.r[9], 0x02030405, "the DMA result is visible at the continuation")
    eq((c1.r[0], c1.r[12] >> 20), (0, 0xBAD), "r0 is zero after wait.cont; registers are the controller's")
    eq(task.dma_queue[0].length, 16, "configured length is 16 again after a wait")
    eq(task.drd[0], (ORG + 16) << 6, "park.cont publishes its target in DRD0[31:6]")
    # No DMA pending: the task simply parks.
    event = run("wait.cont t\nt:", end="nop")[3]
    eq((event.kind, event.dma_pending), (WAIT, False), "wait.cont without DMA")


@test
def wait_resume_clears_predicates():
    cpu, _ = dma_setup()
    cpu, c, task, event = run("""
        li16 r4, 0
        lihi16 r5, 0x8000
        li16 r6, 0x1000
        dma.read r4, r6, 1
        sub32 r31, r6 {discard, publish, raw10_6=0}
        ld.sm 0x2, 0x2, r6, 0x100
        wait.cont resume
    resume:
        task.handoff
    """, cpu=cpu, end=None)
    eq(c.predicates() & 0xFFFF != 0, True, "predicates set before the wait")
    cpu.complete_dma(task)
    cpu.run_stage(0, task)
    eq(c.predicates(), 1 << 25, "selectors 0-15 and 30 are clear at the continuation")


@test
def dma_immediate_mode_and_bus_hooks():
    cpu, ram = dma_setup(dma_immediate=True)
    seen = []
    cpu.external.read_span = lambda address, n: (seen.append((address, n)), (address & ~7, (n + (address & 7) + 7) & ~7))[1]
    cpu, c, task, event = run("""
        li16 r4, 0
        lihi16 r5, 0x8000
        ori16 r5, 0x0103
        li16 r6, 0x1000
        dma.read r4, r6, 1
        memb.read.s0 0x7, r6, 0x0
        memb.write.s0 0x7, r6, 0x8
        addi16 r6, 8
        dma.write r4, r6, 1
        dma.write r4, r6, 2
        dma.write r4, r6, 4
        dma.write r4, r6, 32
        dma.write r4, r6, 256
        dma.read r4, r6, 256
        dma.count 0x100
        dma.read r4, r6, configured
    """, cpu=cpu)
    eq((c.r[7], task.open_dma, task.dma_queue), (3, 0, []), "immediate completion")
    eq(seen[0], (0x80000103, 1), "the bus sees the request")
    eq(cpu.external.log[0], ("r", 0x80000100, 8), "and decides how much it really reads")
    eq([entry[2] for entry in cpu.external.log[1:6]], [1, 2, 4, 32, 256], "fixed write sizes")
    eq(cpu.external.log[-1], ("r", 0x80000100, 264), "dma.count 256, widened by the bus hook")
    cpu.external.read_span = None
    # 64-bit external addresses, and the alternative sampling of write data at completion.
    cpu = Cpu(dma_write_sample="complete")
    high = cpu.external.add_ram(0x1_0000_0000, 0x100)
    cpu, c, task, _ = run("dma.write r4, r6, 4\nli16 r7, 0x55\nmemb.write.s0 0x7, r6, 0x0",
                          {4: 1, 5: 0x10, 6: 0x2000}, cpu=cpu)
    cpu.complete_dma(task)
    eq(high.read(0x10, 4), b"\x55\0\0\0", "sampled at completion")
    raises(BusError, lambda: run("dma.read r4, r6, 1", {4: 0, 5: 0x1234, 6: 0x2000}, dma_immediate=True))


# ---------------------------------------------------------------- task primitives
@test
def handoff_retrigger_and_continuations():
    cpu, c, task, event = run("""
        task.set_cont later
        li16 r5, 1
        task.handoff
    later:
        task.retrigger
        lea14.c1? 0x6, 0x0, again
        task.set_cont.reg r6
        task.handoff
    again:
        task.retrigger.default_cont
        task.handoff
    """, org=0x400, end=None)
    eq((event.kind, event.retrigger, event.continuation, task.drd[0]), (HANDOFF, False, 0x403, 0x100C0),
       "handoff publishes continuation << 6 (0x403 -> 0x100c0 as measured)")
    event = cpu.run_stage(2, task)
    eq((event.kind, event.retrigger, event.continuation), (HANDOFF, True, 0x407), "armed retrigger readmits")
    event = cpu.run_stage(3, task)
    eq((event.retrigger, event.continuation, task.drd[0], task.retrigger), (True, 1, 0x40, False), "default continuation is word one")
    eq(task.dispatches, 3)


@test
def boundary_redispatch_and_completion():
    cpu, c, task, event = run("""
        li16 r9, 0x1a
        lihi16 r10, 0xff12
        ori16 r10, 0x3456
        task.set_end_nia 0x9, r10
        li16 r24, 0xc234
        call callee
        nop
    back:
        add32 r7, r30, r31
        li16 r8, 0x2c0
        task.set_end_nia 0x9, r8
        task.redispatch
    callee:
        li16 r5, 0x77
        task.boundary r24, r30
    """, {7: 5}, end=None)
    eq((event.kind, event.continuation, event.nia, event.payload), (BOUNDARY, ORG + 7, 0x123456, 0xC234), "task.boundary")
    eq(task.drd[0], (ORG + 7) << 6, "DRD0 = source << 6")
    eq(((task.drd[2] & 3) << 14) | (task.drd[3] >> 18), 0xC234, "documented payload decode")
    task.drd[3] |= 0x2ABCD
    c.r[30] = 0x999                                              # a foreign value; the boundary zeroes r30
    seen = []
    cpu.boundary_hook = seen.append
    event = cpu.run_stage(1, task)
    eq((event.kind, event.nia, cpu.controllers[1].r[7]), (REDISPATCH, 0x2C0, 0), "r30 is zero after the boundary")
    eq(seen, [event], "boundary hook")
    event = run("li16 r5, 0x4123\ntask.boundary preserve, r5", cpu=cpu, task=task, end=None)[3]
    eq((event.payload, event.continuation, task.drd[3] & 0x3FFFF), (None, 0x123, 0x2ABCD), "preserve keeps DRD3")
    event = run("li16 r5, 0x4321\ntask.complete_cont r5\nnop", cpu=cpu, task=task, end=None)[3]
    eq((event.kind, event.continuation, task.drd[0]), (COMPLETE, 0x321, 0x321 << 6), "task.complete_cont")


@test
def registers_belong_to_the_controller():
    cpu = Cpu()
    cpu.assemble("""
        li16 r5, 0x1234
        lihi16 r27, 0x2000
        task.set_cont next
        task.retrigger
        task.handoff
    next:
        add32 r6, r5, r31
        add32 r7, r0, r31
        controller.id r8
        task.handoff
    """, org=ORG)
    task = Task(4)
    cpu.run_stage(0, task, entry=ORG)
    cpu.controllers[0].r[0] = 0x55                               # r0 is re-initialised at every dispatch
    cpu.run_stage(0, task)
    c0 = cpu.controllers[0]
    eq((c0.r[6], c0.r[7], c0.r[8]), (0x1234, 0, 0), "same controller keeps r5; r0 = controller id")
    cpu.run_stage(0, task, entry=ORG)
    cpu.run_stage(3, task)
    c3 = cpu.controllers[3]
    eq((c3.r[6], c3.r[7], c3.r[8], c3.r[27]), (0, 3, 3, 0), "migration exposes the other controller's registers")
    # The scramble option makes reliance on registers across a boundary visible even on one controller.
    cpu.scramble = True
    cpu.run_stage(0, task, entry=ORG)
    cpu.run_stage(0, task)
    eq((c0.r[6] >> 20, c0.r[27]), (0xBAD, 0x20000000), "scrambled r1..r25; r27 is plain controller state")


@test
def units():
    cpu = Cpu()
    cpu.local.register(0xC3074, 0x11)
    cpu.muram.w32(0x2000, 0xCAFEF00D)
    task = Task(4, 0x0C)
    task.info_context = (0x10, 0)
    cpu, c, _, _ = run("""
        controller.id r1
        regpair.init r2
        task.info r4
        frame.port_id r5
        li24 r30, 0xc3074
        unit12.load32 r30, r30
        unit12.result r6
        li16 r7, 0x80
        unit12.store32 r7, r30
        unit12.result r8
        li16 r9, 0x2003
        unit12.load32 r9, r9
        unit12.result r10
        unit12.store32 r5, r9
        frame.base r11
        unit0.f8.result r25
        lihi16 r12, 0x12ab
        ori16 r12, 0xcdef
        task.set_fqid r12, r12
        li16 r13, 3
        task.order.set r13 {encoding=op7, raw20_16=0x0}
    """, {11: 9, 25: 9}, cpu=cpu, task=task, ctl=2)
    eq((c.r[1], c.r[2], c.r[3]), (2, 2, 0x41), "controller.id, regpair.init")
    eq((c.r[4], c.r[5]), (0x10040004, 0x0C), "task.info as measured for a synthetic TNUM 4; frame.port_id")
    eq((c.r[6], c.r[8], cpu.local.words[0xC3074]), (0x11, 0x80, 0x80), "unit 12 on an FMan register")
    eq((c.r[10], cpu.muram.r32(0x2000)), (0xCAFEF00D, 0x0C), "unit 12 on MURAM, word-aligned")
    eq((c.r[11], c.r[25], task.fqid, task.order), (0, 0, 0xABCDEF, 3), "frame.base without a frame, f8, fqid, order")
    raises(BusError, lambda: run("unit12.load32 r3, r3", {3: 0x9000_0000}))
    eq(Task(0x5F, 0x0C).info() | 0x80000000, 0x8C5F0C5F, "live-port layout")


CHECKSUM = """
        csum.init
        crc16.init
        csum.setup
        csum.accum r12, r10, 0x1
        csum.result r5
        crc16.result r6
        csum.accum r12, r10, 0x1
        csum.result r7
        csum.setup
        li16 r12, 0
        csum.accum r12, r28, 0xd
        csum.result r8
        crc16.result r31
    """


@test
def checksum_and_crc():
    header = bytes.fromhex("4500003c1c4640004006" + "0000" + "ac100a63ac100a0c")
    cpu = Cpu()
    cpu.muram.write(0x3001, header)                              # odd start address
    task = Task(1)
    task.windows[28] = Window(cpu.muram, 0x3000)
    cpu, c, _, _ = run(CHECKSUM, {10: 0x3000, 12: 19}, cpu=cpu, task=task)
    eq(c.r[5], 0xFFFF ^ 0xB1E6, "IPv4 header sum, not complemented (checksum 0xb1e6)")
    crc = 0
    for byte in header:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    eq(c.r[6], crc, "reflected CRC-16/CCITT, no final XOR")
    eq(c.r[7], c.r[5], "a contribution without csum.setup adds nothing")
    eq(len(cpu.warnings), 1)
    total = (c.r[5] + 0xAC10) % 0xFFFF or 0xFFFF
    eq(c.r[8], total, "range 0 adds one whole word through the r28 window")
    cpu.muram.write(0x3100, b"123456789")
    c = run("crc16.init\ncsum.setup\ncsum.accum r12, r10, 0x0\ncrc16.result r6", {10: 0x3100, 12: 8}, cpu=cpu)[1]
    eq(c.r[6], 0x2189, "CRC consumes range + 1 bytes (CRC-16/KERMIT check value)")
    c2 = run("csum.result r5", cpu=cpu, ctl=1)[1]
    eq(c2.r[5], 0, "the running sum is controller-local")


# ---------------------------------------------------------------- diagnostics
@test
def unimplemented_is_loud():
    for word, mnemonic in ((0x7800F900, "tnum.alloc"), (0x777FF802, "unit0.startup2"), (0x5D0A4008, "dma.status"),
                           (0x4DC8C800, "ccmatch.lookup"), (0x30000000, "noeffect.30"), (0xFB120000, None),
                           (0xC3000010, "pcrel24.c3"), (0x7800F901, "task.order.read")):
        cpu = Cpu()
        cpu.load(ORG, [word])
        error = raises(Unimplemented, lambda: cpu.run_stage(0, Task(1), entry=ORG))
        eq((error.word, error.pc), (word, ORG), "exception fields")
        if mnemonic:
            eq(error.mnemonic, mnemonic, f"{word:#010x}")
    cpu = Cpu()
    cpu.load(ORG, [0xFFFFFFFF, 0xFFFFFFFF, 0x281FF800])
    eq(cpu.run_stage(0, Task(1), entry=ORG).steps, 3, "fill.ff executes sequentially")
    # The outer model can supply an instruction through a hook.
    cpu = Cpu()
    cpu.hooks["tnum.alloc"] = lambda cpu, c, i: c.set(i.a, 0x80000005)
    eq(run("tnum.alloc r3", cpu=cpu)[1].r[3], 0x80000005, "hook")


@test
def trace_counters_and_runaway():
    records = []
    cpu, c, task, event = run("li16 r3, 2\nloop:\nsubi16 r3, 1 {publish}\ncbrnz14 loop", trace=records.append)
    eq((event.steps, cpu.total_steps, len(records)), (6, 6, 6), "instruction counters")
    first = records[1]
    eq((first.pc, first.word, first.text, first.changes["r3"]), (ORG + 1, 0xEC630001, "subi16 r3, 0x1 {publish}", 1), "trace record")
    eq("p" in first.changes and "r3=0x1" in str(first), True, "predicate changes are traced")
    error = raises(Runaway, lambda: run("loop:\nxfer14 loop {encoding=b7}", stage_limit=500))
    eq("500" in str(error), True)
    cpu, c, task, event = run("nop\nnop\nnop")
    cpu.assemble("nop\nnop\nnop\ntask.handoff\n", org=0x200)
    event = cpu.run_stage(0, task, entry=0x200, max_steps=2)
    eq((event.kind, event.resumable, event.pc), (STEPS, True, 0x202), "step budget")
    eq(cpu.run_stage(0, task, resume=True).kind, HANDOFF, "resume")
    raises(EmuError, lambda: cpu.run_stage(0, task, resume=True))


# ---------------------------------------------------------------- assembler front ends
@test
def assembler_paths_agree():
    """The in-process assembler and the command-line one give the same words for the
    hand-written snippets above."""
    source = f".arch ls1046a-fman\n.risc 0\n.org {ORG:#x}\n" + IMMEDIATES + CHECKSUM + "task.handoff\n"
    origin, words = qe.assemble(source)
    eq((origin, len(words)), (ORG, sum(1 for line in source.splitlines()[3:] if line.strip())), "origin and size")
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "snippets.asm")
        with open(path, "w") as f:
            f.write(source)
        eq(qe.assemble_cli(path, os.path.join(directory, "snippets.bin")), words, "command-line assembler")
    eq([qe.decode(w, origin + n).text for n, w in enumerate(words)][:2], ["lihi16 r1, 0x1234", "ori16 r1, 0x5678"],
       "disassembly round trip")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    verbose, quick = "-v" in argv, "--quick" in argv
    pattern = argv[argv.index("-k") + 1] if "-k" in argv else ""
    failed = 0
    started = time.time()
    for fn in TESTS:
        if pattern not in fn.__name__:
            continue
        t0 = time.time()
        try:
            fn(quick=True) if quick and "quick" in fn.__code__.co_varnames else fn()
            outcome = "ok"
        except Exception as error:                               # noqa: BLE001 - report and continue
            failed += 1
            outcome = f"FAIL: {type(error).__name__}: {error}"
            if verbose:
                import traceback
                traceback.print_exc()
        print(f"{fn.__name__:<44} {outcome}  ({time.time() - t0:.1f}s)")
    print(f"{'FAILED' if failed else 'passed'}: {len(TESTS) - failed}/{len(TESTS)} in {time.time() - started:.1f}s")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
