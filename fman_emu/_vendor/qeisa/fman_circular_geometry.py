"""Inclusive circular bit interval shared by FMan bitfield formats."""

MASK32 = 0xFFFF_FFFF


def circular_mask(low: int, high: int) -> int:
    if not 0 <= low < 32 or not 0 <= high < 32:
        raise ValueError("circular-mask bounds must be in 0..31")
    if low <= high:
        return (((1 << (high - low + 1)) - 1) << low) & MASK32
    return (((1 << (high + 1)) - 1) | (MASK32 ^ ((1 << low) - 1))) & MASK32


def circular_mask_from_word(word: int) -> int:
    """Decode the common high-trim/low-bound fields, not the operation."""
    return circular_mask((word >> 1) & 31, 31 - ((word >> 21) & 31))
