"""Interactive 2D editor for the impeller primary meridional flow path."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.meridional import (
    MeridionalDesign,
    MeridionalOverride,
    bezier_point,
    create_edited_meridional_design,
    sample_bezier,
)


class MeridionalCanvas(QWidget):
    geometry_changed = Signal()
    edit_started = Signal()
    selection_changed = Signal(str, int)

    def __init__(self, design: MeridionalDesign, parent=None):
        super().__init__(parent)
        self.setMinimumSize(700, 520)
        self.setMouseTracking(True)
        self.hub_points = [list(point) for point in design.hub_control_points_rz]
        self.shroud_points = [list(point) for point in design.shroud_control_points_rz]
        self.leading_edge_hub_fraction = design.leading_edge_hub_fraction
        self.leading_edge_shroud_fraction = design.leading_edge_shroud_fraction
        self._drag_target: tuple[str, int] | None = None

    def set_geometry(
        self,
        hub_points,
        shroud_points,
        leading_edge_hub_fraction: float,
        leading_edge_shroud_fraction: float,
    ) -> None:
        self.hub_points = [list(point) for point in hub_points]
        self.shroud_points = [list(point) for point in shroud_points]
        self.leading_edge_hub_fraction = float(leading_edge_hub_fraction)
        self.leading_edge_shroud_fraction = float(leading_edge_shroud_fraction)
        self.update()
        self.geometry_changed.emit()

    def control_points(self, curve: str):
        return self.hub_points if curve == "Hub" else self.shroud_points

    def _plot_rect(self) -> QRectF:
        return QRectF(62.0, 28.0, max(100.0, self.width() - 92.0), max(100.0, self.height() - 88.0))

    def _data_bounds(self) -> tuple[float, float, float, float]:
        points = self.hub_points + self.shroud_points
        radii = [point[0] for point in points]
        axials = [point[1] for point in points]
        radial_span = max(radii) - min(radii)
        axial_span = max(axials) - min(axials)
        return (
            max(0.0, min(radii) - 0.10 * radial_span),
            max(radii) + 0.08 * radial_span,
            min(axials) - 0.10 * max(1.0, axial_span),
            max(axials) + 0.08 * max(1.0, axial_span),
        )

    def _to_screen(self, point) -> QPointF:
        r_min, r_max, z_min, z_max = self._data_bounds()
        rect = self._plot_rect()
        x_coord = rect.left() + (point[0] - r_min) / (r_max - r_min) * rect.width()
        y_coord = rect.bottom() - (point[1] - z_min) / (z_max - z_min) * rect.height()
        return QPointF(x_coord, y_coord)

    def _from_screen(self, point: QPointF) -> tuple[float, float]:
        r_min, r_max, z_min, z_max = self._data_bounds()
        rect = self._plot_rect()
        radius = r_min + (point.x() - rect.left()) / rect.width() * (r_max - r_min)
        axial = z_min + (rect.bottom() - point.y()) / rect.height() * (z_max - z_min)
        return radius, axial

    def _polyline(self, painter: QPainter, points, color: str, width: float) -> None:
        painter.setPen(QPen(QColor(color), width))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPolyline(QPolygonF([self._to_screen(point) for point in points]))

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#f4f5f6"))
        rect = self._plot_rect()

        painter.setPen(QPen(QColor("#d2d5d9"), 1.0))
        for index in range(11):
            x_coord = rect.left() + index / 10.0 * rect.width()
            y_coord = rect.top() + index / 10.0 * rect.height()
            painter.drawLine(QPointF(x_coord, rect.top()), QPointF(x_coord, rect.bottom()))
            painter.drawLine(QPointF(rect.left(), y_coord), QPointF(rect.right(), y_coord))

        hub_curve = sample_bezier(tuple(map(tuple, self.hub_points)), 100)
        shroud_curve = sample_bezier(tuple(map(tuple, self.shroud_points)), 100)
        passage = QPolygonF(
            [self._to_screen(point) for point in shroud_curve]
            + [self._to_screen(point) for point in reversed(hub_curve)]
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(216, 226, 218, 175))
        painter.drawPolygon(passage)

        control_pen = QPen(QColor("#8c9299"), 1.0, Qt.PenStyle.DashLine)
        painter.setPen(control_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPolyline(QPolygonF([self._to_screen(point) for point in self.hub_points]))
        painter.drawPolyline(QPolygonF([self._to_screen(point) for point in self.shroud_points]))

        self._polyline(painter, hub_curve, "#353a40", 3.0)
        self._polyline(painter, shroud_curve, "#2f7d4c", 3.0)

        leading_hub = bezier_point(
            tuple(map(tuple, self.hub_points)), self.leading_edge_hub_fraction
        )
        leading_shroud = bezier_point(
            tuple(map(tuple, self.shroud_points)), self.leading_edge_shroud_fraction
        )
        painter.setPen(QPen(QColor("#d99a16"), 2.2))
        painter.drawLine(self._to_screen(leading_hub), self._to_screen(leading_shroud))
        painter.setPen(QPen(QColor("#b24d35"), 2.2))
        painter.drawLine(
            self._to_screen(self.hub_points[-1]),
            self._to_screen(self.shroud_points[-1]),
        )

        painter.setFont(QFont("Segoe UI", 8))
        for curve_name, points, color in (
            ("H", self.hub_points, "#353a40"),
            ("S", self.shroud_points, "#2f7d4c"),
        ):
            for index, point in enumerate(points):
                screen = self._to_screen(point)
                painter.setPen(QPen(QColor(color), 1.5))
                painter.setBrush(QColor("#ffffff"))
                if index in (0, 4):
                    painter.drawRect(QRectF(screen.x() - 4.5, screen.y() - 4.5, 9.0, 9.0))
                else:
                    painter.drawEllipse(screen, 5.0, 5.0)
                painter.drawText(screen + QPointF(7.0, -6.0), f"{curve_name}{index}")

        painter.setPen(QPen(QColor("#343a40"), 1.2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect)
        painter.setFont(QFont("Segoe UI", 9))
        painter.drawText(
            QRectF(rect.left(), rect.bottom() + 24.0, rect.width(), 20.0),
            Qt.AlignmentFlag.AlignCenter,
            "Radius r [mm]",
        )
        painter.drawText(8.0, 20.0, "Axial z [mm]")
        painter.setPen(QPen(QColor("#d99a16"), 2.0))
        painter.drawText(rect.right() - 128.0, rect.top() + 18.0, "Leading edge")
        painter.setPen(QPen(QColor("#b24d35"), 2.0))
        painter.drawText(rect.right() - 128.0, rect.top() + 36.0, "Trailing edge")
        painter.end()

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        nearest = None
        nearest_distance = 12.0
        for curve_name, points in (("Hub", self.hub_points), ("Shroud", self.shroud_points)):
            for index in (1, 2, 3):
                screen = self._to_screen(points[index])
                distance = ((screen.x() - event.position().x()) ** 2 + (screen.y() - event.position().y()) ** 2) ** 0.5
                if distance < nearest_distance:
                    nearest = (curve_name, index)
                    nearest_distance = distance
        if nearest is not None:
            self._drag_target = nearest
            self.edit_started.emit()
            self.selection_changed.emit(*nearest)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_target is None:
            return
        curve_name, index = self._drag_target
        radius, axial = self._from_screen(event.position())
        self.set_control_point(curve_name, index, radius, axial)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_target = None

    def set_control_point(
        self, curve_name: str, index: int, radius: float, axial: float
    ) -> None:
        if index not in (1, 2, 3):
            return
        points = self.control_points(curve_name)
        radial_span = points[4][0] - points[0][0]
        axial_span = points[0][1] - points[4][1]
        radial_epsilon = max(0.05, 0.004 * radial_span)
        axial_epsilon = max(0.05, 0.004 * axial_span)
        if index == 1:
            radius = points[0][0]
            axial = min(
                points[0][1] - axial_epsilon,
                max(points[2][1] + axial_epsilon, axial),
            )
        elif index == 3:
            axial = points[4][1]
            radius = min(
                points[4][0] - radial_epsilon,
                max(points[2][0] + radial_epsilon, radius),
            )
        else:
            radius = min(
                points[4][0] - 2.0 * radial_epsilon,
                max(points[0][0] + 2.0 * radial_epsilon, radius),
            )
            axial = min(
                points[0][1] - 2.0 * axial_epsilon,
                max(points[4][1] + 2.0 * axial_epsilon, axial),
            )
            points[1][1] = min(
                points[0][1] - axial_epsilon,
                max(axial + axial_epsilon, points[1][1]),
            )
            points[3][0] = min(
                points[4][0] - radial_epsilon,
                max(radius + radial_epsilon, points[3][0]),
            )
        points[index] = [radius, axial]
        self.update()
        self.geometry_changed.emit()


class MeridionalEditorDialog(QDialog):
    """Modal editor with precise control-point input and live engineering checks."""

    def __init__(
        self,
        current: MeridionalDesign,
        automatic: MeridionalDesign,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("2D Meridional Profile Editor")
        self.resize(1120, 700)
        self.automatic = automatic
        self._history = []
        self._syncing = False
        self._is_automatic = current.curve_type == automatic.curve_type and (
            current.hub_control_points_rz == automatic.hub_control_points_rz
            and current.shroud_control_points_rz == automatic.shroud_control_points_rz
        )
        self.edited_design: MeridionalDesign | None = None

        root = QVBoxLayout(self)
        content = QHBoxLayout()
        root.addLayout(content, stretch=1)
        self.canvas = MeridionalCanvas(current)
        content.addWidget(self.canvas, stretch=1)

        side = QVBoxLayout()
        content.addLayout(side)
        instructions = QLabel(
            "Drag P1-P3 or enter coordinates. P0/P4 are locked by hydraulic sizing. "
            "P1 keeps the axial inlet tangent; P3 keeps the radial outlet tangent."
        )
        instructions.setWordWrap(True)
        instructions.setMinimumWidth(315)
        side.addWidget(instructions)

        point_group = QGroupBox("Selected control point")
        point_form = QFormLayout(point_group)
        self.combo_curve = QComboBox()
        self.combo_curve.addItems(("Hub", "Shroud"))
        self.combo_point = QComboBox()
        self.combo_point.addItems(tuple(f"P{index}" for index in range(5)))
        self.spin_radius = QDoubleSpinBox()
        self.spin_radius.setRange(-10000.0, 10000.0)
        self.spin_radius.setDecimals(3)
        self.spin_radius.setSuffix(" mm")
        self.spin_axial = QDoubleSpinBox()
        self.spin_axial.setRange(-10000.0, 10000.0)
        self.spin_axial.setDecimals(3)
        self.spin_axial.setSuffix(" mm")
        point_form.addRow("Curve:", self.combo_curve)
        point_form.addRow("Point:", self.combo_point)
        point_form.addRow("Radius r:", self.spin_radius)
        point_form.addRow("Axial z:", self.spin_axial)
        side.addWidget(point_group)

        edge_group = QGroupBox("Blade-edge placement")
        edge_form = QFormLayout(edge_group)
        self.spin_le_hub = QDoubleSpinBox()
        self.spin_le_shroud = QDoubleSpinBox()
        for spin in (self.spin_le_hub, self.spin_le_shroud):
            spin.setRange(0.02, 0.80)
            spin.setDecimals(4)
            spin.setSingleStep(0.01)
        self.spin_le_hub.setValue(current.leading_edge_hub_fraction)
        self.spin_le_shroud.setValue(current.leading_edge_shroud_fraction)
        edge_form.addRow("LE hub fraction:", self.spin_le_hub)
        edge_form.addRow("LE shroud fraction:", self.spin_le_shroud)
        side.addWidget(edge_group)

        check_group = QGroupBox("Live hydraulic checks")
        check_layout = QVBoxLayout(check_group)
        self.lbl_checks = QLabel()
        self.lbl_checks.setWordWrap(True)
        self.lbl_checks.setTextFormat(Qt.TextFormat.RichText)
        check_layout.addWidget(self.lbl_checks)
        side.addWidget(check_group)
        side.addStretch()

        actions = QHBoxLayout()
        self.btn_reset = QPushButton("Reset to calculated")
        self.btn_undo = QPushButton("Undo")
        self.btn_cancel = QPushButton("Cancel")
        self.btn_apply = QPushButton("Apply and regenerate 3D")
        self.btn_apply.setDefault(True)
        actions.addWidget(self.btn_reset)
        actions.addWidget(self.btn_undo)
        actions.addStretch()
        actions.addWidget(self.btn_cancel)
        actions.addWidget(self.btn_apply)
        root.addLayout(actions)

        self.canvas.edit_started.connect(self._push_history)
        self.canvas.geometry_changed.connect(self._on_geometry_changed)
        self.canvas.selection_changed.connect(self._select_point)
        self.combo_curve.currentTextChanged.connect(self._sync_point_fields)
        self.combo_point.currentIndexChanged.connect(self._sync_point_fields)
        self.spin_radius.editingFinished.connect(self._apply_numeric_point)
        self.spin_axial.editingFinished.connect(self._apply_numeric_point)
        self.spin_le_hub.editingFinished.connect(self._apply_le_fractions)
        self.spin_le_shroud.editingFinished.connect(self._apply_le_fractions)
        self.btn_reset.clicked.connect(self._reset_automatic)
        self.btn_undo.clicked.connect(self._undo)
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_apply.clicked.connect(self._accept_validated)

        self.combo_point.setCurrentIndex(2)
        self._sync_point_fields()
        self._validate_live()
        self._update_undo_state()

    def _snapshot(self):
        return (
            tuple(tuple(point) for point in self.canvas.hub_points),
            tuple(tuple(point) for point in self.canvas.shroud_points),
            self.canvas.leading_edge_hub_fraction,
            self.canvas.leading_edge_shroud_fraction,
            self._is_automatic,
        )

    def _push_history(self) -> None:
        snapshot = self._snapshot()
        if not self._history or self._history[-1] != snapshot:
            self._history.append(snapshot)
        self._is_automatic = False
        self._update_undo_state()

    def _update_undo_state(self) -> None:
        self.btn_undo.setEnabled(bool(self._history))

    def _restore_snapshot(self, snapshot) -> None:
        hub, shroud, le_hub, le_shroud, automatic = snapshot
        self._is_automatic = automatic
        self._syncing = True
        self.canvas.set_geometry(hub, shroud, le_hub, le_shroud)
        self.spin_le_hub.setValue(le_hub)
        self.spin_le_shroud.setValue(le_shroud)
        self._syncing = False
        self._sync_point_fields()
        self._validate_live()

    def _undo(self) -> None:
        if self._history:
            self._restore_snapshot(self._history.pop())
        self._update_undo_state()

    def _reset_automatic(self) -> None:
        self._push_history()
        self._is_automatic = True
        self._syncing = True
        self.canvas.set_geometry(
            self.automatic.hub_control_points_rz,
            self.automatic.shroud_control_points_rz,
            self.automatic.leading_edge_hub_fraction,
            self.automatic.leading_edge_shroud_fraction,
        )
        self.spin_le_hub.setValue(self.automatic.leading_edge_hub_fraction)
        self.spin_le_shroud.setValue(self.automatic.leading_edge_shroud_fraction)
        self._syncing = False
        self._sync_point_fields()
        self._validate_live()

    def _select_point(self, curve_name: str, index: int) -> None:
        self._syncing = True
        self.combo_curve.setCurrentText(curve_name)
        self.combo_point.setCurrentIndex(index)
        self._syncing = False
        self._sync_point_fields()

    def _sync_point_fields(self, *_args) -> None:
        if self._syncing:
            return
        curve_name = self.combo_curve.currentText()
        index = self.combo_point.currentIndex()
        point = self.canvas.control_points(curve_name)[index]
        self._syncing = True
        self.spin_radius.setValue(point[0])
        self.spin_axial.setValue(point[1])
        self.spin_radius.setEnabled(index not in (0, 1, 4))
        self.spin_axial.setEnabled(index not in (0, 3, 4))
        self._syncing = False

    def _apply_numeric_point(self) -> None:
        if self._syncing:
            return
        index = self.combo_point.currentIndex()
        if index in (0, 4):
            return
        self._push_history()
        self.canvas.set_control_point(
            self.combo_curve.currentText(),
            index,
            self.spin_radius.value(),
            self.spin_axial.value(),
        )
        self._sync_point_fields()

    def _apply_le_fractions(self) -> None:
        if self._syncing:
            return
        self._push_history()
        self.canvas.leading_edge_hub_fraction = self.spin_le_hub.value()
        self.canvas.leading_edge_shroud_fraction = self.spin_le_shroud.value()
        self.canvas.update()
        self.canvas.geometry_changed.emit()

    def _on_geometry_changed(self) -> None:
        if not self._syncing:
            self._is_automatic = False
        self._sync_point_fields()
        self._validate_live()

    def _current_override(self) -> MeridionalOverride:
        return MeridionalOverride(
            hub_control_points_rz=tuple(
                tuple(point) for point in self.canvas.hub_points
            ),
            shroud_control_points_rz=tuple(
                tuple(point) for point in self.canvas.shroud_points
            ),
            leading_edge_hub_fraction=self.canvas.leading_edge_hub_fraction,
            leading_edge_shroud_fraction=self.canvas.leading_edge_shroud_fraction,
        )

    def _validate_live(self) -> None:
        try:
            edited = create_edited_meridional_design(
                self.automatic, self._current_override()
            )
            self.edited_design = edited
            warnings = []
            if edited.area_uniformity_ratio > 1.35:
                warnings.append("passage-area variation exceeds 1.35")
            if edited.static_moment_imbalance_percent > 25.0:
                warnings.append("hub/shroud static moments differ by more than 25%")
            verdict = (
                '<span style="color:#b45309"><b>VALID WITH ADVISORY</b></span>'
                if warnings
                else '<span style="color:#15803d"><b>VALID</b></span>'
            )
            advisory = "<br>Advisory: " + "; ".join(warnings) if warnings else ""
            self.lbl_checks.setText(
                f"{verdict}<br>"
                f"Minimum channel height: <b>{edited.minimum_channel_height_mm:.2f} mm</b><br>"
                f"Outlet alignment: <b>parallel radial boundaries (locked)</b><br>"
                f"Passage area max/min: <b>{edited.area_uniformity_ratio:.3f}</b><br>"
                f"Minimum curvature radius - hub/shroud: "
                f"<b>{edited.minimum_hub_curvature_radius_mm:.2f} / "
                f"{edited.minimum_shroud_curvature_radius_mm:.2f} mm</b><br>"
                f"Static-moment imbalance: "
                f"<b>{edited.static_moment_imbalance_percent:.1f}%</b>"
                f"{advisory}"
            )
            self.btn_apply.setEnabled(True)
        except ValueError as exc:
            self.edited_design = None
            self.lbl_checks.setText(
                f'<span style="color:#b91c1c"><b>INVALID</b></span><br>{exc}'
            )
            self.btn_apply.setEnabled(False)

    def _accept_validated(self) -> None:
        self._validate_live()
        if self.edited_design is not None:
            self.accept()

    def result_override(self) -> MeridionalOverride | None:
        return None if self._is_automatic else self._current_override()
