# Bundled qeisa subset

Origin: the qe-isa-research project (LS1046A FMan / QE ISA research; local repository
`qe-reconstruction/qe-isa-research`, no public remote), commit
`95f666591c713c2beab57336fb1bd928089e22a7`. No licence has been chosen for it; see the
top-level README.

Only what `fman_emu` needs to decode, disassemble and assemble LS1046A FMan instructions is
bundled. Nothing here is microcode: the data describes instruction encodings and their
assembly syntax.

## Code

| File here | Upstream | Changes |
| --- | --- | --- |
| `qeisa/asm.py`, `disasm.py`, `encodings.py`, `semantic.py`, `generated_families.py` | `qeisa/` | none (byte-identical) |
| `qeisa/fman_bfins.py`, `fman_circular_geometry.py` | `qeisa/` | none; used only as reference models by the self-test |
| `qeisa/__init__.py` | `qeisa/__init__.py` | replaced: upstream re-exports the QEF container parser `firmware.py`, which is not bundled |
| `qe_asm.py` | `tools/qe_asm.py` | docstring; relative imports of the bundled package; `--encodings` / `--semantic-forms` default to `isa/` here instead of the working directory |

Not bundled: every other `qeisa` module, the research tools, analyses, captures, corpora,
documentation and campaign data.

## Data (`isa/`)

The nine files the loaders read: `encodings.yaml`, `semantic-syntax.yaml`,
`addition-family.yaml`, `subtraction-family.yaml`, `alu-matrix-families.yaml`,
`scalar-matrix-families.yaml`, `count-matrix-families.yaml`, `shift-rotate-families.yaml`,
`bit-update-families.yaml`. Upstream they also carry the research record behind each
encoding: evidence and counterevidence entries, test vectors, captures, corpus statistics
and prose, some of which cite instruction words and sequences observed in vendor
microcode. That record is removed here; only what the loaders read is kept.

* `encodings.yaml`: per instruction, removed `evidence`, `counterevidence`, `tests`,
  `semantics` and `include_in_instruction_table`; `confidence` (free text, read into the
  definitions but never used for decoding or assembly) is cut to its leading status word
  (`confirmed`, `strong`, `probable`, ...). Removed the top-level `notes`,
  `condition_models`, `high_selector_model` and the profile descriptions.
* Family files: removed the `corpus`, `semantics`, `note`, `rationale`,
  `canonical_encoding`, `carry_input` / `borrow_input`, `test_overrides`,
  `condition_overrides`, `limitations`, `historical_overlaps` and `counterevidence`
  sections. Prose fields the generator copies into fields nobody reads
  (`description`, `family_description`, `equation`, `operation`, `expression`,
  `computation`, `publish_effect`, `predicate_note`, ...) are empty strings, and every
  string inside the `evidence` and `result` sections is empty. The evidence keys and IDs the
  generator looks up remain.
* All files were re-serialised with PyYAML (comments dropped; `mask`, `value`,
  `base_value` and `exact_mask` written in hex).

Instruction IDs and mnemonics are unchanged. Some IDs and mnemonics contain a 32-bit value:
that is the exact encoding the entry matches, not a code sequence.

Check: with the upstream code at that commit, the reduced data and the upstream data
yield identical instruction definitions (every profile, `confidence` excluded), semantic
forms, decorator definitions and generated-family metadata for every instruction.
Decoding and assembling with this copy and with the upstream package give identical
results.
