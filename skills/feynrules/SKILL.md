---
name: feynrules
bundle: feynrules
description: Author a FeynRules `.fr` model and compile it to a UFO that MadGraph will actually load. Use whenever the user mentions FeynRules, a `.fr` file, UFO models, `M$Parameters`, `M$InteractionOrderHierarchy`, implementing a BSM Lagrangian, or adding new particles and couplings to the SM. Also use proactively before writing a `.fr` from scratch — a handful of declaration mistakes are invisible in FeynRules and only surface later as misleading MadGraph errors.
---

# FeynRules → UFO: getting a model MadGraph will load

Writing a BSM `.fr` from scratch tends to fail on the first two or three
attempts, in predictable ways. The failures rarely announce themselves at the
FeynRules stage — the UFO compiles fine and MadGraph reports something
unrelated later. This skill front-loads the checks.

For running MG5 on the resulting UFO — decay chains, widths, event
generation — see the `mg5` skill.

```
.fr file ──[FeynRules + Mathematica]──▶ UFO/ directory ──▶ (mg5 skill)
```

## Two rules that cover most first-run failures

### 1. Declare Mass and Width once, in the particle class

Writing `Mass -> {MX, 100}` or `Width -> {WX, 0.001}` in a particle class makes
FeynRules **declare `MX` and `WX` automatically** as external parameters with
`BlockName -> MASS` / `DECAY`. Do not re-declare them in `M$Parameters`; the
duplicate makes MG5 fail with:

```
name MX define multiple time. Please correct the UFO model!
```

The exception is an *internal* mass — a derived quantity. Declare that in
`M$Parameters` with `ParameterType -> Internal` and reference it from the
particle class.

### 2. Give every BSM coupling an interaction-order tag

A parameter that enters only through a BSM operator — nothing like `ee`, `gs`
or `yu` multiplying it in that Lagrangian term — leaves FeynRules with no order
to assign, so it falls back to `order = {'1':1}`. MG5 rejects that:

```
Some couplings have '1' order. This is not allowed in MG.
```

Always declare the hierarchy in the `.fr`:

```mathematica
M$InteractionOrderHierarchy = {
  {QCD, 1},
  {QED, 2},
  {NP,  1}
};

M$InteractionOrderLimit = {
  {QCD, 99}, {QED, 99}, {NP, 99}
};
```

and tag every BSM parameter:

```mathematica
yLFV == {
  ParameterType    -> External,
  BlockName        -> NPSEC,
  Value            -> 1.0*^-3,
  InteractionOrder -> {NP, 1}
}
```

Mixed couplings such as `eps * ee` inherit `{NP=1, QED=1}` automatically once
`eps` is tagged.

## Check the UFO before handing it to MG5

Thirty seconds here saves a misleading MG5 error later. After FeynRules
compiles a UFO:

```bash
# (1) Confirm new particles and PDG codes
grep -E "9000022|9000006|Aprime|phid" UFO_dir/particles.py

# (2) Confirm every coupling has a real order tag
grep "'1':1" UFO_dir/couplings.py    # ← MUST be empty

# (3) Confirm the new interaction order is registered
cat UFO_dir/coupling_orders.py       # ← should list NP

# (4) Inspect new vertices for the topology you expect
grep -B1 -A4 "P\.Ap\|P\.phid" UFO_dir/vertices.py
```

If (2) returns anything, `M$InteractionOrderHierarchy` or the `InteractionOrder`
tags are missing. Fix the `.fr` and regenerate. **Do not run MG5 first** — it
will fail with an error that points somewhere else.

## Editing an existing UFO instead of regenerating it

Sometimes the change you need is smaller than a FeynRules round trip — most
often re-pointing a coupling from one fermion generation to another, when the
gauge structure is unchanged. That is a legitimate shortcut and it is a
genuinely small edit: a single-Yukawa model carries **two** vertices for the
interaction (the vertex and its conjugate), and everything else — gauge
couplings, colour flow, mass and width machinery — is flavour-blind.

Work on a **copy** with a new directory name, never the original, and change:

- the two `Vertex(...)` entries in `vertices.py`, e.g.
  `[P.u, P.e__minus__, P.X__tilde__]` → `[P.c, P.mu__minus__, P.X__tilde__]`
  and its conjugate;
- the matching `Decay` entries in `decays.py`, including the **mass symbols**
  inside the partial-width expressions;
- delete `py3_model.pkl` and `__pycache__/` — a stale pickle shadows the edited
  source and you will debug the version you did not write.

### Two traps

**Overlapping symbol substitution.** Replacing `Me → MMU` then `MU → MC` turns
`MMU` into `MMC`, because `MU` matches inside `MMU`. The file still parses,
`MMC` is not a parameter in the model, and the error only surfaces as a wrong
width deep inside MG5. Substitute in one pass with word-boundary regexes, or
order the replacements so no output of one is an input to another.

**No check that the edit is right.** A hand-edited UFO has no test suite. Give
it one: evaluate the edited width expression directly in Python against a
closed form. For a scalar decaying to a massless quark–lepton pair through a
single Yukawa `y`,

```
Gamma = y^2 * M / (16 pi)
```

so `y = 0.5`, `M = 2000 GeV` must give **9.947 GeV**. Then confirm MG5 agrees
by setting `DECAY <pdg> Auto` in `customizecards.dat` and reading the width
back out of the generated `param_card.dat` — agreement to several digits means
the vertex, the coupling, and the decay table are all consistent. Do this
*before* spending a gridpack build.

Finally, check what MG5 actually generated: the LHE's final-state PDG codes
should be the new flavours and only the new flavours (e.g. equal counts of
`4`, `-4`, `13`, `-13` and no `2`/`11`), which also confirms the branching
ratio is 1 by construction.

## Before writing any `.fr`

Answer these internally. If any answer is "not sure", read the reference file
before generating.

1. **What are the new particles?** Spin, charge, self-conjugate or not, and PDG
   codes — pick from 9000001 upward to avoid SM clashes.
2. **What are the new couplings, and which interaction orders do they belong
   to?** SM only, mixed SM+NP, or pure NP?
3. **Are the SM field names what you think?** FeynRules' SM uses
   `e, mu, ta, ve, vm, vt, u, c, t, d, s, b`, with bars as `ebar` and so on.
   The Lorentz index `mu` collides with the muon name — FeynRules resolves it
   by context, but take care with `del[..., mu]` near a muon term.
4. **What is the chirality structure of the BSM operator?** `ProjP = (1+γ⁵)/2 =
   P_R` and `ProjM = P_L`, so $\bar\psi_L \chi_R = \bar\psi\,P_R\,\chi$ becomes
   `psibar.ProjP.chi`.

## Compiling

```bash
# 1. Edit or create the model
$EDITOR models/my_model.fr

# 2. Compile to UFO — the heptapod FeynRulesToUFO tool, or wolframscript directly
#    Tool:   FeynRulesToUFO with model_path / output_dir
#    Direct: wolframscript -file scripts/run_feynrules.wl

# 3. Sanity-check the UFO (above) before going near MG5
```

## Reference files

- **`references/pitfalls.md`** — the model-side pitfalls in full: duplicate
  mass/width, missing interaction order, the Lorentz-index collision,
  self-conjugate vectors (Stückelberg vs broken U(1)′), the chiral-projector
  convention, and `FR_PATH` not reaching the MCP server.
- **`references/fr_template.fr`** — a minimal tested template adding a vector,
  a real scalar and a Yukawa-like LFV operator to the SM. Copy and edit.
