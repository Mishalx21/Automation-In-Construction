"""bnbc.fixtures — deterministic fixture engine for FIV (Fixture-Injected
Verification).

Layers (each importable on its own):

* ``synthetic``  — programmatic micro-model builders (shared with tests)
* ``operators``  — registered, pure, self-describing perturbation operators
* ``measure``    — independent measurement oracle (never imports ifc_helpers)
* ``selfverify`` — post-write re-measurement of every operator claim
* ``planner``    — spec-card sketches -> validated FixtureSpecs
* ``manifest``   — materialise fixtures on disk + manifest.json
* ``gate``       — the acceptance decision (the only source of accepted=True)
"""

