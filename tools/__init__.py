"""
# __init__.py is a part of the HEPTAPOD package.
# Copyright (C) 2025 HEPTAPOD authors (see AUTHORS for details).
# HEPTAPOD is licensed under the GNU GPL v3 or later, see LICENSE for details.
# Please respect the MCnet Guidelines, see GUIDELINES for details.
"""
"""HEP event generator and analysis tools.

Sub-packages are imported defensively so that a subset install
(e.g. `tb install heptapod[inspire,pdg]`) — which omits heavy bundle deps
like pythia8mc, sherpa-mc, sympy, feyngraph — can still load this package.
A failing sub-package is silently dropped; its tools simply do not surface
at serve time.
"""

# Each sub-package is wrapped: if its top-level deps are missing, the
# sub-package fails to import and we keep going. Toolbase's per-tool import
# skip handles the per-tool surfacing.
for _name in (
    "analysis",
    "central_production",
    "eda",
    "feyngraph",
    "feynrules",
    "inspire",
    "logging",
    "mg5",
    "nda",
    "pdg",
    "pythia",
    "sherpa",
    "units",
):
    try:
        __import__(f"{__name__}.{_name}")
    except ImportError:
        pass
del _name
