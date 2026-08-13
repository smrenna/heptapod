#!/usr/bin/env python3
"""
# test_mcm_chain.py is a part of the HEPTAPOD package.
# Copyright (C) 2025 HEPTAPOD authors (see AUTHORS for details).
# HEPTAPOD is licensed under the GNU GPL v3 or later, see LICENSE for details.
# Please respect the MCnet Guidelines, see GUIDELINES for details.

Tests for MCMChainTool and its parsing/chain-walking helpers.

Fixtures under tests/fixtures/ are real get_setup responses fetched from
McM's public REST API for the TTto2L2Nu_HT-500_NJet-7 central chain (GEN-SIM
-> DIGI-RECO -> MiniAOD -> NanoAODv12), captured 2026-08-12. No network
access is required to run these tests -- everything McM/DAS-facing is
substituted with fixture text or a fake resolver.

Run with:
    python test_mcm_chain.py
"""

import sys
import json
from pathlib import Path

# Path setup
SCRIPT_PATH = Path(__file__).resolve()
CENTRAL_PROD_DIR = SCRIPT_PATH.parent           # .../heptapod/tools/central_production
TOOLS_DIR = CENTRAL_PROD_DIR.parent             # .../heptapod/tools
REPO_ROOT = TOOLS_DIR.parent                    # .../heptapod
sys.path.insert(0, str(REPO_ROOT))

from tools.central_production.mcm_chain import (
    MCMChainTool,
    _parse_mcm_setup_script,
    _resolve_parent_prepid,
    _walk_and_parse_chain,
)

FIXTURES = CENTRAL_PROD_DIR / "tests" / "fixtures"

GENSIM_PREPID = "TOP-Run3Summer22EEwmLHEGS-00001"
DRPREMIX_PREPID = "TOP-Run3Summer22EEDRPremix-00001"
MINI_PREPID = "TOP-Run3Summer22EEMiniAODv4-00001"
NANO_PREPID = "TOP-Run3Summer22EENanoAODv12-00001"

# The dbs: dataset strings actually embedded in the mini/nano fixtures'
# --filein flags (verified against live McM 2026-08-12).
MINI_INPUT_DATASET = (
    "/TTto2L2Nu_HT-500_NJet-7_TuneCP5_13p6TeV_powheg-pythia8/"
    "Run3Summer22EEDRPremix-124X_mcRun3_2022_realistic_postEE_v1-v2/AODSIM"
)
NANO_INPUT_DATASET = (
    "/TTto2L2Nu_HT-500_NJet-7_TuneCP5_13p6TeV_powheg-pythia8/"
    "Run3Summer22EEMiniAODv4-130X_mcRun3_2022_realistic_postEE_v6-v1/MINIAODSIM"
)


def _read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_parse_gensim_fragment_step():
    """GEN-SIM: one cmsDriver call, positional fragment arg, no --filein."""
    text = _read_fixture("gensim_setup.sh")
    parsed = _parse_mcm_setup_script(text, GENSIM_PREPID)

    assert parsed["parse_status"] == "ok"
    assert parsed["cmssw_release"] == "CMSSW_12_4_11_patch3"
    assert parsed["scram_arch"] == "el8_amd64_gcc10"
    assert len(parsed["steps"]) == 1

    step = parsed["steps"][0]
    assert step["needs_fragment"] is True
    assert step["fragment_path"] == (
        "Configuration/GenProduction/python/"
        f"{GENSIM_PREPID}-fragment.py"
    )
    assert step["step"] == "LHE,GEN,SIM"
    assert step["conditions"] == "124X_mcRun3_2022_realistic_postEE_v1"
    assert step["filein"] is None
    assert step["requires_grid_proxy"] is False
    print("[OK] test_parse_gensim_fragment_step")


def test_parse_drpremix_two_commands():
    """DIGI-RECO: two cmsDriver calls sharing one prepid (SKILL.md Section 2)."""
    text = _read_fixture("drpremix_setup.sh")
    parsed = _parse_mcm_setup_script(text, DRPREMIX_PREPID)

    assert parsed["parse_status"] == "ok"
    assert len(parsed["steps"]) == 2, "DIGI-RECO must expand into 2 cmsDriver commands"

    digi_step, reco_step = parsed["steps"]

    assert digi_step["step"] == "DIGI,DATAMIX,L1,DIGI2RAW,HLT:2022v14"
    assert digi_step["filein"] == f"file:{GENSIM_PREPID}.root"
    assert digi_step["requires_grid_proxy"] is True, "DIGI step has --pileup_input"
    assert digi_step["pileup_input"] is not None
    assert digi_step["needs_fragment"] is False

    assert reco_step["step"] == "RAW2DIGI,L1Reco,RECO,RECOSIM"
    assert reco_step["filein"] == f"file:{DRPREMIX_PREPID}_0.root"
    assert reco_step["requires_grid_proxy"] is False
    print("[OK] test_parse_drpremix_two_commands")


def test_parse_mini_and_nano_dbs_filein():
    """MiniAOD/NanoAOD: single cmsDriver call, dbs: filein pointing upstream."""
    mini = _parse_mcm_setup_script(_read_fixture("mini_setup.sh"), MINI_PREPID)
    nano = _parse_mcm_setup_script(_read_fixture("nano_setup.sh"), NANO_PREPID)

    assert len(mini["steps"]) == 1
    assert mini["steps"][0]["filein"] == f"dbs:{MINI_INPUT_DATASET}"
    assert mini["steps"][0]["step"] == "PAT"
    assert mini["cmssw_release"] == "CMSSW_13_0_13"

    assert len(nano["steps"]) == 1
    assert nano["steps"][0]["filein"] == f"dbs:{NANO_INPUT_DATASET}"
    assert nano["steps"][0]["step"] == "NANO"
    print("[OK] test_parse_mini_and_nano_dbs_filein")


def test_resolve_parent_prepid_no_filein_is_root():
    parent, warning = _resolve_parent_prepid(None)
    assert parent is None and warning is None
    print("[OK] test_resolve_parent_prepid_no_filein_is_root")


def test_resolve_parent_prepid_local_file():
    # Bare "<PREPID>.root" (GEN-SIM's own output referenced by DIGI-RECO).
    parent, warning = _resolve_parent_prepid(f"file:{GENSIM_PREPID}.root")
    assert parent == GENSIM_PREPID and warning is None

    # "<PREPID>_<N>.root" (RECO command reading DIGI's local output within
    # the same request) -- the trailing "_0" chain-index suffix must be
    # stripped without corrupting the prepid's own hyphenated "-00001".
    parent, warning = _resolve_parent_prepid(f"file:{DRPREMIX_PREPID}_0.root")
    assert parent == DRPREMIX_PREPID and warning is None
    print("[OK] test_resolve_parent_prepid_local_file")


def test_resolve_parent_prepid_dbs():
    calls = []

    def fake_dasgoclient(dataset):
        calls.append(dataset)
        return MINI_PREPID if dataset == MINI_INPUT_DATASET else None

    parent, warning = _resolve_parent_prepid(
        f"dbs:{MINI_INPUT_DATASET}", dasgoclient_fn=fake_dasgoclient
    )
    assert parent == MINI_PREPID and warning is None
    assert calls == [MINI_INPUT_DATASET]

    # Unresolvable dbs: dataset -> stop with a warning, not an exception.
    parent, warning = _resolve_parent_prepid(
        "dbs:/Neutrino_E-10_gun/Run3Summer21PrePremix-.../PREMIX",
        dasgoclient_fn=lambda dataset: None,
    )
    assert parent is None and warning is not None
    print("[OK] test_resolve_parent_prepid_dbs")


def test_walk_and_parse_full_chain():
    """
    End-to-end: starting from the terminal NanoAOD prepid, the walk must
    reconstruct the exact real chain root-first: gensim, drpremix, mini,
    nano -- with 1 + 2 + 1 + 1 = 5 total parsed cmsDriver steps.

    get_setup and dasgoclient are both faked (dict/mapping lookups against
    the four real fixtures) so this needs no network access.
    """
    setup_texts = {
        GENSIM_PREPID: _read_fixture("gensim_setup.sh"),
        DRPREMIX_PREPID: _read_fixture("drpremix_setup.sh"),
        MINI_PREPID: _read_fixture("mini_setup.sh"),
        NANO_PREPID: _read_fixture("nano_setup.sh"),
    }
    # Each dataset resolves to the prepid of the request that *produced* it
    # (not the request that *consumes* it): mini's filein names the AODSIM
    # dataset DRPremix produced; nano's filein names the MINIAODSIM dataset
    # MiniAOD produced.
    dataset_to_prepid = {
        MINI_INPUT_DATASET: DRPREMIX_PREPID,
        NANO_INPUT_DATASET: MINI_PREPID,
    }

    def fake_get_setup(prepid):
        return setup_texts[prepid]

    def fake_resolve_parent(filein):
        return _resolve_parent_prepid(
            filein, dasgoclient_fn=lambda ds: dataset_to_prepid.get(ds)
        )

    chain, warnings = _walk_and_parse_chain(
        NANO_PREPID, get_setup_fn=fake_get_setup, resolve_parent_fn=fake_resolve_parent
    )

    prepids_in_order = [prepid for prepid, _ in chain]
    assert prepids_in_order == [GENSIM_PREPID, DRPREMIX_PREPID, MINI_PREPID, NANO_PREPID], (
        f"Unexpected chain order: {prepids_in_order}"
    )
    assert warnings == [], f"Unexpected warnings on a fully-resolvable chain: {warnings}"

    total_steps = sum(len(parsed["steps"]) for _, parsed in chain)
    assert total_steps == 5, f"Expected 1+2+1+1=5 steps total, got {total_steps}"
    print("[OK] test_walk_and_parse_full_chain")


def test_walk_and_parse_stops_on_unresolvable_parent():
    """A dbs: filein DAS can't resolve should stop the walk with a warning,
    not raise -- the caller still gets everything found so far."""
    setup_texts = {NANO_PREPID: _read_fixture("nano_setup.sh")}

    def fake_get_setup(prepid):
        return setup_texts[prepid]

    def fake_resolve_parent(filein):
        return _resolve_parent_prepid(filein, dasgoclient_fn=lambda ds: None)

    chain, warnings = _walk_and_parse_chain(
        NANO_PREPID, get_setup_fn=fake_get_setup, resolve_parent_fn=fake_resolve_parent
    )
    assert [p for p, _ in chain] == [NANO_PREPID]
    assert len(warnings) == 1 and NANO_PREPID in warnings[0]
    print("[OK] test_walk_and_parse_stops_on_unresolvable_parent")


def test_mcm_chain_tool_end_to_end(tmp_path):
    """Exercise the public Tool interface with mcm_chain's module-level
    network calls monkeypatched -- verifies the JSON envelope shape."""
    import tools.central_production.mcm_chain as mcm_chain_module

    setup_texts = {
        GENSIM_PREPID: _read_fixture("gensim_setup.sh"),
        DRPREMIX_PREPID: _read_fixture("drpremix_setup.sh"),
        MINI_PREPID: _read_fixture("mini_setup.sh"),
        NANO_PREPID: _read_fixture("nano_setup.sh"),
    }
    dataset_to_prepid = {
        "/TTto2L2Nu_HT-500_NJet-7_.../NANOAODSIM": NANO_PREPID,  # tool's own `dataset` input
        NANO_INPUT_DATASET: MINI_PREPID,      # nano's filein -> producer of that dataset
        MINI_INPUT_DATASET: DRPREMIX_PREPID,  # mini's filein -> producer of that dataset
    }

    original_get_setup = mcm_chain_module._mcm_get_setup
    original_dasgoclient = mcm_chain_module._dasgoclient_mcm_prepid
    mcm_chain_module._mcm_get_setup = lambda prepid, timeout=30.0: setup_texts[prepid]
    mcm_chain_module._dasgoclient_mcm_prepid = (
        lambda dataset, timeout=60.0: dataset_to_prepid.get(dataset)
    )
    try:
        tool = MCMChainTool(base_directory=str(tmp_path))
        output = tool.execute(dataset="/TTto2L2Nu_HT-500_NJet-7_.../NANOAODSIM")
        result = json.loads(output)

        assert result["status"] == "ok"
        assert result["schema"] == "mcm-chain-1.0"
        assert result["chain_prepids"] == [
            GENSIM_PREPID, DRPREMIX_PREPID, MINI_PREPID, NANO_PREPID
        ]
        assert len(result["steps"]) == 5

        # No dataset and no request_prepid -> a clean error, not a crash.
        tool2 = MCMChainTool(base_directory=str(tmp_path))
        error_result = json.loads(tool2.execute())
        assert error_result["status"] == "error"
    finally:
        mcm_chain_module._mcm_get_setup = original_get_setup
        mcm_chain_module._dasgoclient_mcm_prepid = original_dasgoclient
    print("[OK] test_mcm_chain_tool_end_to_end")


def main():
    import tempfile

    tests = [
        test_parse_gensim_fragment_step,
        test_parse_drpremix_two_commands,
        test_parse_mini_and_nano_dbs_filein,
        test_resolve_parent_prepid_no_filein_is_root,
        test_resolve_parent_prepid_local_file,
        test_resolve_parent_prepid_dbs,
        test_walk_and_parse_full_chain,
        test_walk_and_parse_stops_on_unresolvable_parent,
    ]

    print("=" * 70)
    print("  MCMChainTool test suite")
    print("=" * 70)

    all_passed = True
    for test in tests:
        try:
            test()
        except Exception as e:
            print(f"[FAIL] {test.__name__}: {e}")
            all_passed = False

    with tempfile.TemporaryDirectory() as tmp_dir:
        try:
            test_mcm_chain_tool_end_to_end(Path(tmp_dir))
        except Exception as e:
            print(f"[FAIL] test_mcm_chain_tool_end_to_end: {e}")
            all_passed = False

    print("=" * 70)
    print("Test suite completed successfully! [OK]" if all_passed
          else "Test suite completed with failures! [FAIL]")
    print("=" * 70)
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
