"""Semantic assembly syntax layered over evidence-backed encoding records."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .encodings import InstructionDefinition
from .generated_families import generated_semantic_metadata


class SemanticSyntaxError(ValueError):
    pass


_DECORATOR_ORDER = {
    "discard": 0,
    "publish": 1,
    "carry": 2,
    "borrow": 2,
    "increment": 2,
    "decrement": 2,
    "reverse": 3,
    "swap8": 4,
    "bswap": 4,
    "bitrev8": 4,
    "rol16": 4,
    "lsr8": 4,
    "lsr1": 4,
    "low16": 4,
    "increment_count": 4,
    "descending": 4,
    "select": 5,
    "next": 6,
    "encoding": 7,
}


def _decorator_order(name: str) -> int:
    return 8 if name.startswith("raw") else _DECORATOR_ORDER.get(name, 9)


@dataclass(frozen=True, slots=True)
class SemanticDecoratorDefinition:
    name: str
    kind: str
    description: str
    prefix: bool = False


def load_decorator_definitions(path: Path) -> tuple[SemanticDecoratorDefinition, ...]:
    """Load the source-backed semantic and exact-encoding decorator glossary."""

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise SemanticSyntaxError(f"{path}: unsupported semantic-syntax schema")
    result: list[SemanticDecoratorDefinition] = []
    seen: set[str] = set()
    for raw in document.get("decorators", []):
        if not isinstance(raw, dict):
            raise SemanticSyntaxError(f"{path}: each decorator definition must be an object")
        name = raw.get("name")
        kind = raw.get("kind")
        description = raw.get("description")
        prefix = bool(raw.get("prefix", False))
        if not isinstance(name, str) or not name:
            raise SemanticSyntaxError(f"{path}: decorator definition requires a name")
        if kind not in {"behavior", "encoding"}:
            raise SemanticSyntaxError(f"{path}: decorator {name!r} has invalid kind")
        if not isinstance(description, str) or not description.strip():
            raise SemanticSyntaxError(f"{path}: decorator {name!r} requires a description")
        if name in seen:
            raise SemanticSyntaxError(f"{path}: duplicate decorator definition {name!r}")
        seen.add(name)
        result.append(SemanticDecoratorDefinition(name, kind, description.strip(), prefix))
    return tuple(result)


@dataclass(frozen=True, slots=True)
class SemanticOperand:
    field: str | None = None
    kind: str | None = None
    literal: str | None = None


@dataclass(frozen=True, slots=True)
class SemanticDecorator:
    name: str
    field: str | None = None
    format: str = "flag"
    negate: bool = False
    value: str | None = None
    minimum: int | None = None
    maximum: int | None = None

    @property
    def dynamic(self) -> bool:
        return self.field is not None


@dataclass(frozen=True, slots=True)
class SemanticForm:
    instruction_id: str
    mnemonic: str
    operands: tuple[SemanticOperand, ...]
    decorators: tuple[SemanticDecorator, ...]
    document: bool = True
    parse_only: bool = False
    deprecated_replacement: str | None = None
    deprecation_message: str | None = None

    @property
    def decorator_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.decorators)


def load_semantic_forms(
    path: Path, definitions: tuple[InstructionDefinition, ...]
) -> dict[str, SemanticForm]:
    """Load and validate exact instruction-ID to semantic-syntax mappings."""

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise SemanticSyntaxError(f"{path}: unsupported semantic-syntax schema")
    decorator_definitions = load_decorator_definitions(path)

    raw_deprecated = document.get("deprecated_mnemonics", {})
    if not isinstance(raw_deprecated, dict):
        raise SemanticSyntaxError(f"{path}: deprecated_mnemonics must be an object")
    deprecated_mnemonics: dict[str, tuple[str, str]] = {}
    for old_mnemonic, raw in raw_deprecated.items():
        if not isinstance(old_mnemonic, str) or not old_mnemonic:
            raise SemanticSyntaxError(f"{path}: deprecated mnemonic names must be non-empty")
        if not isinstance(raw, dict):
            raise SemanticSyntaxError(
                f"{path}: deprecated mnemonic {old_mnemonic!r} must be an object"
            )
        replacement = raw.get("replacement")
        message = raw.get("message")
        if not isinstance(replacement, str) or not replacement:
            raise SemanticSyntaxError(
                f"{path}: deprecated mnemonic {old_mnemonic!r} requires a replacement"
            )
        if not isinstance(message, str) or not message.strip():
            raise SemanticSyntaxError(
                f"{path}: deprecated mnemonic {old_mnemonic!r} requires a message"
            )
        deprecated_mnemonics[old_mnemonic] = (replacement, message.strip())

    def decorator_is_documented(name: str) -> bool:
        return any(
            name.startswith(item.name) if item.prefix else name == item.name
            for item in decorator_definitions
        )

    by_id = {definition.id: definition for definition in definitions}
    encodings_path = path.with_name("encodings.yaml")
    dominant_by_encoding: dict[tuple[int, int], InstructionDefinition] = {}
    for definition in definitions:
        key = (definition.mask, definition.value)
        previous = dominant_by_encoding.get(key)
        if previous is None or definition.precedence > previous.precedence:
            dominant_by_encoding[key] = definition
    result: dict[str, SemanticForm] = {}
    for raw in document.get("forms", []):
        if not isinstance(raw, dict):
            raise SemanticSyntaxError(f"{path}: each form must be an object")
        instruction_id = raw.get("id")
        mnemonic = raw.get("mnemonic")
        if not isinstance(instruction_id, str) or instruction_id not in by_id:
            raise SemanticSyntaxError(f"{path}: unknown instruction ID {instruction_id!r}")
        if instruction_id in result:
            raise SemanticSyntaxError(f"{path}: duplicate form for {instruction_id}")
        if not isinstance(mnemonic, str) or not mnemonic:
            raise SemanticSyntaxError(f"{path}: {instruction_id} requires a mnemonic")

        definition = by_id[instruction_id]
        dominant = dominant_by_encoding[(definition.mask, definition.value)]
        superseded_by_generated = (
            dominant.id != definition.id
            and generated_semantic_metadata(
                dominant.id, encodings_path=encodings_path
            ) is not None
            and mnemonic not in deprecated_mnemonics
        )
        fields = {operand.name for operand in definition.operands}
        operands: list[SemanticOperand] = []
        consumed: set[str] = set()
        for item in raw.get("operands", []):
            if isinstance(item, str):
                operand = SemanticOperand(item)
            elif isinstance(item, dict):
                field = str(item["field"]) if "field" in item else None
                literal = str(item["literal"]) if "literal" in item else None
                operand = SemanticOperand(field, item.get("kind"), literal)
            else:
                raise SemanticSyntaxError(f"{path}: invalid operand in {instruction_id}")
            if (operand.field is None) == (operand.literal is None):
                raise SemanticSyntaxError(
                    f"{path}: operands in {instruction_id} require exactly one of field/literal"
                )
            if operand.field is not None and (
                operand.field not in fields or operand.field in consumed
            ):
                raise SemanticSyntaxError(
                    f"{path}: invalid or duplicate field {operand.field!r} in {instruction_id}"
                )
            operands.append(operand)
            if operand.field is not None:
                consumed.add(operand.field)

        decorators: list[SemanticDecorator] = []
        decorator_names: set[str] = set()
        for item in raw.get("decorators", []):
            if isinstance(item, str):
                decorator = SemanticDecorator(item)
            elif isinstance(item, dict):
                field = str(item["field"]) if "field" in item else None
                decorator = SemanticDecorator(
                    name=str(item.get("name")),
                    field=field,
                    format=str(item.get("format", "integer" if field else "flag")),
                    negate=bool(item.get("negate", False)),
                    value=str(item["value"]) if "value" in item else None,
                    minimum=int(item["min"]) if "min" in item else None,
                    maximum=int(item["max"]) if "max" in item else None,
                )
            else:
                raise SemanticSyntaxError(f"{path}: invalid decorator in {instruction_id}")
            if not decorator.name or decorator.name in decorator_names:
                raise SemanticSyntaxError(
                    f"{path}: invalid or duplicate decorator {decorator.name!r} in {instruction_id}"
                )
            if not decorator_is_documented(decorator.name):
                raise SemanticSyntaxError(
                    f"{path}: undocumented decorator {decorator.name!r} in {instruction_id}"
                )
            if decorator.dynamic:
                if decorator.value is not None:
                    raise SemanticSyntaxError(
                        f"{path}: dynamic decorator {decorator.name!r} cannot fix a value"
                    )
                if decorator.field not in fields or decorator.field in consumed:
                    raise SemanticSyntaxError(
                        f"{path}: invalid decorator field {decorator.field!r} in {instruction_id}"
                    )
                if decorator.format not in {"integer", "predicate"}:
                    raise SemanticSyntaxError(
                        f"{path}: unsupported decorator format in {instruction_id}"
                    )
                if (
                    decorator.minimum is not None
                    and decorator.maximum is not None
                    and decorator.minimum > decorator.maximum
                ):
                    raise SemanticSyntaxError(
                        f"{path}: invalid decorator range in {instruction_id}"
                    )
                consumed.add(decorator.field)
            elif (
                decorator.format != "flag"
                or decorator.negate
                or decorator.minimum is not None
                or decorator.maximum is not None
            ):
                raise SemanticSyntaxError(
                    f"{path}: fixed decorator {decorator.name!r} must be a flag"
                )
            decorators.append(decorator)
            decorator_names.add(decorator.name)

        order = [_decorator_order(decorator.name) for decorator in decorators]
        if order != sorted(order):
            raise SemanticSyntaxError(
                f"{path}: decorators for {instruction_id} are not in canonical order"
            )

        if consumed != fields:
            missing = ", ".join(sorted(fields - consumed))
            raise SemanticSyntaxError(
                f"{path}: {instruction_id} leaves encoding fields unmapped: {missing}"
            )
        deprecated = deprecated_mnemonics.get(mnemonic)
        if not superseded_by_generated:
            # Generated family definitions replace older one-off semantic
            # descriptions of the same exact word pattern. Deprecated syntax
            # remains parse-only so existing listings can still be assembled.
            result[instruction_id] = SemanticForm(
                instruction_id=instruction_id,
                mnemonic=mnemonic,
                operands=tuple(operands),
                decorators=tuple(decorators),
                document=bool(raw.get("document", True)),
                parse_only=deprecated is not None,
                deprecated_replacement=deprecated[0] if deprecated is not None else None,
                deprecation_message=deprecated[1] if deprecated is not None else None,
            )
    for definition in definitions:
        metadata = generated_semantic_metadata(
            definition.id, encodings_path=encodings_path
        )
        if metadata is None:
            continue
        writes = bool(metadata["writes"])
        decorators = []
        if not writes:
            decorators.append(SemanticDecorator("discard"))
        if metadata["publishes"]:
            decorators.append(SemanticDecorator("publish"))
        raw_fixed_decorators = metadata.get("fixed_decorators", ())
        if not isinstance(raw_fixed_decorators, tuple):
            raise SemanticSyntaxError("generated fixed decorators must be a tuple")
        for fixed_decorator in raw_fixed_decorators:
            decorators.append(SemanticDecorator(str(fixed_decorator)))
        if metadata.get("predicate_input"):
            decorators.append(SemanticDecorator(str(metadata["predicate_decorator"])))
        elif metadata.get("literal_one"):
            decorators.append(SemanticDecorator(str(metadata["literal_decorator"])))
        if metadata["transform"] is not None:
            decorators.append(SemanticDecorator(str(metadata["transform"])))
        family_format = str(metadata.get("format", "register_arithmetic"))
        polarity = metadata.get("polarity")
        if polarity is not None:
            conditional_low_bank = family_format in {"conditional_scalar", "conditional_bitset"}
            decorators.append(
                SemanticDecorator(
                    "select",
                    field=str(metadata.get("predicate_field", "predicate")),
                    format="predicate",
                    negate=polarity == "clear",
                    minimum=0 if conditional_low_bank else None,
                    maximum=15 if conditional_low_bank else None,
                )
            )
        if metadata.get("alias_one"):
            decorators.append(
                SemanticDecorator("encoding", value="constant-one-bit")
            )
        encoding_alias = metadata.get("encoding_alias")
        if encoding_alias is not None:
            decorators.append(
                SemanticDecorator("encoding", value=str(encoding_alias))
            )
        if not writes and family_format in {
            "register_arithmetic",
            "conditional_add",
            "conditional_select",
        }:
            decorators.append(
                SemanticDecorator(
                    "raw10_6", field="auxiliary", format="integer"
                )
            )
        if not writes and family_format == "lane8":
            decorators.append(
                SemanticDecorator(
                    "raw15_11", field="auxiliary", format="integer"
                )
            )
        if not writes and family_format in {
            "scalar_logical", "conditional_scalar", "conditional_bitset",
        }:
            decorators.append(
                SemanticDecorator(
                    "raw10_6", field=str(metadata["result_field"]), format="integer"
                )
            )
        generated_operands: tuple[SemanticOperand, ...]
        if family_format == "register_arithmetic":
            generated_operands = (
                SemanticOperand("destination"),
                SemanticOperand("left"),
                SemanticOperand("right"),
            ) if writes else (SemanticOperand("left"), SemanticOperand("right"))
        elif family_format == "immediate16":
            generated_operands = (
                SemanticOperand("register"),
                SemanticOperand("immediate_u16"),
            )
        elif family_format == "lane8":
            generated_operands = (
                (
                    SemanticOperand("destination"),
                    SemanticOperand("source"),
                    SemanticOperand("lane"),
                    SemanticOperand("immediate_u8"),
                )
                if writes
                else (
                    SemanticOperand("source"),
                    SemanticOperand("lane"),
                    SemanticOperand("immediate_u8"),
                )
            )
        elif family_format == "conditional_add":
            generated_operands = (
                (
                    SemanticOperand("destination"),
                    SemanticOperand("left"),
                    SemanticOperand("right"),
                )
                if writes
                else (SemanticOperand("left"), SemanticOperand("right"))
            )
        elif family_format == "conditional_select":
            generated_operands = (
                (
                    SemanticOperand("destination"),
                    SemanticOperand("true_source"),
                    SemanticOperand("false_source"),
                )
                if writes
                else (
                    SemanticOperand("true_source"),
                    SemanticOperand("false_source"),
                )
            )
        elif family_format in {"scalar_logical", "conditional_scalar"}:
            left_field = str(metadata["left_field"])
            right_field = str(metadata["right_field"])
            result_field = str(metadata["result_field"])
            generated_operands = (
                (
                    SemanticOperand(result_field, kind="register"),
                    SemanticOperand(left_field, kind="register"),
                    SemanticOperand(right_field, kind="register"),
                )
                if writes else (
                    SemanticOperand(left_field, kind="register"),
                    SemanticOperand(right_field, kind="register"),
                )
            )
        elif family_format == "conditional_bitset":
            left_field = str(metadata["left_field"])
            index_field = str(metadata["right_field"])
            result_field = str(metadata["result_field"])
            generated_operands = (
                (
                    SemanticOperand(result_field, kind="register"),
                    SemanticOperand(left_field, kind="register"),
                    SemanticOperand(index_field, kind="immediate"),
                )
                if writes else (
                    SemanticOperand(left_field, kind="register"),
                    SemanticOperand(index_field, kind="immediate"),
                )
            )
        else:
            raise SemanticSyntaxError(
                f"unknown generated-family format {family_format!r}"
            )
        result[definition.id] = SemanticForm(
            instruction_id=definition.id,
            mnemonic=str(metadata["mnemonic"]),
            operands=generated_operands,
            decorators=tuple(decorators),
            document=not bool(metadata.get("alias_one") or encoding_alias),
        )
    for mnemonic, (replacement, _message) in deprecated_mnemonics.items():
        if not any(form.mnemonic == mnemonic for form in result.values()):
            raise SemanticSyntaxError(
                f"{path}: deprecated mnemonic {mnemonic!r} has no semantic forms"
            )
        if not any(
            form.mnemonic == replacement and not form.parse_only for form in result.values()
        ):
            raise SemanticSyntaxError(
                f"{path}: replacement mnemonic {replacement!r} has no canonical forms"
            )
    return result


def semantic_form_accepts_values(
    definition: InstructionDefinition,
    operands: tuple[int, ...],
    form: SemanticForm,
) -> bool:
    """Return whether decoded dynamic fields lie inside the admitted frontend range."""

    fields = {
        field.name: value
        for field, value in zip(definition.operands, operands, strict=True)
    }
    for decorator in form.decorators:
        if not decorator.dynamic:
            continue
        value = fields[decorator.field or ""]
        if decorator.minimum is not None and value < decorator.minimum:
            return False
        if decorator.maximum is not None and value > decorator.maximum:
            return False
    return True


def available_decorator_forms(forms: dict[str, SemanticForm], mnemonic: str) -> tuple[str, ...]:
    """Return stable, unique decorator signatures for one semantic operation."""

    signatures = {
        decorator_signature(form, placeholders=True)
        for form in forms.values()
        if form.document and not form.parse_only and form.mnemonic == mnemonic
    }
    return tuple(sorted(signatures, key=lambda item: (item != "none", item)))


def decorator_signature(form: SemanticForm, *, placeholders: bool = False) -> str:
    if not form.decorators:
        return "none"
    items: list[str] = []
    for decorator in form.decorators:
        if not decorator.dynamic:
            items.append(
                decorator.name if decorator.value is None else f"{decorator.name}={decorator.value}"
            )
        elif decorator.format == "predicate":
            value = "!pN" if decorator.negate else "pN"
            items.append(f"{decorator.name}={value}")
        else:
            items.append(f"{decorator.name}=N" if placeholders else decorator.name)
    return "{" + ", ".join(items) + "}"
