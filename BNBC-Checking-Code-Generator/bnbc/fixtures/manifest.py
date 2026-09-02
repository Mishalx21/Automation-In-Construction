"""Manifest builder: materialise planned fixtures on disk and self-verify.

For every planned :class:`~bnbc.contracts.FixtureSpec` this module opens the
base model (or builds a synthetic scaffold), applies the operator, writes the
perturbed IFC under the fixtures directory, and runs
:func:`bnbc.fixtures.selfverify.verify_or_raise` on the written file — a
fixture that cannot prove its own perturbation never enters the manifest.

``manifest.json`` (written next to the fixture files) records the spec-card
version, so an unchanged card skips the rebuild entirely.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import ifcopenshell

from bnbc.contracts import (
    FixtureManifest,
    FixtureSpec,
    FixtureStep,
    SelfVerification,
    SpecCard,
    Verdict,
)
from bnbc.fixtures import selfverify, synthetic
from bnbc.fixtures.errors import FixtureBuildError, FixtureError
from bnbc.fixtures.operators import OPERATORS
from bnbc.fixtures.planner import SYNTHETIC_BASE, build_selector

logger = logging.getLogger("bnbc.fixtures.manifest")

MANIFEST_FILENAME = "manifest.json"


def _default_dirs(rule_id: str) -> tuple[Path, Path]:
    from bnbc import config as cfg  # lazy: keep bnbc.fixtures importable without .env side effects

    return cfg.IFC_DIR, cfg.FIXTURES_DIR / rule_id


def _load_existing(manifest_path: Path, spec_version: int) -> Optional[FixtureManifest]:
    if not manifest_path.exists():
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        if int(data.get("spec_card_version", -1)) != spec_version:
            return None
        manifest = FixtureManifest.model_validate(data)
    except Exception as exc:
        logger.warning("Ignoring unreadable manifest %s (%s)", manifest_path, exc)
        return None
    out_dir = manifest_path.parent
    if all((out_dir / f.file_name).exists() for f in manifest.fixtures):
        logger.info("Reusing existing fixture manifest %s (%d fixtures)",
                    manifest_path, len(manifest.fixtures))
        return manifest
    return None


def _reuse_index(manifest_path: Path) -> dict[str, FixtureSpec]:
    """Built fixtures from any previous manifest, keyed by content hash.

    A regenerated card usually changes a handful of sketches; every unchanged
    (base_model, steps) chain already has a built, self-verified file on disk
    — reuse it instead of rebuilding (observed cost: minutes per fixture on
    real models).
    """
    if not manifest_path.exists():
        return {}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = FixtureManifest.model_validate(data)
    except Exception:
        return {}
    out_dir = manifest_path.parent
    return {
        f.content_hash: f
        for f in manifest.fixtures
        if f.content_hash
        and (out_dir / f.file_name).exists()
        and f.self_verification is not None
        and f.self_verification.ok
    }


def _aggregate(verifications: list[SelfVerification]) -> SelfVerification:
    return SelfVerification(
        measured="; ".join(v.measured for v in verifications) or "(nothing measured)",
        expected="; ".join(v.expected for v in verifications) or "(nothing expected)",
        ok=all(v.ok for v in verifications) and bool(verifications),
    )


def _synthetic_scaffold():
    """Minimal mm-unit model with one storey — the synthetic fixture base."""
    model, _context = synthetic.make_model(units="mm", angle_unit="radian")
    synthetic.add_storey(model, name="Level 1")
    return model


def _spec_steps(spec: FixtureSpec) -> list[FixtureStep]:
    """The spec's operator chain (legacy single-op specs normalise to 1 step)."""
    if spec.steps:
        return list(spec.steps)
    params = {k: v for k, v in spec.params.items() if k != "target"}
    return [FixtureStep(operator=spec.operator, params=params,
                        target=dict(spec.params.get("target") or {}))]


def _expectation_key(rec: dict) -> tuple:
    """Dedup key for identical-shaped self-verification records."""
    return (
        rec.get("check", ""), rec.get("guid", ""), rec.get("name", ""),
        rec.get("end", ""), rec.get("ifc_class", ""), rec.get("pset_name", ""),
    )


def _merge_expectations(step_results: list) -> list[dict]:
    """Final expectation set for an operator chain.

    Only the LAST step that affected an element gets to assert about it: an
    earlier step's claims are stale the moment a later step re-perturbs the
    same GUID (e.g. insert a hooked bar with tail 100, then shorten the tail
    to 40 — the insert's tail claim no longer holds on the written file).
    Records about untouched elements, and non-element records, survive with
    same-key dedup (last occurrence wins).
    """
    last_affector: dict[str, int] = {}
    for idx, result in enumerate(step_results):
        for guid in result.affected_guids:
            last_affector[guid] = idx

    final: dict[tuple, dict] = {}
    for idx, result in enumerate(step_results):
        for rec in result.expected:
            guid = rec.get("guid")
            if guid and last_affector.get(guid, idx) > idx:
                continue  # a later step re-perturbed this element
            final[_expectation_key(rec)] = rec
    return list(final.values())


def build_manifest(
    spec_card: SpecCard | dict,
    planned: list[FixtureSpec],
    base_dir: Path | str | None = None,
    out_dir: Path | str | None = None,
) -> FixtureManifest:
    """Materialise ``planned`` on disk, self-verify each file, write manifest.json."""
    card = spec_card if isinstance(spec_card, SpecCard) else SpecCard.model_validate(spec_card)
    default_base_dir, default_out_dir = _default_dirs(card.rule_id)
    base_root = Path(base_dir) if base_dir is not None else default_base_dir
    out = Path(out_dir) if out_dir is not None else default_out_dir
    manifest_path = out / MANIFEST_FILENAME

    existing = _load_existing(manifest_path, card.version)
    if existing is not None:
        return existing

    out.mkdir(parents=True, exist_ok=True)
    reusable = _reuse_index(manifest_path)
    fixtures: list[FixtureSpec] = []
    for spec in planned:
        prior = reusable.get(spec.content_hash) if spec.content_hash else None
        if prior is not None:
            built = spec.model_copy(deep=True)
            built.self_verification = prior.self_verification
            if built.expected_verdict == Verdict.FAIL and not built.expected_elements:
                built.expected_elements = list(prior.expected_elements)
            if built.file_name != prior.file_name:  # sketch order shifted
                import shutil

                shutil.copyfile(out / prior.file_name, out / built.file_name)
            fixtures.append(built)
            logger.info("Reused fixture %s (content unchanged: %s)",
                        built.fixture_id, built.file_name)
            continue

        if spec.base_model == SYNTHETIC_BASE:
            model = _synthetic_scaffold()
        else:
            base_path = base_root / spec.base_model
            if not base_path.exists():
                raise FixtureError(f"{spec.fixture_id}: base model not found: {base_path}")
            model = ifcopenshell.open(str(base_path))

        # Apply the operator chain in order on the same model; later steps see
        # (and may target) elements earlier steps inserted. ANY failure while
        # materialising/verifying ONE fixture is wrapped with the fixture id
        # and its source sketches: the defect is (almost always) in the
        # sketch/convention that authored it, so the pipeline must be able to
        # route it into the bounded plan-repair loop instead of dying.
        try:
            step_results = []
            affected: list[str] = []
            claims: list[str] = []
            for step in _spec_steps(spec):
                operator = OPERATORS[step.operator]()
                selector = build_selector(step.target) if step.target else None
                result = operator.apply(model, selector, **step.params)
                model = result.model
                step_results.append(result)
                affected.extend(g for g in result.affected_guids if g not in affected)
                claims.append(result.claim)

            fixture_path = out / spec.file_name
            model.write(str(fixture_path))
            verifications = selfverify.verify_or_raise(
                fixture_path, _merge_expectations(step_results)
            )
        except FixtureBuildError:
            raise
        except Exception as exc:
            raise FixtureBuildError(
                f"fixture {spec.fixture_id} ({' + '.join(s.operator for s in _spec_steps(spec))}) "
                f"failed to build: {exc}",
                fixture_id=spec.fixture_id,
                sketch_indices=list(spec.source_sketch_indices),
            ) from exc

        built = spec.model_copy(deep=True)
        built.self_verification = _aggregate(verifications)
        if built.expected_verdict == Verdict.FAIL and not built.expected_elements:
            built.expected_elements = affected
        fixtures.append(built)
        logger.info("Built fixture %s: %s", built.fixture_id, " + ".join(claims))

    manifest = FixtureManifest(rule_id=card.rule_id, fixtures=fixtures)
    payload = manifest.model_dump(mode="json")
    payload["spec_card_version"] = card.version
    manifest_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    logger.info("Fixture manifest written: %s (%d fixtures)", manifest_path, len(fixtures))
    return manifest
