"""Minimal lossless assembler for directives emitted by the baseline disassembler."""

from __future__ import annotations

import ast
import re
import struct
import warnings
from collections.abc import Mapping

from .encodings import InstructionDefinition
from .semantic import SemanticDecorator, SemanticForm, SemanticOperand


class AssemblyError(ValueError):
    pass


class DeprecatedAssemblySyntaxWarning(UserWarning):
    """An accepted source spelling is retained only for input compatibility."""


_LABEL = re.compile(r"^[A-Za-z_.$][A-Za-z0-9_.$]*$")
_DECORATORS = re.compile(r"^(.*?)(?:\s*\{([^{}]*)\})?$")
_ASSEMBLER_ALIASES = {"nop": "HYP-0033"}


def _split_decorators(argument: str) -> tuple[str, tuple[tuple[str, str | None], ...]]:
    match = _DECORATORS.fullmatch(argument.strip())
    if match is None:
        raise AssemblyError("malformed decorator block")
    body, raw_decorators = match.groups()
    if raw_decorators is None:
        return body.strip(), ()
    parsed: list[tuple[str, str | None]] = []
    names: set[str] = set()
    for item in raw_decorators.split(","):
        token = item.strip()
        if not token:
            raise AssemblyError("empty decorator")
        name, separator, value = token.partition("=")
        name = name.strip().lower()
        if not _LABEL.fullmatch(name) or name in names:
            raise AssemblyError(f"invalid or duplicate decorator {name!r}")
        parsed.append((name, value.strip().lower() if separator else None))
        names.add(name)
    return body.strip(), tuple(parsed)


def _decorator_field_value(expected: SemanticDecorator, supplied: str | None) -> str | None:
    if not expected.dynamic:
        required = expected.value.lower() if expected.value is not None else None
        return "" if supplied == required else None
    if supplied is None:
        return None
    if expected.format == "predicate":
        match = re.fullmatch(r"(!?)p(\d+)", supplied)
        if match is None or bool(match.group(1)) != expected.negate:
            return None
        value = int(match.group(2))
        if expected.minimum is not None and value < expected.minimum:
            return None
        if expected.maximum is not None and value > expected.maximum:
            return None
        return str(value)
    try:
        value = int(supplied, 0)
    except ValueError:
        return supplied
    if expected.minimum is not None and value < expected.minimum:
        return None
    if expected.maximum is not None and value > expected.maximum:
        return None
    return supplied


def _semantic_match(
    mnemonic: str,
    supplied: tuple[tuple[str, str | None], ...],
    forms: Mapping[str, SemanticForm],
    arguments: tuple[str, ...] | None = None,
) -> tuple[SemanticForm, dict[str, str]] | None:
    supplied_map = dict(supplied)
    matches: list[tuple[SemanticForm, dict[str, str]]] = []
    for form in forms.values():
        if form.mnemonic != mnemonic or set(form.decorator_names) != set(supplied_map):
            continue
        if arguments is not None:
            if len(arguments) != len(form.operands):
                continue
            if any(
                not _semantic_operand_matches(operand, argument)
                for operand, argument in zip(form.operands, arguments, strict=True)
            ):
                continue
        encoded_fields: dict[str, str] = {}
        for decorator in form.decorators:
            value = _decorator_field_value(decorator, supplied_map[decorator.name])
            if value is None:
                break
            if decorator.field is not None:
                encoded_fields[decorator.field] = value
        else:
            matches.append((form, encoded_fields))
    if len(matches) > 1:
        literal_counts = [
            sum(operand.literal is not None for operand in form.operands)
            for form, _ in matches
        ]
        most_literals = max(literal_counts)
        specific = [
            match
            for match, count in zip(matches, literal_counts, strict=True)
            if count == most_literals
        ]
        if len(specific) == 1:
            return specific[0]
        raise AssemblyError(
            f"ambiguous semantic form {mnemonic} with decorators "
            + ", ".join(name for name, _ in supplied)
        )
    return matches[0] if matches else None


def _argument_list(argument: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in argument.split(",") if item.strip())


def _literal_matches(expected: str, supplied: str) -> bool:
    if re.fullmatch(r"[rR]\d+", expected):
        return supplied.lower() == expected.lower()
    try:
        return _expression(supplied, {}) == int(expected, 0)
    except (AssemblyError, SyntaxError, ValueError):
        return supplied.lower() == expected.lower()


def _semantic_operand_matches(operand: SemanticOperand, supplied: str) -> bool:
    if operand.literal is not None:
        return _literal_matches(operand.literal, supplied)
    is_register = re.fullmatch(r"[rR]\d+", supplied) is not None
    if operand.kind in {
        "register",
        "register_or_zero",
        "register_or_preserve",
        "register_pair_start",
    }:
        return is_register or supplied.lower() in {"zero", "preserve"}
    if operand.kind in {"immediate", "raw", "bit_index"}:
        return not is_register
    return True


def _expression(text: str, labels: dict[str, int]) -> int:
    stripped = text.strip()
    try:
        return int(stripped, 0)
    except ValueError:
        pass
    if stripped in labels:
        return labels[stripped]

    rendered = text
    for label, value in sorted(labels.items(), key=lambda item: -len(item[0])):
        rendered = re.sub(
            rf"(?<![A-Za-z0-9_.$]){re.escape(label)}(?![A-Za-z0-9_.$])",
            str(value),
            rendered,
        )
    node = ast.parse(rendered, mode="eval")

    def evaluate(item: ast.AST) -> int:
        if isinstance(item, ast.Expression):
            return evaluate(item.body)
        if isinstance(item, ast.Constant) and isinstance(item.value, int):
            return item.value
        if isinstance(item, ast.Name) and item.id in labels:
            return labels[item.id]
        if isinstance(item, ast.UnaryOp) and isinstance(item.op, (ast.UAdd, ast.USub, ast.Invert)):
            value = evaluate(item.operand)
            if isinstance(item.op, ast.UAdd):
                return value
            if isinstance(item.op, ast.USub):
                return -value
            return ~value
        if isinstance(item, ast.BinOp) and isinstance(
            item.op,
            (
                ast.Add,
                ast.Sub,
                ast.Mult,
                ast.FloorDiv,
                ast.Mod,
                ast.LShift,
                ast.RShift,
                ast.BitOr,
                ast.BitAnd,
                ast.BitXor,
            ),
        ):
            left, right = evaluate(item.left), evaluate(item.right)
            if isinstance(item.op, ast.Add):
                return left + right
            if isinstance(item.op, ast.Sub):
                return left - right
            if isinstance(item.op, ast.Mult):
                return left * right
            if isinstance(item.op, ast.FloorDiv):
                return left // right
            if isinstance(item.op, ast.Mod):
                return left % right
            if isinstance(item.op, ast.LShift):
                return left << right
            if isinstance(item.op, ast.RShift):
                return left >> right
            if isinstance(item.op, ast.BitOr):
                return left | right
            if isinstance(item.op, ast.BitAnd):
                return left & right
            if isinstance(item.op, ast.BitXor):
                return left ^ right
        raise AssemblyError(f"unsupported expression: {text!r}")

    return evaluate(node)


def assemble_text(
    text: str,
    definitions: tuple[InstructionDefinition, ...] = (),
    semantic_forms: Mapping[str, SemanticForm] | None = None,
) -> bytes:
    parsed: list[tuple[int, str, str, tuple[tuple[str, str | None], ...]]] = []
    labels: dict[str, int] = {}
    semantic_forms = semantic_forms or {}
    definitions_by_id = {definition.id: definition for definition in definitions}
    pc = 0
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.split(";", 1)[0].split("#", 1)[0].strip()
        if not line:
            continue
        if line.endswith(":"):
            label = line[:-1].strip()
            if not _LABEL.fullmatch(label):
                raise AssemblyError(f"line {line_number}: invalid label {label!r}")
            if label in labels:
                raise AssemblyError(f"line {line_number}: duplicate label {label!r}")
            labels[label] = pc
            continue
        directive, _, argument = line.partition(" ")
        try:
            argument, decorators = _split_decorators(argument)
        except AssemblyError as error:
            raise AssemblyError(f"line {line_number}: {error}") from error
        directive = directive.lower()
        arguments = _argument_list(argument)
        semantic = _semantic_match(directive, decorators, semantic_forms, arguments)
        parsed.append((line_number, directive, argument, decorators))
        if directive.lower() == ".org":
            if decorators:
                raise AssemblyError(f"line {line_number}: directives cannot have decorators")
            pc = _expression(argument, labels)
        elif (
            directive.lower() == ".word"
            or any(definition.mnemonic == directive.lower() for definition in definitions)
            or _ASSEMBLER_ALIASES.get(directive) in definitions_by_id
            or semantic is not None
        ):
            pc += 1
        elif directive.lower() not in (".arch", ".risc"):
            raise AssemblyError(
                f"line {line_number}: unsupported instruction/directive {directive!r}"
            )

    output = bytearray()
    pc = 0
    for line_number, directive, argument, decorators in parsed:
        if directive in (".arch", ".risc"):
            continue
        if directive == ".org":
            pc = _expression(argument, labels)
            continue
        arguments = _argument_list(argument)
        semantic = _semantic_match(directive, decorators, semantic_forms, arguments)
        if decorators and semantic is None:
            raise AssemblyError(f"line {line_number}: no {directive} encoding has those decorators")
        instruction: InstructionDefinition | None
        if semantic is not None:
            if semantic[0].deprecation_message is not None:
                warnings.warn(
                    f"line {line_number}: {semantic[0].deprecation_message}",
                    DeprecatedAssemblySyntaxWarning,
                    stacklevel=2,
                )
            instruction = definitions_by_id[semantic[0].instruction_id]
        elif directive in _ASSEMBLER_ALIASES:
            instruction = definitions_by_id.get(_ASSEMBLER_ALIASES[directive])
        else:
            instruction = next(
                (definition for definition in definitions if definition.mnemonic == directive),
                None,
            )
        if instruction is not None:
            semantic_operands = semantic[0].operands if semantic is not None else None
            expected_count = (
                len(semantic_operands)
                if semantic_operands is not None
                else len(instruction.operands)
            )
            if len(arguments) != expected_count:
                raise AssemblyError(
                    f"line {line_number}: {directive} expects {expected_count} operands"
                )
            supplied_fields = dict(semantic[1]) if semantic is not None else {}
            operand_kinds: dict[str, str] = {}
            if semantic_operands is not None:
                for semantic_operand, operand_text in zip(
                    semantic_operands, arguments, strict=True
                ):
                    if semantic_operand.literal is not None:
                        continue
                    assert semantic_operand.field is not None
                    supplied_fields[semantic_operand.field] = operand_text
                    if semantic_operand.kind is not None:
                        operand_kinds[semantic_operand.field] = semantic_operand.kind
            else:
                supplied_fields.update(
                    (operand.name, operand_text)
                    for operand, operand_text in zip(instruction.operands, arguments, strict=True)
                )
            value = instruction.value
            for operand in instruction.operands:
                operand_text = supplied_fields[operand.name]
                operand_kind = operand_kinds.get(operand.name, operand.kind)
                register_match = re.fullmatch(r"[rR](\d+)", operand_text)
                if (operand_kind, operand_text.lower()) in {
                    ("register_or_zero", "zero"),
                    ("register_or_preserve", "preserve"),
                }:
                    decoded_value = 31
                elif (
                    operand_kind
                    in {
                        "register",
                        "register_or_zero",
                        "register_or_preserve",
                        "register_pair_start",
                    }
                    and register_match
                ):
                    decoded_value = int(register_match.group(1))
                else:
                    decoded_value = _expression(operand_text, labels)
                encoded_value = decoded_value
                if operand_kind == "pc_relative":
                    delta = decoded_value - pc
                    linear_encoding = instruction.pc_relative_encoding
                    if (
                        linear_encoding is not None
                        and linear_encoding.form == "linear"
                        and linear_encoding.target_operand == operand.name
                    ):
                        delta -= operand.bias
                    elif operand.modulus is not None:
                        delta = (delta - operand.bias) % operand.modulus
                        if operand.signed and delta & (1 << (operand.width - 1)):
                            delta -= 1 << operand.width
                    if delta % operand.scale:
                        raise AssemblyError(
                            f"line {line_number}: relative target violates scale {operand.scale}"
                        )
                    encoded_value = delta // operand.scale
                    if operand.modulus is None:
                        encoded_value -= operand.bias
                elif operand_kind == "bit_index":
                    if operand.scale == 0 or (decoded_value - operand.bias) % operand.scale:
                        raise AssemblyError(
                            f"line {line_number}: bit index cannot be encoded by this field"
                        )
                    encoded_value = (decoded_value - operand.bias) // operand.scale
                elif operand_kind == "immediate" and (
                    operand.scale != 1 or operand.bias != 0
                ):
                    if operand.scale == 0 or (decoded_value - operand.bias) % operand.scale:
                        raise AssemblyError(
                            f"line {line_number}: immediate cannot be encoded by this field"
                        )
                    encoded_value = (decoded_value - operand.bias) // operand.scale
                elif operand_kind not in (
                    "raw",
                    "immediate",
                    "register",
                    "register_or_zero",
                    "register_or_preserve",
                    "register_or_task_special",
                    "register_pair_start",
                ):
                    raise AssemblyError(
                        f"line {line_number}: unsupported provisional operand kind {operand.kind!r}"
                    )
                minimum = -(1 << (operand.width - 1)) if operand.signed else 0
                maximum = (1 << (operand.width - int(operand.signed))) - 1
                if not minimum <= encoded_value <= maximum:
                    raise AssemblyError(
                        f"line {line_number}: operand {operand.name} is out of range"
                    )
                field_mask = (1 << operand.width) - 1
                value |= (encoded_value & field_mask) << operand.lsb
            if not instruction.matches(value, pc):
                raise AssemblyError(f"line {line_number}: encoded word violates fixed mask")
            output.extend(struct.pack(">I", value))
            pc += 1
            continue
        value = _expression(argument, labels)
        if not 0 <= value <= 0xFFFF_FFFF:
            raise AssemblyError(f"line {line_number}: .word value is outside 32 bits")
        output.extend(struct.pack(">I", value))
        pc += 1
    return bytes(output)
