"""Validation for evidence-backed decoder masks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .generated_families import (
    GENERATED_FAMILY_PREFIXES,
    generated_instruction_entries,
    generated_semantic_metadata,
)


class EncodingConflictError(ValueError):
    pass


class MnemonicSetError(ValueError):
    pass


DEFAULT_ARCHITECTURE_PROFILE = "ls1046a-fman"
ARCHITECTURE_PROFILES = (DEFAULT_ARCHITECTURE_PROFILE, "legacy-qe", "all")
ARCHITECTURAL_STATUSES = ("instruction", "reserved-encoding")


@dataclass(frozen=True, slots=True)
class OperandField:
    name: str
    kind: str
    lsb: int
    width: int
    scale: int
    signed: bool
    bias: int
    modulus: int | None

    def raw(self, word: int) -> int:
        return (word >> self.lsb) & ((1 << self.width) - 1)

    def signed_value(self, word: int) -> int:
        value = self.raw(word)
        if self.signed and value & (1 << (self.width - 1)):
            value -= 1 << self.width
        return value

    def decode(self, word: int, pc: int) -> int:
        value = self.signed_value(word) * self.scale + self.bias
        if self.kind != "pc_relative":
            return value
        target = pc + value
        return target % self.modulus if self.modulus is not None else target


@dataclass(frozen=True, slots=True)
class PcRelativeEncoding:
    target_operand: str
    lsb: int
    width: int
    form: str

    def matches(self, word: int, pc: int, operands: tuple[OperandField, ...]) -> bool:
        field = (word >> self.lsb) & ((1 << self.width) - 1)
        if field & (1 << (self.width - 1)):
            field -= 1 << self.width
        operand = next(item for item in operands if item.name == self.target_operand)
        is_linear = field == operand.decode(word, pc) - pc
        return is_linear if self.form == "linear" else not is_linear


@dataclass(frozen=True, slots=True)
class FieldPairAllowlist:
    first: OperandField
    second: OperandField
    pairs: frozenset[tuple[int, int]]

    def matches(self, word: int) -> bool:
        return (self.first.raw(word), self.second.raw(word)) in self.pairs


@dataclass(frozen=True, slots=True)
class InstructionDefinition:
    id: str
    mnemonic: str
    candidate_kind: str
    confidence: str
    mask: int
    value: int
    length_bits: int
    operands: tuple[OperandField, ...]
    precedence: int
    profiles: tuple[str, ...]
    execution_domain: str | None = None
    synchronization_domain: str | None = None
    arm_participation: str | None = None
    pc_relative_encoding: PcRelativeEncoding | None = None
    architectural_status: str = "instruction"
    field_pair_allowlist: FieldPairAllowlist | None = None

    def matches(self, word: int, pc: int | None = None) -> bool:
        if word & self.mask != self.value:
            return False
        if self.field_pair_allowlist is not None and not self.field_pair_allowlist.matches(word):
            return False
        if self.pc_relative_encoding is None:
            return True
        return pc is not None and self.pc_relative_encoding.matches(word, pc, self.operands)


def load_instruction_definitions(
    path: Path, *, profile: str | None = None
) -> tuple[InstructionDefinition, ...]:
    if profile not in (None, *ARCHITECTURE_PROFILES):
        raise ValueError(f"unknown architecture profile: {profile}")
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw_instructions: list[dict[str, Any]] = [
        *document.get("instructions", []),
        *generated_instruction_entries(path),
    ]
    validate_instruction_masks(raw_instructions)
    definitions: list[InstructionDefinition] = []
    for item in raw_instructions:
        profiles = tuple(item.get("profiles", ()))
        if profile not in (None, "all") and profiles and profile not in profiles:
            continue
        operands: list[OperandField] = []
        for operand in item.get("operands", []):
            field = operand["field"]
            operands.append(
                OperandField(
                    name=operand["name"],
                    kind=operand["kind"],
                    lsb=field["lsb"],
                    width=field["width"],
                    scale=operand.get("scale", 1),
                    signed=operand.get("signed", False),
                    bias=operand.get("bias", 0),
                    modulus=operand.get("modulus"),
                )
            )
        raw_pc_encoding = item.get("pc_relative_encoding")
        pc_encoding = (
            PcRelativeEncoding(
                target_operand=raw_pc_encoding["target_operand"],
                lsb=raw_pc_encoding["field"]["lsb"],
                width=raw_pc_encoding["field"]["width"],
                form=raw_pc_encoding["form"],
            )
            if raw_pc_encoding is not None
            else None
        )
        raw_allowlist = item.get("field_pair_allowlist")
        allowlist = None
        if raw_allowlist is not None:
            by_name = {operand.name: operand for operand in operands}
            first_name, second_name = raw_allowlist["fields"]
            first = by_name[first_name]
            second = by_name[second_name]
            pairs = frozenset(tuple(pair) for pair in raw_allowlist["pairs"])
            if not pairs or any(
                len(pair) != 2
                or not 0 <= pair[0] < (1 << first.width)
                or not 0 <= pair[1] < (1 << second.width)
                for pair in pairs
            ):
                raise EncodingConflictError(f"{item['id']}: invalid field-pair allowlist")
            allowlist = FieldPairAllowlist(first, second, pairs)
        definitions.append(
            InstructionDefinition(
                id=item["id"],
                mnemonic=item["mnemonic"],
                candidate_kind=item.get("candidate_kind", "instruction"),
                confidence=item["confidence"],
                mask=int(item["mask"], 0) if isinstance(item["mask"], str) else item["mask"],
                value=int(item["value"], 0) if isinstance(item["value"], str) else item["value"],
                length_bits=item["length_bits"],
                operands=tuple(operands),
                precedence=item.get("precedence", 0),
                profiles=profiles,
                execution_domain=item.get("execution_domain"),
                synchronization_domain=item.get("synchronization_domain"),
                arm_participation=item.get("arm_participation"),
                pc_relative_encoding=pc_encoding,
                architectural_status=item.get("architectural_status", "instruction"),
                field_pair_allowlist=allowlist,
            )
        )
    return tuple(sorted(definitions, key=lambda item: item.precedence, reverse=True))


def decode_candidates(
    word: int, pc: int, definitions: tuple[InstructionDefinition, ...]
) -> list[tuple[InstructionDefinition, tuple[int, ...]]]:
    return [
        (definition, tuple(operand.decode(word, pc) for operand in definition.operands))
        for definition in definitions
        if definition.matches(word, pc)
    ]


def load_mnemonic_set(path: Path, definitions: tuple[InstructionDefinition, ...]) -> dict[str, str]:
    """Load an explicit display-mnemonic set keyed by hypothesis ID."""

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise MnemonicSetError(f"{path}: unsupported mnemonic-set schema")
    known_ids = {definition.id for definition in definitions}
    aliases: dict[str, str] = {}
    entries = document.get("mnemonics", [])
    if not isinstance(entries, list):
        raise MnemonicSetError(f"{path}: mnemonics must be a list")
    for item in entries:
        if not isinstance(item, dict):
            raise MnemonicSetError(f"{path}: each mnemonic entry must be an object")
        instruction_id = item.get("id")
        mnemonic = item.get("mnemonic")
        if not isinstance(instruction_id, str) or not isinstance(mnemonic, str) or not mnemonic:
            raise MnemonicSetError(f"{path}: mnemonic entries require non-empty id and mnemonic")
        if instruction_id in aliases:
            raise MnemonicSetError(f"{path}: duplicate mnemonic for {instruction_id}")
        if instruction_id not in known_ids:
            raise MnemonicSetError(f"{path}: unknown instruction ID {instruction_id}")
        aliases[instruction_id] = mnemonic
    for definition in definitions:
        if definition.id.startswith(tuple(f"{prefix}-" for prefix in GENERATED_FAMILY_PREFIXES)):
            metadata = generated_semantic_metadata(
                definition.id, encodings_path=path.with_name("encodings.yaml")
            )
            assert metadata is not None
            aliases.setdefault(definition.id, str(metadata["mnemonic"]))
    return aliases


def masks_overlap(left_mask: int, left_value: int, right_mask: int, right_value: int) -> bool:
    shared = left_mask & right_mask
    return ((left_value ^ right_value) & shared) == 0


def validate_instruction_masks(instructions: list[dict[str, Any]]) -> None:
    for index, left in enumerate(instructions):
        architectural_status = left.get("architectural_status", "instruction")
        if architectural_status not in ARCHITECTURAL_STATUSES:
            raise EncodingConflictError(
                f"{left['id']}: unknown architectural_status {architectural_status!r}"
            )
        left_mask = int(left["mask"], 0) if isinstance(left["mask"], str) else left["mask"]
        left_value = int(left["value"], 0) if isinstance(left["value"], str) else left["value"]
        if left_value & ~left_mask:
            raise EncodingConflictError(f"{left['id']}: value sets bits outside mask")
        operand_mask = 0
        for operand in left.get("operands", []):
            field = operand["field"]
            lsb, width = int(field["lsb"]), int(field["width"])
            if lsb < 0 or width <= 0 or lsb + width > 32:
                raise EncodingConflictError(f"{left['id']}: operand field is outside 32 bits")
            field_mask = ((1 << width) - 1) << lsb
            if field_mask & left_mask:
                raise EncodingConflictError(
                    f"{left['id']}: operand {operand['name']} overlaps fixed mask"
                )
            if field_mask & operand_mask:
                raise EncodingConflictError(
                    f"{left['id']}: operand {operand['name']} overlaps another operand"
                )
            modulus = operand.get("modulus")
            if modulus is not None and (
                operand.get("kind") != "pc_relative" or not isinstance(modulus, int) or modulus <= 0
            ):
                raise EncodingConflictError(
                    f"{left['id']}: operand {operand['name']} has invalid modulus"
                )
            operand_mask |= field_mask
        pc_encoding = left.get("pc_relative_encoding")
        if pc_encoding is not None:
            field = pc_encoding.get("field", {}) if isinstance(pc_encoding, dict) else {}
            constraint_lsb = field.get("lsb")
            constraint_width = field.get("width")
            target_operand = (
                pc_encoding.get("target_operand") if isinstance(pc_encoding, dict) else None
            )
            target_names = {
                operand["name"]
                for operand in left.get("operands", [])
                if operand.get("kind") == "pc_relative"
            }
            if (
                not isinstance(pc_encoding, dict)
                or not isinstance(constraint_lsb, int)
                or not isinstance(constraint_width, int)
                or constraint_lsb < 0
                or constraint_width <= 0
                or constraint_lsb + constraint_width > 32
                or target_operand not in target_names
                or pc_encoding.get("form") not in {"linear", "alternate"}
            ):
                raise EncodingConflictError(
                    f"{left['id']}: invalid PC-relative encoding constraint"
                )
        for right in instructions[index + 1 :]:
            right_mask = int(right["mask"], 0) if isinstance(right["mask"], str) else right["mask"]
            right_value = (
                int(right["value"], 0) if isinstance(right["value"], str) else right["value"]
            )
            if not masks_overlap(left_mask, left_value, right_mask, right_value):
                continue
            left_precedence = left.get("precedence")
            right_precedence = right.get("precedence")
            if (
                left_precedence is None
                or right_precedence is None
                or left_precedence == right_precedence
            ):
                raise EncodingConflictError(
                    f"overlapping masks without distinct precedence: {left['id']} and {right['id']}"
                )
