"""Lossless baseline disassembly. Unknown words are never guessed."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping

from .encodings import InstructionDefinition, decode_candidates
from .semantic import SemanticForm, semantic_form_accepts_values


def display_mnemonic(
    instruction_id: str,
    mnemonic: str,
    *,
    mnemonic_set: Mapping[str, str] | None = None,
    fill_as_nop: bool = False,
) -> str:
    """Select a non-canonical mnemonic through exact hypothesis-ID lookup."""

    if fill_as_nop and instruction_id == "HYP-0033":
        return "nop"
    return mnemonic_set.get(instruction_id, mnemonic) if mnemonic_set is not None else mnemonic


def _number(value: int) -> str:
    return f"-0x{-value:x}" if value < 0 else f"0x{value:x}"


def _operand_text(kind: str, value: int) -> str:
    if kind == "register":
        return f"r{value}"
    if kind == "register_or_zero":
        return "zero" if value == 31 else f"r{value}"
    if kind == "register_or_preserve":
        return "preserve" if value == 31 else f"r{value}"
    if kind == "register_pair_start":
        return f"r{value}"
    return _number(value)


def render_semantic_instruction(
    definition: InstructionDefinition,
    operands: tuple[int, ...],
    form: SemanticForm,
    reference_labels: Mapping[int, str],
) -> tuple[str, list[str], list[str]]:
    """Render one decoded definition through its semantic frontend form."""

    fields = {
        field.name: (field, value)
        for field, value in zip(definition.operands, operands, strict=True)
    }
    rendered_operands: list[str] = []
    for semantic_operand in form.operands:
        if semantic_operand.literal is not None:
            rendered_operands.append(semantic_operand.literal)
            continue
        assert semantic_operand.field is not None
        field, value = fields[semantic_operand.field]
        kind = semantic_operand.kind or field.kind
        rendered_operands.append(
            reference_labels.get(value, _number(value))
            if kind == "pc_relative"
            else _operand_text(kind, value)
        )
    rendered_decorators: list[str] = []
    for decorator in form.decorators:
        if not decorator.dynamic:
            rendered_decorators.append(
                decorator.name if decorator.value is None else f"{decorator.name}={decorator.value}"
            )
            continue
        _, value = fields[decorator.field or ""]
        if decorator.format == "predicate":
            prefix = "!" if decorator.negate else ""
            rendered_decorators.append(f"{decorator.name}={prefix}p{value}")
        else:
            rendered_decorators.append(f"{decorator.name}={_number(value)}")
    return form.mnemonic, rendered_operands, rendered_decorators


def _semantic_render_candidate(
    candidates: list[tuple[InstructionDefinition, tuple[int, ...]]],
    semantic_forms: Mapping[str, SemanticForm] | None,
) -> tuple[InstructionDefinition, tuple[int, ...], SemanticForm] | None:
    """Select canonical output syntax without emitting parse-only compatibility forms."""

    if not candidates or not semantic_forms:
        return None
    definition, operands = candidates[0]
    form = semantic_forms.get(definition.id)
    if (
        form is not None
        and not form.parse_only
        and semantic_form_accepts_values(definition, operands, form)
    ):
        return definition, operands, form
    if form is None or not form.parse_only or form.deprecated_replacement is None:
        return None
    for alternative_definition, alternative_operands in candidates[1:]:
        alternative_form = semantic_forms.get(alternative_definition.id)
        if (
            alternative_form is not None
            and not alternative_form.parse_only
            and alternative_form.mnemonic == form.deprecated_replacement
            and semantic_form_accepts_values(
                alternative_definition, alternative_operands, alternative_form
            )
        ):
            return alternative_definition, alternative_operands, alternative_form
    return None


def disassemble_words(
    words: Iterable[int],
    *,
    risc: int = 0,
    origin: int = 0,
    json_output: bool = False,
    definitions: tuple[InstructionDefinition, ...] = (),
    show_confidence: bool = False,
    label_references: bool = False,
    byte_base: int | None = None,
    architecture_profile: str | None = None,
    mnemonic_set: Mapping[str, str] | None = None,
    semantic_forms: Mapping[str, SemanticForm] | None = None,
    fill_as_nop: bool = False,
) -> str:
    values = tuple(words)
    iram_byte_base = origin * 4 if byte_base is None else byte_base
    reference_labels: dict[int, str] = {}
    if label_references:
        for index, word in enumerate(values):
            pc = origin + index
            for definition, operands in decode_candidates(word, pc, definitions):
                for field, decoded in zip(definition.operands, operands, strict=True):
                    if field.kind == "pc_relative" and origin <= decoded < origin + len(values):
                        reference_labels.setdefault(decoded, f"loc_{decoded:08x}")
    if json_output:
        records: list[dict[str, object]] = []
        for index, word in enumerate(values):
            pc = origin + index
            candidates = decode_candidates(word, pc, definitions)
            primary = candidates[0] if candidates else None
            canonical_mnemonic = primary[0].mnemonic if primary else ".word"
            semantic_render = _semantic_render_candidate(candidates, semantic_forms)
            semantic_form = semantic_render[2] if semantic_render is not None else None
            if semantic_render is not None:
                render_definition, render_operands, semantic_form = semantic_render
                mnemonic, rendered_operands, decorators = render_semantic_instruction(
                    render_definition, render_operands, semantic_form, reference_labels
                )
            else:
                mnemonic = display_mnemonic(
                    primary[0].id if primary else "",
                    canonical_mnemonic,
                    mnemonic_set=mnemonic_set,
                    fill_as_nop=fill_as_nop,
                )
                rendered_operands = []
                decorators = []
            record: dict[str, object] = {
                "address_raw": origin + index,
                "address_word": origin + index,
                "address_byte": iram_byte_base + index * 4,
                "word_offset": index,
                "raw_bytes": word.to_bytes(4, "big").hex(),
                "raw_word": f"0x{word:08x}",
                "mnemonic": mnemonic,
                "candidate_kind": primary[0].candidate_kind if primary else "unknown",
                "architectural_status": (primary[0].architectural_status if primary else "unknown"),
                "operands": list(primary[1]) if primary else [],
                "confidence": primary[0].confidence if primary else "unknown",
                "hypothesis_id": primary[0].id if primary else None,
                "execution_domain": primary[0].execution_domain if primary else None,
                "synchronization_domain": (primary[0].synchronization_domain if primary else None),
                "arm_participation": primary[0].arm_participation if primary else None,
                "alternatives": [candidate[0].id for candidate in candidates[1:]],
            }
            if semantic_form is not None:
                record["semantic_operands"] = rendered_operands
                record["decorators"] = decorators
            if mnemonic != canonical_mnemonic:
                record["canonical_mnemonic"] = canonical_mnemonic
            records.append(record)
        return (
            json.dumps(
                {
                    "architecture_profile": architecture_profile,
                    "risc": risc,
                    "origin_raw": origin,
                    "origin_word": origin,
                    "iram_byte_base": iram_byte_base,
                    "instructions": records,
                },
                indent=2,
            )
            + "\n"
        )

    lines = []
    if architecture_profile is not None:
        lines.append(f".arch {architecture_profile}")
    lines.extend(
        [
            f".risc {risc}",
            f".org 0x{origin:x}    ; instruction-word address",
            f"; I-RAM byte base 0x{iram_byte_base:x}",
            "",
            f"risc{risc}_base:",
        ]
    )
    for index, word in enumerate(values):
        pc = origin + index
        if pc in reference_labels and pc != origin:
            lines.append(f"{reference_labels[pc]}:")
        candidates = decode_candidates(word, pc, definitions)
        if not candidates:
            lines.append(f"    .word 0x{word:08x}")
            continue
        definition, operands = candidates[0]
        semantic_render = _semantic_render_candidate(candidates, semantic_forms)
        semantic_form = semantic_render[2] if semantic_render is not None else None
        if semantic_render is not None:
            render_definition, render_operands, semantic_form = semantic_render
            mnemonic, rendered_items, decorators = render_semantic_instruction(
                render_definition, render_operands, semantic_form, reference_labels
            )
        else:
            mnemonic = display_mnemonic(
                definition.id,
                definition.mnemonic,
                mnemonic_set=mnemonic_set,
                fill_as_nop=fill_as_nop,
            )
            rendered_items = [
                reference_labels.get(value, _number(value))
                if field.kind == "pc_relative"
                else _operand_text(field.kind, value)
                for field, value in zip(definition.operands, operands, strict=True)
            ]
            decorators = []
        rendered = ", ".join(rendered_items)
        if decorators:
            rendered += (" " if rendered else "") + "{" + ", ".join(decorators) + "}"
        suffix = (
            f"    ; {definition.confidence}, evidence {definition.id}" if show_confidence else ""
        )
        lines.append(f"    {mnemonic:<8} {rendered}{suffix}".rstrip())
    return "\n".join(lines) + "\n"
