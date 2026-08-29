import json
import os
import struct
import tempfile
import unittest
from dataclasses import replace
import pyvista as pv

from core.blade_builder import (
    _freeform_blade_solid,
    _log_spiral_solid,
    _unbladed_rotating_flow_volume,
    _validate_neighbor_blade_overlap,
    build_diffuser_solid,
    build_front_shroud_solid,
    build_impeller_solid,
    build_rotating_flow_domain,
    build_stationary_rsi_connection,
    build_volute_flow_domain,
    build_volute_material_solid,
    create_cfd_domain_definition,
    export_turbomachinery_for_openfoam,
    rotating_flow_boundary_patches,
    stationary_connection_boundary_patches,
    volute_flow_boundary_patches,
)
from core.pump_design import (
    COLLECTOR_VANED_DIFFUSER,
    PumpRequirements,
    size_pump,
)


def binary_stl_coordinate_extent(path):
    """Return the largest absolute coordinate from an OpenCASCADE binary STL."""
    largest = 0.0
    with open(path, "rb") as stl_file:
        stl_file.read(80)
        triangle_count = struct.unpack("<I", stl_file.read(4))[0]
        for _ in range(triangle_count):
            record = stl_file.read(50)
            values = struct.unpack("<12fH", record)
            largest = max(largest, *(abs(value) for value in values[3:12]))
    return largest


class CadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.design = size_pump(
            PumpRequirements(45.0, 120.0, 2950.0, "Water", 25.0)
        )

    def test_components_are_connected_valid_solids(self):
        for model in (
            build_impeller_solid(self.design.impeller, self.design.meridional),
            build_diffuser_solid(self.design.diffuser, self.design.impeller),
        ):
            shape = model.val()
            self.assertTrue(shape.isValid())
            self.assertEqual(len(shape.Solids()), 1)
            self.assertGreater(shape.Volume(), 0.0)

    def test_blade_is_one_continuous_loft_without_segment_knuckles(self):
        blade = _log_spiral_solid(
            35.0,
            95.0,
            25.0,
            24.0,
            4.0,
            28.0,
            3.0,
            height_end=14.0,
        ).val()
        self.assertTrue(blade.isValid())
        self.assertEqual(len(blade.Solids()), 1)
        self.assertGreater(blade.Volume(), 0.0)
        # A loft has four continuous side faces and two end caps. The former
        # box/knuckle construction produced dozens of artificial faces.
        self.assertLessEqual(len(blade.Faces()), 8)

    def test_primary_impeller_blade_is_a_valid_five_span_surface(self):
        blade, grid = _freeform_blade_solid(
            self.design.impeller,
            self.design.meridional,
        )
        shape = blade.val()
        self.assertTrue(shape.isValid())
        self.assertEqual(len(shape.Solids()), 1)
        self.assertGreater(shape.Volume(), 0.0)
        self.assertEqual(len(grid.span_positions), 5)
        self.assertEqual(len(shape.Faces()), 10)
        self.assertGreater(grid.wrap_angles_deg[0], grid.wrap_angles_deg[-1])
        self.assertEqual(self.design.impeller.blade_leading_edge_shape, "Ellipse")
        self.assertGreater(self.design.impeller.blade_leading_edge_radius, 0.0)
        self.assertEqual(
            _validate_neighbor_blade_overlap(
                blade, self.design.impeller.blade_count_z
            ),
            0.0,
        )
        with self.assertRaisesRegex(ValueError, "Neighboring blade solids intersect"):
            _validate_neighbor_blade_overlap(blade, 40)

    def test_oversized_blade_edge_radius_is_rejected_before_fillet(self):
        invalid_impeller = replace(
            self.design.impeller,
            blade_leading_edge_radius=0.20 * self.design.impeller.blade_thickness,
            blade_trailing_edge_radius=0.20 * self.design.impeller.blade_thickness,
        )
        with self.assertRaisesRegex(ValueError, "robust CAD limit"):
            _freeform_blade_solid(invalid_impeller, self.design.meridional)

    def test_closed_impeller_full_model_contains_front_shroud(self):
        full_shape = build_impeller_solid(
            self.design.impeller,
            self.design.meridional,
            include_front_shroud=True,
        ).val()
        cutaway_shape = build_impeller_solid(
            self.design.impeller,
            self.design.meridional,
            include_front_shroud=False,
        ).val()
        self.assertGreater(full_shape.Volume(), cutaway_shape.Volume())
        shroud_shape = build_front_shroud_solid(
            self.design.impeller,
            self.design.meridional,
        ).val()
        self.assertTrue(shroud_shape.isValid())
        self.assertEqual(len(shroud_shape.Solids()), 1)
        self.assertGreater(shroud_shape.Volume(), 0.0)
        self.assertLessEqual(len(shroud_shape.Faces()), 7)

    def test_eye_collar_is_a_shroud_material_feature(self):
        without_collar = size_pump(
            PumpRequirements(
                45.0,
                120.0,
                2950.0,
                "Water",
                25.0,
                eye_collar_enabled=False,
            )
        )
        with_shape = build_front_shroud_solid(
            self.design.impeller, self.design.meridional
        ).val()
        without_shape = build_front_shroud_solid(
            without_collar.impeller, without_collar.meridional
        ).val()
        self.assertTrue(self.design.impeller.eye_collar_enabled)
        self.assertGreater(with_shape.Volume(), without_shape.Volume())
        self.assertAlmostEqual(
            with_shape.BoundingBox().zmax - without_shape.BoundingBox().zmax,
            self.design.impeller.eye_collar_axial_length,
            places=2,
        )

    def test_open_impeller_option_is_also_a_connected_solid(self):
        open_design = size_pump(
            PumpRequirements(45.0, 120.0, 2950.0, "Water", 25.0, "Open")
        )
        shape = build_impeller_solid(
            open_design.impeller, open_design.meridional
        ).val()
        self.assertTrue(shape.isValid())
        self.assertEqual(len(shape.Solids()), 1)
        self.assertEqual(open_design.impeller.configuration, "Open")

    def test_rotating_flow_domain_has_extensions_and_matching_rsi(self):
        rotor, definition = build_rotating_flow_domain(
            self.design.impeller,
            self.design.meridional,
            self.design.diffuser,
        )
        stationary = build_stationary_rsi_connection(
            self.design.impeller,
            self.design.diffuser,
            definition,
        )
        for model in (rotor, stationary):
            self.assertTrue(model.val().isValid())
            self.assertEqual(len(model.val().Solids()), 1)
            self.assertGreater(model.val().Volume(), 0.0)

        expected_rsi = 0.25 * (
            self.design.impeller.outlet_diameter_d2
            + self.design.diffuser.inlet_diameter_d3
        )
        self.assertAlmostEqual(definition.rsi_radius_mm, expected_rsi, places=3)
        self.assertGreater(
            definition.rsi_radius_mm,
            definition.impeller_outlet_radius_mm,
        )
        self.assertLess(
            definition.rsi_radius_mm,
            definition.stationary_inlet_radius_mm,
        )
        self.assertGreater(
            definition.inlet_plane_z_mm,
            self.design.impeller.back_shroud_thickness
            + self.design.meridional.axial_length,
        )

        gross = _unbladed_rotating_flow_volume(
            self.design.impeller,
            self.design.meridional,
            definition,
        )
        self.assertLess(rotor.val().Volume(), gross.val().Volume())
        rotor_patches = rotating_flow_boundary_patches(rotor, definition)
        stationary_patches = stationary_connection_boundary_patches(
            stationary, definition
        )
        self.assertEqual(
            set(rotor_patches),
            {
                "rotor_inlet",
                "rotor_rsi",
                "rotor_walls",
                "virtual_inlet_walls",
            },
        )
        self.assertEqual(
            set(stationary_patches),
            {
                "stationary_rsi",
                "stationary_connection_outlet",
                "stationary_connection_walls",
            },
        )
        self.assertAlmostEqual(
            rotor_patches["rotor_rsi"].val().Area(),
            stationary_patches["stationary_rsi"].val().Area(),
            places=3,
        )
        self.assertAlmostEqual(
            rotor_patches["rotor_rsi"].val().Area(),
            definition.rsi_area_mm2,
            places=2,
        )

    def test_volute_material_and_complete_stationary_flow_are_valid_solids(self):
        definition = create_cfd_domain_definition(
            self.design.impeller,
            self.design.diffuser,
            self.design.meridional,
            self.design.volute,
        )
        fluid = build_volute_flow_domain(
            self.design.impeller, self.design.volute, definition
        )
        casing = build_volute_material_solid(
            self.design.impeller, self.design.volute, definition
        )
        for model in (fluid, casing):
            self.assertTrue(model.val().isValid())
            self.assertEqual(len(model.val().Solids()), 1)
            self.assertGreater(model.val().Volume(), 0.0)
        patches = volute_flow_boundary_patches(fluid, definition)
        self.assertEqual(
            set(patches),
            {"stationary_rsi", "stationary_outlet", "stationary_walls"},
        )
        self.assertAlmostEqual(
            patches["stationary_rsi"].val().Area(),
            definition.rsi_area_mm2,
            places=1,
        )
        self.assertEqual(len(patches["stationary_outlet"].val().Faces()), 1)

    def test_openfoam_stl_is_in_metres_and_manifest_is_explicit(self):
        with tempfile.TemporaryDirectory() as output_dir:
            files = export_turbomachinery_for_openfoam(
                self.design, output_dir, export_diffuser=False
            )
            extent = binary_stl_coordinate_extent(files["impeller_stl"])
            self.assertGreater(extent, 0.05)
            self.assertLess(extent, 0.2)
            mesh = pv.read(files["impeller_stl"])
            self.assertEqual(mesh.n_open_edges, 0)
            connected = mesh.connectivity()
            self.assertEqual(len(set(connected.cell_data["RegionId"])), 1)
            with open(files["geometry_manifest"], encoding="utf-8") as manifest_file:
                manifest = json.load(manifest_file)
            self.assertEqual(manifest["stl_coordinate_unit"], "m")
            self.assertEqual(
                manifest["package_type"],
                "openfoam_complete_single_stage_volute_domain",
            )
            self.assertEqual(manifest["schema_version"], 7)
            self.assertEqual(manifest["design_id"], self.design.design_id)
            correlations = manifest["main_dimension_correlations"]
            self.assertEqual(
                correlations["intake_coefficient_epsilon"],
                self.design.impeller.intake_coefficient_epsilon,
            )
            self.assertEqual(
                correlations["outlet_width_ratio_b2_d2"],
                self.design.impeller.outlet_width_ratio_b2_d2,
            )
            self.assertEqual(
                correlations["meridional_deceleration_ratio_cm2_cm1"],
                self.design.impeller.meridional_deceleration_ratio,
            )
            self.assertTrue(manifest["exact_neighbor_cad_overlap_validation"])
            self.assertTrue(manifest["generated_components"]["rotating_fluid_domain"])
            self.assertTrue(
                os.path.isfile(files["rotating_flow_domain_stl"])
            )
            rotor_mesh = pv.read(files["rotating_flow_domain_stl"])
            self.assertEqual(rotor_mesh.n_open_edges, 0)
            rotor_connected = rotor_mesh.connectivity()
            self.assertEqual(
                len(set(rotor_connected.cell_data["RegionId"])), 1
            )
            cfd_domain = manifest["cfd_domain"]
            self.assertEqual(
                cfd_domain["interface_pair"]["rotating_patch"], "rotor_rsi"
            )
            self.assertEqual(
                cfd_domain["interface_pair"]["stationary_patch"],
                "stationary_rsi",
            )
            self.assertIn("virtual_inlet_walls", cfd_domain["boundary_patches"])
            self.assertEqual(
                manifest["solver_case"]["formulation"],
                "steady incompressible RANS, single-region MRF",
            )
            self.assertTrue(os.path.isfile(files["openfoam_case_manifest"]))
            for patch_name in cfd_domain["boundary_patches"]:
                self.assertTrue(
                    os.path.isfile(files[f"{patch_name}_stl"]), patch_name
                )
            self.assertGreater(
                manifest["blade_passage_validation"]["minimum_throat_distance_mm"],
                0.0,
            )
            self.assertEqual(
                manifest["blade_edge_geometry"]["leading_shape"],
                "Ellipse",
            )
            self.assertGreater(
                manifest["blade_edge_geometry"]["leading_radius_mm"],
                0.0,
            )
            self.assertEqual(
                manifest["pump_architecture"]["machine_configuration"],
                "Single stage",
            )
            self.assertIn(
                "Volute", manifest["pump_architecture"]["component_sequence"]
            )
            self.assertEqual(manifest["engineering_check_summary"]["fail"], 0)
            self.assertTrue(os.path.isfile(files["engineering_record"]))
            with open(files["engineering_record"], encoding="utf-8") as record_file:
                record = json.load(record_file)
            self.assertEqual(record["design_id"], self.design.design_id)

    def test_default_volute_collector_exports_material_casing(self):
        with tempfile.TemporaryDirectory() as output_dir:
            files = export_turbomachinery_for_openfoam(
                self.design,
                output_dir,
                export_impeller=False,
                export_diffuser=True,
                export_cfd_domain=False,
            )
            self.assertTrue(os.path.isfile(files["volute_stl"]))
            self.assertTrue(os.path.isfile(files["volute_step"]))
            mesh = pv.read(files["volute_stl"])
            self.assertEqual(mesh.n_open_edges, 0)
            self.assertEqual(
                len(set(mesh.connectivity().cell_data["RegionId"])), 1
            )

    def test_multistage_export_is_blocked_until_return_channel_exists(self):
        multistage = size_pump(
            PumpRequirements(
                45.0,
                120.0,
                2950.0,
                "Water",
                25.0,
                stage_count=2,
            )
        )
        with tempfile.TemporaryDirectory() as output_dir:
            with self.assertRaisesRegex(ValueError, "Multistage export is blocked"):
                export_turbomachinery_for_openfoam(
                    multistage, output_dir, export_diffuser=False
                )

    def test_vaned_diffuser_collector_can_export_stator(self):
        diffuser_design = size_pump(
            PumpRequirements(
                45.0,
                120.0,
                2950.0,
                "Water",
                25.0,
                single_stage_collector=COLLECTOR_VANED_DIFFUSER,
            )
        )
        with tempfile.TemporaryDirectory() as output_dir:
            files = export_turbomachinery_for_openfoam(
                diffuser_design,
                output_dir,
                export_impeller=False,
                export_diffuser=True,
            )
            self.assertTrue(os.path.isfile(files["diffuser_stl"]))


if __name__ == "__main__":
    unittest.main()
