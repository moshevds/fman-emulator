"""Bridge to the vendored ISA package (`fman_emu._vendor.qeisa`): decoding of raw
32-bit words (the emulator never looks at assembler source) and an in-process
front end to the assembler for the tests.

The package and its instruction data (`_vendor/isa/*.yaml`) ship with this
repository; see `_vendor/README.md` for their origin.  PyYAML is the only
third-party dependency.
"""
import os, struct, subprocess, sys
from pathlib import Path

from ._vendor.qeisa import disasm, encodings, semantic
from ._vendor.qeisa.asm import assemble_text

ISA_DIR = Path(__file__).resolve().parent / "_vendor" / "isa"
PROFILE = "ls1046a-fman"
_state = {}


def _load():
    if _state:
        return _state
    every = encodings.load_instruction_definitions(ISA_DIR / "encodings.yaml", profile="all")
    # Same selection rule as load_instruction_definitions(profile=PROFILE), without parsing twice.
    defs = tuple(d for d in every if not d.profiles or PROFILE in d.profiles)
    forms = semantic.load_semantic_forms(ISA_DIR / "semantic-syntax.yaml", every)
    ids = {d.id for d in defs}
    _state.update(defs=defs, forms={k: v for k, v in forms.items() if k in ids}, disasm=disasm,
                  candidates=encodings.decode_candidates, assemble_text=assemble_text)
    return _state


class Ins:
    """One decoded instruction word.

    m      semantic mnemonic as the disassembler prints it, without a trailing '?'
    decs   decorators: {'publish': True, 'select': 'p1' / '!p1', 'encoding': 'b7', 'raw10_6': 7, ...}
    f      the definition's named fields, decoded (pc-relative ones are absolute word addresses)
    ops    the operands as printed ("r5", "0x10", "configured", ...)
    a,b,c  the three five-bit selector fields at bits 20..16, 15..11 and 10..6
    """
    __slots__ = ("word", "pc", "m", "decs", "f", "ops", "a", "b", "c", "imm", "target", "text", "fn", "x")

    def __repr__(self):
        return f"<{self.pc:04x}: {self.word:08x} {self.text}>"


def decode(word, pc):
    """Decode like qeisa's disassembler (default semantic syntax): first candidate by precedence."""
    s = _load()
    ins = Ins()
    ins.word, ins.pc, ins.fn, ins.x, ins.ops = word, pc, None, None, []
    ins.a, ins.b, ins.c, ins.imm = (word >> 16) & 31, (word >> 11) & 31, (word >> 6) & 31, word & 0xFFFF
    ins.decs, ins.f, ins.target = {}, {}, None
    candidates = s["candidates"](word, pc, s["defs"])
    if not candidates:
        ins.m, ins.text = ".word", f".word 0x{word:08x}"
        return ins
    definition, operands = candidates[0]
    ins.f = {field.name: value for field, value in zip(definition.operands, operands)}
    for field, value in zip(definition.operands, operands):
        if field.kind == "pc_relative":
            ins.target = value
    d = s["disasm"]
    chosen = d._semantic_render_candidate(candidates, s["forms"])
    if chosen is not None:
        definition, operands, form = chosen
        ins.f = {field.name: value for field, value in zip(definition.operands, operands)}
        mnemonic, shown, decorators = d.render_semantic_instruction(definition, operands, form, {})
        for item in decorators:
            name, _, value = item.partition("=")
            ins.decs[name] = True if not value else value if value.lstrip("!").startswith("p") or name == "encoding" else int(value, 0)
    else:
        mnemonic = definition.mnemonic
        shown = [d._operand_text(field.kind, value) for field, value in zip(definition.operands, operands)]
        decorators = []
    ins.m, ins.ops = mnemonic.rstrip("?"), shown
    ins.text = f"{mnemonic} {', '.join(shown)}".strip() + (" {" + ", ".join(decorators) + "}" if decorators else "")
    return ins


def assemble(source, org=None):
    """Assemble with the vendored qeisa assembler (the code behind `python3 -m fman_emu._vendor.qe_asm`).

    `source` may be a complete file or just instructions; with `org` given the
    `.arch/.risc/.org` header is added.  Returns (origin, [words]).  One .org only.
    """
    s = _load()
    if org is not None:
        source = f".arch {PROFILE}\n.risc 0\n.org {org:#x}\n" + source
    origin = next(int(line.split()[1], 0) for line in source.splitlines() if line.strip().startswith(".org"))
    data = s["assemble_text"](source, s["defs"], s["forms"])
    return origin, list(struct.unpack(f">{len(data) // 4}I", data))


def assemble_cli(path, out):
    """The command-line assembler in a child process, for cross-checking the in-process path."""
    root = str(Path(__file__).resolve().parent.parent)          # the directory that holds fman_emu/
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [root, os.environ.get("PYTHONPATH")])))
    subprocess.run([sys.executable, "-m", "fman_emu._vendor.qe_asm", str(path), "-o", str(out)], env=env,
                   check=True, capture_output=True)
    data = Path(out).read_bytes()
    return list(struct.unpack(f">{len(data) // 4}I", data))


def disassemble(word, pc=0):
    return decode(word, pc).text
