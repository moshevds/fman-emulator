"""Expand compact, evidence-backed instruction-family descriptions."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ADD_FAMILY_FILENAME = "addition-family.yaml"
ADD_FAMILY_ID_PREFIX = "FHW-366-d8add"
SUB_FAMILY_FILENAME = "subtraction-family.yaml"
SUB_FAMILY_ID_PREFIX = "FHW-367-dcsub"
ALU_MATRIX_FILENAME = "alu-matrix-families.yaml"
SCALAR_MATRIX_FILENAME = "scalar-matrix-families.yaml"
COUNT_MATRIX_FILENAME = "count-matrix-families.yaml"
SHIFT_MATRIX_FILENAME = "shift-rotate-families.yaml"
BIT_UPDATE_FILENAME = "bit-update-families.yaml"
ALU_MATRIX_PREFIXES = (
    "FHW-368-e0addhi",
    "FHW-368-e1andhi",
    "FHW-368-e1orhi",
    "FHW-368-e4subhi",
    "FHW-368-e4subhib",
    "FHW-368-e5andnhi",
    "FHW-368-e5ornhi",
    "FHW-368-edandnlo",
    "FHW-368-edornlo",
    "FHW-368-f0addlane",
    "FHW-369-f0addlanec",
    "FHW-368-f1andlane",
    "FHW-368-f1orlane",
    "FHW-369-f4sublaneb",
    "FHW-368-d0cadd",
    "FHW-368-d3select",
)
GENERATED_FAMILY_PREFIXES = (
    ADD_FAMILY_ID_PREFIX,
    SUB_FAMILY_ID_PREFIX,
    *ALU_MATRIX_PREFIXES,
)
FAMILY_FILENAMES = (ADD_FAMILY_FILENAME, SUB_FAMILY_FILENAME)


def _load_families(encodings_path: Path) -> tuple[dict[str, Any], ...]:
    result: list[dict[str, Any]] = []
    for filename in FAMILY_FILENAMES:
        path = encodings_path.with_name(filename)
        if not path.is_file():
            continue
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or document.get("schema_version") != 1:
            raise ValueError(f"{path}: unsupported generated-family schema")
        result.append(document)
    return tuple(result)


def _generate_family(family: dict[str, Any]) -> list[dict[str, Any]]:
    base = int(family["base_value"])
    mask = int(family["exact_mask"])
    precedence = int(family["precedence"])
    profile = str(family["profile"])
    id_prefix = str(family["id_prefix"])
    mnemonic = str(family["mnemonic"])
    operator = str(family["operator"])
    operation = str(family["operation"])
    publish_function = str(family["publish_function"])
    family_description = str(family["family_description"])
    predicate_decorator = str(family["predicate_decorator"])
    is_addition = operator == "+"
    result: list[dict[str, Any]] = []
    for raw_transform in family["transforms"]:
        transform = raw_transform.get("decorator")
        transform_name = str(transform) if transform is not None else "plain"
        selector = int(raw_transform["selector"])
        for literal_one in (0, 1):
            for predicate_input in (0, 1):
                source = (
                    "p0-ignored-one"
                    if predicate_input and literal_one
                    else "p0"
                    if predicate_input
                    else "one"
                    if literal_one
                    else "zero"
                )
                for writes in (False, True):
                    for publishes in (False, True):
                        mode = (
                            ("write" if writes else "discard")
                            + "-"
                            + ("publish" if publishes else "preserve")
                        )
                        instruction_id = f"{id_prefix}-{transform_name}-{source}-{mode}"
                        value = (
                            base
                            | (predicate_input << 23)
                            | (int(writes) << 22)
                            | (int(publishes) << 21)
                            | (selector << 3)
                            | literal_one
                        )
                        operands = [
                            {"name": "left", "kind": "register", "field": {"lsb": 16, "width": 5}},
                            {"name": "right", "kind": "register", "field": {"lsb": 11, "width": 5}},
                            {
                                "name": "destination" if writes else "auxiliary",
                                "kind": "register" if writes else "raw",
                                "field": {"lsb": 6, "width": 5},
                            },
                        ]
                        input_expression = "p0" if predicate_input else str(literal_one)
                        raw_expression = (
                            f"r[left] + r[right] + {input_expression}"
                            if is_addition
                            else f"r[left] - r[right] - {input_expression}"
                        )
                        transform_expression = (
                            "raw" if transform is None else f"{transform_name}(raw)"
                        )
                        result.append(
                            {
                                "id": instruction_id,
                                "mnemonic": f"{mnemonic}.matrix.{transform_name}.{source}.{mode}",
                                "short_mnemonic": mnemonic,
                                "candidate_kind": f"hardware-confirmed-generated-{family['id']}",
                                "confidence": (
                                    "strong LS1046A family equation and confirmed no-visible-effect behavior"
                                    if not writes and not publishes
                                    else "confirmed LS1046A scalar and predicate equation"
                                ),
                                "profiles": [profile],
                                "mask": mask,
                                "value": value,
                                "length_bits": 32,
                                "precedence": precedence,
                                "operands": operands,
                                "semantics": {
                                    "pseudocode": (
                                        f"raw = ({raw_expression}) & 0xffffffff; "
                                        f"computed = {transform_expression}; "
                                        + (
                                            "r[destination] = computed; "
                                            if writes
                                            else "discard(computed); "
                                        )
                                        + (
                                            f"{publish_function}(raw, computed)"
                                            if publishes
                                            else "predicates unchanged"
                                        )
                                    ),
                                    "operation": operation,
                                    "family_description": family_description,
                                    "condition_effect": (
                                        f"publishes unsigned {predicate_decorator} from the untransformed arithmetic and result predicates from the transformed value"
                                        if publishes
                                        else "preserves predicate state"
                                    ),
                                },
                                "evidence": [
                                    {
                                        "analysis": str(family["evidence"]["analysis"]["path"]),
                                        "evidence": str(family["evidence"]["id"]),
                                        "metric": f"full_{family['id']}_matrix",
                                        "value": "256/256 exact cases entered, fell through, recovered, and match input = bit23 ? p0 : bit0 across all transforms and write/publication modes",
                                    }
                                ],
                                "counterevidence": (
                                    [
                                        "The discarded, predicate-preserving quadrant exposes no computed value; its operation identity follows the other three directly observable quadrants."
                                    ]
                                    if not writes and not publishes
                                    else []
                                ),
                                "tests": [],
                            }
                        )
    return result


def generated_instruction_entries(encodings_path: Path) -> list[dict[str, Any]]:
    """Return exact decoder entries expanded from compact family sources."""

    entries = [
        entry for family in _load_families(encodings_path) for entry in _generate_family(family)
    ]
    matrix_path = encodings_path.with_name(ALU_MATRIX_FILENAME)
    if matrix_path.is_file():
        document = yaml.safe_load(matrix_path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or document.get("schema_version") != 1:
            raise ValueError(f"{matrix_path}: unsupported ALU-matrix schema")
        entries.extend(_generate_alu_matrices(document))
    scalar_path = encodings_path.with_name(SCALAR_MATRIX_FILENAME)
    if scalar_path.is_file():
        document = yaml.safe_load(scalar_path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or document.get("schema_version") != 1:
            raise ValueError(f"{scalar_path}: unsupported scalar-matrix schema")
        entries.extend(_generate_scalar_matrices(document))
    count_path = encodings_path.with_name(COUNT_MATRIX_FILENAME)
    if count_path.is_file():
        document = yaml.safe_load(count_path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or document.get("schema_version") != 1:
            raise ValueError(f"{count_path}: unsupported count-matrix schema")
        entries.extend(_generate_count_matrices(document))
    shift_path = encodings_path.with_name(SHIFT_MATRIX_FILENAME)
    if shift_path.is_file():
        document = yaml.safe_load(shift_path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or document.get("schema_version") != 1:
            raise ValueError(f"{shift_path}: unsupported shift-matrix schema")
        entries.extend(_generate_shift_matrices(document))
    bit_path = encodings_path.with_name(BIT_UPDATE_FILENAME)
    if bit_path.is_file():
        document = yaml.safe_load(bit_path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or document.get("schema_version") != 1:
            raise ValueError(f"{bit_path}: unsupported bit-update schema")
        entries.extend(_generate_bit_updates(document))
    return entries


def _generate_bit_updates(document: dict[str, Any]) -> list[dict[str, Any]]:
    if document.get("mode_bits") != {"write": 22, "publish": 21}:
        raise ValueError("bit-update matrix must encode write/publish in bits 22/21")
    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for family in document["families"]:
        cells = family["cells"]
        base = int(family["base_value"])
        if len(cells) != 4 or base & 0x00600000:
            raise ValueError(f"{family['name']}: invalid four-mode base")
        action = str(family["action"])
        index_source = str(family["index_source"])
        index_order = str(family["index_order"])
        if action not in ("set", "clear") or index_source not in ("register", "immediate"):
            raise ValueError(f"{family['name']}: invalid bit-update action or source")
        if index_order not in ("normal", "reverse"):
            raise ValueError(f"{family['name']}: invalid index order")
        for mode, cell in enumerate(cells):
            if len(cell) not in (4, 5) or not cell[3]:
                raise ValueError(f"{family['name']}: missing cell evidence")
            instruction_id, raw_mnemonic, precedence, evidence_keys = cell[:4]
            style = cell[4] if len(cell) == 5 else "standard"
            if style not in (
                "standard",
                "legacy-raw",
                "legacy-reverse-set",
                "legacy-reverse-clear",
            ):
                raise ValueError(f"{instruction_id}: invalid operand style")
            if instruction_id in seen_ids:
                raise ValueError(f"duplicate bit-update ID {instruction_id}")
            seen_ids.add(str(instruction_id))
            writes, publishes = bool(mode & 2), bool(mode & 1)
            if style == "legacy-raw":
                source_name, source_kind = "operand_20_16", "raw"
                index_name, index_kind = "index_operand_15_11", "raw"
                output_name, output_kind = "result_10_6", "raw"
            elif style == "legacy-reverse-set":
                source_name, source_kind = "value", "register"
                index_name, index_kind = "index", "register"
                output_name, output_kind = "destination", "register"
            elif style == "legacy-reverse-clear":
                source_name, source_kind = "source_20_16", "register"
                index_name, index_kind = "index_register_15_11", "register"
                output_name, output_kind = "destination_10_6", "register"
            elif index_source == "immediate":
                source_name, source_kind = "source", "register"
                index_name, index_kind = "bit_index", "immediate"
                output_name = "destination" if writes else "auxiliary"
                output_kind = "register" if writes else "raw"
            else:
                source_name, source_kind = "left", "register"
                index_name, index_kind = "right", "register"
                output_name = "destination" if writes else "auxiliary_10_6"
                output_kind = "register" if writes else "raw"
            index_expression = (
                index_name if index_source == "immediate" else f"r[{index_name}] & 31"
            )
            if index_order == "reverse":
                bit_expression = f"(0x80000000 >> ({index_expression}))"
            else:
                bit_expression = f"(1 << ({index_expression}))"
            operator = "|" if action == "set" else "& ~"
            computed = f"r[{source_name}] {operator} {bit_expression}"
            effect = (
                str(document["publish_effect"])
                if publishes and index_source == "register"
                else "publishes logical-result predicates from the computed value"
                if publishes
                else "does not request predicate publication; sampled predicates unchanged"
                if index_source == "immediate"
                else "preserves p0-p15 in tested states"
                if mode == 0 or style == "legacy-reverse-set"
                else "preserves p0/p1/p2/p3/p10/p11"
                if style == "legacy-raw"
                else "preserves the tested predicates"
            )
            evidence = [
                {
                    "analysis": str(document["evidence"][key]),
                    "evidence": str(key),
                    "metric": "bit_update_mode_support",
                    "value": "scope and tested vectors are recorded in the cited source",
                }
                for key in evidence_keys
            ]
            result.append(
                {
                    "id": str(instruction_id),
                    "mnemonic": str(raw_mnemonic),
                    "short_mnemonic": str(family["name"]),
                    "candidate_kind": "hardware-checked-generated-bit-update-matrix",
                    "confidence": (
                        "strong operation identity from observable siblings; confirmed no scalar write or predicate change"
                        if mode == 0 and index_source == "register"
                        else "confirmed LS1046A scalar and/or predicate behavior for this exact mode"
                    ),
                    "profiles": ["ls1046a-fman"],
                    "mask": 0xFFE0003F,
                    "value": base | (mode << 21),
                    "length_bits": 32,
                    "precedence": int(precedence),
                    "operands": [
                        {
                            "name": source_name,
                            "kind": source_kind,
                            "field": {"lsb": 16, "width": 5},
                        },
                        {"name": index_name, "kind": index_kind, "field": {"lsb": 11, "width": 5}},
                        {"name": output_name, "kind": output_kind, "field": {"lsb": 6, "width": 5}},
                    ],
                    "semantics": {
                        "pseudocode": (
                            f"computed = {computed}; "
                            + (
                                f"r[{output_name}] = computed; "
                                if writes
                                else "discard(computed); "
                            )
                            + (
                                "publish_conditions(computed)"
                                if publishes
                                else "predicates unchanged"
                            )
                        ),
                        "operation": str(family["description"]),
                        "family_description": str(family["description"]),
                        "condition_effect": effect,
                    },
                    "evidence": evidence,
                    "counterevidence": (
                        [
                            "Discard/preserve exposes no computed value; identity follows observable siblings."
                        ]
                        if mode == 0
                        else [
                            "Special-register inputs and timing are not established for every form."
                        ]
                    ),
                    "tests": (
                        [str(document["test_overrides"][instruction_id])]
                        if instruction_id in document.get("test_overrides", {})
                        else []
                    ),
                }
            )
    return result


def _generate_shift_matrices(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand only the evidenced four-mode DB/DF shift and rotate cells."""

    if document.get("mode_bits") != {"write": 22, "publish": 21}:
        raise ValueError("shift matrix must encode write/publish in bits 22/21")
    result: list[dict[str, Any]] = []
    evidence_paths = document["evidence"]
    seen_ids: set[str] = set()
    for family in document["families"]:
        cells = family["cells"]
        if len(cells) != 4:
            raise ValueError(f"{family['name']}: expected four shift modes")
        base = int(family["base_value"])
        if base & 0x00600000 or (base >> 24) not in (0xDB, 0xDF):
            raise ValueError(f"{family['name']}: invalid shift-matrix base")
        count_source = str(family["count_source"])
        count_modifier = str(family.get("count_modifier", "plain"))
        if count_source not in ("register", "immediate"):
            raise ValueError(f"{family['name']}: invalid count source")
        if count_modifier not in ("plain", "reverse", "increment"):
            raise ValueError(f"{family['name']}: invalid count modifier")
        for mode, cell in enumerate(cells):
            if len(cell) not in (4, 5) or not cell[3]:
                raise ValueError(f"{family['name']}: invalid or unsupported mode cell")
            instruction_id, raw_mnemonic, precedence, evidence_keys = cell[:4]
            if instruction_id in seen_ids:
                raise ValueError(f"duplicate shift instruction ID {instruction_id}")
            seen_ids.add(str(instruction_id))
            style = cell[4] if len(cell) == 5 else "standard"
            writes, publishes = bool(mode & 2), bool(mode & 1)
            if style not in ("standard", "legacy-raw", "legacy-test", "legacy-publish"):
                raise ValueError(f"{instruction_id}: invalid operand style")
            if count_source == "immediate":
                count_name, count_field_kind = "count", "immediate"
                count_expression = "count & 31"
            else:
                count_name, count_field_kind = "count", "register"
                count_expression = "r[count] & 31"
            if count_modifier == "reverse":
                count_expression = f"(31 - ({count_expression})) & 31"
            elif count_modifier == "increment":
                count_expression = f"({count_expression} + 1) & 31"
            if style == "legacy-raw":
                source_name, source_kind = "operand_20_16", "raw"
                count_name = "shift_u5" if count_source == "immediate" else "count_operand_15_11"
                count_field_kind = "immediate" if count_source == "immediate" else "raw"
                output_name, output_kind = "result_10_6", "raw"
            else:
                source_name, source_kind = "source", "register"
                if style == "legacy-test":
                    count_name = "shift_u5" if count_source == "immediate" else "count_register"
                elif style == "legacy-publish" and count_source == "immediate":
                    count_name = "shift_u5"
                output_name = "destination" if writes else "auxiliary_10_6"
                output_kind = "register" if writes else "raw"
            computation = str(family["computation"]).format(count=count_expression)
            if style == "legacy-raw":
                computation = computation.replace("r[source]", "r[operand_20_16]")
                computation = computation.replace("r[count]", "r[count_operand_15_11]")
                if count_source == "immediate":
                    computation = computation.replace("count & 31", "shift_u5")
            elif style == "legacy-test" and count_source == "immediate":
                computation = computation.replace("count & 31", "shift_u5")
            elif style == "legacy-test":
                computation = computation.replace("r[count]", "r[count_register]")
            elif style == "legacy-publish" and count_source == "immediate":
                computation = computation.replace("count & 31", "shift_u5")
            if publishes:
                condition_effect = str(family["publish_effect"])
                predicate_action = str(family["publish_action"])
            else:
                condition_effect = (
                    "preserves p0-p15 in three tested incoming states"
                    if mode == 0
                    else "preserves p0/p1/p2/p3/p10/p11"
                    if style == "legacy-raw"
                    else "preserves the tested predicates"
                )
                predicate_action = "predicates unchanged"
            condition_effect = str(
                document.get("condition_overrides", {}).get(instruction_id, condition_effect)
            )
            if not writes and not publishes:
                confidence = "strong operation identity from result-producing siblings; confirmed no scalar write or predicate change"
            else:
                confidence = (
                    "confirmed LS1046A scalar and/or predicate behavior for this exact mode"
                )
            evidence = [
                {
                    "analysis": str(evidence_paths[key]),
                    "evidence": str(key),
                    "metric": "shift_rotate_mode_support",
                    "value": "scope and tested vectors are recorded in the cited source",
                }
                for key in evidence_keys
            ]
            result.append(
                {
                    "id": str(instruction_id),
                    "mnemonic": str(raw_mnemonic),
                    "short_mnemonic": str(family["name"]),
                    "candidate_kind": "hardware-checked-generated-shift-matrix",
                    "confidence": confidence,
                    "profiles": ["ls1046a-fman"],
                    "mask": 0xFFE0003F,
                    "value": base | (mode << 21),
                    "length_bits": 32,
                    "precedence": int(precedence),
                    "operands": [
                        {
                            "name": source_name,
                            "kind": source_kind,
                            "field": {"lsb": 16, "width": 5},
                        },
                        {
                            "name": count_name,
                            "kind": count_field_kind,
                            "field": {"lsb": 11, "width": 5},
                        },
                        {"name": output_name, "kind": output_kind, "field": {"lsb": 6, "width": 5}},
                    ],
                    "semantics": {
                        "pseudocode": (
                            f"computed = {computation}; "
                            + (
                                f"r[{output_name}] = computed; "
                                if writes
                                else "discard(computed); "
                            )
                            + predicate_action
                        ),
                        "operation": str(family["description"]),
                        "family_description": str(family["description"]),
                        "condition_effect": condition_effect,
                    },
                    "evidence": evidence,
                    "counterevidence": (
                        [
                            "Discard/preserve has no visible result; the operation identity follows tested sibling modes."
                        ]
                        if mode == 0
                        else [
                            "Special-register inputs and timing are not established for every form."
                        ]
                    )
                    + (
                        [str(document["limitations"][instruction_id])]
                        if instruction_id in document.get("limitations", {})
                        else []
                    ),
                    "tests": (
                        [str(document["test_overrides"][instruction_id])]
                        if instruction_id in document.get("test_overrides", {})
                        else [
                            "Exact word tested under opposite incoming predicate states with I-RAM and MURAM restoration."
                        ]
                        if "FHW-358" in evidence_keys
                        else []
                    ),
                }
            )
    return result


def _generate_count_matrices(document: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for family in document["families"]:
        cells = family["cells"]
        if len(cells) != 4:
            raise ValueError(f"{family['mnemonic']}: count matrix requires four cells")
        for mode, (instruction_id, precedence, raw_mnemonic, evidence_id) in enumerate(cells):
            writes = bool(mode & 2)
            publishes = bool(mode & 1)
            expression = str(family["expression"])
            result.append(
                {
                    "id": str(instruction_id),
                    "mnemonic": str(raw_mnemonic),
                    "short_mnemonic": str(family["mnemonic"]),
                    "candidate_kind": "hardware-confirmed-generated-count-matrix",
                    "confidence": (
                        "strong no-visible-effect identity from the three observable siblings"
                        if mode == 0
                        else "confirmed LS1046A scalar and sampled predicate behavior"
                    ),
                    "profiles": ["ls1046a-fman"],
                    "mask": 0xFFE0003F,
                    "value": int(family["base_value"]) | (mode << 21),
                    "length_bits": 32,
                    "precedence": int(precedence),
                    "operands": [
                        {"name": "unused_20_16", "kind": "raw", "field": {"lsb": 16, "width": 5}},
                        {"name": "source", "kind": "register", "field": {"lsb": 11, "width": 5}},
                        {
                            "name": "destination" if writes else "auxiliary_10_6",
                            "kind": "register" if writes else "raw",
                            "field": {"lsb": 6, "width": 5},
                        },
                    ],
                    "semantics": {
                        "pseudocode": (
                            f"computed = {expression}; "
                            + ("r[destination] = computed; " if writes else "discard(computed); ")
                            + (
                                str(family["publish_expression"])
                                if publishes
                                else "predicates unchanged"
                            )
                        ),
                        "operation": str(family["description"]),
                        "family_description": str(family["description"]),
                        "condition_effect": (
                            str(family["predicate_note"])
                            if publishes
                            else "preserves the tested predicate state"
                        ),
                    },
                    "evidence": [
                        {
                            "analysis": str(document["evidence"][evidence_id]),
                            "evidence": str(evidence_id),
                            "metric": "count_result_mode_matrix",
                            "value": "hardware-tested cell; the four-mode equation is shared by this family",
                        }
                    ],
                    "counterevidence": (
                        [str(family["counterevidence"])] if "counterevidence" in family else []
                    )
                    + (
                        [
                            "Discard/preserve exposes no result; identity follows observable sibling modes."
                        ]
                        if mode == 0
                        else []
                    ),
                    "tests": [],
                }
            )
    return result


def _scalar_matrix_rows(document: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, object]]]:
    rows: list[tuple[dict[str, Any], dict[str, object]]] = []
    transforms = document["logical_transforms"]
    if [int(item["selector"]) for item in transforms] != list(range(0, 64, 8)):
        raise ValueError("logical transforms must enumerate the complete eight-selector grid")
    for family in document["logical_families"]:
        cells = family["cells"]
        if len(cells) != 32:
            raise ValueError(f"{family['mnemonic']}: logical matrix requires 32 cells")
        for index, (instruction_id, precedence, raw_mnemonic, layout) in enumerate(cells):
            transform = transforms[index // 4]
            mode = index % 4
            writes = bool(mode & 2)
            publishes = bool(mode & 1)
            transform_name = str(transform["name"])
            operands = _scalar_operands(document, str(layout))
            names = {operand["field"]["lsb"]: operand["name"] for operand in operands}
            raw = (
                str(family["equation"])
                .replace("r[left]", f"r[{names[16]}]")
                .replace("r[right]", f"r[{names[11]}]")
            )
            computed = str(transform["expression"])
            value = int(family["base_value"]) | int(transform["selector"]) | (mode << 21)
            rows.append(
                (
                    {
                        "id": str(instruction_id),
                        "mnemonic": str(raw_mnemonic),
                        "short_mnemonic": str(family["mnemonic"]),
                        "candidate_kind": "hardware-confirmed-generated-logical-matrix",
                        "confidence": (
                            "confirmed LS1046A no visible scalar or predicate effect; operation identity follows sibling modes"
                            if mode == 0
                            else "confirmed LS1046A scalar and predicate behavior"
                        ),
                        "profiles": ["ls1046a-fman"],
                        "mask": 0xFFE0003F,
                        "value": value,
                        "length_bits": 32,
                        "precedence": int(precedence),
                        "operands": operands,
                        "semantics": {
                            "pseudocode": (
                                f"raw = ({raw}) & 0xffffffff; computed = {computed}; "
                                + (
                                    f"r[{names[6]}] = computed; "
                                    if writes
                                    else "discard(computed); "
                                )
                                + (
                                    "publish_logical_conditions(computed)"
                                    if publishes
                                    else "predicates unchanged"
                                )
                            ),
                            "operation": str(family["mnemonic"]),
                            "family_description": str(family["description"]),
                            "condition_effect": (
                                "publishes p0-p3 from computed; p4-p15 follow the established logical-result model but were not read back in this campaign"
                                if publishes
                                else "preserves p0-p3 directly; p4-p15 were not read back in this campaign"
                            ),
                        },
                        "evidence": [
                            {
                                "analysis": str(
                                    document["evidence"][family["evidence"]]["analysis"]
                                ),
                                "evidence": str(family["evidence"]),
                                "metric": "logical_transform_matrix",
                                "value": str(document["evidence"][family["evidence"]]["result"]),
                            }
                        ],
                        "counterevidence": [
                            "Contextual register indices retain their separately documented meanings.",
                            *(
                                [
                                    "Discard and preserve expose no computed value; operation identity follows the other three modes."
                                ]
                                if mode == 0
                                else []
                            ),
                        ],
                        "tests": [],
                    },
                    {
                        "mnemonic": str(family["mnemonic"]),
                        "format": "scalar_logical",
                        "writes": writes,
                        "publishes": publishes,
                        "transform": None if transform_name == "plain" else transform_name,
                        "left_field": names[16],
                        "right_field": names[11],
                        "result_field": names[6],
                    },
                )
            )
    for family in document["conditional_families"]:
        cells = family["cells"]
        if len(cells) != 8:
            raise ValueError(f"{family['mnemonic']}: conditional matrix requires eight cells")
        is_bitset = family["mnemonic"] == "bset32"
        for index, (instruction_id, precedence, raw_mnemonic, layout) in enumerate(cells):
            mode = index // 2
            writes = bool(mode & 2)
            publishes = bool(mode & 1)
            polarity = "clear" if index & 1 else "set"
            condition = "!p[predicate]" if polarity == "clear" else "p[predicate]"
            value = int(family["base_value"]) | (mode << 21) | (index & 1)
            operands = _scalar_operands(document, str(layout))
            names = {operand["field"]["lsb"]: operand["name"] for operand in operands}
            equation = (
                str(family["equation"])
                .replace("r[left]", f"r[{names[16]}]")
                .replace("r[right]", f"r[{names[11]}]")
                .replace("bit_index", str(names[11]))
            )
            rows.append(
                (
                    {
                        "id": str(instruction_id),
                        "mnemonic": str(raw_mnemonic),
                        "short_mnemonic": str(family["mnemonic"]),
                        "candidate_kind": "hardware-confirmed-generated-conditional-matrix",
                        "confidence": (
                            "confirmed LS1046A no visible scalar or predicate effect; operation identity follows sibling modes"
                            if mode == 0
                            else "confirmed LS1046A tested conditional behavior"
                        ),
                        "profiles": ["ls1046a-fman"],
                        "mask": 0xFFE00001,
                        "value": value,
                        "length_bits": 32,
                        "precedence": int(precedence),
                        "operands": operands,
                        "semantics": {
                            "pseudocode": (
                                f"selected = {condition} ? ({equation}) & 0xffffffff : r[{names[16]}]; "
                                + (
                                    f"r[{names[6]}] = selected; "
                                    if writes
                                    else "discard(selected); "
                                )
                                + (
                                    "publish_conditions(selected)"
                                    if publishes
                                    else "predicates unchanged"
                                )
                            ),
                            "operation": f"conditional {family['mnemonic']}",
                            "family_description": str(family["description"]),
                            "condition_effect": (
                                (
                                    "publishes result conditions in representative p1-p3 states; other selectors follow the established family model"
                                    if family["evidence"] == "FHW-359"
                                    else "publishes p0-p3 from the selected result; higher selectors follow the established family model"
                                )
                                if publishes
                                else "preserves the tested predicate state"
                            ),
                        },
                        "evidence": [
                            {
                                "analysis": str(
                                    document["evidence"][family["evidence"]]["analysis"]
                                ),
                                "evidence": str(family["evidence"]),
                                "metric": "conditional_result_matrix",
                                "value": str(document["evidence"][family["evidence"]]["result"]),
                            }
                        ],
                        "counterevidence": [
                            "Selector p14 has no known set producer; p16-p31 remain unadmitted for this family."
                        ]
                        if family["evidence"] == "FHW-359"
                        else ["Selectors p16-p31 remain unadmitted for this family."],
                        "tests": [],
                    },
                    {
                        "mnemonic": str(family["mnemonic"]),
                        "format": "conditional_bitset" if is_bitset else "conditional_scalar",
                        "writes": writes,
                        "publishes": publishes,
                        "polarity": polarity,
                        "transform": None,
                        "fixed_decorators": ("reverse",) if is_bitset else (),
                        "left_field": names[16],
                        "right_field": names[11],
                        "result_field": names[6],
                        "predicate_field": names[1],
                    },
                )
            )
    ids = [entry["id"] for entry, _ in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate scalar-matrix instruction ID")
    return rows


def _scalar_operands(document: dict[str, Any], layout: str) -> list[dict[str, Any]]:
    return [
        {"name": str(name), "kind": str(kind), "field": {"lsb": int(lsb), "width": int(width)}}
        for name, kind, lsb, width in document["operand_layouts"][layout]
    ]


def _generate_scalar_matrices(document: dict[str, Any]) -> list[dict[str, Any]]:
    return [entry for entry, _ in _scalar_matrix_rows(document)]


@lru_cache(maxsize=32)
def _scalar_matrix_metadata(path: Path) -> dict[str, dict[str, object]]:
    if not path.is_file():
        return {}
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {str(entry["id"]): metadata for entry, metadata in _scalar_matrix_rows(document)}


def _generate_alu_matrices(document: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = document["evidence"]
    result: list[dict[str, Any]] = []
    for family in document["families"]:
        family_evidence = family.get("evidence", evidence)
        fixed_decorators = tuple(str(item) for item in family.get("fixed_decorators", ()))
        raw_decorator_suffix = "".join(f".{item}" for item in fixed_decorators)
        family_format = str(family["format"])
        polarities = tuple(family.get("polarities", (None,)))
        for polarity in polarities:
            for writes in (False, True):
                for publishes in (False, True):
                    mode = (
                        ("write" if writes else "discard")
                        + "-"
                        + ("publish" if publishes else "preserve")
                    )
                    polarity_name = f"-{polarity}" if polarity is not None else ""
                    instruction_id = f"{family['id_prefix']}{polarity_name}-{mode}"
                    value = (
                        int(family["base_value"])
                        | (int(writes) << 22)
                        | (int(publishes) << 21)
                        | (int(polarity == "clear") if polarity is not None else 0)
                    )
                    operands: list[dict[str, Any]]
                    if family_format == "immediate16":
                        operands = [
                            {
                                "name": "register",
                                "kind": "register",
                                "field": {"lsb": 16, "width": 5},
                            },
                            {
                                "name": "immediate_u16",
                                "kind": "immediate",
                                "field": {"lsb": 0, "width": 16},
                            },
                        ]
                    elif family_format == "lane8":
                        operands = [
                            {
                                "name": "source",
                                "kind": "register",
                                "field": {"lsb": 16, "width": 5},
                            },
                            {
                                "name": "destination" if writes else "auxiliary",
                                "kind": "register" if writes else "raw",
                                "field": {"lsb": 11, "width": 5},
                            },
                            {"name": "lane", "kind": "immediate", "field": {"lsb": 8, "width": 2}},
                            {
                                "name": "immediate_u8",
                                "kind": "immediate",
                                "field": {"lsb": 0, "width": 8},
                            },
                        ]
                    elif family_format == "conditional_add":
                        operands = _conditional_operands(writes, select=False)
                    elif family_format == "conditional_select":
                        operands = _conditional_operands(writes, select=True)
                    else:
                        raise ValueError(f"unknown ALU matrix format: {family_format}")
                    equation = str(family["equation"])
                    result.append(
                        {
                            "id": instruction_id,
                            "mnemonic": (
                                f"{family['mnemonic']}.matrix.{mode}{polarity_name}"
                                f"{raw_decorator_suffix}"
                            ),
                            "short_mnemonic": str(family["mnemonic"]),
                            "candidate_kind": "hardware-confirmed-generated-alu-matrix",
                            "confidence": (
                                "strong LS1046A family equation and confirmed no-visible-effect behavior"
                                if not writes and not publishes
                                else "confirmed LS1046A scalar and predicate equation"
                            ),
                            "profiles": ["ls1046a-fman"],
                            "mask": int(family["exact_mask"]),
                            "value": value,
                            "length_bits": 32,
                            "precedence": 1000,
                            "operands": operands,
                            "semantics": {
                                "pseudocode": (
                                    f"computed = ({equation}) & 0xffffffff; "
                                    + ("write computed; " if writes else "discard(computed); ")
                                    + (
                                        "publish result predicates"
                                        if publishes
                                        else "predicates unchanged"
                                    )
                                ),
                                "operation": str(family["id"]),
                                "family_description": str(family["family_description"]),
                                "condition_effect": (
                                    "publishes arithmetic/logical result predicates"
                                    if publishes
                                    else "preserves predicate state"
                                ),
                            },
                            "evidence": [
                                {
                                    "analysis": str(family_evidence["analysis"]),
                                    "evidence": str(family_evidence["id"]),
                                    "metric": str(family["id"]),
                                    "value": str(
                                        family_evidence.get(
                                            "result",
                                            "all selected write/publication modes and vectors matched",
                                        )
                                    ),
                                }
                            ],
                            "counterevidence": (
                                [
                                    "The discard/preserve form has no visible result; its operation identity follows the other three quadrants."
                                ]
                                if not writes and not publishes
                                else []
                            ),
                            "tests": [],
                        }
                    )
                    ignored_alias = family.get("ignored_bit_alias")
                    if ignored_alias is not None:
                        alias_bit = int(ignored_alias["bit"])
                        alias_name = str(ignored_alias["name"])
                        canonical = result[-1]
                        alias_semantics = dict(canonical["semantics"])
                        alias_semantics["ignored_bits"] = (
                            f"bit {alias_bit} has no architectural effect in the tested "
                            "ordinary-register lane-arithmetic scope"
                        )
                        result.append(
                            {
                                **canonical,
                                "id": f"{instruction_id}-{alias_name}",
                                "mnemonic": f"{canonical['mnemonic']}.{alias_name}",
                                "candidate_kind": "hardware-confirmed-encoding-alias",
                                "confidence": "confirmed ignored encoding bit on LS1046A",
                                "value": value | (1 << alias_bit),
                                "semantics": alias_semantics,
                                "evidence": [
                                    {
                                        "analysis": str(ignored_alias["analysis"]),
                                        "evidence": str(ignored_alias["evidence"]),
                                        "metric": f"{family['id']}_bit_{alias_bit}_alias",
                                        "value": str(ignored_alias["result"]),
                                    }
                                ],
                                "counterevidence": [
                                    "No acquired FMan firmware word uses this noncanonical alias spelling."
                                ],
                                "tests": [
                                    "Retain the explicit encoding decorator for byte-exact assembly."
                                ],
                            }
                        )
    return result


def _conditional_operands(writes: bool, *, select: bool) -> list[dict[str, Any]]:
    operands: list[dict[str, Any]] = [
        {
            "name": "false_source" if select else "left",
            "kind": "register",
            "field": {"lsb": 16, "width": 5},
        },
        {
            "name": "true_source" if select else "right",
            "kind": "register",
            "field": {"lsb": 11, "width": 5},
        },
        {
            "name": "destination" if writes else "auxiliary",
            "kind": "register" if writes else "raw",
            "field": {"lsb": 6, "width": 5},
        },
        {"name": "predicate", "kind": "raw", "field": {"lsb": 1, "width": 5}},
    ]
    return operands


def generated_semantic_metadata(
    instruction_id: str, *, encodings_path: Path | None = None
) -> dict[str, object] | None:
    """Decode a stable generated ID into semantic-frontend attributes."""

    families = {
        ADD_FAMILY_ID_PREFIX: ("add32", "carry", "increment"),
        SUB_FAMILY_ID_PREFIX: ("sub32", "borrow", "decrement"),
    }
    matched = next(
        (
            (prefix, properties)
            for prefix, properties in families.items()
            if instruction_id.startswith(f"{prefix}-")
        ),
        None,
    )
    if matched is None:
        scalar_path = (
            encodings_path.with_name(SCALAR_MATRIX_FILENAME)
            if encodings_path is not None
            else Path(__file__).resolve().parent.parent / "isa" / SCALAR_MATRIX_FILENAME
        )
        return _generated_alu_matrix_metadata(instruction_id) or _scalar_matrix_metadata(
            scalar_path
        ).get(instruction_id)
    prefix, (mnemonic, predicate_decorator, literal_decorator) = matched
    suffix = instruction_id.removeprefix(f"{prefix}-")
    transform, source_and_mode = suffix.split("-", 1)
    sources = ("p0-ignored-one", "zero", "one", "p0")
    source = next(item for item in sources if source_and_mode.startswith(f"{item}-"))
    mode = source_and_mode.removeprefix(f"{source}-")
    return {
        "mnemonic": mnemonic,
        "predicate_decorator": predicate_decorator,
        "literal_decorator": literal_decorator,
        "transform": None if transform == "plain" else transform,
        "literal_one": source == "one",
        "predicate_input": source in {"p0", "p0-ignored-one"},
        "alias_one": source == "p0-ignored-one",
        "writes": mode.startswith("write-"),
        "publishes": mode.endswith("-publish"),
        "format": "register_arithmetic",
    }


def _generated_alu_matrix_metadata(instruction_id: str) -> dict[str, object] | None:
    families: dict[str, tuple[str, str, tuple[str, ...]]] = {
        "FHW-368-e0addhi": ("addhi16", "immediate16", ()),
        "FHW-368-e1andhi": ("andhi16", "immediate16", ()),
        "FHW-368-e1orhi": ("orhi16", "immediate16", ()),
        "FHW-368-e4subhi": ("subhi16", "immediate16", ()),
        "FHW-368-e4subhib": ("subhi16", "immediate16", ("borrow",)),
        "FHW-368-e5andnhi": ("andnhi16", "immediate16", ()),
        "FHW-368-e5ornhi": ("ornhi16", "immediate16", ()),
        "FHW-368-edandnlo": ("andnlo16", "immediate16", ()),
        "FHW-368-edornlo": ("ornlo16", "immediate16", ()),
        "FHW-368-f0addlane": ("addlane8", "lane8", ()),
        "FHW-369-f0addlanec": ("addlane8", "lane8", ("carry",)),
        "FHW-368-f1andlane": ("andlane8", "lane8", ()),
        "FHW-368-f1orlane": ("orlane8", "lane8", ()),
        "FHW-369-f4sublaneb": ("sublane8", "lane8", ("borrow",)),
        "FHW-368-d0cadd": ("add32", "conditional_add", ()),
        "FHW-368-d3select": ("select32", "conditional_select", ()),
    }
    matched = next(
        (
            (prefix, properties)
            for prefix, properties in families.items()
            if instruction_id.startswith(f"{prefix}-")
        ),
        None,
    )
    if matched is None:
        return None
    prefix, (mnemonic, family_format, fixed_decorators) = matched
    suffix = instruction_id.removeprefix(f"{prefix}-")
    encoding_alias: str | None = None
    if suffix.endswith("-bit10-alias"):
        encoding_alias = "bit10-alias"
        suffix = suffix.removesuffix("-bit10-alias")
    polarity: str | None = None
    if suffix.startswith("set-"):
        polarity, suffix = "set", suffix.removeprefix("set-")
    elif suffix.startswith("clear-"):
        polarity, suffix = "clear", suffix.removeprefix("clear-")
    return {
        "mnemonic": mnemonic,
        "format": family_format,
        "fixed_decorators": fixed_decorators,
        "polarity": polarity,
        "writes": suffix.startswith("write-"),
        "publishes": suffix.endswith("-publish"),
        "predicate_input": False,
        "literal_one": False,
        "alias_one": False,
        "encoding_alias": encoding_alias,
        "transform": None,
    }
