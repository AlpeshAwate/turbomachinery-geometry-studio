import json
import os
import tempfile
import unittest

from impeller_generator import load_requirements, requirements_from_mapping


class CommandLineRequirementTests(unittest.TestCase):
    def test_loads_wrapped_yaml_and_converts_sequence_fields(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "design.yaml")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(
                    "requirements:\n"
                    "  head_m: 45\n"
                    "  discharge_m3_h: 120\n"
                    "  rpm: 2950\n"
                    "  liquid_type: Water\n"
                    "  temperature_c: 25\n"
                    "  stage_head_fractions: [1.0]\n"
                    "  blade_thickness_profile:\n"
                    "    chord_fractions: [0.0, 0.5, 1.0]\n"
                    "    hub_factors: [0.7, 1.0, 0.6]\n"
                    "    shroud_factors: [0.6, 0.9, 0.5]\n"
                )
            requirements = load_requirements(path)
            self.assertEqual(requirements.stage_head_fractions, (1.0,))
            self.assertEqual(
                requirements.blade_thickness_profile.chord_fractions,
                (0.0, 0.5, 1.0),
            )

    def test_loads_flat_json(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "design.json")
            with open(path, "w", encoding="utf-8") as stream:
                json.dump(
                    {
                        "head_m": 30,
                        "discharge_m3_h": 80,
                        "rpm": 2900,
                        "liquid_type": "Water",
                        "temperature_c": 20,
                    },
                    stream,
                )
            requirements = load_requirements(path)
            self.assertEqual(requirements.head_m, 30)
            self.assertEqual(requirements.discharge_m3_h, 80)

    def test_unknown_fields_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown pump requirement fields"):
            requirements_from_mapping(
                {
                    "head_m": 45,
                    "discharge_m3_h": 120,
                    "rpm": 2950,
                    "liquid_type": "Water",
                    "temperature_c": 25,
                    "heda_m": 44,
                }
            )


if __name__ == "__main__":
    unittest.main()
