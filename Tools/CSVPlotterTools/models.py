"""Serializable state and CSV loading helpers for CSV Plotter."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
import pandas as pd


STATE_VERSION = 1


@dataclass
class CsvDatasetConfig:
    dataset_id: str = field(default_factory=lambda: uuid4().hex)
    path: str = ""
    label: str = ""
    enabled: bool = True
    x2d: str = ""
    y2d: str = ""
    x3d: str = ""
    y3d: str = ""
    z3d: str = ""
    mode3d: str = "Surface"
    color_mode: str = "Fixed Color"
    color: str = "#1f77b4"
    colormap: str = "Viridis"
    auto_color_range: bool = True
    color_min: float = 0.0
    color_max: float = 1.0
    opacity: float = 0.85
    point_size: float = 5.0
    mesh_color: str = "#202020"
    mesh_width: float = 1.0

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CsvDatasetConfig":
        cfg = cls()
        for key, value in raw.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)
        if not cfg.dataset_id:
            cfg.dataset_id = uuid4().hex
        return cfg


@dataclass
class VtkPlotConfig:
    background: str = "White"
    show_axes: bool = True
    show_colorbar: bool = True
    show_legend: bool = True
    x_title: str = "X"
    y_title: str = "Y"
    z_title: str = "Z"
    text_color: str = "#000000"
    title_font_size: int = 16
    label_font_size: int = 12
    auto_normalize: bool = True
    x_scale: float = 1.0
    y_scale: float = 1.0
    z_scale: float = 1.0
    auto_bounds: bool = True
    x_min: float = 0.0
    x_max: float = 1.0
    y_min: float = 0.0
    y_max: float = 1.0
    z_min: float = 0.0
    z_max: float = 1.0
    screenshot_scale: int = 2

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "VtkPlotConfig":
        cfg = cls()
        if isinstance(raw, dict):
            for key, value in raw.items():
                if hasattr(cfg, key):
                    setattr(cfg, key, value)
        return cfg


@dataclass
class CsvPlotterState:
    version: int = STATE_VERSION
    datasets: list[CsvDatasetConfig] = field(default_factory=list)
    figure: dict[str, Any] = field(default_factory=dict)
    vtk: VtkPlotConfig = field(default_factory=VtkPlotConfig)
    active_dataset_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "datasets": [asdict(item) for item in self.datasets],
            "figure": self.figure,
            "vtk": asdict(self.vtk),
            "active_dataset_id": self.active_dataset_id,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "CsvPlotterState":
        if not isinstance(raw, dict) or raw.get("version", STATE_VERSION) != STATE_VERSION:
            return cls()
        return cls(
            datasets=[CsvDatasetConfig.from_dict(item) for item in raw.get("datasets", []) if isinstance(item, dict)],
            figure=raw.get("figure", {}) if isinstance(raw.get("figure", {}), dict) else {},
            vtk=VtkPlotConfig.from_dict(raw.get("vtk")),
            active_dataset_id=str(raw.get("active_dataset_id", "")),
        )


def load_csv(path: str) -> pd.DataFrame:
    """Read a comma-separated file without altering its columns or row order."""
    return pd.read_csv(path, encoding="utf-8-sig")


def numeric_series(frame: pd.DataFrame, column: str) -> np.ndarray:
    """Convert a selected column to float; non-numeric and Inf become NaN."""
    if not column or column not in frame.columns:
        return np.full(len(frame), np.nan, dtype=float)
    values = pd.to_numeric(frame[column], errors="coerce").to_numpy(dtype=float)
    values[~np.isfinite(values)] = np.nan
    return values


def dataset_display_name(config: CsvDatasetConfig) -> str:
    return config.label or Path(config.path).stem or "CSV"
