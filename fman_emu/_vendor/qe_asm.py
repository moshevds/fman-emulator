"""Command-line assembler: `python3 -m fman_emu._vendor.qe_asm SOURCE -o OUTPUT`.

Vendored from qe-isa-research `tools/qe_asm.py` (see README.md in this
directory).  Changes: this docstring, imports that point at the vendored
`qeisa` package, and default `--encodings` / `--semantic-forms` paths that name
the bundled data instead of paths relative to the working directory.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .qeisa.asm import assemble_text
from .qeisa.encodings import (
    ARCHITECTURE_PROFILES,
    DEFAULT_ARCHITECTURE_PROFILE,
    load_instruction_definitions,
)
from .qeisa.semantic import load_semantic_forms

ISA_DIR = Path(__file__).resolve().parent / "isa"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assemble lossless .word-based FMan/QE source")
    parser.add_argument("source", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--encodings", type=Path, default=ISA_DIR / "encodings.yaml")
    parser.add_argument(
        "--syntax",
        choices=("semantic", "encoding"),
        default="semantic",
        help="accepted source syntax (default: semantic decorators plus encoding mnemonics)",
    )
    parser.add_argument(
        "--semantic-forms", type=Path, default=ISA_DIR / "semantic-syntax.yaml"
    )
    parser.add_argument(
        "--profile",
        choices=ARCHITECTURE_PROFILES,
        default=DEFAULT_ARCHITECTURE_PROFILE,
        help="ISA profile (default: LS1046A FMan; legacy-qe enables incompatible older forms)",
    )
    args = parser.parse_args(argv)
    definitions = load_instruction_definitions(args.encodings, profile=args.profile)
    semantic_forms = None
    if args.syntax == "semantic":
        loaded_forms = load_semantic_forms(
            args.semantic_forms,
            load_instruction_definitions(args.encodings, profile="all"),
        )
        selected_ids = {definition.id for definition in definitions}
        semantic_forms = {
            instruction_id: form
            for instruction_id, form in loaded_forms.items()
            if instruction_id in selected_ids
        }
    args.output.write_bytes(
        assemble_text(
            args.source.read_text(encoding="utf-8"), definitions, semantic_forms
        )
    )
    print(args.output, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
