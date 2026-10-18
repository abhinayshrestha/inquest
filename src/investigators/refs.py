"""Short references the model uses instead of UUIDs: a type tag and the first 8 hex digits.

Models garble 36-character UUIDs; 11-character refs survive. A ref is only ever resolved
against the records the caller allows (one investigation and environment), so a prefix
collision there is the only ambiguity, and it resolves to nothing rather than a guess.
"""

import re
import uuid
from collections.abc import Iterable

REF_HEX_CHARS = 8
PREFIXES = {"evidence": "ev", "hypothesis": "hy", "finding": "fd"}
_REF = re.compile(r"^\[?\s*([a-z]{2})_([0-9a-f]{8})\s*\]?$")


def ref(kind: str, record_id: uuid.UUID) -> str:
    return f"{PREFIXES[kind]}_{record_id.hex[:REF_HEX_CHARS]}"


def ref_index(kind: str, ids: Iterable[uuid.UUID]) -> dict[str, uuid.UUID | None]:
    """ref -> id; a ref shared by two records maps to None (ambiguous)."""
    index: dict[str, uuid.UUID | None] = {}
    for record_id in ids:
        key = ref(kind, record_id)
        index[key] = None if key in index and index[key] != record_id else record_id
    return index


def resolve(kind: str, text: str, index: dict[str, uuid.UUID | None]) -> uuid.UUID | None:
    match = _REF.match(text.strip().lower())
    if match is None or match.group(1) != PREFIXES[kind]:
        return None
    return index.get(f"{match.group(1)}_{match.group(2)}")
