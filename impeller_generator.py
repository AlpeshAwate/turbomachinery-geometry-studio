"""Compatibility command-line entry point for the validated design engine.

The previous standalone generator used geometry unrelated to the GUI and could
create an open, disconnected STL. This wrapper routes generation through the
same sizing and CAD code used by the application.
"""

import os

from core.pump_design import PumpRequirements, size_pump
from core.blade_builder import export_turbomachinery_for_openfoam


def main() -> None:
    requirements = PumpRequirements(
        head_m=45.0,
        discharge_m3_h=120.0,
        rpm=2950.0,
        liquid_type="Water",
        temperature_c=25.0,
    )
    design = size_pump(requirements)
    output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
    exported = export_turbomachinery_for_openfoam(design, output_dir)
    print("Validated geometry package generated:")
    for path in exported.values():
        print(f"  {path}")


if __name__ == "__main__":
    main()
