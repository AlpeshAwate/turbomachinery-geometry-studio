"""
3D CAD Synthesis Engine for Pump Impellers and Diffusers using OpenCASCADE / CadQuery.
Generates validated, connected solid models with angle-driven variable-height
blades, optional front shrouds, shaft hubs, and vaned diffuser stators.
"""

import os
import math
import json
import struct
from dataclasses import asdict, dataclass
import cadquery as cq
from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing
from core.blade_geometry import (
    BladeSurfaceGrid,
    blade_thickness_factor,
    create_blade_surface_grid,
)
from core.pump_design import (
    COLLECTOR_VOLUTE,
    CompletePumpDesign,
    DiffuserDesign,
    ImpellerDesign,
    VoluteDesign,
)
from core.meridional import (
    MeridionalDesign,
    bezier_point,
    bezier_tangent,
    create_meridional_design,
)


def _log_spiral_centerline(
    r_start: float,
    r_end: float,
    beta_start_deg: float,
    beta_end_deg: float,
    sections: int = 18,
) -> list:
    if r_start <= 0.0 or r_end <= r_start:
        raise ValueError("Invalid blade planform dimensions.")

    centerline = []
    theta = 0.0
    previous_r = r_start
    for index in range(sections):
        fraction = index / (sections - 1.0)
        radius = r_start + fraction * (r_end - r_start)
        beta_deg = beta_start_deg + fraction * (beta_end_deg - beta_start_deg)
        if index:
            mid_fraction = (index - 0.5) / (sections - 1.0)
            mid_beta = math.radians(
                beta_start_deg + mid_fraction * (beta_end_deg - beta_start_deg)
            )
            mid_radius = 0.5 * (previous_r + radius)
            theta += (radius - previous_r) / (mid_radius * max(0.05, math.tan(mid_beta)))
        centerline.append((radius * math.cos(theta), radius * math.sin(theta)))
        previous_r = radius

    return centerline


def _log_spiral_solid(
    r_start: float,
    r_end: float,
    beta_start_deg: float,
    beta_end_deg: float,
    thickness: float,
    height_start: float,
    z_base: float,
    height_end: float = None,
    bottom_axial_at_radius=None,
    top_axial_at_radius=None,
) -> cq.Workplane:
    """Build one continuous blade loft between hub and shroud boundaries.

    Cross-sections are normal to the plan-view mean line. This follows the
    CFturbo preliminary-profile recommendation more closely than the former
    union of overlapping boxes and avoids artificial steps/knuckles on the
    pressure and suction surfaces.
    """
    if height_end is None:
        height_end = height_start
    if thickness <= 0.0 or min(height_start, height_end) <= 0.0:
        raise ValueError("Blade thickness and height must be positive.")
    points = _log_spiral_centerline(
        r_start, r_end, beta_start_deg, beta_end_deg, sections=25
    )
    overlap = max(0.15, 0.06 * thickness)
    section_count = len(points)
    section_wires = []
    for section_index, (x_coord, y_coord) in enumerate(points):
        if section_index == 0:
            tangent_x = points[1][0] - x_coord
            tangent_y = points[1][1] - y_coord
        elif section_index == section_count - 1:
            tangent_x = x_coord - points[-2][0]
            tangent_y = y_coord - points[-2][1]
        else:
            tangent_x = points[section_index + 1][0] - points[section_index - 1][0]
            tangent_y = points[section_index + 1][1] - points[section_index - 1][1]

        tangent_length = math.hypot(tangent_x, tangent_y)
        if tangent_length <= 1e-9:
            raise ValueError("Blade mean line contains a zero-length tangent.")
        normal_x = -tangent_y / tangent_length
        normal_y = tangent_x / tangent_length

        fraction = section_index / (section_count - 1.0)
        # The sizing model supplies maximum profile thickness. Keep explicit
        # blunt LE/TE thicknesses until rounded edge design is implemented.
        if fraction <= 0.5:
            thickness_factor = 0.65 + 0.70 * fraction
        else:
            thickness_factor = 1.45 - 0.90 * fraction
        half_thickness = 0.5 * thickness * thickness_factor
        radius = math.hypot(x_coord, y_coord)
        if bottom_axial_at_radius is not None and top_axial_at_radius is not None:
            section_bottom = bottom_axial_at_radius(radius)
            section_top = top_axial_at_radius(radius)
            if section_top <= section_bottom:
                raise ValueError("Meridional blade bounds cross or have zero height.")
        else:
            section_bottom = z_base
            section_top = z_base + height_start + fraction * (
                height_end - height_start
            )

        left_x = x_coord - normal_x * half_thickness
        left_y = y_coord - normal_y * half_thickness
        right_x = x_coord + normal_x * half_thickness
        right_y = y_coord + normal_y * half_thickness
        section_wires.append(
            cq.Wire.makePolygon(
                [
                    (left_x, left_y, section_bottom - overlap),
                    (right_x, right_y, section_bottom - overlap),
                    (right_x, right_y, section_top + overlap),
                    (left_x, left_y, section_top + overlap),
                ],
                close=True,
            )
        )

    blade_solid = cq.Solid.makeLoft(section_wires, ruled=False)
    if not blade_solid.isValid() or blade_solid.Volume() <= 0.0:
        raise ValueError("Continuous blade loft is invalid or has zero volume.")
    return cq.Workplane("XY").newObject([blade_solid])


def _freeform_blade_solid(
    imp: ImpellerDesign,
    meridional: MeridionalDesign,
    *,
    chord_sections: int = 25,
) -> tuple[cq.Workplane, BladeSurfaceGrid]:
    """Build one five-span blade with distinct hub-to-shroud mean lines.

    The pressure and suction sides are offset from the sampled mean surface in
    the direction perpendicular to each mean line within its rotational span
    surface.  Closed section wires are then lofted along the chord.
    """

    grid = create_blade_surface_grid(
        meridional,
        imp.blade_inlet_angles_spanwise,
        imp.blade_outlet_angles_spanwise,
        span_positions=imp.blade_span_positions,
        stacking_fraction=imp.blade_stacking_fraction,
        chord_sections=chord_sections,
    )
    overlap = max(0.15, 0.06 * imp.blade_thickness)
    mean_points = [list(span_line) for span_line in grid.mean_points_xyz]
    for chord_index in range(len(grid.chord_fractions)):
        hub_span_vector = tuple(
            end - start
            for start, end in zip(
                mean_points[0][chord_index], mean_points[1][chord_index]
            )
        )
        shroud_span_vector = tuple(
            end - start
            for start, end in zip(
                mean_points[-2][chord_index], mean_points[-1][chord_index]
            )
        )
        hub_span_length = math.sqrt(sum(value * value for value in hub_span_vector))
        shroud_span_length = math.sqrt(
            sum(value * value for value in shroud_span_vector)
        )
        if min(hub_span_length, shroud_span_length) <= 1.0e-9:
            raise ValueError("Blade span collapses at a chordwise section.")
        mean_points[0][chord_index] = tuple(
            coordinate - overlap * direction / hub_span_length
            for coordinate, direction in zip(
                mean_points[0][chord_index], hub_span_vector
            )
        )
        mean_points[-1][chord_index] = tuple(
            coordinate + overlap * direction / shroud_span_length
            for coordinate, direction in zip(
                mean_points[-1][chord_index], shroud_span_vector
            )
        )

    pressure_grid = []
    suction_grid = []
    for span_index in range(len(grid.span_positions)):
        pressure_line = []
        suction_line = []
        for chord_index, chord_fraction in enumerate(grid.chord_fractions):
            point = mean_points[span_index][chord_index]
            direction = grid.thickness_directions_xyz[span_index][chord_index]
            half_thickness = (
                0.5
                * imp.blade_thickness
                * blade_thickness_factor(chord_fraction)
            )
            pressure_line.append(
                tuple(
                    coordinate + half_thickness * component
                    for coordinate, component in zip(point, direction)
                )
            )
            suction_line.append(
                tuple(
                    coordinate - half_thickness * component
                    for coordinate, component in zip(point, direction)
                )
            )
        pressure_grid.append(pressure_line)
        suction_grid.append(suction_line)

    def spline_face(point_grid) -> cq.Face:
        return cq.Face.makeSplineApprox(
            [[cq.Vector(*point) for point in row] for row in point_grid],
            tol=1.0e-3,
            minDeg=1,
            maxDeg=3,
        )

    pressure_face = spline_face(pressure_grid)
    suction_face = spline_face(suction_grid)

    def distance(first, second) -> float:
        return math.sqrt(sum((a - b) ** 2 for a, b in zip(first, second)))

    def matching_boundary_edge(face: cq.Face, start, end) -> cq.Edge:
        def endpoint_error(edge: cq.Edge) -> float:
            edge_start = edge.startPoint().toTuple()
            edge_end = edge.endPoint().toTuple()
            return min(
                distance(edge_start, start) + distance(edge_end, end),
                distance(edge_start, end) + distance(edge_end, start),
            )

        return min(face.Edges(), key=endpoint_error)

    pressure_boundaries = (
        (pressure_grid[0][0], pressure_grid[0][-1]),
        (pressure_grid[-1][0], pressure_grid[-1][-1]),
        (pressure_grid[0][0], pressure_grid[-1][0]),
        (pressure_grid[0][-1], pressure_grid[-1][-1]),
    )
    suction_boundaries = (
        (suction_grid[0][0], suction_grid[0][-1]),
        (suction_grid[-1][0], suction_grid[-1][-1]),
        (suction_grid[0][0], suction_grid[-1][0]),
        (suction_grid[0][-1], suction_grid[-1][-1]),
    )
    side_faces = [
        cq.Face.makeRuledSurface(
            matching_boundary_edge(pressure_face, *pressure_boundary),
            matching_boundary_edge(suction_face, *suction_boundary),
        )
        for pressure_boundary, suction_boundary in zip(
            pressure_boundaries, suction_boundaries
        )
    ]
    faces = [pressure_face, suction_face, *side_faces]
    sewing = BRepBuilderAPI_Sewing(0.05, True, True, True, False)
    for face in faces:
        sewing.Add(face.wrapped)
    sewing.Perform()
    sewn_shape = cq.Shape.cast(sewing.SewedShape())
    shells = sewn_shape.Shells()
    if len(shells) != 1:
        raise ValueError(
            f"Free-form blade faces did not sew into one shell ({len(shells)} found)."
        )
    blade_solid = cq.Solid.makeSolid(shells[0])
    if imp.blade_leading_edge_shape != "Ellipse" or imp.blade_trailing_edge_shape != "Ellipse":
        raise ValueError("The current hydraulic edge kernel supports elliptic edges only.")
    if not math.isclose(
        imp.blade_leading_edge_radius,
        imp.blade_trailing_edge_radius,
        abs_tol=1.0e-9,
    ):
        raise ValueError(
            "The current edge fillet operation requires equal leading/trailing radii."
        )
    maximum_robust_edge_radius = 0.19 * imp.blade_thickness
    if not 0.0 < imp.blade_leading_edge_radius < maximum_robust_edge_radius:
        raise ValueError(
            "Blade-edge radius must be positive and below the robust CAD limit "
            f"{maximum_robust_edge_radius:.3f} mm for this profile."
        )

    # Round the two pressure/suction transition edges on each thin end face.
    # Axis ratio 1 is the circular special case of CFturbo's elliptic edge.
    cap_faces = sorted(
        blade_solid.Faces(),
        key=lambda face: math.hypot(face.Center().x, face.Center().y),
    )
    transition_edges = []
    for face in (cap_faces[0], cap_faces[-1]):
        transition_edges.extend(
            sorted(face.Edges(), key=lambda edge: edge.Length(), reverse=True)[:2]
        )
    blade_solid = (
        cq.Workplane("XY")
        .newObject([blade_solid])
        .newObject(transition_edges)
        .fillet(imp.blade_leading_edge_radius)
        .val()
    )
    if not blade_solid.isValid() or blade_solid.Volume() <= 0.0:
        raise ValueError("Rounded free-form blade is invalid or has zero volume.")
    return cq.Workplane("XY").newObject([blade_solid]), grid


def _validate_neighbor_blade_overlap(
    single_blade: cq.Workplane,
    blade_count: int,
    *,
    volume_tolerance_mm3: float = 1.0e-4,
) -> float:
    """Reject exact CAD intersection between two periodic neighboring blades."""

    if blade_count < 2:
        raise ValueError("At least two blades are required for overlap validation.")
    neighbor = single_blade.rotate(
        (0, 0, 0),
        (0, 0, 1),
        360.0 / blade_count,
    )
    overlap_volume = single_blade.intersect(neighbor, clean=True).val().Volume()
    if overlap_volume > volume_tolerance_mm3:
        raise ValueError(
            "Neighboring blade solids intersect by "
            f"{overlap_volume:.6f} mm^3; reduce thickness, blade count, or wrap."
        )
    return overlap_volume


def _validated_single_solid(model: cq.Workplane, label: str) -> cq.Workplane:
    """Fail export early if Boolean operations produced an invalid/disconnected part."""
    shape = model.val()
    if not shape.isValid():
        # OCCT Boolean fuses can retain a correct single volume with invalid
        # same-domain edge bookkeeping where blades overlap a revolved shroud.
        # Shape.fix() heals that topology without replacing the analytic faces.
        repaired = shape.fix()
        if repaired.isValid():
            model = cq.Workplane("XY").newObject([repaired])
            shape = repaired
        else:
            raise ValueError(f"{label} CAD shape is invalid and could not be healed.")
    solid_count = len(shape.Solids())
    if solid_count != 1:
        raise ValueError(f"{label} must be one connected solid; generated {solid_count} solids.")
    if shape.Volume() <= 0.0:
        raise ValueError(f"{label} has zero volume.")
    return model


@dataclass(frozen=True)
class CfdDomainDefinition:
    """Traceable dimensions for the simplified radial-stage CFD interfaces."""

    inlet_extension_length_mm: float
    inlet_plane_z_mm: float
    impeller_outlet_radius_mm: float
    stationary_inlet_radius_mm: float
    rsi_radius_mm: float
    rotor_extension_length_mm: float
    stationary_connection_length_mm: float
    rotor_outlet_width_mm: float
    stationary_inlet_width_mm: float
    rsi_area_mm2: float
    interface_shape: str = "cylindrical, r = constant"
    interface_method: str = "mid-gap virtual extension"
    source: str = "CFturbo_en.pdf pp. 595-600"
    stationary_domain_type: str = "annular RSI connection"
    stationary_outlet_center_mm: tuple[float, float, float] | None = None
    stationary_outlet_normal: tuple[float, float, float] | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def create_cfd_domain_definition(
    imp: ImpellerDesign,
    diff: DiffuserDesign,
    meridional: MeridionalDesign,
    volute: VoluteDesign | None = None,
) -> CfdDomainDefinition:
    """Place a cylindrical RSI midway across the rotor-stator radial gap.

    CFturbo recommends extending the rotating flow domain so the blade trailing
    edges do not lie on the RSI, and normally positions the interface midway
    between rotating and stationary components. The stationary-side connection
    occupies the remaining half of that gap.
    """

    impeller_radius = 0.5 * imp.outlet_diameter_d2
    stationary_radius = 0.5 * (
        volute.inlet_diameter_d4 if volute is not None else diff.inlet_diameter_d3
    )
    gap = stationary_radius - impeller_radius
    if gap <= 0.10:
        raise ValueError(
            "The stationary inlet must lie outside the impeller outlet before "
            "a radial rotor-stator interface can be created."
        )
    stationary_width = (
        volute.inlet_width_b4 if volute is not None else diff.inlet_width_b3
    )
    if imp.outlet_width_b2 <= 0.0 or stationary_width <= 0.0:
        raise ValueError("CFD interface widths must be positive.")

    rsi_radius = impeller_radius + 0.5 * gap
    inlet_extension = max(15.0, 0.25 * imp.suction_diameter_ds)
    inlet_plane_z = (
        imp.back_shroud_thickness + meridional.axial_length + inlet_extension
    )
    outlet_center = None
    outlet_normal = None
    stationary_domain_type = "annular RSI connection"
    if volute is not None:
        theta = math.radians(volute.wrap_angle_deg)
        radial = (math.cos(theta), math.sin(theta))
        tangent = (-math.sin(theta), math.cos(theta))
        end_center_radius = 0.5 * (
            volute.station_inner_radii_mm[-1]
            + volute.station_outer_radii_mm[-1]
        )
        total_length = (
            volute.discharge_length_mm + volute.outlet_extension_length_mm
        )
        outlet_center = (
            round(end_center_radius * radial[0] + total_length * tangent[0], 4),
            round(end_center_radius * radial[1] + total_length * tangent[1], 4),
            round(
                imp.back_shroud_thickness
                + 0.5 * volute.discharge_outlet_width_mm,
                4,
            ),
        )
        outlet_normal = (round(tangent[0], 8), round(tangent[1], 8), 0.0)
        stationary_domain_type = "single volute with tangential discharge diffuser"

    return CfdDomainDefinition(
        inlet_extension_length_mm=round(inlet_extension, 4),
        inlet_plane_z_mm=round(inlet_plane_z, 4),
        impeller_outlet_radius_mm=round(impeller_radius, 4),
        stationary_inlet_radius_mm=round(stationary_radius, 4),
        rsi_radius_mm=round(rsi_radius, 4),
        rotor_extension_length_mm=round(rsi_radius - impeller_radius, 4),
        stationary_connection_length_mm=round(
            stationary_radius - rsi_radius, 4
        ),
        rotor_outlet_width_mm=round(imp.outlet_width_b2, 4),
        stationary_inlet_width_mm=round(stationary_width, 4),
        rsi_area_mm2=round(
            2.0 * math.pi * rsi_radius * imp.outlet_width_b2,
            4,
        ),
        stationary_domain_type=stationary_domain_type,
        stationary_outlet_center_mm=outlet_center,
        stationary_outlet_normal=outlet_normal,
    )


def _unbladed_rotating_flow_volume(
    imp: ImpellerDesign,
    meridional: MeridionalDesign,
    definition: CfdDomainDefinition,
) -> cq.Workplane:
    """Revolve the primary passage and add inlet/outlet CFD extensions."""

    datum_z = imp.back_shroud_thickness
    hub_points = [
        (radius, axial + datum_z)
        for radius, axial in meridional.hub_control_points_rz
    ]
    shroud_points = [
        (radius, axial + datum_z)
        for radius, axial in meridional.shroud_control_points_rz
    ]
    passage_profile = (
        cq.Workplane("XZ")
        .moveTo(*hub_points[0])
        .bezier(hub_points[1:], includeCurrent=True)
        .lineTo(*shroud_points[-1])
        .bezier(list(reversed(shroud_points[:-1])), includeCurrent=True)
        .lineTo(*hub_points[0])
        .close()
    )
    passage = passage_profile.revolve(360, (0, 0), (0, 1))

    inlet_overlap = 0.05
    inlet_extension = (
        cq.Workplane("XY")
        .circle(0.5 * imp.suction_diameter_ds)
        .circle(0.5 * imp.hub_diameter_dh)
        .extrude(definition.inlet_extension_length_mm + inlet_overlap)
        .translate(
            (
                0.0,
                0.0,
                datum_z + meridional.axial_length - inlet_overlap,
            )
        )
    )
    radial_overlap = 0.05
    outlet_extension = (
        cq.Workplane("XY")
        .circle(definition.rsi_radius_mm)
        .circle(definition.impeller_outlet_radius_mm - radial_overlap)
        .extrude(imp.outlet_width_b2)
        .translate((0.0, 0.0, datum_z))
    )
    return passage.union(inlet_extension, clean=True).union(
        outlet_extension, clean=True
    )


def build_rotating_flow_domain(
    imp: ImpellerDesign,
    meridional: MeridionalDesign,
    diff: DiffuserDesign,
    volute: VoluteDesign | None = None,
) -> tuple[cq.Workplane, CfdDomainDefinition]:
    """Build the closed rotating fluid volume, including virtual extensions.

    This is the inverse volume occupied by fluid inside the impeller passage;
    it is deliberately separate from the impeller material solid. Blade solids
    are subtracted from the revolved hub/shroud passage before validation.
    """

    definition = create_cfd_domain_definition(imp, diff, meridional, volute)
    flow_domain = _unbladed_rotating_flow_volume(imp, meridional, definition)
    blade, _grid = _freeform_blade_solid(imp, meridional)
    blade = blade.translate((0.0, 0.0, imp.back_shroud_thickness))
    _validate_neighbor_blade_overlap(blade, imp.blade_count_z)
    for index in range(imp.blade_count_z):
        angle_deg = index * (360.0 / imp.blade_count_z)
        flow_domain = flow_domain.cut(
            blade.rotate((0, 0, 0), (0, 0, 1), angle_deg),
            clean=True,
        )
    return _validated_single_solid(
        flow_domain, "Rotating fluid domain"
    ), definition


def build_stationary_rsi_connection(
    imp: ImpellerDesign,
    diff: DiffuserDesign | None,
    definition: CfdDomainDefinition,
) -> cq.Workplane:
    """Create the static virtual volume from the RSI to the component inlet."""

    datum_z = imp.back_shroud_thickness
    profile = (
        cq.Workplane("XZ")
        .moveTo(definition.rsi_radius_mm, datum_z)
        .lineTo(definition.stationary_inlet_radius_mm, datum_z)
        .lineTo(
            definition.stationary_inlet_radius_mm,
            datum_z + definition.stationary_inlet_width_mm,
        )
        .lineTo(
            definition.rsi_radius_mm,
            datum_z + definition.rotor_outlet_width_mm,
        )
        .close()
    )
    connection = profile.revolve(360, (0, 0), (0, 1))
    return _validated_single_solid(connection, "Stationary RSI connection")


def _closed_polygon_wire(points: list[cq.Vector]) -> cq.Wire:
    """Create a predictably oriented closed polygon for a multi-section loft."""

    if len(points) < 3:
        raise ValueError("A loft section needs at least three points.")
    return cq.Wire.makePolygon([*points, points[0]], False)


def _volute_spiral_volume(
    volute: VoluteDesign,
    z_base_mm: float,
    expansion_mm: float = 0.0,
    radial_overlap_mm: float = 0.25,
) -> cq.Workplane:
    """Loft the rectangular Pfleiderer stations into one spiral volume."""

    wires: list[cq.Wire] = []
    z_low = z_base_mm - expansion_mm
    z_high = z_base_mm + volute.inlet_width_b4 + expansion_mm
    for angle_deg, inner_radius, outer_radius in zip(
        volute.station_angles_deg,
        volute.station_inner_radii_mm,
        volute.station_outer_radii_mm,
    ):
        theta = math.radians(angle_deg)
        radial_x = math.cos(theta)
        radial_y = math.sin(theta)
        inner = inner_radius - expansion_mm - radial_overlap_mm
        outer = outer_radius + expansion_mm
        wires.append(
            _closed_polygon_wire(
                [
                    cq.Vector(inner * radial_x, inner * radial_y, z_low),
                    cq.Vector(outer * radial_x, outer * radial_y, z_low),
                    cq.Vector(outer * radial_x, outer * radial_y, z_high),
                    cq.Vector(inner * radial_x, inner * radial_y, z_high),
                ]
            )
        )
    spiral = cq.Solid.makeLoft(wires, False)
    return cq.Workplane("XY").newObject([spiral])


def _volute_discharge_volume(
    volute: VoluteDesign,
    z_base_mm: float,
    expansion_mm: float = 0.0,
) -> cq.Workplane:
    """Build the tangential diffuser and its straight CFD outlet extension."""

    theta = math.radians(volute.wrap_angle_deg)
    radial = (math.cos(theta), math.sin(theta))
    tangent = (-math.sin(theta), math.cos(theta))
    inlet_inner = volute.station_inner_radii_mm[-1]
    inlet_outer = volute.station_outer_radii_mm[-1]
    inlet_center_radius = 0.5 * (inlet_inner + inlet_outer)
    inlet_height = inlet_outer - inlet_inner
    diffuser_length = volute.discharge_length_mm
    total_length = diffuser_length + volute.outlet_extension_length_mm
    section_count = 13
    wires: list[cq.Wire] = []
    for index in range(section_count + 1):
        if index < section_count:
            fraction = index / (section_count - 1.0)
            distance = diffuser_length * fraction
            height = inlet_height + fraction * (
                volute.discharge_outlet_height_mm - inlet_height
            )
            width = volute.inlet_width_b4 + fraction * (
                volute.discharge_outlet_width_mm - volute.inlet_width_b4
            )
        else:
            distance = total_length
            height = volute.discharge_outlet_height_mm
            width = volute.discharge_outlet_width_mm
        center_x = inlet_center_radius * radial[0] + distance * tangent[0]
        center_y = inlet_center_radius * radial[1] + distance * tangent[1]
        half_height = 0.5 * height + expansion_mm
        z_low = z_base_mm - expansion_mm
        z_high = z_base_mm + width + expansion_mm
        wires.append(
            _closed_polygon_wire(
                [
                    cq.Vector(
                        center_x - half_height * radial[0],
                        center_y - half_height * radial[1],
                        z_low,
                    ),
                    cq.Vector(
                        center_x + half_height * radial[0],
                        center_y + half_height * radial[1],
                        z_low,
                    ),
                    cq.Vector(
                        center_x + half_height * radial[0],
                        center_y + half_height * radial[1],
                        z_high,
                    ),
                    cq.Vector(
                        center_x - half_height * radial[0],
                        center_y - half_height * radial[1],
                        z_high,
                    ),
                ]
            )
        )
    diffuser = cq.Solid.makeLoft(wires, False)
    return cq.Workplane("XY").newObject([diffuser])


def build_volute_flow_domain(
    imp: ImpellerDesign,
    volute: VoluteDesign,
    definition: CfdDomainDefinition,
) -> cq.Workplane:
    """Create one watertight stationary fluid domain from RSI to outlet.

    The annular inlet is the virtual RSI connection.  It overlaps the spiral
    inner boundary, and the final spiral section is shared with the tangential
    discharge diffuser, so the returned body is a single fluid solid.
    """

    datum_z = imp.back_shroud_thickness
    inlet_connection = build_stationary_rsi_connection(
        imp,
        None,
        definition,
    )
    spiral = _volute_spiral_volume(volute, datum_z)
    discharge = _volute_discharge_volume(volute, datum_z)
    stationary = inlet_connection.union(spiral, clean=True).union(
        discharge, clean=True
    )
    return _validated_single_solid(stationary, "Volute fluid domain")


def build_volute_material_solid(
    imp: ImpellerDesign,
    volute: VoluteDesign,
    definition: CfdDomainDefinition,
) -> cq.Workplane:
    """Create the preliminary casing material by subtracting the fluid volume."""

    datum_z = imp.back_shroud_thickness
    wall = volute.wall_thickness_mm
    stationary_flow = build_volute_flow_domain(imp, volute, definition)
    annular_envelope = (
        cq.Workplane("XY")
        .circle(0.5 * volute.inlet_diameter_d4 + wall)
        .circle(max(0.1, definition.rsi_radius_mm - wall))
        .extrude(volute.inlet_width_b4 + 2.0 * wall)
        .translate((0.0, 0.0, datum_z - wall))
    )
    spiral_envelope = _volute_spiral_volume(
        volute,
        datum_z,
        expansion_mm=wall,
        radial_overlap_mm=0.25,
    )
    discharge_envelope = _volute_discharge_volume(
        volute, datum_z, expansion_mm=wall
    )
    envelope = annular_envelope.union(spiral_envelope, clean=True).union(
        discharge_envelope, clean=True
    )
    casing = envelope.cut(stationary_flow, clean=True)
    return _validated_single_solid(casing, "Volute casing")


def _face_radius(face: cq.Face) -> float:
    bounds = face.BoundingBox()
    return max(
        abs(bounds.xmin),
        abs(bounds.xmax),
        abs(bounds.ymin),
        abs(bounds.ymax),
    )


def _faces_to_workplane(faces: list[cq.Face], label: str) -> cq.Workplane:
    if not faces:
        raise ValueError(f"No faces were found for CFD patch '{label}'.")
    compound = cq.Compound.makeCompound(faces)
    if not compound.isValid():
        raise ValueError(f"CFD patch '{label}' is invalid.")
    return cq.Workplane("XY").newObject([compound])


def rotating_flow_boundary_patches(
    flow_domain: cq.Workplane,
    definition: CfdDomainDefinition,
) -> dict[str, cq.Workplane]:
    """Partition the rotor shell without making its virtual inlet duct rotate."""

    inlet_faces: list[cq.Face] = []
    rsi_faces: list[cq.Face] = []
    virtual_inlet_wall_faces: list[cq.Face] = []
    wall_faces: list[cq.Face] = []
    virtual_inlet_start_z = (
        definition.inlet_plane_z_mm - definition.inlet_extension_length_mm
    )
    for face in flow_domain.val().Faces():
        bounds = face.BoundingBox()
        is_inlet = (
            face.geomType() == "PLANE"
            and abs(bounds.zmax - bounds.zmin) <= 1.0e-5
            and abs(bounds.zmax - definition.inlet_plane_z_mm) <= 0.02
        )
        is_rsi = (
            face.geomType() == "CYLINDER"
            and abs(_face_radius(face) - definition.rsi_radius_mm) <= 0.02
        )
        is_virtual_inlet_wall = (
            face.geomType() == "CYLINDER"
            and abs(bounds.zmax - definition.inlet_plane_z_mm) <= 0.02
            and bounds.zmin >= virtual_inlet_start_z - 0.02
        )
        if is_inlet:
            inlet_faces.append(face)
        elif is_rsi:
            rsi_faces.append(face)
        elif is_virtual_inlet_wall:
            virtual_inlet_wall_faces.append(face)
        else:
            wall_faces.append(face)
    return {
        "rotor_inlet": _faces_to_workplane(inlet_faces, "rotor_inlet"),
        "rotor_rsi": _faces_to_workplane(rsi_faces, "rotor_rsi"),
        "virtual_inlet_walls": _faces_to_workplane(
            virtual_inlet_wall_faces, "virtual_inlet_walls"
        ),
        "rotor_walls": _faces_to_workplane(wall_faces, "rotor_walls"),
    }


def stationary_connection_boundary_patches(
    connection: cq.Workplane,
    definition: CfdDomainDefinition,
) -> dict[str, cq.Workplane]:
    """Partition the static RSI connection into interface, outlet, and walls."""

    rsi_faces: list[cq.Face] = []
    outlet_faces: list[cq.Face] = []
    wall_faces: list[cq.Face] = []
    for face in connection.val().Faces():
        radius = _face_radius(face)
        if (
            face.geomType() == "CYLINDER"
            and abs(radius - definition.rsi_radius_mm) <= 0.02
        ):
            rsi_faces.append(face)
        elif (
            face.geomType() == "CYLINDER"
            and abs(radius - definition.stationary_inlet_radius_mm) <= 0.02
        ):
            outlet_faces.append(face)
        else:
            wall_faces.append(face)
    return {
        "stationary_rsi": _faces_to_workplane(rsi_faces, "stationary_rsi"),
        "stationary_connection_outlet": _faces_to_workplane(
            outlet_faces, "stationary_connection_outlet"
        ),
        "stationary_connection_walls": _faces_to_workplane(
            wall_faces, "stationary_connection_walls"
        ),
    }


def volute_flow_boundary_patches(
    flow_domain: cq.Workplane,
    definition: CfdDomainDefinition,
) -> dict[str, cq.Workplane]:
    """Partition a complete stationary volute into RSI, outlet, and walls."""

    if (
        definition.stationary_outlet_center_mm is None
        or definition.stationary_outlet_normal is None
    ):
        raise ValueError("Volute outlet metadata is missing from the CFD definition.")
    outlet_center = cq.Vector(*definition.stationary_outlet_center_mm)
    outlet_normal = cq.Vector(*definition.stationary_outlet_normal)
    rsi_faces: list[cq.Face] = []
    outlet_faces: list[cq.Face] = []
    wall_faces: list[cq.Face] = []
    for face in flow_domain.val().Faces():
        radius = _face_radius(face)
        is_rsi = (
            face.geomType() == "CYLINDER"
            and abs(radius - definition.rsi_radius_mm) <= 0.02
        )
        is_outlet = False
        if face.geomType() == "PLANE":
            center_delta = face.Center() - outlet_center
            plane_distance = abs(center_delta.dot(outlet_normal))
            face_normal = face.normalAt(face.Center())
            normal_alignment = abs(face_normal.dot(outlet_normal))
            is_outlet = plane_distance <= 0.05 and normal_alignment >= 0.999
        if is_rsi:
            rsi_faces.append(face)
        elif is_outlet:
            outlet_faces.append(face)
        else:
            wall_faces.append(face)
    return {
        "stationary_rsi": _faces_to_workplane(rsi_faces, "stationary_rsi"),
        "stationary_outlet": _faces_to_workplane(
            outlet_faces, "stationary_outlet"
        ),
        "stationary_walls": _faces_to_workplane(
            wall_faces, "stationary_walls"
        ),
    }


def build_front_shroud_solid(
    imp: ImpellerDesign,
    meridional: MeridionalDesign,
) -> cq.Workplane:
    """Build the closed-impeller front shroud as a separate CAD component."""
    if imp.configuration != "Closed":
        raise ValueError("A front shroud is available only for a closed impeller.")

    backplate_thickness = imp.back_shroud_thickness
    front_shroud_thickness = imp.front_shroud_thickness
    inner_points = [
        (radius, axial + backplate_thickness)
        for radius, axial in meridional.shroud_control_points_rz
    ]
    outer_points_forward = []
    for index in range(33):
        fraction = index / 32.0
        radius, axial = bezier_point(
            meridional.shroud_control_points_rz,
            fraction,
        )
        tangent_r, tangent_z = bezier_tangent(
            meridional.shroud_control_points_rz,
            fraction,
        )
        tangent_length = math.hypot(tangent_r, tangent_z)
        if tangent_length <= 1e-9:
            raise ValueError("Shroud contour has a zero-length tangent.")
        normal_r = -tangent_z / tangent_length
        normal_z = tangent_r / tangent_length
        outer_points_forward.append(
            (
                radius + front_shroud_thickness * normal_r,
                axial
                + backplate_thickness
                + front_shroud_thickness * normal_z,
            )
        )
    outer_points = list(reversed(outer_points_forward))
    shroud_profile = (
        cq.Workplane("XZ")
        .moveTo(*inner_points[0])
        .bezier(inner_points[1:], includeCurrent=True)
        .lineTo(*outer_points[0])
        .spline(outer_points[1:], includeCurrent=True)
    )
    front_shroud = shroud_profile.close().revolve(360, (0, 0), (0, 1))
    if imp.eye_collar_enabled:
        # The inlet neck/wear-ring land belongs to the shroud material solid,
        # not to the primary hydraulic passage.  Its inner diameter therefore
        # remains exactly Ds while it extends axially beyond the shroud nose.
        eye_radius = imp.suction_diameter_ds / 2.0
        collar_outer_radius = eye_radius + imp.eye_collar_radial_thickness
        collar_base_z = inner_points[0][1]
        collar = (
            cq.Workplane("XY")
            .circle(collar_outer_radius)
            .circle(eye_radius)
            .extrude(imp.eye_collar_axial_length)
            .translate((0.0, 0.0, collar_base_z))
        )
        front_shroud = front_shroud.union(collar, clean=True)
    return _validated_single_solid(front_shroud, "Front shroud")


def _export_openfoam_stl(model: cq.Workplane, path: str) -> None:
    """Export unitless STL coordinates in metres (the OpenFOAM convention)."""
    # Tessellate the native analytic millimetre geometry first. Scaling the OCCT
    # shape before tessellation converts some surfaces and can create unmatched
    # seams at fine tolerances. STL is unitless, so scale only its vertex records.
    # OCCT's parallel incremental mesher intermittently raises native access
    # violations on complex Boolean/loft combinations under Windows.  The
    # deterministic single-thread path is slower but stable and produces the
    # same watertight engineering tessellation.
    exported = model.val().exportStl(
        path,
        # Absolute 0.04 mm chordal and 0.08 rad angular deflection is well
        # below preliminary pump-design tolerances while avoiding pathological
        # over-tessellation of long spiral and outlet faces.
        tolerance=0.04,
        angularTolerance=0.08,
        ascii=False,
        relative=False,
        parallel=False,
    )
    if not exported:
        raise ValueError(f"OpenCASCADE failed to tessellate '{path}'.")
    # Scale triangle vertices as a stream. Boundary compounds can tessellate to
    # hundreds of megabytes; loading the entire STL into a bytearray caused an
    # avoidable export-time MemoryError on Windows.
    scaled_path = f"{path}.scaled"
    try:
        with open(path, "rb") as source, open(scaled_path, "wb") as target:
            header = source.read(84)
            if len(header) != 84:
                raise ValueError("STL export is unexpectedly short.")
            triangle_count = struct.unpack_from("<I", header, 80)[0]
            target.write(header)
            for _ in range(triangle_count):
                record = bytearray(source.read(50))
                if len(record) != 50:
                    raise ValueError("Expected OpenCASCADE binary STL output.")
                coordinates = struct.unpack_from("<9f", record, 12)
                struct.pack_into(
                    "<9f",
                    record,
                    12,
                    *(coordinate * 0.001 for coordinate in coordinates),
                )
                target.write(record)
            if source.read(1):
                raise ValueError("Binary STL contains unexpected trailing data.")
        os.replace(scaled_path, path)
    finally:
        if os.path.exists(scaled_path):
            os.remove(scaled_path)


def build_impeller_solid(
    imp: ImpellerDesign,
    meridional: MeridionalDesign | None = None,
    include_front_shroud: bool = True,
) -> cq.Workplane:
    """Construct an impeller around smooth meridional hub/shroud boundaries."""

    if meridional is None:
        # Backwards-compatible fallback for callers that only retained the
        # impeller result. Normal design/export paths pass the exact model.
        meridional = create_meridional_design(
            suction_diameter_ds=imp.suction_diameter_ds,
            hub_diameter_dh=imp.hub_diameter_dh,
            outlet_diameter_d2=imp.outlet_diameter_d2,
            inlet_width_b1=imp.inlet_width_b1,
            outlet_width_b2=imp.outlet_width_b2,
            specific_speed_nq=30.0,
        )

    r_hub = imp.hub_diameter_dh / 2.0
    r_2 = imp.outlet_diameter_d2 / 2.0
    backplate_thickness = imp.back_shroud_thickness
    front_shroud_thickness = 0.0
    bore_radius = max(4.0, r_hub * 0.4)

    hub_curve = [
        (radius, axial + backplate_thickness)
        for radius, axial in meridional.hub_control_points_rz
    ]

    # Revolve the material below the hydraulic hub curve. This replaces the
    # former flat disk and cylindrical hub with the designed axial-to-radial
    # meridional boundary while retaining a shaft bore.
    hub_profile = (
        cq.Workplane("XZ")
        .moveTo(*hub_curve[0])
        .bezier(hub_curve[1:], includeCurrent=True)
    )
    hub_profile = (
        hub_profile
        .lineTo(r_2, 0.0)
        .lineTo(bore_radius, 0.0)
        .lineTo(bore_radius, meridional.axial_length + backplate_thickness)
        .lineTo(r_hub, meridional.axial_length + backplate_thickness)
        .close()
    )
    impeller = hub_profile.revolve(360, (0, 0), (0, 1))

    single_blade, _blade_grid = _freeform_blade_solid(imp, meridional)
    # The pure hydraulic grid uses the meridional coordinate system. Shift the
    # material blade onto the rear-shroud datum used by the CAD assembly.
    single_blade = single_blade.translate((0.0, 0.0, backplate_thickness))
    _validate_neighbor_blade_overlap(single_blade, imp.blade_count_z)

    for index in range(imp.blade_count_z):
        angle_deg = index * (360.0 / imp.blade_count_z)
        impeller = impeller.union(
            single_blade.rotate((0, 0, 0), (0, 0, 1), angle_deg), clean=True
        )

    if imp.configuration == "Closed" and include_front_shroud:
        front_shroud_thickness = imp.front_shroud_thickness
        front_shroud = build_front_shroud_solid(imp, meridional)
        impeller = impeller.union(front_shroud, clean=True)

    # Blade sections overlap the hydraulic boundaries slightly so the Boolean
    # union remains robust. Trim that allowance at the outlet diameter.
    trim_height = (
        backplate_thickness
        + meridional.axial_length
        + front_shroud_thickness
        + (imp.eye_collar_axial_length if imp.eye_collar_enabled else 0.0)
        + 2.0
    )
    outlet_envelope = cq.Workplane("XY").circle(r_2).extrude(trim_height)
    impeller = impeller.intersect(outlet_envelope, clean=True)

    return _validated_single_solid(impeller, "Impeller")


def build_diffuser_solid(diff: DiffuserDesign, imp: ImpellerDesign) -> cq.Workplane:
    """
    Constructs a matching 3D vaned diffuser stator ring.
    """
    r_3 = diff.inlet_diameter_d3 / 2.0
    r_4 = diff.outlet_diameter_d4 / 2.0
    b_3 = diff.inlet_width_b3
    wall_thk = max(3.0, b_3 * 0.3)
    t_d = diff.vane_thickness
    backplate_thickness = max(3.0, min(8.0, 0.22 * b_3))
    outer_radius = r_4 + wall_thk
    inner_backplate_radius = max(1.0, r_3 - wall_thk)

    backplate = (
        cq.Workplane("XY")
        .circle(outer_radius)
        .circle(inner_backplate_radius)
        .extrude(backplate_thickness)
    )
    outer_wall = (
        cq.Workplane("XY")
        .circle(outer_radius)
        .circle(r_4 - 0.25 * t_d)
        .extrude(backplate_thickness + b_3)
    )
    diffuser_assembly = backplate.union(outer_wall)

    vane = _log_spiral_solid(
        r_3,
        r_4,
        diff.vane_inlet_angle_beta3,
        diff.vane_outlet_angle_beta4,
        t_d,
        b_3,
        backplate_thickness,
        height_end=b_3,
    )
    for index in range(diff.vane_count_zd):
        angle_deg = index * (360.0 / diff.vane_count_zd)
        diffuser_assembly = diffuser_assembly.union(
            vane.rotate((0, 0, 0), (0, 0, 1), angle_deg), clean=True
        )

    return _validated_single_solid(diffuser_assembly, "Diffuser")


def export_turbomachinery_for_openfoam(
    design: CompletePumpDesign, 
    output_dir: str,
    export_impeller: bool = True,
    export_diffuser: bool = True,
    export_cfd_domain: bool | None = None,
    generate_solver_case: bool = True,
) -> dict:
    """
    Export material CAD and, when an impeller is selected, its rotating CFD domain.
    """
    if export_cfd_domain is None:
        export_cfd_domain = export_impeller
    selected_collector = (
        design.architecture.single_stage_collector
        if design.architecture is not None
        else None
    )
    uses_volute = selected_collector == COLLECTOR_VOLUTE
    failed_checks = [
        check for check in design.engineering_record.checks if check.status == "fail"
    ]
    if failed_checks:
        raise ValueError(
            f"CAD export blocked by engineering gate: {failed_checks[0].message}"
        )
    if design.architecture and design.architecture.is_multistage:
        raise ValueError(
            "Multistage export is blocked because only the reference-stage impeller "
            "is currently generated; return channels, stage placement, and the final "
            "collector are not yet complete."
        )
    if (
        export_diffuser
        and design.architecture
        and not design.architecture.has_supported_stationary_cad
    ):
        raise ValueError(
            "A stationary collector export was requested for an architecture whose "
            "selected collector does not yet have material CAD."
        )

    os.makedirs(output_dir, exist_ok=True)
    exported_files = {}

    impeller_solid = None
    diffuser_solid = None
    volute_solid = None
    cfd_definition = None
    cfd_boundary_files = {}
    cfd_geometry_bounds_mm = None
    solver_case_info = None

    if export_impeller:
        print("[CAD Builder] Generating 3D Impeller...")
        impeller_solid = build_impeller_solid(design.impeller, design.meridional)
        stl_path = os.path.join(output_dir, "impeller_openfoam.stl")
        step_path = os.path.join(output_dir, "impeller.step")
        
        # STEP retains millimetres; STL coordinates are explicitly exported in metres.
        _export_openfoam_stl(impeller_solid, stl_path)
        cq.exporters.export(impeller_solid, step_path)
        exported_files["impeller_stl"] = stl_path
        exported_files["impeller_step"] = step_path

    if export_diffuser:
        if uses_volute:
            print("[CAD Builder] Generating single-volute casing...")
            collector_definition = create_cfd_domain_definition(
                design.impeller,
                design.diffuser,
                design.meridional,
                design.volute,
            )
            volute_solid = build_volute_material_solid(
                design.impeller, design.volute, collector_definition
            )
            volute_stl = os.path.join(output_dir, "volute_casing_openfoam.stl")
            volute_step = os.path.join(output_dir, "volute_casing.step")
            _export_openfoam_stl(volute_solid, volute_stl)
            cq.exporters.export(volute_solid, volute_step)
            exported_files["volute_stl"] = volute_stl
            exported_files["volute_step"] = volute_step
        else:
            print("[CAD Builder] Generating 3D Diffuser...")
            diffuser_solid = build_diffuser_solid(design.diffuser, design.impeller)
            stl_diff_path = os.path.join(output_dir, "diffuser_openfoam.stl")
            step_diff_path = os.path.join(output_dir, "diffuser.step")
            _export_openfoam_stl(diffuser_solid, stl_diff_path)
            cq.exporters.export(diffuser_solid, step_diff_path)
            exported_files["diffuser_stl"] = stl_diff_path
            exported_files["diffuser_step"] = step_diff_path

    if export_cfd_domain:
        print("[CAD Builder] Generating rotating CFD flow domain and RSI...")
        rotating_domain, cfd_definition = build_rotating_flow_domain(
            design.impeller,
            design.meridional,
            design.diffuser,
            design.volute if uses_volute else None,
        )
        stationary_connection = (
            build_volute_flow_domain(
                design.impeller, design.volute, cfd_definition
            )
            if uses_volute
            else build_stationary_rsi_connection(
                design.impeller,
                design.diffuser,
                cfd_definition,
            )
        )
        rotating_stl = os.path.join(output_dir, "rotating_flow_domain.stl")
        rotating_step = os.path.join(output_dir, "rotating_flow_domain.step")
        stationary_basename = (
            "stationary_volute_flow_domain"
            if uses_volute
            else "stationary_rsi_connection"
        )
        stationary_stl = os.path.join(output_dir, f"{stationary_basename}.stl")
        stationary_step = os.path.join(output_dir, f"{stationary_basename}.step")
        _export_openfoam_stl(rotating_domain, rotating_stl)
        cq.exporters.export(rotating_domain, rotating_step)
        _export_openfoam_stl(stationary_connection, stationary_stl)
        cq.exporters.export(stationary_connection, stationary_step)
        exported_files["rotating_flow_domain_stl"] = rotating_stl
        exported_files["rotating_flow_domain_step"] = rotating_step
        exported_files[f"{stationary_basename}_stl"] = stationary_stl
        exported_files[f"{stationary_basename}_step"] = stationary_step

        rotor_bounds = rotating_domain.val().BoundingBox()
        stationary_bounds = stationary_connection.val().BoundingBox()
        cfd_geometry_bounds_mm = (
            min(rotor_bounds.xmin, stationary_bounds.xmin),
            min(rotor_bounds.ymin, stationary_bounds.ymin),
            min(rotor_bounds.zmin, stationary_bounds.zmin),
            max(rotor_bounds.xmax, stationary_bounds.xmax),
            max(rotor_bounds.ymax, stationary_bounds.ymax),
            max(rotor_bounds.zmax, stationary_bounds.zmax),
        )

        boundary_models = {
            **rotating_flow_boundary_patches(rotating_domain, cfd_definition),
            **(
                volute_flow_boundary_patches(
                    stationary_connection, cfd_definition
                )
                if uses_volute
                else stationary_connection_boundary_patches(
                    stationary_connection, cfd_definition
                )
            ),
        }
        for patch_name, patch_model in boundary_models.items():
            patch_path = os.path.join(output_dir, f"{patch_name}.stl")
            _export_openfoam_stl(patch_model, patch_path)
            key = f"{patch_name}_stl"
            exported_files[key] = patch_path
            cfd_boundary_files[patch_name] = os.path.basename(patch_path)

        if generate_solver_case and uses_volute:
            from core.openfoam_case import generate_steady_mrf_case

            solver_case_dir = os.path.join(output_dir, "openfoam_steady_mrf")
            solver_case_info = generate_steady_mrf_case(
                design=design,
                definition=cfd_definition,
                case_dir=solver_case_dir,
                boundary_stls={
                    name: exported_files[f"{name}_stl"]
                    for name in cfd_boundary_files
                },
                geometry_bounds_mm=cfd_geometry_bounds_mm,
            )
            exported_files["openfoam_case"] = solver_case_dir
            exported_files["openfoam_case_manifest"] = solver_case_info[
                "manifest_path"
            ]

    # Generate OpenFOAM CFD Simulation Guide / Parameters
    of_summary_path = os.path.join(output_dir, "openfoam_case_summary.txt")
    with open(of_summary_path, "w", encoding="utf-8") as f:
        f.write("=================================================================\n")
        f.write("          OPENFOAM GEOMETRY PACKAGE AND DESIGN SUMMARY\n")
        f.write("=================================================================\n\n")
        f.write(f"Design ID:               {design.design_id}\n")
        if design.architecture:
            f.write(
                f"Pump Architecture:        {design.architecture.machine_configuration}\n"
            )
            f.write(f"Stage Count:              {design.architecture.stage_count}\n")
            f.write(
                "Component Sequence:       "
                + " -> ".join(design.architecture.component_sequence)
                + "\n"
            )
            f.write(f"CAD Scope:                {design.architecture.cad_scope}\n")
        f.write(f"Fluid Type:              {design.fluid.name}\n")
        f.write(f"Operating Temperature:   {design.fluid.temperature_c:.1f} °C\n")
        f.write(f"Density (rho):           {design.fluid.density:.2f} kg/m^3\n")
        f.write(f"Kinematic Viscosity (nu): {design.fluid.kinematic_viscosity:.3e} m^2/s\n")
        f.write(f"Vapor Pressure (Pv):     {design.fluid.vapor_pressure:.1f} Pa\n\n")
        
        f.write("OPERATING POINT:\n")
        f.write(f"  Target Head (H):       {design.requirements.head_m:.1f} m\n")
        f.write(f"  Flow Rate (Q):         {design.requirements.discharge_m3_h:.1f} m^3/h ({design.requirements.discharge_m3_h/3600.0:.4f} m^3/s)\n")
        f.write(f"  Rotational Speed (N):  {design.requirements.rpm:.0f} RPM\n")
        omega = 2.0 * math.pi * design.requirements.rpm / 60.0
        f.write(f"  Angular Velocity (w):  {omega:.2f} rad/s\n\n")
        
        f.write("ESTIMATED PERFORMANCE:\n")
        f.write(f"  Specific Speed (Nq):   {design.performance.specific_speed_nq:.1f} (metric)\n")
        f.write(f"  Total Efficiency:      {design.performance.total_efficiency:.1f} %\n")
        f.write(f"  Hydraulic Efficiency:  {design.performance.hydraulic_efficiency:.1f} %\n")
        f.write(f"  Shaft Power:           {design.performance.shaft_power_kw:.2f} kW\n")
        closed_head = (
            design.performance.hydraulic_efficiency / 100.0
            * design.impeller.u2
            * design.impeller.c2u
            / 9.80665
        )
        f.write(f"  Estimated NPSHr:       {design.performance.npsh_required_m:.2f} m\n")
        f.write(f"  Velocity-triangle head closure: {closed_head:.2f} m\n\n")
        
        f.write("GEOMETRY SIZING:\n")
        f.write(f"  Impeller Configuration: {design.impeller.configuration}\n")
        f.write(f"  Impeller Eye (Ds):     {design.impeller.suction_diameter_ds:.1f} mm\n")
        f.write(f"  Impeller Hub (Dh):     {design.impeller.hub_diameter_dh:.1f} mm\n")
        f.write(f"  Impeller Outlet (D2):  {design.impeller.outlet_diameter_d2:.1f} mm\n")
        f.write(f"  Outlet Width (b2):     {design.impeller.outlet_width_b2:.1f} mm\n")
        f.write(f"  Blade Count (Z):       {design.impeller.blade_count_z}\n")
        f.write(f"  Blade Inlet Angle:     {design.impeller.blade_inlet_angle_beta1:.1f} °\n")
        f.write(f"  Blade Outlet Angle:    {design.impeller.blade_outlet_angle_beta2:.1f} °\n")
        f.write(
            "  Blade Edge Shape:      "
            f"{design.impeller.blade_leading_edge_shape}, axis ratio "
            f"{design.impeller.blade_edge_axis_ratio:.2f}\n"
        )
        f.write(
            "  Blade Edge Radius:     "
            f"LE {design.impeller.blade_leading_edge_radius:.3f} mm / "
            f"TE {design.impeller.blade_trailing_edge_radius:.3f} mm\n"
        )
        f.write(
            "  Minimum Blade Throat:  "
            f"{design.blade_passage.minimum_throat_distance_mm:.3f} mm\n"
        )
        f.write(
            "  Blade Throat Area:     "
            f"{design.blade_passage.throat_area_mm2:.3f} mm^2\n"
        )
        f.write(
            "  Passage Area Step Max: "
            f"{design.blade_passage.maximum_adjacent_area_change_percent:.3f} %\n"
        )
        if export_diffuser and uses_volute:
            f.write(f"  Volute Inlet (D4):     {design.volute.inlet_diameter_d4:.1f} mm\n")
            f.write(f"  Volute Width (b4):     {design.volute.inlet_width_b4:.1f} mm\n")
            f.write(f"  Volute Wrap:           {design.volute.wrap_angle_deg:.1f} deg\n")
            f.write(
                f"  Cutwater Thickness:    {design.volute.cutwater_thickness_mm:.1f} mm\n"
            )
            f.write(
                "  Discharge Diffuser:    "
                f"Aout/Ain={design.volute.discharge_area_ratio:.3f}, "
                f"cone={design.volute.discharge_cone_angle_deg:.2f} deg\n\n"
            )
        elif export_diffuser:
            f.write(f"  Diffuser Inlet (D3):   {design.diffuser.inlet_diameter_d3:.1f} mm\n")
            f.write(f"  Diffuser Outlet (D4):  {design.diffuser.outlet_diameter_d4:.1f} mm\n")
            f.write(f"  Diffuser Vane Count:   {design.diffuser.vane_count_zd}\n")
            f.write(f"  Diffuser Vane Inlet:   {design.diffuser.vane_inlet_angle_beta3:.1f} °\n\n")
        elif design.architecture:
            f.write(f"  Stationary CAD Scope:  {design.architecture.cad_scope}\n\n")
        
        warning_checks = [
            check
            for check in design.engineering_record.checks
            if check.status != "pass"
        ]
        f.write("ENGINEERING CHECKS:\n")
        f.write(
            f"  Passed: {len(design.engineering_record.checks) - len(warning_checks)}; "
            f"Warnings: {len(warning_checks)}\n"
        )
        for check in warning_checks:
            f.write(f"  [{check.status.upper()}] {check.key}: {check.message}\n")
        f.write("\n")

        f.write("OPENFOAM GEOMETRY NOTES:\n")
        f.write("  STL coordinate units: metres (STEP model units remain millimetres).\n")
        if cfd_definition:
            f.write(
                "  A closed rotating fluid domain and "
                f"{cfd_definition.stationary_domain_type} are included.\n"
            )
            f.write(
                "  RSI: cylindrical mid-gap interface at r = "
                f"{cfd_definition.rsi_radius_mm:.3f} mm.\n"
            )
            f.write(
                "  Named surface patches: "
                + ", ".join(sorted(cfd_boundary_files))
                + ".\n"
            )
            f.write(
                "  1. The generated steady-MRF case uses one continuous mesh and "
                "omits the two RSI surfaces.\n"
            )
            f.write(
                "  2. Keep rotor_rsi/stationary_rsi for the later matched cyclicAMI "
                "transient case.\n"
            )
            f.write(f"  3. The rotor cell zone uses omega = (0 0 {omega:.2f}) rad/s.\n")
            f.write("  4. Apply the inlet condition on rotor_inlet.\n")
            if uses_volute:
                f.write("  5. Apply the pressure outlet condition on stationary_outlet.\n")
                f.write("  6. Run checkMesh before solving.\n")
            else:
                f.write("  5. Add the downstream discharge collector before solving.\n")
        else:
            f.write("  These files contain material geometry only, not a complete fluid domain.\n")
            f.write("  Create inlet, outlet, casing, and rotating-zone boundaries separately.\n")
        f.write("=================================================================\n")

    exported_files["openfoam_summary"] = of_summary_path

    engineering_record_path = os.path.join(output_dir, "engineering_record.json")
    with open(engineering_record_path, "w", encoding="utf-8") as record_file:
        json.dump(design.engineering_record.to_dict(), record_file, indent=2)
        record_file.write("\n")
    exported_files["engineering_record"] = engineering_record_path

    stationary_boundary_manifest = {}
    if cfd_definition:
        outlet_patch = (
            "stationary_outlet" if uses_volute else "stationary_connection_outlet"
        )
        walls_patch = (
            "stationary_walls" if uses_volute else "stationary_connection_walls"
        )
        stationary_boundary_manifest = {
            "stationary_rsi": {
                "file": cfd_boundary_files["stationary_rsi"],
                "frame": "stationary domain",
                "suggested_type": "cyclicAMI",
            },
            outlet_patch: {
                "file": cfd_boundary_files[outlet_patch],
                "frame": "stationary domain",
                "suggested_type": "patch",
            },
            walls_patch: {
                "file": cfd_boundary_files[walls_patch],
                "frame": "stationary domain",
                "suggested_type": "wall",
            },
        }

    manifest_path = os.path.join(output_dir, "geometry_manifest.json")
    manifest = {
        "schema_version": 7,
        "design_id": design.design_id,
        "package_type": (
            (
                "openfoam_complete_single_stage_volute_domain"
                if uses_volute
                else "openfoam_rotating_domain_with_rsi"
            )
            if cfd_definition
            else "openfoam_material_geometry_only"
        ),
        "stl_coordinate_unit": "m",
        "step_model_unit": "mm",
        "connected_solid_validation": True,
        "operating_point": {
            "head_m": design.requirements.head_m,
            "flow_m3_s": design.requirements.discharge_m3_h / 3600.0,
            "rpm": design.requirements.rpm,
            "omega_rad_s": omega,
        },
        "impeller_configuration": design.impeller.configuration,
        "main_dimension_correlations": {
            "intake_coefficient_epsilon": design.impeller.intake_coefficient_epsilon,
            "outlet_width_ratio_b2_d2": design.impeller.outlet_width_ratio_b2_d2,
            "meridional_deceleration_ratio_cm2_cm1": (
                design.impeller.meridional_deceleration_ratio
            ),
            "slip_factor_sigma": design.impeller.slip_factor_sigma,
        },
        "generated_components": {
            "impeller": bool(export_impeller),
            "single_volute_casing": bool(export_diffuser and uses_volute),
            "vaned_radial_diffuser": bool(export_diffuser and not uses_volute),
            "rotating_fluid_domain": bool(cfd_definition),
            "stationary_volute_fluid_domain": bool(cfd_definition and uses_volute),
            "stationary_rsi_connection": bool(cfd_definition and not uses_volute),
        },
        "volute_geometry": asdict(design.volute) if uses_volute else None,
        "cfd_domain": (
            {
                "scope": (
                    "Closed rotating impeller fluid volume plus a connected stationary "
                    + (
                        "single-volute, cutwater, discharge diffuser and outlet extension."
                        if uses_volute
                        else "RSI connection; downstream discharge collector is pending."
                    )
                ),
                "definition": cfd_definition.to_dict(),
                "rotating_volume_file": os.path.basename(
                    exported_files["rotating_flow_domain_stl"]
                ),
                "stationary_domain_file": os.path.basename(
                    exported_files[f"{stationary_basename}_stl"]
                ),
                "interface_pair": {
                    "name": "rotor_stator_interface_1",
                    "method": "cyclicAMI (reserved for future transient case)",
                    "rotating_patch": "rotor_rsi",
                    "stationary_patch": "stationary_rsi",
                    "geometric_match": "shared cylindrical surface",
                },
                "boundary_patches": {
                    "rotor_inlet": {
                        "file": cfd_boundary_files["rotor_inlet"],
                        "frame": "rotating domain",
                        "suggested_type": "patch",
                    },
                    "rotor_rsi": {
                        "file": cfd_boundary_files["rotor_rsi"],
                        "frame": "rotating domain",
                        "suggested_type": "cyclicAMI",
                    },
                    "rotor_walls": {
                        "file": cfd_boundary_files["rotor_walls"],
                        "frame": "rotating domain",
                        "suggested_type": "wall",
                    },
                    "virtual_inlet_walls": {
                        "file": cfd_boundary_files["virtual_inlet_walls"],
                        "frame": "stationary virtual inlet extension",
                        "suggested_type": "wall",
                    },
                    **stationary_boundary_manifest,
                },
                "limitations": [
                    *(
                        []
                        if solver_case_info
                        else ["No OpenFOAM solver case was generated."]
                    ),
                    "Leakage and secondary shroud-side flow paths are omitted.",
                    *(
                        []
                        if uses_volute
                        else ["The downstream discharge collector is not included."]
                    ),
                ],
            }
            if cfd_definition
            else None
        ),
        "solver_case": (
            {
                "directory": os.path.basename(solver_case_info["case_directory"]),
                "manifest": os.path.relpath(
                    solver_case_info["manifest_path"], output_dir
                ).replace("\\", "/"),
                "formulation": solver_case_info["formulation"],
                "solver": solver_case_info["solver"],
                "target": solver_case_info["target"],
                "rotating_cell_zone": solver_case_info["rotating_cell_zone"],
                "excluded_internal_surfaces": solver_case_info[
                    "excluded_internal_surfaces"
                ],
            }
            if solver_case_info
            else None
        ),
        "pump_architecture": (
            {
                "machine_configuration": design.architecture.machine_configuration,
                "stage_count": design.architecture.stage_count,
                "stage_head_fractions": list(
                    design.architecture.stage_head_fractions
                ),
                "requested_flow_type": design.architecture.requested_flow_type,
                "resolved_flow_types": list(design.architecture.resolved_flow_types),
                "single_stage_collector": design.architecture.single_stage_collector,
                "interstage_return_type": design.architecture.interstage_return_type,
                "final_collector": design.architecture.final_collector,
                "component_sequence": list(design.architecture.component_sequence),
                "cad_scope": design.architecture.cad_scope,
                "complete_assembly_cad": design.architecture.has_complete_assembly_cad,
            }
            if design.architecture
            else None
        ),
        "meridional_geometry": design.meridional.to_dict(),
        "blade_edge_geometry": {
            "leading_shape": design.impeller.blade_leading_edge_shape,
            "trailing_shape": design.impeller.blade_trailing_edge_shape,
            "leading_radius_mm": design.impeller.blade_leading_edge_radius,
            "trailing_radius_mm": design.impeller.blade_trailing_edge_radius,
            "axis_ratio": design.impeller.blade_edge_axis_ratio,
        },
        "blade_passage_validation": design.blade_passage.to_dict(),
        "exact_neighbor_cad_overlap_validation": bool(export_impeller),
        "engineering_record": os.path.basename(engineering_record_path),
        "engineering_check_summary": {
            "pass": sum(
                check.status == "pass" for check in design.engineering_record.checks
            ),
            "warning": sum(
                check.status == "warning" for check in design.engineering_record.checks
            ),
            "fail": sum(
                check.status == "fail" for check in design.engineering_record.checks
            ),
        },
        "warning": (
            (
                "The complete preliminary single-stage volute fluid domain and a "
                "steady-MRF OpenFOAM case are included; mesh verification and solved "
                "CFD results remain pending."
                if uses_volute
                else "Rotating domain and RSI connection are complete, but the downstream "
                "collector, mesh, boundary-condition dictionaries, and solver case are pending."
            )
            if cfd_definition
            else "Not a complete OpenFOAM fluid domain or solver case."
        ),
    }
    with open(manifest_path, "w", encoding="utf-8") as manifest_file:
        json.dump(manifest, manifest_file, indent=2)
        manifest_file.write("\n")
    exported_files["geometry_manifest"] = manifest_path
    return exported_files
