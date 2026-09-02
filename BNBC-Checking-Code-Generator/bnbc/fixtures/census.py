"""Plan-time census of base models: which IFC classes exist, how many, and
which element names — so a dangling fixture target fails at PLAN time
(seconds, with a precise defect) instead of at BUILD time (minutes into
materialising other fixtures).

The census is a best-effort validator input: if a model cannot be opened or
censused, validation is skipped for it — the build-time errors still backstop.
Cached as ``<model>.census.json`` next to the model, keyed on file size+mtime.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("bnbc.fixtures.census")

#: Names kept per class (dedup'd); enough for name_contains checks without
#: ballooning the cache on 4000-element models.
MAX_NAMES_PER_CLASS = 5000


def census_path(model_path: Path) -> Path:
    return model_path.with_name(model_path.name + ".census.json")


def _stamp(model_path: Path) -> dict[str, Any]:
    stat = model_path.stat()
    return {"size": stat.st_size, "mtime": int(stat.st_mtime)}


def build_census(model_path: Path) -> dict[str, Any]:
    """{ifc_class: {"count": int, "names": [unique names]}} for IfcProducts."""
    import ifcopenshell  # lazy

    model = ifcopenshell.open(str(model_path))
    classes: dict[str, dict[str, Any]] = {}
    for el in model.by_type("IfcProduct"):
        cls = el.is_a()
        bucket = classes.setdefault(cls, {"count": 0, "names": []})
        bucket["count"] += 1
        name = str(getattr(el, "Name", "") or "")
        if name and len(bucket["names"]) < MAX_NAMES_PER_CLASS:
            bucket["names"].append(name)
    for bucket in classes.values():
        bucket["names"] = sorted(set(bucket["names"]))
    return classes


def build_digest(model_path: Path, ifc_classes: list[str]) -> dict[str, Any]:
    """Sample-element digest: for each class, one element's set attributes and
    pset names/keys — the "look at the data before writing code" step,
    mechanised (the Code-Agent skill forced this via scratch scripts; here the
    pipeline computes it once and injects it into the draft prompt)."""
    import ifcopenshell  # lazy
    import ifcopenshell.util.element as element_util

    model = ifcopenshell.open(str(model_path))
    digest: dict[str, Any] = {"classes": {}}
    try:
        from bnbc.fixtures import measure

        digest["unit_scale_mm"] = measure.unit_scale_mm(model)
    except Exception:
        digest["unit_scale_mm"] = None
    for cls in ifc_classes:
        try:
            elements = model.by_type(cls)
        except Exception:
            continue
        if not elements:
            continue
        el = elements[0]
        attrs: dict[str, Any] = {}
        try:
            for key, value in el.get_info(recursive=False).items():
                if key in ("id", "type", "OwnerHistory") or value is None:
                    continue
                if isinstance(value, (str, int, float, bool)):
                    attrs[key] = value if not isinstance(value, str) else value[:60]
                elif hasattr(value, "wrappedValue"):
                    attrs[key] = value.wrappedValue
                elif hasattr(value, "is_a"):
                    attrs[key] = f"<{value.is_a()}>"
                elif isinstance(value, (tuple, list)):
                    attrs[key] = f"<{len(value)} item(s)>"
        except Exception:
            pass
        psets: dict[str, list[str]] = {}
        try:
            for pname, props in (element_util.get_psets(el) or {}).items():
                if isinstance(props, dict):
                    psets[pname] = sorted(k for k in props.keys() if k != "id")[:15]
        except Exception:
            pass
        digest["classes"][cls] = {
            "count": len(elements),
            "sample_attributes": attrs,
            "sample_psets": psets,
        }
    return digest


def load_digest(model_path: Path | str, ifc_classes: list[str]) -> Optional[dict[str, Any]]:
    """Cached sample-element digest for one model, or None when unavailable."""
    path = Path(model_path)
    if not path.exists() or not ifc_classes:
        return None
    key = ",".join(sorted(set(ifc_classes)))
    cache = path.with_name(path.name + ".digest.json")
    try:
        stamp = _stamp(path)
        if cache.exists():
            payload = json.loads(cache.read_text(encoding="utf-8"))
            if payload.get("stamp") == stamp and payload.get("key") == key:
                return payload["digest"]
        digest = build_digest(path, sorted(set(ifc_classes)))
        try:
            cache.write_text(
                json.dumps({"stamp": stamp, "key": key, "digest": digest}),
                encoding="utf-8",
            )
        except OSError:
            pass
        return digest
    except Exception as exc:
        logger.warning("Digest unavailable for %s (%s)", path.name, exc)
        return None


def load_census(model_path: Path | str) -> Optional[dict[str, Any]]:
    """Cached census for one model, or None when unavailable (skip validation)."""
    path = Path(model_path)
    if not path.exists():
        return None
    cache = census_path(path)
    try:
        stamp = _stamp(path)
        if cache.exists():
            payload = json.loads(cache.read_text(encoding="utf-8"))
            if payload.get("stamp") == stamp:
                return payload["classes"]
        classes = build_census(path)
        try:
            cache.write_text(
                json.dumps({"stamp": stamp, "classes": classes}), encoding="utf-8"
            )
        except OSError:  # read-only dir etc. — census still usable this run
            pass
        logger.info("Censused %s: %d product classes", path.name, len(classes))
        return classes
    except Exception as exc:
        logger.warning("Census unavailable for %s (%s) — target validation skipped",
                       path.name, exc)
        return None
