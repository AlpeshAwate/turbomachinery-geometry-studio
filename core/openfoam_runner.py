"""Execute and evaluate a generated steady-MRF OpenFOAM pump case.

The runner is deliberately fail-closed.  A completed ``simpleFoam`` process is
not enough to accept a result: mesh quality, single-region topology, solver
convergence, mass-flow closure, pressure rise, and rotor torque must all be
present and pass their respective gates.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import glob
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
from typing import Callable, Sequence, TYPE_CHECKING

if TYPE_CHECKING:
    from core.pump_design import CompletePumpDesign


RESULT_SCHEMA_VERSION = 1
OPENFOAM_STEPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("foamVersion", ("printenv", "WM_PROJECT_VERSION")),
    ("surfaceFeatureExtract", ("surfaceFeatureExtract",)),
    ("blockMesh", ("blockMesh",)),
    ("snappyHexMesh", ("snappyHexMesh", "-overwrite")),
    ("checkMesh_initial", ("checkMesh", "-allGeometry", "-allTopology")),
    ("topoSet", ("topoSet",)),
    ("checkMesh_final", ("checkMesh", "-allGeometry", "-allTopology")),
    ("simpleFoam", ("simpleFoam",)),
)


@dataclass(frozen=True)
class EvaluationThresholds:
    """Acceptance limits for the first automated CFD screening pass."""

    maximum_non_orthogonality: float = 70.0
    maximum_skewness: float = 20.0
    maximum_flow_imbalance_fraction: float = 0.01
    maximum_final_residual: float = 1.0e-5
    maximum_efficiency_percent: float = 105.0


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Executor = Callable[[Sequence[str], str, float], ProcessResult]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: str, value: dict) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


def _default_executor(command: Sequence[str], cwd: str, timeout_s: float) -> ProcessResult:
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        check=False,
    )
    return ProcessResult(completed.returncode, completed.stdout, completed.stderr)


def _windows_path_to_wsl(path: str) -> str:
    """Convert an absolute Windows path without requiring a running distro."""

    if path.startswith("/"):
        return path
    resolved = os.path.abspath(path).replace("\\", "/")
    match = re.fullmatch(r"([A-Za-z]):/(.*)", resolved)
    if not match:
        return resolved
    return f"/mnt/{match.group(1).lower()}/{match.group(2)}"


def _command_for_backend(
    argv: Sequence[str],
    case_dir: str,
    backend: str,
    wsl_distribution: str | None,
    openfoam_bashrc: str | None,
) -> list[str]:
    command_text = shlex.join(argv)
    if backend == "local":
        if openfoam_bashrc:
            command_text = f"source {shlex.quote(openfoam_bashrc)} && {command_text}"
            return ["bash", "-lc", command_text]
        return list(argv)

    wsl_case = _windows_path_to_wsl(case_dir)
    prefix = f"cd {shlex.quote(wsl_case)}"
    if openfoam_bashrc:
        bashrc = _windows_path_to_wsl(openfoam_bashrc)
        prefix += f" && source {shlex.quote(bashrc)}"
    shell_command = f"{prefix} && {command_text}"
    command = ["wsl.exe"]
    if wsl_distribution:
        command.extend(("--distribution", wsl_distribution))
    command.extend(("--", "bash", "-lc", shell_command))
    return command


def resolve_backend(backend: str) -> str:
    normalized = backend.lower()
    if normalized not in {"auto", "local", "wsl"}:
        raise ValueError("OpenFOAM backend must be one of: auto, local, wsl.")
    if normalized == "auto":
        return "wsl" if os.name == "nt" else "local"
    return normalized


def _parse_float(value: str) -> float:
    return float(value.rstrip(",;"))


_FLOAT_PATTERN = re.compile(
    r"(?<![A-Za-z])[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
)


def parse_check_mesh(log_text: str) -> dict:
    """Extract the final quality summary from a ``checkMesh`` log."""

    def last_match(pattern: str) -> str | None:
        matches = re.findall(pattern, log_text, flags=re.IGNORECASE)
        return matches[-1] if matches else None

    cells = last_match(r"\bcells:\s*(\d+)")
    regions = last_match(r"Number of regions:\s*(\d+)")
    non_ortho = last_match(
        r"Mesh non-orthogonality Max:\s*([-+0-9.eE]+)"
    )
    skewness = last_match(r"Max skewness\s*=\s*([-+0-9.eE]+)")
    failed_checks = last_match(r"Failed\s+(\d+)\s+mesh checks")
    return {
        "cells": int(cells) if cells is not None else None,
        "regions": int(regions) if regions is not None else None,
        "maximum_non_orthogonality": (
            _parse_float(non_ortho) if non_ortho is not None else None
        ),
        "maximum_skewness": (
            _parse_float(skewness) if skewness is not None else None
        ),
        "failed_checks": int(failed_checks) if failed_checks is not None else 0,
        "mesh_ok": "Mesh OK." in log_text,
    }


def parse_openfoam_release(log_text: str) -> int | None:
    """Return an OpenCFD year/month release number such as ``2312``."""

    matches = re.findall(r"(?:OpenFOAM[-_ ]?)?v(\d{4})\b", log_text, re.IGNORECASE)
    return int(matches[-1]) if matches else None


def parse_solver_log(log_text: str) -> dict:
    """Extract last SIMPLE residuals and explicit convergence evidence."""

    last_by_field: dict[str, float] = {}
    pattern = re.compile(
        r"Solving for\s+([^,\s]+),\s+Initial residual\s*=\s*"
        r"([-+0-9.eE]+),\s+Final residual\s*=\s*([-+0-9.eE]+)",
        flags=re.IGNORECASE,
    )
    for field, initial, _final in pattern.findall(log_text):
        last_by_field[field] = _parse_float(initial)

    velocity_components = [
        value
        for field, value in last_by_field.items()
        if field.lower() in {"ux", "uy", "uz"}
    ]
    residuals = {
        field: value
        for field, value in last_by_field.items()
        if field.lower() not in {"ux", "uy", "uz"}
    }
    if velocity_components:
        residuals["U"] = max(velocity_components)

    convergence_match = re.findall(
        r"SIMPLE solution converged in\s+(\d+)\s+iterations", log_text
    )
    times = re.findall(r"^Time\s*=\s*([-+0-9.eE]+)", log_text, re.MULTILINE)
    return {
        "converged": bool(convergence_match),
        "convergence_iteration": (
            int(convergence_match[-1]) if convergence_match else None
        ),
        "last_time": _parse_float(times[-1]) if times else None,
        "final_residuals": residuals,
        "ended_cleanly": bool(re.search(r"^End\s*$", log_text, re.MULTILINE)),
    }


def _latest_data_line(case_dir: str, object_name: str, filename: str) -> str | None:
    candidates = glob.glob(
        os.path.join(case_dir, "postProcessing", object_name, "*", filename)
    )
    if not candidates:
        return None

    def sort_key(path: str) -> tuple[float, float]:
        time_name = Path(path).parent.name
        try:
            numeric_time = float(time_name)
        except ValueError:
            numeric_time = -math.inf
        return numeric_time, os.path.getmtime(path)

    for path in sorted(candidates, key=sort_key, reverse=True):
        with open(path, encoding="utf-8", errors="replace") as stream:
            lines = [line.strip() for line in stream if line.strip() and not line.lstrip().startswith("#")]
        if lines:
            return lines[-1]
    return None


def _surface_scalar(case_dir: str, object_name: str) -> float | None:
    line = _latest_data_line(case_dir, object_name, "surfaceFieldValue.dat")
    if line is None:
        return None
    values = [_parse_float(item) for item in _FLOAT_PATTERN.findall(line)]
    return values[-1] if len(values) >= 2 else None


def _rotor_torque(case_dir: str) -> float | None:
    line = _latest_data_line(case_dir, "rotorForces", "moment.dat")
    if line is None:
        return None
    values = [_parse_float(item) for item in _FLOAT_PATTERN.findall(line)]
    # Time is followed by the total moment vector, then its contributions.
    return values[3] if len(values) >= 4 else None


def read_performance_outputs(case_dir: str) -> dict:
    return {
        "inlet_flow_m3_s": _surface_scalar(case_dir, "inletFlow"),
        "outlet_flow_m3_s": _surface_scalar(case_dir, "outletFlow"),
        "inlet_total_pressure_pa": _surface_scalar(case_dir, "inletTotalPressure"),
        "outlet_total_pressure_pa": _surface_scalar(case_dir, "outletTotalPressure"),
        "rotor_torque_z_n_m": _rotor_torque(case_dir),
    }


def _gate(name: str, passed: bool, message: str, **evidence: object) -> dict:
    return {
        "name": name,
        "status": "pass" if passed else "fail",
        "message": message,
        **evidence,
    }


def evaluate_outputs(
    design: "CompletePumpDesign",
    case_dir: str,
    check_mesh_log: str,
    solver_log: str,
    thresholds: EvaluationThresholds = EvaluationThresholds(),
) -> tuple[dict, list[dict]]:
    """Evaluate completed solver artifacts and return metrics plus gates."""

    mesh = parse_check_mesh(check_mesh_log)
    solver = parse_solver_log(solver_log)
    raw = read_performance_outputs(case_dir)
    gates = [
        _gate(
            "mesh_completed",
            bool(mesh["mesh_ok"]) and mesh["failed_checks"] == 0,
            "checkMesh must finish with Mesh OK and no failed checks.",
            observed={"mesh_ok": mesh["mesh_ok"], "failed_checks": mesh["failed_checks"]},
        ),
        _gate(
            "single_fluid_region",
            mesh["regions"] == 1,
            "The MRF screening case must contain exactly one connected fluid region.",
            observed=mesh["regions"],
            limit=1,
        ),
        _gate(
            "maximum_non_orthogonality",
            mesh["maximum_non_orthogonality"] is not None
            and mesh["maximum_non_orthogonality"] <= thresholds.maximum_non_orthogonality,
            "Mesh non-orthogonality must stay within the configured limit.",
            observed=mesh["maximum_non_orthogonality"],
            limit=thresholds.maximum_non_orthogonality,
        ),
        _gate(
            "maximum_skewness",
            mesh["maximum_skewness"] is not None
            and mesh["maximum_skewness"] <= thresholds.maximum_skewness,
            "Mesh skewness must stay within the configured limit.",
            observed=mesh["maximum_skewness"],
            limit=thresholds.maximum_skewness,
        ),
    ]

    required_residuals = {"p", "U", "k", "omega"}
    observed_residuals = solver["final_residuals"]
    residuals_complete = required_residuals.issubset(observed_residuals)
    maximum_residual = (
        max(observed_residuals[name] for name in required_residuals)
        if residuals_complete
        else None
    )
    gates.extend(
        (
            _gate(
                "solver_convergence",
                bool(solver["converged"]) and bool(solver["ended_cleanly"]),
                "simpleFoam must explicitly report SIMPLE convergence and end cleanly.",
                observed={
                    "converged": solver["converged"],
                    "ended_cleanly": solver["ended_cleanly"],
                },
            ),
            _gate(
                "final_residuals",
                residuals_complete
                and maximum_residual is not None
                and maximum_residual <= thresholds.maximum_final_residual,
                "Final residual evidence is required for p, U, k, and omega.",
                observed={
                    "maximum": maximum_residual,
                    "fields": observed_residuals,
                },
                limit=thresholds.maximum_final_residual,
            ),
        )
    )

    values_complete = all(value is not None and math.isfinite(value) for value in raw.values())
    gates.append(
        _gate(
            "performance_outputs_complete",
            values_complete,
            "Inlet/outlet flow and total pressure plus rotor torque are required.",
            observed=raw,
        )
    )

    performance = {**raw}
    if values_complete:
        inlet_flow = abs(float(raw["inlet_flow_m3_s"]))
        outlet_flow = abs(float(raw["outlet_flow_m3_s"]))
        target_flow = design.requirements.discharge_m3_h / 3600.0
        mean_flow = 0.5 * (inlet_flow + outlet_flow)
        imbalance = abs(inlet_flow - outlet_flow) / max(target_flow, 1.0e-12)
        pressure_rise = float(raw["outlet_total_pressure_pa"]) - float(
            raw["inlet_total_pressure_pa"]
        )
        density = design.fluid.density
        head = pressure_rise / (density * 9.80665)
        torque = abs(float(raw["rotor_torque_z_n_m"]))
        omega = 2.0 * math.pi * design.requirements.rpm / 60.0
        shaft_power_w = torque * omega
        hydraulic_power_w = density * 9.80665 * mean_flow * head
        efficiency = (
            100.0 * hydraulic_power_w / shaft_power_w
            if shaft_power_w > 0.0
            else None
        )
        performance.update(
            {
                "mean_flow_m3_s": mean_flow,
                "flow_imbalance_fraction": imbalance,
                "total_pressure_rise_pa": pressure_rise,
                "head_m": head,
                "shaft_power_kw": shaft_power_w / 1000.0,
                "hydraulic_power_kw": hydraulic_power_w / 1000.0,
                "hydraulic_efficiency_percent": efficiency,
                "head_error_percent": 100.0
                * (head - design.requirements.head_m)
                / design.requirements.head_m,
            }
        )
        gates.extend(
            (
                _gate(
                    "flow_closure",
                    imbalance <= thresholds.maximum_flow_imbalance_fraction,
                    "Inlet and outlet volume flow must close within the configured fraction of target flow.",
                    observed=imbalance,
                    limit=thresholds.maximum_flow_imbalance_fraction,
                ),
                _gate(
                    "physical_performance",
                    pressure_rise > 0.0
                    and torque > 0.0
                    and efficiency is not None
                    and math.isfinite(efficiency)
                    and 0.0 < efficiency <= thresholds.maximum_efficiency_percent,
                    "Pressure rise, torque, and efficiency must be positive and physically bounded.",
                    observed={
                        "pressure_rise_pa": pressure_rise,
                        "torque_n_m": torque,
                        "efficiency_percent": efficiency,
                    },
                    limit={"maximum_efficiency_percent": thresholds.maximum_efficiency_percent},
                ),
            )
        )
    else:
        gates.extend(
            (
                _gate(
                    "flow_closure",
                    False,
                    "Flow closure cannot be evaluated because outputs are incomplete.",
                ),
                _gate(
                    "physical_performance",
                    False,
                    "Hydraulic performance cannot be evaluated because outputs are incomplete.",
                ),
            )
        )

    return {"mesh": mesh, "solver": solver, "performance": performance}, gates


def evaluate_openfoam_case(
    design: "CompletePumpDesign",
    case_dir: str,
    *,
    backend: str = "auto",
    wsl_distribution: str | None = None,
    openfoam_bashrc: str | None = None,
    timeout_s: float = 7200.0,
    thresholds: EvaluationThresholds = EvaluationThresholds(),
    executor: Executor | None = None,
) -> dict:
    """Run a generated case and always write ``simulation_result.json``."""

    case_dir = os.path.abspath(case_dir)
    manifest_path = os.path.join(case_dir, "case_manifest.json")
    if not os.path.isfile(manifest_path):
        raise FileNotFoundError(f"OpenFOAM case manifest not found: {manifest_path}")
    with open(manifest_path, encoding="utf-8") as stream:
        manifest_bytes = stream.read().encode("utf-8")
    manifest = json.loads(manifest_bytes)
    if manifest.get("design_id") != design.design_id:
        raise ValueError(
            "OpenFOAM case design ID does not match the requested design: "
            f"{manifest.get('design_id')} != {design.design_id}"
        )

    selected_backend = resolve_backend(backend)
    run = executor or _default_executor
    log_dir = os.path.join(case_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    commands: list[dict] = []
    version_gate: dict | None = None
    execution_gate = _gate(
        "openfoam_execution", True, "No OpenFOAM command returned an error."
    )
    started_at = _utc_now()

    for step_name, argv in OPENFOAM_STEPS:
        command = _command_for_backend(
            argv, case_dir, selected_backend, wsl_distribution, openfoam_bashrc
        )
        try:
            process = run(command, case_dir, timeout_s)
            combined = process.stdout
            if process.stderr:
                combined += ("\n" if combined else "") + process.stderr
            returncode = process.returncode
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            combined = f"{type(exc).__name__}: {exc}\n"
            returncode = 127 if isinstance(exc, FileNotFoundError) else 124

        log_path = os.path.join(log_dir, f"{step_name}.log")
        with open(log_path, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(combined)
        commands.append(
            {
                "step": step_name,
                "command": command,
                "return_code": returncode,
                "log": os.path.relpath(log_path, case_dir).replace("\\", "/"),
            }
        )
        if returncode != 0:
            hint = (
                "Install a WSL distribution and OpenCFD OpenFOAM v2312+, then pass "
                "--openfoam-bashrc when it is not sourced automatically."
                if selected_backend == "wsl"
                else "Source an OpenCFD OpenFOAM v2312+ environment or pass --openfoam-bashrc."
            )
            execution_gate = _gate(
                "openfoam_execution",
                False,
                f"{step_name} failed with return code {returncode}. {hint}",
                observed={"failed_step": step_name, "return_code": returncode},
            )
            break
        if step_name == "foamVersion":
            release = parse_openfoam_release(combined)
            version_gate = _gate(
                "openfoam_version",
                release is not None and release >= 2312,
                "The automated case requires OpenCFD OpenFOAM v2312 or newer.",
                observed=release,
                limit={"minimum_release": 2312},
            )
            if version_gate["status"] == "fail":
                break

    analysis = {"mesh": {}, "solver": {}, "performance": {}}
    gates = [execution_gate]
    if version_gate is not None:
        gates.append(version_gate)
    if execution_gate["status"] == "pass" and (
        version_gate is None or version_gate["status"] == "pass"
    ):
        final_mesh_path = os.path.join(log_dir, "checkMesh_final.log")
        solver_path = os.path.join(log_dir, "simpleFoam.log")
        with open(final_mesh_path, encoding="utf-8", errors="replace") as stream:
            mesh_log = stream.read()
        with open(solver_path, encoding="utf-8", errors="replace") as stream:
            solver_log = stream.read()
        analysis, output_gates = evaluate_outputs(
            design, case_dir, mesh_log, solver_log, thresholds
        )
        gates.extend(output_gates)

    status = "passed" if all(gate["status"] == "pass" for gate in gates) else "failed"
    result = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "design_id": design.design_id,
        "status": status,
        "started_at_utc": started_at,
        "completed_at_utc": _utc_now(),
        "case_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "backend": selected_backend,
        "thresholds": asdict(thresholds),
        "commands": commands,
        **analysis,
        "gates": gates,
        "limitations": [
            "Steady single-region MRF screening result; not a transient blade-passing calculation.",
            "Boundary layers are not generated, so y+ and mesh independence remain release gates.",
        ],
    }
    _write_json(os.path.join(case_dir, "simulation_result.json"), result)
    return result
