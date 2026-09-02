"""Totality guarantee: every public ifc_helpers function returns a value (or
None) on degenerate input and NEVER raises.

The drafter prompt and the auto-generated helper reference ADVERTISE this
guarantee ("do not wrap ifc_helpers calls in try/except") — a helper that
starts raising on bad data silently breaks that contract and re-triggers the
defensive except:pass churn the static gate then rejects (observed live:
11 static errors on 8.1.6.5). This suite is the tripwire.
"""

from __future__ import annotations

import pytest

import ifc_helpers
from bnbc.fixtures import synthetic


@pytest.fixture()
def degenerate_setup():
    model, _ctx = synthetic.make_model(units="mm", angle_unit="radian")
    bare_bar = synthetic.make_bare_bar(model, name="no geometry bar")
    return model, bare_bar


#: name -> args builder (model, bare_bar) -> tuple of call args
_CALLS = {
    "unwrap": lambda m, b: (None,),
    "length_unit_to_mm": lambda m, b: (m,),
    "element_label": lambda m, b: (b,),
    "element_storey": lambda m, b: (b,),
    "get_bbox_mm": lambda m, b: (None,),
    "is_stirrup_or_tie": lambda m, b: (b,),
    "get_bar_diameter_mm": lambda m, b: (b,),
    "get_material_fc_fy": lambda m, b: (b,),
    "is_slab_area_reinforcement": lambda m, b: (b,),
    "property_sets": lambda m, b: (b,),
    "geom_settings": lambda m, b: (),
    "get_bar_placement_fast": lambda m, b: (b,),
    "get_bar_directrix_points": lambda m, b: (b,),
    "get_bar_bend_info": lambda m, b: (b,),
    "get_bar_bbox_fast": lambda m, b: (b, 1.0),
    "get_bar_centroid_fast": lambda m, b: (b, 1.0),
}


def test_every_public_helper_is_covered():
    """A new public helper must register a degenerate-input call here."""
    assert set(ifc_helpers.__all__) == set(_CALLS)


@pytest.mark.parametrize("name", sorted(_CALLS))
def test_helper_never_raises_on_degenerate_input(name, degenerate_setup):
    model, bare_bar = degenerate_setup
    fn = getattr(ifc_helpers, name)
    fn(*_CALLS[name](model, bare_bar))  # must not raise
