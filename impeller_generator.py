"""Command-line workflow for pump sizing, geometry export, and CFD screening."""

from __future__ import annotations

import argparse
from dataclasses import fields
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from core.blade_builder import export_turbomachinery_for_openfoam
from core.blade_geometry import BladeThicknessProfile
from core.meridional import MeridionalOverride
from core.openfoam_runner import evaluate_openfoam_case
from core.pump_design import (
    PumpRequirements,
    ReferenceImpellerGeometry,
    size_pump,
)


_TUPLE_FIELDS = {
    "stage_head_fractions",
    "spanwise_inlet_angles_override",
    "spanwise_outlet_angles_override",
}


def _tuple_points(
    values: Sequence[Sequence[float]], name: str
) -> tuple[tuple[float, float], ...]:
    try:
        return tuple((float(point[0]), float(point[1])) for point in values)
    except (IndexError, TypeError, ValueError) as exc:
        raise ValueError(
            f"{name} must be a list of [radius_mm, axial_mm] points."
        ) from exc


def requirements_from_mapping(value: Mapping[str, Any]) -> PumpRequirements:
    """Build validated requirement objects from a YAML/JSON mapping."""

    if "requirements" in value:
        wrapped = value["requirements"]
        if not isinstance(wrapped, Mapping):
            raise ValueError("The 'requirements' value must be a mapping.")
        value = wrapped

    allowed = {field.name for field in fields(PumpRequirements)}
    unknown = sorted(set(value).difference(allowed))
    if unknown:
        raise ValueError("Unknown pump requirement fields: " + ", ".join(unknown))
    data = dict(value)
    for name in _TUPLE_FIELDS:
        if data.get(name) is not None:
            data[name] = tuple(float(item) for item in data[name])

    reference = data.get("reference_impeller")
    if reference is not None:
        if not isinstance(reference, Mapping):
            raise ValueError("reference_impeller must be a mapping.")
        data["reference_impeller"] = ReferenceImpellerGeometry(**reference)

    profile = data.get("blade_thickness_profile")
    if profile is not None:
        if not isinstance(profile, Mapping):
            raise ValueError("blade_thickness_profile must be a mapping.")
        profile_data = dict(profile)
        for name in ("chord_fractions", "hub_factors", "shroud_factors"):
            if name in profile_data:
                profile_data[name] = tuple(float(item) for item in profile_data[name])
        data["blade_thickness_profile"] = BladeThicknessProfile(**profile_data)

    meridional = data.get("meridional_override")
    if meridional is not None:
        if not isinstance(meridional, Mapping):
            raise ValueError("meridional_override must be a mapping.")
        meridional_data = dict(meridional)
        for name in ("hub_control_points_rz", "shroud_control_points_rz"):
            if name in meridional_data:
                meridional_data[name] = _tuple_points(meridional_data[name], name)
        data["meridional_override"] = MeridionalOverride(**meridional_data)

    try:
        return PumpRequirements(**data)
    except TypeError as exc:
        raise ValueError(f"Invalid or incomplete pump requirements: {exc}") from exc


def load_requirements(path: str) -> PumpRequirements:
    """Read pump requirements from YAML or JSON."""

    suffix = Path(path).suffix.lower()
    if suffix not in {".yaml", ".yml", ".json"}:
        raise ValueError("Requirements file must use .yaml, .yml, or .json.")
    with open(path, encoding="utf-8") as stream:
        if suffix == ".json":
            value = json.load(stream)
        else:
            value = yaml.safe_load(stream)
    if not isinstance(value, Mapping):
        raise ValueError("Requirements file must contain a mapping/object.")
    return requirements_from_mapping(value)


def _default_output(requirements_path: str) -> str:
    return os.path.join(os.getcwd(), "output", Path(requirements_path).stem)


def _export(requirements: PumpRequirements, output_dir: str) -> tuple[object, dict]:
    design = size_pump(requirements)
    exported = export_turbomachinery_for_openfoam(design, output_dir)
    return design, exported


def _print_exports(exported: Mapping[str, str]) -> None:
    print("Validated geometry package generated:")
    for name, path in exported.items():
        print(f"  {name}: {path}")


def _legacy_generate() -> int:
    requirements = PumpRequirements(
        head_m=45.0,
        discharge_m3_h=120.0,
        rpm=2950.0,
        liquid_type="Water",
        temperature_c=25.0,
    )
    output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
    _, exported = _export(requirements, output_dir)
    _print_exports(exported)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pumpai",
        description="Generate validated pump geometry and run an OpenFOAM MRF screening case.",
    )
    commands = parser.add_subparsers(dest="command")
    export = commands.add_parser("export", help="Size and export a pump geometry package.")
    export.add_argument("requirements", help="YAML or JSON pump requirements file.")
    export.add_argument("--output", help="Output directory; defaults to output/<file-stem>.")

    evaluate = commands.add_parser(
        "evaluate", help="Export, mesh, solve, and gate an OpenFOAM screening case."
    )
    evaluate.add_argument("requirements", help="YAML or JSON pump requirements file.")
    evaluate.add_argument("--output", help="Output directory; defaults to output/<file-stem>.")
    evaluate.add_argument(
        "--backend", choices=("auto", "local", "wsl"), default="auto"
    )
    evaluate.add_argument("--wsl-distribution", help="Optional WSL distribution name.")
    evaluate.add_argument(
        "--openfoam-bashrc",
        help="OpenFOAM bashrc path when the environment is not sourced automatically.",
    )
    evaluate.add_argument(
        "--timeout",
        type=float,
        default=7200.0,
        help="Maximum seconds allowed for each OpenFOAM command (default: 7200).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command is None:
        return _legacy_generate()

    try:
        requirements = load_requirements(args.requirements)
        output_dir = os.path.abspath(args.output or _default_output(args.requirements))
        design, exported = _export(requirements, output_dir)
        _print_exports(exported)
        if args.command == "export":
            return 0

        case_dir = exported.get("openfoam_case")
        if not case_dir:
            raise ValueError(
                "Evaluation requires a complete single-stage volute CFD assembly."
            )
        result = evaluate_openfoam_case(
            design,
            case_dir,
            backend=args.backend,
            wsl_distribution=args.wsl_distribution,
            openfoam_bashrc=args.openfoam_bashrc,
            timeout_s=args.timeout,
        )
        result_path = os.path.join(case_dir, "simulation_result.json")
        print(f"CFD evaluation {result['status']}: {result_path}")
        if result["status"] != "passed":
            failed = [gate for gate in result["gates"] if gate["status"] == "fail"]
            for gate in failed:
                print(f"  FAIL {gate['name']}: {gate['message']}")
            return 2
        return 0
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
        print(f"pumpai error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
