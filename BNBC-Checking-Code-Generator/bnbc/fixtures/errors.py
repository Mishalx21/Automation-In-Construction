"""Exceptions for the fixture toolkit."""

from __future__ import annotations


class FixtureError(Exception):
    """Base class for all fixture-toolkit errors."""


class TargetNotFoundError(FixtureError):
    """The requested target element/feature could not be located in the model."""


class UnsupportedGeometryError(FixtureError):
    """The element's geometry idiom is not supported by this operator."""


class SelfVerificationError(FixtureError):
    """Post-write independent measurement did not confirm the claimed change."""

    def __init__(self, message: str, verification=None):
        super().__init__(message)
        self.verification = verification


class FixtureBuildError(FixtureError):
    """One fixture failed to materialise (operator crash, self-verification
    failure, unresolvable target at build time).

    Carries the fixture id and the spec-card sketch indices that produced it,
    so the pipeline can route the failure into the bounded plan-repair loop
    (a spec-authored defect must never be a terminal engine crash — observed
    live: both 8.3.4.2 and 8.1.6.5 were rejected at build time on defects the
    spec model could have fixed with feedback).
    """

    def __init__(self, message: str, fixture_id: str = "",
                 sketch_indices: list[int] | None = None):
        super().__init__(message)
        self.fixture_id = fixture_id
        self.sketch_indices = sketch_indices or []


class PlanningError(FixtureError):
    """A fixture sketch could not be mapped onto the operator vocabulary.

    ``defects`` optionally carries the structured form: a list of
    ``{"sketch_index": int | None, "message": str}`` records (``None`` =
    card-level defect, e.g. a missing coverage minimum). Structured defects
    enable sketch-level repair instead of whole-card regeneration.
    """

    def __init__(self, message: str, defects: list[dict] | None = None):
        super().__init__(message)
        self.defects = defects or []
