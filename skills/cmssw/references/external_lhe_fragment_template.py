# Configuration/GenProduction/python/<process_name>_external_lhe_cff.py
#
# Lighter-weight fragment: shower an LHE file you already have (e.g. from an
# interactive MG5 `launch` while validating the process — mg5 skill) directly,
# skipping the gridpack build entirely. Use this as a fast local check of the
# shower step before spending time on Exercise-2-style gridpack production.
#
# Point --filein at the LHE in the cmsDriver.py invocation (see SKILL.md
# step 5); there is no ExternalLHEProducer block here — CMSSW reads the LHE
# straight off disk via LHESource.

import FWCore.ParameterSet.Config as cms

from Configuration.Generator.Pythia8CommonSettings_cfi import *
from Configuration.Generator.MCTunes2017.PythiaCP5Settings_cfi import *

generator = cms.EDFilter("Pythia8HadronizerFilter",
    maxEventsToPrint = cms.untracked.int32(1),
    pythiaPylistVerbosity = cms.untracked.int32(1),
    filterEfficiency = cms.untracked.double(1.0),
    pythiaHepMCVerbosity = cms.untracked.bool(False),
    comEnergy = cms.double(13000.),
    PythiaParameters = cms.PSet(
        pythia8CommonSettingsBlock,
        pythia8CP5SettingsBlock,
        processParameters = cms.vstring(
             # 'PartonLevel:MPI = off',
             # Paste any BuildDecayTableTool .cmnd lines here for BSM states
             # the LHE left stable — see gridpack_fragment_template.py.
        ),
        parameterSets = cms.vstring('pythia8CommonSettings',
                                    'pythia8CP5Settings',
                                    'processParameters',
                                    )
    )
)

# Optional Rivet validation — only if a matching routine exists (pitfall 3
# in SKILL.md). Otherwise validate the LHE directly with heptapod's
# LHEToJSONLTool -> EventJSONLToNumpyTool -> kinematics tools before this step.
#
# from GeneratorInterface.RivetInterface.rivetAnalyzer_cfi import rivetAnalyzer
# rivetAnalyzer.AnalysisNames = cms.vstring('MC_XS', 'MC_FSPARTICLES')
# rivetAnalyzer.OutputFile = cms.string("<process_name>_external_lhe.yoda")
# ProductionFilterSequence = cms.Sequence(generator*rivetAnalyzer)

ProductionFilterSequence = cms.Sequence(generator)
