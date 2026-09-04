# Turbomachinery Geometry Studio

A preliminary centrifugal-pump sizing and CAD application. It computes a
traceable 1D operating-point design, builds connected impeller, diffuser, and
single-volute solids with CadQuery, previews them with PyVista, and exports STEP
plus OpenFOAM-oriented STL geometry.

The GUI offers two impeller configurations:

- **Closed** (default): smooth fourth-order Bezier hub and front-shroud flow
  surfaces, variable-height blades, and the calculated suction-eye opening.
- **Open**: the same meridional hub and virtual blade-tip boundary without the
  front material shroud.

Each calculation receives a deterministic design ID and a versioned engineering
record containing inputs, derived parameters, selected preliminary correlations,
CFturbo page references, and pass/warning/fail checks. `CFturbo_en.pdf` is the
master geometry reference; `docs/CFTURBO_GEOMETRY_REFERENCE.md` is its project
navigation index.

## Important scope

This is a preliminary design tool, not a certified pump-selection or CFD solver.
For the supported single-stage volute architecture, the OpenFOAM package now
contains a connected rotating impeller domain, cylindrical rotor-stator
interface, stationary annular inlet, flow-sized spiral, compensated cutwater,
tangential discharge diffuser, outlet extension, and named boundary patches.
The exporter also creates a first-pass single-region steady-MRF OpenFOAM case
with `blockMesh`, `surfaceFeatureExtract`, `snappyHexMesh`, a cylindrical rotor
cell zone, k-omega SST fields, solver controls, and run scripts. The command-line
runner can execute that case locally or through WSL and rejects incomplete or
non-physical results. It remains a screening setup: final geometry must be
checked with detailed loss modelling,
stress analysis, manufacturability review, mesh/y+ independence studies,
transient CFD where needed, and experimental validation.

The sizing engine currently supports centrifugal designs with metric specific
speed `Nq` between 5 and 100. Inputs outside that range are rejected instead of
being silently clamped.

## Installation

Use 64-bit Python 3.11:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -e .
```

Run the GUI:

```powershell
python gui.py
```

On Windows, `run_app.bat` uses the Python launcher when available. A command-line
default geometry package can be generated with:

```powershell
python impeller_generator.py
```

## Automated geometry and CFD workflow

Pump requirements can be supplied as YAML or JSON. A starting file is provided
at `examples/design_001.yaml`.

Generate geometry and the OpenFOAM case without running the solver:

```powershell
python impeller_generator.py export examples/design_001.yaml
```

Generate, mesh, solve, and evaluate in a sourced Linux OpenFOAM environment:

```sh
pumpai evaluate examples/design_001.yaml --backend local
```

On Windows, install a WSL distribution and OpenCFD OpenFOAM v2312 or newer, then
run:

```powershell
pumpai evaluate examples/design_001.yaml --backend wsl `
  --wsl-distribution Ubuntu `
  --openfoam-bashrc /usr/lib/openfoam/openfoam2606/etc/bashrc
```

For WSL execution, the runner automatically stages each case under a temporary
Linux path without spaces and copies the mesh, solver times, post-processing
data, and logs back afterward. This is required because OpenFOAM rejects case
working-directory names containing spaces, which are common in Windows project
paths.

The default output is `output/<requirements-file-stem>`. Each OpenFOAM command
writes a log under `openfoam_steady_mrf/logs`. The final
`simulation_result.json` records the design ID, case-manifest hash, mesh and
convergence evidence, flow closure, pressure rise, head, torque, power,
efficiency, and every acceptance gate. A failed gate returns process exit code
2; solver completion alone is not treated as a valid result.

The desktop application exposes the same workflow on its **CFD Evaluation**
tab. Compute a current single-stage volute design, confirm the WSL distribution
and OpenFOAM bashrc, choose an output directory, and click **Run CFD
Evaluation**. Geometry export, meshing, `checkMesh`, and `simpleFoam` run in the
background while command output streams into the log panel. The summary panel
shows the measured head, pressure rise, efficiency, power, torque, mesh size,
and pass/fail gate count. The active command can be cancelled, and **Open
Results Folder** opens the case containing logs and `simulation_result.json`.

## Output units and validation

- Internal CadQuery geometry and STEP files use millimetres.
- OpenFOAM STL coordinates are exported in metres.
- Every generated component is checked for validity, positive volume, and exactly
  one connected solid before export.
- `geometry_manifest.json` records the unit convention, operating point,
  collector geometry, CFD interfaces, and named patch files.
- `openfoam_steady_mrf/case_manifest.json` records the generated solver target,
  mesh envelope, MRF zone, included external patches, deliberately excluded RSI
  surfaces, and current CFD limitations.
- `openfoam_steady_mrf/simulation_result.json` records a versioned CFD result
  and fail-closed acceptance gates after `pumpai evaluate`.
- `engineering_record.json` records the reproducible design evidence and checks.

Material STEP/STL and CFD fluid-domain STEP/STL are exported as distinct files.
For a single-stage volute, the fluid volumes and inlet/RSI/outlet/wall patches
are constructed. The generated steady-MRF case uses one continuous fluid mesh,
so the matching `rotor_rsi` and `stationary_rsi` surfaces are intentionally not
included in `snappyHexMesh`; they are retained in the parent geometry package
for the later transient cyclic-AMI workflow. OpenFOAM itself is not bundled;
`pumpai evaluate` requires a Linux/WSL OpenFOAM installation, and accepted
screening results still require mesh/y+ independence before engineering release.
Other stationary architectures remain partial.

The meridional passage is smooth and parameterized. Impeller blades now use a
five-span free-form mean surface with local inlet-angle variation, smooth
spanwise pressure/suction surfaces, leading-edge stacking, and thickness placed
perpendicular to each mean line in its rotational span surface. The current
thickness law remains preliminary. Leading and trailing edges now use the
circular axis-ratio-1 case of an elliptic hydraulic rounding, with radius and
edge-extent checks recorded in the design evidence. Blade-passage validation
records spanwise leading/trailing-edge throat distances, minimum neighbor
clearance, throat area, and chordwise passage-area progression. The CAD builder
also rejects exact solid overlap between periodic neighboring blades before
patterning. Blade loading/surface velocity and CFD validation are still
required before manufacturing or performance claims. The 2D meridional editor
supports constrained five-point
Bezier hub/shroud editing, live passage and curvature checks, blade-edge
placement, undo/reset, and regeneration of the connected 3D CAD. Closed
impellers also expose a separate eye-collar/wear-ring-land material parameter.
The vaned stator is still preliminary. The single-volute collector uses the
CFturbo Pfleiderer `cu*r = constant` rule with internal-flow accumulation,
330–360 degree wrap validation, cutwater compensation beginning at 270 degrees,
and a discharge-diffuser cone-angle check.

## Tests

```powershell
python -m unittest discover -s tests -v
```

The CAD topology tests are intentionally slower because they run OpenCASCADE
Boolean operations.
