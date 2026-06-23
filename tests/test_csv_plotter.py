import io
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pandas as pd
from matplotlib.figure import Figure
from PySide6.QtWidgets import QApplication

from plot_config import FigureConfig, new_curve
from plot_property_dialog import PlotPropertyDialog
from Tools.CSVPlotterTools.models import CsvDatasetConfig, CsvPlotterState, numeric_series
from Tools.CSVPlotterTools.rendering import render_shared_figure
from Tools.CSVPlotterTools.vtk_utils import build_surface_with_holes


class CsvPlotterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_numeric_conversion_keeps_positions_as_nan(self):
        frame = pd.DataFrame({"sample_id": [1, 2, 3, 4], "value": [1, "bad", np.inf, 4]})
        values = numeric_series(frame, "value")
        self.assertEqual(len(values), 4)
        self.assertTrue(np.isnan(values[1]))
        self.assertTrue(np.isnan(values[2]))

    def test_2d_line_keeps_original_order_and_gaps(self):
        config = FigureConfig(); config.curves = [new_curve("dataset", "left", 0)]
        x = np.array([0.0, 2.0, 1.0, 3.0])
        y = np.array([0.0, np.nan, 1.0, 2.0])
        figure = Figure()
        ax = render_shared_figure(figure, config, [{"key": "dataset", "label": "sample", "x": x, "y": y, "errors": {}}])
        np.testing.assert_array_equal(ax.lines[0].get_xdata(), x)
        self.assertTrue(np.isnan(ax.lines[0].get_ydata()[1]))

    def test_surface_removes_faces_touching_nan_z(self):
        x, y = np.meshgrid(np.arange(3.0), np.arange(3.0))
        z = x + y; z[1, 1] = np.nan
        result = build_surface_with_holes(x.ravel(), y.ravel(), z.ravel())
        self.assertIsNotNone(result.polydata, result.reason)
        validity = result.polydata.GetPointData().GetArray("csv_valid_z")
        ids = __import__("vtk").vtkIdList(); cells = result.polydata.GetPolys(); cells.InitTraversal()
        while cells.GetNextCell(ids):
            self.assertTrue(all(validity.GetTuple1(ids.GetId(i)) > .5 for i in range(ids.GetNumberOfIds())))

    def test_state_round_trip_and_shared_property(self):
        dataset = CsvDatasetConfig(path="sample.csv", label="sample", x2d="x", y2d="y")
        state = CsvPlotterState(datasets=[dataset], figure=FigureConfig().to_dict())
        restored = CsvPlotterState.from_dict(state.to_dict())
        self.assertEqual(restored.datasets[0].x2d, "x")
        config = FigureConfig(); config.curves = [new_curve(dataset.dataset_id, "left", 0)]
        dialog = PlotPropertyDialog(config, [], lambda value: None, lambda value: None,
                                    shared_y_axis=True, curve_names={dataset.dataset_id: "sample"},
                                    curve_columns={dataset.dataset_id: ["x", "y"]})
        self.assertEqual(dialog.axis_combo.count(), 2)


if __name__ == "__main__":
    unittest.main()
