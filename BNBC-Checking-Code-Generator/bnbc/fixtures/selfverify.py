"""Post-write self-verification of fixture files.

Every operator emits machine-checkable ``expected`` records
(:class:`~bnbc.fixtures.operators.base.OperatorResult`). After the perturbed
model is written to disk, :func:`verify_file` re-opens it and independently
re-measures every record with ``bnbc.fixtures.measure`` — never
``ifc_helpers``, so a helper bug cannot silently agree with a fixture bug.
A mismatch is a hard build failure (:func:`verify_or_raise`), never a warning.
"""

from __future__ import annotations

import math
from typing import Any

import ifcopenshell

from bnbc.contracts import SelfVerification
from bnbc.fixtures import measure
from bnbc.fixtures.errors import SelfVerificationError


def _approx(measured: float | None, expected: float, tol: float) -> bool:
    return measured is not None and math.isfinite(measured) and abs(measured - expected) <= tol


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.3f}"
    if isinstance(value, (list, tuple)):
        return "(" + ", ".join(_fmt(v) for v in value) + ")"
    return str(value)


def _check_terminal_tail(model, rec) -> tuple[str, str, bool]:
    el = measure.element_by_guid(model, rec["guid"])
    hook = measure.terminal_hook(el, end=rec.get("end", "end")) if el is not None else None
    measured = hook.tail_mm if hook else None
    return (
        f"tail={_fmt(measured)} mm",
        f"tail={_fmt(rec['value_mm'])} mm (tol {rec.get('tol_mm', 0.5)})",
        _approx(measured, float(rec["value_mm"]), float(rec.get("tol_mm", 0.5))),
    )


def _check_arc_angle(model, rec) -> tuple[str, str, bool]:
    el = measure.element_by_guid(model, rec["guid"])
    arcs = [s for s in (measure.directrix_segments(el) if el is not None else []) if s.kind == "arc"]
    ordinal = int(rec.get("arc_ordinal", 0))
    measured = arcs[ordinal].angle_deg if 0 <= ordinal < len(arcs) else None
    return (
        f"arc[{ordinal}].angle={_fmt(measured)} deg",
        f"angle={_fmt(rec['value_deg'])} deg (tol {rec.get('tol_deg', 1.0)})",
        _approx(measured, float(rec["value_deg"]), float(rec.get("tol_deg", 1.0))),
    )


def _check_hook(model, rec) -> tuple[str, str, bool]:
    el = measure.element_by_guid(model, rec["guid"])
    hook = measure.terminal_hook(el, end=rec.get("end", "end")) if el is not None else None
    tol_deg = float(rec.get("tol_deg", 1.0))
    tol_mm = float(rec.get("tol_mm", 0.5))
    ok = hook is not None
    parts_m, parts_e = [], []
    for key, attr, tol in (
        ("angle_deg", "angle_deg", tol_deg),
        ("tail_mm", "tail_mm", tol_mm),
        ("radius_mm", "radius_mm", tol_mm),
    ):
        if key not in rec:
            continue
        measured = getattr(hook, attr) if hook is not None else None
        parts_m.append(f"{key}={_fmt(measured)}")
        parts_e.append(f"{key}={_fmt(rec[key])}")
        ok = ok and _approx(measured, float(rec[key]), tol)
    return (
        "hook " + (", ".join(parts_m) if hook else "absent"),
        "hook " + ", ".join(parts_e),
        ok,
    )


def _check_hook_absent(model, rec) -> tuple[str, str, bool]:
    el = measure.element_by_guid(model, rec["guid"])
    hooks = [
        end for end in ("start", "end")
        if el is not None and measure.terminal_hook(el, end=end) is not None
    ]
    return (
        f"terminal hooks at: {hooks or 'none'}",
        "no terminal hook at either end",
        el is not None and not hooks,
    )


def _check_placement_origin(model, rec) -> tuple[str, str, bool]:
    el = measure.element_by_guid(model, rec["guid"])
    origin = measure.placement_origin_mm(el) if el is not None else None
    exp = [float(v) for v in rec["value_mm"]]
    tol = float(rec.get("tol_mm", 0.1))
    ok = origin is not None and all(_approx(origin[i], exp[i], tol) for i in range(3))
    return (f"origin={_fmt(origin)} mm", f"origin={_fmt(exp)} mm (tol {tol})", ok)


def _check_guid_present(model, rec) -> tuple[str, str, bool]:
    el = measure.element_by_guid(model, rec["guid"])
    want_class = rec.get("ifc_class")
    ok = el is not None and (want_class is None or el.is_a(want_class))
    return (
        f"guid {rec['guid']}: {'present as ' + el.is_a() if el is not None else 'absent'}",
        "guid present" + (f" as {want_class}" if want_class else ""),
        ok,
    )


def _check_guid_absent(model, rec) -> tuple[str, str, bool]:
    el = measure.element_by_guid(model, rec["guid"])
    return (
        f"guid {rec['guid']}: {'present' if el is not None else 'absent'}",
        "guid absent",
        el is None,
    )


def _check_type_count(model, rec) -> tuple[str, str, bool]:
    count = len(model.by_type(rec["ifc_class"]))
    return (
        f"{rec['ifc_class']} count={count}",
        f"count={int(rec['value'])}",
        count == int(rec["value"]),
    )


def _check_attribute(model, rec) -> tuple[str, str, bool]:
    el = measure.element_by_guid(model, rec["guid"])
    measured = measure.unwrap(getattr(el, rec["name"], None)) if el is not None else "<no element>"
    expected = rec.get("value")
    if isinstance(expected, float) and isinstance(measured, (int, float)):
        ok = _approx(float(measured), expected, float(rec.get("tol", 1e-6)))
    else:
        ok = measured == expected
    return (f"{rec['name']}={_fmt(measured)}", f"{rec['name']}={_fmt(expected)}", ok)


def _check_pset_absent(model, rec) -> tuple[str, str, bool]:
    el = measure.element_by_guid(model, rec["guid"])
    names = measure.pset_names(el) if el is not None else []
    want = rec.get("name", "*")
    ok = not names if want == "*" else want not in names
    return (f"psets={names}", f"pset {want} absent", ok)


def _check_pset_value(model, rec) -> tuple[str, str, bool]:
    el = measure.element_by_guid(model, rec["guid"])
    measured = (
        measure.pset_value(el, rec["pset_name"], rec["name"]) if el is not None else None
    )
    expected = rec.get("value")
    label = f"{rec['pset_name']}.{rec['name']}"
    if isinstance(expected, (int, float)) and not isinstance(expected, bool) \
            and isinstance(measured, (int, float)):
        ok = _approx(float(measured), float(expected), float(rec.get("tol", 1e-6)))
    else:
        ok = measured == expected
    return (f"{label}={_fmt(measured)}", f"{label}={_fmt(expected)}", ok)


def _check_diameter(model, rec) -> tuple[str, str, bool]:
    el = measure.element_by_guid(model, rec["guid"])
    measured = measure.bar_diameter_mm(el) if el is not None else None
    return (
        f"diameter={_fmt(measured)} mm",
        f"diameter={_fmt(rec['value_mm'])} mm (tol {rec.get('tol_mm', 0.01)})",
        _approx(measured, float(rec["value_mm"]), float(rec.get("tol_mm", 0.01))),
    )


def _check_material_name(model, rec) -> tuple[str, str, bool]:
    el = measure.element_by_guid(model, rec["guid"])
    names = measure.element_material_names(el) if el is not None else []
    return (f"materials={names}", f"material {rec['value']!r} present", rec["value"] in names)


def _check_profile_dims(model, rec) -> tuple[str, str, bool]:
    el = measure.element_by_guid(model, rec["guid"])
    dims = measure.profile_dims_mm(el) if el is not None else None
    tol = float(rec.get("tol_mm", 0.1))
    ok = dims is not None and _approx(dims[0], float(rec["x_mm"]), tol) and \
        _approx(dims[1], float(rec["y_mm"]), tol)
    return (
        f"profile={_fmt(dims)} mm",
        f"profile=({_fmt(float(rec['x_mm']))}, {_fmt(float(rec['y_mm']))}) mm (tol {tol})",
        ok,
    )


_CHECKS = {
    "terminal_tail": _check_terminal_tail,
    "arc_angle": _check_arc_angle,
    "hook": _check_hook,
    "hook_absent": _check_hook_absent,
    "placement_origin": _check_placement_origin,
    "guid_present": _check_guid_present,
    "guid_absent": _check_guid_absent,
    "type_count": _check_type_count,
    "attribute": _check_attribute,
    "pset_absent": _check_pset_absent,
    "pset_value": _check_pset_value,
    "diameter": _check_diameter,
    "material_name": _check_material_name,
    "profile_dims": _check_profile_dims,
}


def verify_file(path, expected_records: list[dict[str, Any]]) -> list[SelfVerification]:
    """Re-open ``path`` and independently re-measure every expected record."""
    model = ifcopenshell.open(str(path))
    results: list[SelfVerification] = []
    for rec in expected_records:
        kind = rec.get("check", "")
        fn = _CHECKS.get(kind)
        if fn is None:
            results.append(SelfVerification(
                measured=f"<no verifier for check kind {kind!r}>",
                expected=str(rec), ok=False,
            ))
            continue
        try:
            measured, expected, ok = fn(model, rec)
        except Exception as exc:
            measured, expected, ok = f"<verifier error: {exc}>", str(rec), False
        results.append(SelfVerification(measured=measured, expected=expected, ok=ok))
    return results


def verify_or_raise(path, expected_records: list[dict[str, Any]]) -> list[SelfVerification]:
    """:func:`verify_file`, but any mismatch raises :class:`SelfVerificationError`."""
    results = verify_file(path, expected_records)
    failures = [r for r in results if not r.ok]
    if failures:
        detail = "; ".join(f"expected {r.expected} but measured {r.measured}" for r in failures)
        raise SelfVerificationError(
            f"fixture {path} failed self-verification: {detail}", verification=failures
        )
    return results
