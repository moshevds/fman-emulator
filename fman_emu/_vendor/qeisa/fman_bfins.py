"""Decode the LS1046A FMan F8-FB circular bitfield-insert format."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .fman_circular_geometry import circular_mask

MASK32 = 0xFFFF_FFFF
MAJOR_MASK = 0xFC00_0000
MAJOR_VALUE = 0xF800_0000


@dataclass(frozen=True, slots=True)
class BfinsFields:
    """The six independent fields carried by an F8-FB instruction word."""

    high_trim: int
    source_payload: int
    destination: int
    rotate: int
    low_bound: int
    source_mode: str

    @property
    def high_bound(self) -> int:
        return 31 - self.high_trim

    @property
    def destination_lsb(self) -> int:
        return self.low_bound

    @property
    def source_lsb(self) -> int:
        return (self.low_bound - self.rotate) & 31

    @property
    def width(self) -> int:
        return ((self.high_bound - self.low_bound) & 31) + 1

    def as_dict(self) -> dict[str, int | str]:
        return {
            **asdict(self),
            "high_bound": self.high_bound,
            "source_lsb": self.source_lsb,
            "destination_lsb": self.destination_lsb,
            "width": self.width,
        }


def rotate_left32(value: int, count: int) -> int:
    count &= 31
    value &= MASK32
    return value if count == 0 else ((value << count) | (value >> (32 - count))) & MASK32


def decode_bfins_word(word: int) -> BfinsFields:
    """Decode an F8-FB word, rejecting words outside the major format."""

    word &= MASK32
    if word & MAJOR_MASK != MAJOR_VALUE:
        raise ValueError(f"0x{word:08x} is outside the F8-FB format")
    return BfinsFields(
        high_trim=(word >> 21) & 31,
        source_payload=(word >> 16) & 31,
        destination=(word >> 11) & 31,
        rotate=(word >> 6) & 31,
        low_bound=(word >> 1) & 31,
        source_mode="immediate5" if word & 1 else "register",
    )


def encode_bfins_fields(fields: BfinsFields) -> int:
    """Encode the six fields without assigning any additional behavior bits."""

    for name in ("high_trim", "source_payload", "destination", "rotate", "low_bound"):
        value = getattr(fields, name)
        if not 0 <= value < 32:
            raise ValueError(f"{name} must be in 0..31")
    if fields.source_mode not in {"register", "immediate5"}:
        raise ValueError("source_mode must be 'register' or 'immediate5'")
    return (
        MAJOR_VALUE
        | (fields.high_trim << 21)
        | (fields.source_payload << 16)
        | (fields.destination << 11)
        | (fields.rotate << 6)
        | (fields.low_bound << 1)
        | (fields.source_mode == "immediate5")
    )


def fields_for_geometry(
    *,
    destination: int,
    source_payload: int,
    source_mode: str,
    source_lsb: int,
    destination_lsb: int,
    width: int,
) -> BfinsFields:
    """Convert intuitive insert geometry to the hardware endpoint/rotate fields."""

    if not 0 <= source_lsb < 32 or not 0 <= destination_lsb < 32:
        raise ValueError("source and destination bit positions must be in 0..31")
    if not 1 <= width <= 32:
        raise ValueError("width must be in 1..32")
    high_bound = (destination_lsb + width - 1) & 31
    return BfinsFields(
        high_trim=31 - high_bound,
        source_payload=source_payload,
        destination=destination,
        rotate=(destination_lsb - source_lsb) & 31,
        low_bound=destination_lsb,
        source_mode=source_mode,
    )


def execute_bfins(word: int, destination_value: int, source_value: int | None = None) -> int:
    """Execute the characterized scalar equation for one F8-FB word."""

    fields = decode_bfins_word(word)
    if fields.source_mode == "register":
        if source_value is None:
            raise ValueError("register source mode requires source_value")
        source = source_value
    else:
        source = fields.source_payload
    mask = circular_mask(fields.low_bound, fields.high_bound)
    rotated = rotate_left32(source, fields.rotate)
    return ((destination_value & ~mask) | (rotated & mask)) & MASK32
