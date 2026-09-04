import os
import sys
import gc
import queue
import subprocess
import threading
import time

# Force QtPy to use PySide6 consistently
os.environ["QT_API"] = "pyside6"

# Ensure current script directory is in Python path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import traceback
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
    QLabel, QSpinBox, QDoubleSpinBox, QComboBox, QPushButton, 
    QGroupBox, QFormLayout, QMessageBox, QProgressBar, QTabWidget,
    QCheckBox, QRadioButton, QButtonGroup, QScrollArea, QFileDialog,
    QFrame, QLineEdit, QPlainTextEdit
)
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QFont, QColor

from pyvistaqt import QtInteractor
import pyvista as pv

from core.fluids import FLUID_DISPATCH, FLUID_TEMPERATURE_RANGES
from core.pump_design import (
    COLLECTOR_VANELESS_DIFFUSER,
    COLLECTOR_VANED_DIFFUSER,
    COLLECTOR_VOLUTE,
    CompletePumpDesign,
    FINAL_DISCHARGE_CASING,
    FINAL_VOLUTE,
    FLOW_TYPE_AUTO,
    FLOW_TYPE_MIXED,
    FLOW_TYPE_RADIAL,
    PumpRequirements,
    RETURN_BOWL,
    RETURN_FREE_FORM,
    RETURN_RADIAL,
    size_pump,
)
from core.blade_builder import (
    build_diffuser_solid,
    build_front_shroud_solid,
    build_impeller_solid,
    build_volute_material_solid,
    create_cfd_domain_definition,
    export_turbomachinery_for_openfoam,
)
from core.meridional import create_meridional_design
from core.openfoam_runner import ProcessResult, evaluate_openfoam_case
from meridional_editor import MeridionalEditorDialog


DEFAULT_OPENFOAM_BASHRC = "/usr/lib/openfoam/openfoam2606/etc/bashrc"


def cfd_run_is_supported(design, export_impeller=True, export_diffuser=True):
    """Return whether the current selection can create a complete CFD case."""

    return bool(
        design
        and design.architecture
        and design.architecture.has_complete_assembly_cad
        and export_impeller
        and export_diffuser
    )


def format_cfd_result_summary(result):
    """Create a compact, copyable summary of a gated OpenFOAM result."""

    status = str(result.get("status", "unknown")).upper()
    design_id = result.get("design_id", "-")
    mesh = result.get("mesh") or {}
    performance = result.get("performance") or {}
    gates = result.get("gates") or []
    passed = sum(gate.get("status") == "pass" for gate in gates)
    failed = [gate for gate in gates if gate.get("status") != "pass"]

    def metric(name, suffix, decimals=3):
        value = performance.get(name)
        return "-" if value is None else f"{float(value):.{decimals}f} {suffix}".strip()

    lines = [
        f"CFD {status}",
        f"Design: {design_id}",
        f"Mesh: {mesh.get('cells', '-')} cells; {mesh.get('regions', '-')} region(s)",
        f"Head: {metric('head_m', 'm')}",
        f"Pressure rise: {metric('total_pressure_rise_pa', 'Pa', 1)}",
        f"Hydraulic efficiency: {metric('hydraulic_efficiency_percent', '%', 2)}",
        f"Shaft power: {metric('shaft_power_kw', 'kW')}",
        f"Rotor torque: {metric('rotor_torque_z_n_m', 'N m')}",
        f"Acceptance gates: {passed} passed, {len(failed)} failed",
    ]
    lines.extend(
        f"FAIL {gate.get('name', 'unknown')}: {gate.get('message', '')}"
        for gate in failed
    )
    return "\n".join(lines)


def _export_preview_stl(model, path, tolerance=0.08, angular_tolerance=0.12):
    """Tessellate a viewport proxy without OCCT's unstable parallel mesher."""

    exported = model.val().exportStl(
        path,
        tolerance=tolerance,
        angularTolerance=angular_tolerance,
        ascii=False,
        relative=False,
        parallel=False,
    )
    if not exported:
        raise ValueError(f"OpenCASCADE failed to create preview STL '{path}'.")


class TurbomachineryWorker(QThread):
    """Background worker to size pump and generate 3D CAD without freezing UI."""
    result_ready = Signal(object, str, str, str, str)
    error = Signal(str)
    progress = Signal(str)

    def __init__(self, req: PumpRequirements, gen_impeller: bool, gen_diffuser: bool):
        super().__init__()
        self.req = req
        self.gen_impeller = gen_impeller
        self.gen_diffuser = gen_diffuser

    def run(self):
        try:
            self.progress.emit("⚙️ Computing 1D Meanline & Fluid Properties...")
            design = size_pump(self.req)

            impeller_stl = ""
            impeller_cutaway_stl = ""
            front_shroud_stl = ""
            diffuser_stl = ""
            temp_dir = os.path.join(SCRIPT_DIR, "preview_cache")
            os.makedirs(temp_dir, exist_ok=True)

            if self.gen_impeller:
                if design.impeller.configuration == "Closed":
                    # Build the comparatively small standalone shroud first.
                    # OCCT retains substantial triangulation/Boolean caches;
                    # constructing this third after both large impeller models
                    # caused excessive memory growth and an unresponsive UI.
                    self.progress.emit("Generating separated front-shroud preview...")
                    front_shroud = build_front_shroud_solid(
                        design.impeller,
                        design.meridional,
                    )
                    front_shroud_stl = os.path.join(
                        temp_dir, "temp_front_shroud.stl"
                    )
                    _export_preview_stl(
                        front_shroud,
                        front_shroud_stl,
                        # This is a shaded viewport proxy, not an engineering
                        # export. Fine angular tessellation of the standalone
                        # revolved spline can take minutes in OCCT while adding
                        # no visible value after smooth shading.
                        tolerance=0.15,
                        angular_tolerance=0.25,
                    )
                    del front_shroud
                    gc.collect()
                self.progress.emit("🌀 Generating 3D Impeller Solid (OpenCASCADE)...")
                imp_solid = build_impeller_solid(design.impeller, design.meridional, include_front_shroud=True)
                impeller_stl = os.path.join(temp_dir, "temp_impeller_full.stl")
                _export_preview_stl(imp_solid, impeller_stl, 0.04, 0.08)
                del imp_solid
                gc.collect()
                if design.impeller.configuration == "Closed":
                    self.progress.emit("Generating front-shroud cutaway preview...")
                    imp_cutaway = build_impeller_solid(
                        design.impeller,
                        design.meridional,
                        include_front_shroud=False,
                    )
                    impeller_cutaway_stl = os.path.join(
                        temp_dir, "temp_impeller_cutaway.stl"
                    )
                    _export_preview_stl(
                        imp_cutaway,
                        impeller_cutaway_stl,
                        tolerance=0.04,
                        angular_tolerance=0.08,
                    )
                    del imp_cutaway
                    gc.collect()

            if (
                self.gen_diffuser
                and design.architecture
                and design.architecture.has_supported_stationary_cad
            ):
                self.progress.emit("🔘 Generating 3D Diffuser Stator (OpenCASCADE)...")
                if design.architecture.single_stage_collector == COLLECTOR_VOLUTE:
                    definition = create_cfd_domain_definition(
                        design.impeller,
                        design.diffuser,
                        design.meridional,
                        design.volute,
                    )
                    diff_solid = build_volute_material_solid(
                        design.impeller, design.volute, definition
                    )
                else:
                    diff_solid = build_diffuser_solid(
                        design.diffuser, design.impeller
                    )
                diffuser_stl = os.path.join(temp_dir, "temp_diffuser.stl")
                _export_preview_stl(diff_solid, diffuser_stl, 0.08, 0.12)
                del diff_solid
                gc.collect()

            self.progress.emit("✨ Rendering 3D Geometry in Viewport...")
            self.result_ready.emit(
                design,
                impeller_stl,
                impeller_cutaway_stl,
                front_shroud_stl,
                diffuser_stl,
            )

        except Exception as e:
            err_msg = traceback.format_exc()
            print(f"[Worker Error] {err_msg}")
            self.error.emit(str(e))


class ExportWorker(QThread):
    """Generate high-resolution CAD exports without blocking Qt's event loop."""
    completed = Signal(object)
    error = Signal(str)

    def __init__(self, design, target_dir, export_impeller, export_diffuser):
        super().__init__()
        self.design = design
        self.target_dir = target_dir
        self.export_impeller = export_impeller
        self.export_diffuser = export_diffuser

    def run(self):
        try:
            result = export_turbomachinery_for_openfoam(
                self.design,
                self.target_dir,
                export_impeller=self.export_impeller,
                export_diffuser=self.export_diffuser,
            )
            self.completed.emit(result)
        except Exception as exc:
            self.error.emit(str(exc))


class CfdEvaluationWorker(QThread):
    """Export and solve a CFD case while streaming process output to the GUI."""

    completed = Signal(object, object, str)
    error = Signal(str)
    cancelled = Signal(str)
    progress = Signal(str)
    log = Signal(str)

    def __init__(
        self,
        design,
        target_dir,
        backend,
        wsl_distribution,
        openfoam_bashrc,
        timeout_s,
    ):
        super().__init__()
        self.design = design
        self.target_dir = target_dir
        self.backend = backend
        self.wsl_distribution = wsl_distribution or None
        self.openfoam_bashrc = openfoam_bashrc or None
        self.timeout_s = timeout_s
        self._cancel_requested = threading.Event()
        self._process_lock = threading.Lock()
        self._current_process = None

    def cancel(self):
        self._cancel_requested.set()
        with self._process_lock:
            process = self._current_process
        if process is not None and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass

    def _streaming_executor(self, command, cwd, timeout_s):
        """Run one solver command with bounded memory and responsive cancellation."""

        self.log.emit(f"\n$ {subprocess.list2cmdline(list(command))}")
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        with self._process_lock:
            self._current_process = process

        output_queue = queue.Queue()
        sentinel = object()

        def read_output():
            try:
                for line in process.stdout:
                    output_queue.put(line)
            finally:
                output_queue.put(sentinel)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout_s
        output = []
        pending = []
        last_emit = time.monotonic()

        try:
            while True:
                if self._cancel_requested.is_set():
                    if process.poll() is None:
                        try:
                            process.kill()
                        except OSError:
                            pass
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    if process.poll() is None:
                        process.kill()
                    process.wait()
                    raise subprocess.TimeoutExpired(
                        list(command), timeout_s, output="".join(output)
                    )
                try:
                    item = output_queue.get(timeout=min(0.2, remaining))
                except queue.Empty:
                    item = None
                if item is sentinel:
                    break
                if item is not None:
                    output.append(item)
                    pending.append(item)
                if pending and (
                    len(pending) >= 20 or time.monotonic() - last_emit >= 0.25
                ):
                    self.log.emit("".join(pending).rstrip())
                    pending.clear()
                    last_emit = time.monotonic()
            if pending:
                self.log.emit("".join(pending).rstrip())
            returncode = process.wait()
            stderr = "Cancelled by user." if self._cancel_requested.is_set() else ""
            return ProcessResult(returncode, "".join(output), stderr)
        finally:
            with self._process_lock:
                self._current_process = None

    def run(self):
        case_dir = ""
        try:
            self.progress.emit("Exporting validated CAD and CFD flow domains...")
            self.log.emit(
                "Preparing a new geometry package. STEP stays in millimetres; "
                "OpenFOAM STL geometry uses metres."
            )
            exported = export_turbomachinery_for_openfoam(
                self.design,
                self.target_dir,
                export_impeller=True,
                export_diffuser=True,
            )
            case_dir = exported.get("openfoam_case", "")
            if not case_dir:
                raise ValueError(
                    "The selected architecture did not generate a complete OpenFOAM case."
                )
            if self._cancel_requested.is_set():
                self.cancelled.emit(case_dir)
                return

            self.progress.emit("Running mesh generation and steady-MRF CFD screening...")
            result = evaluate_openfoam_case(
                self.design,
                case_dir,
                backend=self.backend,
                wsl_distribution=self.wsl_distribution,
                openfoam_bashrc=self.openfoam_bashrc,
                timeout_s=self.timeout_s,
                executor=self._streaming_executor,
            )
            if self._cancel_requested.is_set():
                self.cancelled.emit(case_dir)
            else:
                self.completed.emit(result, exported, case_dir)
        except Exception as exc:
            if self._cancel_requested.is_set():
                self.cancelled.emit(case_dir)
            else:
                self.error.emit(str(exc))


class PumpStudioApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Computational Turbomachinery Studio - Impeller & Diffuser Engine")
        self.resize(1380, 860)

        self.current_design: CompletePumpDesign = None
        self.cached_impeller_stl = ""
        self.cached_impeller_cutaway_stl = ""
        self.cached_front_shroud_stl = ""
        self.cached_diffuser_stl = ""
        self.meridional_override = None
        self.worker = None
        self.export_worker = None
        self.cfd_worker = None
        self.last_cfd_case_dir = ""
        self.cfd_output_uses_default = True
        self.design_is_stale = False
        self.inputs_changed_during_computation = False
        self.open_meridional_after_compute = False

        self.init_ui()
        self.apply_dark_theme()

        # Run initial calculation
        self.start_computation()

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        root_layout = QHBoxLayout(central_widget)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(10)

        # =========================================================================
        # 1. LEFT PANEL: INPUT REQUIREMENTS & OPERATING CONDITIONS
        # =========================================================================
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(5, 5, 5, 5)

        # Header Title
        title_box = QWidget()
        t_layout = QVBoxLayout(title_box)
        t_layout.setContentsMargins(0, 0, 0, 0)
        lbl_title = QLabel("Turbomachinery Studio")
        lbl_title.setStyleSheet("font-size: 20px; font-weight: bold; color: #38bdf8;")
        lbl_subtitle = QLabel("Physics-Driven Algorithmic Design Engine")
        lbl_subtitle.setStyleSheet("font-size: 11px; color: #94a3b8;")
        t_layout.addWidget(lbl_title)
        t_layout.addWidget(lbl_subtitle)
        left_layout.addWidget(title_box)

        # Input Form Group
        grp_inputs = QGroupBox("Operating Requirements")
        form_layout = QFormLayout(grp_inputs)
        form_layout.setSpacing(8)

        # Head (m)
        self.spin_head = QDoubleSpinBox()
        self.spin_head.setRange(1.0, 1000.0)
        self.spin_head.setValue(45.0)
        self.spin_head.setSuffix(" m")
        self.spin_head.setDecimals(1)
        form_layout.addRow("Required Head (H):", self.spin_head)

        # Discharge Q (m3/h)
        self.spin_flow = QDoubleSpinBox()
        self.spin_flow.setRange(0.5, 10000.0)
        self.spin_flow.setValue(120.0)
        self.spin_flow.setSuffix(" m³/h")
        self.spin_flow.setDecimals(1)
        form_layout.addRow("Discharge (Q):", self.spin_flow)

        # Rotational Speed (RPM)
        self.spin_rpm = QSpinBox()
        self.spin_rpm.setRange(200, 30000)
        self.spin_rpm.setValue(2950)
        self.spin_rpm.setSuffix(" RPM")
        self.spin_rpm.setSingleStep(50)
        form_layout.addRow("Speed (N):", self.spin_rpm)

        # Liquid Type
        self.combo_liquid = QComboBox()
        self.combo_liquid.addItems(list(FLUID_DISPATCH.keys()))
        self.combo_liquid.setCurrentText("Water")
        self.combo_liquid.currentTextChanged.connect(self.on_liquid_changed)
        form_layout.addRow("Liquid Type:", self.combo_liquid)

        # Operating Temperature (°C)
        self.spin_temp = QDoubleSpinBox()
        self.spin_temp.setRange(-180.0, 200.0)
        self.spin_temp.setValue(25.0)
        self.spin_temp.setSuffix(" °C")
        self.spin_temp.setDecimals(1)
        form_layout.addRow("Operating Temp (T):", self.spin_temp)

        left_layout.addWidget(grp_inputs)

        # Pump and stage architecture. CFturbo treats the project as an ordered
        # component sequence and sizes each impeller from its assigned energy.
        grp_arch = QGroupBox("Pump Architecture")
        arch_layout = QFormLayout(grp_arch)

        self.spin_stage_count = QSpinBox()
        self.spin_stage_count.setRange(1, 5)
        self.spin_stage_count.setValue(1)
        arch_layout.addRow("Number of stages:", self.spin_stage_count)

        self.combo_head_split = QComboBox()
        self.combo_head_split.addItem("Equal head per stage", "Equal")
        arch_layout.addRow("Head distribution:", self.combo_head_split)

        self.combo_flow_type = QComboBox()
        self.combo_flow_type.addItem("Auto - validate topology", FLOW_TYPE_AUTO)
        self.combo_flow_type.addItem("Radial", FLOW_TYPE_RADIAL)
        self.combo_flow_type.addItem(
            "Mixed-flow - CAD not implemented", FLOW_TYPE_MIXED
        )
        arch_layout.addRow("Impeller flow type:", self.combo_flow_type)

        self.combo_single_collector = QComboBox()
        self.combo_single_collector.addItem(COLLECTOR_VOLUTE, COLLECTOR_VOLUTE)
        self.combo_single_collector.addItem(
            COLLECTOR_VANED_DIFFUSER, COLLECTOR_VANED_DIFFUSER
        )
        self.combo_single_collector.addItem(
            COLLECTOR_VANELESS_DIFFUSER, COLLECTOR_VANELESS_DIFFUSER
        )
        arch_layout.addRow("Single-stage collector:", self.combo_single_collector)

        self.combo_interstage_return = QComboBox()
        self.combo_interstage_return.addItem(RETURN_RADIAL, RETURN_RADIAL)
        self.combo_interstage_return.addItem(RETURN_BOWL, RETURN_BOWL)
        self.combo_interstage_return.addItem(RETURN_FREE_FORM, RETURN_FREE_FORM)
        arch_layout.addRow("Interstage return:", self.combo_interstage_return)

        self.combo_final_collector = QComboBox()
        self.combo_final_collector.addItem(FINAL_VOLUTE, FINAL_VOLUTE)
        self.combo_final_collector.addItem(
            FINAL_DISCHARGE_CASING, FINAL_DISCHARGE_CASING
        )
        arch_layout.addRow("Final collector:", self.combo_final_collector)
        left_layout.addWidget(grp_arch)

        # Generation Components Group
        grp_comps = QGroupBox("Components to Build")
        comp_layout = QVBoxLayout(grp_comps)
        self.chk_gen_impeller = QCheckBox("Impeller (Rotor)")
        self.chk_gen_impeller.setChecked(True)
        self.chk_gen_diffuser = QCheckBox("Vaned Diffuser (Stator)")
        self.chk_gen_diffuser.setChecked(True)
        self.combo_impeller_config = QComboBox()
        self.combo_impeller_config.addItem("Closed — Front + Rear Shroud", "Closed")
        self.combo_impeller_config.addItem("Open — Rear Shroud Only", "Open")
        comp_layout.addWidget(self.chk_gen_impeller)
        comp_layout.addWidget(self.chk_gen_diffuser)
        comp_layout.addWidget(QLabel("Impeller configuration:"))
        comp_layout.addWidget(self.combo_impeller_config)
        self.chk_eye_collar = QCheckBox("Eye collar / wear-ring land")
        self.chk_eye_collar.setChecked(True)
        self.chk_eye_collar.setToolTip(
            "Add the axial inlet neck to the front-shroud material while "
            "preserving the hydraulic suction-eye diameter."
        )
        self.spin_eye_collar_length = QDoubleSpinBox()
        self.spin_eye_collar_length.setRange(1.0, 40.0)
        self.spin_eye_collar_length.setValue(8.0)
        self.spin_eye_collar_length.setDecimals(1)
        self.spin_eye_collar_length.setSuffix(" mm")
        comp_layout.addWidget(self.chk_eye_collar)
        collar_form = QFormLayout()
        collar_form.addRow("Collar axial length:", self.spin_eye_collar_length)
        comp_layout.addLayout(collar_form)
        left_layout.addWidget(grp_comps)

        self.btn_edit_meridional = QPushButton("Edit Meridional Profile (2D)")
        self.btn_edit_meridional.setEnabled(False)
        self.btn_edit_meridional.setToolTip(
            "Edit the five-point hub and shroud Bezier contours with live "
            "passage, curvature, and static-moment checks."
        )
        self.btn_edit_meridional.clicked.connect(self.open_meridional_editor)
        left_layout.addWidget(self.btn_edit_meridional)

        self.spin_stage_count.valueChanged.connect(self._update_architecture_controls)
        self.combo_single_collector.currentIndexChanged.connect(
            self._update_architecture_controls
        )
        self.combo_impeller_config.currentIndexChanged.connect(
            self._update_impeller_material_controls
        )
        self.chk_eye_collar.toggled.connect(self._update_impeller_material_controls)
        self._update_architecture_controls()
        self._update_impeller_material_controls()

        # Generate Button
        self.btn_compute = QPushButton("🚀 Compute & Generate 3D CAD")
        self.btn_compute.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #2563eb, stop:1 #3b82f6);
                color: white;
                font-weight: bold;
                padding: 12px;
                border-radius: 6px;
                font-size: 14px;
            }
            QPushButton:hover {
                background: #1d4ed8;
            }
            QPushButton:disabled {
                background: #475569;
                color: #94a3b8;
            }
        """)
        self.btn_compute.clicked.connect(self.start_computation)
        left_layout.addWidget(self.btn_compute)

        # Progress Bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(False)
        left_layout.addWidget(self.progress_bar)

        # Status Label
        self.lbl_status = QLabel("Ready.")
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setStyleSheet("color: #38bdf8; font-size: 12px;")
        left_layout.addWidget(self.lbl_status)

        # Export for OpenFOAM Button
        self.btn_export = QPushButton("💾 Export CAD + OpenFOAM Case")
        self.btn_export.setStyleSheet("""
            QPushButton {
                background-color: #059669;
                color: white;
                font-weight: bold;
                padding: 12px;
                border-radius: 6px;
                font-size: 13px;
            }
            QPushButton:hover {
                background-color: #047857;
            }
        """)
        self.btn_export.clicked.connect(self.export_openfoam_files)
        left_layout.addWidget(self.btn_export)

        left_layout.addStretch()
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setFixedWidth(410)
        left_scroll.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
        )
        left_scroll.setWidget(left_panel)
        root_layout.addWidget(left_scroll)

        # =========================================================================
        # 2. CENTER PANEL: 3D VIEWPORT & VIEW CONTROLS
        # =========================================================================
        center_panel = QWidget()
        center_layout = QVBoxLayout(center_panel)
        center_layout.setContentsMargins(0, 0, 0, 0)

        self.workspace_tabs = QTabWidget(center_panel)
        viewport_page = QWidget()
        viewport_layout = QVBoxLayout(viewport_page)
        viewport_layout.setContentsMargins(0, 0, 0, 0)

        # Viewport Toolbar
        toolbar = QWidget()
        tb_layout = QHBoxLayout(toolbar)
        tb_layout.setContentsMargins(0, 0, 0, 0)

        tb_layout.addWidget(QLabel("<b>3D CAD View:</b>"))
        self.lbl_view_state = QLabel("GENERATING")
        self.lbl_view_state.setToolTip(
            "Indicates whether the displayed CAD matches the current input values."
        )
        tb_layout.addWidget(self.lbl_view_state)
        self.rb_view_both = QRadioButton("Assembly (Both)")
        self.rb_view_both.setChecked(True)
        self.rb_view_imp = QRadioButton("Impeller Only")
        self.rb_view_diff = QRadioButton("Diffuser Only")

        self.view_grp = QButtonGroup(self)
        self.view_grp.addButton(self.rb_view_both)
        self.view_grp.addButton(self.rb_view_imp)
        self.view_grp.addButton(self.rb_view_diff)
        self.view_grp.buttonClicked.connect(self.update_viewport_display)

        tb_layout.addWidget(self.rb_view_both)
        tb_layout.addWidget(self.rb_view_imp)
        tb_layout.addWidget(self.rb_view_diff)

        self.chk_feature_edges = QCheckBox("Edges")
        self.chk_feature_edges.setChecked(True)
        self.chk_feature_edges.setToolTip(
            "Show boundaries, silhouettes, and sharp CAD-like crease lines."
        )
        self.chk_feature_edges.toggled.connect(self.update_viewport_display)
        tb_layout.addWidget(self.chk_feature_edges)

        self.chk_show_mesh_edges = QCheckBox("Triangles")
        self.chk_show_mesh_edges.setChecked(False)
        self.chk_show_mesh_edges.setToolTip(
            "Show the dense STL tessellation used for preview and CFD export."
        )
        self.chk_show_mesh_edges.toggled.connect(self.update_viewport_display)
        tb_layout.addWidget(self.chk_show_mesh_edges)

        self.chk_cutaway_shroud = QCheckBox("Hide Shroud")
        self.chk_cutaway_shroud.setChecked(False)
        self.chk_cutaway_shroud.setEnabled(False)
        self.chk_cutaway_shroud.setToolTip(
            "Preview only: hide the front shroud to inspect blade passages. "
            "Closed-impeller CAD exports retain the front shroud."
        )
        self.chk_cutaway_shroud.toggled.connect(self.update_viewport_display)
        tb_layout.addWidget(self.chk_cutaway_shroud)

        self.chk_transparent_diffuser = QCheckBox("Diffuser X-ray")
        self.chk_transparent_diffuser.setChecked(True)
        self.chk_transparent_diffuser.toggled.connect(self.update_viewport_display)
        tb_layout.addWidget(self.chk_transparent_diffuser)
        tb_layout.addStretch()

        self.btn_reset_cam = QPushButton("Reset Camera")
        self.btn_reset_cam.clicked.connect(self.reset_camera)
        tb_layout.addWidget(self.btn_reset_cam)

        viewport_layout.addWidget(toolbar)

        # PyVistaQt 3D Viewport
        self.plotter = QtInteractor(viewport_page)
        self.plotter.set_background('#cfd4da', top='#f5f6f7')
        self.plotter.show_axes()
        try:
            self.plotter.enable_anti_aliasing("fxaa")
        except Exception:
            # Some older VTK/OpenGL combinations do not expose FXAA.
            pass
        viewport_layout.addWidget(self.plotter.interactor, stretch=1)
        self.workspace_tabs.addTab(viewport_page, "3D CAD")

        cfd_page = QWidget()
        cfd_layout = QVBoxLayout(cfd_page)
        cfd_layout.setContentsMargins(12, 12, 12, 12)

        cfd_setup = QGroupBox("Automated steady-MRF screening")
        cfd_form = QFormLayout(cfd_setup)
        self.combo_cfd_backend = QComboBox()
        self.combo_cfd_backend.addItem("Automatic", "auto")
        self.combo_cfd_backend.addItem("WSL (Windows)", "wsl")
        self.combo_cfd_backend.addItem("Local Linux", "local")
        self.combo_cfd_backend.setCurrentIndex(1 if os.name == "nt" else 2)
        self.combo_cfd_backend.currentIndexChanged.connect(
            self._update_cfd_backend_controls
        )
        cfd_form.addRow("Execution backend:", self.combo_cfd_backend)

        self.edit_cfd_distribution = QLineEdit("Ubuntu")
        self.edit_cfd_distribution.setToolTip(
            "WSL distribution containing the OpenFOAM installation."
        )
        cfd_form.addRow("WSL distribution:", self.edit_cfd_distribution)

        self.edit_cfd_bashrc = QLineEdit(DEFAULT_OPENFOAM_BASHRC)
        self.edit_cfd_bashrc.setToolTip(
            "OpenFOAM environment script inside Linux/WSL."
        )
        cfd_form.addRow("OpenFOAM bashrc:", self.edit_cfd_bashrc)

        output_row = QWidget()
        output_layout = QHBoxLayout(output_row)
        output_layout.setContentsMargins(0, 0, 0, 0)
        self.edit_cfd_output = QLineEdit(
            os.path.join(SCRIPT_DIR, "output", "gui_cfd")
        )
        self.edit_cfd_output.textEdited.connect(self._mark_custom_cfd_output)
        self.btn_browse_cfd_output = QPushButton("Browse...")
        self.btn_browse_cfd_output.clicked.connect(self.browse_cfd_output)
        output_layout.addWidget(self.edit_cfd_output, stretch=1)
        output_layout.addWidget(self.btn_browse_cfd_output)
        cfd_form.addRow("Output directory:", output_row)

        self.spin_cfd_timeout_hours = QDoubleSpinBox()
        self.spin_cfd_timeout_hours.setRange(0.1, 24.0)
        self.spin_cfd_timeout_hours.setValue(2.0)
        self.spin_cfd_timeout_hours.setDecimals(1)
        self.spin_cfd_timeout_hours.setSuffix(" h per command")
        cfd_form.addRow("Timeout:", self.spin_cfd_timeout_hours)
        cfd_layout.addWidget(cfd_setup)

        cfd_actions = QHBoxLayout()
        self.btn_run_cfd = QPushButton("Run CFD Evaluation")
        self.btn_run_cfd.setEnabled(False)
        self.btn_run_cfd.setStyleSheet(
            "QPushButton { background-color: #0284c7; color: white; "
            "font-weight: bold; padding: 10px; border-radius: 5px; } "
            "QPushButton:hover { background-color: #0369a1; } "
            "QPushButton:disabled { background-color: #475569; color: #94a3b8; }"
        )
        self.btn_run_cfd.clicked.connect(self.start_cfd_evaluation)
        self.btn_cancel_cfd = QPushButton("Cancel")
        self.btn_cancel_cfd.setEnabled(False)
        self.btn_cancel_cfd.clicked.connect(self.cancel_cfd_evaluation)
        self.btn_open_cfd_folder = QPushButton("Open Results Folder")
        self.btn_open_cfd_folder.setEnabled(False)
        self.btn_open_cfd_folder.clicked.connect(self.open_cfd_results_folder)
        cfd_actions.addWidget(self.btn_run_cfd)
        cfd_actions.addWidget(self.btn_cancel_cfd)
        cfd_actions.addWidget(self.btn_open_cfd_folder)
        cfd_actions.addStretch()
        cfd_layout.addLayout(cfd_actions)

        self.lbl_cfd_status = QLabel(
            "Compute a complete single-stage volute design to enable CFD evaluation."
        )
        self.lbl_cfd_status.setWordWrap(True)
        self.lbl_cfd_status.setStyleSheet("color: #38bdf8; font-weight: bold;")
        cfd_layout.addWidget(self.lbl_cfd_status)

        self.cfd_summary = QPlainTextEdit()
        self.cfd_summary.setReadOnly(True)
        self.cfd_summary.setPlaceholderText(
            "Mesh, convergence, pressure rise, head, power, efficiency, and "
            "acceptance gates will appear here."
        )
        self.cfd_summary.setMaximumHeight(180)
        cfd_layout.addWidget(self.cfd_summary)

        self.cfd_log = QPlainTextEdit()
        self.cfd_log.setReadOnly(True)
        self.cfd_log.document().setMaximumBlockCount(5000)
        self.cfd_log.setPlaceholderText("Live OpenFOAM command output will appear here.")
        cfd_layout.addWidget(self.cfd_log, stretch=1)

        self.workspace_tabs.addTab(cfd_page, "CFD Evaluation")
        center_layout.addWidget(self.workspace_tabs)
        self._update_cfd_backend_controls()

        root_layout.addWidget(center_panel, stretch=2)

        # =========================================================================
        # 3. RIGHT PANEL: ENGINEERING RESULTS DASHBOARD
        # =========================================================================
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(5, 5, 5, 5)
        right_panel.setFixedWidth(380)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        dashboard_widget = QWidget()
        dash_layout = QVBoxLayout(dashboard_widget)
        dash_layout.setSpacing(10)

        grp_arch_result = QGroupBox("Pump Architecture & Stage Plan")
        f_arch_result = QFormLayout(grp_arch_result)
        self.lbl_arch_config = QLabel("-")
        self.lbl_stage_heads = QLabel("-")
        self.lbl_stage_nq = QLabel("-")
        self.lbl_flow_topology = QLabel("-")
        self.lbl_component_path = QLabel("-")
        self.lbl_component_path.setWordWrap(True)
        self.lbl_cad_scope = QLabel("-")
        self.lbl_cad_scope.setWordWrap(True)
        f_arch_result.addRow("Configuration:", self.lbl_arch_config)
        f_arch_result.addRow("Stage heads:", self.lbl_stage_heads)
        f_arch_result.addRow("Stage Nq:", self.lbl_stage_nq)
        f_arch_result.addRow("Resolved topology:", self.lbl_flow_topology)
        f_arch_result.addRow("Component path:", self.lbl_component_path)
        f_arch_result.addRow("Current CAD scope:", self.lbl_cad_scope)
        dash_layout.addWidget(grp_arch_result)

        # 1. Performance & Efficiencies
        grp_perf = QGroupBox("Performance & Power")
        f_perf = QFormLayout(grp_perf)
        self.lbl_nq = QLabel("-")
        self.lbl_eta_total = QLabel("-")
        self.lbl_eta_hyd = QLabel("-")
        self.lbl_power_shaft = QLabel("-")
        self.lbl_power_hyd = QLabel("-")
        self.lbl_npsh = QLabel("-")
        f_perf.addRow("Specific Speed (Nq):", self.lbl_nq)
        f_perf.addRow("Total Efficiency (η):", self.lbl_eta_total)
        f_perf.addRow("Hydraulic Efficiency (η_h):", self.lbl_eta_hyd)
        f_perf.addRow("Shaft Power (P_shaft):", self.lbl_power_shaft)
        f_perf.addRow("Hydraulic Power (P_hyd):", self.lbl_power_hyd)
        f_perf.addRow("Estimated NPSHr:", self.lbl_npsh)
        dash_layout.addWidget(grp_perf)

        # 2. Fluid Thermodynamic State
        grp_fluid = QGroupBox("Fluid Thermodynamic State")
        f_fluid = QFormLayout(grp_fluid)
        self.lbl_rho = QLabel("-")
        self.lbl_visc = QLabel("-")
        self.lbl_pv = QLabel("-")
        f_fluid.addRow("Density (ρ):", self.lbl_rho)
        f_fluid.addRow("Kinematic Viscosity (ν):", self.lbl_visc)
        f_fluid.addRow("Vapor Pressure (Pv):", self.lbl_pv)
        dash_layout.addWidget(grp_fluid)

        # 3. Impeller Sizing (Rotor)
        grp_imp_dim = QGroupBox("Impeller Dimensions (Rotor)")
        f_imp = QFormLayout(grp_imp_dim)
        self.lbl_imp_eye = QLabel("-")
        self.lbl_imp_config = QLabel("-")
        self.lbl_imp_hub = QLabel("-")
        self.lbl_imp_d2 = QLabel("-")
        self.lbl_imp_b2 = QLabel("-")
        self.lbl_imp_z = QLabel("-")
        self.lbl_imp_beta1 = QLabel("-")
        self.lbl_imp_beta2 = QLabel("-")
        self.lbl_imp_blade_surface = QLabel("-")
        self.lbl_imp_blade_edges = QLabel("-")
        self.lbl_imp_min_throat = QLabel("-")
        self.lbl_imp_throat_area = QLabel("-")
        self.lbl_imp_passage_area = QLabel("-")
        self.lbl_imp_u2 = QLabel("-")
        self.lbl_imp_slip = QLabel("-")
        self.lbl_imp_intake_coefficient = QLabel("-")
        self.lbl_imp_width_ratio = QLabel("-")
        self.lbl_imp_meridional_ratio = QLabel("-")
        self.lbl_shroud_alignment = QLabel("-")
        self.lbl_shroud_thickness = QLabel("-")
        self.lbl_eye_collar = QLabel("-")
        f_imp.addRow("Suction Eye (Ds):", self.lbl_imp_eye)
        f_imp.addRow("Configuration:", self.lbl_imp_config)
        f_imp.addRow("Hub Diameter (Dh):", self.lbl_imp_hub)
        f_imp.addRow("Outlet Diameter (D2):", self.lbl_imp_d2)
        f_imp.addRow("Outlet Width (b2):", self.lbl_imp_b2)
        f_imp.addRow("Blade Count (Z):", self.lbl_imp_z)
        f_imp.addRow("Inlet Blade Angle (β1):", self.lbl_imp_beta1)
        f_imp.addRow("Outlet Blade Angle (β2):", self.lbl_imp_beta2)
        f_imp.addRow("Blade mean surface:", self.lbl_imp_blade_surface)
        f_imp.addRow("Blade edges:", self.lbl_imp_blade_edges)
        f_imp.addRow("Minimum blade throat:", self.lbl_imp_min_throat)
        f_imp.addRow("Throat area:", self.lbl_imp_throat_area)
        f_imp.addRow("Passage-area progression:", self.lbl_imp_passage_area)
        f_imp.addRow("Tip Speed (u2):", self.lbl_imp_u2)
        f_imp.addRow("Wiesner Slip Factor (σ):", self.lbl_imp_slip)
        f_imp.addRow("Intake coefficient (epsilon):", self.lbl_imp_intake_coefficient)
        f_imp.addRow("Outlet width ratio (b2/D2):", self.lbl_imp_width_ratio)
        f_imp.addRow("Meridional ratio (cm2/cm1):", self.lbl_imp_meridional_ratio)
        f_imp.addRow("Outlet shrouds:", self.lbl_shroud_alignment)
        f_imp.addRow("Rear / front thickness:", self.lbl_shroud_thickness)
        f_imp.addRow("Eye collar:", self.lbl_eye_collar)
        dash_layout.addWidget(grp_imp_dim)

        # 4. Diffuser Sizing (Stator)
        grp_diff_dim = QGroupBox("Diffuser Dimensions (Stator)")
        f_diff = QFormLayout(grp_diff_dim)
        self.lbl_diff_d3 = QLabel("-")
        self.lbl_diff_d4 = QLabel("-")
        self.lbl_diff_b3 = QLabel("-")
        self.lbl_diff_zd = QLabel("-")
        self.lbl_diff_beta3 = QLabel("-")
        self.lbl_diff_beta4 = QLabel("-")
        f_diff.addRow("Inlet Diameter (D3):", self.lbl_diff_d3)
        f_diff.addRow("Outlet Diameter (D4):", self.lbl_diff_d4)
        f_diff.addRow("Passage Width (b3):", self.lbl_diff_b3)
        f_diff.addRow("Vane Count (Zd):", self.lbl_diff_zd)
        f_diff.addRow("Vane Inlet Angle (β3):", self.lbl_diff_beta3)
        f_diff.addRow("Vane Outlet Angle (β4):", self.lbl_diff_beta4)
        dash_layout.addWidget(grp_diff_dim)

        # 5. Traceability and deterministic engineering checks
        grp_checks = QGroupBox("Design Validation")
        f_checks = QFormLayout(grp_checks)
        self.lbl_design_id = QLabel("-")
        self.lbl_meridional = QLabel("-")
        self.lbl_check_summary = QLabel("-")
        self.lbl_check_warnings = QLabel("-")
        self.lbl_check_warnings.setWordWrap(True)
        f_checks.addRow("Design ID:", self.lbl_design_id)
        f_checks.addRow("Meridional model:", self.lbl_meridional)
        f_checks.addRow("Checks:", self.lbl_check_summary)
        f_checks.addRow("Advisories:", self.lbl_check_warnings)
        dash_layout.addWidget(grp_checks)

        dash_layout.addStretch()
        scroll_area.setWidget(dashboard_widget)
        right_layout.addWidget(scroll_area)

        root_layout.addWidget(right_panel)
        self._connect_design_stale_signals()

    def _connect_design_stale_signals(self):
        for widget in (
            self.spin_head,
            self.spin_flow,
            self.spin_rpm,
            self.spin_temp,
            self.spin_stage_count,
            self.spin_eye_collar_length,
        ):
            widget.valueChanged.connect(self._mark_design_stale)

        for widget in (
            self.combo_liquid,
            self.combo_head_split,
            self.combo_flow_type,
            self.combo_single_collector,
            self.combo_interstage_return,
            self.combo_final_collector,
            self.combo_impeller_config,
        ):
            widget.currentIndexChanged.connect(self._mark_design_stale)

        self.chk_gen_impeller.toggled.connect(self._mark_design_stale)
        self.chk_gen_diffuser.toggled.connect(self._mark_design_stale)
        self.chk_eye_collar.toggled.connect(self._mark_design_stale)

    def _mark_design_stale(self, *args):
        if self.worker is not None and self.worker.isRunning():
            self.inputs_changed_during_computation = True
            self.meridional_override = None
            return
        if self.current_design is None:
            return
        self.meridional_override = None
        self.design_is_stale = True
        self.btn_export.setEnabled(False)
        if hasattr(self, "btn_run_cfd"):
            self.btn_run_cfd.setEnabled(False)
            self.lbl_cfd_status.setText(
                "Inputs changed. Regenerate the design before starting another CFD run."
            )
        self.btn_edit_meridional.setEnabled(
            self.spin_stage_count.value() == 1
            and bool(self.cached_impeller_stl)
            and self.chk_gen_impeller.isChecked()
        )
        self._set_view_state("stale")
        self.lbl_status.setText(
            "Inputs changed. The previous valid geometry remains displayed; "
            "click Compute to regenerate it for the new values."
        )

    def _set_view_state(self, state: str) -> None:
        presentations = {
            "current": ("CURRENT", "#16a34a", "Displayed CAD matches the current inputs."),
            "stale": (
                "STALE - PREVIOUS RESULT",
                "#f59e0b",
                "Inputs changed; displayed CAD and dashboard show the previous valid result.",
            ),
            "generating": (
                "REGENERATING",
                "#38bdf8",
                "Geometry is being regenerated; the previous valid result remains visible.",
            ),
            "empty": (
                "NO VALID RESULT",
                "#ef4444",
                "No valid geometry has been generated yet.",
            ),
        }
        text, color, tooltip = presentations[state]
        self.lbl_view_state.setText(text)
        self.lbl_view_state.setStyleSheet(
            f"color: {color}; font-weight: bold; padding: 2px 6px;"
        )
        self.lbl_view_state.setToolTip(tooltip)

    def _clear_dashboard_labels(self):
        for label in (
            self.lbl_arch_config, self.lbl_stage_heads, self.lbl_stage_nq,
            self.lbl_flow_topology, self.lbl_component_path, self.lbl_cad_scope,
            self.lbl_nq, self.lbl_eta_total, self.lbl_eta_hyd,
            self.lbl_power_shaft, self.lbl_power_hyd, self.lbl_npsh,
            self.lbl_rho, self.lbl_visc, self.lbl_pv,
            self.lbl_imp_eye, self.lbl_imp_config, self.lbl_imp_hub,
            self.lbl_imp_d2, self.lbl_imp_b2, self.lbl_imp_z,
            self.lbl_imp_beta1, self.lbl_imp_beta2,
            self.lbl_imp_blade_surface, self.lbl_imp_blade_edges,
            self.lbl_imp_min_throat,
            self.lbl_imp_throat_area, self.lbl_imp_passage_area,
            self.lbl_imp_u2,
            self.lbl_imp_slip, self.lbl_imp_intake_coefficient,
            self.lbl_imp_width_ratio, self.lbl_imp_meridional_ratio,
            self.lbl_shroud_alignment,
            self.lbl_shroud_thickness, self.lbl_eye_collar,
            self.lbl_diff_d3, self.lbl_diff_d4,
            self.lbl_diff_b3, self.lbl_diff_zd, self.lbl_diff_beta3,
            self.lbl_diff_beta4, self.lbl_design_id, self.lbl_meridional,
            self.lbl_check_summary, self.lbl_check_warnings,
        ):
            label.setText("-")

    def _update_architecture_controls(self, _value=None):
        """Show only controls and CAD toggles valid for the selected arrangement."""
        multistage = self.spin_stage_count.value() > 1
        self.combo_head_split.setEnabled(multistage)
        self.combo_single_collector.setEnabled(not multistage)
        self.combo_interstage_return.setEnabled(multistage)
        self.combo_final_collector.setEnabled(multistage)

        if multistage:
            self.chk_gen_diffuser.setText("Interstage return-channel CAD (pending)")
            self.chk_gen_diffuser.setChecked(False)
            self.chk_gen_diffuser.setEnabled(False)
        else:
            collector = self.combo_single_collector.currentData()
            supported = collector in {COLLECTOR_VOLUTE, COLLECTOR_VANED_DIFFUSER}
            self.chk_gen_diffuser.setText(
                "Volute + discharge diffuser"
                if collector == COLLECTOR_VOLUTE
                else "Vaned radial diffuser (stator)"
            )
            self.chk_gen_diffuser.setEnabled(supported)
            self.chk_gen_diffuser.setChecked(supported)

    def _update_impeller_material_controls(self, _value=None):
        closed = self.combo_impeller_config.currentData() == "Closed"
        self.chk_eye_collar.setEnabled(closed)
        self.spin_eye_collar_length.setEnabled(
            closed and self.chk_eye_collar.isChecked()
        )

    def _update_cfd_backend_controls(self, _value=None):
        backend = self.combo_cfd_backend.currentData()
        uses_wsl = backend == "wsl" or (backend == "auto" and os.name == "nt")
        self.edit_cfd_distribution.setEnabled(uses_wsl)

    def _mark_custom_cfd_output(self, _text):
        self.cfd_output_uses_default = False

    def browse_cfd_output(self):
        start_dir = self.edit_cfd_output.text().strip() or SCRIPT_DIR
        target_dir = QFileDialog.getExistingDirectory(
            self, "Select CFD Output Directory", start_dir
        )
        if target_dir:
            self.cfd_output_uses_default = False
            self.edit_cfd_output.setText(target_dir)

    def _cfd_selection_is_supported(self):
        return cfd_run_is_supported(
            self.current_design,
            self.chk_gen_impeller.isChecked(),
            self.chk_gen_diffuser.isChecked(),
        )

    def _set_cfd_controls_enabled(self, enabled):
        self.combo_cfd_backend.setEnabled(enabled)
        self.edit_cfd_bashrc.setEnabled(enabled)
        self.edit_cfd_output.setEnabled(enabled)
        self.btn_browse_cfd_output.setEnabled(enabled)
        self.spin_cfd_timeout_hours.setEnabled(enabled)
        if enabled:
            self._update_cfd_backend_controls()
        else:
            self.edit_cfd_distribution.setEnabled(False)

    def _restore_actions_after_cfd(self):
        self.progress_bar.setVisible(False)
        self.btn_cancel_cfd.setEnabled(False)
        self._set_cfd_controls_enabled(True)
        self.btn_compute.setEnabled(True)
        multistage = bool(
            self.current_design
            and self.current_design.architecture
            and self.current_design.architecture.is_multistage
        )
        self.btn_export.setEnabled(
            bool(self.current_design and not self.design_is_stale and not multistage)
        )
        self.btn_run_cfd.setEnabled(
            bool(not self.design_is_stale and self._cfd_selection_is_supported())
        )
        self.btn_edit_meridional.setEnabled(
            bool(
                self.current_design
                and self.spin_stage_count.value() == 1
                and self.cached_impeller_stl
                and self.chk_gen_impeller.isChecked()
            )
        )

    def start_cfd_evaluation(self):
        if self.current_design is None:
            QMessageBox.warning(self, "No Design", "Please compute geometry first.")
            return
        if self.design_is_stale:
            QMessageBox.warning(
                self,
                "Stale design",
                "Regenerate the current inputs before running CFD.",
            )
            return
        if not self._cfd_selection_is_supported():
            QMessageBox.warning(
                self,
                "CFD case unavailable",
                "Automated CFD currently requires a complete single-stage volute "
                "assembly with both the impeller and volute selected.",
            )
            return
        if self.cfd_worker is not None and self.cfd_worker.isRunning():
            return

        target_text = self.edit_cfd_output.text().strip()
        if not target_text:
            QMessageBox.warning(
                self, "CFD output", "Select an output directory for the CFD run."
            )
            return
        target_dir = os.path.abspath(target_text)

        backend = self.combo_cfd_backend.currentData()
        self.cfd_log.clear()
        self.cfd_summary.clear()
        self.last_cfd_case_dir = ""
        self.btn_open_cfd_folder.setEnabled(False)
        self.workspace_tabs.setCurrentIndex(1)
        self.lbl_cfd_status.setText(
            "Preparing geometry. This screening run can take a significant amount of time."
        )
        self.lbl_status.setText("Preparing automated CFD evaluation...")
        self.progress_bar.setVisible(True)
        self.btn_compute.setEnabled(False)
        self.btn_export.setEnabled(False)
        self.btn_run_cfd.setEnabled(False)
        self.btn_edit_meridional.setEnabled(False)
        self.btn_cancel_cfd.setEnabled(True)
        self._set_cfd_controls_enabled(False)

        self.cfd_worker = CfdEvaluationWorker(
            self.current_design,
            target_dir,
            backend,
            self.edit_cfd_distribution.text().strip(),
            self.edit_cfd_bashrc.text().strip(),
            self.spin_cfd_timeout_hours.value() * 3600.0,
        )
        self.cfd_worker.progress.connect(self.on_cfd_progress)
        self.cfd_worker.log.connect(self.append_cfd_log)
        self.cfd_worker.completed.connect(self.on_cfd_finished)
        self.cfd_worker.error.connect(self.on_cfd_error)
        self.cfd_worker.cancelled.connect(self.on_cfd_cancelled)
        self.cfd_worker.start()

    def on_cfd_progress(self, message):
        self.lbl_cfd_status.setText(message)
        self.lbl_status.setText(message)

    def append_cfd_log(self, message):
        if message:
            self.cfd_log.appendPlainText(message.rstrip())

    def cancel_cfd_evaluation(self):
        if self.cfd_worker is None or not self.cfd_worker.isRunning():
            return
        self.btn_cancel_cfd.setEnabled(False)
        self.lbl_cfd_status.setText("Cancellation requested; stopping the active command...")
        self.cfd_worker.cancel()

    def on_cfd_finished(self, result, _exported_files, case_dir):
        self.last_cfd_case_dir = case_dir
        self.btn_open_cfd_folder.setEnabled(bool(case_dir))
        summary = format_cfd_result_summary(result)
        self.cfd_summary.setPlainText(summary)
        self.append_cfd_log("\n" + summary)
        passed = result.get("status") == "passed"
        self.lbl_cfd_status.setText(
            "CFD evaluation passed every acceptance gate."
            if passed
            else "CFD completed, but one or more acceptance gates failed."
        )
        self.lbl_status.setText(
            "CFD evaluation passed."
            if passed
            else "CFD evaluation finished with failed engineering gates."
        )
        self._restore_actions_after_cfd()
        message = (
            f"{summary}\n\nFull result:\n"
            f"{os.path.join(case_dir, 'simulation_result.json')}"
        )
        if passed:
            QMessageBox.information(self, "CFD Evaluation Passed", message)
        else:
            QMessageBox.warning(self, "CFD Evaluation Failed Gates", message)

    def on_cfd_error(self, message):
        self.lbl_cfd_status.setText(f"CFD evaluation error: {message}")
        self.lbl_status.setText(f"CFD evaluation error: {message}")
        self.append_cfd_log(f"\nERROR: {message}")
        self._restore_actions_after_cfd()
        QMessageBox.critical(self, "CFD Evaluation Error", message)

    def on_cfd_cancelled(self, case_dir):
        self.last_cfd_case_dir = case_dir
        self.btn_open_cfd_folder.setEnabled(bool(case_dir))
        self.lbl_cfd_status.setText("CFD evaluation cancelled.")
        self.lbl_status.setText("CFD evaluation cancelled.")
        self.append_cfd_log("\nCFD evaluation cancelled by the user.")
        self._restore_actions_after_cfd()

    def open_cfd_results_folder(self):
        if self.last_cfd_case_dir and os.path.isdir(self.last_cfd_case_dir):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.last_cfd_case_dir))
        else:
            QMessageBox.warning(
                self, "CFD results", "No completed CFD case directory is available."
            )

    def on_liquid_changed(self, liquid_name: str):
        low, high = FLUID_TEMPERATURE_RANGES[liquid_name]
        defaults = {"Liquid Methane (LNG)": -161.5}
        self.spin_temp.setRange(low, high)
        self.spin_temp.setValue(defaults.get(liquid_name, min(25.0, high)))

    def start_computation(self):
        self.btn_compute.setEnabled(False)
        self.btn_export.setEnabled(False)
        self.btn_run_cfd.setEnabled(False)
        self.btn_edit_meridional.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.inputs_changed_during_computation = False
        self._set_view_state("generating")

        req = PumpRequirements(
            head_m=self.spin_head.value(),
            discharge_m3_h=self.spin_flow.value(),
            rpm=float(self.spin_rpm.value()),
            liquid_type=self.combo_liquid.currentText(),
            temperature_c=self.spin_temp.value(),
            impeller_configuration=self.combo_impeller_config.currentData(),
            stage_count=self.spin_stage_count.value(),
            impeller_flow_type=self.combo_flow_type.currentData(),
            single_stage_collector=self.combo_single_collector.currentData(),
            interstage_return_type=self.combo_interstage_return.currentData(),
            final_collector=self.combo_final_collector.currentData(),
            eye_collar_enabled=self.chk_eye_collar.isChecked(),
            eye_collar_length_mm=self.spin_eye_collar_length.value(),
            meridional_override=self.meridional_override,
        )

        gen_imp = self.chk_gen_impeller.isChecked()
        gen_diff = self.chk_gen_diffuser.isChecked()

        self.worker = TurbomachineryWorker(req, gen_imp, gen_diff)
        self.worker.progress.connect(self.lbl_status.setText)
        self.worker.result_ready.connect(self.on_computation_finished)
        self.worker.error.connect(self.on_computation_error)
        self.worker.start()

    def on_computation_finished(
        self,
        design: CompletePumpDesign,
        imp_stl: str,
        imp_cutaway_stl: str,
        front_shroud_stl: str,
        diff_stl: str,
    ):
        changed_while_running = self.inputs_changed_during_computation
        self.inputs_changed_during_computation = False
        self.current_design = design
        self.cached_impeller_stl = imp_stl
        self.cached_impeller_cutaway_stl = imp_cutaway_stl
        self.cached_front_shroud_stl = front_shroud_stl
        self.cached_diffuser_stl = diff_stl

        self.progress_bar.setVisible(False)
        self.btn_compute.setEnabled(True)
        warning_count = sum(
            check.status == "warning" for check in design.engineering_record.checks
        )
        multistage = bool(design.architecture and design.architecture.is_multistage)
        self.design_is_stale = changed_while_running
        self.btn_export.setEnabled(not multistage and not self.design_is_stale)
        can_run_cfd = not self.design_is_stale and self._cfd_selection_is_supported()
        self.btn_run_cfd.setEnabled(can_run_cfd)
        if self.cfd_output_uses_default:
            self.edit_cfd_output.setText(
                os.path.join(SCRIPT_DIR, "output", design.design_id)
            )
        self.lbl_cfd_status.setText(
            "Ready to export, mesh, solve, and evaluate this design."
            if can_run_cfd
            else (
                "Inputs changed during generation; regenerate before CFD evaluation."
                if self.design_is_stale
                else "Automated CFD requires the complete single-stage volute assembly."
            )
        )
        self.chk_cutaway_shroud.setEnabled(
            design.impeller.configuration == "Closed" and bool(imp_cutaway_stl)
        )
        self.btn_edit_meridional.setEnabled(
            self.spin_stage_count.value() == 1
            and bool(imp_stl)
            and self.chk_gen_impeller.isChecked()
        )
        result_scope = (
            "Stage plan sized; reference-stage radial impeller preview generated"
            if multistage
            else "Supported 3D geometry synthesized"
        )
        if self.design_is_stale:
            self._set_view_state("stale")
            self.lbl_status.setText(
                "Inputs changed during generation. The generated result remains visible "
                "but is stale; click Compute again to use the latest values."
            )
        else:
            self._set_view_state("current")
            self.lbl_status.setText(
                f"{result_scope}; {warning_count} engineering advisories recorded."
            )

        # Update Dashboard
        self.update_dashboard(design)

        # Update 3D Viewport
        self.update_viewport_display()

        open_editor = (
            self.open_meridional_after_compute
            and not self.design_is_stale
            and not multistage
            and bool(imp_stl)
        )
        self.open_meridional_after_compute = False
        if open_editor:
            # Let the computation signal finish restoring the main-window state
            # before entering the editor's modal event loop.
            QTimer.singleShot(0, self.open_meridional_editor)

    def open_meridional_editor(self):
        """Edit the primary flow path and regenerate all dependent CAD."""
        if self.worker is not None and self.worker.isRunning():
            self.lbl_status.setText(
                "Geometry is already being generated. Open the meridional editor "
                "after the current calculation completes."
            )
            return
        if self.spin_stage_count.value() > 1:
            QMessageBox.warning(
                self,
                "Meridional profile",
                "The current editor applies to one radial stage at a time. "
                "Stage selection will be added with multistage CAD.",
            )
            return
        if self.current_design is None or self.design_is_stale:
            self.open_meridional_after_compute = True
            self.lbl_status.setText(
                "Regenerating the current inputs before opening the meridional editor..."
            )
            self.start_computation()
            return
        if self.current_design.architecture and self.current_design.architecture.is_multistage:
            QMessageBox.warning(
                self,
                "Meridional profile",
                "The current editor applies to one radial stage at a time. "
                "Stage selection will be added with multistage CAD.",
            )
            return

        impeller = self.current_design.impeller
        automatic = create_meridional_design(
            suction_diameter_ds=impeller.suction_diameter_ds,
            hub_diameter_dh=impeller.hub_diameter_dh,
            outlet_diameter_d2=impeller.outlet_diameter_d2,
            inlet_width_b1=impeller.inlet_width_b1,
            outlet_width_b2=impeller.outlet_width_b2,
            specific_speed_nq=self.current_design.performance.specific_speed_nq,
        )
        editor = MeridionalEditorDialog(
            self.current_design.meridional,
            automatic,
            self,
        )
        if editor.exec():
            self.meridional_override = editor.result_override()
            self.lbl_status.setText(
                "Applying meridional profile and regenerating connected 3D CAD..."
            )
            self.start_computation()

    def update_dashboard(self, d: CompletePumpDesign):
        architecture = d.architecture
        if architecture:
            self.lbl_arch_config.setText(
                f"<b>{architecture.machine_configuration}</b> "
                f"({architecture.stage_count} stage{'s' if architecture.stage_count != 1 else ''})"
            )
            self.lbl_stage_heads.setText(
                ", ".join(f"S{stage.index}: {stage.head_m:.2f} m" for stage in d.stages)
            )
            self.lbl_stage_nq.setText(
                ", ".join(
                    f"S{stage.index}: {stage.performance.specific_speed_nq:.1f}"
                    for stage in d.stages
                )
            )
            self.lbl_flow_topology.setText(
                ", ".join(
                    f"S{stage.index}: {stage.resolved_flow_type} "
                    f"(psi={stage.work_coefficient_psi:.3f})"
                    for stage in d.stages
                )
            )
            self.lbl_component_path.setText(
                " -> ".join(architecture.component_sequence)
            )
            self.lbl_cad_scope.setText(architecture.cad_scope)

        # Performance
        self.lbl_nq.setText(f"<b>{d.performance.specific_speed_nq:.1f}</b> ({d.performance.specific_speed_ns:.0f} US)")
        self.lbl_eta_total.setText(f"<b>{d.performance.total_efficiency:.1f} %</b>")
        self.lbl_eta_hyd.setText(f"{d.performance.hydraulic_efficiency:.1f} %")
        self.lbl_power_shaft.setText(f"<b>{d.performance.shaft_power_kw:.2f} kW</b>")
        self.lbl_power_hyd.setText(f"{d.performance.hydraulic_power_kw:.2f} kW")
        self.lbl_npsh.setText(f"<b>{d.performance.npsh_required_m:.2f} m</b>")

        # Fluid
        self.lbl_rho.setText(f"{d.fluid.density:.1f} kg/m³")
        cst = d.fluid.kinematic_viscosity * 1e6
        self.lbl_visc.setText(f"{cst:.2f} cSt")
        self.lbl_pv.setText(f"{d.fluid.vapor_pressure/1000.0:.2f} kPa")

        # Impeller
        self.lbl_imp_eye.setText(f"{d.impeller.suction_diameter_ds:.1f} mm")
        self.lbl_imp_config.setText(f"<b>{d.impeller.configuration}</b>")
        self.lbl_imp_hub.setText(f"{d.impeller.hub_diameter_dh:.1f} mm")
        self.lbl_imp_d2.setText(f"<b>{d.impeller.outlet_diameter_d2:.1f} mm</b>")
        self.lbl_imp_b2.setText(f"{d.impeller.outlet_width_b2:.1f} mm")
        self.lbl_imp_z.setText(f"<b>{d.impeller.blade_count_z} blades</b>")
        self.lbl_imp_beta1.setText(f"{d.impeller.blade_inlet_angle_beta1:.1f}°")
        self.lbl_imp_beta2.setText(f"{d.impeller.blade_outlet_angle_beta2:.1f}°")
        beta1_span = d.impeller.blade_inlet_angles_spanwise
        self.lbl_imp_blade_surface.setText(
            f"<b>{len(d.impeller.blade_span_positions)} spans</b>, "
            f"LE stacked; β1 {beta1_span[0]:.1f}–{beta1_span[-1]:.1f}°"
        )
        self.lbl_imp_blade_edges.setText(
            f"<b>{d.impeller.blade_leading_edge_shape}</b>, "
            f"R LE/TE {d.impeller.blade_leading_edge_radius:.2f}/"
            f"{d.impeller.blade_trailing_edge_radius:.2f} mm"
        )
        passage = d.blade_passage
        self.lbl_imp_min_throat.setText(
            f"<b>{passage.minimum_throat_distance_mm:.2f} mm</b> "
            f"@ span {passage.minimum_throat_span_fraction:.2f}, "
            f"x/c {passage.minimum_throat_chord_fraction:.2f}"
        )
        self.lbl_imp_throat_area.setText(
            f"<b>{passage.throat_area_mm2:.1f} mm²</b> "
            f"@ x/c {passage.throat_area_chord_fraction:.2f}"
        )
        self.lbl_imp_passage_area.setText(
            f"{passage.passage_area_min_mm2:.1f}–"
            f"{passage.passage_area_max_mm2:.1f} mm²; "
            f"max step {passage.maximum_adjacent_area_change_percent:.1f}%"
        )
        self.lbl_imp_u2.setText(f"{d.impeller.u2:.1f} m/s")
        self.lbl_imp_slip.setText(f"{d.impeller.slip_factor_sigma:.3f}")
        self.lbl_imp_intake_coefficient.setText(
            f"{d.impeller.intake_coefficient_epsilon:.3f}"
        )
        self.lbl_imp_width_ratio.setText(
            f"{d.impeller.outlet_width_ratio_b2_d2:.3f}"
        )
        self.lbl_imp_meridional_ratio.setText(
            f"{d.impeller.meridional_deceleration_ratio:.3f}"
        )
        self.lbl_shroud_alignment.setText(
            f"<b>Parallel radial outlet</b> (b2={d.impeller.outlet_width_b2:.1f} mm)"
        )
        self.lbl_shroud_thickness.setText(
            f"{d.impeller.back_shroud_thickness:.2f} / "
            f"{d.impeller.front_shroud_thickness:.2f} mm"
        )
        self.lbl_eye_collar.setText(
            f"{d.impeller.eye_collar_axial_length:.1f} mm axial"
            if d.impeller.eye_collar_enabled
            else "Disabled"
        )

        # Diffuser / collector
        show_stationary = (
            architecture is not None and architecture.has_supported_stationary_cad
        )
        show_vaned_diffuser = (
            show_stationary
            and architecture.single_stage_collector == COLLECTOR_VANED_DIFFUSER
        )
        if show_vaned_diffuser:
            self.lbl_diff_d3.setText(f"<b>{d.diffuser.inlet_diameter_d3:.1f} mm</b>")
            self.lbl_diff_d4.setText(f"{d.diffuser.outlet_diameter_d4:.1f} mm")
            self.lbl_diff_b3.setText(f"{d.diffuser.inlet_width_b3:.1f} mm")
            self.lbl_diff_zd.setText(f"<b>{d.diffuser.vane_count_zd} vanes</b>")
            self.lbl_diff_beta3.setText(f"{d.diffuser.vane_inlet_angle_beta3:.1f}°")
            self.lbl_diff_beta4.setText(f"{d.diffuser.vane_outlet_angle_beta4:.1f}°")
        elif show_stationary:
            self.lbl_diff_d3.setText(f"<b>{d.volute.inlet_diameter_d4:.1f} mm</b>")
            self.lbl_diff_d4.setText(
                f"Aout/Ain {d.volute.discharge_area_ratio:.3f}"
            )
            self.lbl_diff_b3.setText(f"{d.volute.inlet_width_b4:.1f} mm")
            self.lbl_diff_zd.setText("Single volute")
            self.lbl_diff_beta3.setText(
                f"{d.volute.inlet_flow_angle_alpha4:.1f}° inlet"
            )
            self.lbl_diff_beta4.setText(
                f"{d.volute.discharge_cone_angle_deg:.1f}° cone"
            )
        else:
            self.lbl_diff_d3.setText("Not generated")
            self.lbl_diff_d4.setText("-")
            self.lbl_diff_b3.setText("-")
            self.lbl_diff_zd.setText("-")
            self.lbl_diff_beta3.setText("-")
            self.lbl_diff_beta4.setText("-")

        # Traceability and checks
        checks = d.engineering_record.checks
        pass_count = sum(check.status == "pass" for check in checks)
        warning_checks = [check for check in checks if check.status == "warning"]
        fail_count = sum(check.status == "fail" for check in checks)
        self.lbl_design_id.setText(f"<b>{d.design_id}</b>")
        self.lbl_meridional.setText(
            f"{d.meridional.curve_type}, L={d.meridional.axial_length:.1f} mm"
        )
        self.lbl_check_summary.setText(
            f"{pass_count} pass / {len(warning_checks)} warning / {fail_count} fail"
        )
        self.lbl_check_warnings.setText(
            "; ".join(check.message for check in warning_checks) or "None"
        )

    def update_viewport_display(self):
        self.plotter.clear()
        # Renderer.clear() removes PyVista's complete five-light kit along
        # with the actors.  Restore it before adding the CAD surfaces so the
        # live Qt viewport matches the validated standalone render instead of
        # appearing as an unlit, flat-colour silhouette.
        self.plotter.enable_lightkit()

        show_imp = self.rb_view_both.isChecked() or self.rb_view_imp.isChecked()
        show_diff = self.rb_view_both.isChecked() or self.rb_view_diff.isChecked()
        show_mesh_triangles = self.chk_show_mesh_edges.isChecked()
        show_feature_edges = self.chk_feature_edges.isChecked()

        # The neutral engineering CAD presentation is the only 3D mode.
        # Triangles remain available solely as an explicit inspection overlay.
        self.plotter.set_background('#cfd4da', top='#f5f6f7')
        impeller_color = '#c7aa76'
        diffuser_color = '#b8c5d1'
        impeller_edge_color = '#403c36'
        diffuser_edge_color = '#46515b'
        mesh_edge_color = '#7a746b'

        if show_imp and self.cached_impeller_stl and os.path.exists(self.cached_impeller_stl):
            separated_closed_preview = (
                self.current_design is not None
                and self.current_design.impeller.configuration == "Closed"
                and self.cached_impeller_cutaway_stl
                and os.path.exists(self.cached_impeller_cutaway_stl)
                and self.cached_front_shroud_stl
                and os.path.exists(self.cached_front_shroud_stl)
            )
            impeller_path = (
                self.cached_impeller_cutaway_stl
                if separated_closed_preview
                else self.cached_impeller_stl
            )
            mesh_imp = pv.read(impeller_path).clean()
            self._add_part_surface(
                mesh_imp,
                impeller_color,
                mesh_edge_color,
                show_mesh_triangles,
            )

            hide_shroud = self.chk_cutaway_shroud.isChecked()
            if separated_closed_preview and not hide_shroud:
                mesh_shroud = pv.read(self.cached_front_shroud_stl).clean()
                self._add_part_surface(
                    mesh_shroud,
                    impeller_color,
                    mesh_edge_color,
                    show_mesh_triangles,
                )
                if show_feature_edges:
                    # Keep the hub, bore, and blade leading edges that are
                    # genuinely visible through the suction eye.  Restricting
                    # them to the aperture avoids projecting hidden blade
                    # curves through the opaque front shroud.
                    eye_radius = 0.5 * self.current_design.impeller.suction_diameter_ds
                    self._add_cad_edges(
                        mesh_imp,
                        impeller_edge_color,
                        max_radius=eye_radius,
                        add_silhouette=False,
                    )
                    self._add_cad_edges(mesh_shroud, impeller_edge_color)
            elif show_feature_edges:
                self._add_cad_edges(mesh_imp, impeller_edge_color)

        if show_diff and self.cached_diffuser_stl and os.path.exists(self.cached_diffuser_stl):
            mesh_diff = pv.read(self.cached_diffuser_stl)
            assembly_transparency = (
                show_imp and self.chk_transparent_diffuser.isChecked()
            )
            opacity = 0.28 if assembly_transparency else 1.0
            self._add_part_surface(
                mesh_diff.clean(),
                diffuser_color,
                mesh_edge_color,
                show_mesh_triangles,
                opacity=opacity,
            )
            if show_feature_edges:
                self._add_cad_edges(
                    mesh_diff,
                    diffuser_edge_color,
                    opacity=0.45 if assembly_transparency else 1.0,
                )

        self._set_isometric_camera()

    def _add_part_surface(
        self,
        mesh,
        color: str,
        mesh_edge_color: str,
        show_mesh_triangles: bool,
        opacity: float = 1.0,
    ):
        self.plotter.add_mesh(
            mesh,
            color=color,
            show_edges=show_mesh_triangles,
            edge_color=mesh_edge_color,
            smooth_shading=True,
            split_sharp_edges=True,
            feature_angle=42.0,
            ambient=0.15,
            diffuse=0.78,
            specular=0.42,
            specular_power=38.0,
            opacity=opacity,
        )

    def _add_cad_edges(
        self,
        mesh,
        color: str,
        opacity: float = 1.0,
        max_radius: float | None = None,
        add_silhouette: bool = True,
    ):
        """Overlay Creo-like silhouettes and meaningful geometric creases."""
        feature_edges = mesh.extract_feature_edges(
            feature_angle=42.0,
            boundary_edges=False,
            non_manifold_edges=False,
            feature_edges=True,
            manifold_edges=False,
            clear_data=True,
        )
        if max_radius is not None and feature_edges.n_cells:
            centers = feature_edges.cell_centers().points
            visible_cells = [
                index
                for index, center in enumerate(centers)
                if (center[0] * center[0] + center[1] * center[1]) ** 0.5
                <= max_radius
            ]
            feature_edges = (
                feature_edges.extract_cells(visible_cells).extract_surface()
                if visible_cells
                else pv.PolyData()
            )
        if feature_edges.n_cells:
            self.plotter.add_mesh(
                feature_edges,
                color=color,
                line_width=1.0,
                opacity=opacity,
                lighting=False,
                render_lines_as_tubes=False,
            )
        if add_silhouette:
            self.plotter.add_silhouette(
                mesh,
                color=color,
                line_width=1.8,
                opacity=opacity,
                decimate=0.0,
            )

    def reset_camera(self):
        self._set_isometric_camera()

    def _set_isometric_camera(self):
        """Use a repeatable perspective that makes axial depth visible."""
        self.plotter.disable_parallel_projection()
        self.plotter.view_isometric()
        self.plotter.reset_camera()
        self.plotter.camera.zoom(0.92)

    def on_computation_error(self, err_msg: str):
        self.open_meridional_after_compute = False
        self.progress_bar.setVisible(False)
        self.btn_compute.setEnabled(True)
        self.btn_export.setEnabled(
            bool(
                self.current_design
                and not self.design_is_stale
                and not (
                    self.current_design.architecture
                    and self.current_design.architecture.is_multistage
                )
            )
        )
        self.btn_run_cfd.setEnabled(
            bool(not self.design_is_stale and self._cfd_selection_is_supported())
        )
        self.btn_edit_meridional.setEnabled(
            bool(
                self.current_design
                and self.spin_stage_count.value() == 1
                and self.cached_impeller_stl
                and self.chk_gen_impeller.isChecked()
            )
        )
        self._set_view_state("stale" if self.current_design else "empty")
        self.lbl_status.setText(f"❌ Error: {err_msg}")
        QMessageBox.critical(self, "Turbomachinery Sizing Error", f"Failed to compute geometry:\n{err_msg}")

    def export_openfoam_files(self):
        if self.current_design is None:
            QMessageBox.warning(self, "No Design", "Please compute geometry first!")
            return
        if self.design_is_stale:
            QMessageBox.warning(
                self,
                "Stale design",
                "The displayed geometry does not match the current inputs. "
                "Regenerate it before export.",
            )
            return

        target_dir = QFileDialog.getExistingDirectory(self, "Select Export Directory", SCRIPT_DIR)
        if not target_dir:
            return

        self.lbl_status.setText(
            "⏳ Exporting CAD, fluid domains, and steady-MRF OpenFOAM case..."
        )
        self.progress_bar.setVisible(True)
        self.btn_compute.setEnabled(False)
        self.btn_export.setEnabled(False)
        self.btn_run_cfd.setEnabled(False)
        self.export_worker = ExportWorker(
            self.current_design,
            target_dir,
            self.chk_gen_impeller.isChecked(),
            self.chk_gen_diffuser.isChecked(),
        )
        self.export_worker.completed.connect(self.on_export_finished)
        self.export_worker.error.connect(self.on_export_error)
        self.export_worker.start()

    def on_export_finished(self, exported_files):
        self.progress_bar.setVisible(False)
        self.btn_compute.setEnabled(True)
        self.btn_export.setEnabled(
            bool(
                self.current_design
                and not self.design_is_stale
                and not (
                    self.current_design.architecture
                    and self.current_design.architecture.is_multistage
                )
            )
        )
        self.btn_run_cfd.setEnabled(
            bool(not self.design_is_stale and self._cfd_selection_is_supported())
        )
        msg = "Exported CAD and CFD package:\n\n"
        for path in exported_files.values():
            msg += f"• {os.path.basename(path)}\n"
        msg += (
            "\nSTLs use metres; STEP files use millimetres. "
            "The steady-MRF case must pass checkMesh before it is solved."
        )
        self.lbl_status.setText("🎉 CAD and OpenFOAM case export complete!")
        QMessageBox.information(self, "CAD + CFD Export Complete", msg)

    def on_export_error(self, message):
        self.progress_bar.setVisible(False)
        self.btn_compute.setEnabled(True)
        self.btn_export.setEnabled(
            bool(
                self.current_design
                and not self.design_is_stale
                and not (
                    self.current_design.architecture
                    and self.current_design.architecture.is_multistage
                )
            )
        )
        self.btn_run_cfd.setEnabled(
            bool(not self.design_is_stale and self._cfd_selection_is_supported())
        )
        self.lbl_status.setText(f"Export error: {message}")
        QMessageBox.critical(self, "Export Error", message)

    def apply_dark_theme(self):
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #0f172a;
                color: #f8fafc;
                font-family: 'Segoe UI', Arial, sans-serif;
            }
            QGroupBox {
                border: 1px solid #334155;
                border-radius: 6px;
                margin-top: 12px;
                font-weight: bold;
                color: #38bdf8;
                padding-top: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
            QSpinBox, QDoubleSpinBox, QComboBox, QLineEdit, QPlainTextEdit {
                background-color: #1e293b;
                border: 1px solid #475569;
                border-radius: 4px;
                padding: 5px;
                color: #ffffff;
                font-size: 13px;
            }
            QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus,
            QLineEdit:focus, QPlainTextEdit:focus {
                border: 1px solid #38bdf8;
            }
            QRadioButton, QCheckBox {
                color: #e2e8f0;
                font-size: 12px;
                spacing: 6px;
            }
            QLabel {
                font-size: 12px;
            }
        """)


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = PumpStudioApp()
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
