# DFX Agent Proposal
## AI-Powered Design for Manufacturing / Assembly Feedback Loop for Turbomachinery Studio

---

## 1. Problem Statement

The current application generates deterministic, physics-driven impeller and diffuser geometry from user requirements. However, the generated geometry is **not validated against manufacturing or assembly constraints** until a human expert reviews it. This creates a slow, manual iteration cycle:

```
User inputs → Compute → 3D CAD → Human DFX review → Manual edits → Repeat
```

There is no automated feedback loop that evaluates the produced geometry for manufacturability, assembly feasibility, or design best practices, and no way to apply AI-suggested improvements with a single action.

---

## 2. Proposed Solution

Build a **DFX Agent** as a separate, optional module that analyzes generated geometry and suggests parameter-level improvements. The agent operates as a background worker inside the existing Qt GUI, returning structured suggestions that the user can apply with one click, triggering an automatic regeneration of the geometry.

### 2.1 High-Level Loop

```
┌──────────────────┐     ┌───────────────────┐     ┌──────────────────┐
│ Compute & Generate│────▶│ DFX Agent Analyze │────▶│ Suggestions Panel│
│ 3D CAD            │     │ (rule engine + AI)│     │ (per-item Apply) │
└──────────────────┘     └───────────────────┘     └────────┬─────────┘
                                                           │ Apply click
                                                           ▼
                                                   ┌──────────────────┐
                                                   │ Mutate params +  │
                                                   │ Regenerate CAD   │
                                                   └──────────────────┘
```

### 2.2 Architecture

```
core/
  dfx_agent/
    __init__.py
    geometry_reader.py      # Reads STL/STEP metrics via CadQuery/PyVista
    rule_engine.py          # Deterministic DFX checks (no AI required)
    ai_suggester.py         # Optional LLM-backed improvement suggestions
    models.py               # DFXCheck, DFXSuggestion dataclasses
    feedback_loop.py        # Maps suggestions → parameter mutations
```

### 2.3 Core Data Models

```python
@dataclass
class DFXCheck:
    category: str            # "DFM" | "DFA" | "DesignForCasting" | "Inspection"
    severity: str            # "pass" | "warning" | "fail"
    key: str                 # e.g. "min_blade_thickness"
    message: str
    suggested_param: str | None = None      # e.g. "blade_thickness"
    suggested_delta: float | None = None    # e.g. +0.5 mm
    confidence: str = "high"                # "high" | "medium" | "low"

@dataclass
class DFXSuggestion:
    id: str
    category: str
    severity: str
    description: str
    parameter_path: str      # e.g. "meridional.shroud_control_points_rz[2][1]"
    current_value: Any
    suggested_value: Any
    delta_description: str
    source: str              # "rule" | "ai"
```

---

## 3. DFX Check Categories

### 3.1 Design for Manufacturing (DFM)

| Check | Rule | Suggestion |
|-------|------|------------|
| Minimum blade thickness | `blade_thickness >= 1.5 mm` | Increase `blade_thickness` |
| Minimum cutter radius at hub/shroud | `min_curvature_radius >= 2.0 mm` | Adjust Bezier control points |
| Draft angle on shroud | Surface normal vs. axis < 3° | Raise shroud outlet axial |
| Wall thickness uniformity | `area_uniformity_ratio <= 3.0` | Redistribute meridional contour |
| Outlet width vs. tool access | `b2 >= 3.0 mm` | Increase `outlet_width_b2` |

### 3.2 Design for Assembly (DFA)

| Check | Rule | Suggestion |
|-------|------|------------|
| Part count | Closed impeller = 1 part | Accept / no action |
| Balance-plate accessibility | Backplate flat, central bore present | Flag if bore missing |
| Handling features | `Ds >= 50 mm` or handling ribs | Suggest increase `suction_diameter_ds` |

### 3.3 Design for Casting / 3D Printing

| Check | Rule | Suggestion |
|-------|------|------------|
| Section uniformity | `min_channel_height >= 2.0 mm` | Smooth meridional transition |
| Hot-tear risk fillets | `min_curvature_radius >= 1.5 mm` | Add fillet to control points |
| Support removal | Draft > 3° on overhangs | Adjust shroud lean |

### 3.4 Design for Inspection

| Check | Rule | Suggestion |
|-------|------|------------|
| Datum availability | Backplate face, bore, outlet face present | Flag missing features |
| CMM probe reach | `b2 <= 1.5 * D2` | Reduce outlet width if needed |

---

## 4. AI Integration (Optional Layer)

### 4.1 Recommended Backend: Ollama

- **Model**: `llama3.1:8b` or `mistral:7b-instruct-v0.3`
- **Why**: Free, offline, no API keys, no data leaves machine, Python-native
- **Fallback**: Google Gemini 1.5 Flash (free tier) or Groq (free tier)

### 4.2 Prompt Strategy

Feed the AI a structured summary of the geometry metrics and rule-check results, and constrain the output to a parseable format:

```python
prompt = f"""You are a turbomachinery DFX engineer.

METRICS:
- Blade thickness: {blade_thickness} mm
- Outlet width b2: {b2} mm
- Outlet diameter D2: {d2} mm
- Specific speed Nq: {nq}
- Min shroud curvature: {min_shroud_radius} mm
- Min hub curvature: {min_hub_radius} mm
- Area uniformity ratio: {area_uniformity}
- Static moment imbalance: {static_imbalance}%

CHECKS:
{checks_text}

Return ONLY this JSON structure:
{{
  "critical": [{{"param": "...", "delta": ...}}],
  "warnings": [{{"param": "...", "delta": ...}}],
  "notes": ["..."]
}}
"""
```

### 4.3 Parsing AI Output

The AI suggester must validate and sanitize AI output before applying it:

```python
def _parse_ai_suggestions(self, raw_text: str, design: CompletePumpDesign) -> list[DFXSuggestion]:
    # Extract JSON block
    # Validate each suggested parameter exists in PumpRequirements or MeridionalOverride
    # Clamp deltas to safe ranges
    # Return structured DFXSuggestion list
```

---

## 5. Integration with Existing Application

### 5.1 GUI Changes (`gui.py`)

Add a new **DFX Analysis** section in the right dashboard panel and a button in the left toolbar:

```python
# Left panel
self.btn_dfx = QPushButton("🔍 Run DFX Analysis")
self.btn_dfx.clicked.connect(self.start_dfx_analysis)

# Right panel (new group box)
grp_dfx = QGroupBox("DFX Report")
f_dfx = QFormLayout(grp_dfx)
self.lbl_dfx_summary = QLabel("-")
self.dfx_suggestions_layout = QVBoxLayout()
# ... dynamic suggestion buttons added here
```

### 5.2 Worker Thread

```python
class DFXWorker(QThread):
    progress = Signal(str)
    finished = Signal(list)   # list[DFXCheck]
    error = Signal(str)

    def __init__(self, design: CompletePumpDesign, use_ai: bool = False):
        super().__init__()
        self.design = design
        self.use_ai = use_ai

    def run(self):
        try:
            self.progress.emit("Running deterministic DFX checks...")
            checks = run_rule_engine(self.design)

            if self.use_ai:
                self.progress.emit("Running AI DFX analysis...")
                ai_suggestions = run_ai_suggester(self.design, checks)
                checks.extend(ai_suggestions)

            self.finished.emit(checks)
        except Exception as exc:
            self.error.emit(str(exc))
```

### 5.3 Apply Handler

```python
def apply_dfx_suggestion(self, suggestion: DFXSuggestion):
    # 1. Mutate PumpRequirements or MeridionalOverride
    self._apply_suggestion_to_design(suggestion)

    # 2. Mark geometry stale, clear caches
    self._mark_design_stale()

    # 3. Regenerate
    self.start_computation()

    # 4. Optionally auto-iterate
    if self.chk_dfx_auto_iterate.isChecked():
        QTimer.singleShot(500, self._dfx_auto_iteration_step)
```

### 5.4 Auto-Iteration Mode

```python
MAX_DFX_ITERATIONS = 10
self._dfx_iteration_count = 0
self._dfx_history = []

def _dfx_auto_iteration_step(self):
    if self._dfx_iteration_count >= MAX_DFX_ITERATIONS:
        self.lbl_status.setText("DFX auto-iterate: max iterations reached.")
        return

    suggestion = self.dfx_agent.get_highest_priority_suggestion()
    if not suggestion:
        self.lbl_status.setText("DFX clean — no further iterations needed.")
        return

    if suggestion.id in [s.id for s in self._dfx_history[-3:]]:
        self.lbl_status.setText("DFX auto-iterate: oscillation detected, stopping.")
        return

    self._dfx_history.append(suggestion)
    self._dfx_iteration_count += 1
    self.apply_dfx_suggestion(suggestion)
```

---

## 6. Implementation Phases

### Phase 1: Rule Engine (Week 1-2)
- [ ] Create `core/dfx_agent/` package
- [ ] Implement `geometry_reader.py` to extract metrics from `CompletePumpDesign`
- [ ] Implement `rule_engine.py` with 8-12 deterministic checks
- [ ] Add DFX worker thread and basic GUI panel
- [ ] Display pass/fail summary in dashboard

### Phase 2: Apply Loop (Week 3)
- [ ] Implement `DFXSuggestion` dataclass and resolver
- [ ] Map suggestions to `PumpRequirements` and `MeridionalOverride` mutations
- [ ] Add per-suggestion "Apply" buttons in GUI
- [ ] Wire apply → regenerate → re-analyze loop
- [ ] Add safety guardrails (max iterations, oscillation detection)

### Phase 3: AI Layer (Week 4-5)
- [ ] Add `ai_suggester.py` with Ollama backend
- [ ] Design and test prompt templates for turbomachinery DFX
- [ ] Implement JSON parser with validation and clamping
- [ ] Add AI toggle in GUI
- [ ] Add fallback to Gemini/Groq if Ollama unavailable

### Phase 4: Polish (Week 6)
- [ ] Export DFX report to JSON/Markdown alongside existing `engineering_record.json`
- [ ] Add DFX summary to OpenFOAM export package
- [ ] User documentation and example workflows
- [ ] Unit tests for rule engine and suggestion resolver

---

## 7. Risk Assessment

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| AI suggests invalid geometry | Medium | Strict parameter validation before apply |
| Oscillating suggestions | Medium | History tracking, max iterations, dampening |
| Ollama not installed on user machine | High | Graceful fallback to rule-only mode |
| Slow AI inference on CPU | Medium | Show progress, allow cancel, use small model (8B) |
| Complex suggestions (Bezier edits) hard to map | High | Defer complex edits to existing meridional editor |

---

## 8. Alternatives Considered

| Alternative | Pros | Cons |
|-------------|------|------|
| PicoGK as parallel kernel | Advanced voxel geometry | No Python bindings, requires full rewrite |
| Cloud-only AI API | Fast, powerful | Requires internet, API keys, cost |
| External standalone script | Zero GUI changes | No apply-loop, poor UX |

---

## 9. Success Criteria

1. DFX rule engine runs in < 2 seconds on any generated design
2. AI analysis completes in < 30 seconds on CPU-only hardware
3. User can apply any single suggestion with one click and see regenerated geometry within 10 seconds
4. Auto-iterate mode converges to zero warnings in <= 5 iterations for 80% of designs
5. Zero changes to existing sizing, meridional, or blade-generation code

---

## 10. References

- `CFturbo_en.pdf` — master technical reference for turbomachinery geometry
- `docs/CFTURBO_GEOMETRY_REFERENCE.md` — project geometry checklist
- `core/meridional.py` — existing Bezier flow-path parameterization
- `core/blade_builder.py` — existing OpenCASCADE CAD synthesis
- `gui.py` — existing Qt worker-thread pattern for background computation
