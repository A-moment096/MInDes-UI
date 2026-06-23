"""VTK geometry helpers with explicit NaN holes for CSV surfaces."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import vtk


@dataclass
class SurfaceBuildResult:
    polydata: vtk.vtkPolyData | None
    reason: str = ""
    valid_points: int = 0


def aggregate_xy(x: np.ndarray, y: np.ndarray, z: np.ndarray):
    """Collapse duplicate finite X/Y coordinates while preserving all-invalid Z holes."""
    buckets: dict[tuple[float, float], list[float]] = {}
    for xv, yv, zv in zip(x, y, z):
        if not (np.isfinite(xv) and np.isfinite(yv)):
            continue
        buckets.setdefault((float(xv), float(yv)), []).append(float(zv))
    xs, ys, zs = [], [], []
    for (xv, yv), values in buckets.items():
        finite = [v for v in values if np.isfinite(v)]
        xs.append(xv); ys.append(yv); zs.append(float(np.mean(finite)) if finite else np.nan)
    return np.asarray(xs), np.asarray(ys), np.asarray(zs)


def build_surface_with_holes(x, y, z, transform=None) -> SurfaceBuildResult:
    """Delaunay-triangulate X/Y and remove every face incident to an invalid Z vertex."""
    x, y, z = aggregate_xy(np.asarray(x, float), np.asarray(y, float), np.asarray(z, float))
    finite_z = np.isfinite(z)
    if len(x) < 3 or finite_z.sum() < 3:
        return SurfaceBuildResult(None, "fewer than three valid points", int(finite_z.sum()))
    xy = np.column_stack([x, y])
    if np.linalg.matrix_rank(xy - xy.mean(axis=0)) < 2:
        return SurfaceBuildResult(None, "X/Y points are collinear", int(finite_z.sum()))

    display = np.column_stack([x, y, np.where(finite_z, z, 0.0)])
    if transform is not None:
        display = transform(display)

    points = vtk.vtkPoints()
    validity = vtk.vtkUnsignedCharArray(); validity.SetName("csv_valid_z")
    raw_z = vtk.vtkDoubleArray(); raw_z.SetName("csv_z")
    for point, zv, valid in zip(display, z, finite_z):
        points.InsertNextPoint(*map(float, point))
        validity.InsertNextValue(1 if valid else 0)
        raw_z.InsertNextValue(float(zv) if valid else 0.0)

    source = vtk.vtkPolyData(); source.SetPoints(points)
    source.GetPointData().AddArray(validity); source.GetPointData().SetScalars(raw_z)
    delaunay = vtk.vtkDelaunay2D(); delaunay.SetInputData(source); delaunay.Update()
    output = delaunay.GetOutput()
    out_validity = output.GetPointData().GetArray("csv_valid_z")
    if out_validity is None:
        return SurfaceBuildResult(None, "triangulation lost validity metadata", int(finite_z.sum()))

    kept = vtk.vtkCellArray(); ids = vtk.vtkIdList()
    cells = output.GetPolys(); cells.InitTraversal()
    while cells.GetNextCell(ids):
        if ids.GetNumberOfIds() != 3:
            continue
        vertex_ids = [ids.GetId(i) for i in range(3)]
        if all(out_validity.GetTuple1(pid) > 0.5 for pid in vertex_ids):
            kept.InsertNextCell(3)
            for pid in vertex_ids:
                kept.InsertCellPoint(pid)

    if kept.GetNumberOfCells() == 0:
        return SurfaceBuildResult(None, "no valid triangle remains after applying holes", int(finite_z.sum()))
    result = vtk.vtkPolyData(); result.SetPoints(output.GetPoints()); result.SetPolys(kept)
    result.GetPointData().ShallowCopy(output.GetPointData())
    return SurfaceBuildResult(result, "", int(finite_z.sum()))


def build_scatter(x, y, z, transform=None) -> vtk.vtkPolyData:
    x, y, z = map(lambda v: np.asarray(v, float), (x, y, z))
    valid = np.isfinite(x) & np.isfinite(y) & np.isfinite(z)
    display = np.column_stack([x[valid], y[valid], z[valid]])
    if transform is not None and len(display):
        display = transform(display)
    points = vtk.vtkPoints(); vertices = vtk.vtkCellArray(); raw_z = vtk.vtkDoubleArray(); raw_z.SetName("csv_z")
    for point, zv in zip(display, z[valid]):
        pid = points.InsertNextPoint(*map(float, point)); vertices.InsertNextCell(1); vertices.InsertCellPoint(pid)
        raw_z.InsertNextValue(float(zv))
    poly = vtk.vtkPolyData(); poly.SetPoints(points); poly.SetVerts(vertices); poly.GetPointData().SetScalars(raw_z)
    return poly


def make_lookup_table(name: str, value_range: tuple[float, float]):
    import matplotlib
    cmap_name = {"grayscale": "gray", "cool-warm": "coolwarm"}.get(name.lower(), name.lower())
    cmap = matplotlib.colormaps.get_cmap(cmap_name if cmap_name in matplotlib.colormaps else "viridis")
    lut = vtk.vtkLookupTable(); lut.SetNumberOfTableValues(256); lut.SetRange(*map(float, value_range))
    for index in range(256):
        r, g, b, a = cmap(index / 255.0); lut.SetTableValue(index, r, g, b, a)
    lut.Build(); return lut


def hex_to_rgb(color: str):
    color = color.lstrip("#")
    if len(color) != 6:
        return 0.0, 0.0, 0.0
    return tuple(int(color[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
