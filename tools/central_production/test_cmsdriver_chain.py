#!/usr/bin/env python3
"""
# test_cmsdriver_chain.py is a part of the HEPTAPOD package.
# Copyright (C) 2025 HEPTAPOD authors (see AUTHORS for details).
# HEPTAPOD is licensed under the GNU GPL v3 or later, see LICENSE for details.
# Please respect the MCnet Guidelines, see GUIDELINES for details.

Tests for CMSDriverChainTool and its helpers.

Fed from the same real McM get_setup fixtures as test_mcm_chain.py, parsed
through MCMChainTool's own parser, so the input is exactly what the two tools
exchange in practice. No network, no CMSSW.

Run with:
    python test_cmsdriver_chain.py
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve()
CENTRAL_PROD_DIR = SCRIPT_PATH.parent
REPO_ROOT = CENTRAL_PROD_DIR.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools.central_production.mcm_chain import _parse_mcm_setup_script
from tools.central_production.cmsdriver_chain import (
    CMSDriverChainTool,
    _build_cmsdriver_command,
    _build_make_cfgs_script,
    _group_by_release,
    _rewrite_chain,
    _short_tier,
)

FIXTURES = CENTRAL_PROD_DIR / "tests" / "fixtures"


def _real_steps():
    """The four-request TTto2L2Nu chain, parsed from the captured fixtures."""
    steps = []
    for fixture, prepid in [
        ("gensim_setup.sh", "TOP-Run3Summer22EEwmLHEGS-00001"),
        ("drpremix_setup.sh", "TOP-Run3Summer22EEDRPremix-00001"),
        ("mini_setup.sh", "TOP-Run3Summer22EEMiniAODv4-00001"),
        ("nano_setup.sh", "TOP-Run3Summer22EENanoAODv12-00001"),
    ]:
        parsed = _parse_mcm_setup_script((FIXTURES / fixture).read_text(), prepid)
        steps.extend(parsed["steps"])
    return steps


# ====================================================================== #
# ======================= Helper-level tests =========================== #
# ====================================================================== #

def test_short_tier():
    """Output names say what the file is, not which McM request made it."""
    assert _short_tier("LHE,GEN,SIM", 0) == "GS"
    assert _short_tier("DIGI,DATAMIX,L1,DIGI2RAW,HLT:2022v14", 1) == "DR"
    assert _short_tier("RAW2DIGI,L1Reco,RECO,RECOSIM", 2) == "AOD"
    assert _short_tier("PAT", 3) == "MiniAOD"
    assert _short_tier("NANO", 4) == "NanoAOD"
    assert _short_tier("", 7) == "step8"
    print("[OK] test_short_tier")


def test_rewrite_chain_repoints_every_input():
    """Each step reads its predecessor's output; the root reads nothing."""
    steps = _real_steps()
    cfgs, warnings = _rewrite_chain(steps, prefix="MySig", events_per_job=100)

    assert len(cfgs) == len(steps)
    assert cfgs[0]["filein"] is None                # root generates from fragment
    assert cfgs[0]["needs_fragment"] is True
    assert cfgs[0]["events"] == 100
    for prev, cur in zip(cfgs, cfgs[1:]):
        assert cur["filein"] == f"file:{prev['fileout']}", (prev, cur)
        assert cur["events"] == -1                  # consume all input

    # No McM prepid filename and no dbs: dataset survives into the plan.
    for cfg in cfgs:
        assert "TOP-Run3Summer22EE" not in (cfg["filein"] or "")
        assert not (cfg["filein"] or "").startswith("dbs:")

    # Replacing a dbs: input is reported, since nothing is downloaded.
    assert any("dbs:" in w or "NOT downloaded" in w for w in warnings)
    print("[OK] test_rewrite_chain_repoints_every_input")


def test_rewrite_chain_preserves_campaign_conditions():
    """Conditions/beamspot/era must survive verbatim -- that is the whole point."""
    cfgs, _ = _rewrite_chain(_real_steps(), prefix="MySig", events_per_job=50)
    first = cfgs[0]["flags"]
    assert first["conditions"] == "124X_mcRun3_2022_realistic_postEE_v1"
    assert first["beamspot"] == "Realistic25ns13p6TeVEarly2022Collision"
    assert first["era"] == "Run3"

    # The premix step keeps its pileup input, which is what makes it premix.
    premix = [c for c in cfgs if "DATAMIX" in (c["flags"].get("step") or "")]
    assert premix, "no DATAMIX step found in the fixture chain"
    assert "Neutrino_E-10_gun" in str(premix[0]["flags"].get("pileup_input"))
    print("[OK] test_rewrite_chain_preserves_campaign_conditions")


def test_group_by_release_splits_at_boundaries():
    """A release change means a separate area and a separate condor cluster."""
    cfgs, _ = _rewrite_chain(_real_steps(), prefix="MySig", events_per_job=100)
    groups = _group_by_release(cfgs)

    assert len(groups) >= 2, "the real chain changes release at MiniAOD"
    releases = [g["cmssw_release"] for g in groups]
    assert len(set(releases)) == len(releases), "adjacent groups must differ"
    # Every cfg lands in exactly one group, order preserved.
    flat = [c for g in groups for c in g["cfgs"]]
    assert [c["index"] for c in flat] == list(range(len(cfgs)))
    print("[OK] test_group_by_release_splits_at_boundaries")


def test_cmsdriver_command_shape():
    """--no_exec always; McM's own -n/--number_out never leak through."""
    cfgs, _ = _rewrite_chain(_real_steps(), prefix="MySig", events_per_job=100)
    root = _build_cmsdriver_command(cfgs[0], "Configuration/GenProduction/python/frag.py")

    assert root.startswith("cmsDriver.py")
    assert "Configuration/GenProduction/python/frag.py" in root
    assert "--no_exec --mc" in root
    assert "-n 100" in root
    assert "--python_filename MySig_GS_cfg.py" in root
    assert "--fileout file:MySig_GS.root" in root
    assert "--filein" not in root                    # root has no input
    assert "--number_out" not in root

    second = _build_cmsdriver_command(cfgs[1])
    assert "--filein file:MySig_GS.root" in second
    assert "-n -1" in second
    print("[OK] test_cmsdriver_command_shape")


def test_generated_script_is_valid_bash(tmp_path: Path):
    """One release area per group, and the whole thing must parse."""
    cfgs, _ = _rewrite_chain(_real_steps(), prefix="MySig", events_per_job=100)
    groups = _group_by_release(cfgs)
    text = _build_make_cfgs_script(groups, "/work/frag.py", str(tmp_path))

    path = tmp_path / "make_cfgs.sh"
    path.write_text(text)
    completed = subprocess.run(["bash", "-n", str(path)],
                               capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr

    # cmsrel once per release group, not once per step.
    assert text.count("cmsrel ") == len(groups)
    assert "unset PYTHONPATH" in text
    assert "scram b -j 4" in text                    # fragment must be built
    print("[OK] test_generated_script_is_valid_bash")


# ====================================================================== #
# ========================== Tool-level tests ========================== #
# ====================================================================== #

def test_tool_end_to_end(tmp_path: Path):
    """MCMChainTool's steps in, a runnable plan out."""
    tool = CMSDriverChainTool(base_directory=str(tmp_path))
    r = json.loads(tool.execute(
        steps=_real_steps(), prefix="MySig",
        fragment_path=str(tmp_path / "frag.py"), events=100))

    assert r["status"] == "ok"
    assert r["n_steps"] == len(_real_steps())
    assert Path(r["script"]).is_file()
    assert len(r["release_groups"]) >= 2
    # Each group names the file the next stage consumes.
    for group in r["release_groups"]:
        assert group["final_output"].endswith(".root")
    print("[OK] test_tool_end_to_end")


def test_tool_accepts_full_mcm_json(tmp_path: Path):
    """Passing MCMChainTool's whole JSON blob works, not just its steps list."""
    blob = json.dumps({"status": "ok", "schema": "mcm-chain-1.0",
                       "steps": _real_steps()})
    tool = CMSDriverChainTool(base_directory=str(tmp_path))
    r = json.loads(tool.execute(steps=blob, prefix="MySig",
                                fragment_path="/work/frag.py"))
    assert r["status"] == "ok" and r["n_steps"] == len(_real_steps())
    print("[OK] test_tool_accepts_full_mcm_json")


def test_tool_warns_without_fragment(tmp_path: Path):
    """A root step needing a fragment must not silently emit a placeholder."""
    tool = CMSDriverChainTool(base_directory=str(tmp_path))
    r = json.loads(tool.execute(steps=_real_steps(), prefix="MySig"))
    joined = " ".join(r["warnings"])
    assert "fragment" in joined.lower()
    assert "ProductionFilterSequence" in joined      # says WHY editing matters
    print("[OK] test_tool_warns_without_fragment")


def test_tool_rejects_bad_input(tmp_path: Path):
    """Missing or malformed steps are clean errors, not tracebacks."""
    tool = CMSDriverChainTool(base_directory=str(tmp_path))
    assert tool.execute().startswith("Error:")
    assert tool.execute(steps="not json at all").startswith("Error:")
    assert tool.execute(steps=[]).startswith("Error:")
    print("[OK] test_tool_rejects_bad_input")


def main():
    simple = [
        test_short_tier,
        test_rewrite_chain_repoints_every_input,
        test_rewrite_chain_preserves_campaign_conditions,
        test_group_by_release_splits_at_boundaries,
        test_cmsdriver_command_shape,
    ]
    tmp_tests = [
        test_generated_script_is_valid_bash,
        test_tool_end_to_end,
        test_tool_accepts_full_mcm_json,
        test_tool_warns_without_fragment,
        test_tool_rejects_bad_input,
    ]

    print("=" * 70)
    print("  CMSDriverChainTool test suite")
    print("=" * 70)

    ok = True
    for test in simple:
        try:
            test()
        except Exception as e:
            print(f"[FAIL] {test.__name__}: {e}")
            ok = False
    for test in tmp_tests:
        with tempfile.TemporaryDirectory() as d:
            try:
                test(Path(d))
            except Exception as e:
                print(f"[FAIL] {test.__name__}: {e}")
                ok = False

    print("=" * 70)
    print("Test suite completed successfully! [OK]" if ok
          else "Test suite completed with failures! [FAIL]")
    print("=" * 70)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
