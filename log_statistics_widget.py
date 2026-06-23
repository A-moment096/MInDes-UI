# log_statistics_widget.py
import os
import re
import json
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTabWidget, QPlainTextEdit, QComboBox, QFrame, 
    QLabel, QPushButton, QFileDialog, QMenu, QMessageBox, QScrollArea, QSizePolicy
)
from PySide6.QtCore import Qt, Signal, QFileSystemWatcher, QTimer, QSettings
from PySide6.QtGui import QFont, QKeySequence, QShortcut, QStandardItemModel, QStandardItem
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from matplotlib import ticker
from matplotlib.path import Path as MplPath

from plot_config import FigureConfig, new_curve
from plot_property_dialog import PlotPropertyDialog

# === 定义候选文件名（按优先级排序，高 → 低）===
LOG_CANDIDATES = ["Log.txt", "log.txt"]
STAT_CANDIDATES = ["Statistics.txt", "data_statistics.txt"]


class PopupComboBox(QComboBox):
    popupShown = Signal()
    popupHidden = Signal()

    def showPopup(self):
        self.popupShown.emit()
        super().showPopup()

    def hidePopup(self):
        super().hidePopup()
        self.popupHidden.emit()


class CheckableComboBox(QComboBox):
    """Compact ordered multi-select combo that keeps its popup scroll position."""
    selectionChanged = Signal(list)
    popupShown = Signal()
    popupHidden = Signal()

    def __init__(self, parent=None, maximum=6):
        super().__init__(parent)
        self.setEditable(True)
        self.lineEdit().setReadOnly(True)
        self.lineEdit().setPlaceholderText("Select data...")
        self.setModel(QStandardItemModel(self))
        self.view().pressed.connect(self._toggle_item)
        self.maximum = maximum
        self._order = []
        self._skip_next_hide = False

    def showPopup(self):
        self.popupShown.emit()
        super().showPopup()

    def hidePopup(self):
        if self._skip_next_hide:
            self._skip_next_hide = False
            return
        super().hidePopup()
        self.popupHidden.emit()

    def set_items(self, values, checked=None):
        checked = list(checked or [])
        self.blockSignals(True)
        model = self.model(); model.clear()
        for value in values:
            item = QStandardItem(str(value))
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            item.setData(Qt.CheckState.Checked if value in checked else Qt.CheckState.Unchecked,
                         Qt.ItemDataRole.CheckStateRole)
            model.appendRow(item)
        self._order = [value for value in checked if value in values]
        self.blockSignals(False)
        self._update_text()

    def checked_items(self):
        return list(self._order)

    def set_checked_items(self, values, emit=False):
        values = [v for v in values if self.findText(v) >= 0][:self.maximum]
        self.blockSignals(True)
        for row in range(self.model().rowCount()):
            item = self.model().item(row)
            item.setCheckState(Qt.CheckState.Checked if item.text() in values else Qt.CheckState.Unchecked)
        self._order = list(values)
        self.blockSignals(False)
        self._update_text()
        if emit:
            self.selectionChanged.emit(self.checked_items())

    def _toggle_item(self, index):
        item = self.model().itemFromIndex(index)
        text = item.text()
        if item.checkState() == Qt.CheckState.Checked:
            item.setCheckState(Qt.CheckState.Unchecked)
            self._order = [v for v in self._order if v != text]
        else:
            if len(self._order) >= self.maximum:
                return
            item.setCheckState(Qt.CheckState.Checked)
            self._order.append(text)
        self._skip_next_hide = True
        self._update_text()
        self.selectionChanged.emit(self.checked_items())

    def _update_text(self):
        text = ", ".join(self._order) if self._order else ""
        self.lineEdit().setText(text)
        self.setToolTip(text)

def get_existing_candidates_by_mtime(base_dir: Path, candidates: list[str]) -> list[Path]:
    """
    返回 base_dir 下所有命中的候选文件，按“最后写入时间”从新到旧排序。
    若写入时间相同，则按 candidates 中的先后顺序决定优先级。
    """
    ranked = []

    for priority, name in enumerate(candidates):
        path = base_dir / name
        if not (path.exists() and path.is_file()):
            continue
        try:
            ranked.append((path.stat().st_mtime_ns, -priority, path))
        except OSError:
            continue

    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in ranked]

class LogStatisticsWidget(QWidget):
    """
    升级版 Log & Statistics Widget
    - 支持外部设置项目路径（.mindes 同名目录）
    - 自动监听 Log.txt / Statistics.txt 文件变化
    - 多Y轴多曲线选择（左/右Y轴为多选列表）
    - 状态消息通过信号发出，供主窗口状态栏显示
    """

    # 状态信号：(message, level) 其中 level in {"info", "warning", "error"}
    statusMessage = Signal(str, str)

    def __init__(self, parent=None, progress_callback=None):
        super().__init__(parent)
        self.progress_callback = progress_callback
        self._project_path: Optional[Path] = None  # .mindes 同名结果目录
        self.data_df: Optional[pd.DataFrame] = None
        self._current_schema: tuple[str, ...] = ()
        self._pending_figure_df: Optional[pd.DataFrame] = None
        self._figure_lock_count = 0
        self._refresh_dirty = False
        self._refresh_in_progress = False
        self._parse_retry_count = 0
        self.log_content = ""
        self.stat_content = ""
        # 绘图监控
        self.is_drawing = False

        self.figure_settings = QSettings("MInDes", "MInDes-UI")
        try:
            saved = json.loads(self.figure_settings.value("figure/default_style_v1", "{}", type=str))
        except (TypeError, ValueError, json.JSONDecodeError):
            saved = {}
        self.figure_config = FigureConfig.from_dict(saved)
        template_payload = {"version": 1, "curves": saved.get("curve_templates", [])} if isinstance(saved, dict) else {}
        self._curve_templates = FigureConfig.from_dict(template_payload).curves

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(250)
        self._refresh_timer.timeout.connect(self._process_scheduled_refresh)

        # 文件监听器
        self._report_progress("   Creating Log widget watcher...")
        self.watcher = QFileSystemWatcher(self)
        self.watcher.fileChanged.connect(self._on_file_changed)
        self.watcher.directoryChanged.connect(self._on_file_changed)

        self.setup_ui()

        self._report_progress("Binding shortcuts...")
        self.setup_shortcuts()

        self._apply_canvas_size()

    def _report_progress(self, detail: str):
        if self.progress_callback:
            self.progress_callback(detail)

    def set_project_path(self, project_folder: str):
        """由主窗口调用：设置当前 .mindes 文件路径，自动推导结果目录"""
        if not project_folder:
            self._project_path = None
            self.statusMessage.emit("Project path cleared.", "info")
            # 可选：清空 UI
            self.log_edit.setPlainText("(No valid project path)")
            self.stat_edit.setPlainText("(No valid project path)")
            self.data_df = None
            self.update_combo_boxes()
            return

        # 将传入的 project_folder 视为 _project_path（无后缀的基础路径）
        base_path = Path(project_folder).resolve()
        self._project_path = base_path  # 这就是结果目录路径

        # 推导对应的 .mindes 文件路径
        mindes_path = base_path.with_suffix(".mindes")

        # 如果目录不存在，不报错，等运行后生成
        if not self._project_path.exists():
            self.log_edit.setPlainText("(Result directory not created yet)")
            self.stat_edit.setPlainText("(Result directory not created yet)")
            self.data_df = None
            self.update_combo_boxes()
            self.statusMessage.emit(f"Waiting for result dir: {self._project_path.name}", "info")
            return
        # 尝试加载
        self.load_log_and_statistics()

    def setup_shortcuts(self):
        self.load_log_stat_shortcut = QShortcut(QKeySequence("Ctrl+L"), self)
        self.load_log_stat_shortcut.activated.connect(self.load_log_and_statistics)

    def setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(5, 5, 5, 5)
        main_layout.setSpacing(5)

        # === 使用 QTabWidget 管理三个页面 ===
        self._report_progress("   Creating Log/Statistic tabs...")
        self.tab_widget = QTabWidget()
        main_layout.addWidget(self.tab_widget)

        # --- Tab 1: Log ---
        log_container = QWidget()
        log_layout = QVBoxLayout(log_container)
        log_layout.setContentsMargins(0, 0, 0, 0)  # 关键：去除容器边距
        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setFont(self._get_monospace_font())
        self.log_edit.setStyleSheet("background-color: #f0f0f0; color: black;")
        self.log_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        log_layout.addWidget(self.log_edit)
        self.tab_widget.addTab(log_container, "Log")

        # --- Tab 2: Statistic ---
        stat_container = QWidget()
        stat_layout = QVBoxLayout(stat_container)
        stat_layout.setContentsMargins(0, 0, 0, 0)  # 关键：去除容器边距
        self.stat_edit = QPlainTextEdit()
        self.stat_edit.setReadOnly(True)
        self.stat_edit.setFont(self._get_monospace_font())
        self.stat_edit.setStyleSheet("background-color: #f0f0f0; color: black;")
        self.stat_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        stat_layout.addWidget(self.stat_edit)
        self.tab_widget.addTab(stat_container, "Statistic")

        # --- Tab 3: Plot ---
        self._report_progress("   Creating Figure tab controls...")
        plot_page = QWidget()
        plot_layout = QVBoxLayout(plot_page)
        plot_layout.setContentsMargins(10, 5, 10, 5)

        # === 控制面板：X, Y1, Y2 单选 + Plot + Property ===
        control_hbox = QHBoxLayout()
        control_hbox.setSpacing(8)

        # X Axis
        x_label = QLabel("X Axis:")
        self.x_combo = PopupComboBox()
        self.x_combo.setFixedWidth(120)  # ← 改为 120px
        self.x_combo.popupShown.connect(self._lock_figure_updates)
        self.x_combo.popupHidden.connect(self._unlock_figure_updates)
        control_hbox.addWidget(x_label)
        control_hbox.addWidget(self.x_combo)

        # Left Y Axis
        y1_label = QLabel("Left Y:")
        self.y1_combo = CheckableComboBox(maximum=6)
        self.y1_combo.setMinimumWidth(180)
        self.y1_combo.selectionChanged.connect(lambda _: self._on_y_selection_changed("left"))
        self.y1_combo.popupShown.connect(self._lock_figure_updates)
        self.y1_combo.popupHidden.connect(self._unlock_figure_updates)
        control_hbox.addWidget(y1_label)
        control_hbox.addWidget(self.y1_combo)

        # Right Y Axis
        y2_label = QLabel("Right Y:")
        self.y2_combo = CheckableComboBox(maximum=6)
        self.y2_combo.setMinimumWidth(180)
        self.y2_combo.selectionChanged.connect(lambda _: self._on_y_selection_changed("right"))
        self.y2_combo.popupShown.connect(self._lock_figure_updates)
        self.y2_combo.popupHidden.connect(self._unlock_figure_updates)
        control_hbox.addWidget(y2_label)
        control_hbox.addWidget(self.y2_combo)

        self.figure_update_label = QLabel("")
        self.figure_update_label.setStyleSheet("color:#b36b00; font-style:italic;")
        control_hbox.addWidget(self.figure_update_label)
        control_hbox.addStretch()
        plot_layout.addLayout(control_hbox)

        # >>> 横线 <<<
        top_line = QFrame()
        top_line.setFrameShape(QFrame.Shape.HLine)
        top_line.setFrameShadow(QFrame.Shadow.Sunken)
        plot_layout.addWidget(top_line)

        # Matplotlib 画布
        self._report_progress("   Creating plot canvas...")
        self.plot_figure = Figure(figsize=(6, 4), dpi=100)
        self.plot_canvas = FigureCanvas(self.plot_figure)
        self.plot_canvas.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.plot_scroll = QScrollArea()
        self.plot_scroll.setWidgetResizable(False)
        self.plot_scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.plot_scroll.setWidget(self.plot_canvas)
        plot_layout.addWidget(self.plot_scroll, 1)

        # >>> 横线 <<<
        bottom_line = QFrame()
        bottom_line.setFrameShape(QFrame.Shape.HLine)
        bottom_line.setFrameShadow(QFrame.Shadow.Sunken)
        plot_layout.addWidget(bottom_line)

        # === 操作按钮：Draw / Property / Save ===
        self._report_progress("   Creating plot actions...")
        button_hbox = QHBoxLayout()
        button_hbox.setSpacing(8)

        self.plot_btn = QPushButton("📊 Draw")
        self.plot_btn.setShortcut(QKeySequence("Ctrl+D"))
        self.plot_btn.clicked.connect(self.update_plot)
        button_hbox.addWidget(self.plot_btn)

        self.property_btn = QPushButton("⚙️ Property")
        self.property_btn.setShortcut(QKeySequence("Ctrl+P"))
        self.property_btn.clicked.connect(self.open_plot_customization_dialog)
        button_hbox.addWidget(self.property_btn)

        self.export_btn = QPushButton("📤 Export Data")
        self.export_btn.setShortcut(QKeySequence("Ctrl+E"))
        self.export_btn.clicked.connect(self.export_to_excel)
        button_hbox.addWidget(self.export_btn)

        self.save_btn = QPushButton("💾 Export Figure")
        self.save_btn.setShortcut(QKeySequence("Ctrl+S"))
        self.save_btn.clicked.connect(self.save_plot)
        button_hbox.addWidget(self.save_btn)

        plot_layout.addLayout(button_hbox)

        self.tab_widget.addTab(plot_page, "Figure")

        # 初始化空图
        self.plot_figure.clear()
        self.plot_canvas.draw()

        # 右键菜单（用于图形调整）
        self.plot_canvas.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.plot_canvas.customContextMenuRequested.connect(self.show_plot_context_menu)

    def export_to_excel(self):
        """将当前 data_df 导出为 Excel 文件"""
        if self.data_df is None or self.data_df.empty:
            self.statusMessage.emit("No data to export.", "warning")
            QMessageBox.warning(self, "Export Error", "No data available to export.")
            return

        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Data to Excel",
            "",
            "Excel Files (*.xlsx);;CSV Files (*.csv);;All Files (*)"
        )
        if not file_path:
            return

        try:
            if file_path.lower().endswith('.csv'):
                self.data_df.to_csv(file_path, index=False)
            else:
                # Ensure .xlsx extension
                if not file_path.lower().endswith('.xlsx'):
                    file_path += '.xlsx'
                self.data_df.to_excel(file_path, index=False, engine='openpyxl')
            self.statusMessage.emit(f"Data exported: {os.path.basename(file_path)}", "info")
        except Exception as e:
            self.statusMessage.emit(f"Export failed: {e}", "error")
            QMessageBox.critical(self, "Export Error", f"Failed to export data:\n{e}")

    def _get_monospace_font(self):
        font = QFont()
        families = ["Consolas", "Courier New", "Monaco", "DejaVu Sans Mono", "monospace"]
        for family in families:
            font.setFamily(family)
            if font.family() == family:
                break
        font.setPointSize(9)
        return font

    def _clear_watcher(self):
        files = self.watcher.files()
        if files:
            self.watcher.removePaths(files)
        directories = self.watcher.directories()
        if directories:
            self.watcher.removePaths(directories)

    def load_log_and_statistics(self):
        """从 self._project_path 加载日志和统计文件（支持多版本命名）"""
        if not self._project_path or not self._project_path.exists():
            self.log_edit.setPlainText("(No valid project path)")
            self.stat_edit.setPlainText("(No valid project path)")
            if self.data_df is None:
                self.update_combo_boxes()
            return
        # 清除旧监听
        self._clear_watcher()
        self.watcher.addPath(str(self._project_path))
        # --- 加载 Log 文件 ---
        log_content = "(Log file not found)"
        loaded_log_path = None
        log_candidates = get_existing_candidates_by_mtime(self._project_path, LOG_CANDIDATES)
        for path in log_candidates:
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    log_content = f.read()
                loaded_log_path = path
                break
            except Exception as e:
                self.statusMessage.emit(f"Failed to read {path.name}: {e}", "error")
                continue

        self.log_edit.setPlainText(log_content)
        # 滚动到底部
        self.log_edit.verticalScrollBar().setValue(
            self.log_edit.verticalScrollBar().maximum()
        )
        for path in log_candidates:
            self.watcher.addPath(str(path))
        # --- 加载 Statistics 文件 ---
        stat_content = "(Statistics file not found)"
        loaded_stat_path = None
        parsed_df = None
        stat_candidates = get_existing_candidates_by_mtime(self._project_path, STAT_CANDIDATES)
        for path in stat_candidates:
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    stat_content = f.read()
                loaded_stat_path = path
                # 尝试解析为 DataFrame
                parsed_df = self.parse_statistics_to_dataframe(path)
                if parsed_df is not None:
                    break
            except Exception as e:
                self.statusMessage.emit(f"Failed to read or parse {path.name}: {e}", "error")
                continue

        self.stat_edit.setPlainText(stat_content)
        # >>> 滚动到底部 <<<
        self.stat_edit.verticalScrollBar().setValue(
            self.stat_edit.verticalScrollBar().maximum()
        )
        for path in stat_candidates:
            self.watcher.addPath(str(path))
        if parsed_df is not None:
            self._parse_retry_count = 0
            self._stage_figure_dataframe(parsed_df)
        elif stat_candidates and self._parse_retry_count < 3:
            self._parse_retry_count += 1
            self._refresh_dirty = True
            if not self._refresh_timer.isActive():
                self._refresh_timer.start()
        # --- 发送状态消息 ---
        msg_parts = []
        if loaded_log_path:
            if loaded_log_path.name != "Log.txt":
                msg_parts.append(f"legacy log: {loaded_log_path.name}")
        if loaded_stat_path:
            if loaded_stat_path.name != "Statistics.txt":
                msg_parts.append(f"legacy stats: {loaded_stat_path.name}")
        if loaded_log_path or loaded_stat_path:
            base_msg = f"Data loaded from: {self._project_path.name}"
            if msg_parts:
                base_msg += " (" + ", ".join(msg_parts) + ")"
            self.statusMessage.emit(base_msg, "info")
        else:
            self.statusMessage.emit(f"No output files found in: {self._project_path.name}", "warning")

    def _on_file_changed(self, path: str):
        """当被监视的文件（Log.txt / Statistics.txt）发生变化时触发"""
        from pathlib import Path
        file_name = Path(path).name
        self.statusMessage.emit(f"Detected change in {file_name}, update queued...", "info")
        self._refresh_dirty = True
        if not self._refresh_timer.isActive():
            self._refresh_timer.start()

    def _process_scheduled_refresh(self):
        if self._refresh_in_progress or not self._refresh_dirty:
            return
        self._refresh_dirty = False
        self._refresh_in_progress = True
        try:
            self.load_log_and_statistics()
        finally:
            self._refresh_in_progress = False
        if self._refresh_dirty:
            self._refresh_timer.start()

    def parse_statistics_to_dataframe(self, stat_file: Path):
        """尝试将 Statistics.txt 解析为结构化 DataFrame"""
        try:
            # 主路径：标准表格格式（支持空格、制表符分隔）
            df = pd.read_csv(
                stat_handled := str(stat_file),
                comment='#',
                sep=r'\s+',
                skip_blank_lines=True,
                on_bad_lines='warn',  # 改为 warn，便于调试
                engine='python'       # 更容错（可选）
            )
            if not df.empty and len(df.columns) >= 1:
                return df
        except Exception as e:
            self.statusMessage.emit(f"Primary parsing failed: {e}", "warning")

        # === Fallback: only if main fails ===
        return None

    def load_from_excel(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Open Excel File", "", "Excel Files (*.xlsx *.xls);;All Files (*)"
        )
        if not file_path:
            return

        try:
            loaded_df = pd.read_excel(file_path)
            self.log_edit.setPlainText(f"(Data loaded from: {os.path.basename(file_path)})")
            self.stat_edit.setPlainText("(Excel mode – no text display)")
            self._stage_figure_dataframe(loaded_df, force=True)
            self.statusMessage.emit(f"Loaded Excel: {os.path.basename(file_path)}", "info")
        except Exception as e:
            self.statusMessage.emit(f"Failed to load Excel: {e}", "error")
            QMessageBox.critical(self, "Load Error", f"Failed to load Excel:\n{e}")

    def update_combo_boxes(self):
        """Rebuild selectors only when the schema actually changes."""
        columns = [str(c) for c in self.data_df.columns] if self.data_df is not None else []
        current_x = self.x_combo.currentText()
        left = [c.column for c in self.figure_config.curves if c.side == "left" and c.column in columns]
        right = [c.column for c in self.figure_config.curves if c.side == "right" and c.column in columns]
        self.x_combo.blockSignals(True)
        self.x_combo.clear(); self.x_combo.addItems(columns)
        if current_x in columns:
            self.x_combo.setCurrentText(current_x)
        elif columns:
            self.x_combo.setCurrentIndex(0)
        self.x_combo.blockSignals(False)
        self.y1_combo.set_items(columns, left)
        self.y2_combo.set_items(columns, right)
        self._sync_curve_config(left, right)

    def _lock_figure_updates(self):
        self._figure_lock_count += 1

    def _unlock_figure_updates(self):
        self._figure_lock_count = max(0, self._figure_lock_count - 1)
        if self._figure_lock_count == 0 and self._pending_figure_df is not None:
            pending = self._pending_figure_df
            self._pending_figure_df = None
            self.figure_update_label.clear()
            self._commit_figure_dataframe(pending)

    def _stage_figure_dataframe(self, df: pd.DataFrame, force=False):
        if self._figure_lock_count and not force:
            self._pending_figure_df = df.copy()
            self.figure_update_label.setText("Figure update queued")
            return
        self._commit_figure_dataframe(df)

    def _commit_figure_dataframe(self, df: pd.DataFrame):
        schema = tuple(str(c) for c in df.columns)
        schema_changed = schema != self._current_schema
        self.data_df = df
        if schema_changed:
            self._current_schema = schema
            self.update_combo_boxes()
        if self.is_drawing:
            self.update_plot()

    def _on_y_selection_changed(self, side):
        current = self.y1_combo if side == "left" else self.y2_combo
        other = self.y2_combo if side == "left" else self.y1_combo
        values = current.checked_items()
        duplicates = set(values) & set(other.checked_items())
        if duplicates:
            values = [v for v in values if v not in duplicates]
            current.set_checked_items(values)
            self.statusMessage.emit(
                f"A data column can only belong to one Y side: {', '.join(sorted(duplicates))}",
                "warning",
            )
        self._sync_curve_config(self.y1_combo.checked_items(), self.y2_combo.checked_items())

    def _sync_curve_config(self, left, right):
        existing = {(c.side, c.column): c for c in self.figure_config.curves}
        curves = []
        for side, values in (("left", left), ("right", right)):
            for side_index, value in enumerate(values[:6]):
                curve = existing.get((side, value))
                if curve is None:
                    templates = [c for c in self._curve_templates if c.side == side]
                    if side_index < len(templates):
                        curve = deepcopy(templates[side_index])
                        curve.column = value; curve.side = side
                        curve.legend_text = value; curve.axis.label.text = value
                        curve.error.column = ""
                    else:
                        curve = new_curve(value, side, len(curves))
                curves.append(curve)
        self.figure_config.curves = curves

    def update_plot(self):
        self.plot_figure.clear()
        if self.data_df is None or self.data_df.empty:
            self.plot_canvas.draw()
            self.is_drawing = False
            return

        x_col = self.x_combo.currentText()
        if not x_col or x_col not in self.data_df.columns:
            self.plot_canvas.draw()
            self.is_drawing = False
            return
        try:
            self._render_publication_plot(x_col)
            self.plot_canvas.draw()
            self.is_drawing = True
        except Exception as exc:
            if self.figure_config.use_latex:
                try:
                    self.figure_config.use_latex = False
                    self.plot_figure.clear()
                    self._render_publication_plot(x_col)
                    self.plot_canvas.draw()
                    self.is_drawing = True
                    self.statusMessage.emit(f"LaTeX rendering failed; switched to MathText: {exc}", "warning")
                    return
                except Exception:
                    pass
            self.plot_canvas.draw()
            self.is_drawing = False
            self.statusMessage.emit(f"Figure rendering failed: {exc}", "error")
            QMessageBox.warning(self, "Figure Error", str(exc))

    def _render_publication_plot(self, x_col):
        cfg = self.figure_config
        self._apply_canvas_size()
        transparent = cfg.background == "Transparent"
        self.plot_figure.patch.set_facecolor("none" if transparent else "white")
        self.plot_figure.patch.set_alpha(0.0 if transparent else 1.0)
        left, right = cfg.margin_left_cm / cfg.width_cm, 1.0 - cfg.margin_right_cm / cfg.width_cm
        bottom, top = cfg.margin_bottom_cm / cfg.height_cm, 1.0 - cfg.margin_top_cm / cfg.height_cm
        if not (0 <= left < right <= 1 and 0 <= bottom < top <= 1):
            raise ValueError("Figure margins leave no plotting area.")
        self.plot_figure.subplots_adjust(left=left, right=right, bottom=bottom, top=top)

        x = pd.to_numeric(self.data_df[x_col], errors="coerce").to_numpy(dtype=float)
        base = self.plot_figure.add_subplot(111)
        self._apply_axis_scale(base, cfg.x_axis, "x")
        side_counts = {"left": 0, "right": 0}
        handles, labels = [], []
        latex = bool(cfg.use_latex and shutil.which("latex"))
        if cfg.use_latex and not latex:
            cfg.use_latex = False
            self.statusMessage.emit("LaTeX executable not found; using MathText.", "warning")

        for curve in [c for c in cfg.curves if c.visible and c.column in self.data_df.columns]:
            rank = side_counts[curve.side]
            side_counts[curve.side] += 1
            if curve.side == "left" and rank == 0:
                ax = base
            else:
                ax = base.twinx()
                ax.patch.set_visible(False)
                ax.xaxis.set_visible(False)
                if curve.side == "left":
                    ax.yaxis.set_label_position("left"); ax.yaxis.tick_left()
                    ax.spines["left"].set_position(("axes", 0.0))
            y = pd.to_numeric(self.data_df[curve.column], errors="coerce").to_numpy(dtype=float)
            mask = np.isfinite(x) & np.isfinite(y)
            if not np.any(mask):
                continue
            marker = "" if curve.marker == "None" else curve.marker
            line_style = "" if curve.linestyle == "None" else curve.linestyle
            line, = ax.plot(
                x[mask], y[mask], color=curve.color, linewidth=curve.linewidth,
                linestyle=line_style, marker=marker, markersize=curve.markersize,
                markerfacecolor=curve.marker_face_color, markeredgecolor=curve.marker_edge_color,
                markeredgewidth=curve.marker_edge_width, markevery=max(1, curve.markevery),
                label=curve.legend_text or curve.column,
            )
            handles.append(line); labels.append(curve.legend_text or curve.column)
            self._draw_error(ax, curve, x, y, mask)
            self._apply_axis_scale(ax, curve.axis, "y")
            self._style_y_axis(ax, curve, rank, latex)

        self._style_x_axis(base, x_col, latex)
        if side_counts["left"] == 0:
            base.tick_params(axis="y", left=False, labelleft=False)
        self._apply_global_spines(base, side_counts)
        if cfg.title.visible and cfg.title.text:
            title = base.set_title(cfg.title.text, **self._font_kwargs(cfg.title))
            title.set_usetex(latex)
        if cfg.legend.visible and handles:
            kwargs = dict(
                loc=cfg.legend.location, ncol=cfg.legend.columns,
                frameon=cfg.legend.frame_visible, facecolor=cfg.legend.face_color,
                edgecolor=cfg.legend.edge_color, framealpha=cfg.legend.frame_alpha,
                prop={"family": cfg.legend.font.font, "size": cfg.legend.font.size,
                      "weight": "bold" if cfg.legend.font.bold else "normal",
                      "style": "italic" if cfg.legend.font.italic else "normal"},
            )
            if cfg.legend.custom_anchor:
                kwargs["bbox_to_anchor"] = (cfg.legend.anchor_x, cfg.legend.anchor_y)
            legend = base.legend(handles, labels, **kwargs)
            for text in legend.get_texts():
                text.set_color(cfg.legend.font.color); text.set_usetex(latex)

    def _apply_canvas_size(self):
        cfg = self.figure_config
        width_in, height_in = cfg.width_cm / 2.54, cfg.height_cm / 2.54
        if hasattr(self, "plot_figure"):
            self.plot_figure.set_size_inches(width_in, height_in, forward=False)
        if hasattr(self, "plot_canvas"):
            screen = self.screen()
            dpi = screen.logicalDotsPerInch() if screen else 96.0
            self.plot_canvas.setFixedSize(max(100, round(width_in * dpi)), max(100, round(height_in * dpi)))

    def _apply_axis_scale(self, ax, style, dimension):
        setter = ax.set_xscale if dimension == "x" else ax.set_yscale
        setter({"Linear": "linear", "Log10": "log", "SymLog": "symlog"}.get(style.scale, "linear"))
        if not style.auto_range:
            (ax.set_xlim if dimension == "x" else ax.set_ylim)(style.minimum, style.maximum)
        if style.inverted:
            (ax.invert_xaxis if dimension == "x" else ax.invert_yaxis)()
        self._apply_locator_formatter(ax, style, dimension)

    def _apply_locator_formatter(self, ax, style, dimension):
        axis = ax.xaxis if dimension == "x" else ax.yaxis
        tick = style.tick
        if tick.manual_mode == "Positions":
            try:
                values = [float(v.strip()) for v in tick.positions.split(",") if v.strip()]
            except ValueError as exc:
                raise ValueError(f"Invalid manual tick list: {tick.positions}") from exc
            if values:
                axis.set_major_locator(ticker.FixedLocator(values))
        elif tick.manual_mode == "Start/Stop/Step" and tick.step > 0:
            count = int(np.floor((tick.stop - tick.start) / tick.step)) + 1
            if count > 10000:
                raise ValueError("Manual tick range creates too many ticks.")
            axis.set_major_locator(ticker.FixedLocator(tick.start + np.arange(max(0, count)) * tick.step))
        if tick.minor_visible:
            if style.scale == "Linear":
                axis.set_minor_locator(ticker.AutoMinorLocator())
            elif style.scale == "SymLog":
                axis.set_minor_locator(ticker.SymmetricalLogLocator(base=10, linthresh=1.0, subs=np.arange(2, 10)))
            else:
                axis.set_minor_locator(ticker.LogLocator(subs="auto"))
        else:
            axis.set_minor_locator(ticker.NullLocator())
        if tick.format_mode == "Fixed":
            axis.set_major_formatter(ticker.FormatStrFormatter(f"%.{tick.decimals}f"))
        elif tick.format_mode == "Scientific":
            formatter = ticker.ScalarFormatter(useMathText=True)
            formatter.set_scientific(True); formatter.set_powerlimits((0, 0)); axis.set_major_formatter(formatter)
        elif tick.format_mode == "Percent":
            axis.set_major_formatter(ticker.PercentFormatter(xmax=1.0, decimals=tick.decimals))

    @staticmethod
    def _font_kwargs(style):
        return dict(fontfamily=style.font, fontsize=style.size,
                    fontweight="bold" if style.bold else "normal",
                    fontstyle="italic" if style.italic else "normal", color=style.color)

    def _style_x_axis(self, ax, x_col, latex):
        style = self.figure_config.x_axis; tick = style.tick
        if style.label.visible:
            label = ax.set_xlabel(style.label.text or x_col, **self._font_kwargs(style.label)); label.set_usetex(latex)
        ax.tick_params(axis="x", which="major", bottom=tick.major_visible and tick.show_bottom,
                       top=tick.major_visible and tick.show_top,
                       direction=tick.direction, length=tick.major_length, width=tick.width, colors=tick.color)
        ax.tick_params(axis="x", which="minor", bottom=tick.minor_visible and tick.show_bottom,
                       top=tick.minor_visible and tick.show_top,
                       direction=tick.direction, length=tick.minor_length, width=tick.width, colors=tick.color)
        for label in ax.get_xticklabels():
            label.update(self._font_kwargs(tick.font)); label.set_usetex(latex)
        g = style.grid
        if g.visible:
            ax.grid(True, axis="x", which="major", color=g.color, linestyle=g.linestyle,
                    linewidth=g.linewidth, alpha=g.alpha)
        else:
            ax.grid(False, axis="x", which="major")

    def _style_y_axis(self, ax, curve, rank, latex):
        style = curve.axis; tick = style.tick; side = curve.side
        for name, spine in ax.spines.items():
            if name != side:
                spine.set_visible(False)
        spine = ax.spines[side]
        global_visible = self.figure_config.show_left_spine if side == "left" else self.figure_config.show_right_spine
        spine.set_visible(style.spine_visible and global_visible)
        spine.set_linewidth(style.spine_width); spine.set_color(style.spine_color)
        side_kwargs = {"left": side == "left" and tick.show_left,
                       "right": side == "right" and tick.show_right,
                       "labelleft": side == "left" and tick.show_left,
                       "labelright": side == "right" and tick.show_right}
        ax.tick_params(axis="y", which="major", direction="inout", length=0, width=tick.width,
                       colors=tick.color, pad=4 + rank * 28, **side_kwargs)
        ax.tick_params(axis="y", which="minor", direction=tick.direction, length=tick.minor_length,
                       width=tick.width, colors=tick.color, **side_kwargs)
        for label in ax.get_yticklabels():
            label.update(self._font_kwargs(tick.font)); label.set_usetex(latex)
        if style.label.visible:
            label = ax.set_ylabel(style.label.text or curve.column, labelpad=28 + rank * 48,
                                  **self._font_kwargs(style.label)); label.set_usetex(latex)
        self._apply_custom_tick_marker(ax, side, rank, tick)
        g = style.grid
        if g.visible:
            ax.grid(True, axis="y", which="major", color=g.color, linestyle=g.linestyle,
                    linewidth=g.linewidth, alpha=g.alpha)
        else:
            ax.grid(False, axis="y", which="major")

    def _apply_custom_tick_marker(self, ax, side, rank, tick_style):
        slots = [("out", 1), ("out", 0), ("out", -1), ("in", 0), ("in", 1), ("in", -1)]
        direction, slope = slots[min(rank, 5)]
        outward = -1 if side == "left" else 1
        dx = outward if direction == "out" else -outward
        marker = MplPath([(0.0, 0.0), (float(dx), float(slope) * 0.65)],
                         [MplPath.MOVETO, MplPath.LINETO])
        side_enabled = tick_style.show_left if side == "left" else tick_style.show_right
        for mtick in ax.yaxis.get_major_ticks():
            line = mtick.tick1line if side == "left" else mtick.tick2line
            line.set_visible(tick_style.major_visible and side_enabled); line.set_marker(marker)
            line.set_markersize(tick_style.major_length)
            line.set_markeredgewidth(tick_style.width); line.set_color(tick_style.color)

    def _apply_global_spines(self, ax, side_counts):
        cfg = self.figure_config; style = cfg.x_axis
        visibility = {"top": cfg.show_top_spine, "bottom": cfg.show_bottom_spine}
        for name, visible in visibility.items():
            if name in ax.spines:
                ax.spines[name].set_visible(visible)
                ax.spines[name].set_linewidth(style.spine_width); ax.spines[name].set_color(style.spine_color)
        if side_counts["left"] == 0:
            ax.spines["left"].set_visible(cfg.show_left_spine)
            ax.spines["left"].set_linewidth(style.spine_width); ax.spines["left"].set_color(style.spine_color)
        if side_counts["right"]:
            ax.spines["right"].set_visible(False)
        else:
            ax.spines["right"].set_visible(cfg.show_right_spine)
            ax.spines["right"].set_linewidth(style.spine_width); ax.spines["right"].set_color(style.spine_color)

    def _draw_error(self, ax, curve, x, y, mask):
        error = curve.error
        if error.mode == "None":
            return
        if error.source == "Column":
            if not error.column or error.column not in self.data_df.columns:
                return
            values = pd.to_numeric(self.data_df[error.column], errors="coerce").to_numpy(dtype=float)
        else:
            values = np.full_like(y, error.constant, dtype=float)
        if np.any(values[np.isfinite(values)] < 0):
            raise ValueError(f"{curve.column}: error values must be non-negative.")
        valid = mask & np.isfinite(values) & (values >= 0)
        if not np.any(valid):
            return
        xv, yv, ev = x[valid], y[valid], values[valid]
        if error.mode in ("Bars", "Bars + Band"):
            ax.errorbar(xv, yv, yerr=ev, fmt="none", errorevery=max(1, error.every),
                        ecolor=error.color, elinewidth=error.linewidth,
                        capsize=error.capsize, capthick=error.capthick)
        if error.mode in ("Band", "Bars + Band"):
            ax.fill_between(xv, yv - ev, yv + ev, color=error.fill_color,
                            alpha=error.fill_alpha, linewidth=0)

    def save_plot(self):
        if not hasattr(self, 'plot_figure') or not self.plot_figure.axes:
            self.statusMessage.emit("No figure to save.", "warning")
            return

        file_path, selected_filter = QFileDialog.getSaveFileName(
            self, "Export Figure", "",
            "PNG (*.png);;JPEG (*.jpg);;TIFF (*.tif *.tiff);;PDF (*.pdf);;SVG (*.svg);;All Files (*)"
        )
        if not file_path:
            return

        ext_map = {
            "PNG (*.png)": ".png",
            "JPEG (*.jpg)": ".jpg",
            "TIFF (*.tif *.tiff)": ".tif",
            "PDF (*.pdf)": ".pdf",
            "SVG (*.svg)": ".svg"
        }
        lower_path = file_path.lower()
        valid_exts = ['.png', '.jpg', '.jpeg', '.tif', '.tiff', '.pdf', '.svg']
        if not any(lower_path.endswith(ext) for ext in valid_exts):
            ext = ext_map.get(selected_filter, ".png")
            file_path += ext

        try:
            transparent = self.figure_config.background == "Transparent"
            self.plot_figure.savefig(
                file_path,
                dpi=self.figure_config.export_dpi,
                transparent=transparent,
                facecolor="none" if transparent else "white",
            )
            self.statusMessage.emit(f"Figure saved: {os.path.basename(file_path)}", "info")
        except Exception as e:
            self.statusMessage.emit(f"Failed to save figure: {e}", "error")
            QMessageBox.critical(self, "Save Error", f"Failed to save figure:\n{e}")

    def show_plot_context_menu(self, pos):
        menu = QMenu(self)
        draw_action = menu.addAction("Draw (Ctrl+D)")
        customize_action = menu.addAction("Property (Ctrl+P)")
        save_action = menu.addAction("Export Figure (Ctrl+S)")
        action = menu.exec(self.plot_canvas.mapToGlobal(pos))
        if action == draw_action:
            self.update_plot()
        elif action == customize_action:
            self.open_plot_customization_dialog()
        elif action == save_action:
            self.save_plot()

    def open_plot_customization_dialog(self):
        if self.data_df is None or self.data_df.empty:
            QMessageBox.warning(self, "No Data", "Please load statistic data first.")
            return
        self._lock_figure_updates()
        try:
            dialog = PlotPropertyDialog(
                self.figure_config,
                [str(c) for c in self.data_df.columns],
                self._apply_figure_config,
                self._save_figure_defaults,
                self,
            )
            dialog.exec()
        finally:
            self._unlock_figure_updates()

    def _apply_figure_config(self, config):
        self.figure_config = config
        left = [c.column for c in config.curves if c.side == "left"]
        right = [c.column for c in config.curves if c.side == "right"]
        self.y1_combo.set_checked_items(left)
        self.y2_combo.set_checked_items(right)
        self._apply_canvas_size()
        self.update_plot()

    def _save_figure_defaults(self, config):
        payload = config.to_dict(include_curves=False)
        templates = []
        for curve in config.to_dict(include_curves=True)["curves"]:
            curve["column"] = ""; curve["legend_text"] = ""; curve["axis"]["label"]["text"] = ""
            curve["error"]["column"] = ""
            templates.append(curve)
        payload["curve_templates"] = templates
        self.figure_settings.setValue(
            "figure/default_style_v1",
            json.dumps(payload, ensure_ascii=False),
        )
        self._curve_templates = FigureConfig.from_dict({"version": 1, "curves": templates}).curves
