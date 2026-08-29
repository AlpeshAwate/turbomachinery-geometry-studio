# CFturbo geometry reference

## Authority and scope

The project-local `CFturbo_en.pdf` is the master engineering knowledge document
for geometry work. It is a 722-page CFturbo manual generated on 2026-06-22. This
file is a navigation aid and implementation checklist; consult the PDF pages
listed below before changing the corresponding algorithm.

The authoritative design sequence is:

1. operating point, fluid state, units, and component coupling;
2. impeller main dimensions;
3. primary meridional flow path (hub and shroud);
4. leading and trailing edge locations;
5. meridional flow and cross-section checks;
6. blade properties, spans, velocity triangles, incidence, and slip;
7. blade mean surfaces, profiles, thickness, and edge shapes;
8. stator or vaned-diffuser geometry and rotor-stator interaction checks;
9. volute, discharge diffuser, and cut-water geometry;
10. separate flow-domain and material-domain construction;
11. virtual extensions and rotor-stator interfaces for CFD;
12. topology, manufacturability, and simulation validation.

## Manual navigation map

| Subject | PDF pages |
| --- | ---: |
| Global setup and performance prediction | 111-131 |
| Export, CAD, CFD, and CFD-Pre formats | 134-213 |
| Units, fluids, and empirical functions | 228-262 |
| 3D model and model tree | 270-287 |
| Centrifugal/mixed-flow pump main dimensions | 294-313 |
| Impeller meridional contour | 439-466 |
| Blade properties and span definition | 467-508 |
| Blade mean lines and flow checks | 509-546 |
| Blade profiles and thickness | 547-555 |
| Blade edges | 556-565 |
| Impeller CFD setup and virtual geometry | 593-600 |
| Impeller model settings and finishing | 601-615 |
| Stator and radial diffuser | 616-635 |
| Volute, diffuser, and cut-water | 636-697 |
| References and symbols | 698-705 |

## Pump-geometry baseline

These are checks and initial ranges, not hard-coded universal design values.
They must remain traceable to the design point and selected empirical model.

### Main dimensions

- CFturbo supports impeller specific speed `nq` from 5 to 500 and identifies
  roughly 10 to 160 as the recommended centrifugal/mixed-flow range.
- Main dimensions are coupled through Euler work, continuity, velocity
  triangles, efficiencies, blockage, and slip. Diameter, width, and blade
  angles must not be calculated as independent decorative dimensions.
- For centrifugal impellers, the manual gives an initial work-coefficient range
  of about 0.7 to 1.3.
- Initial `b2/d2` lies roughly between 0.04 and 0.30 and rises with specific
  speed. The meridional deceleration ratio `cm2/cm1` is approximately 0.60 to
  0.95, also dependent on specific speed.
- Hydraulic and volumetric efficiencies affect sizing. The manual illustrates
  typical hydraulic efficiency around 0.85-0.93 and volumetric efficiency around
  0.93-0.99; actual values require evidence and correction for size, fluid, and
  design type.
- Suction sizing must include cavitation criteria and cannot be validated by one
  estimated NPSHr number alone.

### Meridional flow path

- Hub and shroud are primary hydraulic boundaries, not flat support plates.
- Use smooth Bezier, B-spline, or controlled line/arc contours. Connections
  inside the blade region should be tangent and free of tiny artifact segments.
- Curvature and curvature gradients should remain smooth and low, approaching
  zero at contour ends where practical.
- Flow-passage cross-sectional area should change steadily; avoid local maxima
  and minima unless explicitly justified.
- Leading and trailing edges are curves between hub and shroud. Their endpoints,
  tilt, sweep, and relation to local contour normals are part of the design.
- For low-specific-speed centrifugal impellers (`nq` near or below 30), a nearly
  axial leading edge is common. At higher specific speed, extending the leading
  edge into the suction region can improve loading distribution and suction
  behavior.
- Compare hub and shroud static moments between leading and trailing edges to
  distribute energy transfer more uniformly.

### Blades

- The normal starting point for a centrifugal/mixed-flow pump is a free-form 3D
  blade, represented by several spanwise mean lines between hub and shroud.
- Use enough spans to create a stable surface; the manual recommends at least
  four when low span count causes solid-generation problems.
- A typical pump blade count is 3-7. Select the count using loading, friction,
  pitch, blockage, passage area, and rotor-stator interaction checks.
- Inlet blade angle follows the inlet velocity triangle and incidence target.
  Pump values are commonly below 40 degrees and should generally not fall below
  roughly 15-18 degrees when efficiency is considered.
- Outlet blade angle and impeller diameter are coupled by Euler work and slip.
  The manual lists 15-45 degrees for pumps, with 20-27 degrees common.
- Slip must use an explicit model such as Guelich/Wiesner, Aungier/Wiesner,
  Pfleiderer, or von Backstroem, including the model's applicability and
  corrections. The resulting deviation angle should normally remain below about
  10-14 degrees.
- Blade blockage depends on thickness, pitch, blade angle, and thickness mode.
  Do not use one unexplained global blockage factor as the final model.
- Construct pressure and suction surfaces from spanwise mean lines and a
  controlled thickness distribution. The manual recommends thickness applied
  perpendicular to each mean line for stability.
- Design rounded or otherwise explicit leading and trailing edges; a blunt
  extrusion is not the hydraulic default.
- Validate blade overlap, curvature, loading, throat area, throat distance at
  every span, passage-area progression, incidence, Reynolds number, and surface
  velocity criteria before accepting geometry.

### Stator and radial diffuser

- Stator inlet/outlet geometry must be coupled to neighboring component
  interfaces; axial and radial mismatches are not acceptable.
- Coordinate rotor and stator blade counts. Avoid shared periodicities (`m=0`)
  and first/second-order `m=1` combinations; higher interaction modes are
  preferred to reduce pulsation, vibration, and noise risk.
- For a pump radial diffuser, check throat width, outlet distance, actual and
  allowable opening angle, area ratio, pressure recovery, and nondimensional
  length. Manual guide values include `AR < 3`, `c3q/c2 = 0.7-0.85` for low
  specific speed, `L34/a3 = 2.5-6`, and `b3/a3 = 0.8-2`.
- A vaned stator is not a substitute for a volute and discharge branch.

### Volute and discharge diffuser

- Volute inlet diameter and width derive from the upstream component and its
  interface. At higher specific speed, the manual suggests `b4/b2` around
  1.05-1.2; different designs may justify other ratios.
- Select a manufacturable cross-section family and develop its area around the
  spiral using an explicit rule such as Pfleiderer angular-momentum conservation
  or Stepanoff constant mean velocity.
- Include spiral development, diffuser/branch, and cut-water as separate but
  coupled geometry stages.
- Check cut-water clearance, wrap angle, intersection topology, cross-section
  progression, velocity ratio, diffuser cone angle, and predicted pressure
  recovery/losses.

### CFD geometry

- Material-domain solids and fluid-domain solids are different deliverables.
  OpenFOAM requires the closed fluid volume and named boundary/interface patches,
  not merely STL files of the hardware.
- Place the rotor-stator interface in the gap between rotating and stationary
  components. Extend the rotor flow domain to the interface rather than placing
  blade trailing edges directly on it.
- Add the matching RSI connection to the stationary component and verify there
  is no overlap, inversion, or gap.
- Generate real and simplified virtual flow domains deliberately. Any omitted
  leakage or secondary path must be documented as a modeling assumption.

## Acceptance rule for future geometry changes

A geometry change is not complete until it has:

- source parameters and units recorded in the engineering model;
- its applicable CFturbo page range identified;
- numerical hydraulic checks performed before CAD generation;
- smooth, non-self-intersecting hub, shroud, and blade surfaces;
- passage, throat, blockage, clearance, and interface checks;
- separate material and fluid-domain topology validation;
- connected/watertight solid checks and a geometry manifest;
- tests that fail when the relevant engineering constraint is violated.

