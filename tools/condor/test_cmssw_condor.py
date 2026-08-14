#!/usr/bin/env python3
"""
# test_cmssw_condor.py is a part of the HEPTAPOD package.
# Copyright (C) 2025 HEPTAPOD authors (see AUTHORS for details).
# HEPTAPOD is licensed under the GNU GPL v3 or later, see LICENSE for details.
# Please respect the MCnet Guidelines, see GUIDELINES for details.

Tests for CMSSWJobSubmitTool, CondorJobStatusTool, and their helpers.

No HTCondor installation and no network access is required: condor_submit and
condor_q are substituted where they would be invoked, and every generated
artifact is checked as text.

Run with:
    python test_cmssw_condor.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve()
CONDOR_DIR = SCRIPT_PATH.parent                 # .../heptapod/tools/condor
TOOLS_DIR = CONDOR_DIR.parent                   # .../heptapod/tools
REPO_ROOT = TOOLS_DIR.parent                    # .../heptapod
sys.path.insert(0, str(REPO_ROOT))

from tools.condor import cmssw_condor as mod
from tools.condor.cmssw_condor import (
    CMSSWJobSubmitTool,
    CondorJobStatusTool,
    _build_jdl,
    _build_wrapper,
    _check_submit_dir,
    _image_for_arch,
    _parse_cluster_id,
    _scan_unreachable_inputs,
)

# A cfg stub with the shape the checks care about: an absolute gridpack path
# on a filesystem the worker cannot see.
CFG_WITH_LOCAL_GRIDPACK = """
import FWCore.ParameterSet.Config as cms
process = cms.Process('GEN')
process.externalLHEProducer = cms.EDProducer('ExternalLHEProducer',
  args = cms.vstring('/uscms_data/d2/someone/gridpacks/model_tarball.tar.xz'),
  numberOfParameters = cms.uint32(1),
)
process.source = cms.Source("EmptySource")
"""

CFG_CLEAN = """
import FWCore.ParameterSet.Config as cms
process = cms.Process('GEN')
process.source = cms.Source("EmptySource")
"""


def _write_cfg(tmp: Path, text: str = CFG_CLEAN, name: str = "step1_cfg.py") -> Path:
    path = tmp / name
    path.write_text(text)
    return path


# ====================================================================== #
# ======================= Helper-level tests =========================== #
# ====================================================================== #

def test_image_for_arch():
    """SCRAM_ARCH prefix selects the container image; unknown arch is not guessed."""
    image, os_tag = _image_for_arch("el8_amd64_gcc10")
    assert image.endswith("cms:rhel8"), image
    assert os_tag == "EL8"

    image, os_tag = _image_for_arch("el9_amd64_gcc11")
    assert image.endswith("cms:rhel9"), image
    assert os_tag == "EL9"

    assert _image_for_arch("slc7_amd64_gcc700")[0].endswith("cms:rhel7")

    # Unknown and empty both refuse rather than defaulting.
    assert _image_for_arch("winnt_amd64_gcc99") == (None, None)
    assert _image_for_arch("") == (None, None)
    print("[OK] test_image_for_arch")


def test_check_submit_dir_rejects_node_local_paths():
    """The schedd is a different host, so a /tmp submit dir cannot be read."""
    assert _check_submit_dir("/tmp/jobs") is not None
    assert _check_submit_dir("/var/tmp/jobs") is not None
    assert _check_submit_dir("/dev/shm/jobs") is not None

    # Shared areas pass.
    assert _check_submit_dir("/uscms_data/d2/user/jobs") is None
    assert _check_submit_dir("/uscms/home/user/jobs") is None

    # The message has to name the cause, since condor reports it as ENOENT.
    msg = _check_submit_dir("/tmp/jobs")
    assert "schedd" in msg.lower()
    print("[OK] test_check_submit_dir_rejects_node_local_paths")


def test_scan_unreachable_inputs():
    """Absolute paths on unmounted filesystems are reported; transferred ones differently."""
    findings = _scan_unreachable_inputs(CFG_WITH_LOCAL_GRIDPACK, transferred=[])
    assert len(findings) == 1
    assert "model_tarball.tar.xz" in findings[0]
    assert "transfer_input_files" in findings[0]

    # Same path, but transferred in: the advice changes to "use the basename".
    findings = _scan_unreachable_inputs(
        CFG_WITH_LOCAL_GRIDPACK,
        transferred=["/uscms_data/d2/someone/gridpacks/model_tarball.tar.xz"],
    )
    assert len(findings) == 1
    assert "bare filename" in findings[0]

    # A cvmfs path is reachable and must not be flagged.
    cvmfs_cfg = "args = cms.vstring('/cvmfs/cms.cern.ch/phys_generator/gp.tar.xz')"
    assert _scan_unreachable_inputs(cvmfs_cfg, transferred=[]) == []

    assert _scan_unreachable_inputs(CFG_CLEAN, transferred=[]) == []
    print("[OK] test_scan_unreachable_inputs")


def test_wrapper_seeds_are_per_job():
    """Each job derives its own seed from $1; the patch seeds every RNG module."""
    text = _build_wrapper(
        cfg_file="/some/where/step1_cfg.py",
        cmssw_release="CMSSW_12_4_8",
        scram_arch="el8_amd64_gcc10",
        output_files=["step1.root"],
        first_seed=1000,
    )
    assert 'PROC="$1"' in text
    assert "SEED=$(( 1000 + PROC ))" in text
    # Seeds every registered RNG module rather than naming generator explicitly,
    # so cfgs with externalLHEProducer/VtxSmeared are covered too.
    assert "RandomNumberGeneratorService" in text
    assert "initialSeed" in text
    assert "firstLuminosityBlock" in text
    # Release setup and per-process output naming.
    assert "cmsrel CMSSW_12_4_8" in text
    assert "export SCRAM_ARCH=el8_amd64_gcc10" in text
    assert 'mv "step1.root" "step1_${PROC}.root"' in text
    # No stage-out unless asked.
    assert "xrdcp" not in text
    print("[OK] test_wrapper_seeds_are_per_job")


def test_wrapper_eos_stageout_and_threads():
    """output_eos_dir switches to xrdcp; n_threads reaches the cmsRun call."""
    text = _build_wrapper(
        cfg_file="step1_cfg.py",
        cmssw_release="CMSSW_13_0_13",
        scram_arch="el8_amd64_gcc11",
        output_files=["nano.root"],
        first_seed=42,
        output_eos_dir="root://cmseos.fnal.gov//store/user/me/out/",
        n_threads=4,
    )
    assert "cmsRun -n 4 job_cfg.py" in text
    assert 'xrdcp -f "nano_${PROC}.root" ' \
           '"root://cmseos.fnal.gov//store/user/me/out/nano_${PROC}.root"' in text
    print("[OK] test_wrapper_eos_stageout_and_threads")


def test_wrapper_local_mode_reuses_release():
    """Local mode fixes the seed and does not rebuild an existing release area."""
    text = _build_wrapper(
        cfg_file="step1_cfg.py",
        cmssw_release="CMSSW_12_4_8",
        scram_arch="el8_amd64_gcc10",
        output_files=["step1.root"],
        first_seed=7,
        local=True,
    )
    assert "PROC=0" in text
    assert "if [ ! -d CMSSW_12_4_8 ]; then cmsrel CMSSW_12_4_8; fi" in text
    print("[OK] test_wrapper_local_mode_reuses_release")


def test_jdl_always_sets_transfer_output_files():
    """Unset, HTCondor would return the whole scratch dir including the release."""
    jdl = _build_jdl(
        executable="run_cmssw.sh",
        transfer_input_files=["/shared/step1_cfg.py", "/shared/gp.tar.xz"],
        output_files=["step1.root"],
        image="/cvmfs/singularity.opensciencegrid.org/cmssw/cms:rhel8",
        n_jobs=25,
        request_cpus=1,
        request_memory_mb=4000,
        request_disk_kb=10_000_000,
        proxy_path="/uscms/home/me/x509up_u1234",
        stage_out_to_eos=False,
    )
    assert "transfer_output_files   = step1_$(Process).root" in jdl
    assert "queue 25" in jdl
    assert '+ApptainerImage         = "/cvmfs/singularity.opensciencegrid.org/cmssw/cms:rhel8"' in jdl
    assert "x509userproxy           = /uscms/home/me/x509up_u1234" in jdl
    assert "arguments               = $(Process)" in jdl
    assert "/shared/gp.tar.xz" in jdl

    # Staging out to EOS means nothing comes back through the schedd.
    jdl_eos = _build_jdl(
        executable="run_cmssw.sh",
        transfer_input_files=[],
        output_files=["step1.root"],
        image="img",
        n_jobs=1,
        request_cpus=1,
        request_memory_mb=4000,
        request_disk_kb=1000,
        proxy_path=None,
        stage_out_to_eos=True,
    )
    # The key is that the directive is still PRESENT and empty, never absent:
    # absent means "return everything in scratch", which is the release area.
    output_lines = [ln for ln in jdl_eos.splitlines()
                    if ln.startswith("transfer_output_files")]
    assert len(output_lines) == 1, output_lines
    assert output_lines[0].split("=", 1)[1].strip() == ""
    assert "no_such_file" not in jdl_eos
    # No proxy given and none inferable here, so the directive is omitted.
    assert "x509userproxy" not in jdl_eos
    print("[OK] test_jdl_always_sets_transfer_output_files")


def test_parse_cluster_id():
    assert _parse_cluster_id("1 job(s) submitted to cluster 30052257.") == "30052257"
    assert _parse_cluster_id("nothing useful") is None
    assert _parse_cluster_id("") is None
    print("[OK] test_parse_cluster_id")


# ====================================================================== #
# ========================== Tool-level tests ========================== #
# ====================================================================== #

def test_tool_defaults_to_condor(tmp_path: Path):
    """use_condor defaults True: a JDL is written without being asked for."""
    cfg = _write_cfg(tmp_path)
    tool = CMSSWJobSubmitTool(base_directory=str(tmp_path))
    result = json.loads(tool.execute(
        cfg_file=str(cfg),
        cmssw_release="CMSSW_12_4_8",
        scram_arch="el8_amd64_gcc10",
        n_jobs=10,
        output_files=["step1.root"],
        allow_nonshared_submit_dir=True,
    ))

    assert result["status"] == "ok"
    assert result["mode"] == "condor"          # the default, not passed in
    assert result["submitted"] is False        # but nothing fires unsolicited
    assert result["container_os"] == "EL8"
    assert result["seeds"][:3] == [1000, 1001, 1002]
    assert Path(result["written"]["jdl"]).is_file()
    assert Path(result["written"]["wrapper"]).is_file()
    assert os.access(result["written"]["wrapper"], os.X_OK)
    assert "condor_submit submit.jdl" in result["submit_command"]
    print("[OK] test_tool_defaults_to_condor")


def test_tool_local_mode_writes_no_jdl(tmp_path: Path):
    """use_condor=False is the escape hatch: a run script, no submit file."""
    cfg = _write_cfg(tmp_path)
    tool = CMSSWJobSubmitTool(base_directory=str(tmp_path))
    result = json.loads(tool.execute(
        cfg_file=str(cfg),
        cmssw_release="CMSSW_12_4_8",
        scram_arch="el8_amd64_gcc10",
        use_condor=False,
    ))

    assert result["mode"] == "local"
    assert "jdl" not in result["written"]
    assert "run_command" in result
    assert "escape hatch" in result["note"]
    print("[OK] test_tool_local_mode_writes_no_jdl")


def test_tool_warns_about_unreachable_gridpack(tmp_path: Path):
    """The commonest silent failure is caught before submission, not after."""
    cfg = _write_cfg(tmp_path, CFG_WITH_LOCAL_GRIDPACK)
    tool = CMSSWJobSubmitTool(base_directory=str(tmp_path))
    result = json.loads(tool.execute(
        cfg_file=str(cfg),
        cmssw_release="CMSSW_12_4_8",
        scram_arch="el8_amd64_gcc10",
        allow_nonshared_submit_dir=True,
    ))

    assert result["status"] == "ok"
    joined = " ".join(result["warnings"])
    assert "model_tarball.tar.xz" in joined
    assert "does not exist on the worker node" in joined
    print("[OK] test_tool_warns_about_unreachable_gridpack")


def test_tool_rejects_bad_inputs(tmp_path: Path):
    """Unknown arch, missing cfg, and thread/slot mismatch are hard errors."""
    cfg = _write_cfg(tmp_path)
    tool = CMSSWJobSubmitTool(base_directory=str(tmp_path))

    out = tool.execute(cfg_file=str(cfg), cmssw_release="CMSSW_12_4_8",
                       scram_arch="winnt_amd64_gcc99")
    assert out.startswith("Error:") and "SCRAM_ARCH" in out

    out = tool.execute(cfg_file=str(tmp_path / "nope.py"),
                       cmssw_release="CMSSW_12_4_8",
                       scram_arch="el8_amd64_gcc10")
    assert out.startswith("Error:") and "cfg not found" in out

    out = tool.execute(cfg_file=str(cfg), cmssw_release="CMSSW_12_4_8",
                       scram_arch="el8_amd64_gcc10",
                       n_threads=8, request_cpus=1)
    assert out.startswith("Error:") and "oversubscribes" in out

    out = tool.execute(cfg_file=str(cfg))
    assert out.startswith("Error:") and "Missing required input" in out
    print("[OK] test_tool_rejects_bad_inputs")


def test_tool_rejects_node_local_job_dir(tmp_path: Path):
    """A /tmp job_dir is refused for condor, but is fine for a local run."""
    cfg = _write_cfg(tmp_path)
    tool = CMSSWJobSubmitTool(base_directory=str(tmp_path))

    out = tool.execute(cfg_file=str(cfg), cmssw_release="CMSSW_12_4_8",
                       scram_arch="el8_amd64_gcc10", job_dir="/tmp/heptapod_jobs")
    assert out.startswith("Error:") and "shared storage" in out

    with tempfile.TemporaryDirectory(dir="/tmp") as local_dir:
        result = json.loads(tool.execute(
            cfg_file=str(cfg), cmssw_release="CMSSW_12_4_8",
            scram_arch="el8_amd64_gcc10", job_dir=local_dir, use_condor=False))
        assert result["status"] == "ok"
    print("[OK] test_tool_rejects_node_local_job_dir")


def test_tool_submits_when_asked(tmp_path: Path):
    """submit=True shells out to condor_submit and records the cluster id."""
    cfg = _write_cfg(tmp_path)
    calls = []

    class _Completed:
        returncode = 0
        stdout = "2 job(s) submitted to cluster 99887766."
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs.get("cwd")))
        return _Completed()

    original = mod.subprocess.run
    mod.subprocess.run = fake_run
    try:
        tool = CMSSWJobSubmitTool(base_directory=str(tmp_path))
        result = json.loads(tool.execute(
            cfg_file=str(cfg), cmssw_release="CMSSW_12_4_8",
            scram_arch="el8_amd64_gcc10", n_jobs=2, submit=True,
            allow_nonshared_submit_dir=True))
    finally:
        mod.subprocess.run = original

    assert result["submitted"] is True
    assert result["cluster_id"] == "99887766"
    assert calls and calls[0][0][0] == "condor_submit"
    print("[OK] test_tool_submits_when_asked")


def test_tool_reports_missing_condor_binary(tmp_path: Path):
    """A site without HTCondor still gets its files, plus a warning."""
    cfg = _write_cfg(tmp_path)

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("condor_submit")

    original = mod.subprocess.run
    mod.subprocess.run = fake_run
    try:
        tool = CMSSWJobSubmitTool(base_directory=str(tmp_path))
        result = json.loads(tool.execute(
            cfg_file=str(cfg), cmssw_release="CMSSW_12_4_8",
            scram_arch="el8_amd64_gcc10", submit=True,
            allow_nonshared_submit_dir=True))
    finally:
        mod.subprocess.run = original

    assert result["submitted"] is False
    assert Path(result["written"]["jdl"]).is_file()
    assert any("not found on PATH" in w for w in result["warnings"])
    print("[OK] test_tool_reports_missing_condor_binary")


def test_status_tool_parses_condor_q(tmp_path: Path):
    """Status counts by name and surfaces hold reasons."""
    class _Completed:
        returncode = 0
        stdout = (
            "0 2 undefined\n"
            "1 5 Transfer input files failure at access point lpcschedd5\n"
            "2 1 undefined\n"
            "3 5 Error from slot1: memory limit exceeded\n"
        )
        stderr = ""

    original = mod.subprocess.run
    mod.subprocess.run = lambda cmd, **kw: _Completed()
    try:
        tool = CondorJobStatusTool(base_directory=str(tmp_path))
        result = json.loads(tool.execute(cluster_id="12345"))
    finally:
        mod.subprocess.run = original

    assert result["counts"] == {"running": 1, "held": 2, "idle": 1}
    assert len(result["held_jobs"]) == 2
    assert "lpcschedd5" in result["held_jobs"][0]["reason"]
    print("[OK] test_status_tool_parses_condor_q")


def test_status_tool_empty_queue_explains_schedd(tmp_path: Path):
    """An empty result is ambiguous, so say so rather than implying success."""
    class _Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    original = mod.subprocess.run
    mod.subprocess.run = lambda cmd, **kw: _Completed()
    try:
        tool = CondorJobStatusTool(base_directory=str(tmp_path))
        result = json.loads(tool.execute(cluster_id="12345"))
    finally:
        mod.subprocess.run = original

    assert result["counts"] == {}
    assert "schedd" in result["note"]

    tool = CondorJobStatusTool(base_directory=str(tmp_path))
    assert tool.execute().startswith("Error:")
    print("[OK] test_status_tool_empty_queue_explains_schedd")


def test_status_tool_tails_error_logs(tmp_path: Path):
    """Non-empty stderr from logs/ is surfaced with the status."""
    job_dir = tmp_path / "jobs"
    (job_dir / "logs").mkdir(parents=True)
    (job_dir / "logs" / "job.1.0.err").write_text(
        "\n".join(f"line {i}" for i in range(50)))
    (job_dir / "logs" / "job.1.1.err").write_text("")   # empty: skipped

    class _Completed:
        returncode = 0
        stdout = "0 5 held for a reason\n"
        stderr = ""

    original = mod.subprocess.run
    mod.subprocess.run = lambda cmd, **kw: _Completed()
    try:
        tool = CondorJobStatusTool(base_directory=str(tmp_path))
        result = json.loads(tool.execute(cluster_id="1", job_dir="jobs"))
    finally:
        mod.subprocess.run = original

    tails = result["error_log_tails"]
    assert "job.1.0.err" in tails
    assert "job.1.1.err" not in tails
    assert tails["job.1.0.err"].count("\n") <= 20
    assert "line 49" in tails["job.1.0.err"]
    print("[OK] test_status_tool_tails_error_logs")


def test_generated_wrapper_is_valid_bash(tmp_path: Path):
    """The emitted wrapper must at least parse under bash -n."""
    cfg = _write_cfg(tmp_path)
    tool = CMSSWJobSubmitTool(base_directory=str(tmp_path))
    result = json.loads(tool.execute(
        cfg_file=str(cfg), cmssw_release="CMSSW_12_4_8",
        scram_arch="el8_amd64_gcc10", n_jobs=3,
        output_files=["step1.root", "nano.root"],
        output_eos_dir="root://cmseos.fnal.gov//store/user/me/out",
        allow_nonshared_submit_dir=True,
    ))
    wrapper = result["written"]["wrapper"]

    completed = subprocess.run(["bash", "-n", wrapper],
                               capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr

    # The seed patch must also be valid python once extracted from the heredoc.
    text = Path(wrapper).read_text()
    start = text.index("<<'HEPTAPOD_SEED_PATCH'") + len("<<'HEPTAPOD_SEED_PATCH'")
    end = text.index("HEPTAPOD_SEED_PATCH", start)
    compile(text[start:end], "seed_patch", "exec")
    print("[OK] test_generated_wrapper_is_valid_bash")


def main():
    simple_tests = [
        test_image_for_arch,
        test_check_submit_dir_rejects_node_local_paths,
        test_scan_unreachable_inputs,
        test_wrapper_seeds_are_per_job,
        test_wrapper_eos_stageout_and_threads,
        test_wrapper_local_mode_reuses_release,
        test_jdl_always_sets_transfer_output_files,
        test_parse_cluster_id,
    ]
    tmp_tests = [
        test_tool_defaults_to_condor,
        test_tool_local_mode_writes_no_jdl,
        test_tool_warns_about_unreachable_gridpack,
        test_tool_rejects_bad_inputs,
        test_tool_rejects_node_local_job_dir,
        test_tool_submits_when_asked,
        test_tool_reports_missing_condor_binary,
        test_status_tool_parses_condor_q,
        test_status_tool_empty_queue_explains_schedd,
        test_status_tool_tails_error_logs,
        test_generated_wrapper_is_valid_bash,
    ]

    print("=" * 70)
    print("  CMSSW/HTCondor tool test suite")
    print("=" * 70)

    all_passed = True
    for test in simple_tests:
        try:
            test()
        except Exception as e:
            print(f"[FAIL] {test.__name__}: {e}")
            all_passed = False

    for test in tmp_tests:
        with tempfile.TemporaryDirectory() as tmp_dir:
            try:
                test(Path(tmp_dir))
            except Exception as e:
                print(f"[FAIL] {test.__name__}: {e}")
                all_passed = False

    print("=" * 70)
    print("Test suite completed successfully! [OK]" if all_passed
          else "Test suite completed with failures! [FAIL]")
    print("=" * 70)
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
