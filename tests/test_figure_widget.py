import io
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pandas as pd
from PySide6.QtWidgets import QApplication

from log_statistics_widget import LogStatisticsWidget
from plot_config import FigureConfig


class FigureWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.widget = LogStatisticsWidget()
        self.widget.figure_config = FigureConfig()
        x = np.arange(20, dtype=float)
        self.frame = pd.DataFrame({
            "x": x,
            "left_a": x,
            "left_b": x ** 2,
            "right_a": np.sin(x),
            "error": np.full_like(x, 0.1),
        })
        self.widget._stage_figure_dataframe(self.frame)

    def test_multi_axis_render_and_png_export(self):
        self.widget.y1_combo.set_checked_items(["left_a", "left_b"], emit=True)
        self.widget.y2_combo.set_checked_items(["right_a"], emit=True)
        curve = self.widget.figure_config.curves[0]
        curve.error.mode = "Bars + Band"
        curve.error.source = "Column"
        curve.error.column = "error"
        self.widget.update_plot()
        self.assertEqual(len(self.widget.plot_figure.axes), 3)
        stream = io.BytesIO()
        self.widget.plot_figure.savefig(stream, format="png", dpi=120)
        self.assertGreater(stream.tell(), 1000)

    def test_refresh_lock_keeps_selectors_stable(self):
        self.widget.y1_combo.set_checked_items(["left_a", "left_b"], emit=True)
        selected = self.widget.y1_combo.checked_items()
        self.widget._lock_figure_updates()
        self.widget._stage_figure_dataframe(self.frame.assign(left_a=self.frame.left_a + 5))
        self.assertIsNotNone(self.widget._pending_figure_df)
        self.assertEqual(self.widget.y1_combo.checked_items(), selected)
        self.widget._unlock_figure_updates()
        self.assertIsNone(self.widget._pending_figure_df)
        self.assertEqual(self.widget.y1_combo.checked_items(), selected)

    def test_duplicate_column_is_rejected(self):
        self.widget.y1_combo.set_checked_items(["left_a"], emit=True)
        self.widget.y2_combo.set_checked_items(["left_a"], emit=True)
        self.assertEqual(self.widget.y2_combo.checked_items(), [])


if __name__ == "__main__":
    unittest.main()
