# Project engineering authority

`CFturbo_en.pdf` is the master technical reference for turbomachinery geometry
in this repository. Before changing hydraulic sizing, impeller geometry, stator
geometry, volute geometry, blade construction, flow-domain construction, or CFD
interfaces, consult the applicable pages of that PDF.

Use `docs/CFTURBO_GEOMETRY_REFERENCE.md` as the navigation index and condensed
checklist, but verify equations, definitions, assumptions, and parameter ranges
against the source PDF before implementation. The summary is not a substitute
for the manual.

Engineering precedence for this project is:

1. Explicit user requirements and operating conditions.
2. `CFturbo_en.pdf` for geometry definitions, dependencies, checks, and accepted
   turbomachinery design practice.
3. `Feedback.md` for platform architecture, roadmap, and product objectives.
4. Existing source code and tests, which may be corrected when they conflict
   with the above.

Do not treat a visually plausible CAD solid as proof of hydraulic correctness.
Geometry changes must preserve the parameter chain, units, flow-passage
continuity, topology checks, and the distinction between material and fluid
domains.
