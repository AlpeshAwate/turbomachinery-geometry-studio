"""Published centrifugal-pump regression benchmarks.

Sources:
- Jia et al. (2022), Frontiers in Energy Research 10:866037,
  https://doi.org/10.3389/fenrg.2022.866037 (Table 1).
- Aliuly et al. (2024), Applied Sciences 14:10161,
  https://doi.org/10.3390/app142210161 (Tables 1-3).

The tolerances reflect preliminary one-dimensional sizing. They are strict
enough to detect the former collapsed-eye/collapsed-b2 behavior without
claiming CFD or manufacturer-detail accuracy.
"""

import unittest

from core.blade_builder import build_impeller_solid
from core.pump_design import PumpRequirements, size_pump


def relative_error(calculated: float, reference: float) -> float:
    return abs(calculated - reference) / reference


class ResearchBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.jia = size_pump(
            PumpRequirements(39.0, 10.0, 3000.0, "Water", 20.0)
        )
        cls.aliuly = size_pump(
            PumpRequirements(32.0, 300.0, 1500.0, "Water", 20.0)
        )

    def test_jia_low_specific_speed_impeller_is_not_falsely_rejected(self):
        impeller = self.jia.impeller
        self.assertAlmostEqual(self.jia.performance.specific_speed_nq, 10.13, places=2)
        self.assertLess(relative_error(impeller.suction_diameter_ds, 50.0), 0.08)
        self.assertLess(relative_error(impeller.outlet_diameter_d2, 160.0), 0.08)
        self.assertLess(relative_error(impeller.outlet_width_b2, 10.0), 0.20)
        self.assertLess(abs(impeller.blade_inlet_angle_beta1 - 25.0), 3.0)
        self.assertLess(abs(impeller.blade_outlet_angle_beta2 - 25.0), 2.0)
        self.assertGreaterEqual(impeller.blade_count_z, 3)
        self.assertLessEqual(impeller.blade_count_z, 7)
        self.assertGreaterEqual(impeller.outlet_width_ratio_b2_d2, 0.04)

    def test_aliuly_main_dimensions_and_angles_stay_within_preliminary_accuracy(self):
        impeller = self.aliuly.impeller
        self.assertLess(relative_error(impeller.suction_diameter_ds, 190.0), 0.08)
        self.assertLess(relative_error(impeller.hub_diameter_dh, 60.0), 0.08)
        self.assertLess(relative_error(impeller.outlet_diameter_d2, 324.0), 0.10)
        self.assertLess(relative_error(impeller.outlet_width_b2, 40.0), 0.12)
        self.assertLess(abs(impeller.blade_inlet_angle_beta1 - 20.0), 3.0)
        self.assertLess(abs(impeller.blade_inlet_angles_spanwise[0] - 32.0), 3.0)
        self.assertLess(abs(impeller.blade_inlet_angles_spanwise[-1] - 14.0), 3.0)
        self.assertLess(abs(impeller.blade_outlet_angle_beta2 - 34.0), 2.0)

    def test_aliuly_bep_performance_remains_conservatively_bounded(self):
        bep = size_pump(
            PumpRequirements(30.3, 340.1, 1500.0, "Water", 20.0)
        )
        self.assertLess(abs(bep.performance.total_efficiency - 75.3), 7.0)
        self.assertLess(relative_error(bep.performance.shaft_power_kw, 37.32), 0.10)

    def test_benchmark_closed_impellers_are_valid_connected_solids(self):
        for design in (self.jia, self.aliuly):
            shape = build_impeller_solid(
                design.impeller,
                design.meridional,
                include_front_shroud=True,
            ).val()
            self.assertTrue(shape.isValid())
            self.assertEqual(len(shape.Solids()), 1)
            self.assertGreater(shape.Volume(), 0.0)


if __name__ == "__main__":
    unittest.main()
