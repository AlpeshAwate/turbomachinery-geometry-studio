"""Traceable preliminary sizing for centrifugal pump impellers and diffusers.

The numerical model follows the design dependency chain documented in the
project-local CFturbo manual: operating point -> main dimensions -> velocity
triangles -> incidence/blockage/slip -> meridional passage -> stationary part.
It is a preliminary engineering model and does not replace CFD or test evidence.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import math
from core.blade_geometry import (
    BladeHydraulicMetrics,
    BladePassageMetrics,
    BladeThicknessProfile,
    blade_thickness_factor,
    create_blade_surface_grid,
    evaluate_blade_hydraulics,
    evaluate_blade_passage,
    thickness_factor_at,
)
from core.engineering_model import (
    EngineeringCheck,
    EngineeringRecord,
    create_engineering_record,
)
from core.fluids import FluidState, get_fluid_properties
from core.meridional import (
    MeridionalDesign,
    MeridionalOverride,
    bezier_point,
    create_edited_meridional_design,
    create_meridional_design,
)


G = 9.80665  # m/s^2
MIN_SUPPORTED_NQ = 5.0
MAX_SUPPORTED_NQ = 100.0
MAX_STAGE_COUNT = 5
BLADE_SPAN_POSITIONS = (0.0, 0.25, 0.5, 0.75, 1.0)

DESIGN_MODE_AUTOMATIC = "Automatic CFturbo"
DESIGN_MODE_REFERENCE = "Reference reconstruction"
SUPPORTED_DESIGN_MODES = {DESIGN_MODE_AUTOMATIC, DESIGN_MODE_REFERENCE}

SLIP_GULICH_WIESNER = "Gulich/Wiesner"
SLIP_AUNGIER_WIESNER = "Aungier/Wiesner"
SLIP_PFLEIDERER = "Pfleiderer"
SLIP_VON_BACKSTROEM = "Von Backstroem"
SLIP_USER_DEFINED = "User-defined"
SUPPORTED_SLIP_MODELS = {
    SLIP_GULICH_WIESNER,
    SLIP_AUNGIER_WIESNER,
    SLIP_PFLEIDERER,
    SLIP_VON_BACKSTROEM,
    SLIP_USER_DEFINED,
}

FLOW_TYPE_AUTO = "Auto"
FLOW_TYPE_RADIAL = "Radial"
FLOW_TYPE_MIXED = "Mixed-flow"
SUPPORTED_FLOW_TYPES = {FLOW_TYPE_AUTO, FLOW_TYPE_RADIAL, FLOW_TYPE_MIXED}

COLLECTOR_VOLUTE = "Volute"
COLLECTOR_VANED_DIFFUSER = "Vaned radial diffuser"
COLLECTOR_VANELESS_DIFFUSER = "Vaneless diffuser"
SUPPORTED_SINGLE_STAGE_COLLECTORS = {
    COLLECTOR_VOLUTE,
    COLLECTOR_VANED_DIFFUSER,
    COLLECTOR_VANELESS_DIFFUSER,
}

RETURN_RADIAL = "Radial return channel"
RETURN_BOWL = "Bowl stator"
RETURN_FREE_FORM = "Free-form stator"
SUPPORTED_INTERSTAGE_RETURNS = {RETURN_RADIAL, RETURN_BOWL, RETURN_FREE_FORM}

FINAL_VOLUTE = "Volute"
FINAL_DISCHARGE_CASING = "Discharge casing"
SUPPORTED_FINAL_COLLECTORS = {FINAL_VOLUTE, FINAL_DISCHARGE_CASING}


class DesignValidationError(ValueError):
    """Raised when inputs or resulting geometry are outside this model's scope."""


@dataclass(frozen=True)
class ReferenceImpellerGeometry:
    """Published or measured geometry that must not be silently optimized."""

    suction_diameter_ds: float
    outlet_diameter_d2: float
    outlet_width_b2: float
    blade_inlet_angle_beta1: float
    blade_outlet_angle_beta2: float
    blade_count_z: int
    blade_thickness: float
    hub_diameter_dh: float | None = None
    inlet_width_b1: float | None = None
    source: str = "User supplied reference"


@dataclass(frozen=True)
class ImpellerMaterialDesign:
    """Material contours kept separate from the hydraulic flow boundaries."""

    back_shroud_thickness_mm: float
    front_shroud_thickness_mm: float
    eye_collar_enabled: bool
    eye_collar_axial_length_mm: float
    eye_collar_radial_thickness_mm: float
    hub_fillet_radius_mm: float
    shroud_fillet_radius_mm: float
    contour_definition: str = "Offset from hydraulic boundaries"


@dataclass
class PumpRequirements:
    head_m: float
    discharge_m3_h: float
    rpm: float
    liquid_type: str
    temperature_c: float
    impeller_configuration: str = "Closed"
    stage_count: int = 1
    stage_head_fractions: tuple[float, ...] | None = None
    impeller_flow_type: str = FLOW_TYPE_AUTO
    single_stage_collector: str = COLLECTOR_VOLUTE
    interstage_return_type: str = RETURN_RADIAL
    final_collector: str = FINAL_VOLUTE
    eye_collar_enabled: bool = True
    eye_collar_length_mm: float | None = None
    meridional_override: MeridionalOverride | None = None
    design_mode: str = DESIGN_MODE_AUTOMATIC
    reference_impeller: ReferenceImpellerGeometry | None = None
    slip_model: str = SLIP_GULICH_WIESNER
    user_slip_factor: float | None = None
    blade_count_override: int | None = None
    blade_thickness_profile: BladeThicknessProfile | None = None
    spanwise_inlet_angles_override: tuple[float, ...] | None = None
    spanwise_outlet_angles_override: tuple[float, ...] | None = None
    blade_stacking_fraction: float = 0.0
    blade_loading_bias: float = 0.0


@dataclass
class ImpellerDesign:
    # Main dimensions in millimetres.
    suction_diameter_ds: float
    hub_diameter_dh: float
    inlet_diameter_d1m: float
    inlet_width_b1: float
    outlet_diameter_d2: float
    outlet_width_b2: float
    blade_thickness: float
    configuration: str
    back_shroud_thickness: float
    front_shroud_thickness: float
    eye_collar_enabled: bool
    eye_collar_axial_length: float
    eye_collar_radial_thickness: float

    # Free-form blade mean-surface definition. Span 0 is hub; span 1 is shroud.
    blade_span_positions: tuple[float, ...]
    blade_inlet_angles_spanwise: tuple[float, ...]
    blade_outlet_angles_spanwise: tuple[float, ...]
    blade_stacking_fraction: float
    blade_angle_distribution: str
    blade_leading_edge_shape: str
    blade_trailing_edge_shape: str
    blade_leading_edge_radius: float
    blade_trailing_edge_radius: float
    blade_edge_axis_ratio: float

    # Angles in degrees.
    blade_inlet_angle_beta1: float
    blade_outlet_angle_beta2: float
    flow_inlet_angle_beta1f: float
    flow_outlet_angle_beta2f: float
    flow_outlet_angle_alpha2: float
    incidence_angle_i1: float
    deviation_angle_delta2: float

    # Blade count, slip, and open-passage fractions.
    blade_count_z: int
    slip_factor_sigma: float
    inlet_blockage_factor: float
    outlet_blockage_factor: float

    # Traceable CFturbo main-dimension parameters.
    intake_coefficient_epsilon: float
    outlet_width_ratio_b2_d2: float
    meridional_deceleration_ratio: float

    # Velocities in m/s.
    u1: float
    u2: float
    c1m: float
    c2m: float
    c2u: float
    w1: float
    w2: float
    slip_model: str = SLIP_GULICH_WIESNER
    design_mode: str = DESIGN_MODE_AUTOMATIC
    blade_loading_bias: float = 0.0
    blade_thickness_profile: BladeThicknessProfile = BladeThicknessProfile()
    material_design: ImpellerMaterialDesign | None = None


@dataclass
class DiffuserDesign:
    inlet_diameter_d3: float
    outlet_diameter_d4: float
    inlet_width_b3: float
    vane_inlet_angle_beta3: float
    vane_outlet_angle_beta4: float
    vane_count_zd: int
    vane_thickness: float
    area_ratio_a4_a3: float
    length_to_throat_ratio: float
    throat_aspect_ratio: float


@dataclass(frozen=True)
class VoluteDesign:
    """Preliminary single-volute collector sized from the internal pump flow.

    The angular stations describe the hydraulic fluid passage, not casing
    material.  Diameters and lengths are millimetres; velocities are m/s.
    """

    volute_type: str
    design_rule: str
    cross_section_type: str
    discharge_diffuser_type: str
    internal_flow_rate_m3_s: float
    inlet_diameter_d4: float
    inlet_width_b4: float
    inlet_width_ratio_b4_b2: float
    inlet_meridional_velocity_cm4: float
    inlet_tangential_velocity_cu4: float
    inlet_flow_angle_alpha4: float
    wrap_angle_deg: float
    blind_flow_fraction: float
    cutwater_compensation_start_deg: float
    cutwater_thickness_mm: float
    cutwater_clearance_mm: float
    wall_thickness_mm: float
    station_angles_deg: tuple[float, ...]
    station_inner_radii_mm: tuple[float, ...]
    station_outer_radii_mm: tuple[float, ...]
    station_areas_mm2: tuple[float, ...]
    discharge_inlet_area_mm2: float
    discharge_outlet_area_mm2: float
    discharge_area_ratio: float
    discharge_velocity_ratio: float
    discharge_length_mm: float
    discharge_outlet_width_mm: float
    discharge_outlet_height_mm: float
    discharge_cone_angle_deg: float
    discharge_max_cone_angle_deg: float
    outlet_extension_length_mm: float


@dataclass
class PumpPerformance:
    specific_speed_nq: float
    specific_speed_ns: float
    hydraulic_efficiency: float
    volumetric_efficiency: float
    mechanical_efficiency: float
    total_efficiency: float
    shaft_power_kw: float
    hydraulic_power_kw: float
    npsh_required_m: float
    vapor_pressure_head_m: float


@dataclass(frozen=True)
class PumpStageDesign:
    """Hydraulic and geometry result for one impeller in a series pump."""

    index: int
    energy_fraction: float
    head_m: float
    rpm: float
    resolved_flow_type: str
    work_coefficient_psi: float
    performance: PumpPerformance
    impeller: ImpellerDesign
    diffuser: DiffuserDesign
    volute: VoluteDesign
    meridional: MeridionalDesign
    blade_passage: BladePassageMetrics
    blade_hydraulics: BladeHydraulicMetrics | None = None


@dataclass(frozen=True)
class PumpArchitecture:
    """Traceable component plan derived from the selected pump arrangement."""

    stage_count: int
    machine_configuration: str
    stage_head_fractions: tuple[float, ...]
    requested_flow_type: str
    resolved_flow_types: tuple[str, ...]
    single_stage_collector: str
    interstage_return_type: str
    final_collector: str
    component_sequence: tuple[str, ...]
    cad_scope: str

    @property
    def is_multistage(self) -> bool:
        return self.stage_count > 1

    @property
    def has_supported_stationary_cad(self) -> bool:
        return self.stage_count == 1 and self.single_stage_collector in {
            COLLECTOR_VOLUTE,
            COLLECTOR_VANED_DIFFUSER,
        }

    @property
    def has_complete_assembly_cad(self) -> bool:
        # Only the single-stage volute path currently continues from the rotor
        # through the stationary collector to a defined discharge outlet.  The
        # vaned stator still needs its downstream discharge collector.
        return (
            self.stage_count == 1
            and self.single_stage_collector == COLLECTOR_VOLUTE
        )


@dataclass
class CompletePumpDesign:
    requirements: PumpRequirements
    fluid: FluidState
    performance: PumpPerformance
    impeller: ImpellerDesign
    diffuser: DiffuserDesign
    volute: VoluteDesign
    meridional: MeridionalDesign
    blade_passage: BladePassageMetrics
    engineering_record: EngineeringRecord
    architecture: PumpArchitecture | None = None
    stages: tuple[PumpStageDesign, ...] = ()
    blade_hydraulics: BladeHydraulicMetrics | None = None

    @property
    def design_id(self) -> str:
        return self.engineering_record.design_id

    @property
    def total_stage_head_m(self) -> float:
        return sum(stage.head_m for stage in self.stages) or self.requirements.head_m


def _open_pitch_fraction(
    diameter_m: float,
    thickness_m: float,
    blade_angle_deg: float,
    blade_count: int,
) -> float:
    """Tangential blockage model from blade pitch, thickness, and angle."""

    projected_blockage = (
        blade_count
        * thickness_m
        / (
            math.pi
            * diameter_m
            * max(0.08, math.sin(math.radians(blade_angle_deg)))
        )
    )
    return max(0.50, min(0.99, 1.0 - projected_blockage))


def _interaction_penalty(rotor_count: int, stator_count: int) -> int:
    """Penalize rotor/stator periodicity combinations identified by CFturbo."""

    penalty = 0
    for rotor_order in range(1, 4):
        for stator_order in range(1, 4):
            mode = abs(rotor_order * rotor_count - stator_order * stator_count)
            if mode == 0:
                penalty += 1000
            elif mode == 1 and rotor_order <= 2:
                penalty += 100
            elif mode == 1:
                penalty += 20
            elif mode == 2:
                penalty += 2
    return penalty


def _minimum_interaction_mode(rotor_count: int, stator_count: int) -> int:
    return min(
        abs(rotor_order * rotor_count - stator_order * stator_count)
        for rotor_order in range(1, 4)
        for stator_order in range(1, 4)
    )


def _range_check(
    *,
    key: str,
    category: str,
    value: float,
    unit: str,
    lower: float,
    upper: float,
    source: str,
    label: str,
    hard: bool = False,
) -> EngineeringCheck:
    inside = lower <= value <= upper
    status = "pass" if inside else ("fail" if hard else "warning")
    return EngineeringCheck(
        key=key,
        category=category,
        status=status,
        value=round(value, 6),
        unit=unit,
        lower_limit=lower,
        upper_limit=upper,
        source=source,
        message=(
            f"{label} is within the reference range."
            if inside
            else f"{label} is outside the reference range {lower:g}..{upper:g} {unit}."
        ),
    )


def _specific_speed_fraction(nq: float) -> float:
    """Map the CFturbo centrifugal recommendation (nq 5..160) to 0..1."""

    return max(0.0, min(1.0, (nq - 5.0) / 155.0))


def _intake_coefficient(nq: float) -> float:
    """Preliminary pump intake coefficient, increasing with specific speed.

    CFturbo permits 0.05..0.40.  The exponent retains a generous eye for
    low-specific-speed pumps while avoiding the former fixed 2.5 m/s minimum.
    """

    fraction = _specific_speed_fraction(nq)
    return 0.05 + 0.35 * fraction**0.85


def _outlet_width_ratio(nq: float) -> float:
    """Preliminary b2/d2 within CFturbo's 0.04..0.30 pump envelope."""

    fraction = _specific_speed_fraction(nq)
    return 0.04 + 0.26 * fraction**0.64


def _outlet_blade_angle(nq: float) -> float:
    """Initial beta2 target used by the coupled Euler/diameter solution."""

    return max(20.0, min(40.0, 21.0 + 0.40 * nq))


def _wiesner_radius_correction(
    *, inlet_diameter: float, outlet_diameter: float, beta2_deg: float, blades: int
) -> float:
    """Gulich/Wiesner radius-ratio correction from CFturbo pp. 503-505."""

    epsilon = max(0.0, min(0.999, inlet_diameter / outlet_diameter))
    epsilon_limit = math.exp(
        -8.16 * math.sin(math.radians(beta2_deg)) / max(blades, 1)
    )
    if epsilon <= epsilon_limit:
        return 1.0
    normalized = (epsilon - epsilon_limit) / max(1.0e-9, 1.0 - epsilon_limit)
    return max(0.0, 1.0 - normalized**3)


def _slip_coefficient(
    model: str,
    *,
    beta2_deg: float,
    blade_count: int,
    inlet_hub_diameter: float,
    inlet_shroud_diameter: float,
    outlet_diameter: float,
    nq: float,
    user_value: float | None = None,
) -> float:
    """Return a traceable preliminary slip coefficient."""

    if model not in SUPPORTED_SLIP_MODELS:
        raise DesignValidationError(f"Unsupported slip model '{model}'.")
    if not 3 <= blade_count <= 24:
        raise DesignValidationError("Slip calculation requires 3..24 blades.")
    beta = math.radians(beta2_deg)
    if model == SLIP_USER_DEFINED:
        if user_value is None or not math.isfinite(user_value):
            raise DesignValidationError(
                "User-defined slip model requires a finite slip factor."
            )
        return max(0.45, min(0.98, float(user_value)))

    base_deficit = math.sqrt(max(0.0, math.sin(beta))) / blade_count**0.7
    hub_correction = _wiesner_radius_correction(
        inlet_diameter=inlet_hub_diameter,
        outlet_diameter=outlet_diameter,
        beta2_deg=beta2_deg,
        blades=blade_count,
    )
    shroud_correction = _wiesner_radius_correction(
        inlet_diameter=inlet_shroud_diameter,
        outlet_diameter=outlet_diameter,
        beta2_deg=beta2_deg,
        blades=blade_count,
    )
    radius_correction = 0.5 * (hub_correction + shroud_correction)

    if model == SLIP_GULICH_WIESNER:
        flow_correction = max(
            0.98,
            1.02 + 1.2e-3 * max(0.0, nq - 50.0),
        )
        slip = 1.0 - flow_correction * radius_correction * base_deficit
    elif model == SLIP_AUNGIER_WIESNER:
        slip = 1.0 - radius_correction * base_deficit
    elif model == SLIP_PFLEIDERER:
        experience_number = 0.75
        solidity_proxy = blade_count * max(
            0.1, 1.0 - inlet_shroud_diameter / outlet_diameter
        )
        slip = 1.0 - experience_number / max(2.0, solidity_proxy * 1.6)
    else:  # Von Backstroem, CFturbo p. 507
        epsilon = max(
            0.5,
            inlet_shroud_diameter / max(outlet_diameter, 1.0e-9),
        )
        solidity = blade_count * (1.0 - epsilon) / max(
            1.0e-9, 2.0 * math.pi * math.sin(beta)
        )
        slip = 1.0 - 1.0 / max(1.0, 5.0 * solidity * math.sin(beta))
    return max(0.55, min(0.95, slip))


def _size_single_volute(
    *,
    flow_m3_s: float,
    volumetric_efficiency: float,
    specific_speed_nq: float,
    impeller_diameter_m: float,
    impeller_width_m: float,
    impeller_meridional_velocity_m_s: float,
    impeller_tangential_velocity_m_s: float,
) -> VoluteDesign:
    """Size a single volute using CFturbo's Pfleiderer x=1 rule.

    For ``cu*r = constant`` and a rectangular radial/axial section, integrating
    the tangential flux gives ``Q(theta)=b*K*ln(ro/ri)``.  This keeps the
    generated station areas tied to the internal flow instead of applying an
    arbitrary visual scale law.  A small blind-flow fraction prevents the
    tongue station from collapsing to zero area.
    """

    internal_flow = flow_m3_s / volumetric_efficiency
    inlet_diameter = 1.08 * impeller_diameter_m
    inlet_radius = 0.5 * inlet_diameter
    width_ratio = max(1.05, min(1.20, 1.05 + 0.002 * specific_speed_nq))
    inlet_width = width_ratio * impeller_width_m
    inlet_cu = impeller_tangential_velocity_m_s * (
        impeller_diameter_m / inlet_diameter
    )
    inlet_cm = internal_flow / (math.pi * inlet_diameter * inlet_width)
    inlet_alpha = math.degrees(math.atan2(inlet_cm, max(0.05, inlet_cu)))

    wrap_angle = 350.0
    blind_flow_fraction = 0.025
    compensation_start = 270.0
    tongue_thickness = max(0.003, min(0.010, 0.18 * inlet_width))
    tongue_clearance = inlet_radius - 0.5 * impeller_diameter_m
    wall_thickness = max(0.004, min(0.012, 0.18 * inlet_width))
    angular_momentum = max(1.0e-6, inlet_cu * inlet_radius)

    station_angles: list[float] = []
    station_inner_radii: list[float] = []
    station_outer_radii: list[float] = []
    station_areas: list[float] = []
    station_count = 37
    for index in range(station_count):
        fraction = index / (station_count - 1.0)
        angle = wrap_angle * fraction
        compensation_fraction = max(
            0.0,
            min(
                1.0,
                (angle - compensation_start)
                / max(1.0, wrap_angle - compensation_start),
            ),
        )
        inner_radius = inlet_radius + tongue_thickness * compensation_fraction
        local_flow = internal_flow * (
            blind_flow_fraction + (1.0 - blind_flow_fraction) * fraction
        )
        outer_radius = inner_radius * math.exp(
            local_flow / (inlet_width * angular_momentum)
        )
        station_angles.append(round(angle, 4))
        station_inner_radii.append(round(inner_radius * 1000.0, 4))
        station_outer_radii.append(round(outer_radius * 1000.0, 4))
        station_areas.append(
            round(inlet_width * (outer_radius - inner_radius) * 1.0e6, 4)
        )

    discharge_inlet_area = station_areas[-1] / 1.0e6
    discharge_velocity_ratio = 0.65
    discharge_outlet_area = discharge_inlet_area / discharge_velocity_ratio
    discharge_outlet_width = 1.15 * inlet_width
    discharge_outlet_height = discharge_outlet_area / discharge_outlet_width
    discharge_inlet_height = (
        station_outer_radii[-1] - station_inner_radii[-1]
    ) / 1000.0

    def hydraulic_diameter(width: float, height: float) -> float:
        return 2.0 * width * height / (width + height)

    inlet_hydraulic_diameter = hydraulic_diameter(
        inlet_width, discharge_inlet_height
    )
    outlet_hydraulic_diameter = hydraulic_diameter(
        discharge_outlet_width, discharge_outlet_height
    )
    target_included_angle = 8.0
    discharge_length = max(
        2.5 * inlet_hydraulic_diameter,
        (outlet_hydraulic_diameter - inlet_hydraulic_diameter)
        / (2.0 * math.tan(math.radians(0.5 * target_included_angle))),
    )
    cone_angle = math.degrees(
        2.0
        * math.atan2(
            outlet_hydraulic_diameter - inlet_hydraulic_diameter,
            2.0 * discharge_length,
        )
    )
    max_cone_angle = 16.5 * math.sqrt(
        (0.5 * inlet_hydraulic_diameter) / discharge_length
    )
    outlet_extension = 2.0 * outlet_hydraulic_diameter

    return VoluteDesign(
        volute_type="Single",
        design_rule="Pfleiderer angular momentum, cu*r^1 = constant",
        cross_section_type="Rectangular with cutwater compensation",
        discharge_diffuser_type="Tangential rectangular",
        internal_flow_rate_m3_s=round(internal_flow, 8),
        inlet_diameter_d4=round(inlet_diameter * 1000.0, 3),
        inlet_width_b4=round(inlet_width * 1000.0, 3),
        inlet_width_ratio_b4_b2=round(width_ratio, 4),
        inlet_meridional_velocity_cm4=round(inlet_cm, 4),
        inlet_tangential_velocity_cu4=round(inlet_cu, 4),
        inlet_flow_angle_alpha4=round(inlet_alpha, 4),
        wrap_angle_deg=wrap_angle,
        blind_flow_fraction=blind_flow_fraction,
        cutwater_compensation_start_deg=compensation_start,
        cutwater_thickness_mm=round(tongue_thickness * 1000.0, 3),
        cutwater_clearance_mm=round(tongue_clearance * 1000.0, 3),
        wall_thickness_mm=round(wall_thickness * 1000.0, 3),
        station_angles_deg=tuple(station_angles),
        station_inner_radii_mm=tuple(station_inner_radii),
        station_outer_radii_mm=tuple(station_outer_radii),
        station_areas_mm2=tuple(station_areas),
        discharge_inlet_area_mm2=round(discharge_inlet_area * 1.0e6, 3),
        discharge_outlet_area_mm2=round(discharge_outlet_area * 1.0e6, 3),
        discharge_area_ratio=round(
            discharge_outlet_area / discharge_inlet_area, 4
        ),
        discharge_velocity_ratio=discharge_velocity_ratio,
        discharge_length_mm=round(discharge_length * 1000.0, 3),
        discharge_outlet_width_mm=round(discharge_outlet_width * 1000.0, 3),
        discharge_outlet_height_mm=round(discharge_outlet_height * 1000.0, 3),
        discharge_cone_angle_deg=round(cone_angle, 4),
        discharge_max_cone_angle_deg=round(max_cone_angle, 4),
        outlet_extension_length_mm=round(outlet_extension * 1000.0, 3),
    )


def _size_single_stage(req: PumpRequirements) -> CompletePumpDesign:
    """Size one radial impeller for the head stored on ``req``."""

    values = (req.head_m, req.discharge_m3_h, req.rpm, req.temperature_c)
    if not all(math.isfinite(value) for value in values):
        raise DesignValidationError("All operating inputs must be finite numbers.")
    if req.head_m <= 0.0 or req.discharge_m3_h <= 0.0 or req.rpm <= 0.0:
        raise DesignValidationError("Head, discharge, and rotational speed must be positive.")
    if req.impeller_configuration not in {"Open", "Closed"}:
        raise DesignValidationError(
            "Impeller configuration must be either 'Open' or 'Closed'."
        )
    if req.design_mode not in SUPPORTED_DESIGN_MODES:
        raise DesignValidationError(f"Unsupported design mode '{req.design_mode}'.")
    if req.slip_model not in SUPPORTED_SLIP_MODELS:
        raise DesignValidationError(f"Unsupported slip model '{req.slip_model}'.")
    if req.design_mode == DESIGN_MODE_REFERENCE and req.reference_impeller is None:
        raise DesignValidationError(
            "Reference reconstruction requires published impeller dimensions."
        )
    if req.blade_count_override is not None and not 3 <= req.blade_count_override <= 7:
        raise DesignValidationError("Automatic pump blade-count override must be 3..7.")
    if not 0.0 <= req.blade_stacking_fraction <= 1.0:
        raise DesignValidationError("Blade stacking fraction must be between 0 and 1.")
    if not -0.95 <= req.blade_loading_bias <= 0.95:
        raise DesignValidationError("Blade loading bias must be between -0.95 and 0.95.")

    head = req.head_m
    flow = req.discharge_m3_h / 3600.0
    speed = req.rpm
    omega = 2.0 * math.pi * speed / 60.0

    fluid = get_fluid_properties(req.liquid_type, req.temperature_c)
    density = fluid.density

    nq = speed * math.sqrt(flow) / (head**0.75)
    ns = nq * 51.64
    if not MIN_SUPPORTED_NQ <= nq <= MAX_SUPPORTED_NQ:
        raise DesignValidationError(
            f"Specific speed Nq={nq:.1f} is outside this radial-model range "
            f"{MIN_SUPPORTED_NQ:g}..{MAX_SUPPORTED_NQ:g}. Change speed, flow, "
            "head, or use a mixed/axial-flow model."
        )

    # Preliminary efficiency estimates.
    eta_h = 1.0 - 0.071 * max(0.001, flow) ** -0.18 * (
        (max(10.0, nq) / 25.0) ** -0.1
    )
    eta_h = max(0.70, min(0.93, eta_h))
    viscosity_ratio = max(1.0, fluid.kinematic_viscosity / 1.0e-6)
    viscosity_factor = 1.0 / (1.0 + 0.06 * math.log10(viscosity_ratio) ** 2)
    eta_h = max(0.55, eta_h * viscosity_factor)
    eta_v = 1.0 / (1.0 + 0.68 * max(10.0, nq) ** -0.667)
    eta_v = max(0.85, min(0.98, eta_v))
    eta_m = 0.97
    eta_total = eta_h * eta_v * eta_m

    hydraulic_power_kw = density * G * flow * head / 1000.0
    shaft_power_kw = hydraulic_power_kw / eta_total

    # NPSHr remains an estimate until NPSHa and the suction system are supplied.
    suction_specific_speed = 210.0
    npsh_required = max(
        1.0,
        ((speed * math.sqrt(flow)) / suction_specific_speed) ** (4.0 / 3.0),
    )
    vapor_pressure_head = fluid.vapor_pressure / (density * G)

    # Eye and shaft/hub dimensions. Automatic mode searches intake coefficient,
    # b2/d2, beta2 and blade count together. Reference mode preserves published
    # dimensions and reports rather than silently repairs deviations.
    torque = shaft_power_kw * 1000.0 / omega
    allowable_shear = 25.0e6
    shaft_diameter = (
        (16.0 * torque) / (math.pi * allowable_shear)
    ) ** (1.0 / 3.0) * 1.4
    base_hub_diameter = max(0.015, shaft_diameter * 1.25)
    incidence_target = 2.5 + 0.02 * nq
    profile = req.blade_thickness_profile or BladeThicknessProfile()

    def automatic_eye(epsilon: float) -> tuple[float, float, float, float]:
        suction_velocity = epsilon * math.sqrt(2.0 * G * head)
        required_area = flow / (suction_velocity * eta_v)
        hub = base_hub_diameter
        for _ in range(4):
            suction = math.sqrt(hub**2 + 4.0 * required_area / math.pi)
            hub = max(base_hub_diameter, 0.25 * suction)
            hub = min(0.48 * suction, hub)
        suction = math.sqrt(hub**2 + 4.0 * required_area / math.pi)
        area = math.pi / 4.0 * (suction**2 - hub**2)
        mean = math.sqrt((suction**2 + hub**2) / 2.0)
        return suction, hub, area, mean

    def outlet_state(
        diameter: float,
        *,
        beta2: float,
        blades: int,
        width_ratio: float,
        suction: float,
        hub: float,
        thickness_mm: float | None = None,
    ) -> tuple[float, float, float, float, float, float, float]:
        u_tip = math.pi * diameter * speed / 60.0
        thickness = (
            max(2.0, min(6.0, 1.5 + 0.015 * diameter * 1000.0))
            if thickness_mm is None
            else thickness_mm
        )
        outlet_open = _open_pitch_fraction(
            diameter, thickness / 1000.0, beta2, blades
        )
        outlet_width = width_ratio * diameter
        c_meridional = flow / (
            math.pi * diameter * outlet_width * eta_v * outlet_open
        )
        slip_value = _slip_coefficient(
            req.slip_model,
            beta2_deg=beta2,
            blade_count=blades,
            inlet_hub_diameter=hub,
            inlet_shroud_diameter=suction,
            outlet_diameter=diameter,
            nq=nq,
            user_value=req.user_slip_factor,
        )
        ideal_whirl = u_tip - c_meridional / math.tan(math.radians(beta2))
        actual_whirl = slip_value * ideal_whirl
        delivered = eta_h * u_tip * actual_whirl / G
        return (
            u_tip,
            slip_value,
            actual_whirl,
            delivered,
            c_meridional,
            outlet_width,
            outlet_open,
        )

    def close_head(
        *, beta2: float, blades: int, width_ratio: float, suction: float, hub: float
    ) -> tuple[float, tuple[float, ...]]:
        inlet_mean = math.sqrt((suction**2 + hub**2) / 2.0)
        lower = max(inlet_mean * 1.08, 0.02)
        upper = max(lower * 1.5, 0.10)
        while outlet_state(
            upper,
            beta2=beta2,
            blades=blades,
            width_ratio=width_ratio,
            suction=suction,
            hub=hub,
        )[3] < head and upper < 4.0:
            upper *= 1.35
        if outlet_state(
            upper,
            beta2=beta2,
            blades=blades,
            width_ratio=width_ratio,
            suction=suction,
            hub=hub,
        )[3] < head:
            raise DesignValidationError(
                "Unable to close the Euler head within a practical diameter."
            )
        for _ in range(72):
            middle = 0.5 * (lower + upper)
            delivered = outlet_state(
                middle,
                beta2=beta2,
                blades=blades,
                width_ratio=width_ratio,
                suction=suction,
                hub=hub,
            )[3]
            if delivered < head:
                lower = middle
            else:
                upper = middle
        diameter = 0.5 * (lower + upper)
        return diameter, outlet_state(
            diameter,
            beta2=beta2,
            blades=blades,
            width_ratio=width_ratio,
            suction=suction,
            hub=hub,
        )

    if req.design_mode == DESIGN_MODE_REFERENCE:
        reference = req.reference_impeller
        assert reference is not None
        suction_diameter = reference.suction_diameter_ds / 1000.0
        hub_diameter = (
            reference.hub_diameter_dh / 1000.0
            if reference.hub_diameter_dh is not None
            else min(0.48 * suction_diameter, max(base_hub_diameter, 0.30 * suction_diameter))
        )
        if not 0.0 < hub_diameter < suction_diameter:
            raise DesignValidationError("Reference hub diameter must be smaller than the eye.")
        inlet_area = math.pi / 4.0 * (suction_diameter**2 - hub_diameter**2)
        inlet_mean_diameter = math.sqrt(
            (suction_diameter**2 + hub_diameter**2) / 2.0
        )
        d2 = reference.outlet_diameter_d2 / 1000.0
        b2 = reference.outlet_width_b2 / 1000.0
        beta1_blade = reference.blade_inlet_angle_beta1
        beta2_blade = reference.blade_outlet_angle_beta2
        blade_count = reference.blade_count_z
        blade_thickness_mm = reference.blade_thickness
        if not 3 <= blade_count <= 24:
            raise DesignValidationError("Reference blade count must be 3..24.")
        b2_d2 = b2 / d2
        (
            u2,
            slip,
            c2u,
            delivered_head,
            c2m,
            _outlet_width,
            outlet_open_fraction,
        ) = outlet_state(
            d2,
            beta2=beta2_blade,
            blades=blade_count,
            width_ratio=b2_d2,
            suction=suction_diameter,
            hub=hub_diameter,
            thickness_mm=blade_thickness_mm,
        )
        u1 = math.pi * inlet_mean_diameter * speed / 60.0
        inlet_open_fraction = _open_pitch_fraction(
            inlet_mean_diameter,
            thickness_factor_at(profile, span_fraction=0.5, chord_fraction=0.0)
            * blade_thickness_mm
            / 1000.0,
            beta1_blade,
            blade_count,
        )
        c1m = flow / (inlet_area * eta_v * inlet_open_fraction)
        beta1_flow = math.degrees(math.atan2(c1m, u1))
        intake_coefficient = c1m / math.sqrt(2.0 * G * head)
    else:
        target_epsilon = _intake_coefficient(nq)
        target_width_ratio = _outlet_width_ratio(nq)
        target_beta2 = _outlet_blade_angle(nq)
        epsilon_values = tuple(
            target_epsilon - index / 4.0 * (target_epsilon - 0.05)
            for index in range(5)
        )
        width_values = sorted(
            {
                0.04,
                max(0.04, target_width_ratio - 0.03),
                max(0.04, target_width_ratio - 0.015),
                target_width_ratio,
                min(0.30, target_width_ratio + 0.015),
            }
        )
        beta_values = sorted(
            {
                max(15.0, min(45.0, target_beta2 + offset))
                for offset in (-4.0, -2.0, 0.0, 2.0, 4.0)
            }
        )
        blade_values = (
            (req.blade_count_override,)
            if req.blade_count_override is not None
            else tuple(range(3, 8))
        )
        candidates = []
        for epsilon in epsilon_values:
            suction, hub, inlet_area_candidate, inlet_mean = automatic_eye(epsilon)
            u1_candidate = math.pi * inlet_mean * speed / 60.0
            for width_ratio in width_values:
                for beta2_candidate in beta_values:
                    for blades in blade_values:
                        diameter, state = close_head(
                            beta2=beta2_candidate,
                            blades=blades,
                            width_ratio=width_ratio,
                            suction=suction,
                            hub=hub,
                        )
                        thickness = max(
                            2.0, min(6.0, 1.5 + 0.015 * diameter * 1000.0)
                        )
                        c1_unblocked = flow / (inlet_area_candidate * eta_v)
                        beta1_estimate = math.degrees(
                            math.atan2(c1_unblocked, u1_candidate)
                        ) + incidence_target
                        inlet_open = _open_pitch_fraction(
                            inlet_mean,
                            thickness_factor_at(
                                profile, span_fraction=0.5, chord_fraction=0.0
                            )
                            * thickness
                            / 1000.0,
                            beta1_estimate,
                            blades,
                        )
                        c1_candidate = flow / (
                            inlet_area_candidate * eta_v * inlet_open
                        )
                        beta1_estimate = max(
                            8.0,
                            min(
                                45.0,
                                math.degrees(math.atan2(c1_candidate, u1_candidate))
                                + incidence_target,
                            ),
                        )
                        c2_candidate = state[4]
                        flow_beta2 = math.degrees(
                            math.atan2(c2_candidate, max(0.1, state[0] - state[2]))
                        )
                        deviation_candidate = beta2_candidate - flow_beta2
                        deceleration_candidate = c2_candidate / c1_candidate
                        mean_angle = math.radians(
                            0.5 * (beta1_estimate + beta2_candidate)
                        )
                        pfleiderer_count = 6.5 * (
                            (diameter + inlet_mean)
                            / max(0.01, diameter - inlet_mean)
                        ) * math.sin(mean_angle)
                        range_penalty = (
                            max(0.0, 0.60 - deceleration_candidate) * 90.0
                            + max(0.0, deceleration_candidate - 0.95) * 50.0
                            + max(0.0, deviation_candidate - 14.0) * 8.0
                        )
                        preference_penalty = (
                            3.0 * abs(width_ratio - target_width_ratio)
                            + 0.2 * abs(beta2_candidate - target_beta2)
                            + 0.25 * abs(blades - pfleiderer_count)
                            + 2.0 * abs(epsilon - target_epsilon)
                        )
                        candidates.append(
                            (
                                range_penalty + preference_penalty,
                                epsilon,
                                suction,
                                hub,
                                inlet_area_candidate,
                                inlet_mean,
                                u1_candidate,
                                c1_candidate,
                                beta1_estimate,
                                beta2_candidate,
                                blades,
                                thickness,
                                diameter,
                                state,
                                inlet_open,
                            )
                        )
        if not candidates:
            raise DesignValidationError("Coupled impeller search produced no solution.")
        (
            _score,
            intake_coefficient,
            suction_diameter,
            hub_diameter,
            inlet_area,
            inlet_mean_diameter,
            u1,
            c1m,
            beta1_blade,
            beta2_blade,
            blade_count,
            blade_thickness_mm,
            d2,
            state,
            inlet_open_fraction,
        ) = min(candidates, key=lambda candidate: candidate[0])
        (
            u2,
            slip,
            c2u,
            delivered_head,
            c2m,
            b2,
            outlet_open_fraction,
        ) = state
        beta1_flow = math.degrees(math.atan2(c1m, u1))
        b2_d2 = b2 / d2

    if req.design_mode == DESIGN_MODE_AUTOMATIC and abs(delivered_head - head) / head > 0.005:
        raise DesignValidationError("Outlet sizing did not converge on requested head.")
    if not 0.0 < b2_d2 <= 0.30:
        raise DesignValidationError("Outlet width ratio must remain positive and <= 0.30.")
    b2_d2 = b2 / d2
    if req.design_mode == DESIGN_MODE_AUTOMATIC and not 0.04 <= b2_d2 <= 0.30:
        raise DesignValidationError(
            f"Outlet width ratio b2/D2={b2_d2:.3f} is outside the CFturbo "
            "initial centrifugal-pump range 0.04..0.30."
        )
    meridional_deceleration = c2m / c1m

    # Equivalent passage width matching the annular suction area.
    b1 = (
        req.reference_impeller.inlet_width_b1 / 1000.0
        if req.design_mode == DESIGN_MODE_REFERENCE
        and req.reference_impeller is not None
        and req.reference_impeller.inlet_width_b1 is not None
        else inlet_area / (math.pi * inlet_mean_diameter)
    )
    w1 = math.hypot(c1m, u1)
    w2 = math.hypot(c2m, u2 - c2u)
    beta2_flow = math.degrees(math.atan2(c2m, max(0.1, u2 - c2u)))
    incidence = beta1_blade - beta1_flow
    deviation = beta2_blade - beta2_flow
    alpha2 = math.degrees(math.atan2(c2m, max(0.1, c2u)))
    back_shroud_thickness_mm = max(3.0, min(8.0, 0.22 * b2 * 1000.0))
    front_shroud_thickness_mm = max(2.5, min(6.0, 0.18 * b2 * 1000.0))
    if req.eye_collar_length_mm is None:
        eye_collar_length_mm = max(4.0, min(12.0, 0.25 * b1 * 1000.0))
    else:
        eye_collar_length_mm = float(req.eye_collar_length_mm)
    if not math.isfinite(eye_collar_length_mm) or not 1.0 <= eye_collar_length_mm <= 40.0:
        raise DesignValidationError("Eye-collar axial length must be between 1 and 40 mm.")
    eye_collar_enabled = bool(
        req.eye_collar_enabled and req.impeller_configuration == "Closed"
    )

    radial_work_coefficient = G * head / (0.5 * u2**2)
    if not 0.7 <= radial_work_coefficient <= 1.3:
        raise DesignValidationError(
            f"Radial work coefficient psi={radial_work_coefficient:.3f} is outside "
            "the CFturbo centrifugal range 0.7..1.3. A mixed-flow meridional "
            "model is required for this operating point."
        )
    if suction_diameter >= d2:
        raise DesignValidationError(
            "Radial work coefficient/topology gate failed because the calculated "
            "suction eye reaches the impeller outlet radius. A mixed-flow "
            "meridional model is required."
        )

    automatic_meridional = create_meridional_design(
        suction_diameter_ds=round(suction_diameter * 1000.0, 1),
        hub_diameter_dh=round(hub_diameter * 1000.0, 1),
        outlet_diameter_d2=round(d2 * 1000.0, 1),
        inlet_width_b1=round(b1 * 1000.0, 1),
        outlet_width_b2=round(b2 * 1000.0, 1),
        specific_speed_nq=round(nq, 2),
    )
    meridional = (
        create_edited_meridional_design(
            automatic_meridional,
            req.meridional_override,
        )
        if req.meridional_override is not None
        else automatic_meridional
    )

    # Resolve inlet beta at five meridional spans from the local peripheral
    # speed, then shift the distribution so the mid-span angle remains exactly
    # the angle used by the coupled 1D sizing loop. The outlet radius is common
    # to all spans in this radial model, so beta2 remains uniform there.
    hub_le = bezier_point(
        meridional.hub_control_points_rz,
        meridional.leading_edge_hub_fraction,
    )
    shroud_le = bezier_point(
        meridional.shroud_control_points_rz,
        meridional.leading_edge_shroud_fraction,
    )
    raw_inlet_angles = []
    for span in BLADE_SPAN_POSITIONS:
        leading_radius_mm = hub_le[0] + span * (shroud_le[0] - hub_le[0])
        local_u = omega * leading_radius_mm / 1000.0
        raw_inlet_angles.append(
            math.degrees(math.atan2(c1m, max(0.1, local_u))) + incidence_target
        )
    inlet_angle_shift = beta1_blade - raw_inlet_angles[2]
    # A radial impeller's hub-to-shroud inlet-angle spread grows as the eye
    # becomes more three-dimensional. Preserve the velocity-triangle mean at
    # mid-span while allowing the hub angle to rise and shroud angle to fall.
    spanwise_spread_correction = max(0.0, min(10.0, 0.36 * (nq - 10.0)))
    spanwise_beta1 = tuple(
        round(
            max(
                8.0,
                min(
                    45.0,
                    angle
                    + inlet_angle_shift
                    + spanwise_spread_correction * (0.5 - span),
                ),
            ),
            2,
        )
        for span, angle in zip(BLADE_SPAN_POSITIONS, raw_inlet_angles)
    )
    spanwise_beta2 = tuple(
        round(beta2_blade, 2) for _ in BLADE_SPAN_POSITIONS
    )
    if req.spanwise_inlet_angles_override is not None:
        if len(req.spanwise_inlet_angles_override) != len(BLADE_SPAN_POSITIONS):
            raise DesignValidationError("Inlet-angle override requires five span values.")
        spanwise_beta1 = tuple(float(value) for value in req.spanwise_inlet_angles_override)
    if req.spanwise_outlet_angles_override is not None:
        if len(req.spanwise_outlet_angles_override) != len(BLADE_SPAN_POSITIONS):
            raise DesignValidationError("Outlet-angle override requires five span values.")
        spanwise_beta2 = tuple(float(value) for value in req.spanwise_outlet_angles_override)
    if any(
        not math.isfinite(value) or not 8.0 <= value <= 60.0
        for value in spanwise_beta1 + spanwise_beta2
    ):
        raise DesignValidationError("All spanwise blade angles must lie between 8 and 60 degrees.")

    material_design = ImpellerMaterialDesign(
        back_shroud_thickness_mm=round(back_shroud_thickness_mm, 3),
        front_shroud_thickness_mm=round(front_shroud_thickness_mm, 3),
        eye_collar_enabled=eye_collar_enabled,
        eye_collar_axial_length_mm=(
            round(eye_collar_length_mm, 3) if eye_collar_enabled else 0.0
        ),
        eye_collar_radial_thickness_mm=(
            round(front_shroud_thickness_mm, 3) if eye_collar_enabled else 0.0
        ),
        hub_fillet_radius_mm=round(min(2.0, 0.18 * blade_thickness_mm), 3),
        shroud_fillet_radius_mm=round(min(1.5, 0.14 * blade_thickness_mm), 3),
    )

    # Preliminary radial vaned diffuser.
    d3 = d2 * 1.08
    b3 = b2 + 0.002
    c3u = c2u * d2 / d3
    c3m = c2m * (d2 * b2) / (d3 * b3)
    alpha3 = math.degrees(math.atan2(c3m, max(0.1, c3u)))
    beta3_vane = max(10.0, min(25.0, alpha3 + 1.2))
    d4 = d3 * 1.35
    beta4_vane = 28.0
    stator_count = min(
        range(blade_count + 1, blade_count + 9),
        key=lambda candidate: _interaction_penalty(blade_count, candidate),
    )
    diffuser_thickness_mm = max(2.5, min(8.0, 2.0 + 0.02 * d4 * 1000.0))

    throat = max(
        0.001,
        math.pi * d3 / stator_count
        - (diffuser_thickness_mm / 1000.0)
        / max(0.1, math.sin(math.radians(beta3_vane))),
    )
    outlet_gap = max(
        0.001,
        math.pi * d4 / stator_count
        - (diffuser_thickness_mm / 1000.0)
        / max(0.1, math.sin(math.radians(beta4_vane))),
    )
    diffuser_area_ratio = outlet_gap / throat
    length_to_throat = (0.5 * (d4 - d3)) / throat
    throat_aspect = b3 / throat

    performance = PumpPerformance(
        specific_speed_nq=round(nq, 2),
        specific_speed_ns=round(ns, 1),
        hydraulic_efficiency=round(eta_h * 100.0, 1),
        volumetric_efficiency=round(eta_v * 100.0, 1),
        mechanical_efficiency=round(eta_m * 100.0, 1),
        total_efficiency=round(eta_total * 100.0, 1),
        shaft_power_kw=round(shaft_power_kw, 2),
        hydraulic_power_kw=round(hydraulic_power_kw, 2),
        npsh_required_m=round(npsh_required, 2),
        vapor_pressure_head_m=round(vapor_pressure_head, 2),
    )
    impeller = ImpellerDesign(
        suction_diameter_ds=round(suction_diameter * 1000.0, 1),
        hub_diameter_dh=round(hub_diameter * 1000.0, 1),
        inlet_diameter_d1m=round(inlet_mean_diameter * 1000.0, 1),
        inlet_width_b1=round(b1 * 1000.0, 1),
        outlet_diameter_d2=round(d2 * 1000.0, 1),
        outlet_width_b2=round(b2 * 1000.0, 1),
        blade_thickness=round(blade_thickness_mm, 1),
        configuration=req.impeller_configuration,
        back_shroud_thickness=round(back_shroud_thickness_mm, 2),
        front_shroud_thickness=round(front_shroud_thickness_mm, 2),
        eye_collar_enabled=eye_collar_enabled,
        eye_collar_axial_length=(
            round(eye_collar_length_mm, 2) if eye_collar_enabled else 0.0
        ),
        eye_collar_radial_thickness=(
            round(front_shroud_thickness_mm, 2) if eye_collar_enabled else 0.0
        ),
        blade_span_positions=BLADE_SPAN_POSITIONS,
        blade_inlet_angles_spanwise=spanwise_beta1,
        blade_outlet_angles_spanwise=spanwise_beta2,
        blade_stacking_fraction=req.blade_stacking_fraction,
        blade_angle_distribution=(
            "Biased cubic smoothstep along each meridional span"
        ),
        blade_leading_edge_shape="Ellipse",
        blade_trailing_edge_shape="Ellipse",
        blade_leading_edge_radius=round(0.18 * blade_thickness_mm, 2),
        blade_trailing_edge_radius=round(0.18 * blade_thickness_mm, 2),
        blade_edge_axis_ratio=1.0,
        blade_inlet_angle_beta1=round(beta1_blade, 1),
        blade_outlet_angle_beta2=round(beta2_blade, 1),
        flow_inlet_angle_beta1f=round(beta1_flow, 1),
        flow_outlet_angle_beta2f=round(beta2_flow, 1),
        flow_outlet_angle_alpha2=round(alpha2, 1),
        incidence_angle_i1=round(incidence, 2),
        deviation_angle_delta2=round(deviation, 2),
        blade_count_z=blade_count,
        slip_factor_sigma=round(slip, 3),
        inlet_blockage_factor=round(inlet_open_fraction, 4),
        outlet_blockage_factor=round(outlet_open_fraction, 4),
        intake_coefficient_epsilon=round(intake_coefficient, 4),
        outlet_width_ratio_b2_d2=round(b2_d2, 4),
        meridional_deceleration_ratio=round(meridional_deceleration, 4),
        u1=round(u1, 2),
        u2=round(u2, 2),
        c1m=round(c1m, 2),
        c2m=round(c2m, 2),
        c2u=round(c2u, 2),
        w1=round(w1, 2),
        w2=round(w2, 2),
        slip_model=req.slip_model,
        design_mode=req.design_mode,
        blade_loading_bias=req.blade_loading_bias,
        blade_thickness_profile=profile,
        material_design=material_design,
    )
    blade_surface_grid = create_blade_surface_grid(
        meridional,
        impeller.blade_inlet_angles_spanwise,
        impeller.blade_outlet_angles_spanwise,
        span_positions=impeller.blade_span_positions,
        stacking_fraction=impeller.blade_stacking_fraction,
        loading_bias=impeller.blade_loading_bias,
        chord_sections=41,
    )
    blade_passage = evaluate_blade_passage(
        blade_surface_grid,
        maximum_thickness_mm=impeller.blade_thickness,
        blade_count=impeller.blade_count_z,
        thickness_profile=impeller.blade_thickness_profile,
    )
    blade_hydraulics = evaluate_blade_hydraulics(
        blade_surface_grid,
        blade_passage,
        blade_count=impeller.blade_count_z,
        rpm=speed,
        flow_rate_m3_s=flow / eta_v,
        kinematic_viscosity_m2_s=fluid.kinematic_viscosity,
    )
    minimum_span_chord_length = min(
        sum(math.dist(start, end) for start, end in zip(line, line[1:]))
        for line in blade_surface_grid.mean_points_xyz
    )
    maximum_edge_extent_percent = (
        100.0
        * max(
            impeller.blade_leading_edge_radius,
            impeller.blade_trailing_edge_radius,
        )
        / minimum_span_chord_length
    )
    minimum_edge_half_thickness = (
        0.5
        * impeller.blade_thickness
        * min(
            thickness_factor_at(
                profile, span_fraction=span, chord_fraction=chord
            )
            for span in (0.0, 1.0)
            for chord in (0.0, 1.0)
        )
    )
    cad_edge_radius_limit = min(
        minimum_edge_half_thickness,
        0.19 * impeller.blade_thickness,
    )
    diffuser = DiffuserDesign(
        inlet_diameter_d3=round(d3 * 1000.0, 1),
        outlet_diameter_d4=round(d4 * 1000.0, 1),
        inlet_width_b3=round(b3 * 1000.0, 1),
        vane_inlet_angle_beta3=round(beta3_vane, 1),
        vane_outlet_angle_beta4=round(beta4_vane, 1),
        vane_count_zd=stator_count,
        vane_thickness=round(diffuser_thickness_mm, 1),
        area_ratio_a4_a3=round(diffuser_area_ratio, 3),
        length_to_throat_ratio=round(length_to_throat, 3),
        throat_aspect_ratio=round(throat_aspect, 3),
    )
    volute = _size_single_volute(
        flow_m3_s=flow,
        volumetric_efficiency=eta_v,
        specific_speed_nq=nq,
        impeller_diameter_m=d2,
        impeller_width_m=b2,
        impeller_meridional_velocity_m_s=c2m,
        impeller_tangential_velocity_m_s=c2u,
    )
    head_error_percent = 100.0 * abs(delivered_head - head) / head
    minimum_mode = _minimum_interaction_mode(blade_count, stator_count)
    checks = [
        _range_check(
            key="specific_speed_recommended",
            category="main_dimensions",
            value=nq,
            unit="-",
            lower=10.0,
            upper=160.0,
            source="CFturbo_en.pdf pp. 293-294",
            label="Centrifugal/mixed-flow specific speed",
        ),
        _range_check(
            key="head_closure_error",
            category="velocity_triangles",
            value=head_error_percent,
            unit="%",
            lower=0.0,
            upper=0.5,
            source="CFturbo_en.pdf pp. 305-306, 499-508",
            label="Euler/slip head-closure error",
            hard=True,
        ),
        _range_check(
            key="outlet_width_ratio",
            category="main_dimensions",
            value=b2_d2,
            unit="-",
            lower=0.04,
            upper=0.30,
            source="CFturbo_en.pdf p. 301",
            label="Outlet width ratio b2/d2",
            hard=True,
        ),
        _range_check(
            key="intake_coefficient",
            category="main_dimensions",
            value=intake_coefficient,
            unit="-",
            lower=0.05,
            upper=0.40,
            source="CFturbo_en.pdf pp. 299-300",
            label="Pump intake coefficient epsilon",
            hard=True,
        ),
        _range_check(
            key="meridional_deceleration",
            category="main_dimensions",
            value=meridional_deceleration,
            unit="cm2/cm1",
            lower=0.60,
            upper=0.95,
            source="CFturbo_en.pdf p. 301",
            label="Impeller meridional deceleration",
        ),
        _range_check(
            key="blade_inlet_angle",
            category="blade_properties",
            value=beta1_blade,
            unit="deg",
            lower=15.0,
            upper=40.0,
            source="CFturbo_en.pdf pp. 497-498",
            label="Blade inlet angle",
        ),
        _range_check(
            key="blade_outlet_angle",
            category="blade_properties",
            value=beta2_blade,
            unit="deg",
            lower=15.0,
            upper=45.0,
            source="CFturbo_en.pdf pp. 499-502",
            label="Blade outlet angle",
        ),
        _range_check(
            key="blade_span_count",
            category="blade_geometry",
            value=len(impeller.blade_span_positions),
            unit="spans",
            lower=4.0,
            upper=11.0,
            source="CFturbo_en.pdf pp. 490-495, 608",
            label="Free-form blade span count",
            hard=True,
        ),
        EngineeringCheck(
            key="spanwise_inlet_angle_distribution",
            category="blade_geometry",
            status="pass",
            value=round(
                max(impeller.blade_inlet_angles_spanwise)
                - min(impeller.blade_inlet_angles_spanwise),
                3,
            ),
            unit="deg",
            lower_limit=None,
            upper_limit=None,
            source="CFturbo_en.pdf pp. 490-495, 520-521",
            message=(
                "Five spanwise inlet angles are derived from local peripheral "
                "speed and recorded; beta2 is uniform at the radial outlet."
            ),
        ),
        EngineeringCheck(
            key="rounded_blade_edges",
            category="blade_edges",
            status=(
                "pass"
                if 0.0 < impeller.blade_leading_edge_radius < cad_edge_radius_limit
                and 0.0 < impeller.blade_trailing_edge_radius < cad_edge_radius_limit
                else "fail"
            ),
            value=impeller.blade_leading_edge_radius,
            unit="mm radius",
            lower_limit=0.0,
            upper_limit=round(cad_edge_radius_limit, 4),
            source="CFturbo_en.pdf pp. 556-559",
            message=(
                "Leading and trailing edges use valid circular-axis-ratio-1 "
                "elliptic rounding."
                if 0.0 < impeller.blade_leading_edge_radius < cad_edge_radius_limit
                and 0.0 < impeller.blade_trailing_edge_radius < cad_edge_radius_limit
                else "Blade-edge radius exceeds the profile or robust CAD-kernel limit."
            ),
        ),
        _range_check(
            key="blade_edge_extent",
            category="blade_edges",
            value=maximum_edge_extent_percent,
            unit="% chord",
            lower=0.0,
            upper=10.0,
            source="CFturbo_en.pdf pp. 556, 559",
            label="Rounded blade-edge axial extent",
            hard=True,
        ),
        EngineeringCheck(
            key="neighbor_blade_clearance",
            category="blade_passage",
            status=(
                "pass" if blade_passage.sampled_intersection_free else "fail"
            ),
            value=blade_passage.minimum_throat_distance_mm,
            unit="mm",
            lower_limit=0.05,
            upper_limit=None,
            source="CFturbo_en.pdf pp. 555, 563",
            message=(
                "Sampled neighboring pressure/suction surfaces remain separated."
                if blade_passage.sampled_intersection_free
                else "Neighboring blade profiles intersect or fall below CAD tolerance."
            ),
        ),
        EngineeringCheck(
            key="blade_throat_area",
            category="blade_passage",
            status="pass" if blade_passage.throat_area_mm2 > 0.0 else "fail",
            value=blade_passage.throat_area_mm2,
            unit="mm2",
            lower_limit=0.0,
            upper_limit=None,
            source="CFturbo_en.pdf pp. 528, 555, 563",
            message=(
                "Sampled blade-to-blade throat area is positive and recorded."
                if blade_passage.throat_area_mm2 > 0.0
                else "Blade-to-blade throat area has collapsed."
            ),
        ),
        _range_check(
            key="blade_passage_area_progression",
            category="blade_passage",
            value=blade_passage.maximum_adjacent_area_change_percent,
            unit="%/station",
            lower=0.0,
            upper=10.0,
            source="CFturbo_en.pdf pp. 510, 528, 563",
            label="Sampled blade-passage area progression",
        ),
        _range_check(
            key="outlet_deviation",
            category="blade_properties",
            value=deviation,
            unit="deg",
            lower=0.0,
            upper=14.0,
            source="CFturbo_en.pdf pp. 500-502",
            label="Outlet deviation angle",
        ),
        _range_check(
            key="inlet_open_pitch",
            category="blockage",
            value=inlet_open_fraction,
            unit="-",
            lower=0.70,
            upper=1.0,
            source="CFturbo_en.pdf pp. 478-481, 497",
            label="Leading-edge open pitch fraction",
        ),
        _range_check(
            key="outlet_open_pitch",
            category="blockage",
            value=outlet_open_fraction,
            unit="-",
            lower=0.70,
            upper=1.0,
            source="CFturbo_en.pdf pp. 478-481, 500",
            label="Trailing-edge open pitch fraction",
        ),
        _range_check(
            key="meridional_area_ratio",
            category="meridional",
            value=meridional.area_ratio_outlet_to_inlet,
            unit="-",
            lower=0.80,
            upper=1.60,
            source="CFturbo_en.pdf pp. 444-458",
            label="Outlet-to-inlet meridional area ratio",
        ),
        _range_check(
            key="meridional_passage_uniformity",
            category="meridional",
            value=meridional.area_uniformity_ratio,
            unit="max/min",
            lower=1.0,
            upper=1.35,
            source="CFturbo_en.pdf pp. 448, 465-466",
            label="Meridional passage-area variation",
        ),
        EngineeringCheck(
            key="radial_outlet_parallelism",
            category="meridional",
            status="pass",
            value=impeller.outlet_width_b2,
            unit="mm",
            lower_limit=impeller.outlet_width_b2,
            upper_limit=impeller.outlet_width_b2,
            source="CFturbo_en.pdf pp. 448-450",
            message=(
                "Hub and shroud endpoint handles are horizontal, preserving a "
                "parallel radial outlet passage of width b2."
            ),
        ),
        EngineeringCheck(
            key="meridional_curvature_radius",
            category="meridional",
            status="pass",
            value=min(
                meridional.minimum_hub_curvature_radius_mm,
                meridional.minimum_shroud_curvature_radius_mm,
            ),
            unit="mm",
            lower_limit=None,
            upper_limit=None,
            source="CFturbo_en.pdf pp. 448, 465-466",
            message="Hub and shroud curvature radii are finite and recorded for review.",
        ),
        _range_check(
            key="diffuser_area_ratio",
            category="stator",
            value=diffuser_area_ratio,
            unit="-",
            lower=1.0,
            upper=3.0,
            source="CFturbo_en.pdf pp. 630-633",
            label="Preliminary radial-diffuser area ratio",
        ),
        _range_check(
            key="diffuser_length_to_throat",
            category="stator",
            value=length_to_throat,
            unit="-",
            lower=2.5,
            upper=6.0,
            source="CFturbo_en.pdf pp. 631-633",
            label="Preliminary radial-diffuser L34/a3",
        ),
        _range_check(
            key="diffuser_throat_aspect",
            category="stator",
            value=throat_aspect,
            unit="-",
            lower=0.8,
            upper=2.0,
            source="CFturbo_en.pdf pp. 631-633",
            label="Preliminary radial-diffuser b3/a3",
        ),
        EngineeringCheck(
            key="rotor_stator_interaction_mode",
            category="stator",
            status="pass" if minimum_mode >= 2 else "warning",
            value=minimum_mode,
            unit="-",
            lower_limit=2.0,
            upper_limit=None,
            source="CFturbo_en.pdf pp. 628-629",
            message=(
                "Low-order rotor/stator periodicity is avoided."
                if minimum_mode >= 2
                else "A low-order rotor/stator interaction mode remains."
            ),
        ),
        EngineeringCheck(
            key="npsh_evidence",
            category="cavitation",
            status="warning",
            value=round(npsh_required, 4),
            unit="m",
            lower_limit=None,
            upper_limit=None,
            source="CFturbo_en.pdf pp. 299-300, 309-310",
            message="NPSHr is estimated; NPSHa and suction-system evidence are not supplied.",
        ),
    ]
    if req.single_stage_collector == COLLECTOR_VOLUTE:
        areas_increase = all(
            end > start
            for start, end in zip(
                volute.station_areas_mm2, volute.station_areas_mm2[1:]
            )
        )
        checks.extend(
            [
                _range_check(
                    key="volute_inlet_width_ratio",
                    category="volute",
                    value=volute.inlet_width_ratio_b4_b2,
                    unit="-",
                    lower=1.05,
                    upper=1.20,
                    source="CFturbo_en.pdf pp. 641-642",
                    label="Volute inlet width ratio b4/b2",
                ),
                _range_check(
                    key="volute_inlet_flow_angle",
                    category="volute",
                    value=volute.inlet_flow_angle_alpha4,
                    unit="deg",
                    lower=0.0,
                    upper=45.0,
                    source="CFturbo_en.pdf pp. 657-658",
                    label="Volute inlet absolute-flow angle",
                ),
                _range_check(
                    key="volute_wrap_angle",
                    category="volute",
                    value=volute.wrap_angle_deg,
                    unit="deg",
                    lower=330.0,
                    upper=360.0,
                    source="CFturbo_en.pdf pp. 656, 679-680",
                    label="Volute spiral wrap angle",
                    hard=True,
                ),
                EngineeringCheck(
                    key="volute_area_progression",
                    category="volute",
                    status="pass" if areas_increase else "fail",
                    value=round(
                        volute.station_areas_mm2[-1]
                        / volute.station_areas_mm2[0],
                        4,
                    ),
                    unit="Aend/A0",
                    lower_limit=1.0,
                    upper_limit=None,
                    source="CFturbo_en.pdf pp. 658-661",
                    message=(
                        "Pfleiderer x=1 station areas increase monotonically with "
                        "the accumulated internal flow."
                        if areas_increase
                        else "Volute station area does not increase monotonically."
                    ),
                ),
                _range_check(
                    key="volute_discharge_area_ratio",
                    category="volute_diffuser",
                    value=volute.discharge_area_ratio,
                    unit="-",
                    lower=1.0,
                    upper=2.5,
                    source="CFturbo_en.pdf pp. 669-677",
                    label="Discharge-diffuser area ratio",
                ),
                _range_check(
                    key="volute_discharge_cone_angle",
                    category="volute_diffuser",
                    value=volute.discharge_cone_angle_deg,
                    unit="deg",
                    lower=0.0,
                    upper=volute.discharge_max_cone_angle_deg,
                    source="CFturbo_en.pdf p. 677",
                    label="Discharge-diffuser equivalent cone angle",
                ),
            ]
        )

    record = create_engineering_record(
        parameters={
            "requirements": {
                "head_m": float(req.head_m),
                "discharge_m3_h": float(req.discharge_m3_h),
                "rpm": float(req.rpm),
                "liquid_type": req.liquid_type,
                "temperature_c": float(req.temperature_c),
                "impeller_configuration": req.impeller_configuration,
                "eye_collar_enabled": bool(req.eye_collar_enabled),
                "eye_collar_length_mm": (
                    None
                    if req.eye_collar_length_mm is None
                    else float(req.eye_collar_length_mm)
                ),
                "meridional_override": (
                    None
                    if req.meridional_override is None
                    else asdict(req.meridional_override)
                ),
            },
            "fluid_state": asdict(fluid),
            "performance": asdict(performance),
            "impeller": asdict(impeller),
            "diffuser": asdict(diffuser),
            "volute": asdict(volute),
            "meridional": meridional.to_dict(),
            "blade_passage": blade_passage.to_dict(),
        },
        correlations={
            "specific_speed": "metric Nq = n*sqrt(Q)/H^0.75",
            "head_closure": (
                "coupled Euler work, beta2, slip, blockage, b2/d2 and diameter"
            ),
            "suction_diameter": (
                "CFturbo intake coefficient epsilon=c0/sqrt(2Y), Nq-dependent"
            ),
            "outlet_width": (
                "CFturbo b2/d2 0.04..0.30 envelope, Nq-dependent"
            ),
            "slip": "preliminary Wiesner form",
            "blockage": "tangential projection of blade thickness on pitch",
            "meridional_contour": "fourth-order Bezier primary flow path",
            "blade_mean_surface": (
                "five meridional spans; cubic beta law; leading-edge stacking"
            ),
            "blade_thickness_direction": (
                "perpendicular to mean line in each rotational span surface"
            ),
            "blade_edges": (
                "circular axis-ratio-1 elliptic rounding at leading/trailing edges"
            ),
            "blade_passage_validation": blade_passage.method,
            "npshr": "suction-specific-speed estimate, S=210 metric",
            "rotor_stator_count": "CFturbo low-order periodicity penalty",
            "volute_spiral": (
                "Pfleiderer cu*r^1=constant with angular internal-flow accumulation"
            ),
            "volute_cutwater": (
                "linear inner-radius compensation from 270 deg to spiral end"
            ),
            "volute_discharge_diffuser": (
                "tangential rectangular diffuser checked by equivalent cone angle"
            ),
        },
        checks=checks,
    )

    return CompletePumpDesign(
        requirements=req,
        fluid=fluid,
        performance=performance,
        impeller=impeller,
        diffuser=diffuser,
        volute=volute,
        meridional=meridional,
        blade_passage=blade_passage,
        engineering_record=record,
    )


def _validated_stage_head_fractions(req: PumpRequirements) -> tuple[float, ...]:
    if isinstance(req.stage_count, bool) or not isinstance(req.stage_count, int):
        raise DesignValidationError("Stage count must be an integer.")
    if not 1 <= req.stage_count <= MAX_STAGE_COUNT:
        raise DesignValidationError(
            f"Stage count must be between 1 and {MAX_STAGE_COUNT}."
        )

    if req.stage_head_fractions is None:
        equal_fraction = 1.0 / req.stage_count
        return tuple(equal_fraction for _ in range(req.stage_count))

    fractions = tuple(float(value) for value in req.stage_head_fractions)
    if len(fractions) != req.stage_count:
        raise DesignValidationError(
            "The number of stage head fractions must equal the stage count."
        )
    if not all(math.isfinite(value) and value > 0.0 for value in fractions):
        raise DesignValidationError("Every stage head fraction must be finite and positive.")
    if not math.isclose(sum(fractions), 1.0, rel_tol=0.0, abs_tol=1.0e-6):
        raise DesignValidationError("Stage head fractions must sum to 1.0.")
    return fractions


def _validate_architecture_choices(req: PumpRequirements) -> None:
    if req.impeller_flow_type not in SUPPORTED_FLOW_TYPES:
        raise DesignValidationError(
            f"Impeller flow type must be one of {sorted(SUPPORTED_FLOW_TYPES)}."
        )
    if req.single_stage_collector not in SUPPORTED_SINGLE_STAGE_COLLECTORS:
        raise DesignValidationError(
            "Unsupported single-stage collector selection."
        )
    if req.interstage_return_type not in SUPPORTED_INTERSTAGE_RETURNS:
        raise DesignValidationError("Unsupported interstage return selection.")
    if req.final_collector not in SUPPORTED_FINAL_COLLECTORS:
        raise DesignValidationError("Unsupported final collector selection.")
    if req.impeller_flow_type == FLOW_TYPE_MIXED:
        raise DesignValidationError(
            "Mixed-flow was requested, but the current CAD kernel only creates a radial "
            "meridional outlet. Radial substitution is blocked until the inclined/conical "
            "mixed-flow meridional and blade generator is implemented."
        )


def _component_sequence(
    req: PumpRequirements,
    resolved_flow_types: tuple[str, ...],
) -> tuple[str, ...]:
    sequence: list[str] = ["Inlet"]
    if req.stage_count == 1:
        sequence.append(f"Impeller 1 ({resolved_flow_types[0]})")
        sequence.append("Vaneless rotor-stator gap")
        if req.single_stage_collector == COLLECTOR_VOLUTE:
            sequence.extend(("Volute", "Cutwater", "Discharge diffuser"))
        elif req.single_stage_collector == COLLECTOR_VANED_DIFFUSER:
            sequence.extend(("Vaned radial diffuser", "Discharge collector"))
        else:
            sequence.extend(("Vaneless diffuser", "Discharge collector"))
        return tuple(sequence)

    for stage_index, flow_type in enumerate(resolved_flow_types, start=1):
        sequence.append(f"Impeller {stage_index} ({flow_type})")
        if stage_index < req.stage_count:
            sequence.append(f"{req.interstage_return_type} {stage_index}")
            sequence.append(f"Stage {stage_index + 1} inlet")
    sequence.append(f"Final {req.final_collector}")
    return tuple(sequence)


def _cad_scope(req: PumpRequirements) -> str:
    if req.stage_count > 1:
        return (
            "Reference-stage radial impeller CAD only; interstage return passages, "
            "stage placement, and final collector CAD are pending."
        )
    if req.single_stage_collector == COLLECTOR_VANED_DIFFUSER:
        return (
            "Radial impeller and preliminary vaned radial-diffuser material CAD; "
            "rotating fluid domain plus RSI connection export is supported, while "
            "the complete stationary fluid domain is pending."
        )
    if req.single_stage_collector == COLLECTOR_VOLUTE:
        return (
            "Radial impeller plus single-volute, compensated cutwater, and "
            "tangential discharge-diffuser material CAD; connected rotating and "
            "stationary fluid-domain export with a cylindrical RSI is supported."
        )
    return (
        "Radial impeller material CAD plus rotating fluid domain and RSI connection "
        "export; complete vaneless-diffuser and collector flow passages are pending."
    )


def _stage_topology_checks(stage: PumpStageDesign) -> list[EngineeringCheck]:
    hub_outlet = stage.meridional.hub_control_points_rz[-1]
    shroud_outlet = stage.meridional.shroud_control_points_rz[-1]
    outlet_radius_mismatch = abs(hub_outlet[0] - shroud_outlet[0])
    psi_inside = 0.7 <= stage.work_coefficient_psi <= 1.3
    radial_geometry = outlet_radius_mismatch <= 0.01
    return [
        EngineeringCheck(
            key=f"stage_{stage.index}.radial_work_coefficient",
            category="flow_topology",
            status="pass" if psi_inside else "fail",
            value=stage.work_coefficient_psi,
            unit="-",
            lower_limit=0.7,
            upper_limit=1.3,
            source="CFturbo_en.pdf p. 300",
            message=(
                f"Stage {stage.index} work coefficient is consistent with a centrifugal impeller."
                if psi_inside
                else f"Stage {stage.index} work coefficient is outside the centrifugal range."
            ),
        ),
        EngineeringCheck(
            key=f"stage_{stage.index}.radial_meridional_outlet",
            category="flow_topology",
            status="pass" if radial_geometry else "fail",
            value=round(outlet_radius_mismatch, 6),
            unit="mm",
            lower_limit=0.0,
            upper_limit=0.01,
            source="CFturbo_en.pdf pp. 294, 439-466",
            message=(
                f"Stage {stage.index} hub and shroud terminate on one cylindrical radius, "
                "giving a radial meridional outlet."
                if radial_geometry
                else f"Stage {stage.index} meridional outlet is not radial."
            ),
        ),
    ]


def _aggregate_performance(
    req: PumpRequirements,
    stages: tuple[PumpStageDesign, ...],
) -> PumpPerformance:
    flow = req.discharge_m3_h / 3600.0
    global_nq = req.rpm * math.sqrt(flow) / (req.head_m**0.75)
    weights = [stage.head_m / req.head_m for stage in stages]
    hydraulic_power = sum(stage.performance.hydraulic_power_kw for stage in stages)
    shaft_power = sum(stage.performance.shaft_power_kw for stage in stages)

    def weighted(attribute: str) -> float:
        return sum(
            weight * getattr(stage.performance, attribute)
            for weight, stage in zip(weights, stages)
        )

    return PumpPerformance(
        specific_speed_nq=round(global_nq, 2),
        specific_speed_ns=round(global_nq * 51.64, 1),
        hydraulic_efficiency=round(weighted("hydraulic_efficiency"), 1),
        volumetric_efficiency=round(weighted("volumetric_efficiency"), 1),
        mechanical_efficiency=round(weighted("mechanical_efficiency"), 1),
        total_efficiency=round(100.0 * hydraulic_power / shaft_power, 1),
        shaft_power_kw=round(shaft_power, 2),
        hydraulic_power_kw=round(hydraulic_power, 2),
        # In a series pump the first impeller controls suction-system NPSH.
        npsh_required_m=stages[0].performance.npsh_required_m,
        vapor_pressure_head_m=stages[0].performance.vapor_pressure_head_m,
    )


def size_pump(req: PumpRequirements) -> CompletePumpDesign:
    """Size a single- or multi-stage radial pump without topology substitution.

    CFturbo's multistage power splitting is applied as ``H_i = e_i H_global``.
    The same series flow passes through every stage. Each stage is independently
    sized and checked because its specific speed changes with its assigned head.
    """

    fractions = _validated_stage_head_fractions(req)
    _validate_architecture_choices(req)
    if req.meridional_override is not None and req.stage_count != 1:
        raise DesignValidationError(
            "The current meridional editor applies to one radial stage at a time. "
            "Clear the edit or select a single-stage pump."
        )

    stage_results: list[PumpStageDesign] = []
    stage_records: list[EngineeringRecord] = []
    for index, fraction in enumerate(fractions, start=1):
        stage_req = replace(
            req,
            head_m=req.head_m * fraction,
            stage_count=1,
            stage_head_fractions=None,
            impeller_flow_type=FLOW_TYPE_RADIAL,
        )
        stage_design = _size_single_stage(stage_req)
        work_coefficient = (
            G * stage_req.head_m / (0.5 * stage_design.impeller.u2**2)
        )
        if not 0.7 <= work_coefficient <= 1.3:
            raise DesignValidationError(
                f"Stage {index} work coefficient psi={work_coefficient:.3f} is outside "
                "the CFturbo centrifugal range 0.7..1.3. Refusing to label this "
                "radial geometry as hydraulically consistent."
            )
        stage_results.append(
            PumpStageDesign(
                index=index,
                energy_fraction=round(fraction, 8),
                head_m=round(stage_req.head_m, 6),
                rpm=float(stage_req.rpm),
                resolved_flow_type=FLOW_TYPE_RADIAL,
                work_coefficient_psi=round(work_coefficient, 4),
                performance=stage_design.performance,
                impeller=stage_design.impeller,
                diffuser=stage_design.diffuser,
                volute=stage_design.volute,
                meridional=stage_design.meridional,
                blade_passage=stage_design.blade_passage,
            )
        )
        stage_records.append(stage_design.engineering_record)

    stages = tuple(stage_results)
    resolved_types = tuple(stage.resolved_flow_type for stage in stages)
    architecture = PumpArchitecture(
        stage_count=req.stage_count,
        machine_configuration=("Multistage" if req.stage_count > 1 else "Single stage"),
        stage_head_fractions=tuple(round(value, 8) for value in fractions),
        requested_flow_type=req.impeller_flow_type,
        resolved_flow_types=resolved_types,
        single_stage_collector=req.single_stage_collector,
        interstage_return_type=req.interstage_return_type,
        final_collector=req.final_collector,
        component_sequence=_component_sequence(req, resolved_types),
        cad_scope=_cad_scope(req),
    )

    checks: list[EngineeringCheck] = []
    for stage, stage_record in zip(stages, stage_records):
        checks.extend(
            replace(
                check,
                key=f"stage_{stage.index}.{check.key}",
                message=f"Stage {stage.index}: {check.message}",
            )
            for check in stage_record.checks
        )
        checks.extend(_stage_topology_checks(stage))
    checks.append(
        EngineeringCheck(
            key="multistage_head_balance",
            category="architecture",
            status="pass",
            value=round(sum(stage.head_m for stage in stages), 6),
            unit="m",
            lower_limit=float(req.head_m),
            upper_limit=float(req.head_m),
            source="CFturbo_en.pdf pp. 109-110, 432-433",
            message="Stage heads sum to the global required head.",
        )
    )
    checks.append(
        EngineeringCheck(
            key="assembly_cad_completeness",
            category="architecture",
            status="pass" if architecture.has_complete_assembly_cad else "warning",
            value="complete" if architecture.has_complete_assembly_cad else "partial",
            unit="-",
            lower_limit=None,
            upper_limit=None,
            source="CFturbo_en.pdf pp. 45-46, 109-110, 616-697",
            message=architecture.cad_scope,
        )
    )

    failed_checks = [check for check in checks if check.status == "fail"]
    if failed_checks:
        raise DesignValidationError(failed_checks[0].message)

    global_performance = _aggregate_performance(req, stages)
    primary_stage = stages[0]
    fluid = get_fluid_properties(req.liquid_type, req.temperature_c)
    record = create_engineering_record(
        parameters={
            "requirements": {
                "head_m": float(req.head_m),
                "discharge_m3_h": float(req.discharge_m3_h),
                "rpm": float(req.rpm),
                "liquid_type": req.liquid_type,
                "temperature_c": float(req.temperature_c),
                "impeller_configuration": req.impeller_configuration,
                "stage_count": req.stage_count,
                "stage_head_fractions": (
                    None
                    if req.stage_head_fractions is None
                    else [float(value) for value in req.stage_head_fractions]
                ),
                "impeller_flow_type": req.impeller_flow_type,
                "single_stage_collector": req.single_stage_collector,
                "interstage_return_type": req.interstage_return_type,
                "final_collector": req.final_collector,
                "eye_collar_enabled": bool(req.eye_collar_enabled),
                "eye_collar_length_mm": (
                    None
                    if req.eye_collar_length_mm is None
                    else float(req.eye_collar_length_mm)
                ),
                "meridional_override": (
                    None
                    if req.meridional_override is None
                    else asdict(req.meridional_override)
                ),
            },
            "architecture": asdict(architecture),
            "system_performance": asdict(global_performance),
            "fluid_state": asdict(fluid),
            "stages": [asdict(stage) for stage in stages],
        },
        correlations={
            "stage_head_split": "CFturbo power splitting H_i = e_i*H_global",
            "series_flow": "Q_i = Q_global for every stage",
            "specific_speed": "stage metric Nq_i = n_i*sqrt(Q)/H_i^0.75",
            "work_coefficient": "psi = Y/(u2^2/2)",
            "topology_gate": "radial work coefficient plus radial meridional outlet",
            "component_coupling": "upstream outlet defines downstream inlet",
        },
        checks=checks,
    )

    return CompletePumpDesign(
        requirements=req,
        fluid=fluid,
        performance=global_performance,
        impeller=primary_stage.impeller,
        diffuser=primary_stage.diffuser,
        volute=primary_stage.volute,
        meridional=primary_stage.meridional,
        blade_passage=primary_stage.blade_passage,
        engineering_record=record,
        architecture=architecture,
        stages=stages,
    )
