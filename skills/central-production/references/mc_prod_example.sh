#!/bin/bash
#
# Annotated copy of ppd-short-exercise/mc_prod.sh — a worked example of the
# central-production chain described in skills/central-production/SKILL.md.
#
# This reproduces TOP-Run3Summer22EEwmLHEGS-00001, NOT the PPD exercise's own
# assigned target dataset (TTto2L2Nu_HT-500_NJet-7_TuneCP5_13p6TeV_powheg-pythia8).
# Use it for the chain SHAPE and flag patterns — release names, conditions
# strings, and where the release boundary falls are specific to this one
# chain and will differ for any other dataset. Re-derive them from McM
# per SKILL.md §1.
#
# Run inside an EL8 container with a valid CMS grid proxy (SKILL.md §5):
#   cmssw-el8
#   voms-proxy-init -voms cms

source /cvmfs/cms.cern.ch/cmsset_default.sh

# --- §3: root fragment fetched from McM's public REST API, not hand-written.
# The exercise's own script leaves the next step as a manual TODO because
# which filter to strip is dataset-specific.
curl -s -k https://cms-pdmv-prod.web.cern.ch/mcm/public/restapi/requests/get_fragment/TOP-Run3Summer22EEwmLHEGS-00001 \
  --retry 3 --create-dirs \
  -o Configuration/GenProduction/python/TOP-Run3Summer22EEwmLHEGS-00001-fragment.py

# HERE YOU NEED TO MODIFY THE FRAGMENT to strip the original request's
# kinematic/decay filters if you want an unfiltered reproduction (SKILL.md §3).

[ -s Configuration/GenProduction/python/TOP-Run3Summer22EEwmLHEGS-00001-fragment.py ] || exit $?

# --- §4: release #1 — GEN-SIM, DIGI, RECO, and MiniAOD all happen here.
export SCRAM_ARCH=el8_amd64_gcc10
source /cvmfs/cms.cern.ch/cmsset_default.sh

if [ -r CMSSW_12_4_11_patch3/src ]; then
  echo release CMSSW_12_4_11_patch3 already exists
else
  scram p CMSSW CMSSW_12_4_11_patch3
fi
cd CMSSW_12_4_11_patch3/src
eval `scram runtime -sh`

mv ../../Configuration .
scram b

export SEED=2045

# --- §2: step 1 of 5 cmsDrivers — GEN-SIM (this chained request's own step)
cmsDriver.py Configuration/GenProduction/python/TOP-Run3Summer22EEwmLHEGS-00001-fragment.py \
  --python_filename TOP-Run3Summer22EEwmLHEGS-00001_1_cfg.py \
  --eventcontent RAWSIM,LHE --customise Configuration/DataProcessing/Utils.addMonitoring \
  --datatier GEN-SIM,LHE --fileout file:TOP-Run3Summer22EEwmLHEGS-00001.root \
  --conditions 124X_mcRun3_2022_realistic_postEE_v1 \
  --beamspot Realistic25ns13p6TeVEarly2022Collision \
  --customise_commands process.RandomNumberGeneratorService.externalLHEProducer.initialSeed="int(${SEED})" \
  --step LHE,GEN,SIM --geometry DB:Extended --era Run3 --no_exec --mc -n 100

# --- §2/§5: step 2 of 5 — DIGI/L1/HLT with Premix pileup.
# --pileup_input is a DBS-registered dataset read over xrootd: this line
# builds fine with no grid proxy, but cmsRun will fail to open the pileup
# files at run time without one (SKILL.md §5).
cmsDriver.py --python_filename TOP-Run3Summer22EEDRPremix-00001_1_cfg.py \
  --eventcontent PREMIXRAW --customise Configuration/DataProcessing/Utils.addMonitoring \
  --datatier GEN-SIM-RAW --fileout file:TOP-Run3Summer22EEDRPremix-00001_0.root \
  --pileup_input "dbs:/Neutrino_E-10_gun/Run3Summer21PrePremix-Summer22_124X_mcRun3_2022_realistic_v11-v2/PREMIX" \
  --conditions 124X_mcRun3_2022_realistic_postEE_v1 \
  --step DIGI,DATAMIX,L1,DIGI2RAW,HLT:2022v14 \
  --procModifiers premix_stage2,siPixelQualityRawToDigi \
  --geometry DB:Extended --filein file:TOP-Run3Summer22EEwmLHEGS-00001.root \
  --datamix PreMix --era Run3 --no_exec --mc -n -1

# --- §2: step 3 of 5 — RECO, still the same "DIGI-RECO" chained request.
# This is the +1 that makes "4 requests, 5 cmsDrivers" (SKILL.md §2).
cmsDriver.py --python_filename TOP-Run3Summer22EEDRPremix-00001_2_cfg.py \
  --eventcontent AODSIM --customise Configuration/DataProcessing/Utils.addMonitoring \
  --datatier AODSIM --fileout file:TOP-Run3Summer22EEDRPremix-00001.root \
  --conditions 124X_mcRun3_2022_realistic_postEE_v1 \
  --step RAW2DIGI,L1Reco,RECO,RECOSIM --procModifiers siPixelQualityRawToDigi \
  --geometry DB:Extended --filein file:TOP-Run3Summer22EEDRPremix-00001_0.root \
  --era Run3 --no_exec --mc -n -1

# --- §2: step 4 of 5 — MiniAOD (PAT). Still release #1 in this example —
# contrast with the exercise's own assigned dataset, where the release
# boundary falls here instead of at NanoAOD (SKILL.md §4).
cmsDriver.py --python_filename TOP-Run3Summer22EEMiniAODv3-00001_1_cfg.py \
  --eventcontent MINIAODSIM --customise Configuration/DataProcessing/Utils.addMonitoring \
  --datatier MINIAODSIM --fileout file:TOP-Run3Summer22EEMiniAODv3-00001.root \
  --conditions 124X_mcRun3_2022_realistic_postEE_v1 --step PAT --geometry DB:Extended \
  --filein file:TOP-Run3Summer22EEDRPremix-00001.root --era Run3 --no_exec --mc -n -1

cmsRun TOP-Run3Summer22EEwmLHEGS-00001_1_cfg.py
cmsRun TOP-Run3Summer22EEDRPremix-00001_1_cfg.py
cmsRun TOP-Run3Summer22EEDRPremix-00001_2_cfg.py
cmsRun TOP-Run3Summer22EEMiniAODv3-00001_1_cfg.py

cd ../../   # move back to basedir

# --- §4: release #2 — NanoAOD only, in a DIFFERENT CMSSW release from steps 1-4.
export SCRAM_ARCH=el8_amd64_gcc10
source /cvmfs/cms.cern.ch/cmsset_default.sh

if [ -r CMSSW_12_6_0_patch1/src ]; then
  echo release CMSSW_12_6_0_patch1 already exists
else
  scram p CMSSW CMSSW_12_6_0_patch1
fi
cd CMSSW_12_6_0_patch1/src
eval `scram runtime -sh`

scram b

# --- §2: step 5 of 5 — NanoAOD. --filein reaches back across the release
# boundary with a relative path into the OTHER release's src/ (SKILL.md §4).
# --eventcontent/--datatier are already NANOAODSIM (flat ntuple), not the
# NANOEDMAODSIM McM hands you by default (SKILL.md §6).
cmsDriver.py --python_filename TOP-Run3Summer22EENanoAODv11-00001_1_cfg.py \
  --eventcontent NANOAODSIM --customise Configuration/DataProcessing/Utils.addMonitoring \
  --datatier NANOAODSIM --fileout file:TOP-Run3Summer22EENanoAODv11-00001.root \
  --conditions 126X_mcRun3_2022_realistic_postEE_v1 --step NANO --scenario pp \
  --filein file:../../CMSSW_12_4_11_patch3/src/TOP-Run3Summer22EEMiniAODv3-00001.root \
  --era Run3,run3_nanoAOD_124 --no_exec --mc -n -1

cmsRun TOP-Run3Summer22EENanoAODv11-00001_1_cfg.py

# Recipe origin, for cross-checking against McM directly:
# https://cms-pdmv-prod.web.cern.ch/mcm/public/restapi/requests/get_setup/TOP-Run3Summer22EEwmLHEGS-00001
