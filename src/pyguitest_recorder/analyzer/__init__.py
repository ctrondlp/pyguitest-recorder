"""Grouping raw input into canonical events, and inferring synchronization."""

from .normalize import MODIFIERS, Normalizer, NormalizerOptions
from .sync import SyncOptions, infer_synchronization, strip_inferred

__all__ = [
    "MODIFIERS",
    "Normalizer",
    "NormalizerOptions",
    "SyncOptions",
    "infer_synchronization",
    "strip_inferred",
]
