#!/usr/bin/env python3
"""
# test_gridpack_build.py is a part of the HEPTAPOD package.
# Copyright (C) 2025 HEPTAPOD authors (see AUTHORS for details).
# HEPTAPOD is licensed under the GNU GPL v3 or later, see LICENSE for details.
# Please respect the MCnet Guidelines, see GUIDELINES for details.

Tests for GridpackBuildTool and its helpers.

No genproductions checkout, no CMSSW and no network: the tree is faked on
disk, and nothing is executed (run_build defaults to False).

Run with:
    python test_gridpack_build.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools.gridpack.gridpack_build import (
    GridpackBuildTool,
    _build_gridpack_script,
    _build_launch_command,
    _common_launch_dir,
    _container_for_arch,
    _parse_gridpack_log,
    _proc_card_model_path,
    _relative_carddir,
)

PROC_CARD_LOCAL_MODEL = """
# comment line
import model /models/MyBSM_UFO

define p = g u c d s b u~ c~ d~ s~ b~
generate p p > X X~, (X > c mu-), (X~ > c~ mu+)
output MyProc -nojpeg
"""

PROC_CARD_BARE_MODEL = """
import model sm
generate p p > t t~
output MyProc -nojpeg
"""

# Abridged from a real gridpack_generation log: a pilot run with the physics,
# then the grid-packing pass that legitimately reports zero.
LOG_TEXT = """
  === Results Summary for run: pilotrun tag: tag_1 ===

     Cross-section :   8.991e-06 +- 3.811e-08 pb
     Nb of events  :  1000

store_events
  === Results Summary for run: GridRun_234567 tag: tag_1 ===

     Cross-section :   0 +- 0 pb
Creating tarball
"""


def _fake_genproductions(root: Path) -> Path:
    """Minimal tree with the one file the tool probes for."""
    mg5 = root / "genproductions" / "bin" / "MadGraph5_aMCatNLO"
    mg5.mkdir(parents=True)
    (mg5 / "gridpack_generation.sh").write_text("#!/bin/bash\necho fake\n")
    return mg5


def _cards(root: Path, name: str = "MyProc",
           proc_text: str = PROC_CARD_BARE_MODEL) -> Path:
    d = root / "cards" / name
    d.mkdir(parents=True)
    (d / f"{name}_proc_card.dat").write_text(proc_text)
    (d / f"{name}_run_card.dat").write_text("6800.0 = ebeam1\n")
    return d


# ====================================================================== #
# ======================= Helper-level tests =========================== #
# ====================================================================== #

def test_container_for_arch():
    """Arch selects the cmssw-elN wrapper; unknown is refused, not guessed."""
    assert _container_for_arch("el8_amd64_gcc10") == "cmssw-el8"
    assert _container_for_arch("el9_amd64_gcc11") == "cmssw-el9"
    assert _container_for_arch("slc7_amd64_gcc700") == "cmssw-el7"
    assert _container_for_arch("winnt_amd64_gcc99") is None
    assert _container_for_arch("") is None
    print("[OK] test_container_for_arch")


def test_proc_card_model_path():
    """Only an ABSOLUTE import imposes a bind-mount constraint."""
    assert _proc_card_model_path(PROC_CARD_LOCAL_MODEL) == "/models/MyBSM_UFO"
    # A bare name resolves inside the sandboxed MG5 and needs no handling.
    assert _proc_card_model_path(PROC_CARD_BARE_MODEL) is None
    assert _proc_card_model_path("") is None
    print("[OK] test_proc_card_model_path")


def test_common_launch_dir():
    """The container binds only the cwd, so both inputs must sit beneath it."""
    got = _common_launch_dir(["/a/b/genproductions/bin/MadGraph5_aMCatNLO",
                              "/a/b/work/models/MyUFO"])
    assert got == "/a/b", got
    # A single path still yields a directory.
    single = _common_launch_dir(["/a/b/genproductions"])
    assert single.startswith("/a/b")
    print("[OK] test_common_launch_dir")


def test_relative_carddir(tmp_path: Path):
    """Cards inside the tree get a relative path; outside is reported."""
    mg5 = _fake_genproductions(tmp_path)
    inside = mg5 / "cards" / "MyProc"
    inside.mkdir(parents=True)
    rel, err = _relative_carddir(str(inside), str(mg5))
    assert err is None and rel == "cards/MyProc", (rel, err)

    outside = _cards(tmp_path)
    rel, err = _relative_carddir(str(outside), str(mg5))
    assert err == "outside", (rel, err)

    rel, err = _relative_carddir(str(tmp_path / "nope"), str(mg5))
    assert rel is None and "does not exist" in err
    print("[OK] test_relative_carddir")


def test_parse_gridpack_log():
    """The trailing '0 +- 0 pb' grid pass must not be mistaken for the result."""
    parsed = _parse_gridpack_log(LOG_TEXT)
    assert parsed["n_cross_section_reports"] == 2
    assert parsed["cross_section_pb"] == 8.991e-06
    assert abs(parsed["cross_section_fb"] - 8.991e-03) < 1e-12
    assert parsed["cross_section_err_pb"] == 3.811e-08

    # A log with only the zero pass reports no usable cross-section.
    only_zero = _parse_gridpack_log("Cross-section :   0 +- 0 pb")
    assert "cross_section_pb" not in only_zero
    assert _parse_gridpack_log("")["n_cross_section_reports"] == 0
    print("[OK] test_parse_gridpack_log")


def test_build_script_uses_relative_carddir():
    """The script must pass carddir relative; absolute is the classic failure."""
    text = _build_gridpack_script(
        process_name="MyProc", rel_carddir="cards/MyProc",
        genprod_mg5_dir="/a/b/genproductions/bin/MadGraph5_aMCatNLO",
        scram_arch="el8_amd64_gcc10", queue="local",
        launch_dir="/a/b", container="cmssw-el8")
    assert "./gridpack_generation.sh MyProc cards/MyProc local ALL el8_amd64_gcc10" in text
    assert "unset PYTHONPATH" in text
    assert "export SCRAM_ARCH=el8_amd64_gcc10" in text
    # No absolute carddir anywhere in the invocation.
    assert " /a/b/genproductions/bin/MadGraph5_aMCatNLO/cards" not in text
    print("[OK] test_build_script_uses_relative_carddir")


def test_launch_command_cds_to_common_ancestor():
    """The cd is load-bearing: only the cwd is bind-mounted."""
    cmd = _build_launch_command("/a/b/build.sh", "/a/b", "cmssw-el8")
    assert cmd == "cd /a/b && cmssw-el8 --command-to-run bash /a/b/build.sh"
    plain = _build_launch_command("/a/b/build.sh", "/a/b", None)
    assert plain == "cd /a/b && bash /a/b/build.sh"
    print("[OK] test_launch_command_cds_to_common_ancestor")


# ====================================================================== #
# ========================== Tool-level tests ========================== #
# ====================================================================== #

def test_tool_copies_outside_cards_and_goes_relative(tmp_path: Path):
    """Cards outside the tree are copied in and addressed relatively."""
    mg5 = _fake_genproductions(tmp_path)
    cards = _cards(tmp_path)
    tool = GridpackBuildTool(base_directory=str(tmp_path))
    r = json.loads(tool.execute(
        process_name="MyProc", cards_dir=str(cards),
        genproductions_dir=str(tmp_path / "genproductions"),
        scram_arch="el8_amd64_gcc10"))

    assert r["status"] == "ok"
    assert r["executed"] is False              # nothing runs unsolicited
    assert r["relative_carddir"] == "cards/MyProc"
    assert (mg5 / "cards" / "MyProc" / "MyProc_proc_card.dat").is_file()
    assert any("copied into the genproductions tree" in w
               for w in r["warnings"])
    assert r["container"] == "cmssw-el8"
    print("[OK] test_tool_copies_outside_cards_and_goes_relative")


def test_tool_launch_dir_covers_local_model(tmp_path: Path):
    """With an absolute model import, launch from the common ancestor."""
    _fake_genproductions(tmp_path)
    model = tmp_path / "work" / "models" / "MyBSM_UFO"
    model.mkdir(parents=True)
    proc = PROC_CARD_LOCAL_MODEL.replace("/models/MyBSM_UFO", str(model))
    cards = _cards(tmp_path, proc_text=proc)

    tool = GridpackBuildTool(base_directory=str(tmp_path))
    r = json.loads(tool.execute(
        process_name="MyProc", cards_dir=str(cards),
        genproductions_dir=str(tmp_path / "genproductions"),
        scram_arch="el8_amd64_gcc10"))

    assert r["model_path"] == str(model)
    # Must be at or above both genproductions and the model, not inside either.
    launch = Path(r["launch_dir"])
    assert str(model).startswith(str(launch))
    assert str(tmp_path / "genproductions").startswith(str(launch))
    assert any("only the cwd is bind-mounted" in w.lower() or
               "common ancestor" in w for w in r["warnings"])
    print("[OK] test_tool_launch_dir_covers_local_model")


def test_tool_warns_on_missing_model(tmp_path: Path):
    """A model path that does not exist is caught before the build, not during."""
    _fake_genproductions(tmp_path)
    cards = _cards(tmp_path, proc_text=PROC_CARD_LOCAL_MODEL)  # /models/... absent
    tool = GridpackBuildTool(base_directory=str(tmp_path))
    r = json.loads(tool.execute(
        process_name="MyProc", cards_dir=str(cards),
        genproductions_dir=str(tmp_path / "genproductions"),
        scram_arch="el8_amd64_gcc10"))
    assert any("does not exist" in w for w in r["warnings"])
    print("[OK] test_tool_warns_on_missing_model")


def test_launch_dir_includes_genproductions_root(tmp_path: Path):
    """The build reads Utilities/ and MetaData/ ABOVE bin/MadGraph5_aMCatNLO."""
    mg5 = _fake_genproductions(tmp_path)
    root = tmp_path / "genproductions"
    (root / "Utilities" / "scripts").mkdir(parents=True)
    (root / "MetaData").mkdir()
    cards = _cards(tmp_path)

    tool = GridpackBuildTool(base_directory=str(tmp_path))
    r = json.loads(tool.execute(
        process_name="MyProc", cards_dir=str(cards),
        genproductions_dir=str(root), scram_arch="el8_amd64_gcc10"))

    launch = Path(r["launch_dir"]).resolve()
    # Both helper trees must be visible from the launch dir, or the
    # $DEFAULT_PDF_SETS substitution silently degrades.
    assert str((root / "Utilities").resolve()).startswith(str(launch))
    assert str((root / "MetaData").resolve()).startswith(str(launch))
    assert str(mg5.resolve()).startswith(str(launch))
    print("[OK] test_launch_dir_includes_genproductions_root")


def test_launch_dir_covers_the_script_itself(tmp_path: Path):
    """The container must be able to read the script it is told to run."""
    root = tmp_path / "genproductions"
    _fake_genproductions(tmp_path)
    cards = _cards(tmp_path)
    build = tmp_path / "elsewhere" / "work"      # deliberately NOT under root

    tool = GridpackBuildTool(base_directory=str(tmp_path))
    r = json.loads(tool.execute(
        process_name="MyProc", cards_dir=str(cards),
        genproductions_dir=str(root), scram_arch="el8_amd64_gcc10",
        build_dir=str(build)))

    launch = Path(r["launch_dir"]).resolve()
    assert str(Path(r["script"]).resolve()).startswith(str(launch)), \
        "script written outside the bind mount would be invisible in the container"
    assert str(root.resolve()).startswith(str(launch))
    print("[OK] test_launch_dir_covers_the_script_itself")


def test_tool_rejects_bad_inputs(tmp_path: Path):
    """Unknown arch, absent genproductions, and missing fields are hard errors."""
    _fake_genproductions(tmp_path)
    cards = _cards(tmp_path)
    tool = GridpackBuildTool(base_directory=str(tmp_path))

    out = tool.execute(process_name="MyProc", cards_dir=str(cards),
                       genproductions_dir=str(tmp_path / "genproductions"),
                       scram_arch="winnt_amd64_gcc99")
    assert out.startswith("Error:") and "SCRAM_ARCH" in out

    out = tool.execute(process_name="MyProc", cards_dir=str(cards),
                       genproductions_dir=str(tmp_path / "nothing_here"))
    assert out.startswith("Error:") and "genproductions not found" in out

    out = tool.execute(process_name="MyProc")
    assert out.startswith("Error:") and "Missing required input" in out
    print("[OK] test_tool_rejects_bad_inputs")


def test_generated_script_is_valid_bash(tmp_path: Path):
    """The emitted build script must at least parse."""
    _fake_genproductions(tmp_path)
    cards = _cards(tmp_path)
    tool = GridpackBuildTool(base_directory=str(tmp_path))
    r = json.loads(tool.execute(
        process_name="MyProc", cards_dir=str(cards),
        genproductions_dir=str(tmp_path / "genproductions"),
        scram_arch="el8_amd64_gcc10"))
    completed = subprocess.run(["bash", "-n", r["script"]],
                               capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert os.access(r["script"], os.X_OK)
    print("[OK] test_generated_script_is_valid_bash")


def test_verify_only_mode(tmp_path: Path):
    """A finished build can be checked from its log without rebuilding."""
    log = tmp_path / "gp.log"
    log.write_text(LOG_TEXT)
    tool = GridpackBuildTool(base_directory=str(tmp_path))
    r = json.loads(tool.execute(verify_log=str(log)))
    assert r["cross_section_pb"] == 8.991e-06
    assert "cross_section_fb" in r

    empty = tmp_path / "empty.log"
    empty.write_text("nothing here")
    r2 = json.loads(tool.execute(verify_log=str(empty)))
    assert "warning" in r2
    print("[OK] test_verify_only_mode")


def main():
    simple = [
        test_container_for_arch,
        test_proc_card_model_path,
        test_common_launch_dir,
        test_parse_gridpack_log,
        test_build_script_uses_relative_carddir,
        test_launch_command_cds_to_common_ancestor,
    ]
    tmp_tests = [
        test_relative_carddir,
        test_tool_copies_outside_cards_and_goes_relative,
        test_tool_launch_dir_covers_local_model,
        test_tool_warns_on_missing_model,
        test_launch_dir_includes_genproductions_root,
        test_launch_dir_covers_the_script_itself,
        test_tool_rejects_bad_inputs,
        test_generated_script_is_valid_bash,
        test_verify_only_mode,
    ]

    print("=" * 70)
    print("  GridpackBuildTool test suite")
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
