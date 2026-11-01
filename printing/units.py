"""Physical units recovered from the original FDA/FDU serializer and renderer."""
from __future__ import annotations

from dataclasses import asdict, dataclass

from tools.patterns.fvd_pattern import MM_PER_POSTSCRIPT_POINT, POSTSCRIPT_POINT_NAME, Pattern


class UnitResolutionError(ValueError):
    pass


@dataclass(frozen=True)
class UnitResolution:
    mm_per_source_unit: float
    source_unit: str
    automatic: bool
    serializer_version: int
    evidence: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


def resolve_pattern_units(pattern: Pattern) -> UnitResolution:
    """Resolve supported native pattern units without guessing from dimensions.

    This applies only to parsed original-format patterns. Synthetic geometry and
    other import formats must declare their own units. Incomplete drawings stay
    blocked even though their decoded point subset uses the same native unit.
    """
    if not isinstance(pattern, Pattern):
        raise UnitResolutionError("Automatic units require a parsed FDA/FDU pattern")
    if pattern.serializer_version not in (23, 30) or pattern.family not in (1, 2, 4):
        raise UnitResolutionError("This native pattern version/family has no verified unit conversion")
    if not pattern.complete_geometry:
        raise UnitResolutionError("This pattern has incomplete geometry and cannot be prepared for cutting")
    if not pattern.paths:
        raise UnitResolutionError("This pattern has no decoded vector paths")
    return UnitResolution(
        mm_per_source_unit=MM_PER_POSTSCRIPT_POINT,
        source_unit=POSTSCRIPT_POINT_NAME,
        automatic=True,
        serializer_version=pattern.serializer_version,
        evidence=("native-TTrajecte-Llegir-IDs-3-and-63",
                  "native-FD_GetPatternSize-divides-by-72",
                  "native-EPStoPLT-multiplies-by-25.4-over-72",
                  "original-UI-dimension-corroboration"),
    )
