"""Smooth meridional hub/shroud definition for centrifugal pump impellers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Iterable, Sequence


PointRZ = tuple[float, float]


@dataclass(frozen=True)
class MeridionalDesign:
    """Primary flow-path curves in millimetres, represented in ``(r, z)``."""

    curve_type: str
    axial_length: float
    hub_control_points_rz: tuple[PointRZ, ...]
    shroud_control_points_rz: tuple[PointRZ, ...]
    leading_edge_hub_fraction: float
    leading_edge_shroud_fraction: float
    trailing_edge_hub_fraction: float
    trailing_edge_shroud_fraction: float
    inlet_area_mm2: float
    outlet_area_mm2: float
    area_ratio_outlet_to_inlet: float
    minimum_channel_height_mm: float
    area_uniformity_ratio: float
    minimum_hub_curvature_radius_mm: float
    minimum_shroud_curvature_radius_mm: float
    hub_static_moment_mm2: float
    shroud_static_moment_mm2: float
    static_moment_imbalance_percent: float
    cross_section_method: str = "Paired hub/shroud normal-section approximation"
    sample_fractions: tuple[float, ...] = ()
    sample_areas_mm2: tuple[float, ...] = ()
    area_local_extrema_count: int = 0
    maximum_adjacent_area_change_percent: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class MeridionalOverride:
    """User-edited primary flow-path control points and blade-edge locations."""

    hub_control_points_rz: tuple[PointRZ, ...]
    shroud_control_points_rz: tuple[PointRZ, ...]
    leading_edge_hub_fraction: float
    leading_edge_shroud_fraction: float

    @classmethod
    def from_design(cls, design: MeridionalDesign) -> "MeridionalOverride":
        return cls(
            hub_control_points_rz=design.hub_control_points_rz,
            shroud_control_points_rz=design.shroud_control_points_rz,
            leading_edge_hub_fraction=design.leading_edge_hub_fraction,
            leading_edge_shroud_fraction=design.leading_edge_shroud_fraction,
        )


def bezier_point(control_points: Sequence[PointRZ], fraction: float) -> PointRZ:
    """Evaluate a Bezier curve of arbitrary degree using de Casteljau's method."""

    if len(control_points) < 2:
        raise ValueError("A meridional curve requires at least two control points.")
    t = min(1.0, max(0.0, float(fraction)))
    points = [[float(radius), float(axial)] for radius, axial in control_points]
    while len(points) > 1:
        points = [
            [
                (1.0 - t) * start[0] + t * end[0],
                (1.0 - t) * start[1] + t * end[1],
            ]
            for start, end in zip(points, points[1:])
        ]
    return points[0][0], points[0][1]


def bezier_tangent(control_points: Sequence[PointRZ], fraction: float) -> PointRZ:
    """Return the exact first derivative of a Bezier curve in ``(r, z)``."""
    if len(control_points) < 2:
        raise ValueError("A meridional curve requires at least two control points.")
    degree = len(control_points) - 1
    derivative_points = tuple(
        (
            degree * (end[0] - start[0]),
            degree * (end[1] - start[1]),
        )
        for start, end in zip(control_points, control_points[1:])
    )
    if len(derivative_points) == 1:
        return derivative_points[0]
    return bezier_point(derivative_points, fraction)


def bezier_second_derivative(
    control_points: Sequence[PointRZ], fraction: float
) -> PointRZ:
    """Return the exact second derivative of a Bezier curve in ``(r, z)``."""
    if len(control_points) < 3:
        return 0.0, 0.0
    degree = len(control_points) - 1
    derivative_points = tuple(
        (
            degree
            * (degree - 1)
            * (end[0] - 2.0 * middle[0] + start[0]),
            degree
            * (degree - 1)
            * (end[1] - 2.0 * middle[1] + start[1]),
        )
        for start, middle, end in zip(
            control_points, control_points[1:], control_points[2:]
        )
    )
    if len(derivative_points) == 1:
        return derivative_points[0]
    return bezier_point(derivative_points, fraction)


def sample_bezier(
    control_points: Sequence[PointRZ], sections: int = 48
) -> tuple[PointRZ, ...]:
    if sections < 2:
        raise ValueError("At least two meridional samples are required.")
    return tuple(
        bezier_point(control_points, index / (sections - 1.0))
        for index in range(sections)
    )


def axial_at_radius(control_points: Sequence[PointRZ], radius: float) -> float:
    """Invert a monotonic-radius Bezier curve and return its axial coordinate."""

    start_radius = control_points[0][0]
    end_radius = control_points[-1][0]
    boundary_tolerance = 8.0 * max(
        math.ulp(start_radius),
        math.ulp(end_radius),
    )
    if (
        not math.isfinite(radius)
        or radius < start_radius - boundary_tolerance
        or radius > end_radius + boundary_tolerance
    ):
        raise ValueError(
            f"Radius {radius:g} mm is outside curve range "
            f"{start_radius:g}..{end_radius:g} mm."
        )
    radius = min(end_radius, max(start_radius, radius))
    if radius == start_radius:
        return control_points[0][1]
    if radius == end_radius:
        return control_points[-1][1]
    low = 0.0
    high = 1.0
    for _ in range(64):
        mid = 0.5 * (low + high)
        current_radius, _ = bezier_point(control_points, mid)
        if current_radius < radius:
            low = mid
        else:
            high = mid
    return bezier_point(control_points, 0.5 * (low + high))[1]


def _validate_monotonic_radius(points: Iterable[PointRZ], label: str) -> None:
    radii = [point[0] for point in points]
    if any(next_radius <= radius for radius, next_radius in zip(radii, radii[1:])):
        raise ValueError(f"{label} radius must increase monotonically.")


def _curve_static_moment(control_points: Sequence[PointRZ]) -> float:
    samples = sample_bezier(control_points, 161)
    return sum(
        0.5 * (start[0] + end[0]) * math.hypot(
            end[0] - start[0], end[1] - start[1]
        )
        for start, end in zip(samples, samples[1:])
    )


def _minimum_curvature_radius(control_points: Sequence[PointRZ]) -> float:
    radii = []
    for index in range(1, 160):
        fraction = index / 160.0
        tangent_r, tangent_z = bezier_tangent(control_points, fraction)
        second_r, second_z = bezier_second_derivative(control_points, fraction)
        speed_squared = tangent_r * tangent_r + tangent_z * tangent_z
        curvature = abs(tangent_r * second_z - tangent_z * second_r) / max(
            1.0e-12, speed_squared**1.5
        )
        if curvature > 1.0e-9:
            radii.append(1.0 / curvature)
    return min(radii) if radii else math.inf


def _cross_section_metrics(
    hub_points: Sequence[PointRZ],
    shroud_points: Sequence[PointRZ],
    *,
    sections: int = 41,
) -> tuple[tuple[float, ...], tuple[float, ...], int, float]:
    """Return flow-normal area evidence along paired contour parameters.

    Connecting equal Bezier parameters provides a stable approximation of the
    local quasi-orthogonal section.  It reproduces the exact annular inlet area
    and radial outlet area, unlike the former constant-radius vertical slice.
    """

    fractions = tuple(index / (sections - 1.0) for index in range(sections))
    areas = []
    for fraction in fractions:
        hub = bezier_point(hub_points, fraction)
        shroud = bezier_point(shroud_points, fraction)
        section_length = math.dist(hub, shroud)
        area = math.pi * (hub[0] + shroud[0]) * section_length
        if area <= 0.0:
            raise ValueError("Meridional cross-section area must remain positive.")
        areas.append(area)
    extrema = sum(
        1
        for start, middle, end in zip(areas, areas[1:], areas[2:])
        if (middle - start) * (end - middle) < -1.0e-8
    )
    maximum_change = max(
        100.0 * abs(end - start) / max(start, 1.0e-12)
        for start, end in zip(areas, areas[1:])
    )
    return (
        fractions,
        tuple(areas),
        extrema,
        maximum_change,
    )


def _validate_control_points(
    points: Sequence[PointRZ], expected: Sequence[PointRZ], label: str
) -> tuple[PointRZ, ...]:
    if len(points) != 5:
        raise ValueError(f"{label} must contain five fourth-order Bezier points.")
    normalized = tuple((float(radius), float(axial)) for radius, axial in points)
    if not all(math.isfinite(value) for point in normalized for value in point):
        raise ValueError(f"{label} contains a non-finite coordinate.")
    for edited, locked, endpoint in (
        (normalized[0], expected[0], 0),
        (normalized[-1], expected[-1], 4),
    ):
        if not (
            math.isclose(edited[0], locked[0], abs_tol=1.0e-6)
            and math.isclose(edited[1], locked[1], abs_tol=1.0e-6)
        ):
            raise ValueError(f"{label} endpoint P{endpoint} is hydraulically locked.")
    if not math.isclose(normalized[1][0], normalized[0][0], abs_tol=1.0e-6):
        raise ValueError(f"{label} P1 must preserve the axial inlet tangent.")
    if not math.isclose(normalized[3][1], normalized[4][1], abs_tol=1.0e-6):
        raise ValueError(f"{label} P3 must preserve the radial outlet tangent.")
    samples = sample_bezier(normalized, 120)
    _validate_monotonic_radius(samples, label)
    return normalized


def create_edited_meridional_design(
    automatic: MeridionalDesign,
    override: MeridionalOverride,
) -> MeridionalDesign:
    """Validate an edited primary flow path against its locked main dimensions."""
    hub_points = _validate_control_points(
        override.hub_control_points_rz,
        automatic.hub_control_points_rz,
        "Hub contour",
    )
    shroud_points = _validate_control_points(
        override.shroud_control_points_rz,
        automatic.shroud_control_points_rz,
        "Shroud contour",
    )
    for label, value in (
        ("hub", override.leading_edge_hub_fraction),
        ("shroud", override.leading_edge_shroud_fraction),
    ):
        if not math.isfinite(value) or not 0.02 <= value <= 0.80:
            raise ValueError(
                f"Leading-edge {label} fraction must be between 0.02 and 0.80."
            )

    inlet_radius = shroud_points[0][0]
    outlet_radius = shroud_points[-1][0]
    passage_areas = []
    minimum_height = math.inf
    for index in range(81):
        radius = inlet_radius + index / 80.0 * (outlet_radius - inlet_radius)
        height = axial_at_radius(shroud_points, radius) - axial_at_radius(
            hub_points, radius
        )
        if height <= 0.0:
            raise ValueError(
                f"Hub and shroud cross near radius {radius:.2f} mm."
            )
        minimum_height = min(minimum_height, height)
        passage_areas.append(2.0 * math.pi * radius * height)

    fractions, normal_areas, extrema, maximum_change = _cross_section_metrics(
        hub_points, shroud_points
    )
    area_uniformity = max(normal_areas) / min(normal_areas)
    hub_static_moment = _curve_static_moment(hub_points)
    shroud_static_moment = _curve_static_moment(shroud_points)
    static_reference = 0.5 * (hub_static_moment + shroud_static_moment)
    static_imbalance = (
        100.0 * abs(hub_static_moment - shroud_static_moment) / static_reference
    )

    return MeridionalDesign(
        curve_type="Bezier order 4 (user edited)",
        axial_length=automatic.axial_length,
        hub_control_points_rz=tuple(
            (round(radius, 4), round(axial, 4)) for radius, axial in hub_points
        ),
        shroud_control_points_rz=tuple(
            (round(radius, 4), round(axial, 4)) for radius, axial in shroud_points
        ),
        leading_edge_hub_fraction=round(
            float(override.leading_edge_hub_fraction), 4
        ),
        leading_edge_shroud_fraction=round(
            float(override.leading_edge_shroud_fraction), 4
        ),
        trailing_edge_hub_fraction=1.0,
        trailing_edge_shroud_fraction=1.0,
        inlet_area_mm2=automatic.inlet_area_mm2,
        outlet_area_mm2=automatic.outlet_area_mm2,
        area_ratio_outlet_to_inlet=automatic.area_ratio_outlet_to_inlet,
        minimum_channel_height_mm=round(minimum_height, 3),
        area_uniformity_ratio=round(area_uniformity, 4),
        minimum_hub_curvature_radius_mm=round(
            _minimum_curvature_radius(hub_points), 3
        ),
        minimum_shroud_curvature_radius_mm=round(
            _minimum_curvature_radius(shroud_points), 3
        ),
        hub_static_moment_mm2=round(hub_static_moment, 3),
        shroud_static_moment_mm2=round(shroud_static_moment, 3),
        static_moment_imbalance_percent=round(static_imbalance, 3),
        sample_fractions=tuple(round(value, 4) for value in fractions),
        sample_areas_mm2=tuple(round(value, 3) for value in normal_areas),
        area_local_extrema_count=extrema,
        maximum_adjacent_area_change_percent=round(maximum_change, 4),
    )


def create_meridional_design(
    *,
    suction_diameter_ds: float,
    hub_diameter_dh: float,
    outlet_diameter_d2: float,
    inlet_width_b1: float,
    outlet_width_b2: float,
    specific_speed_nq: float,
) -> MeridionalDesign:
    """Create smooth fourth-order Bezier hub and shroud flow boundaries.

    The parameterization follows the CFturbo meridional-contour guidance:
    vertical inlet tangency, horizontal outlet tangency, low terminal curvature,
    and a gradual axial-to-radial turn.  It is an explicit preliminary geometry,
    not a replacement for meridional potential-flow or CFD validation.
    """

    r_hub = 0.5 * hub_diameter_dh
    r_eye = 0.5 * suction_diameter_ds
    r_outlet = 0.5 * outlet_diameter_d2
    if not 0.0 < r_hub < r_eye < r_outlet:
        raise ValueError("Meridional radii must satisfy 0 < hub < eye < outlet.")

    axial_length = max(1.55 * inlet_width_b1, 0.25 * outlet_diameter_d2)
    hub_turn_radius = r_hub + 0.43 * (r_outlet - r_hub)
    shroud_turn_radius = r_eye + 0.38 * (r_outlet - r_eye)

    hub_points: tuple[PointRZ, ...] = (
        (r_hub, axial_length),
        (r_hub, 0.78 * axial_length),
        (hub_turn_radius, 0.47 * axial_length),
        (r_outlet - 0.22 * (r_outlet - hub_turn_radius), 0.0),
        (r_outlet, 0.0),
    )
    shroud_points: tuple[PointRZ, ...] = (
        (r_eye, axial_length),
        (r_eye, axial_length - 0.22 * (axial_length - outlet_width_b2)),
        (
            shroud_turn_radius,
            outlet_width_b2 + 0.48 * (axial_length - outlet_width_b2),
        ),
        (
            r_outlet - 0.24 * (r_outlet - shroud_turn_radius),
            outlet_width_b2,
        ),
        (r_outlet, outlet_width_b2),
    )

    hub_samples = sample_bezier(hub_points, 80)
    shroud_samples = sample_bezier(shroud_points, 80)
    _validate_monotonic_radius(hub_samples, "Hub curve")
    _validate_monotonic_radius(shroud_samples, "Shroud curve")

    minimum_height = math.inf
    passage_areas = []
    for index in range(41):
        radius = r_eye + index / 40.0 * (r_outlet - r_eye)
        height = axial_at_radius(shroud_points, radius) - axial_at_radius(
            hub_points, radius
        )
        minimum_height = min(minimum_height, height)
        passage_areas.append(2.0 * math.pi * radius * height)
    if minimum_height <= 0.0:
        raise ValueError("Hub and shroud meridional curves cross.")

    inlet_area = math.pi / 4.0 * (
        suction_diameter_ds**2 - hub_diameter_dh**2
    )
    outlet_area = math.pi * outlet_diameter_d2 * outlet_width_b2
    area_ratio = outlet_area / inlet_area
    fractions, normal_areas, extrema, maximum_change = _cross_section_metrics(
        hub_points, shroud_points
    )

    # Higher-nq designs commonly extend the shroud-side leading edge farther
    # into the suction region. Fractions are locations on each Bezier curve.
    nq_fraction = min(1.0, max(0.0, (specific_speed_nq - 20.0) / 80.0))
    le_hub = 0.33 - 0.08 * nq_fraction
    le_shroud = 0.12 - 0.04 * nq_fraction
    hub_static_moment = _curve_static_moment(hub_points)
    shroud_static_moment = _curve_static_moment(shroud_points)
    static_reference = 0.5 * (hub_static_moment + shroud_static_moment)

    return MeridionalDesign(
        curve_type="Bezier order 4",
        axial_length=round(axial_length, 3),
        hub_control_points_rz=tuple(
            (round(radius, 4), round(axial, 4)) for radius, axial in hub_points
        ),
        shroud_control_points_rz=tuple(
            (round(radius, 4), round(axial, 4)) for radius, axial in shroud_points
        ),
        leading_edge_hub_fraction=round(le_hub, 4),
        leading_edge_shroud_fraction=round(le_shroud, 4),
        trailing_edge_hub_fraction=1.0,
        trailing_edge_shroud_fraction=1.0,
        inlet_area_mm2=round(inlet_area, 3),
        outlet_area_mm2=round(outlet_area, 3),
        area_ratio_outlet_to_inlet=round(area_ratio, 4),
        minimum_channel_height_mm=round(minimum_height, 3),
        area_uniformity_ratio=round(max(normal_areas) / min(normal_areas), 4),
        minimum_hub_curvature_radius_mm=round(
            _minimum_curvature_radius(hub_points), 3
        ),
        minimum_shroud_curvature_radius_mm=round(
            _minimum_curvature_radius(shroud_points), 3
        ),
        hub_static_moment_mm2=round(hub_static_moment, 3),
        shroud_static_moment_mm2=round(shroud_static_moment, 3),
        static_moment_imbalance_percent=round(
            100.0
            * abs(hub_static_moment - shroud_static_moment)
            / static_reference,
            3,
        ),
        sample_fractions=tuple(round(value, 4) for value in fractions),
        sample_areas_mm2=tuple(round(value, 3) for value in normal_areas),
        area_local_extrema_count=extrema,
        maximum_adjacent_area_change_percent=round(maximum_change, 4),
    )
