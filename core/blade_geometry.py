"""Free-form spanwise blade mean-surface construction.

The geometry in this module is hydraulic geometry: it describes the blade mean
surface and the direction used to place pressure/suction-side thickness.  It is
kept independent of CadQuery so the span definition can be tested before a CAD
solid is attempted.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Sequence

from core.meridional import MeridionalDesign, bezier_point


PointXYZ = tuple[float, float, float]


@dataclass(frozen=True)
class BladeSurfaceGrid:
    """Sampled blade mean surface, indexed as ``[span][chord]``."""

    span_positions: tuple[float, ...]
    chord_fractions: tuple[float, ...]
    mean_points_xyz: tuple[tuple[PointXYZ, ...], ...]
    thickness_directions_xyz: tuple[tuple[PointXYZ, ...], ...]
    wrap_angles_deg: tuple[float, ...]
    stacking_fraction: float


@dataclass(frozen=True)
class BladePassageMetrics:
    """Sampled blade-to-blade throat and passage evidence in millimetres."""

    method: str
    sample_span_count: int
    sample_chord_count: int
    minimum_throat_distance_mm: float
    minimum_throat_span_fraction: float
    minimum_throat_chord_fraction: float
    leading_edge_throat_distances_mm: tuple[float, ...]
    trailing_edge_throat_distances_mm: tuple[float, ...]
    throat_area_mm2: float
    throat_area_chord_fraction: float
    passage_area_min_mm2: float
    passage_area_max_mm2: float
    passage_area_uniformity_ratio: float
    maximum_adjacent_area_change_percent: float
    passage_areas_mm2: tuple[float, ...]
    sampled_intersection_free: bool

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class BladeThicknessProfile:
    """Editable hub/shroud thickness law used by hydraulic and CAD models.

    Factors multiply ``maximum_thickness_mm``.  CFturbo treats the hub and
    shroud profiles independently and recommends applying thickness normal to
    each span mean line; this immutable definition keeps that choice traceable.
    """

    chord_fractions: tuple[float, ...] = (0.0, 0.45, 1.0)
    hub_factors: tuple[float, ...] = (0.65, 1.0, 0.55)
    shroud_factors: tuple[float, ...] = (0.58, 0.90, 0.48)
    pressure_side_fraction: float = 0.5
    definition: str = "Perpendicular to mean line"

    def __post_init__(self) -> None:
        if len(self.chord_fractions) < 3:
            raise ValueError("A blade thickness law requires at least three stations.")
        if not (
            len(self.chord_fractions)
            == len(self.hub_factors)
            == len(self.shroud_factors)
        ):
            raise ValueError("Hub and shroud thickness laws must share chord stations.")
        if self.chord_fractions[0] != 0.0 or self.chord_fractions[-1] != 1.0:
            raise ValueError("Thickness chord stations must start at 0 and end at 1.")
        if any(
            end <= start
            for start, end in zip(self.chord_fractions, self.chord_fractions[1:])
        ):
            raise ValueError("Thickness chord stations must increase strictly.")
        if any(value <= 0.0 for value in self.hub_factors + self.shroud_factors):
            raise ValueError("Blade thickness factors must be positive.")
        if not 0.0 <= self.pressure_side_fraction <= 1.0:
            raise ValueError("Pressure-side thickness fraction must lie between 0 and 1.")


@dataclass(frozen=True)
class BladeHydraulicMetrics:
    """Pre-CFD blade loading and surface-velocity screening evidence."""

    method: str
    meanline_lengths_mm: tuple[float, ...]
    solidities: tuple[float, ...]
    overlap_angles_deg: tuple[float, ...]
    reynolds_numbers: tuple[float, ...]
    average_relative_velocities_m_s: tuple[float, ...]
    pressure_surface_velocities_m_s: tuple[float, ...]
    suction_surface_velocities_m_s: tuple[float, ...]
    velocity_loading_coefficients: tuple[float, ...]
    maximum_velocity_loading_coefficient: float
    minimum_pressure_velocity_ratio: float
    maximum_suction_velocity_ratio: float

    def to_dict(self) -> dict:
        return asdict(self)


def _length(vector: PointXYZ) -> float:
    return math.sqrt(sum(component * component for component in vector))


def _normalize(vector: PointXYZ) -> PointXYZ:
    magnitude = _length(vector)
    if magnitude <= 1.0e-10:
        raise ValueError("Blade surface contains a zero-length direction.")
    return tuple(component / magnitude for component in vector)  # type: ignore[return-value]


def _subtract(end: PointXYZ, start: PointXYZ) -> PointXYZ:
    return tuple(b - a for a, b in zip(start, end))  # type: ignore[return-value]


def _dot(first: PointXYZ, second: PointXYZ) -> float:
    return sum(a * b for a, b in zip(first, second))


def _smooth_angle(start_deg: float, end_deg: float, fraction: float) -> float:
    """Cubic angle law with zero slope at the leading and trailing edges."""

    smooth_fraction = fraction * fraction * (3.0 - 2.0 * fraction)
    return start_deg + smooth_fraction * (end_deg - start_deg)


def blade_thickness_factor(
    chord_fraction: float,
) -> float:
    """Preliminary smooth profile law with finite leading/trailing thickness."""

    if not 0.0 <= chord_fraction <= 1.0:
        raise ValueError("Chord fraction must lie between 0 and 1.")
    if chord_fraction <= 0.45:
        normalized = chord_fraction / 0.45
        return 0.65 + 0.35 * math.sin(0.5 * math.pi * normalized)
    normalized = (chord_fraction - 0.45) / 0.55
    return 0.55 + 0.45 * math.cos(0.5 * math.pi * normalized)


def _linear_profile_value(
    stations: Sequence[float], values: Sequence[float], fraction: float
) -> float:
    for start_index, (start, end) in enumerate(zip(stations, stations[1:])):
        if fraction <= end:
            local = (fraction - start) / (end - start)
            return values[start_index] + local * (
                values[start_index + 1] - values[start_index]
            )
    return values[-1]


def thickness_factor_at(
    profile: BladeThicknessProfile | None,
    *,
    span_fraction: float,
    chord_fraction: float,
) -> float:
    """Return the span-morphed thickness factor at a blade location."""

    if profile is None:
        return blade_thickness_factor(chord_fraction)
    hub = _linear_profile_value(
        profile.chord_fractions, profile.hub_factors, chord_fraction
    )
    shroud = _linear_profile_value(
        profile.chord_fractions, profile.shroud_factors, chord_fraction
    )
    return hub + span_fraction * (shroud - hub)


def blade_side_grids(
    grid: BladeSurfaceGrid,
    maximum_thickness_mm: float,
    thickness_profile: BladeThicknessProfile | None = None,
) -> tuple[tuple[tuple[PointXYZ, ...], ...], tuple[tuple[PointXYZ, ...], ...]]:
    """Return pressure/suction sample grids around a blade mean surface."""

    if maximum_thickness_mm <= 0.0:
        raise ValueError("Blade thickness must be positive.")
    pressure_grid = []
    suction_grid = []
    pressure_fraction = (
        thickness_profile.pressure_side_fraction
        if thickness_profile is not None
        else 0.5
    )
    for span_index, mean_line in enumerate(grid.mean_points_xyz):
        pressure_line = []
        suction_line = []
        for chord_index, (point, direction) in enumerate(
            zip(mean_line, grid.thickness_directions_xyz[span_index])
        ):
            full_thickness = (
                maximum_thickness_mm
                * thickness_factor_at(
                    thickness_profile,
                    span_fraction=grid.span_positions[span_index],
                    chord_fraction=grid.chord_fractions[chord_index],
                )
            )
            pressure_line.append(
                tuple(
                    coordinate + pressure_fraction * full_thickness * component
                    for coordinate, component in zip(point, direction)
                )
            )
            suction_line.append(
                tuple(
                    coordinate
                    - (1.0 - pressure_fraction) * full_thickness * component
                    for coordinate, component in zip(point, direction)
                )
            )
        pressure_grid.append(tuple(pressure_line))
        suction_grid.append(tuple(suction_line))
    return tuple(pressure_grid), tuple(suction_grid)


def _rotate_about_axis(point: PointXYZ, angle_rad: float) -> PointXYZ:
    cosine = math.cos(angle_rad)
    sine = math.sin(angle_rad)
    return (
        point[0] * cosine - point[1] * sine,
        point[0] * sine + point[1] * cosine,
        point[2],
    )


def _point_segment_distance(
    point: PointXYZ,
    segment_start: PointXYZ,
    segment_end: PointXYZ,
) -> float:
    segment = _subtract(segment_end, segment_start)
    length_squared = _dot(segment, segment)
    if length_squared <= 1.0e-16:
        return _length(_subtract(point, segment_start))
    relative = _subtract(point, segment_start)
    fraction = min(1.0, max(0.0, _dot(relative, segment) / length_squared))
    closest = tuple(
        start + fraction * direction
        for start, direction in zip(segment_start, segment)
    )
    return _length(_subtract(point, closest))


def _point_polyline_distance(point: PointXYZ, polyline: Sequence[PointXYZ]) -> float:
    return min(
        _point_segment_distance(point, start, end)
        for start, end in zip(polyline, polyline[1:])
    )


def evaluate_blade_passage(
    grid: BladeSurfaceGrid,
    *,
    maximum_thickness_mm: float,
    blade_count: int,
    thickness_profile: BladeThicknessProfile | None = None,
    clearance_tolerance_mm: float = 0.05,
) -> BladePassageMetrics:
    """Evaluate sampled throat distance and passage-area progression.

    Distances are measured from both sides of one blade to the corresponding
    side of its periodic neighbors. Passage area is the spanwise integral of
    those local normal gaps at each chord station. An exact solid-overlap test
    remains the CAD builder's final nonintersection gate.
    """

    if blade_count < 2:
        raise ValueError("At least two blades are required for passage checks.")
    if clearance_tolerance_mm <= 0.0:
        raise ValueError("Clearance tolerance must be positive.")
    pressure_grid, suction_grid = blade_side_grids(
        grid, maximum_thickness_mm, thickness_profile
    )
    pitch_angle = 2.0 * math.pi / blade_count
    gap_by_span: list[list[float]] = []

    for pressure_line, suction_line in zip(pressure_grid, suction_grid):
        positive_neighbor_suction = tuple(
            _rotate_about_axis(point, pitch_angle) for point in suction_line
        )
        negative_neighbor_pressure = tuple(
            _rotate_about_axis(point, -pitch_angle) for point in pressure_line
        )
        span_gaps = []
        for pressure_point, suction_point in zip(pressure_line, suction_line):
            span_gaps.append(
                min(
                    _point_polyline_distance(
                        pressure_point, positive_neighbor_suction
                    ),
                    _point_polyline_distance(
                        suction_point, negative_neighbor_pressure
                    ),
                )
            )
        gap_by_span.append(span_gaps)

    minimum_span_index, minimum_chord_index = min(
        (
            (span_index, chord_index)
            for span_index in range(len(grid.span_positions))
            for chord_index in range(len(grid.chord_fractions))
        ),
        key=lambda location: gap_by_span[location[0]][location[1]],
    )
    minimum_distance = gap_by_span[minimum_span_index][minimum_chord_index]
    passage_areas = []
    for chord_index in range(len(grid.chord_fractions)):
        area = 0.0
        for span_index in range(len(grid.span_positions) - 1):
            span_width = _length(
                _subtract(
                    grid.mean_points_xyz[span_index + 1][chord_index],
                    grid.mean_points_xyz[span_index][chord_index],
                )
            )
            area += (
                0.5
                * (
                    gap_by_span[span_index][chord_index]
                    + gap_by_span[span_index + 1][chord_index]
                )
                * span_width
            )
        passage_areas.append(area)

    throat_chord_index = min(
        range(len(passage_areas)), key=passage_areas.__getitem__
    )
    minimum_area = passage_areas[throat_chord_index]
    maximum_area = max(passage_areas)
    maximum_adjacent_area_change = max(
        100.0 * abs(end - start) / max(start, 1.0e-12)
        for start, end in zip(passage_areas, passage_areas[1:])
    )
    return BladePassageMetrics(
        method=(
            "Sampled periodic pressure/suction polyline clearance with "
            "spanwise normal-gap area integration"
        ),
        sample_span_count=len(grid.span_positions),
        sample_chord_count=len(grid.chord_fractions),
        minimum_throat_distance_mm=round(minimum_distance, 4),
        minimum_throat_span_fraction=round(
            grid.span_positions[minimum_span_index], 4
        ),
        minimum_throat_chord_fraction=round(
            grid.chord_fractions[minimum_chord_index], 4
        ),
        leading_edge_throat_distances_mm=tuple(
            round(gaps[0], 4) for gaps in gap_by_span
        ),
        trailing_edge_throat_distances_mm=tuple(
            round(gaps[-1], 4) for gaps in gap_by_span
        ),
        throat_area_mm2=round(minimum_area, 3),
        throat_area_chord_fraction=round(
            grid.chord_fractions[throat_chord_index], 4
        ),
        passage_area_min_mm2=round(minimum_area, 3),
        passage_area_max_mm2=round(maximum_area, 3),
        passage_area_uniformity_ratio=round(
            maximum_area / max(minimum_area, 1.0e-12), 4
        ),
        maximum_adjacent_area_change_percent=round(
            maximum_adjacent_area_change, 4
        ),
        passage_areas_mm2=tuple(round(area, 3) for area in passage_areas),
        sampled_intersection_free=minimum_distance > clearance_tolerance_mm,
    )


def create_blade_surface_grid(
    meridional: MeridionalDesign,
    inlet_angles_deg: Sequence[float],
    outlet_angles_deg: Sequence[float],
    *,
    span_positions: Sequence[float],
    stacking_fraction: float = 0.0,
    loading_bias: float = 0.0,
    chord_sections: int = 25,
) -> BladeSurfaceGrid:
    """Create spanwise mean lines on the designed meridional passage.

    Each span begins on the interpolated meridional leading edge and ends on
    the common radial outlet.  Angular position is obtained by integrating the
    blade-angle law along meridional distance, ``dtheta = dm/(r*tan(beta))``.
    Thickness directions lie in each span's rotational surface and are normal
    to its mean line, matching CFturbo's recommended stable profile method.
    """

    spans = tuple(float(value) for value in span_positions)
    beta1 = tuple(float(value) for value in inlet_angles_deg)
    beta2 = tuple(float(value) for value in outlet_angles_deg)
    if len(spans) < 4:
        raise ValueError("A free-form blade surface requires at least four spans.")
    if len(beta1) != len(spans) or len(beta2) != len(spans):
        raise ValueError("Each blade span requires inlet and outlet angles.")
    if spans[0] != 0.0 or spans[-1] != 1.0 or any(
        end <= start for start, end in zip(spans, spans[1:])
    ):
        raise ValueError("Blade spans must increase strictly from 0 to 1.")
    if chord_sections < 9:
        raise ValueError("At least nine chordwise sections are required.")
    if not 0.0 <= stacking_fraction <= 1.0:
        raise ValueError("Blade stacking fraction must lie between 0 and 1.")
    if not -0.95 <= loading_bias <= 0.95:
        raise ValueError("Blade loading bias must lie between -0.95 and 0.95.")
    if any(not 8.0 <= angle <= 60.0 for angle in beta1 + beta2):
        raise ValueError("Blade angles must lie between 8 and 60 degrees.")

    chord = tuple(index / (chord_sections - 1.0) for index in range(chord_sections))
    meridional_lines: list[list[tuple[float, float]]] = []
    angular_lines: list[list[float]] = []

    for span, inlet_angle, outlet_angle in zip(spans, beta1, beta2):
        rz_line: list[tuple[float, float]] = []
        for fraction in chord:
            hub_parameter = meridional.leading_edge_hub_fraction + fraction * (
                meridional.trailing_edge_hub_fraction
                - meridional.leading_edge_hub_fraction
            )
            shroud_parameter = meridional.leading_edge_shroud_fraction + fraction * (
                meridional.trailing_edge_shroud_fraction
                - meridional.leading_edge_shroud_fraction
            )
            hub_radius, hub_axial = bezier_point(
                meridional.hub_control_points_rz, hub_parameter
            )
            shroud_radius, shroud_axial = bezier_point(
                meridional.shroud_control_points_rz, shroud_parameter
            )
            rz_line.append(
                (
                    hub_radius + span * (shroud_radius - hub_radius),
                    hub_axial + span * (shroud_axial - hub_axial),
                )
            )

        theta_line = [0.0]
        for index in range(1, chord_sections):
            start_radius, start_axial = rz_line[index - 1]
            end_radius, end_axial = rz_line[index]
            meridional_step = math.hypot(
                end_radius - start_radius, end_axial - start_axial
            )
            middle_radius = 0.5 * (start_radius + end_radius)
            middle_fraction = 0.5 * (chord[index - 1] + chord[index])
            smooth_fraction = middle_fraction * middle_fraction * (
                3.0 - 2.0 * middle_fraction
            )
            biased_fraction = smooth_fraction + loading_bias * (
                4.0 * smooth_fraction * (1.0 - smooth_fraction)
            )
            biased_fraction = min(1.0, max(0.0, biased_fraction))
            middle_beta = math.radians(
                inlet_angle + biased_fraction * (outlet_angle - inlet_angle)
            )
            theta_line.append(
                theta_line[-1]
                + meridional_step
                / (middle_radius * max(0.05, math.tan(middle_beta)))
            )
        meridional_lines.append(rz_line)
        angular_lines.append(theta_line)

    # Stack all mean lines at one chordwise position.  Leading-edge stacking is
    # the default because it produces an unswept pump inlet while allowing the
    # span-dependent beta law to determine trailing-edge rake naturally.
    stack_index = round(stacking_fraction * (chord_sections - 1))
    reference_span = min(
        range(len(spans)), key=lambda index: abs(spans[index] - 0.5)
    )
    reference_theta = angular_lines[reference_span][stack_index]
    for theta_line in angular_lines:
        shift = reference_theta - theta_line[stack_index]
        for index in range(chord_sections):
            theta_line[index] += shift

    mean_points: list[list[PointXYZ]] = []
    for rz_line, theta_line in zip(meridional_lines, angular_lines):
        mean_points.append(
            [
                (
                    radius * math.cos(theta),
                    radius * math.sin(theta),
                    axial,
                )
                for (radius, axial), theta in zip(rz_line, theta_line)
            ]
        )

    thickness_directions: list[list[PointXYZ]] = []
    for span_index, (rz_line, theta_line, xyz_line) in enumerate(
        zip(meridional_lines, angular_lines, mean_points)
    ):
        directions: list[PointXYZ] = []
        for chord_index, point in enumerate(xyz_line):
            if chord_index == 0:
                tangent = _subtract(xyz_line[1], point)
                dr = rz_line[1][0] - rz_line[0][0]
                dz = rz_line[1][1] - rz_line[0][1]
            elif chord_index == chord_sections - 1:
                tangent = _subtract(point, xyz_line[-2])
                dr = rz_line[-1][0] - rz_line[-2][0]
                dz = rz_line[-1][1] - rz_line[-2][1]
            else:
                tangent = _subtract(
                    xyz_line[chord_index + 1], xyz_line[chord_index - 1]
                )
                dr = rz_line[chord_index + 1][0] - rz_line[chord_index - 1][0]
                dz = rz_line[chord_index + 1][1] - rz_line[chord_index - 1][1]

            theta = theta_line[chord_index]
            meridional_direction = _normalize(
                (dr * math.cos(theta), dr * math.sin(theta), dz)
            )
            circumferential_direction: PointXYZ = (
                -math.sin(theta),
                math.cos(theta),
                0.0,
            )
            tangent = _normalize(tangent)
            meridional_component = _dot(tangent, meridional_direction)
            circumferential_component = _dot(tangent, circumferential_direction)
            thickness_direction = _normalize(
                tuple(
                    -circumferential_component * meridional_axis
                    + meridional_component * circumferential_axis
                    for meridional_axis, circumferential_axis in zip(
                        meridional_direction, circumferential_direction
                    )
                )  # type: ignore[arg-type]
            )
            # A consistent circumferential sign prevents loft twisting.
            if _dot(thickness_direction, circumferential_direction) < 0.0:
                thickness_direction = tuple(
                    -component for component in thickness_direction
                )  # type: ignore[assignment]
            directions.append(thickness_direction)
        thickness_directions.append(directions)

    return BladeSurfaceGrid(
        span_positions=spans,
        chord_fractions=chord,
        mean_points_xyz=tuple(tuple(line) for line in mean_points),
        thickness_directions_xyz=tuple(
            tuple(line) for line in thickness_directions
        ),
        wrap_angles_deg=tuple(
            math.degrees(theta_line[-1] - theta_line[0])
            for theta_line in angular_lines
        ),
        stacking_fraction=float(stacking_fraction),
    )


def evaluate_blade_hydraulics(
    grid: BladeSurfaceGrid,
    passage: BladePassageMetrics,
    *,
    blade_count: int,
    rpm: float,
    flow_rate_m3_s: float,
    kinematic_viscosity_m2_s: float,
) -> BladeHydraulicMetrics:
    """Estimate spanwise loading before CFD using traceable 1D relations.

    This is deliberately a screening model, not a substitute for CFturbo's
    Stanitz/Prian surface calculation or CFD.  It supplies the missing
    mean-line length, solidity, overlap, Reynolds and surface-velocity evidence
    needed to reject obviously overloaded geometry.
    """

    if blade_count < 2 or rpm <= 0.0 or flow_rate_m3_s <= 0.0:
        raise ValueError("Blade hydraulic inputs must be positive.")
    omega = 2.0 * math.pi * rpm / 60.0
    meanline_lengths = []
    solidities = []
    overlaps = []
    reynolds = []
    average_velocities = []
    pressure_velocities = []
    suction_velocities = []
    loadings = []
    passage_flow = flow_rate_m3_s / blade_count
    representative_area_m2 = max(
        1.0e-10,
        0.5 * (passage.passage_area_min_mm2 + passage.passage_area_max_mm2)
        * 1.0e-6,
    )
    meridional_velocity = passage_flow / representative_area_m2

    for span_line, wrap_angle in zip(grid.mean_points_xyz, grid.wrap_angles_deg):
        length_mm = sum(
            _length(_subtract(end, start))
            for start, end in zip(span_line, span_line[1:])
        )
        inlet_radius_m = math.hypot(span_line[0][0], span_line[0][1]) / 1000.0
        outlet_radius_m = math.hypot(span_line[-1][0], span_line[-1][1]) / 1000.0
        mean_radius_m = 0.5 * (inlet_radius_m + outlet_radius_m)
        pitch_mm = 2.0 * math.pi * mean_radius_m * 1000.0 / blade_count
        solidity = length_mm / max(pitch_mm, 1.0e-9)
        rotational_velocity = omega * mean_radius_m
        average_velocity = math.hypot(meridional_velocity, rotational_velocity)
        # Guelich-style screening: loading rises with wrap/work per blade and
        # falls with solidity.  Cap only the velocity split, never the evidence.
        loading = abs(math.radians(wrap_angle)) / max(solidity * blade_count, 1.0e-9)
        velocity_split = min(0.75, 0.5 * loading)
        pressure_velocity = average_velocity * (1.0 - velocity_split)
        suction_velocity = average_velocity * (1.0 + velocity_split)
        meanline_lengths.append(length_mm)
        solidities.append(solidity)
        overlaps.append(max(0.0, abs(wrap_angle) - 360.0 / blade_count))
        reynolds.append(
            average_velocity * length_mm / 1000.0
            / max(kinematic_viscosity_m2_s, 1.0e-12)
        )
        average_velocities.append(average_velocity)
        pressure_velocities.append(pressure_velocity)
        suction_velocities.append(suction_velocity)
        loadings.append(loading)

    return BladeHydraulicMetrics(
        method=(
            "Pre-CFD spanwise solidity/Reynolds and symmetric surface-velocity "
            "screening from sampled mean lines and passage flow"
        ),
        meanline_lengths_mm=tuple(round(value, 3) for value in meanline_lengths),
        solidities=tuple(round(value, 4) for value in solidities),
        overlap_angles_deg=tuple(round(value, 3) for value in overlaps),
        reynolds_numbers=tuple(round(value, 1) for value in reynolds),
        average_relative_velocities_m_s=tuple(
            round(value, 4) for value in average_velocities
        ),
        pressure_surface_velocities_m_s=tuple(
            round(value, 4) for value in pressure_velocities
        ),
        suction_surface_velocities_m_s=tuple(
            round(value, 4) for value in suction_velocities
        ),
        velocity_loading_coefficients=tuple(round(value, 4) for value in loadings),
        maximum_velocity_loading_coefficient=round(max(loadings), 4),
        minimum_pressure_velocity_ratio=round(
            min(p / a for p, a in zip(pressure_velocities, average_velocities)), 4
        ),
        maximum_suction_velocity_ratio=round(
            max(s / a for s, a in zip(suction_velocities, average_velocities)), 4
        ),
    )
