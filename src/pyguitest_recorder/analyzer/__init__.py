"""Grouping raw input into canonical events, and inferring synchronization."""

from .normalize import (
    MODIFIERS,
    Normalizer,
    NormalizerOptions,
    chord_matches,
    parse_chord,
)
from .sync import SyncOptions, infer_synchronization, strip_inferred

__all__ = [
    "MODIFIERS",
    "Normalizer",
    "NormalizerOptions",
    "chord_matches",
    "parse_chord",
    "SyncOptions",
    "infer_synchronization",
    "strip_inferred",
]
