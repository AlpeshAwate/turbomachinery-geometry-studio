import math
import unittest

from core.fluids import get_fluid_properties
from core.meridional import (
    MeridionalOverride,
    axial_at_radius,
    bezier_tangent,
    create_edited_meridional_design,
    sample_bezier,
)
from core.pump_design import (
    COLLECTOR_VANED_DIFFUSER,
    G,
    DesignValidationError,
    FLOW_TYPE_MIXED,
    PumpRequirements,
    size_pump,
)


class PumpDesignTests(unittest.TestCase):
    def test_default_design_closes_requested_head(self):
        design = size_pump(PumpRequirements(45.0, 120.0, 2950.0, "Water", 25.0))
        impeller = design.impeller
        predicted_head = (
            design.performance.hydraulic_efficiency
            / 100.0
            * impeller.u2
            * impeller.c2u
            / G
        )
        self.assertAlmostEqual(predicted_head, 45.0, delta=0.15)

    def test_blade_surface_uses_five_traceable_meridional_spans(self):
        design = size_pump(PumpRequirements(45.0, 120.0, 2950.0, "Water", 25.0))
        impeller = design.impeller
        self.assertEqual(impeller.blade_span_positions, (0.0, 0.25, 0.5, 0.75, 1.0))
        self.assertEqual(len(impeller.blade_inlet_angles_spanwise), 5)
        self.assertGreater(
            impeller.blade_inlet_angles_spanwise[0],
            impeller.blade_inlet_angles_spanwise[-1],
        )
        self.assertTrue(
            any(
                check.key.endswith("blade_span_count") and check.status == "pass"
                for check in design.engineering_record.checks
            )
        )

    def test_blade_passage_checks_are_traceable_hard_gates(self):
        design = size_pump(PumpRequirements(45.0, 120.0, 2950.0, "Water", 25.0))
        passage = design.blade_passage
        self.assertTrue(passage.sampled_intersection_free)
        self.assertGreater(passage.minimum_throat_distance_mm, 0.0)
        self.assertGreater(passage.throat_area_mm2, 0.0)
        checks = {
            check.key.split(".")[-1]: check
            for check in design.engineering_record.checks
        }
        self.assertEqual(checks["neighbor_blade_clearance"].status, "pass")
        self.assertEqual(checks["blade_throat_area"].status, "pass")
        self.assertIn(
            "blade_passage",
            design.engineering_record.parameters["stages"][0],
        )

    def test_hydraulic_blade_edges_are_rounded_and_checked(self):
        design = size_pump(PumpRequirements(45.0, 120.0, 2950.0, "Water", 25.0))
        impeller = design.impeller
        self.assertEqual(impeller.blade_leading_edge_shape, "Ellipse")
        self.assertEqual(impeller.blade_trailing_edge_shape, "Ellipse")
        self.assertEqual(impeller.blade_edge_axis_ratio, 1.0)
        self.assertGreater(impeller.blade_leading_edge_radius, 0.0)
        checks = {
            check.key.split(".")[-1]: check
            for check in design.engineering_record.checks
        }
        self.assertEqual(checks["rounded_blade_edges"].status, "pass")
        self.assertEqual(checks["blade_edge_extent"].status, "pass")

    def test_unsupported_specific_speed_is_rejected(self):
        with self.assertRaises(DesignValidationError):
            size_pump(PumpRequirements(5.0, 1.0, 500.0, "Water", 25.0))

    def test_impeller_configuration_is_explicit_and_validated(self):
        closed = size_pump(
            PumpRequirements(45.0, 120.0, 2950.0, "Water", 25.0, "Closed")
        )
        self.assertEqual(closed.impeller.configuration, "Closed")
        with self.assertRaises(DesignValidationError):
            size_pump(
                PumpRequirements(45.0, 120.0, 2950.0, "Water", 25.0, "Semi-open")
            )

    def test_fluid_temperature_is_not_silently_clamped(self):
        with self.assertRaises(ValueError):
            get_fluid_properties("Ethanol", 76.0)
        state = get_fluid_properties("Ethanol", 75.0)
        self.assertEqual(state.temperature_c, 75.0)

    def test_viscous_fluid_receives_efficiency_derating(self):
        water = size_pump(PumpRequirements(45.0, 120.0, 2950.0, "Water", 25.0))
        crude = size_pump(
            PumpRequirements(45.0, 120.0, 2950.0, "Crude Oil (Medium)", 25.0)
        )
        self.assertLess(
            crude.performance.hydraulic_efficiency,
            water.performance.hydraulic_efficiency,
        )

    def test_engineering_record_is_deterministic_and_has_no_failed_gates(self):
        requirements = PumpRequirements(45.0, 120.0, 2950.0, "Water", 25.0)
        first = size_pump(requirements)
        second = size_pump(requirements)
        integer_inputs = size_pump(
            PumpRequirements(45, 120, 2950, "Water", 25)
        )
        self.assertEqual(first.design_id, second.design_id)
        self.assertEqual(first.design_id, integer_inputs.design_id)
        self.assertTrue(first.design_id.startswith("pump-"))
        self.assertFalse(
            [check for check in first.engineering_record.checks if check.status == "fail"]
        )
        self.assertIn("CFturbo_en.pdf", first.engineering_record.source_documents)

    def test_meridional_curves_are_monotonic_and_do_not_cross(self):
        design = size_pump(
            PumpRequirements(45.0, 120.0, 2950.0, "Water", 25.0)
        )
        meridional = design.meridional
        for control_points in (
            meridional.hub_control_points_rz,
            meridional.shroud_control_points_rz,
        ):
            samples = sample_bezier(control_points, 80)
            self.assertTrue(
                all(b[0] > a[0] for a, b in zip(samples, samples[1:]))
            )
        eye_radius = design.impeller.suction_diameter_ds / 2.0
        outlet_radius = design.impeller.outlet_diameter_d2 / 2.0
        for index in range(21):
            radius = eye_radius + index / 20.0 * (outlet_radius - eye_radius)
            hub_z = axial_at_radius(meridional.hub_control_points_rz, radius)
            shroud_z = axial_at_radius(meridional.shroud_control_points_rz, radius)
            self.assertGreater(shroud_z, hub_z)
        self.assertGreater(meridional.minimum_channel_height_mm, 0.0)

    def test_axial_at_radius_tolerates_endpoint_roundoff_only(self):
        control_points = ((10.0, 7.0), (14.0, 3.0), (20.0, 1.0))

        self.assertEqual(
            axial_at_radius(control_points, math.nextafter(10.0, -math.inf)),
            7.0,
        )
        self.assertEqual(
            axial_at_radius(control_points, math.nextafter(20.0, math.inf)),
            1.0,
        )
        with self.assertRaisesRegex(ValueError, "outside curve range"):
            axial_at_radius(control_points, 10.0 - 1.0e-6)
        with self.assertRaisesRegex(ValueError, "outside curve range"):
            axial_at_radius(control_points, 20.0 + 1.0e-6)
        with self.assertRaisesRegex(ValueError, "outside curve range"):
            axial_at_radius(control_points, math.nan)

    def test_meridional_bezier_endpoint_tangents_are_preserved(self):
        design = size_pump(
            PumpRequirements(45.0, 120.0, 2950.0, "Water", 25.0)
        )
        shroud = design.meridional.shroud_control_points_rz
        start_tangent = bezier_tangent(shroud, 0.0)
        end_tangent = bezier_tangent(shroud, 1.0)
        self.assertAlmostEqual(start_tangent[0], 0.0, places=9)
        self.assertLess(start_tangent[1], 0.0)
        self.assertGreater(end_tangent[0], 0.0)
        self.assertAlmostEqual(end_tangent[1], 0.0, places=9)

    def test_meridional_override_is_validated_recorded_and_changes_design(self):
        automatic = size_pump(
            PumpRequirements(45.0, 120.0, 2950.0, "Water", 25.0)
        )
        hub = list(automatic.meridional.hub_control_points_rz)
        shroud = list(automatic.meridional.shroud_control_points_rz)
        hub[2] = (hub[2][0] - 1.5, hub[2][1] + 1.0)
        shroud[2] = (shroud[2][0] + 1.0, shroud[2][1] - 0.8)
        override = MeridionalOverride(
            hub_control_points_rz=tuple(hub),
            shroud_control_points_rz=tuple(shroud),
            leading_edge_hub_fraction=0.30,
            leading_edge_shroud_fraction=0.11,
        )
        edited = size_pump(
            PumpRequirements(
                45.0,
                120.0,
                2950.0,
                "Water",
                25.0,
                meridional_override=override,
            )
        )
        self.assertIn("user edited", edited.meridional.curve_type)
        self.assertNotEqual(automatic.design_id, edited.design_id)
        self.assertEqual(
            edited.engineering_record.parameters["requirements"][
                "meridional_override"
            ]["hub_control_points_rz"][2],
            hub[2],
        )
        self.assertGreater(edited.meridional.minimum_channel_height_mm, 0.0)

    def test_meridional_override_cannot_move_locked_endpoints(self):
        design = size_pump(
            PumpRequirements(45.0, 120.0, 2950.0, "Water", 25.0)
        )
        hub = list(design.meridional.hub_control_points_rz)
        hub[0] = (hub[0][0] + 1.0, hub[0][1])
        override = MeridionalOverride(
            hub_control_points_rz=tuple(hub),
            shroud_control_points_rz=design.meridional.shroud_control_points_rz,
            leading_edge_hub_fraction=design.meridional.leading_edge_hub_fraction,
            leading_edge_shroud_fraction=design.meridional.leading_edge_shroud_fraction,
        )
        with self.assertRaisesRegex(ValueError, "hydraulically locked"):
            create_edited_meridional_design(design.meridional, override)

    def test_meridional_override_is_not_silently_applied_to_all_stages(self):
        design = size_pump(
            PumpRequirements(45.0, 120.0, 2950.0, "Water", 25.0)
        )
        override = MeridionalOverride.from_design(design.meridional)
        with self.assertRaisesRegex(DesignValidationError, "one radial stage"):
            size_pump(
                PumpRequirements(
                    45.0,
                    120.0,
                    2950.0,
                    "Water",
                    25.0,
                    stage_count=2,
                    meridional_override=override,
                )
            )

    def test_default_architecture_is_single_stage_volute_and_radial(self):
        design = size_pump(
            PumpRequirements(45.0, 120.0, 2950.0, "Water", 25.0)
        )
        self.assertEqual(design.architecture.machine_configuration, "Single stage")
        self.assertEqual(len(design.stages), 1)
        self.assertEqual(design.stages[0].resolved_flow_type, "Radial")
        self.assertIn("Volute", design.architecture.component_sequence)
        self.assertTrue(design.architecture.has_supported_stationary_cad)
        self.assertTrue(design.architecture.has_complete_assembly_cad)

    def test_single_volute_is_sized_from_internal_flow_and_checked(self):
        design = size_pump(
            PumpRequirements(45.0, 120.0, 2950.0, "Water", 25.0)
        )
        volute = design.volute
        expected_internal_flow = (
            design.requirements.discharge_m3_h
            / 3600.0
            / (design.performance.volumetric_efficiency / 100.0)
        )
        self.assertAlmostEqual(
            volute.internal_flow_rate_m3_s, expected_internal_flow, places=4
        )
        self.assertGreaterEqual(volute.inlet_width_ratio_b4_b2, 1.05)
        self.assertLessEqual(volute.inlet_width_ratio_b4_b2, 1.20)
        self.assertGreaterEqual(volute.wrap_angle_deg, 330.0)
        self.assertTrue(
            all(
                end > start
                for start, end in zip(
                    volute.station_areas_mm2, volute.station_areas_mm2[1:]
                )
            )
        )
        self.assertLessEqual(
            volute.discharge_cone_angle_deg,
            volute.discharge_max_cone_angle_deg,
        )
        volute_checks = {
            check.key: check.status
            for check in design.engineering_record.checks
            if "volute" in check.key
        }
        self.assertTrue(volute_checks)
        self.assertNotIn("fail", volute_checks.values())
        self.assertGreaterEqual(design.stages[0].work_coefficient_psi, 0.7)
        self.assertLessEqual(design.stages[0].work_coefficient_psi, 1.3)

    def test_multistage_head_is_split_and_each_stage_is_resized(self):
        design = size_pump(
            PumpRequirements(
                45.0,
                120.0,
                2950.0,
                "Water",
                25.0,
                stage_count=2,
            )
        )
        self.assertEqual(len(design.stages), 2)
        self.assertAlmostEqual(design.total_stage_head_m, 45.0, places=6)
        self.assertTrue(all(stage.head_m == 22.5 for stage in design.stages))
        self.assertTrue(
            all(abs(stage.performance.specific_speed_nq - 52.13) < 0.02 for stage in design.stages)
        )
        self.assertIn("Radial return channel 1", design.architecture.component_sequence)
        self.assertIn("Final Volute", design.architecture.component_sequence)

    def test_custom_stage_head_fractions_are_validated(self):
        design = size_pump(
            PumpRequirements(
                45.0,
                120.0,
                2950.0,
                "Water",
                25.0,
                stage_count=2,
                stage_head_fractions=(0.4, 0.6),
            )
        )
        self.assertEqual([stage.head_m for stage in design.stages], [18.0, 27.0])
        with self.assertRaisesRegex(DesignValidationError, "sum to 1.0"):
            size_pump(
                PumpRequirements(
                    45.0,
                    120.0,
                    2950.0,
                    "Water",
                    25.0,
                    stage_count=2,
                    stage_head_fractions=(0.4, 0.5),
                )
            )

    def test_mixed_flow_request_cannot_be_silently_substituted(self):
        with self.assertRaisesRegex(DesignValidationError, "Radial substitution is blocked"):
            size_pump(
                PumpRequirements(
                    45.0,
                    120.0,
                    2950.0,
                    "Water",
                    25.0,
                    impeller_flow_type=FLOW_TYPE_MIXED,
                )
            )

    def test_radial_topology_gate_rejects_stage_split_that_enters_mixed_range(self):
        with self.assertRaisesRegex(DesignValidationError, "work coefficient"):
            size_pump(
                PumpRequirements(
                    45.0,
                    120.0,
                    2950.0,
                    "Water",
                    25.0,
                    stage_count=4,
                )
            )

    def test_vaned_diffuser_collector_enables_supported_stationary_cad(self):
        design = size_pump(
            PumpRequirements(
                45.0,
                120.0,
                2950.0,
                "Water",
                25.0,
                single_stage_collector=COLLECTOR_VANED_DIFFUSER,
            )
        )
        self.assertTrue(design.architecture.has_supported_stationary_cad)


if __name__ == "__main__":
    unittest.main()
