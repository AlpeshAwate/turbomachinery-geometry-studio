from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import gui
from gui import (
    CfdEvaluationWorker,
    PumpStudioApp,
    cfd_run_is_supported,
    format_cfd_result_summary,
)


class GuiStateTests(unittest.TestCase):
    def test_gui_source_contains_no_mojibake_degree_symbols(self):
        source = Path(gui.__file__).read_text(encoding="utf-8")

        self.assertNotIn("Â°", source)

    def test_input_change_keeps_previous_geometry_as_stale_result(self):
        window = PumpStudioApp.__new__(PumpStudioApp)
        previous_design = object()
        window.worker = None
        window.current_design = previous_design
        window.meridional_override = object()
        window.design_is_stale = False
        window.cached_impeller_stl = "impeller.stl"
        window.cached_impeller_cutaway_stl = "cutaway.stl"
        window.cached_front_shroud_stl = "shroud.stl"
        window.cached_diffuser_stl = "diffuser.stl"
        window.btn_export = MagicMock()
        window.btn_edit_meridional = MagicMock()
        window.spin_stage_count = MagicMock()
        window.spin_stage_count.value.return_value = 1
        window.chk_gen_impeller = MagicMock()
        window.chk_gen_impeller.isChecked.return_value = True
        window.lbl_view_state = MagicMock()
        window.lbl_status = MagicMock()
        window.plotter = MagicMock()

        window._mark_design_stale()

        self.assertIs(window.current_design, previous_design)
        self.assertEqual(window.cached_impeller_stl, "impeller.stl")
        self.assertEqual(window.cached_impeller_cutaway_stl, "cutaway.stl")
        self.assertEqual(window.cached_front_shroud_stl, "shroud.stl")
        self.assertEqual(window.cached_diffuser_stl, "diffuser.stl")
        self.assertTrue(window.design_is_stale)
        self.assertIsNone(window.meridional_override)
        window.plotter.clear.assert_not_called()
        window.btn_export.setEnabled.assert_called_once_with(False)
        window.btn_edit_meridional.setEnabled.assert_called_once_with(True)
        window.lbl_view_state.setText.assert_called_once_with(
            "STALE - PREVIOUS RESULT"
        )

    def test_stale_profile_edit_regenerates_then_queues_editor(self):
        window = PumpStudioApp.__new__(PumpStudioApp)
        window.worker = None
        window.current_design = object()
        window.design_is_stale = True
        window.open_meridional_after_compute = False
        window.spin_stage_count = MagicMock()
        window.spin_stage_count.value.return_value = 1
        window.lbl_status = MagicMock()
        window.start_computation = MagicMock()

        window.open_meridional_editor()

        self.assertTrue(window.open_meridional_after_compute)
        window.start_computation.assert_called_once_with()
        window.lbl_status.setText.assert_called_once_with(
            "Regenerating the current inputs before opening the meridional editor..."
        )

    def test_cfd_run_requires_complete_assembly_and_both_components(self):
        complete = SimpleNamespace(
            architecture=SimpleNamespace(has_complete_assembly_cad=True)
        )
        partial = SimpleNamespace(
            architecture=SimpleNamespace(has_complete_assembly_cad=False)
        )

        self.assertTrue(cfd_run_is_supported(complete, True, True))
        self.assertFalse(cfd_run_is_supported(complete, False, True))
        self.assertFalse(cfd_run_is_supported(complete, True, False))
        self.assertFalse(cfd_run_is_supported(partial, True, True))
        self.assertFalse(cfd_run_is_supported(None, True, True))

    def test_cfd_summary_reports_metrics_and_failed_gate(self):
        summary = format_cfd_result_summary(
            {
                "status": "failed",
                "design_id": "pump-test",
                "mesh": {"cells": 1200, "regions": 1},
                "performance": {
                    "head_m": 44.25,
                    "total_pressure_rise_pa": 432100.0,
                    "hydraulic_efficiency_percent": 81.75,
                    "shaft_power_kw": 17.1,
                    "rotor_torque_z_n_m": 55.2,
                },
                "gates": [
                    {"name": "mesh_completed", "status": "pass", "message": "ok"},
                    {
                        "name": "flow_closure",
                        "status": "fail",
                        "message": "Flow imbalance is too high.",
                    },
                ],
            }
        )

        self.assertIn("CFD FAILED", summary)
        self.assertIn("Head: 44.250 m", summary)
        self.assertIn("Hydraulic efficiency: 81.75 %", summary)
        self.assertIn("Acceptance gates: 1 passed, 1 failed", summary)
        self.assertIn("FAIL flow_closure", summary)

    @patch("gui.evaluate_openfoam_case")
    @patch("gui.export_turbomachinery_for_openfoam")
    def test_cfd_worker_exports_then_invokes_gated_runner(
        self, export_case, evaluate_case
    ):
        design = SimpleNamespace(design_id="pump-test")
        export_case.return_value = {"openfoam_case": "case-dir"}
        evaluate_case.return_value = {"status": "passed"}
        worker = CfdEvaluationWorker(
            design,
            "output-dir",
            "wsl",
            "Ubuntu",
            "/opt/openfoam/etc/bashrc",
            123.0,
        )

        worker.run()

        export_case.assert_called_once_with(
            design,
            "output-dir",
            export_impeller=True,
            export_diffuser=True,
        )
        _, kwargs = evaluate_case.call_args
        self.assertEqual(kwargs["backend"], "wsl")
        self.assertEqual(kwargs["wsl_distribution"], "Ubuntu")
        self.assertEqual(kwargs["openfoam_bashrc"], "/opt/openfoam/etc/bashrc")
        self.assertEqual(kwargs["timeout_s"], 123.0)
        self.assertTrue(callable(kwargs["executor"]))


if __name__ == "__main__":
    unittest.main()
