"""Property dialog for CSV Plotter's VTK view."""
from __future__ import annotations

from copy import deepcopy

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox,
    QTabWidget, QVBoxLayout, QWidget,
)

from plot_property_dialog import ColorButton


class VtkPropertyDialog(QDialog):
    def __init__(self, vtk_config, dataset, apply_callback, parent=None):
        super().__init__(parent)
        self.setWindowTitle("3D Properties")
        self.resize(620, 660)
        self.config = deepcopy(vtk_config)
        self.dataset = deepcopy(dataset) if dataset is not None else None
        self.apply_callback = apply_callback
        root = QVBoxLayout(self); tabs = QTabWidget(); root.addWidget(tabs, 1)
        tabs.addTab(self._build_dataset_page(), "Dataset")
        tabs.addTab(self._build_scene_page(), "Scene")
        tabs.addTab(self._build_axes_page(), "Axes")
        buttons = QHBoxLayout(); root.addLayout(buttons); buttons.addStretch()
        apply_btn = QPushButton("Apply"); ok_btn = QPushButton("OK"); cancel_btn = QPushButton("Cancel")
        buttons.addWidget(apply_btn); buttons.addWidget(ok_btn); buttons.addWidget(cancel_btn)
        apply_btn.clicked.connect(self._apply); ok_btn.clicked.connect(self._accept); cancel_btn.clicked.connect(self.reject)
        self._load()

    @staticmethod
    def _double(minimum=-1e12, maximum=1e12, decimals=6):
        widget = QDoubleSpinBox(); widget.setRange(minimum, maximum); widget.setDecimals(decimals); widget.setKeyboardTracking(False)
        return widget

    def _build_dataset_page(self):
        page = QWidget(); form = QFormLayout(page)
        self.dataset_name = QLabel("No active dataset")
        self.mode = QComboBox(); self.mode.addItems(["Surface", "Mesh", "Scatter"])
        self.color_mode = QComboBox(); self.color_mode.addItems(["Fixed Color", "Z Colormap"])
        self.color = ColorButton("#1f77b4")
        self.cmap = QComboBox(); self.cmap.addItems(["Viridis", "Plasma", "Coolwarm", "Rainbow", "Grayscale"])
        self.auto_range = QCheckBox("Auto"); self.range_min = self._double(); self.range_max = self._double()
        self.opacity = self._double(0, 1, 2); self.point_size = self._double(1, 30, 1)
        self.mesh_color = ColorButton("#202020"); self.mesh_width = self._double(0.1, 10, 2)
        for label, widget in (("Active dataset:", self.dataset_name), ("Mode:", self.mode),
                              ("Color mode:", self.color_mode), ("Fixed color:", self.color),
                              ("Colormap:", self.cmap), ("Color range:", self.auto_range),
                              ("Range minimum:", self.range_min), ("Range maximum:", self.range_max),
                              ("Opacity:", self.opacity), ("Point size:", self.point_size),
                              ("Mesh color:", self.mesh_color), ("Mesh width:", self.mesh_width)):
            form.addRow(label, widget)
        return page

    def _build_scene_page(self):
        page = QWidget(); form = QFormLayout(page)
        self.background = QComboBox(); self.background.addItems(["White", "Light Gray", "Gray", "Dark Gray", "Black"])
        self.show_axes = QCheckBox("Show cube axes"); self.show_colorbar = QCheckBox("Show active colorbar")
        self.show_legend = QCheckBox("Show dataset legend")
        self.auto_normalize = QCheckBox("Normalize union ranges")
        self.x_scale = self._double(0.001, 1000, 3); self.y_scale = self._double(0.001, 1000, 3); self.z_scale = self._double(0.001, 1000, 3)
        self.screenshot_scale = QSpinBox(); self.screenshot_scale.setRange(1, 8)
        form.addRow("Background:", self.background); form.addRow("Axes:", self.show_axes)
        form.addRow("Colorbar:", self.show_colorbar); form.addRow("Legend:", self.show_legend)
        form.addRow("Auto visual normalization:", self.auto_normalize)
        form.addRow("X visual factor:", self.x_scale); form.addRow("Y visual factor:", self.y_scale); form.addRow("Z visual factor:", self.z_scale)
        form.addRow("Screenshot scale:", self.screenshot_scale)
        return page

    def _build_axes_page(self):
        page = QWidget(); form = QFormLayout(page)
        self.x_title = QLineEdit(); self.y_title = QLineEdit(); self.z_title = QLineEdit()
        self.text_color = ColorButton("#000000")
        self.title_size = QSpinBox(); self.title_size.setRange(6, 48)
        self.label_size = QSpinBox(); self.label_size.setRange(6, 36)
        self.auto_bounds = QCheckBox("Auto from visible data")
        self.x_min = self._double(); self.x_max = self._double(); self.y_min = self._double(); self.y_max = self._double(); self.z_min = self._double(); self.z_max = self._double()
        for label, widget in (("X title:", self.x_title), ("Y title:", self.y_title), ("Z title:", self.z_title),
                              ("Text color:", self.text_color), ("Title size:", self.title_size), ("Label size:", self.label_size),
                              ("Bounds:", self.auto_bounds), ("X min:", self.x_min), ("X max:", self.x_max),
                              ("Y min:", self.y_min), ("Y max:", self.y_max), ("Z min:", self.z_min), ("Z max:", self.z_max)):
            form.addRow(label, widget)
        return page

    def _load(self):
        c = self.config
        for widget, value in ((self.background, c.background),): widget.setCurrentText(value)
        self.show_axes.setChecked(c.show_axes); self.show_colorbar.setChecked(c.show_colorbar); self.show_legend.setChecked(c.show_legend)
        self.auto_normalize.setChecked(c.auto_normalize); self.x_scale.setValue(c.x_scale); self.y_scale.setValue(c.y_scale); self.z_scale.setValue(c.z_scale)
        self.screenshot_scale.setValue(c.screenshot_scale); self.x_title.setText(c.x_title); self.y_title.setText(c.y_title); self.z_title.setText(c.z_title)
        self.text_color.set_color(c.text_color); self.title_size.setValue(c.title_font_size); self.label_size.setValue(c.label_font_size)
        self.auto_bounds.setChecked(c.auto_bounds)
        for widget, value in ((self.x_min, c.x_min), (self.x_max, c.x_max), (self.y_min, c.y_min),
                              (self.y_max, c.y_max), (self.z_min, c.z_min), (self.z_max, c.z_max)): widget.setValue(value)
        if self.dataset is None:
            for widget in (self.mode, self.color_mode, self.color, self.cmap, self.auto_range, self.range_min,
                           self.range_max, self.opacity, self.point_size, self.mesh_color, self.mesh_width): widget.setEnabled(False)
            return
        d = self.dataset; self.dataset_name.setText(d.label)
        self.mode.setCurrentText(d.mode3d); self.color_mode.setCurrentText(d.color_mode); self.color.set_color(d.color)
        self.cmap.setCurrentText(d.colormap); self.auto_range.setChecked(d.auto_color_range)
        self.range_min.setValue(d.color_min); self.range_max.setValue(d.color_max); self.opacity.setValue(d.opacity)
        self.point_size.setValue(d.point_size); self.mesh_color.set_color(d.mesh_color); self.mesh_width.setValue(d.mesh_width)

    def _save(self):
        c = self.config; c.background = self.background.currentText(); c.show_axes = self.show_axes.isChecked()
        c.show_colorbar = self.show_colorbar.isChecked(); c.show_legend = self.show_legend.isChecked(); c.auto_normalize = self.auto_normalize.isChecked()
        c.x_scale = self.x_scale.value(); c.y_scale = self.y_scale.value(); c.z_scale = self.z_scale.value(); c.screenshot_scale = self.screenshot_scale.value()
        c.x_title = self.x_title.text(); c.y_title = self.y_title.text(); c.z_title = self.z_title.text(); c.text_color = self.text_color.color()
        c.title_font_size = self.title_size.value(); c.label_font_size = self.label_size.value(); c.auto_bounds = self.auto_bounds.isChecked()
        c.x_min = self.x_min.value(); c.x_max = self.x_max.value(); c.y_min = self.y_min.value(); c.y_max = self.y_max.value(); c.z_min = self.z_min.value(); c.z_max = self.z_max.value()
        if self.dataset is not None:
            d = self.dataset; d.mode3d = self.mode.currentText(); d.color_mode = self.color_mode.currentText(); d.color = self.color.color(); d.colormap = self.cmap.currentText()
            d.auto_color_range = self.auto_range.isChecked(); d.color_min = self.range_min.value(); d.color_max = self.range_max.value(); d.opacity = self.opacity.value()
            d.point_size = self.point_size.value(); d.mesh_color = self.mesh_color.color(); d.mesh_width = self.mesh_width.value()

    def _valid(self):
        self._save()
        c = self.config
        if not c.auto_bounds and not (c.x_min < c.x_max and c.y_min < c.y_max and c.z_min < c.z_max):
            QMessageBox.warning(self, "Invalid Bounds", "Each minimum must be smaller than its maximum."); return False
        if self.dataset is not None and not self.dataset.auto_color_range and self.dataset.color_min >= self.dataset.color_max:
            QMessageBox.warning(self, "Invalid Range", "Color minimum must be smaller than maximum."); return False
        return True

    def _apply(self):
        if self._valid(): self.apply_callback(deepcopy(self.config), deepcopy(self.dataset))

    def _accept(self):
        if self._valid(): self.apply_callback(deepcopy(self.config), deepcopy(self.dataset)); self.accept()
