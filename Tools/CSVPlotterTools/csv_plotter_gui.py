"""Multi-file 2D/3D CSV plotting dialog."""
from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd
import vtk
from PySide6.QtCore import Qt, QSettings
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFileDialog,
    QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton, QScrollArea,
    QSizePolicy, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qtagg import NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure
from vtkmodules.qt.QVTKRenderWindowInteractor import QVTKRenderWindowInteractor

from plot_config import FigureConfig, new_curve
from plot_property_dialog import PlotPropertyDialog
from .models import CsvDatasetConfig, CsvPlotterState, VtkPlotConfig, dataset_display_name, load_csv, numeric_series
from .rendering import render_shared_figure
from .vtk_properties import VtkPropertyDialog
from .vtk_utils import build_scatter, build_surface_with_holes, hex_to_rgb, make_lookup_table


STATE_KEY = "csv_plotter/state_v1"
TABLE_HEADERS = ["On", "Label", "File", "2D X", "2D Y", "3D X", "3D Y", "3D Z", "3D Mode", "Color Mode"]


class CSVPlotterDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("CSV Plotter")
        self.setWindowFlag(Qt.WindowType.Window, True)
        self._set_size(parent)
        self.settings = QSettings("MInDes", "MInDes-UI")
        self.state = self._load_state()
        figure_state = self.state.figure
        if not figure_state:
            try:
                figure_state = json.loads(self.settings.value("csv_plotter/default_2d_v1", "{}", type=str))
            except (TypeError, ValueError, json.JSONDecodeError):
                figure_state = {}
        self.figure_config = FigureConfig.from_dict(figure_state)
        self.vtk_config = self.state.vtk
        self.frames: dict[str, pd.DataFrame] = {}
        self._table_loading = False
        self._closing = False
        self._build_ui()
        self._restore_datasets()

    def _set_size(self, parent):
        screen = QGuiApplication.primaryScreen().availableGeometry()
        if parent is not None:
            geo = parent.geometry(); width = max(1100, min(int(geo.width() * .95), screen.width() - 30)); height = max(760, min(int(geo.height() * .95), screen.height() - 50))
            self.resize(width, height); self.move(max(screen.x(), geo.x() + (geo.width() - width) // 2), max(screen.y(), geo.y() + (geo.height() - height) // 2))
        else:
            self.resize(max(1100, int(screen.width() * .8)), max(760, int(screen.height() * .8)))

    def _load_state(self):
        try:
            raw = json.loads(self.settings.value(STATE_KEY, "{}", type=str))
        except (TypeError, ValueError, json.JSONDecodeError):
            raw = {}
        return CsvPlotterState.from_dict(raw)

    def _build_ui(self):
        root = QVBoxLayout(self); root.setContentsMargins(6, 6, 6, 6)
        controls = QHBoxLayout(); root.addLayout(controls)
        for text, slot in (("Add CSV...", self.add_csv), ("Remove", self.remove_selected),
                           ("Reload", self.reload_selected), ("Relocate...", self.relocate_selected)):
            button = QPushButton(text); button.clicked.connect(slot); controls.addWidget(button)
        controls.addStretch()

        self.table = QTableWidget(0, len(TABLE_HEADERS)); self.table.setHorizontalHeaderLabels(TABLE_HEADERS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False); self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.itemChanged.connect(self._sync_from_table); self.table.itemSelectionChanged.connect(self._active_changed)
        root.addWidget(self.table, 0)

        self.tabs = QTabWidget(); root.addWidget(self.tabs, 1)
        self._build_2d_tab(); self._build_3d_tab()
        self.status = QLabel("Add one or more CSV files to begin.")
        self.status.setStyleSheet("background:#f0f0f0;padding:4px;border-top:1px solid #bbb;")
        root.addWidget(self.status)

    def _build_2d_tab(self):
        page = QWidget(); layout = QVBoxLayout(page); layout.setContentsMargins(0, 0, 0, 0)
        self.figure = Figure(figsize=(6.3, 3.94), dpi=100); self.canvas = FigureCanvas(self.figure)
        layout.addWidget(NavigationToolbar(self.canvas, page))
        self.plot_scroll = QScrollArea(); self.plot_scroll.setWidgetResizable(False); self.plot_scroll.setAlignment(Qt.AlignCenter); self.plot_scroll.setWidget(self.canvas)
        layout.addWidget(self.plot_scroll, 1)
        buttons = QHBoxLayout(); layout.addLayout(buttons)
        for text, slot in (("Draw 2D", self.draw_2d), ("Property", self.open_2d_property), ("Export Figure", self.export_2d)):
            button = QPushButton(text); button.clicked.connect(slot); buttons.addWidget(button)
        buttons.addStretch(); self.tabs.addTab(page, "2D Plot")
        self._apply_canvas_size()

    def _build_3d_tab(self):
        page = QWidget(); layout = QVBoxLayout(page); layout.setContentsMargins(0, 0, 0, 0)
        self.vtk_widget = QVTKRenderWindowInteractor(page); layout.addWidget(self.vtk_widget, 1)
        self.renderer = vtk.vtkRenderer(); self.vtk_widget.GetRenderWindow().AddRenderer(self.renderer)
        self.iren = self.vtk_widget.GetRenderWindow().GetInteractor(); self.iren.SetInteractorStyle(vtk.vtkInteractorStyleTrackballCamera()); self.iren.Initialize()
        self.axes = vtk.vtkCubeAxesActor(); self.axes.SetCamera(self.renderer.GetActiveCamera())
        self.scalarbar = vtk.vtkScalarBarActor(); self.scalarbar.SetNumberOfLabels(5); self.scalarbar.SetPosition(.86, .15); self.scalarbar.SetWidth(.1); self.scalarbar.SetHeight(.7)
        buttons = QHBoxLayout(); layout.addLayout(buttons)
        actions = [("Draw 3D", self.draw_3d), ("Property", self.open_3d_property), ("Reset", self.reset_view),
                   ("View X", lambda: self.view_axis("X")), ("View Y", lambda: self.view_axis("Y")),
                   ("View Z", lambda: self.view_axis("Z")), ("Screenshot", self.save_screenshot)]
        for text, slot in actions:
            button = QPushButton(text); button.clicked.connect(slot); buttons.addWidget(button)
        buttons.addStretch(); self.tabs.addTab(page, "3D Plot")

    def _restore_datasets(self):
        for dataset in self.state.datasets:
            if Path(dataset.path).is_file():
                try:
                    self.frames[dataset.dataset_id] = load_csv(dataset.path)
                except Exception as exc:
                    self.status.setText(f"Failed to restore {dataset.path}: {exc}")
            self._append_row(dataset)
        if self.table.rowCount():
            target = next((i for i, d in enumerate(self.state.datasets) if d.dataset_id == self.state.active_dataset_id), 0)
            self.table.selectRow(target)
        self._sync_figure_curves()

    def add_csv(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Add CSV files", "", "CSV files (*.csv);;All files (*)")
        known = {os.path.normcase(os.path.abspath(d.path)) for d in self.state.datasets}
        for path in paths:
            normalized = os.path.normcase(os.path.abspath(path))
            if normalized in known:
                continue
            try:
                frame = load_csv(path)
            except Exception as exc:
                QMessageBox.warning(self, "CSV Error", f"Failed to read {path}:\n{exc}"); continue
            dataset = CsvDatasetConfig(path=os.path.abspath(path), label=Path(path).stem)
            palette = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#17becf"]
            dataset.color = palette[len(self.state.datasets) % len(palette)]
            self.state.datasets.append(dataset); self.frames[dataset.dataset_id] = frame; self._append_row(dataset); known.add(normalized)
        if paths:
            self._sync_figure_curves(); self._save_state()

    def _append_row(self, dataset):
        self._table_loading = True
        row = self.table.rowCount(); self.table.insertRow(row)
        enabled = QCheckBox(); enabled.setChecked(dataset.enabled); enabled.stateChanged.connect(self._sync_from_table)
        holder = QWidget(); hbox = QHBoxLayout(holder); hbox.setContentsMargins(0, 0, 0, 0); hbox.setAlignment(Qt.AlignCenter); hbox.addWidget(enabled); self.table.setCellWidget(row, 0, holder)
        label_item = QTableWidgetItem(dataset.label); label_item.setData(Qt.UserRole, dataset.dataset_id); self.table.setItem(row, 1, label_item)
        path_item = QTableWidgetItem(Path(dataset.path).name); path_item.setFlags(path_item.flags() & ~Qt.ItemIsEditable); path_item.setToolTip(dataset.path); self.table.setItem(row, 2, path_item)
        frame = self.frames.get(dataset.dataset_id); columns = [str(c) for c in frame.columns] if frame is not None else []
        for column, value in zip(range(3, 8), (dataset.x2d, dataset.y2d, dataset.x3d, dataset.y3d, dataset.z3d)):
            combo = QComboBox(); combo.addItem(""); combo.addItems(columns); combo.setCurrentText(value if value in columns else ""); combo.currentTextChanged.connect(self._sync_from_table); self.table.setCellWidget(row, column, combo)
        mode = QComboBox(); mode.addItems(["Surface", "Mesh", "Scatter"]); mode.setCurrentText(dataset.mode3d); mode.currentTextChanged.connect(self._sync_from_table); self.table.setCellWidget(row, 8, mode)
        color_mode = QComboBox(); color_mode.addItems(["Fixed Color", "Z Colormap"]); color_mode.setCurrentText(dataset.color_mode); color_mode.currentTextChanged.connect(self._sync_from_table); self.table.setCellWidget(row, 9, color_mode)
        if frame is None:
            for col in range(self.table.columnCount()):
                item = self.table.item(row, col)
                if item: item.setBackground(QColor("#ffd6d6"))
            enabled.setChecked(False)
        self._table_loading = False

    def _dataset_at_row(self, row):
        item = self.table.item(row, 1)
        if item is None: return None
        dataset_id = item.data(Qt.UserRole)
        return next((d for d in self.state.datasets if d.dataset_id == dataset_id), None)

    def _selected_dataset(self):
        rows = self.table.selectionModel().selectedRows()
        return self._dataset_at_row(rows[0].row()) if rows else None

    def _sync_from_table(self, *_):
        if self._table_loading: return
        for row in range(self.table.rowCount()):
            d = self._dataset_at_row(row)
            if d is None: continue
            holder = self.table.cellWidget(row, 0); d.enabled = holder.findChild(QCheckBox).isChecked()
            d.label = self.table.item(row, 1).text().strip() or Path(d.path).stem
            d.x2d = self.table.cellWidget(row, 3).currentText(); d.y2d = self.table.cellWidget(row, 4).currentText()
            d.x3d = self.table.cellWidget(row, 5).currentText(); d.y3d = self.table.cellWidget(row, 6).currentText(); d.z3d = self.table.cellWidget(row, 7).currentText()
            d.mode3d = self.table.cellWidget(row, 8).currentText(); d.color_mode = self.table.cellWidget(row, 9).currentText()
        self._sync_figure_curves(); self._save_state()

    def _active_changed(self):
        dataset = self._selected_dataset()
        self.state.active_dataset_id = dataset.dataset_id if dataset else ""
        self._save_state()

    def remove_selected(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows: return
        row = rows[0].row(); dataset = self._dataset_at_row(row)
        if dataset:
            self.state.datasets.remove(dataset); self.frames.pop(dataset.dataset_id, None)
        self.table.removeRow(row); self._sync_figure_curves(); self._save_state()

    def reload_selected(self):
        dataset = self._selected_dataset()
        if dataset is None or not Path(dataset.path).is_file(): return
        try:
            self.frames[dataset.dataset_id] = load_csv(dataset.path)
        except Exception as exc:
            QMessageBox.warning(self, "CSV Error", str(exc)); return
        row = self.table.currentRow(); self.table.removeRow(row); self._append_row_at(dataset, row); self.table.selectRow(row); self._save_state()

    def _append_row_at(self, dataset, row):
        configs_after = self.state.datasets.index(dataset)
        self.state.datasets.remove(dataset); self.state.datasets.append(dataset)
        self._append_row(dataset)
        new_row = self.table.rowCount() - 1
        if row != new_row:
            self.table.setCurrentCell(new_row, 1)

    def relocate_selected(self):
        dataset = self._selected_dataset()
        if dataset is None: return
        path, _ = QFileDialog.getOpenFileName(self, "Relocate CSV", dataset.path, "CSV files (*.csv);;All files (*)")
        if not path: return
        try: frame = load_csv(path)
        except Exception as exc: QMessageBox.warning(self, "CSV Error", str(exc)); return
        dataset.path = os.path.abspath(path); self.frames[dataset.dataset_id] = frame
        row = self.table.currentRow(); self.table.removeRow(row); self._append_row(dataset); self.table.selectRow(self.table.rowCount() - 1); self._save_state()

    def _sync_figure_curves(self):
        existing = {curve.column: curve for curve in self.figure_config.curves}; curves = []
        for index, dataset in enumerate(self.state.datasets):
            curve = existing.get(dataset.dataset_id)
            if curve is None:
                curve = new_curve(dataset.dataset_id, "left", index); curve.color = dataset.color; curve.marker_edge_color = dataset.color
            curve.legend_text = dataset_display_name(dataset); curves.append(curve)
        self.figure_config.curves = curves

    def _series_2d(self):
        output = []
        for dataset in self.state.datasets:
            frame = self.frames.get(dataset.dataset_id)
            if not dataset.enabled or frame is None or not dataset.x2d or not dataset.y2d: continue
            x, y = numeric_series(frame, dataset.x2d), numeric_series(frame, dataset.y2d)
            errors = {str(column): numeric_series(frame, str(column)) for column in frame.columns}
            output.append({"key": dataset.dataset_id, "label": dataset_display_name(dataset), "x": x, "y": y, "errors": errors})
        return output

    def draw_2d(self):
        self._sync_from_table(); series = self._series_2d()
        if not series:
            self.status.setText("No enabled dataset has complete 2D X/Y mappings."); return
        try:
            render_shared_figure(self.figure, self.figure_config, series); self._apply_canvas_size(); self.canvas.draw()
            self.status.setText(f"2D: rendered {len(series)} dataset(s); NaN/Inf values remain as gaps.")
        except Exception as exc:
            if self.figure_config.use_latex:
                self.figure_config.use_latex = False
                try: render_shared_figure(self.figure, self.figure_config, series); self.canvas.draw(); self.status.setText(f"LaTeX failed; using MathText: {exc}"); return
                except Exception: pass
            QMessageBox.warning(self, "2D Plot Error", str(exc))

    def _apply_canvas_size(self):
        width_in, height_in = self.figure_config.width_cm / 2.54, self.figure_config.height_cm / 2.54
        self.figure.set_size_inches(width_in, height_in, forward=False)
        screen = self.screen(); dpi = screen.logicalDotsPerInch() if screen else 96
        self.canvas.setFixedSize(max(100, round(width_in * dpi)), max(100, round(height_in * dpi)))

    def open_2d_property(self):
        self._sync_from_table(); self._sync_figure_curves()
        names = {d.dataset_id: dataset_display_name(d) for d in self.state.datasets}
        columns = {d.dataset_id: [str(c) for c in self.frames[d.dataset_id].columns] for d in self.state.datasets if d.dataset_id in self.frames}
        dialog = PlotPropertyDialog(self.figure_config, [], self._apply_2d_config, self._save_2d_defaults,
                                    self, shared_y_axis=True, curve_names=names, curve_columns=columns)
        dialog.exec()

    def _apply_2d_config(self, config):
        self.figure_config = config; self.draw_2d(); self._save_state()

    def _save_2d_defaults(self, config):
        self.settings.setValue("csv_plotter/default_2d_v1", json.dumps(config.to_dict(include_curves=False), ensure_ascii=False))

    def export_2d(self):
        if not self.figure.axes: self.draw_2d()
        if not self.figure.axes: return
        path, selected = QFileDialog.getSaveFileName(self, "Export 2D Figure", "", "PNG (*.png);;JPEG (*.jpg);;TIFF (*.tif *.tiff);;PDF (*.pdf);;SVG (*.svg)")
        if not path: return
        ext = {"PNG (*.png)": ".png", "JPEG (*.jpg)": ".jpg", "TIFF (*.tif *.tiff)": ".tif", "PDF (*.pdf)": ".pdf", "SVG (*.svg)": ".svg"}.get(selected, ".png")
        if not Path(path).suffix: path += ext
        transparent = self.figure_config.background == "Transparent"
        self.figure.savefig(path, dpi=self.figure_config.export_dpi, transparent=transparent, facecolor="none" if transparent else "white")
        self.status.setText(f"Saved 2D figure: {Path(path).name}")

    def _configured_3d(self):
        items = []
        for dataset in self.state.datasets:
            frame = self.frames.get(dataset.dataset_id)
            if not dataset.enabled or frame is None or not all((dataset.x3d, dataset.y3d, dataset.z3d)): continue
            items.append((dataset, numeric_series(frame, dataset.x3d), numeric_series(frame, dataset.y3d), numeric_series(frame, dataset.z3d)))
        return items

    def draw_3d(self):
        self._sync_from_table(); items = self._configured_3d()
        if not items: self.status.setText("No enabled dataset has complete 3D X/Y/Z mappings."); return
        self.renderer.RemoveAllViewProps(); cfg = self.vtk_config
        arrays = []
        for _, x, y, z in items:
            valid = np.isfinite(x) & np.isfinite(y) & np.isfinite(z)
            if valid.any(): arrays.append(np.column_stack([x[valid], y[valid], z[valid]]))
        if not arrays: self.status.setText("No finite 3D points are available."); return
        union = np.vstack(arrays); raw_min = np.nanmin(union, axis=0); raw_max = np.nanmax(union, axis=0)
        if not cfg.auto_bounds:
            raw_min = np.array([cfg.x_min, cfg.y_min, cfg.z_min]); raw_max = np.array([cfg.x_max, cfg.y_max, cfg.z_max])
        span = np.where(raw_max > raw_min, raw_max - raw_min, 1.0)
        factors = np.array([cfg.x_scale, cfg.y_scale, cfg.z_scale], float)
        scale = factors / span if cfg.auto_normalize else factors
        transform = lambda points: (np.asarray(points) - raw_min) * scale
        fallback_messages = []
        lut_by_id = {}
        actor_entries = []
        for dataset, x, y, z in items:
            if not cfg.auto_bounds:
                in_range = ((x >= raw_min[0]) & (x <= raw_max[0]) & (y >= raw_min[1]) & (y <= raw_max[1]) &
                            (~np.isfinite(z) | ((z >= raw_min[2]) & (z <= raw_max[2]))))
                x = np.where(in_range, x, np.nan); y = np.where(in_range, y, np.nan); z = np.where(in_range, z, np.nan)
            mode = dataset.mode3d
            if mode == "Scatter": poly = build_scatter(x, y, z, transform)
            else:
                result = build_surface_with_holes(x, y, z, transform)
                if result.polydata is None:
                    poly = build_scatter(x, y, z, transform); fallback_messages.append(f"{dataset.label}: {result.reason}; Scatter used")
                    mode = "Scatter"
                else: poly = result.polydata
            mapper = vtk.vtkPolyDataMapper(); mapper.SetInputData(poly)
            valid_z = z[np.isfinite(z)]
            if dataset.color_mode == "Z Colormap" and len(valid_z):
                value_range = (float(np.min(valid_z)), float(np.max(valid_z))) if dataset.auto_color_range else (dataset.color_min, dataset.color_max)
                lut = make_lookup_table(dataset.colormap, value_range); mapper.SetLookupTable(lut); mapper.SetScalarRange(*value_range); mapper.ScalarVisibilityOn(); lut_by_id[dataset.dataset_id] = lut
            else: mapper.ScalarVisibilityOff()
            actor = vtk.vtkActor(); actor.SetMapper(mapper); actor.GetProperty().SetOpacity(dataset.opacity)
            actor.GetProperty().SetColor(*hex_to_rgb(dataset.color))
            if mode == "Scatter": actor.GetProperty().SetRepresentationToPoints(); actor.GetProperty().SetPointSize(dataset.point_size)
            elif mode == "Mesh": actor.GetProperty().EdgeVisibilityOn(); actor.GetProperty().SetEdgeColor(*hex_to_rgb(dataset.mesh_color)); actor.GetProperty().SetLineWidth(dataset.mesh_width)
            self.renderer.AddActor(actor); actor_entries.append((dataset, poly))
        display_max = (raw_max - raw_min) * scale
        self._add_axes((0, display_max[0], 0, display_max[1], 0, display_max[2]), raw_min, raw_max)
        self._add_active_scalarbar(lut_by_id)
        self._add_legend(actor_entries)
        self.renderer.SetBackground(*self._background_rgb(cfg.background)); self.renderer.ResetCamera(); self.renderer.ResetCameraClippingRange(); self.vtk_widget.GetRenderWindow().Render()
        text = f"3D: rendered {len(actor_entries)} dataset(s)."
        if fallback_messages: text += " " + " | ".join(fallback_messages)
        self.status.setText(text); self._save_state()

    def _add_axes(self, bounds, raw_min, raw_max):
        cfg = self.vtk_config
        if not cfg.show_axes: return
        self.axes.SetBounds(*map(float, bounds)); self.axes.SetXTitle(cfg.x_title); self.axes.SetYTitle(cfg.y_title); self.axes.SetZTitle(cfg.z_title); self.axes.SetCamera(self.renderer.GetActiveCamera())
        if hasattr(self.axes, "SetXAxisRange"):
            self.axes.SetXAxisRange(float(raw_min[0]), float(raw_max[0])); self.axes.SetYAxisRange(float(raw_min[1]), float(raw_max[1])); self.axes.SetZAxisRange(float(raw_min[2]), float(raw_max[2]))
        color = hex_to_rgb(cfg.text_color)
        for axis in range(3):
            self.axes.GetTitleTextProperty(axis).SetColor(*color); self.axes.GetTitleTextProperty(axis).SetFontSize(cfg.title_font_size)
            self.axes.GetLabelTextProperty(axis).SetColor(*color); self.axes.GetLabelTextProperty(axis).SetFontSize(cfg.label_font_size)
        self.axes.GetXAxesLinesProperty().SetColor(*color); self.axes.GetYAxesLinesProperty().SetColor(*color); self.axes.GetZAxesLinesProperty().SetColor(*color)
        self.renderer.AddActor(self.axes)

    def _add_active_scalarbar(self, luts):
        if not self.vtk_config.show_colorbar or not luts: return
        active = self._selected_dataset(); dataset_id = active.dataset_id if active and active.dataset_id in luts else next(iter(luts))
        dataset = next(d for d in self.state.datasets if d.dataset_id == dataset_id)
        self.scalarbar.SetLookupTable(luts[dataset_id]); self.scalarbar.SetTitle(dataset.z3d or dataset.label)
        color = hex_to_rgb(self.vtk_config.text_color); self.scalarbar.GetTitleTextProperty().SetColor(*color); self.scalarbar.GetLabelTextProperty().SetColor(*color)
        self.renderer.AddActor2D(self.scalarbar)

    def _add_legend(self, entries):
        if not self.vtk_config.show_legend or not entries: return
        legend = vtk.vtkLegendBoxActor(); legend.SetNumberOfEntries(len(entries)); legend.SetPosition(.02, .02); legend.SetWidth(.22); legend.SetHeight(min(.35, .06 * len(entries) + .05))
        sphere = vtk.vtkSphereSource(); sphere.Update()
        for index, (dataset, _) in enumerate(entries):
            legend.SetEntry(index, sphere.GetOutput(), dataset_display_name(dataset), hex_to_rgb(dataset.color))
        legend.GetEntryTextProperty().SetColor(*hex_to_rgb(self.vtk_config.text_color)); self.renderer.AddActor2D(legend)

    @staticmethod
    def _background_rgb(name):
        return {"White": (1, 1, 1), "Light Gray": (.85, .85, .85), "Gray": (.5, .5, .5), "Dark Gray": (.2, .2, .2), "Black": (0, 0, 0)}.get(name, (1, 1, 1))

    def open_3d_property(self):
        dialog = VtkPropertyDialog(self.vtk_config, self._selected_dataset(), self._apply_3d_config, self); dialog.exec()

    def _apply_3d_config(self, config, dataset):
        self.vtk_config = config
        if dataset is not None:
            target = next((d for d in self.state.datasets if d.dataset_id == dataset.dataset_id), None)
            if target:
                index = self.state.datasets.index(target); self.state.datasets[index] = dataset
                row = self.table.currentRow(); self._table_loading = True; self.table.cellWidget(row, 8).setCurrentText(dataset.mode3d); self.table.cellWidget(row, 9).setCurrentText(dataset.color_mode); self._table_loading = False
        self.draw_3d(); self._save_state()

    def reset_view(self):
        self.renderer.ResetCamera(); self.renderer.ResetCameraClippingRange(); self.vtk_widget.GetRenderWindow().Render()

    def view_axis(self, axis):
        bounds = self.renderer.ComputeVisiblePropBounds(); center = [(bounds[i * 2] + bounds[i * 2 + 1]) / 2 for i in range(3)]; distance = max(bounds[1]-bounds[0], bounds[3]-bounds[2], bounds[5]-bounds[4], 1) * 2.5
        camera = self.renderer.GetActiveCamera(); camera.SetFocalPoint(*center)
        if axis == "X": camera.SetPosition(center[0] + distance, center[1], center[2]); camera.SetViewUp(0, 0, 1)
        elif axis == "Y": camera.SetPosition(center[0], center[1] + distance, center[2]); camera.SetViewUp(0, 0, 1)
        else: camera.SetPosition(center[0], center[1], center[2] + distance); camera.SetViewUp(0, 1, 0)
        self.renderer.ResetCameraClippingRange(); self.vtk_widget.GetRenderWindow().Render()

    def save_screenshot(self):
        path, selected = QFileDialog.getSaveFileName(self, "Save 3D Screenshot", "", "PNG (*.png);;JPEG (*.jpg);;TIFF (*.tif *.tiff)")
        if not path: return
        ext = {"PNG (*.png)": ".png", "JPEG (*.jpg)": ".jpg", "TIFF (*.tif *.tiff)": ".tif"}.get(selected, ".png")
        if not Path(path).suffix: path += ext
        capture = vtk.vtkWindowToImageFilter(); capture.SetInput(self.vtk_widget.GetRenderWindow()); capture.SetScale(self.vtk_config.screenshot_scale); capture.ReadFrontBufferOff(); capture.Update()
        suffix = Path(path).suffix.lower()
        writer = vtk.vtkPNGWriter() if suffix == ".png" else vtk.vtkJPEGWriter() if suffix in (".jpg", ".jpeg") else vtk.vtkTIFFWriter()
        writer.SetFileName(path); writer.SetInputConnection(capture.GetOutputPort()); writer.Write(); self.status.setText(f"Saved 3D screenshot: {Path(path).name}")

    def _save_state(self):
        self.state.figure = self.figure_config.to_dict(include_curves=True); self.state.vtk = self.vtk_config
        self.settings.setValue(STATE_KEY, json.dumps(self.state.to_dict(), ensure_ascii=False))

    def closeEvent(self, event):
        self._closing = True; self._sync_from_table(); self._save_state()
        try: self.vtk_widget.Finalize()
        except Exception: pass
        super().closeEvent(event)


def main():
    app = QApplication.instance() or QApplication([]); dialog = CSVPlotterDialog(); dialog.show(); app.exec()


if __name__ == "__main__":
    main()
