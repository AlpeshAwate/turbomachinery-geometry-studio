import math
import unittest

from core.blade_geometry import create_blade_surface_grid, evaluate_blade_passage
from core.pump_design import PumpRequirements, size_pump


class BladeGeometryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.design = size_pump(
            PumpRequirements(45.0, 120.0, 2950.0, "Water", 25.0)
        )
        cls.grid = create_blade_surface_grid(
            cls.design.meridional,
            cls.design.impeller.blade_inlet_angles_spanwise,
            cls.design.impeller.blade_outlet_angles_spanwise,
            span_positions=cls.design.impeller.blade_span_positions,
            stacking_fraction=cls.design.impeller.blade_stacking_fraction,
            chord_sections=41,
        )
        cls.passage = evaluate_blade_passage(
            cls.grid,
            maximum_thickness_mm=cls.design.impeller.blade_thickness,
            blade_count=cls.design.impeller.blade_count_z,
        )

    def test_leading_edge_stacking_and_spanwise_wrap_are_explicit(self):
        leading_angles = [
            math.atan2(line[0][1], line[0][0])
            for line in self.grid.mean_points_xyz
        ]
        self.assertLess(max(leading_angles) - min(leading_angles), 1.0e-9)
        self.assertGreater(
            max(self.grid.wrap_angles_deg) - min(self.grid.wrap_angles_deg),
            10.0,
        )

    def test_thickness_direction_is_unit_and_normal_to_mean_line(self):
        chord_index = len(self.grid.chord_fractions) // 2
        for span_index, line in enumerate(self.grid.mean_points_xyz):
            tangent = tuple(
                end - start
                for start, end in zip(
                    line[chord_index - 1], line[chord_index + 1]
                )
            )
            direction = self.grid.thickness_directions_xyz[span_index][chord_index]
            tangent_length = math.sqrt(sum(value * value for value in tangent))
            direction_length = math.sqrt(sum(value * value for value in direction))
            cosine = sum(
                a * b for a, b in zip(tangent, direction)
            ) / (tangent_length * direction_length)
            self.assertAlmostEqual(direction_length, 1.0, places=6)
            self.assertAlmostEqual(cosine, 0.0, delta=0.03)

    def test_neighbor_throat_and_passage_area_are_positive(self):
        passage = self.passage
        self.assertTrue(passage.sampled_intersection_free)
        self.assertGreater(passage.minimum_throat_distance_mm, 0.05)
        self.assertGreater(passage.throat_area_mm2, 0.0)
        self.assertEqual(len(passage.leading_edge_throat_distances_mm), 5)
        self.assertEqual(len(passage.trailing_edge_throat_distances_mm), 5)
        self.assertEqual(len(passage.passage_areas_mm2), 41)
        self.assertLess(passage.maximum_adjacent_area_change_percent, 10.0)

    def test_sampled_gate_detects_an_overcrowded_blade_pattern(self):
        overcrowded = evaluate_blade_passage(
            self.grid,
            maximum_thickness_mm=self.design.impeller.blade_thickness,
            blade_count=40,
        )
        self.assertFalse(overcrowded.sampled_intersection_free)
        self.assertLess(overcrowded.minimum_throat_distance_mm, 0.05)

    def test_surface_grid_accepts_sizing_minimum_blade_angle(self):
        grid = create_blade_surface_grid(
            self.design.meridional,
            (8.0,) * len(self.design.impeller.blade_span_positions),
            self.design.impeller.blade_outlet_angles_spanwise,
            span_positions=self.design.impeller.blade_span_positions,
            stacking_fraction=self.design.impeller.blade_stacking_fraction,
            chord_sections=9,
        )

        self.assertEqual(len(grid.mean_points_xyz), 5)
        self.assertTrue(
            all(
                math.isfinite(coordinate)
                for span in grid.mean_points_xyz
                for point in span
                for coordinate in point
            )
        )

    def test_surface_grid_rejects_angle_below_sizing_contract(self):
        with self.assertRaisesRegex(ValueError, "between 8 and 60"):
            create_blade_surface_grid(
                self.design.meridional,
                (7.99,) * len(self.design.impeller.blade_span_positions),
                self.design.impeller.blade_outlet_angles_spanwise,
                span_positions=self.design.impeller.blade_span_positions,
                stacking_fraction=self.design.impeller.blade_stacking_fraction,
                chord_sections=9,
            )


if __name__ == "__main__":
    unittest.main()
