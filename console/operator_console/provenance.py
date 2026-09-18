"""Every fact this screen displays carries where it came from.

The project's discipline is that a claim without a label is not a claim. The
screen applies the same four labels the documents use, and it applies them per
displayed value rather than per page, because one panel routinely mixes a value
read out of the journal file with a value inferred from it.

- ``SOURCE``     read from a pinned source or a specification.
- ``OBSERVED``   read from an artifact this process actually opened.
- ``INFERRED``   engineering judgment applied to something observed.
- ``UNVERIFIED`` not established; shown so its absence is visible.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Provenance(str, Enum):
    SOURCE = "SOURCE"
    OBSERVED = "OBSERVED"
    INFERRED = "INFERRED"
    UNVERIFIED = "UNVERIFIED"


@dataclass(frozen=True)
class Fact:
    """A displayable value that cannot exist without its provenance.

    ``origin`` names the artifact and, where it helps, the column or key the
    value came from, so a reader can go and check it. A fact with no origin is
    a construction error rather than a rendering choice.
    """

    label: str
    value: str
    provenance: Provenance
    origin: str
    note: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.provenance, Provenance):
            raise ValueError(f"provenance must be a Provenance, not {self.provenance!r}")
        if not self.label:
            raise ValueError("a fact must be labelled")
        if not self.origin:
            raise ValueError(f"fact {self.label!r} has no origin; every displayed fact names one")


def observed(label: str, value: object, origin: str, note: str = "") -> Fact:
    return Fact(label, _text(value), Provenance.OBSERVED, origin, note)


def inferred(label: str, value: object, origin: str, note: str = "") -> Fact:
    return Fact(label, _text(value), Provenance.INFERRED, origin, note)


def unverified(label: str, value: object, origin: str, note: str = "") -> Fact:
    return Fact(label, _text(value), Provenance.UNVERIFIED, origin, note)


def source(label: str, value: object, origin: str, note: str = "") -> Fact:
    return Fact(label, _text(value), Provenance.SOURCE, origin, note)


def _text(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)
