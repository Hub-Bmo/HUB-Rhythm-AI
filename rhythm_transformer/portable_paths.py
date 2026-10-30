"""Resolve historical Windows provenance paths strictly inside this handoff."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def resolve_reference(value):
    if not value:
        raise ValueError("Historical reference is missing")
    normalized = str(value).replace("\\", "/")
    direct = Path(normalized)
    candidates = [direct] if direct.is_absolute() else [ROOT / direct]
    for marker in ("outputs/", "projects/", "data/", "reproduced_runs/"):
        if marker in normalized:
            candidates.append(ROOT / normalized[normalized.index(marker):])
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate.is_relative_to(ROOT) and candidate.exists():
            return candidate
    raise FileNotFoundError(f"Packaged reference not found: {value}")
