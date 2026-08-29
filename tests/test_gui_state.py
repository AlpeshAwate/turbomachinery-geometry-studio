from pathlib import Path
import unittest
from unittest.mock import MagicMock

import gui
from gui import PumpStudioApp


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


if __name__ == "__main__":
    unittest.main()
