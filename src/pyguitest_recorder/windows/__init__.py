"""Window and element context for a screen coordinate."""

from .atspi_effects import AtspiEffectLog
from .resolver import ContextResolver, DesktopResolver, NullResolver, Observation

__all__ = [
    "AtspiEffectLog",
    "ContextResolver",
    "DesktopResolver",
    "NullResolver",
    "Observation",
]
