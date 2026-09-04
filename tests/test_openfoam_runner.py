import json
import math
import os
import tempfile
import unittest

from core.openfoam_runner import (
    ProcessResult,
    _windows_path_to_wsl,
    evaluate_openfoam_case,
    evaluate_outputs,
    parse_check_mesh,
    parse_openfoam_release,
    parse_solver_log,
)
from core.pump_design import PumpRequirements, size_pump


CHECK_MESH_LOG = """
Mesh stats
    cells:            582104
Number of regions: 1 (OK).
Mesh non-orthogonality Max: 61.2 average: 8.4
Max skewness = 3.1 OK.
Mesh OK.
"""

SOLVER_LOG = """
Time = 410
smoothSolver:  Solving for Ux, Initial residual = 7e-06, Final residual = 2e-07, No Iterations 2
smoothSolver:  Solving for Uy, Initial residual = 8e-06, Final residual = 2e-07, No Iterations 2
smoothSolver:  Solving for Uz, Initial residual = 6e-06, Final residual = 2e-07, No Iterations 2
GAMG:  Solving for p, Initial residual = 9e-06, Final residual = 3e-07, No Iterations 2
smoothSolver:  Solving for k, Initial residual = 5e-06, Final residual = 1e-07, No Iterations 2
smoothSolver:  Solving for omega, Initial residual = 4e-06, Final residual = 1e-07, No Iterations 2
SIMPLE solution converged in 410 iterations
End
"""


class OpenFoamRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.design = size_pump(
            PumpRequirements(45.0, 120.0, 2950.0, "Water", 25.0)
        )

    def _write_surface(self, case_dir, name, value):
        directory = os.path.join(case_dir, "postProcessing", name, "0")
        os.makedirs(directory, exist_ok=True)
        with open(
            os.path.join(directory, "surfaceFieldValue.dat"),
            "w",
            encoding="utf-8",
        ) as stream:
            stream.write(f"# Time value\n410 {value}\n")

    def _write_complete_outputs(self, case_dir):
        target_flow = self.design.requirements.discharge_m3_h / 3600.0
        density = self.design.fluid.density
        pressure_rise = density * 9.80665 * 45.0
        omega = 2.0 * math.pi * self.design.requirements.rpm / 60.0
        hydraulic_power = density * 9.80665 * target_flow * 45.0
        torque = hydraulic_power / (0.82 * omega)
        self._write_surface(case_dir, "inletFlow", -target_flow)
        self._write_surface(case_dir, "outletFlow", target_flow * 0.998)
        self._write_surface(case_dir, "inletTotalPressure", 101325.0)
        self._write_surface(
            case_dir, "outletTotalPressure", 101325.0 + pressure_rise
        )
        directory = os.path.join(case_dir, "postProcessing", "rotorForces", "0")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "moment.dat"), "w", encoding="utf-8") as stream:
            stream.write(
                "# Time total pressure viscous porous\n"
                f"410 (0 0 {torque}) (0 0 {torque * 0.98}) "
                f"(0 0 {torque * 0.02}) (0 0 0)\n"
            )

    def _write_manifest(self, case_dir):
        with open(
            os.path.join(case_dir, "case_manifest.json"), "w", encoding="utf-8"
        ) as stream:
            json.dump({"schema_version": 1, "design_id": self.design.design_id}, stream)

    def test_parsers_extract_mesh_and_last_iteration_residuals(self):
        mesh = parse_check_mesh(CHECK_MESH_LOG)
        solver = parse_solver_log(
            SOLVER_LOG.replace("Time = 410", "Time = 1\n"
            "smoothSolver: Solving for Ux, Initial residual = 0.1, Final residual = 0.01, No Iterations 2\n"
            "Time = 410")
        )
        self.assertTrue(mesh["mesh_ok"])
        self.assertEqual(mesh["regions"], 1)
        self.assertEqual(mesh["cells"], 582104)
        self.assertAlmostEqual(mesh["maximum_non_orthogonality"], 61.2)
        self.assertTrue(solver["converged"])
        self.assertAlmostEqual(solver["final_residuals"]["U"], 8.0e-6)

    def test_wsl_bashrc_path_is_not_rewritten_when_already_posix(self):
        self.assertEqual(
            _windows_path_to_wsl("/opt/openfoam/etc/bashrc"),
            "/opt/openfoam/etc/bashrc",
        )

    def test_openfoam_release_parser_accepts_open_cfd_release_format(self):
        self.assertEqual(parse_openfoam_release("OpenFOAM-v2312"), 2312)
        self.assertIsNone(parse_openfoam_release("OpenFOAM 11"))

    def test_completed_outputs_pass_all_hydraulic_gates(self):
        with tempfile.TemporaryDirectory() as case_dir:
            self._write_complete_outputs(case_dir)
            analysis, gates = evaluate_outputs(
                self.design, case_dir, CHECK_MESH_LOG, SOLVER_LOG
            )
            self.assertTrue(all(gate["status"] == "pass" for gate in gates))
            self.assertAlmostEqual(analysis["performance"]["head_m"], 45.0)
            self.assertAlmostEqual(
                analysis["performance"]["hydraulic_efficiency_percent"],
                81.918,
                places=3,
            )

    def test_runner_writes_passed_versioned_result(self):
        with tempfile.TemporaryDirectory() as case_dir:
            self._write_manifest(case_dir)
            self._write_complete_outputs(case_dir)

            def executor(command, cwd, timeout):
                executable = " ".join(command)
                if "WM_PROJECT_VERSION" in executable:
                    return ProcessResult(0, "OpenFOAM-v2312\n")
                if "checkMesh" in executable:
                    return ProcessResult(0, CHECK_MESH_LOG)
                if "simpleFoam" in executable:
                    return ProcessResult(0, SOLVER_LOG)
                return ProcessResult(0, "End\n")

            result = evaluate_openfoam_case(
                self.design, case_dir, backend="local", executor=executor
            )
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["schema_version"], 1)
            self.assertEqual(len(result["commands"]), 8)
            with open(
                os.path.join(case_dir, "simulation_result.json"), encoding="utf-8"
            ) as stream:
                saved = json.load(stream)
            self.assertEqual(saved["design_id"], self.design.design_id)
            self.assertEqual(saved["status"], "passed")

    def test_runner_stops_and_records_environment_failure(self):
        with tempfile.TemporaryDirectory() as case_dir:
            self._write_manifest(case_dir)

            def executor(command, cwd, timeout):
                return ProcessResult(127, "", "surfaceFeatureExtract: not found")

            result = evaluate_openfoam_case(
                self.design, case_dir, backend="local", executor=executor
            )
            self.assertEqual(result["status"], "failed")
            self.assertEqual(len(result["commands"]), 1)
            self.assertEqual(result["gates"][0]["name"], "openfoam_execution")
            self.assertTrue(
                os.path.isfile(os.path.join(case_dir, "simulation_result.json"))
            )

    def test_wsl_backend_stages_case_in_space_free_runtime_directory(self):
        with tempfile.TemporaryDirectory(prefix="pumpai case ") as case_dir:
            self._write_manifest(case_dir)
            self._write_complete_outputs(case_dir)
            observed_commands = []

            def executor(command, cwd, timeout):
                executable = " ".join(command)
                observed_commands.append(executable)
                if "WM_PROJECT_VERSION" in executable:
                    return ProcessResult(0, "OpenFOAM-v2606\n")
                if "checkMesh" in executable:
                    return ProcessResult(0, CHECK_MESH_LOG)
                if "simpleFoam" in executable:
                    return ProcessResult(0, SOLVER_LOG)
                return ProcessResult(0, "")

            result = evaluate_openfoam_case(
                self.design,
                case_dir,
                backend="wsl",
                wsl_distribution="Ubuntu",
                openfoam_bashrc="/opt/openfoam/etc/bashrc",
                executor=executor,
            )

            self.assertEqual(result["status"], "passed")
            self.assertTrue(result["wsl_case_staged"])
            self.assertEqual(result["commands"][0]["step"], "wsl_stage")
            self.assertEqual(result["commands"][-1]["step"], "wsl_sync")
            solver_command = next(
                command for command in observed_commands if "surfaceFeatureExtract" in command
            )
            self.assertIn("cd /tmp/pumpai-case-", solver_command)
            self.assertNotIn(case_dir, solver_command)


if __name__ == "__main__":
    unittest.main()
