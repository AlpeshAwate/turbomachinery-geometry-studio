import json
import os
import tempfile
import unittest

from core.blade_builder import create_cfd_domain_definition
from core.openfoam_case import generate_steady_mrf_case
from core.pump_design import PumpRequirements, size_pump


class OpenFoamCaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.design = size_pump(
            PumpRequirements(45.0, 120.0, 2950.0, "Water", 25.0)
        )
        cls.definition = create_cfd_domain_definition(
            cls.design.impeller,
            cls.design.diffuser,
            cls.design.meridional,
            cls.design.volute,
        )

    def _dummy_boundaries(self, root):
        boundaries = {}
        for name in (
            "rotor_inlet",
            "rotor_walls",
            "virtual_inlet_walls",
            "stationary_outlet",
            "stationary_walls",
        ):
            path = os.path.join(root, f"{name}.stl")
            with open(path, "w", encoding="ascii") as stream:
                stream.write("solid empty\nendsolid empty\n")
            boundaries[name] = path
        return boundaries

    def test_generates_single_region_mrf_case_without_rsi_boundaries(self):
        with tempfile.TemporaryDirectory() as root:
            case_dir = os.path.join(root, "case")
            result = generate_steady_mrf_case(
                self.design,
                self.definition,
                case_dir,
                self._dummy_boundaries(root),
                (-220.0, -220.0, 0.0, 360.0, 300.0, 90.0),
            )
            self.assertEqual(result["solver"], "simpleFoam")
            self.assertEqual(result["rotating_cell_zone"], "rotorZone")
            self.assertEqual(
                result["excluded_internal_surfaces"],
                ["rotor_rsi", "stationary_rsi"],
            )
            tri_dir = os.path.join(case_dir, "constant", "triSurface")
            self.assertFalse(os.path.exists(os.path.join(tri_dir, "rotor_rsi.stl")))
            self.assertFalse(
                os.path.exists(os.path.join(tri_dir, "stationary_rsi.stl"))
            )
            for relative in (
                "0/U",
                "0/p",
                "0/k",
                "0/omega",
                "0/nut",
                "constant/MRFProperties",
                "system/blockMeshDict",
                "system/snappyHexMeshDict",
                "system/topoSetDict",
                "system/controlDict",
                "Allrun",
                "case_manifest.json",
            ):
                self.assertTrue(os.path.isfile(os.path.join(case_dir, relative)))

            with open(
                os.path.join(case_dir, "constant", "MRFProperties"),
                encoding="utf-8",
            ) as stream:
                mrf = stream.read()
            self.assertIn("cellZone            rotorZone", mrf)
            self.assertIn("virtual_inlet_walls", mrf)
            with open(os.path.join(case_dir, "0", "U"), encoding="utf-8") as stream:
                velocity = stream.read()
            self.assertIn("flowRateInletVelocity", velocity)
            self.assertIn("rotatingWallVelocity", velocity)
            self.assertIn("type slip", velocity)
            with open(
                os.path.join(case_dir, "system", "snappyHexMeshDict"),
                encoding="utf-8",
            ) as stream:
                snappy = stream.read()
            self.assertNotIn("rotor_rsi", snappy)
            self.assertNotIn("stationary_rsi", snappy)
            with open(
                os.path.join(case_dir, "system", "controlDict"),
                encoding="utf-8",
            ) as stream:
                control = stream.read()
            self.assertIn("type            pressure", control)
            self.assertIn("name            rotor_inlet", control)
            self.assertIn("name            stationary_outlet", control)
            self.assertIn("patches         (rotor_walls)", control)
            self.assertIn("type            solverInfo", control)
            with open(result["manifest_path"], encoding="utf-8") as stream:
                manifest = json.load(stream)
            self.assertEqual(manifest["preflight"]["stl_coordinate_unit"], "m")
            self.assertEqual(manifest["operating_point"]["target_head_m"], 45.0)

    def test_missing_required_patch_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            boundaries = self._dummy_boundaries(root)
            del boundaries["stationary_outlet"]
            with self.assertRaisesRegex(ValueError, "stationary_outlet"):
                generate_steady_mrf_case(
                    self.design,
                    self.definition,
                    os.path.join(root, "case"),
                    boundaries,
                    (-220.0, -220.0, 0.0, 360.0, 300.0, 90.0),
                )


if __name__ == "__main__":
    unittest.main()
