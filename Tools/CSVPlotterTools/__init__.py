"""Multi-file CSV plotting tool."""

from .models import CsvDatasetConfig, CsvPlotterState, VtkPlotConfig

__all__ = ["CSVPlotterDialog", "CsvDatasetConfig", "CsvPlotterState", "VtkPlotConfig"]


def __getattr__(name):
    if name == "CSVPlotterDialog":
        from .csv_plotter_gui import CSVPlotterDialog
        return CSVPlotterDialog
    raise AttributeError(name)
