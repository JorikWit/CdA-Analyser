"""Graphical User Interface for CDA analyzer (PyQt5 Version)"""

# Standard library imports
import sys
import os
import json
import argparse
import logging
import threading
import faulthandler
import tempfile
from pathlib import Path
import traceback

_logger = logging.getLogger(__name__)

# Third-party imports
import pandas as pd
import numpy as np
import folium
import matplotlib.pyplot as plt
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

# PyQt5 imports
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QLabel, QPushButton, QTextEdit, QLineEdit,
    QFileDialog, QMessageBox, QProgressBar, QScrollArea,
    QSplashScreen, QGridLayout, QFrame, QDialog, QSlider, QSpinBox, QCheckBox
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QUrl, QTimer, QRect, QByteArray
from PyQt5.QtGui import QFont, QIcon, QPixmap, QPainter, QBrush, QLinearGradient, QColor
from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEngineSettings

# Local module imports
from icon import LOGO_BASE64
from fit_parser import FITParser
from analyzer import CDAAnalyzer
from weather import WeatherService
from config import DEFAULT_PARAMETERS

_CRASH_APP = None
_CRASH_LOG_PATH = Path.cwd() / "cda_analyzer_crash.log"
_STAGE_LOG_PATH = Path.cwd() / "cda_analyzer_stage.log"
_FILE_LOG_ENABLED = False


def _append_crash_log(message):
    """Best-effort append to crash log file."""
    if not _FILE_LOG_ENABLED:
        return
    try:
        with _CRASH_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(message + "\n")
    except Exception:
        pass


def _mark_stage(stage):
    """Persist last known execution stage for native crash diagnostics."""
    if not _FILE_LOG_ENABLED:
        return
    try:
        with _STAGE_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(stage + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:
        pass


def _show_fatal_dialog(title, message):
    """Show a fatal error dialog if QApplication is available."""
    try:
        if _CRASH_APP is not None:
            QMessageBox.critical(None, title, message)
    except Exception:
        pass


def _python_excepthook(exc_type, exc_value, exc_tb):
    """Handle uncaught exceptions from Python main thread."""
    tb = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    text = f"[UNCAUGHT PYTHON EXCEPTION]\n{tb}"
    _append_crash_log(text)
    _logger.critical(text)
    extra = f"\n\nSee log: {_CRASH_LOG_PATH}" if _FILE_LOG_ENABLED else ""
    _show_fatal_dialog("Unhandled Error", f"An unexpected error occurred.\n\n{exc_value}{extra}")


def _threading_excepthook(args):
    """Handle uncaught exceptions from Python threads."""
    tb = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
    text = f"[UNCAUGHT THREAD EXCEPTION] thread={args.thread.name}\n{tb}"
    _append_crash_log(text)
    _logger.critical(text)
    extra = f"\n\nSee log: {_CRASH_LOG_PATH}" if _FILE_LOG_ENABLED else ""
    _show_fatal_dialog("Background Thread Error", f"A background thread crashed.\n\n{args.exc_value}{extra}")


def _qt_message_handler(mode, context, message):
    """Capture Qt warnings/errors that may not raise Python exceptions."""
    # Never let exceptions escape a Qt message handler.
    # Escaping here can terminate the process without a Python traceback.
    try:
        try:
            mode_name = {
                0: "QtDebugMsg",
                1: "QtWarningMsg",
                2: "QtCriticalMsg",
                3: "QtFatalMsg",
                4: "QtInfoMsg",
            }.get(int(mode), f"QtMsg({int(mode)})")
        except Exception:
            mode_name = "QtMsg"

        text = f"[{mode_name}] {message}"
        _append_crash_log(text)
    except Exception:
        pass


def _install_global_error_reporting(app, enable_file_log=False, crash_log_path=None):
    """Install global hooks so crashes always leave a report."""
    global _CRASH_APP, _FILE_LOG_ENABLED, _CRASH_LOG_PATH, _STAGE_LOG_PATH
    _CRASH_APP = app
    _FILE_LOG_ENABLED = bool(enable_file_log)

    if crash_log_path:
        _CRASH_LOG_PATH = Path(crash_log_path)
        _STAGE_LOG_PATH = _CRASH_LOG_PATH.with_name(_CRASH_LOG_PATH.stem + "_stage.log")

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    if _FILE_LOG_ENABLED:
        # Ensure logging has a persistent file sink.
        has_file_handler = any(
            isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", "") == str(_CRASH_LOG_PATH)
            for h in root_logger.handlers
        )
        if not has_file_handler:
            file_handler = logging.FileHandler(_CRASH_LOG_PATH, encoding="utf-8")
            file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
            root_logger.addHandler(file_handler)

    # Dump Python fault traces (segfaults, aborts) where possible.
    try:
        if _FILE_LOG_ENABLED:
            crash_file = _CRASH_LOG_PATH.open("a", encoding="utf-8")
            faulthandler.enable(crash_file, all_threads=True)
        else:
            faulthandler.enable(all_threads=True)
    except Exception as e:
        _append_crash_log(f"[WARN] Could not enable faulthandler: {e}")

    # Python-level uncaught exceptions.
    sys.excepthook = _python_excepthook
    try:
        threading.excepthook = _threading_excepthook
    except Exception:
        pass

    # Qt-level warnings/errors are redirected only when file logging is enabled.
    if _FILE_LOG_ENABLED:
        try:
            from PyQt5.QtCore import qInstallMessageHandler
            qInstallMessageHandler(_qt_message_handler)
        except Exception as e:
            _append_crash_log(f"[WARN] Could not install Qt message handler: {e}")

class WorkerThread(QThread):
    """Background thread for analysis"""
    finished = pyqtSignal(object, str, object)  # results, error, preprocessed_segments
    status = pyqtSignal(str)

    def __init__(self, analyzer, ride_data, weather_service):
        super().__init__()
        self.analyzer = analyzer
        self.ride_data = ride_data
        self.weather_service = weather_service

    def _emit_status(self, message):
        self.status.emit(message)
        _logger.info(message)

    def _prepare_elevation_for_analysis(self):
        use_api = bool(self.analyzer.parameters.get('use_open_elevation_api', False))

        has_api_altitude = (
            'altitude_api' in self.ride_data.columns and
            self.ride_data['altitude_api'].notna().any()
        )

        if use_api and has_api_altitude:
            self.ride_data['altitude'] = self.ride_data['altitude_api']
            self.analyzer.elevation_source = 'Open-Elevation API (preloaded at file load)'
            self._emit_status("Elevation source: Open-Elevation API (preloaded)")
            return

        if 'altitude_fit' in self.ride_data.columns:
            self.ride_data['altitude'] = self.ride_data['altitude_fit']
        self.analyzer.elevation_source = 'FIT file'
        if use_api and not has_api_altitude:
            self._emit_status("Elevation API selected, but no preloaded API altitude found: using FIT altitude")
        else:
            self._emit_status("Elevation source: FIT file")

    def run(self):
        try:
            _mark_stage("worker:run:start")
            self._prepare_elevation_for_analysis()
            _mark_stage("worker:run:after_elevation")
            # Preprocess the ride data first
            preprocessed_segments = self.analyzer.preprocess_ride_data(self.ride_data, self.weather_service)
            _mark_stage("worker:run:after_preprocess")
            # Then analyze with the preprocessed segments
            results = self.analyzer.analyze_ride(self.ride_data, self.weather_service, preprocessed_segments)
            _mark_stage("worker:run:after_analyze")
            self.finished.emit(results, None, preprocessed_segments)
        except Exception as e:
            _mark_stage("worker:run:exception")
            tb = traceback.format_exc()
            _append_crash_log(f"[WORKER EXCEPTION]\n{tb}")
            _logger.exception("Worker thread failed")
            self.finished.emit(None, f"{e}\n\n{tb}", None)

class CustomProgress(QProgressBar):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pos = 0
        self._animate = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._advance)

    def setRange(self, minimum, maximum):
        super().setRange(minimum, maximum)
        if minimum == 0 and maximum == 0:
            self._startIndeterminate()
        else:
            self._stopIndeterminate()

    def _startIndeterminate(self):
        if not self._animate:
            self._animate = True
            self._pos = 0
            self._timer.start(30)
            self.update()

    def _stopIndeterminate(self):
        if self._animate:
            self._animate = False
            self._timer.stop()
            self.update()

    def _advance(self):
        self._pos += 5
        if self._pos > self.width():
            self._pos = -self.width() // 10
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        rect = self.rect()

        # Background
        painter.setBrush(QColor("#e0e0e0"))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(rect, 5, 5)

        if self._animate:
            # 10% wide moving chunk
            chunk_width = rect.width() // 10
            chunk_rect = QRect(self._pos, rect.y(), chunk_width, rect.height())

            gradient = QLinearGradient(chunk_rect.topLeft(), chunk_rect.topRight())
            gradient.setColorAt(0, QColor("#2196F3"))
            gradient.setColorAt(1, QColor("#21CBF3"))

            painter.setBrush(QBrush(gradient))
            painter.drawRoundedRect(chunk_rect, 5, 5)

            # Optional: show text as "Loading..."
            painter.setPen(Qt.black)
            painter.drawText(rect, Qt.AlignCenter, "Loading...")

        else:
            # Normal percentage mode
            if self.maximum() > 0:
                fill_width = int(rect.width() * self.value() / self.maximum())
                if fill_width > 0:
                    chunk_rect = QRect(rect.x(), rect.y(), fill_width, rect.height())

                    gradient = QLinearGradient(chunk_rect.topLeft(), chunk_rect.topRight())
                    gradient.setColorAt(0, QColor("#2196F3"))
                    gradient.setColorAt(1, QColor("#21CBF3"))

                    painter.setBrush(QBrush(gradient))
                    painter.drawRoundedRect(chunk_rect, 5, 5)

            # Draw progress text
            painter.setPen(Qt.black)
            painter.drawText(rect, Qt.AlignCenter, f"{self.value()}%")

def resource_path(relative_path):
    """Get absolute path to resource, works for dev and for PyInstaller."""
    try:
        base_path = sys._MEIPASS
    except AttributeError:
        base_path = os.path.abspath(".")
    path = os.path.join(base_path, relative_path)
    if not os.path.exists(path):
        alt_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), relative_path)
        if os.path.exists(alt_path):
            path = alt_path
        else:
            _logger.warning("Resource not found: %s", path)
    return path

class GUIInterface(QMainWindow):
    def __init__(self, app):
        super().__init__()
        self.app = app  # Store reference
        self.setWindowTitle("CdA Analyzer")
        self.resize(1200, 1000)
        self._set_window_icon()

        # Initialize data
        self.fit_file_path = None
        self.parameters = DEFAULT_PARAMETERS.copy()
        self.ride_data = None
        self.analysis_results = None
        self.preprocessed_segments = None
        self.simulation_results = None
        self.segment_data_map = {}
        self.current_figure = None
        self.current_canvas = None
        self.sim_figure = None
        self.sim_canvas = None
        self.worker = None
        self._map_html_path = None
        self.load_weather_api_on_file_load = False
        self.load_elevation_api_on_file_load = False
        self.weather_api_loaded = False
        self.elevation_api_loaded = False
        self.preloaded_weather_samples = []

        self.home_directory = os.path.expanduser('~')
        self.downloads_path = os.path.join(self.home_directory, 'Downloads')
        self.last_browse_path = os.path.abspath('~')

        self.analyzer = CDAAnalyzer(self.parameters)
        self.weather_service = WeatherService()

        # Setup UI immediately — window is already about to be shown
        self._setup_ui()

        # Bring to front
        self.raise_()
        self.activateWindow()

    def _setup_ui(self):
        """Setup the user interface with PyQt5"""
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        # Tab widget
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        self._setup_file_tab()
        self._setup_parameters_tab()
        self._setup_results_tab()
        self._setup_simulation_tab()

    def _show_about_dialog(self):
        dialog = QDialog(self, flags=Qt.Dialog | Qt.WindowTitleHint | Qt.WindowCloseButtonHint)
        dialog.setWindowTitle("About CdA Analyzer")
        dialog.setFixedWidth(400)  # Optional: fixed width

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        # Logo
        logo_data = QByteArray.fromBase64(LOGO_BASE64.encode('utf-8'))
        pixmap = QPixmap()
        pixmap.loadFromData(logo_data)
        pixmap = pixmap.scaledToWidth(80, Qt.SmoothTransformation)  # Smaller logo
        logo_label = QLabel()
        logo_label.setPixmap(pixmap)
        logo_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(logo_label)

        # Text
        about_text = """
        <b>CdA Analyzer</b><br>
        Version 1.0<br><br>
        <b>Author:</b> Jorik Wittevrongel<br>
        <b>GitHub:</b> <a href='https://github.com/JorikWit/CdA-Analyser'>https://github.com/JorikWit/CdA-Analyser</a><br><br>

        This program is licensed under the
        GNU General Public License v3.0 (GPLv3).<br>
        See the LICENSE file for details.<br><br>

        <b>Third-party libraries:</b><br>
        - fitparse (BSD License)<br>
        - folium (MIT License)<br>
        - geopy (MIT License)<br>
        - matplotlib (Matplotlib License, BSD-compatible)<br>
        - numpy (BSD-3-Clause)<br>
        - pandas (BSD-3-Clause)<br>
        - Pillow (PIL Software License, similar to MIT)<br>
        - PyQt5 (GPL v3)<br>
        - PyQt5_sip (GPL v3)<br>
        - requests (Apache-2.0)<br>
        - scipy (BSD License)<br>
        """
        text_label = QLabel(about_text)
        text_label.setTextFormat(Qt.RichText)
        text_label.setOpenExternalLinks(True)  # Make GitHub link clickable
        text_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        layout.addWidget(text_label)

        # OK button
        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(dialog.accept)
        ok_btn.setFixedWidth(80)
        ok_btn.setDefault(True)
        ok_btn.setAutoDefault(True)
        ok_btn.setCursor(Qt.PointingHandCursor)
        layout.addWidget(ok_btn, alignment=Qt.AlignCenter)

        dialog.exec_()

    def _setup_file_tab(self):
        self.file_frame = QWidget()
        layout = QGridLayout(self.file_frame)
        layout.setContentsMargins(10, 10, 10, 10)  # Tight margins for each row

        # Title
        title = QLabel("Select FIT File:")
        title.setFont(QFont("Arial", 12, QFont.Bold))
        layout.addWidget(title, 0, 0, 1, 7, alignment=Qt.AlignCenter)

        # Browse button and file label (row 1)
        browse_btn = QPushButton("Browse FIT File")
        browse_btn.clicked.connect(self._browse_fit_file)
        layout.addWidget(browse_btn, 1, 2)

        self.file_label = QLabel("No file selected")
        self.file_label.setStyleSheet("color: #555;")
        layout.addWidget(self.file_label, 1, 4)

        # API call options during file load
        self.load_weather_api_checkbox = QCheckBox("Call Weather API on file load")
        self.load_weather_api_checkbox.setChecked(False)
        layout.addWidget(self.load_weather_api_checkbox, 2, 2, 1, 2)

        self.load_elevation_api_checkbox = QCheckBox("Call Elevation API on file load")
        self.load_elevation_api_checkbox.setChecked(False)
        layout.addWidget(self.load_elevation_api_checkbox, 2, 4, 1, 2)

        # File status (row 3) - expandable
        self.file_status = QTextEdit()
        self.file_status.setReadOnly(True)
        layout.addWidget(self.file_status, 3, 0, 1, 7)  # Span both columns

        # About button in corner (row 0, column 6)
        about_btn = QPushButton("?")
        about_btn.setFixedSize(25, 25)  # Small square button
        about_btn.setToolTip("About this program")
        about_btn.clicked.connect(self._show_about_dialog)
        layout.addWidget(about_btn, 0, 6, alignment=Qt.AlignTop | Qt.AlignRight)

        # Set row stretch so file_status can expand if window is resized
        layout.setRowStretch(3, 1)  # Give extra vertical space to file_status row

        # Optional: make columns expand properly
        layout.setColumnStretch(0, 2)
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(2, 1)
        layout.setColumnStretch(3, 1)
        layout.setColumnStretch(4, 1)
        layout.setColumnStretch(5, 1)
        layout.setColumnStretch(6, 2)

        self.tabs.addTab(self.file_frame, "File Selection")

    def _setup_parameters_tab(self):
        self.parameters_frame = QWidget()
        layout = QVBoxLayout(self.parameters_frame)

        # Title
        title = QLabel("Analysis Parameters:")
        title.setFont(QFont("Arial", 12, QFont.Bold))
        layout.addWidget(title, alignment=Qt.AlignCenter)

        # Button layout
        button_layout = QHBoxLayout()
        run_btn = QPushButton("Run Analysis")
        run_btn.clicked.connect(self._run_analysis)
        button_layout.addStretch()
        button_layout.addWidget(run_btn)
        button_layout.addStretch()
        layout.addLayout(button_layout)

        # Scroll Area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        
        # 👇 Remove spacing and margins
        scroll_layout.setSpacing(0)  # No space between rows
        scroll_layout.setContentsMargins(0, 0, 0, 0)  # Optional: remove inner padding

        self.param_entries = {}
        self.param_checkboxes = {}
        hidden_parameter_keys = {'weather_sample_distance_m'}
        for key, value in self.parameters.items():
            if key in hidden_parameter_keys:
                continue

            row = QHBoxLayout()
            row.setSpacing(10)  # Optional: small spacing within the row
            row.setContentsMargins(10, 10, 0, 0)  # Tight margins for each row

            label = QLabel(key.replace('_', ' ').title())
            label.setFixedWidth(200)
            
            # Handle boolean parameters as checkboxes
            if isinstance(value, bool):
                checkbox = QCheckBox()
                checkbox.setChecked(value)
                self.param_checkboxes[key] = checkbox
                row.addWidget(label)
                row.addWidget(checkbox)
            else:
                entry = QLineEdit(str(value))
                entry.setFixedWidth(150)
                self.param_entries[key] = entry
                row.addWidget(label)
                row.addWidget(entry)

            row.addStretch()
            scroll_layout.addLayout(row)

        scroll_layout.addStretch()
        scroll.setWidget(scroll_content)
        layout.addWidget(scroll)

        self.tabs.addTab(self.parameters_frame, "Parameters")

    def _setup_results_tab(self):
        self.results_frame = QWidget()
        layout = QVBoxLayout(self.results_frame)

        # Title
        title = QLabel("Analysis:")
        title.setFont(QFont("Arial", 12, QFont.Bold))
        layout.addWidget(title, alignment=Qt.AlignCenter)

        # Analysis status
        analysis_layout = QVBoxLayout()
        self.progress = CustomProgress()
        self.progress.setRange(0, 100)  # Indeterminate
        self.progress.setValue(0)   
        self.progress.setAlignment(Qt.AlignCenter)  # center text in bar

        #self.progress.setVisible(False)
        self.analysis_status = QLabel("Ready to analyze")
        analysis_layout.addWidget(self.progress)
        analysis_layout.addWidget(self.analysis_status, alignment=Qt.AlignCenter)
        layout.addLayout(analysis_layout)

        # Wind effect factor slider (visible in all tabs)
        wind_effect_layout = QHBoxLayout()
        wind_effect_label = QLabel("Wind Effect Factor:")
        wind_effect_label.setFont(QFont("Arial", 10, QFont.Bold))
        wind_effect_layout.addWidget(wind_effect_label)
        
        self.wind_effect_slider = QSlider(Qt.Horizontal)
        self.wind_effect_slider.setMinimum(0)
        self.wind_effect_slider.setMaximum(100)
        self.wind_effect_slider.setValue(int(self.analyzer.parameters['wind_effect_factor'] * 100))
        self.wind_effect_slider.setTickPosition(QSlider.TicksBelow)
        self.wind_effect_slider.setTickInterval(10)
        self.wind_effect_slider.valueChanged.connect(self._on_wind_effect_slider_moved)
        self.wind_effect_slider.sliderReleased.connect(self._on_wind_effect_changed)
        wind_effect_layout.addWidget(self.wind_effect_slider)
        
        self.wind_effect_value_label = QLabel(f"{self.analyzer.parameters['wind_effect_factor']:.2f}")
        self.wind_effect_value_label.setFont(QFont("Arial", 10))
        self.wind_effect_value_label.setMinimumWidth(40)
        wind_effect_layout.addWidget(self.wind_effect_value_label)
        
        layout.addLayout(wind_effect_layout)

        # Results notebook (tabs)
        self.results_notebook = QTabWidget()
        layout.addWidget(self.results_notebook, 1)

        # Summary tab
        self.summary_frame = QWidget()
        sum_layout = QVBoxLayout(self.summary_frame)
        
        self.summary_text = QTextEdit()
        self.summary_text.setReadOnly(True)
        self.summary_text.append("Run analysis to see results here")
        sum_layout.addWidget(self.summary_text)
        self.results_notebook.addTab(self.summary_frame, "Summary")

        # Map tab — now with QWebEngineView
        self.map_frame = QWidget()
        map_layout = QVBoxLayout(self.map_frame)

        self.map_webview = QWebEngineView()
        self.map_webview.setMinimumHeight(400)
        self.map_webview.settings().setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
        map_layout.addWidget(self.map_webview)

        self.map_refresh_btn = QPushButton("Generate / Refresh Map")
        self.map_refresh_btn.clicked.connect(self._generate_map)
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()                 
        btn_layout.addWidget(self.map_refresh_btn)
        btn_layout.addStretch()                
        map_layout.addLayout(btn_layout)

        self.results_notebook.addTab(self.map_frame, "Map")

        # Plot tab (unchanged)
        self.plot_frame = QWidget()
        plot_layout = QVBoxLayout(self.plot_frame)
        self.plot_label = QLabel("Plots will be displayed here after analysis")
        self.plot_label.setAlignment(Qt.AlignCenter)
        plot_layout.addWidget(self.plot_label)
        self.plot_button = QPushButton("Generate Plots")
        self.plot_button.clicked.connect(self._generate_plots)
        plot_layout.addWidget(self.plot_button)
        self.results_notebook.addTab(self.plot_frame, "Plots")

        # Keep for proper export in future
        # # Bottom buttons
        # btn_layout = QHBoxLayout()
        # prev_btn = QPushButton("← Previous")
        # prev_btn.clicked.connect(lambda: self.tabs.setCurrentWidget(self.parameters_frame))
        # export_btn = QPushButton("Export Results")
        # export_btn.clicked.connect(self._export_results)
        # btn_layout.addWidget(prev_btn)
        # btn_layout.addStretch()
        # btn_layout.addWidget(export_btn)
        # layout.addLayout(btn_layout)

        self.tabs.addTab(self.results_frame, "Results & Analysis")

    def _setup_simulation_tab(self):
        """Setup weather simulation tab"""
        self.simulation_frame = QWidget()
        layout = QVBoxLayout(self.simulation_frame)

        # Title
        title = QLabel("Weather Simulation:")
        title.setFont(QFont("Arial", 12, QFont.Bold))
        layout.addWidget(title, alignment=Qt.AlignCenter)

        # Controls frame
        controls_frame = QWidget()
        controls_layout = QVBoxLayout(controls_frame)
        
        # Wind Speed slider
        wind_speed_layout = QHBoxLayout()
        wind_speed_label = QLabel("Wind Speed (m/s):")
        wind_speed_label.setFont(QFont("Arial", 10, QFont.Bold))
        wind_speed_label.setFixedWidth(150)
        wind_speed_layout.addWidget(wind_speed_label)
        
        self.sim_wind_speed_slider = QSlider(Qt.Horizontal)
        self.sim_wind_speed_slider.setMinimum(0)
        self.sim_wind_speed_slider.setMaximum(200)  # 0-20 m/s
        self.sim_wind_speed_slider.setValue(0)
        self.sim_wind_speed_slider.setTickPosition(QSlider.TicksBelow)
        self.sim_wind_speed_slider.setTickInterval(20)
        self.sim_wind_speed_slider.valueChanged.connect(self._on_simulation_params_changed)
        wind_speed_layout.addWidget(self.sim_wind_speed_slider)
        
        self.sim_wind_speed_value = QLabel("0.0")
        self.sim_wind_speed_value.setFixedWidth(50)
        wind_speed_layout.addWidget(self.sim_wind_speed_value)
        
        controls_layout.addLayout(wind_speed_layout)
        
        # Wind Angle slider
        wind_angle_layout = QHBoxLayout()
        wind_angle_label = QLabel("Wind Angle (°):")
        wind_angle_label.setFont(QFont("Arial", 10, QFont.Bold))
        wind_angle_label.setFixedWidth(150)
        wind_angle_layout.addWidget(wind_angle_label)
        
        self.sim_wind_angle_slider = QSlider(Qt.Horizontal)
        self.sim_wind_angle_slider.setMinimum(-180)
        self.sim_wind_angle_slider.setMaximum(180)
        self.sim_wind_angle_slider.setValue(0)
        self.sim_wind_angle_slider.setTickPosition(QSlider.TicksBelow)
        self.sim_wind_angle_slider.setTickInterval(45)
        self.sim_wind_angle_slider.valueChanged.connect(self._on_simulation_params_changed)
        wind_angle_layout.addWidget(self.sim_wind_angle_slider)
        
        self.sim_wind_angle_value = QLabel("0")
        self.sim_wind_angle_value.setFixedWidth(50)
        wind_angle_layout.addWidget(self.sim_wind_angle_value)
        
        controls_layout.addLayout(wind_angle_layout)
        
        # Wind Effect Factor slider
        wind_factor_layout = QHBoxLayout()
        wind_factor_label = QLabel("Wind Effect Factor:")
        wind_factor_label.setFont(QFont("Arial", 10, QFont.Bold))
        wind_factor_label.setFixedWidth(150)
        wind_factor_layout.addWidget(wind_factor_label)
        
        self.sim_wind_factor_slider = QSlider(Qt.Horizontal)
        self.sim_wind_factor_slider.setMinimum(0)
        self.sim_wind_factor_slider.setMaximum(100)
        self.sim_wind_factor_slider.setValue(int(self.analyzer.parameters['wind_effect_factor'] * 100))
        self.sim_wind_factor_slider.setTickPosition(QSlider.TicksBelow)
        self.sim_wind_factor_slider.setTickInterval(10)
        self.sim_wind_factor_slider.valueChanged.connect(self._on_simulation_params_changed)
        wind_factor_layout.addWidget(self.sim_wind_factor_slider)
        
        self.sim_wind_factor_value = QLabel(f"{self.analyzer.parameters['wind_effect_factor']:.2f}")
        self.sim_wind_factor_value.setFixedWidth(50)
        wind_factor_layout.addWidget(self.sim_wind_factor_value)
        
        controls_layout.addLayout(wind_factor_layout)

        # Temperature (°C)
        temp_layout = QHBoxLayout()
        temp_label = QLabel("Temperature (°C):")
        temp_label.setFont(QFont("Arial", 10, QFont.Bold))
        temp_label.setFixedWidth(150)
        temp_layout.addWidget(temp_label)

        self.sim_temp_entry = QLineEdit("15.0")
        self.sim_temp_entry.setFixedWidth(80)
        temp_layout.addWidget(self.sim_temp_entry)
        temp_layout.addStretch()
        controls_layout.addLayout(temp_layout)

        # Air Pressure (hPa)
        press_layout = QHBoxLayout()
        press_label = QLabel("Air Pressure (hPa):")
        press_label.setFont(QFont("Arial", 10, QFont.Bold))
        press_label.setFixedWidth(150)
        press_layout.addWidget(press_label)

        self.sim_pressure_entry = QLineEdit("1013.25")
        self.sim_pressure_entry.setFixedWidth(80)
        press_layout.addWidget(self.sim_pressure_entry)
        press_layout.addStretch()
        controls_layout.addLayout(press_layout)
        
        # Simulate button
        simulate_btn = QPushButton("Run Simulation")
        simulate_btn.clicked.connect(self._run_simulation)
        controls_layout.addWidget(simulate_btn)
        
        layout.addWidget(controls_frame)
        
        # Results notebook (tabs)
        self.simulation_notebook = QTabWidget()
        layout.addWidget(self.simulation_notebook, 1)

        # Summary tab
        self.sim_summary_frame = QWidget()
        sim_sum_layout = QVBoxLayout(self.sim_summary_frame)
        self.sim_summary_text = QTextEdit()
        self.sim_summary_text.setReadOnly(True)
        self.sim_summary_text.append("Run simulation to see results here")
        sim_sum_layout.addWidget(self.sim_summary_text)
        self.simulation_notebook.addTab(self.sim_summary_frame, "Summary")

        # Plots tab
        self.sim_plot_frame = QWidget()
        sim_plot_layout = QVBoxLayout(self.sim_plot_frame)
        self.sim_plot_label = QLabel("Plots will be displayed here after simulation")
        self.sim_plot_label.setAlignment(Qt.AlignCenter)
        sim_plot_layout.addWidget(self.sim_plot_label)
        self.simulation_notebook.addTab(self.sim_plot_frame, "Plots")

        self.tabs.addTab(self.simulation_frame, "Weather Simulation")

    def _browse_fit_file(self):
        # Adjust to proper default path
        if not os.path.exists(self.last_browse_path) or self.last_browse_path == '~':
            self.last_browse_path = os.path.join(self.home_directory, 'Downloads')
        
        path, _ = QFileDialog.getOpenFileName(
            self, "Select FIT File", self.last_browse_path, "FIT files (*.fit);;All files (*)"
        )
        
        if path:
            self.fit_file_path = path
            self.last_browse_path = os.path.dirname(path)
            self.file_label.setText(Path(path).name)
            self._load_fit_file()

    def _load_fit_file(self):
        if not self.fit_file_path:
            QMessageBox.critical(self, "Error", "Please select a FIT file first")
            return
        if not self._can_load_new_file():
            QMessageBox.warning(
                self,
                "Analysis running",
                "Wait for the current analysis to finish before loading a new FIT file."
            )
            return
        try:
            _mark_stage("ui:load_fit:start")
            self.file_status.clear()
            self.file_status.append("Loading FIT file...\n")
            QApplication.processEvents()

            # Save current parameters from UI (including checkbox state) BEFORE loading
            self._save_parameters()

            use_weather_api_on_load = bool(self.load_weather_api_checkbox.isChecked())
            use_elevation_api_on_load = bool(self.load_elevation_api_checkbox.isChecked())

            self.weather_api_loaded = False
            self.elevation_api_loaded = False
            self.preloaded_weather_samples = []

            fit_parser = FITParser()
            self.ride_data = fit_parser.parse_fit_file(
                self.fit_file_path,
                use_open_elevation_api=use_elevation_api_on_load,
            )
            elev_source = fit_parser.elevation_source
            self.file_status.append(f"Successfully loaded {len(self.ride_data)} data points\n")

            if use_elevation_api_on_load:
                self.elevation_api_loaded = (
                    'altitude_api' in self.ride_data.columns and
                    self.ride_data['altitude_api'].notna().any()
                )
                if self.elevation_api_loaded:
                    self.file_status.append("Elevation API done: preloaded altitude available\n")
                else:
                    self.file_status.append("Elevation API selected but no altitude API data available\n")
            else:
                self.file_status.append("Elevation API on load: disabled\n")

            if use_weather_api_on_load:
                self._prefetch_weather_api_on_load()
            else:
                self.file_status.append("Weather API on load: disabled\n")

            cols = ', '.join(self.ride_data.columns[:10])
            self.file_status.append(f"Columns: {cols}\n")
            if len(self.ride_data.columns) > 10:
                self.file_status.append(f"... and {len(self.ride_data.columns) - 10} more\n")

            # Parameter tab checkboxes are calculation-only; enabled only if preloaded data exists.
            self.parameters['use_open_elevation_api'] = bool(self.elevation_api_loaded)
            self.parameters['use_weather_api'] = bool(self.weather_api_loaded)

            self.analyzer = CDAAnalyzer(self.parameters)
            self.analyzer.elevation_source = elev_source
            self.analyzer.preloaded_weather_samples = list(self.preloaded_weather_samples)
            self.analyzer.allow_runtime_weather_fetch = False
            self.weather_service = WeatherService()

            if 'use_open_elevation_api' in self.param_checkboxes:
                self.param_checkboxes['use_open_elevation_api'].setChecked(self.parameters['use_open_elevation_api'])
            if 'use_weather_api' in self.param_checkboxes:
                self.param_checkboxes['use_weather_api'].setChecked(self.parameters['use_weather_api'])

            self._enable_segment_parameters()
            self._cleanup_results(full_reset=True)
            self.tabs.setCurrentWidget(self.parameters_frame)
            _mark_stage("ui:load_fit:done")
        except Exception as e:
            _mark_stage("ui:load_fit:exception")
            self.file_status.append(f"Error loading FIT file: {str(e)}\n")
            QMessageBox.critical(self, "Error", str(e))

    def _can_load_new_file(self):
        """Return False when analysis worker is still running.

        Loading a new FIT while background analysis is active can leave Qt objects
        in an inconsistent state and cause hard-to-reproduce crashes.
        """
        return not (self.worker is not None and self.worker.isRunning())

    def _save_parameters(self):
        try:
            for key, entry in self.param_entries.items():
                value = entry.text()
                orig = DEFAULT_PARAMETERS[key]
                if isinstance(orig, int):
                    self.parameters[key] = int(value)
                elif isinstance(orig, float):
                    self.parameters[key] = float(value)
                else:
                    self.parameters[key] = value
            
            # Handle checkboxes for boolean parameters
            for key, checkbox in self.param_checkboxes.items():
                self.parameters[key] = checkbox.isChecked()

            # Persist file-load API selection checkboxes.
            if hasattr(self, 'load_weather_api_checkbox'):
                self.load_weather_api_on_file_load = self.load_weather_api_checkbox.isChecked()
            if hasattr(self, 'load_elevation_api_checkbox'):
                self.load_elevation_api_on_file_load = self.load_elevation_api_checkbox.isChecked()
            
            # Update slider if wind_effect_factor changed
            if 'wind_effect_factor' in self.parameters:
                factor_value = self.parameters['wind_effect_factor']
                slider_value = int(factor_value * 100)
                self.wind_effect_slider.blockSignals(True)
                self.wind_effect_slider.setValue(slider_value)
                self.wind_effect_slider.blockSignals(False)
                self.wind_effect_value_label.setText(f"{factor_value:.2f}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error saving parameters: {str(e)}")

    def _sync_api_parameter_checkbox_state(self):
        """Enable/disable API parameter checkboxes based on preloaded data availability."""
        weather_key = 'use_weather_api'
        elevation_key = 'use_open_elevation_api'

        if weather_key in self.param_checkboxes:
            checkbox = self.param_checkboxes[weather_key]
            if self.weather_api_loaded:
                checkbox.setEnabled(True)
            else:
                checkbox.setChecked(False)
                checkbox.setEnabled(False)
                self.parameters[weather_key] = False

        if elevation_key in self.param_checkboxes:
            checkbox = self.param_checkboxes[elevation_key]
            if self.elevation_api_loaded:
                checkbox.setEnabled(True)
            else:
                checkbox.setChecked(False)
                checkbox.setEnabled(False)
                self.parameters[elevation_key] = False

    def _prefetch_weather_api_on_load(self):
        """Prefetch weather data for the full route at 3km local-time intervals."""
        self.preloaded_weather_samples = []
        self.weather_api_loaded = False

        if self.ride_data is None:
            return

        sample_distance_m = float(self.parameters.get('weather_sample_distance_m', 3000.0))
        self.file_status.append(f"Weather API: preloading route weather every {sample_distance_m:.0f} m...")
        QApplication.processEvents()

        prefetch = self.weather_service.prefetch_weather_for_ride(
            self.ride_data,
            sample_distance_m=sample_distance_m,
            status_callback=lambda msg: self.file_status.append(msg),
        )
        self.preloaded_weather_samples = prefetch.get('samples', [])
        self.weather_api_loaded = len(self.preloaded_weather_samples) > 0

        self.file_status.append(
            f"Weather API done: samples={prefetch.get('sample_count', 0)}, "
            f"grouped_calls={prefetch.get('grouped_request_count', 0)}"
        )
        QApplication.processEvents()

    def _disable_segment_parameters(self):
        for i, key in enumerate(list(self.parameters.keys())[:8]):
            if key in self.param_entries:
                self.param_entries[key].setEnabled(False)
            if key in self.param_checkboxes:
                self.param_checkboxes[key].setEnabled(False)

        self._sync_api_parameter_checkbox_state()

    def _enable_segment_parameters(self):
        for i, key in enumerate(list(self.parameters.keys())[:8]):
            if key in self.param_entries:
                self.param_entries[key].setEnabled(True)
            if key in self.param_checkboxes:
                self.param_checkboxes[key].setEnabled(True)

        self._sync_api_parameter_checkbox_state()

    def _safe_delete_canvas(self, canvas):
        if canvas is None:
            return
        layout = canvas.parentWidget().layout() if canvas.parentWidget() else None
        if layout:
            layout.removeWidget(canvas)
        canvas.setParent(None)
        canvas.deleteLater()

    def _cleanup_results(self, full_reset=False):
        if self.summary_text:
            self.summary_text.clear()
            self.summary_text.append("Run analysis to see results here")

        if self.current_figure:
            # Keep canvas alive and clear figure to avoid draw_idle callbacks
            # targeting a deleted FigureCanvasQTAgg.
            self.current_figure.clear()
            if self.current_canvas:
                self.current_canvas.draw()

        if full_reset:
            self.analysis_results = None
            self.preprocessed_segments = None
            self.simulation_results = None
            self.segment_data_map = {}

            if self.sim_summary_text:
                self.sim_summary_text.clear()
                self.sim_summary_text.append("Run simulation to see results here")

            if self.sim_figure:
                self.sim_figure.clear()
                if self.sim_canvas:
                    self.sim_canvas.draw()

            if self.map_webview:
                self.map_webview.setHtml("<html><body><p>Run analysis to display map</p></body></html>")

    def _run_analysis(self):
        if self.ride_data is None:
            QMessageBox.critical(self, "Error", "Please load a FIT file first")
            return

        _mark_stage("ui:run_analysis:start")

        self._cleanup_results()
        self.summary_text.clear()
        self.summary_text.append("Running analysis...")
        self._save_parameters()
        self.analyzer.update_parameters(self.parameters)
        self.analyzer.preloaded_weather_samples = list(self.preloaded_weather_samples)
        self.analyzer.allow_runtime_weather_fetch = False

        self.tabs.setCurrentWidget(self.results_frame)
        #self.progress.setVisible(True)
        self.progress.setRange(0, 0)  # Indeterminate
        self.analysis_status.setText("Running analysis in background...")

        # Run in thread
        self.worker = WorkerThread(self.analyzer, self.ride_data, self.weather_service)
        self.worker.status.connect(self._on_worker_status)
        self.worker.finished.connect(self._on_analysis_complete)
        self.worker.start()
        _mark_stage("ui:run_analysis:worker_started")

    def _on_worker_status(self, message):
        self.analysis_status.setText(message)
        if self.summary_text:
            self.summary_text.append(message)
        if self.file_status:
            self.file_status.append(message)

    def _on_analysis_complete(self, results, error, preprocessed_segments):
        try:
            _mark_stage("ui:on_analysis_complete:start")
            #self.progress.setVisible(False)
            self.progress.setRange(0, 100)
            self.progress.setValue(100)
            self.analysis_status.setText("Analysis complete!" if not error else "Analysis failed")
            self.summary_text.clear()

            if error:
                _mark_stage("ui:on_analysis_complete:error")
                self.summary_text.append(f"<b>Error during analysis:</b> {error}")
                QMessageBox.critical(self, "Error", f"Analysis failed: {error}")
                self.analysis_results = None
                self.preprocessed_segments = None
            else:
                _mark_stage("ui:on_analysis_complete:success_path")
                self.analysis_results = results
                self.preprocessed_segments = preprocessed_segments
                self._create_segment_mapping()
                _mark_stage("ui:on_analysis_complete:after_mapping")
                self._display_analysis_results()
                _mark_stage("ui:on_analysis_complete:after_summary")

                # Auto-generate visuals, but isolate failures so UI remains usable.
                self.tabs.setCurrentWidget(self.results_frame)
                self._auto_generate_visuals()

                # Return to summary after auto-generation.
                self.results_notebook.setCurrentWidget(self.summary_frame)
                self.analysis_status.setText("Analysis complete!")
                _mark_stage("ui:on_analysis_complete:done")
        except Exception:
            _mark_stage("ui:on_analysis_complete:exception")
            tb = traceback.format_exc()
            _append_crash_log(f"[UI CALLBACK EXCEPTION]\n{tb}")
            _logger.exception("Unhandled exception in _on_analysis_complete")
            extra = f"\n\nSee log: {_CRASH_LOG_PATH}" if _FILE_LOG_ENABLED else ""
            QMessageBox.critical(self, "Unhandled Error", f"An unexpected UI error occurred.{extra}")

    def _auto_generate_visuals(self):
        """Generate map and plots automatically after analysis.

        Failures are reported and logged per visual type, without aborting the
        analysis result display.
        """
        _mark_stage("ui:auto_visuals:start")

        # Map
        try:
            self.results_notebook.setCurrentWidget(self.map_frame)
            self._generate_map()
            _mark_stage("ui:auto_visuals:map_ok")
        except Exception:
            tb = traceback.format_exc()
            _append_crash_log(f"[AUTO MAP EXCEPTION]\n{tb}")
            _logger.exception("Automatic map generation failed")
            QMessageBox.warning(self, "Map Generation Failed", "Analysis completed, but map generation failed.")
            _mark_stage("ui:auto_visuals:map_fail")

        # Plots
        try:
            self.results_notebook.setCurrentWidget(self.plot_frame)
            self._generate_plots()
            _mark_stage("ui:auto_visuals:plots_ok")
        except Exception:
            tb = traceback.format_exc()
            _append_crash_log(f"[AUTO PLOTS EXCEPTION]\n{tb}")
            _logger.exception("Automatic plot generation failed")
            QMessageBox.warning(self, "Plot Generation Failed", "Analysis completed, but plot generation failed.")
            _mark_stage("ui:auto_visuals:plots_fail")

    def _create_segment_mapping(self):
        if not self.analysis_results or self.ride_data is None:
            return
        self.segment_data_map = {}
        for segment in self.analysis_results['segments']:
            seg_id = segment['segment_id']
            start = segment['start_time']
            end = segment['end_time']
            mask = (self.ride_data['timestamp'] >= start) & (self.ride_data['timestamp'] <= end)
            indices = self.ride_data[mask].index.tolist()
            self.segment_data_map[seg_id] = indices

    def _display_analysis_results(self):
        if not self.analysis_results:
            return
        r = self.analysis_results
        t = self.summary_text

        t.append("=" * 100)
        t.append("CDA ANALYSIS RESULTS")
        t.append("=" * 100)
        t.append("\nParameters used:")
        for k, v in r['parameters'].items():
            t.append(f"  {k}: {v}")
        t.append("")

        s = r['summary']

        ride = s.get('ride_info') if s else None
        if ride:
            t.append("Ride Information:")
            t.append(f"  Date: {ride.get('date', 'N/A')}")
            t.append(f"  Start time: {ride.get('start_time', 'N/A')}")
            t.append(f"  End time: {ride.get('end_time', 'N/A')}")
            t.append(
                f"  Total duration: {ride.get('duration_hms', 'N/A')} "
                f"({ride.get('duration_seconds', 'N/A')} s)"
            )
            if ride.get('total_distance_m') is not None:
                t.append(f"  Total distance: {ride['total_distance_m']:.0f} m")
            if ride.get('average_speed_kmh') is not None:
                t.append(f"  Average speed: {ride['average_speed_kmh']:.2f} km/h")
            if ride.get('elevation_gain_m') is not None:
                t.append(f"  Elevation Gain: {ride['elevation_gain_m']:.1f} m")
            t.append("")

        s = r['summary']

        if s:
            avg_temp = s.get('avg_temp')
            avg_press = s.get('avg_press')
            avg_wind  = s.get('avg_wind_speed')
            avg_dir   = s.get('avg_wind_direction')
            t.append("Weather Conditions:")
            t.append(f"  Average temperature: {avg_temp:.1f} °C" if avg_temp is not None and not (isinstance(avg_temp, float) and avg_temp != avg_temp) else "  Average temperature: N/A")
            t.append(f"  Average pressure: {avg_press:.2f} hPa" if avg_press is not None and not (isinstance(avg_press, float) and avg_press != avg_press) else "  Average pressure: N/A")
            t.append(f"  Average wind speed: {avg_wind:.1f} m/s" if avg_wind is not None else "  Average wind speed: N/A")
            t.append(f"  Average wind direction: {avg_dir:.2f} °" if avg_dir is not None and not (isinstance(avg_dir, float) and avg_dir != avg_dir) else "  Average wind direction: N/A")

        t.append(f"Segment Analysis ({len(r['segments'])} steady segments found):")
        t.append("-" * 250)
        use_api = self.parameters.get('use_open_elevation_api', False)
        if use_api:
            t.append(f"{'ID':<3}\t{'Dur':>6}\t{'Dist':>8}\t{'Elev FIT':>8}\t{'Elev API':>8}\t{'v_g':>6}\t{'v_w':>7}\t{'v_a':>6}\t{'w_angle':>6}\t{'Yaw':>5}\t{'Slope':>6}\t{'Power':>6}\t{'CdA':>7}")
            t.append(f"{'':3}\t{'(s)':>6}\t{'(m)':>8}\t{'(m)':>8}\t{'(m)':>8}\t{'m/s':>6}\t{'m/s':>7}\t{'m/s':>6}\t{'(deg)':>6}\t{'(deg)':>5}\t{'(deg)':>6}\t{'(W)':>6}\t{'':>7}")
        else:
            t.append(f"{'ID':<3}\t{'Dur':>6}\t{'Dist':>8}\t{'Elev':>6}\t{'v_g':>6}\t{'v_w':>7}\t{'v_a':>6}\t{'w_angle':>6}\t{'Yaw':>5}\t{'Slope':>6}\t{'Power':>6}\t{'CdA':>7}")
            t.append(f"{'':3}\t{'(s)':>6}\t{'(m)':>8}\t{'(m)':>6}\t{'m/s':>6}\t{'m/s':>7}\t{'m/s':>6}\t{'(deg)':>6}\t{'(deg)':>5}\t{'(deg)':>6}\t{'(W)':>6}\t{'':>7}")
        t.append("-" * 250)
        s = r['summary'] if r.get('summary') else {}
        for s in r['segments']:
            # Use precomputed yaw from analyzer
            yaw = s.get('yaw', 0.0)
            
            if use_api:
                fit_str = f"{s['start_elevation_fit']:>8.0f}" if s.get('start_elevation_fit') is not None else f"{'N/A':>8}"
                api_str = f"{s['start_elevation_api']:>8.0f}" if s.get('start_elevation_api') is not None else f"{'N/A':>8}"
                t.append(
                    f"{s['segment_id']:<3}\t{s['duration']:>6.0f}\t{s['distance']:>8.0f}\t"
                    f"{fit_str}\t{api_str}\t{s.get('v_ground', s['speed']):>6.2f}\t{s.get('v_wind', s['effective_wind']):>+7.2f}\t{s.get('v_air', s['air_speed']):>6.2f}\t"
                    f"{s['wind_angle']:>6.0f}\t{yaw:>5.1f}\t{s['slope']:>6.1f}\t{s['power']:>6.0f}\t{s['cda']:>7.4f}"
                )
            else:
                elev_str = f"{s['start_elevation']:>6.0f}" if s.get('start_elevation') is not None else f"{'N/A':>6}"
                t.append(
                    f"{s['segment_id']:<3}\t{s['duration']:>6.0f}\t{s['distance']:>8.0f}\t"
                    f"{elev_str}\t{s.get('v_ground', s['speed']):>6.2f}\t{s.get('v_wind', s['effective_wind']):>+7.2f}\t{s.get('v_air', s['air_speed']):>6.2f}\t"
                    f"{s['wind_angle']:>6.0f}\t{yaw:>5.1f}\t{s['slope']:>6.1f}\t{s['power']:>6.0f}\t{s['cda']:>7.4f}"
                )
        t.append("\nSummary:")
        t.append("-" * 100)

        s = r['summary']

        if s:
            t.append(f"Total segments analyzed: {s['total_segments']}")
            t.append(f"GPS coords: {'Yes' if s.get('has_gps_coordinates', False) else 'No'}  |  Elev source: {s.get('elevation_source', 'Unknown')}")
            keep_percent = s.get('keep_percent', self.analyzer.parameters.get('cda_keep_percent', 80.0))
            kept_used = s.get('kept_segments_used', s['total_segments'])
            t.append(f"Weighted CdA (all segments): {s.get('weighted_cda_all', s['weighted_cda']):.4f}")
            t.append(f"Weighted CdA ({keep_percent:.0f}%): {s.get('weighted_cda_kept', s['weighted_cda']):.4f} [{kept_used} segments]")
            t.append(f"Average CdA: {s['average_cda']:.4f}")
            t.append(f"CdA standard deviation: {s['cda_std']:.4f}")
            if s.get('wind_coefficients'):
                a, b, c = s['wind_coefficients']
                t.append(f"Wind Angle Formula: CdA = {a:.2e}*θ² + {b:.2e}*θ + {c:.2e}")
            t.append(f"Average wind speed (meteo): {s['avg_wind_speed']:.1f} m/s")
            t.append(f"Average ground speed  v_g: {s.get('avg_ground_speed', 0):.2f} m/s")
            t.append(f"Average wind component v_w: {s.get('avg_wind_component', 0):+.2f} m/s  (+headwind / -tailwind)")
            t.append(f"Average air speed      v_a: {s['avg_air_speed']:.2f} m/s")
            t.append(f"Total analysis duration: {s['total_duration']:.0f} seconds")
            t.append(f"Total distance analyzed: {s['total_distance']:.0f} meters")
        else:
            t.append("No steady segments found.")

        

    def _generate_segment_colors(self, n_segments):
        base_colors = []
        for cmap_name in ['tab20', 'tab20b', 'tab20c']:
            cmap = plt.colormaps[cmap_name]
            base_colors.extend([cmap(i) for i in range(20)])
        if n_segments <= 60:
            return base_colors[:n_segments]
        colors = []
        rotation_step = 7
        for i in range(n_segments):
            offset = (i // 60) * rotation_step
            idx = (i + offset) % 60
            colors.append(base_colors[idx])
        return colors

    def _generate_map(self):
        if not self.analysis_results or self.ride_data is None:
            QMessageBox.critical(self, "Error", "Please run analysis first")
            return
        try:
            if 'latitude' not in self.ride_data.columns or 'longitude' not in self.ride_data.columns:
                QMessageBox.critical(self, "Error", "No GPS data available in FIT file")
                return
            valid_coords = self.ride_data.dropna(subset=['latitude', 'longitude'])
            if len(valid_coords) == 0:
                QMessageBox.critical(self, "Error", "No valid GPS coordinates found")
                return

            mid_idx = len(valid_coords) // 2
            center_lat = valid_coords.iloc[mid_idx]['latitude']
            center_lon = valid_coords.iloc[mid_idx]['longitude']

            m = folium.Map(location=[center_lat, center_lon], zoom_start=13)
            full_path = list(zip(valid_coords['latitude'], valid_coords['longitude']))
            if len(full_path) > 1:
                folium.PolyLine(full_path, color='gray', weight=2, opacity=0.5, tooltip="Full Ride").add_to(m)

            segments = self.analysis_results['segments']
            if not segments:
                QMessageBox.warning(self, "No Segments", "No steady segments to display.")
                return

            colors = self._generate_segment_colors(len(segments))
            colors_hex = [f"#{int(c[0]*255):02x}{int(c[1]*255):02x}{int(c[2]*255):02x}" for c in colors]

            for i, segment in enumerate(segments):
                seg_id = segment['segment_id']
                if seg_id not in self.segment_data_map:
                    continue
                idx = self.segment_data_map[seg_id]
                if not idx:
                    continue
                data = self.ride_data.iloc[idx].dropna(subset=['latitude', 'longitude'])
                coords = list(zip(data['latitude'], data['longitude']))
                if len(coords) < 2:
                    continue
                color = colors_hex[i]
                popup = (
                    f"<b>Segment {seg_id}</b><br>"
                    f"CdA: {segment['cda']:.4f}<br>"
                    f"Speed: {segment['speed']:.2f} m/s<br>"
                    f"Power: {segment['power']:.0f} W<br>"
                    f"Slope: {segment['slope']:.2f}°"
                )
                folium.PolyLine(coords, color=color, weight=5, opacity=0.9, tooltip=f"Segment {seg_id}",
                                popup=folium.Popup(popup, max_width=250)).add_to(m)
                folium.Marker(
                    location=coords[0],
                    icon=folium.DivIcon(html=f"""
                    <div style="background-color:{color}; border:2px solid white; border-radius:50%; width:24px; height:24px;
                                display:flex; align-items:center; justify-content:center; color:white; font-weight:bold; font-size:12px;">
                    {seg_id}</div>""")
                ).add_to(m)

            # Loading big folium output via setHtml can fail silently in WebEngine.
            # Save to a temp html file and load by URL for robust rendering.
            self._map_html_path = os.path.join(tempfile.gettempdir(), "cda_analyzer_map.html")
            m.save(self._map_html_path)
            self.map_webview.setUrl(QUrl.fromLocalFile(self._map_html_path))

        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error generating map: {str(e)}")

    def _generate_plots(self):
        if not self.analysis_results or self.ride_data is None:
            QMessageBox.critical(self, "Error", "Please run analysis first")
            return
        try:
            segments = self.analysis_results['segments']
            if not segments:
                QMessageBox.warning(self, "No Data", "No steady segments found for plotting.")
                return

            colors = self._generate_segment_colors(len(segments))
            colors_hex = [f"#{int(c[0]*255):02x}{int(c[1]*255):02x}{int(c[2]*255):02x}" for c in colors]

            if self.current_figure is None:
                self.current_figure = Figure(figsize=(16, 10))
            else:
                self.current_figure.clear()
            gs = self.current_figure.add_gridspec(3, 2, hspace=0.45, wspace=0.3)

            cda_vals = [s['cda'] for s in segments]
            air_speeds = [s.get('air_speed', 0) for s in segments]
            seg_ids = [s['segment_id'] for s in segments]
            speeds = [s['speed'] for s in segments]
            powers = [s['power'] for s in segments]
            yaw_vals = [s.get('yaw', 0.0) for s in segments]
            wind_angles = [s.get('wind_angle', 0) for s in segments]

            # --- 1. Speed + Power vs Distance ---
            ax1 = self.current_figure.add_subplot(gs[0, 0])
            ax1.plot(self.ride_data['distance']/1000, self.ride_data['speed'], 'lightgray', alpha=0.5, lw=1, label='Full ride (speed)')
            for i, s in enumerate(segments):
                idx = self.segment_data_map.get(s['segment_id'], [])
                if not idx: continue
                d = self.ride_data.iloc[idx]
                ax1.plot(d['distance']/1000, d['speed'], color=colors[i], lw=2, alpha=0.9, label=f"Seg {s['segment_id']}")
            ax1.set_title('Speed + Power vs Distance', fontsize=10, fontweight='bold')
            ax1.set_xlabel('Distance (km)', fontsize=8)
            ax1.set_ylabel('Speed (m/s)', fontsize=8, color='blue')
            ax1.tick_params(axis='y', labelcolor='blue', labelsize=8)
            ax1.tick_params(axis='x', labelsize=6)
            ax1.grid(True, alpha=0.3)
            if len(segments) <= 10:
                ax1.legend(fontsize=6, loc='upper left')
            ax1_r = ax1.twinx()
            ax1_r.plot(self.ride_data['distance']/1000, self.ride_data['power'], color='orange', alpha=0.5, lw=1)
            for i, s in enumerate(segments):
                idx = self.segment_data_map.get(s['segment_id'], [])
                if not idx: continue
                d = self.ride_data.iloc[idx]
                ax1_r.plot(d['distance']/1000, d['power'], color=colors[i], lw=2.5, alpha=0.8, linestyle='--')
            ax1_r.set_ylabel('Power (W)', fontsize=8, color='red')
            ax1_r.tick_params(axis='y', labelcolor='red', labelsize=8)

            # --- 2. CdA by Segment ---
            ax2 = self.current_figure.add_subplot(gs[0, 1])
            bars = ax2.bar(seg_ids, cda_vals, color=colors, alpha=0.8, edgecolor='k', linewidth=0.7)
            ax2.set_title('CdA by Segment', fontsize=10, fontweight='bold')
            ax2.set_xlabel('Segment ID', fontsize=8)
            ax2.set_ylabel('CdA', fontsize=8)
            ax2.tick_params(axis='x', labelsize=9)
            ax2.grid(True, axis='y', alpha=0.3)
            for bar, cda in zip(bars, cda_vals):
                ax2.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.001,
                         f'{cda:.3f}', ha='center', fontsize=5)

            # --- 3. CdA vs Air Speed ---
            ax3 = self.current_figure.add_subplot(gs[1, 0])
            ax3.scatter(air_speeds, cda_vals, c=colors, s=100, alpha=0.8, edgecolors='k', linewidth=0.5)
            for i, sid in enumerate(seg_ids):
                ax3.annotate(str(sid), (air_speeds[i], cda_vals[i]), xytext=(5, 5), textcoords='offset points', fontsize=6, alpha=0.8)
            ax3.set_title('CdA vs Air Speed', fontsize=10, fontweight='bold')
            ax3.set_xlabel('Air Speed (m/s)', fontsize=8)
            ax3.set_ylabel('CdA', fontsize=8)
            ax3.grid(True, alpha=0.3)

            # --- 4. Speed vs Power ---
            ax4 = self.current_figure.add_subplot(gs[1, 1])
            ax4.scatter(speeds, powers, c=colors, s=100, alpha=0.8, edgecolors='k', linewidth=0.5)
            for i, sid in enumerate(seg_ids):
                ax4.annotate(str(sid), (speeds[i], powers[i]), xytext=(5, 5), textcoords='offset points', fontsize=6, alpha=0.8)
            ax4.set_title('Speed vs Power', fontsize=10, fontweight='bold')
            ax4.set_xlabel('Speed (m/s)', fontsize=8)
            ax4.set_ylabel('Power (W)', fontsize=8)
            ax4.grid(True, alpha=0.3)

            # --- 5. CdA vs Yaw ---
            ax5 = self.current_figure.add_subplot(gs[2, 0])
            sc5 = ax5.scatter(yaw_vals, cda_vals, c=air_speeds, cmap='viridis', s=100, alpha=0.8, edgecolors='k', linewidth=0.5)
            mask_20 = [abs(y) <= 20 for y in yaw_vals]
            yv20 = [yaw_vals[i] for i in range(len(yaw_vals)) if mask_20[i]]
            cv20 = [cda_vals[i] for i in range(len(cda_vals)) if mask_20[i]]
            if len(set([round(y, 1) for y in yv20])) >= 3:
                co = np.polyfit(yv20, cv20, 2)
                ax5.plot(np.linspace(-20, 20, 200), np.poly1d(co)(np.linspace(-20, 20, 200)), color='red', lw=1.5)
                ax5.text(0.95, 0.05, f"y={co[0]:.3e}x\u00b2+{co[1]:.3e}x+{co[2]:.3e}", transform=ax5.transAxes,
                         fontsize=7, color='red', ha='right', va='bottom', bbox=dict(facecolor='white', alpha=0.6))
            for i, sid in enumerate(seg_ids):
                ax5.annotate(str(sid), (yaw_vals[i], cda_vals[i]), xytext=(5, 5), textcoords='offset points', fontsize=6, alpha=0.8)
            ax5.set_title('CdA vs Yaw Angle', fontsize=10, fontweight='bold')
            ax5.set_xlabel('Yaw (\u00b0) \u2014 Crosswind from rider perspective', fontsize=8)
            ax5.set_ylabel('CdA', fontsize=8)
            ax5.set_xlim(-20, 20)
            ax5.set_xticks([-20, -10, 0, 10, 20])
            ax5.grid(True, alpha=0.3)
            self.current_figure.colorbar(sc5, ax=ax5).set_label('Air Speed (m/s)', fontsize=8)

            # --- 6. CdA vs Wind Angle ---
            ax6 = self.current_figure.add_subplot(gs[2, 1])
            sc6 = ax6.scatter(wind_angles, cda_vals, c=air_speeds, cmap='viridis', s=100, alpha=0.8, edgecolors='k', linewidth=0.5)
            if len(set([round(w, 1) for w in wind_angles])) >= 3:
                co6 = np.polyfit(wind_angles, cda_vals, 2)
                x6 = np.linspace(-180, 180, 300)
                ax6.plot(x6, np.poly1d(co6)(x6), color='red', lw=1.5)
                ax6.text(0.98, 0.05, f"y={co6[0]:.3e}x\u00b2+{co6[1]:.3e}x+{co6[2]:.3e}", transform=ax6.transAxes,
                         fontsize=7, color='red', ha='right', va='bottom', bbox=dict(facecolor='white', alpha=0.6))
            for i, sid in enumerate(seg_ids):
                ax6.annotate(str(sid), (wind_angles[i], cda_vals[i]), xytext=(5, 5), textcoords='offset points', fontsize=6, alpha=0.8)
            ax6.set_title('CdA vs Wind Angle', fontsize=10, fontweight='bold')
            ax6.set_xlabel('Wind Angle (\u00b0) — Headwind [\u00b1180\u00b0], Tailwind [0\u00b0]', fontsize=8)
            ax6.set_ylabel('CdA', fontsize=8)
            ax6.set_xlim(-180, 180)
            ax6.set_xticks([-180, -135, -90, -45, 0, 45, 90, 135, 180])
            ax6.grid(True, alpha=0.3)
            self.current_figure.colorbar(sc6, ax=ax6).set_label('Air Speed (m/s)', fontsize=8)

            # Summary text
            weighted_metrics = self.analyzer._calculate_weighted_cda_metrics(segments)
            weighted_kept = weighted_metrics['weighted_cda_kept']
            keep_percent = weighted_metrics['keep_percent']
            std_cda = np.std(cda_vals)
            total_distance = sum(s['distance'] for s in segments) / 1000
            summary = (
                f"Weighted CdA {keep_percent:.0f}%: {weighted_kept:.3f}\n"
                f"CdA Std Dev: {std_cda:.3f}\n"
                f"Total Distance: {total_distance:.1f} km"
            )
            self.current_figure.text(0.45, 0.015, summary, ha='center', va='bottom', fontsize=9, fontweight='bold',
                                     bbox=dict(facecolor='white', edgecolor='black', boxstyle='round,pad=0.5'))

            self.current_figure.suptitle("CDA Analysis Plots", fontsize=12, fontweight='bold', y=0.99)
            self.current_figure.subplots_adjust(top=0.96, bottom=0.08, left=0.05, right=0.98)

            # Create canvas once; then reuse it for future draws.
            if self.current_canvas is None:
                self.current_canvas = FigureCanvas(self.current_figure)
                if self.plot_label and self.plot_label.parent() is not None:
                    self.plot_label.setParent(None)
                if self.plot_button and self.plot_button.parent() is not None:
                    self.plot_button.setParent(None)
                layout = self.plot_frame.layout()
                layout.addWidget(self.current_canvas)

            self.current_canvas.draw()

        except Exception as e:
            self._cleanup_results()
            QMessageBox.critical(self, "Error", f"Error generating plots: {str(e)}")

    def _export_results(self):
        if not self.analysis_results:
            QMessageBox.critical(self, "Error", "No results to export")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Results", "", "JSON files (*.json);;CSV files (*.csv);;All files (*)"
        )
        if not path:
            return
        try:
            if path.endswith('.json'):
                export_data = json.loads(json.dumps(self.analysis_results, default=str))
                with open(path, 'w') as f:
                    json.dump(export_data, f, indent=2)
            elif path.endswith('.csv'):
                df = pd.DataFrame(self.analysis_results['segments'])
                df.to_csv(path, index=False)
            QMessageBox.information(self, "Success", "Results exported successfully.")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Export failed: {str(e)}")

    def _on_simulation_params_changed(self):
        """Update simulation parameter display values"""
        wind_speed = (self.sim_wind_speed_slider.value() / 10.0) + 0.000001
        wind_angle = self.sim_wind_angle_slider.value()
        wind_factor = self.sim_wind_factor_slider.value() / 100.0
        
        self.sim_wind_speed_value.setText(f"{wind_speed:.1f}")
        self.sim_wind_angle_value.setText(f"{wind_angle}")
        self.sim_wind_factor_value.setText(f"{wind_factor:.2f}")

    def _run_simulation(self):
        """Run weather simulation with manual wind parameters"""
        if not self.analysis_results or self.ride_data is None:
            QMessageBox.critical(self, "Error", "Please run analysis first")
            return
        
        try:
            wind_speed = (self.sim_wind_speed_slider.value() / 10.0) + 0.000001
            wind_angle = self.sim_wind_angle_slider.value()
            wind_factor = self.sim_wind_factor_slider.value() / 100.0
            temperature = float(self.sim_temp_entry.text())
            pressure = float(self.sim_pressure_entry.text())

            
            self.sim_summary_text.clear()
            self.sim_summary_text.append(f"Running simulation with:\n- Wind Speed: {wind_speed:.1f} m/s\n- Wind Angle: {wind_angle}°\n- Wind Effect Factor: {wind_factor:.2f}\n\nProcessing segments...")
            #self.simulation_notebook.setCurrentWidget(self.sim_summary_frame)
            
            # Calculate simulated results
            self.simulation_results = self._calculate_simulation_results(wind_speed, wind_angle, wind_factor, temperature, pressure)
            
            if not self.simulation_results:
                self.sim_summary_text.append("\nError: No valid segments found in simulation!")
                return
            
            # Display results
            self._display_simulation_results(wind_speed, wind_angle, wind_factor, temperature, pressure)
            
            # Generate plots
            self._generate_simulation_plots()
            
            self.sim_summary_text.append("\n\nSimulation complete!")
            
        except Exception as e:
            import traceback
            traceback.print_exc()
            QMessageBox.critical(self, "Error", f"Simulation failed: {str(e)}")

    def _calculate_simulation_results(self, wind_speed, wind_angle, wind_factor, temperature, pressure):
        """Calculate CdA results with simulated wind conditions"""
        if not self.analysis_results or not self.preprocessed_segments:
            return None
        
        simulation_results = []
        
        # Use the preprocessed segments from the original analysis
        for i, segment_df in enumerate(self.preprocessed_segments):
            # Create weather data with simulated wind
            # Must include both temperature+pressure for air_density calculation, and wind parameters
            weather_data = {
                'wind_speed': wind_speed,
                'wind_direction': wind_angle,
                'air_density': 0.001,
                'temperature': temperature,
                'pressure': pressure
            }
            
            if len(segment_df) < 10:
                continue
            
            # Update analyzer with simulation wind factor
            orig_factor = self.analyzer.parameters['wind_effect_factor']
            try:
                self.analyzer.update_parameters({'wind_effect_factor': wind_factor})
                # Calculate CdA with simulated wind
                result = self.analyzer.calculate_cda_for_segment(segment_df, weather_data)
            finally:
                # Always restore the original factor, even if an exception is raised
                self.analyzer.update_parameters({'wind_effect_factor': orig_factor})
            
            if result:
                start_elev = float(segment_df['altitude'].iloc[0]) if 'altitude' in segment_df.columns and not segment_df['altitude'].isna().all() else None
                start_elev_fit = float(segment_df['altitude_fit'].iloc[0]) if 'altitude_fit' in segment_df.columns and not segment_df['altitude_fit'].isna().all() else None
                start_elev_api = float(segment_df['altitude_api'].iloc[0]) if 'altitude_api' in segment_df.columns and not segment_df['altitude_api'].isna().all() else None
                result.update({
                    'segment_id': i,
                    'start_time': segment_df['timestamp'].iloc[0],
                    'end_time': segment_df['timestamp'].iloc[-1],
                    'start_elevation': start_elev,
                    'start_elevation_fit': start_elev_fit,
                    'start_elevation_api': start_elev_api,
                })
                simulation_results.append(result)
        return simulation_results

    def _display_simulation_results(self, wind_speed, wind_angle, wind_factor, temperature, pressure):
        """Display simulation results in summary"""
        if not self.simulation_results:
            self.sim_summary_text.clear()
            self.sim_summary_text.append("No results to display")
            return

        self.sim_summary_text.clear()
        t = self.sim_summary_text

        # --- Header (now identical to analysis summary formatting) ---
        t.append("=" * 100)
        t.append("WEATHER SIMULATION RESULTS")
        t.append("=" * 100)
        t.append("")  # blank line for consistent spacing

        # --- Simulation Parameters ---
        t.append("Simulation Parameters:")
        t.append(f"  Wind Speed: {wind_speed:.1f} m/s")
        t.append(f"  Wind Angle: {wind_angle}°")
        t.append(f"  Wind Effect Factor: {wind_factor:.2f}")
        t.append(f"  Temperature: {temperature:.2f}")
        t.append(f"  Pressure: {pressure:.2f}\n")

        # --- Segment Table Header ---
        use_api = self.parameters.get('use_open_elevation_api', False)
        has_gps = 'latitude' in self.ride_data.columns if self.ride_data is not None else False
        t.append(f"Segment Results ({len(self.simulation_results)} segments):")
        t.append("-" * 250)
        if use_api:
            t.append(f"{'ID':<3}\t{'Dur':>6}\t{'Dist':>8}\t{'Elev FIT':>8}\t{'Elev API':>8}\t{'v_g':>6}\t{'v_w':>7}\t{'v_a':>6}\t"
                    f"{'w_angle':>6}\t{'Yaw':>5}\t{'Slope':>6}\t{'Power':>6}\t{'CdA':>7}")
            t.append(f"{'':3}\t{'(s)':>6}\t{'(m)':>8}\t{'(m)':>8}\t{'(m)':>8}\t{'m/s':>6}\t{'m/s':>7}\t{'m/s':>6}\t"
                    f"{'(deg)':>6}\t{'(deg)':>5}\t{'(deg)':>6}\t{'(W)':>6}\t{'':>7}")
        else:
            t.append(f"{'ID':<3}\t{'Dur':>6}\t{'Dist':>8}\t{'Elev':>6}\t{'v_g':>6}\t{'v_w':>7}\t{'v_a':>6}\t"
                    f"{'w_angle':>6}\t{'Yaw':>5}\t{'Slope':>6}\t{'Power':>6}\t{'CdA':>7}")
            t.append(f"{'':3}\t{'(s)':>6}\t{'(m)':>8}\t{'(m)':>6}\t{'m/s':>6}\t{'m/s':>7}\t{'m/s':>6}\t"
                    f"{'(deg)':>6}\t{'(deg)':>5}\t{'(deg)':>6}\t{'(W)':>6}\t{'':>7}")
        t.append("-" * 200)

        # --- Segment Rows ---
        for s in self.simulation_results:
            # Use precomputed yaw from analyzer
            yaw = s.get('yaw', 0.0)
            
            if use_api:
                fit_str = f"{s['start_elevation_fit']:>8.0f}" if s.get('start_elevation_fit') is not None else f"{'N/A':>8}"
                api_str = f"{s['start_elevation_api']:>8.0f}" if s.get('start_elevation_api') is not None else f"{'N/A':>8}"
                t.append(
                    f"{s['segment_id']:<3}\t{s['duration']:>6.0f}\t{s['distance']:>8.0f}\t"
                    f"{fit_str}\t{api_str}\t{s.get('v_ground', s['speed']):>6.2f}\t{s.get('v_wind', s['effective_wind']):>+7.2f}\t{s.get('v_air', s['air_speed']):>6.2f}\t"
                    f"{s['wind_angle']:>6.0f}\t{yaw:>5.1f}\t{s['slope']:>6.1f}\t{s['power']:>6.0f}\t{s['cda']:>7.4f}"
                )
            else:
                elev_str = f"{s['start_elevation']:>6.0f}" if s.get('start_elevation') is not None else f"{'N/A':>6}"
                t.append(
                    f"{s['segment_id']:<3}\t{s['duration']:>6.0f}\t{s['distance']:>8.0f}\t"
                    f"{elev_str}\t{s.get('v_ground', s['speed']):>6.2f}\t{s.get('v_wind', s['effective_wind']):>+7.2f}\t{s.get('v_air', s['air_speed']):>6.2f}\t"
                    f"{s['wind_angle']:>6.0f}\t{yaw:>5.1f}\t{s['slope']:>6.1f}\t{s['power']:>6.0f}\t{s['cda']:>7.4f}"
                )

        # --- Summary Section ---
        t.append("\nSummary:")
        t.append("-" * 100)

        cda_values = [s['cda'] for s in self.simulation_results]

        if cda_values:
            weighted_metrics = self.analyzer._calculate_weighted_cda_metrics(self.simulation_results)
            weighted_all = weighted_metrics['weighted_cda_all']
            weighted_kept = weighted_metrics['weighted_cda_kept']
            keep_percent = weighted_metrics['keep_percent']
            kept_used = weighted_metrics['kept_segments_used']
            t.append(f"GPS coords: {'Yes' if hasattr(self.ride_data, 'latitude') and 'latitude' in self.ride_data.columns else 'No'}  |  Elev source: {self.analyzer.elevation_source if self.analyzer.elevation_source else 'Unknown'}")
            t.append(f"Weighted CdA (all segments): {weighted_all:.4f}")
            t.append(f"Weighted CdA ({keep_percent:.0f}%): {weighted_kept:.4f} [{kept_used} segments]")
            t.append(f"Average CdA: {np.mean(cda_values):.4f}")
            t.append(f"CdA standard deviation: {np.std(cda_values):.4f}")
            t.append(f"Min CdA: {np.min(cda_values):.4f}")
            t.append(f"Max CdA: {np.max(cda_values):.4f}")


    def _fetch_missing_elevation_data(self):
        """Fetch missing elevation data from Open-Elevation API after file load"""
        from elevation import ElevationService
        import numpy as np
        
        _logger.info("Fetching missing elevation API data for segments...")
        
        try:
            # Collect all unique GPS coordinates from all segments
            all_coords = []
            for segment_df in self.preprocessed_segments:
                if 'latitude' in segment_df.columns and 'longitude' in segment_df.columns:
                    coords = segment_df[['latitude', 'longitude']].dropna()
                    all_coords.extend(zip(coords['latitude'], coords['longitude']))
            
            if not all_coords:
                _logger.warning("No GPS coordinates available for elevation lookup")
                return
            
            # Deduplicate and fetch elevations
            unique_coords = list(dict.fromkeys(all_coords))
            elevation_service = ElevationService()
            elevation_map = elevation_service.get_elevations_batch(unique_coords)
            
            if not elevation_map:
                _logger.warning("Failed to fetch elevation data from API")
                return
            
            # Add altitude_api column to each segment
            for segment_df in self.preprocessed_segments:
                if 'latitude' in segment_df.columns and 'longitude' in segment_df.columns:
                    def get_elevation(row):
                        key = (row['latitude'], row['longitude'])
                        fit_alt = row.get('altitude_fit', row.get('altitude', np.nan))
                        return elevation_map.get(key, fit_alt)
                    
                    segment_df['altitude_api'] = segment_df.apply(get_elevation, axis=1)
            
            self.analyzer.elevation_source = 'Open-Elevation API (fetched during simulation)'
            _logger.info(f"Successfully fetched {len(elevation_map)} elevations from Open-Elevation API")
        except Exception as e:
            _logger.warning(f"Error fetching elevation data: {e}")
    
    def _generate_simulation_plots(self):
        """Generate plots for simulation results"""
        if not self.simulation_results or self.ride_data is None:
            _logger.warning("No simulation results or ride data to plot.")
            return
        
        try:
            segments = self.simulation_results
            if not segments:
                return

            colors = self._generate_segment_colors(len(segments))
            colors_hex = [f"#{int(c[0]*255):02x}{int(c[1]*255):02x}{int(c[2]*255):02x}" for c in colors]

            if self.sim_figure is None:
                self.sim_figure = Figure(figsize=(16, 10))
            else:
                self.sim_figure.clear()
            gs = self.sim_figure.add_gridspec(3, 2, hspace=0.45, wspace=0.3)

            cda_vals = [s['cda'] for s in segments]
            air_speeds = [s.get('air_speed', 0) for s in segments]
            seg_ids = [s['segment_id'] for s in segments]
            speeds = [s['speed'] for s in segments]
            powers = [s['power'] for s in segments]
            yaw_vals = [s.get('yaw', 0.0) for s in segments]
            wind_angles = [s.get('wind_angle', 0) for s in segments]

            # --- 1. Speed + Power vs Distance ---
            ax1 = self.sim_figure.add_subplot(gs[0, 0])
            ax1.plot(self.ride_data['distance']/1000, self.ride_data['speed'], 'lightgray', alpha=0.5, lw=1, label='Full ride (speed)')
            for i, s in enumerate(segments):
                idx = self.segment_data_map.get(s['segment_id'], [])
                if not idx: continue
                d = self.ride_data.iloc[idx]
                ax1.plot(d['distance']/1000, d['speed'], color=colors[i], lw=2, alpha=0.9, label=f"Seg {s['segment_id']}")
            ax1.set_title('Speed + Power vs Distance', fontsize=10, fontweight='bold')
            ax1.set_xlabel('Distance (km)', fontsize=8)
            ax1.set_ylabel('Speed (m/s)', fontsize=8, color='blue')
            ax1.tick_params(axis='y', labelcolor='blue', labelsize=8)
            ax1.tick_params(axis='x', labelsize=6)
            ax1.grid(True, alpha=0.3)
            if len(segments) <= 10:
                ax1.legend(fontsize=6, loc='upper left')
            ax1_r = ax1.twinx()
            ax1_r.plot(self.ride_data['distance']/1000, self.ride_data['power'], color='orange', alpha=0.5, lw=1)
            for i, s in enumerate(segments):
                idx = self.segment_data_map.get(s['segment_id'], [])
                if not idx: continue
                d = self.ride_data.iloc[idx]
                ax1_r.plot(d['distance']/1000, d['power'], color=colors[i], lw=2.5, alpha=0.8, linestyle='--')
            ax1_r.set_ylabel('Power (W)', fontsize=8, color='red')
            ax1_r.tick_params(axis='y', labelcolor='red', labelsize=8)

            # --- 2. CdA by Segment ---
            ax2 = self.sim_figure.add_subplot(gs[0, 1])
            bars = ax2.bar(seg_ids, cda_vals, color=colors, alpha=0.8, edgecolor='k', linewidth=0.7)
            ax2.set_title('CdA by Segment', fontsize=10, fontweight='bold')
            ax2.set_xlabel('Segment ID', fontsize=8)
            ax2.set_ylabel('CdA', fontsize=8)
            ax2.tick_params(axis='x', labelsize=9)
            ax2.grid(True, axis='y', alpha=0.3)
            for bar, cda in zip(bars, cda_vals):
                ax2.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.001,
                         f'{cda:.3f}', ha='center', fontsize=5)

            # --- 3. CdA vs Air Speed ---
            ax3 = self.sim_figure.add_subplot(gs[1, 0])
            ax3.scatter(air_speeds, cda_vals, c=colors, s=100, alpha=0.8, edgecolors='k', linewidth=0.5)
            for i, sid in enumerate(seg_ids):
                ax3.annotate(str(sid), (air_speeds[i], cda_vals[i]), xytext=(5, 5), textcoords='offset points', fontsize=6, alpha=0.8)
            ax3.set_title('CdA vs Air Speed', fontsize=10, fontweight='bold')
            ax3.set_xlabel('Air Speed (m/s)', fontsize=8)
            ax3.set_ylabel('CdA', fontsize=8)
            ax3.grid(True, alpha=0.3)

            # --- 4. Speed vs Power ---
            ax4 = self.sim_figure.add_subplot(gs[1, 1])
            ax4.scatter(speeds, powers, c=colors, s=100, alpha=0.8, edgecolors='k', linewidth=0.5)
            for i, sid in enumerate(seg_ids):
                ax4.annotate(str(sid), (speeds[i], powers[i]), xytext=(5, 5), textcoords='offset points', fontsize=6, alpha=0.8)
            ax4.set_title('Speed vs Power', fontsize=10, fontweight='bold')
            ax4.set_xlabel('Speed (m/s)', fontsize=8)
            ax4.set_ylabel('Power (W)', fontsize=8)
            ax4.grid(True, alpha=0.3)

            # --- 5. CdA vs Yaw ---
            ax5 = self.sim_figure.add_subplot(gs[2, 0])
            sc5 = ax5.scatter(yaw_vals, cda_vals, c=air_speeds, cmap='viridis', s=100, alpha=0.8, edgecolors='k', linewidth=0.5)
            mask_20 = [abs(y) <= 20 for y in yaw_vals]
            yv20 = [yaw_vals[i] for i in range(len(yaw_vals)) if mask_20[i]]
            cv20 = [cda_vals[i] for i in range(len(cda_vals)) if mask_20[i]]
            if len(set([round(y, 1) for y in yv20])) >= 3:
                co = np.polyfit(yv20, cv20, 2)
                ax5.plot(np.linspace(-20, 20, 200), np.poly1d(co)(np.linspace(-20, 20, 200)), color='red', lw=1.5)
                ax5.text(0.95, 0.05, f"y={co[0]:.3e}x\u00b2+{co[1]:.3e}x+{co[2]:.3e}", transform=ax5.transAxes,
                         fontsize=7, color='red', ha='right', va='bottom', bbox=dict(facecolor='white', alpha=0.6))
            for i, sid in enumerate(seg_ids):
                ax5.annotate(str(sid), (yaw_vals[i], cda_vals[i]), xytext=(5, 5), textcoords='offset points', fontsize=6, alpha=0.8)
            ax5.set_title('CdA vs Yaw Angle', fontsize=10, fontweight='bold')
            ax5.set_xlabel('Yaw (\u00b0) \u2014 Crosswind from rider perspective', fontsize=8)
            ax5.set_ylabel('CdA', fontsize=8)
            ax5.set_xlim(-20, 20)
            ax5.set_xticks([-20, -10, 0, 10, 20])
            ax5.grid(True, alpha=0.3)
            self.sim_figure.colorbar(sc5, ax=ax5).set_label('Air Speed (m/s)', fontsize=8)

            # --- 6. CdA vs Wind Angle ---
            ax6 = self.sim_figure.add_subplot(gs[2, 1])
            sc6 = ax6.scatter(wind_angles, cda_vals, c=air_speeds, cmap='viridis', s=100, alpha=0.8, edgecolors='k', linewidth=0.5)
            if len(set([round(w, 1) for w in wind_angles])) >= 3:
                co6 = np.polyfit(wind_angles, cda_vals, 2)
                x6 = np.linspace(-180, 180, 300)
                ax6.plot(x6, np.poly1d(co6)(x6), color='red', lw=1.5)
                ax6.text(0.98, 0.05, f"y={co6[0]:.3e}x\u00b2+{co6[1]:.3e}x+{co6[2]:.3e}", transform=ax6.transAxes,
                         fontsize=7, color='red', ha='right', va='bottom', bbox=dict(facecolor='white', alpha=0.6))
            for i, sid in enumerate(seg_ids):
                ax6.annotate(str(sid), (wind_angles[i], cda_vals[i]), xytext=(5, 5), textcoords='offset points', fontsize=6, alpha=0.8)
            ax6.set_title('CdA vs Wind Angle', fontsize=10, fontweight='bold')
            ax6.set_xlabel('Wind Angle (\u00b0) \u2014 Headwind [\u00b1180\u00b0], Tailwind [0\u00b0]', fontsize=8)
            ax6.set_ylabel('CdA', fontsize=8)
            ax6.set_xlim(-180, 180)
            ax6.set_xticks([-180, -135, -90, -45, 0, 45, 90, 135, 180])
            ax6.grid(True, alpha=0.3)
            self.sim_figure.colorbar(sc6, ax=ax6).set_label('Air Speed (m/s)', fontsize=8)

            # Summary text
            weighted_metrics = self.analyzer._calculate_weighted_cda_metrics(segments)
            weighted_kept = weighted_metrics['weighted_cda_kept']
            keep_percent = weighted_metrics['keep_percent']
            std_cda = np.std(cda_vals)
            total_distance = sum(s['distance'] for s in segments) / 1000
            summary = (
                f"Weighted CdA ({keep_percent:.0f}%): {weighted_kept:.3f}\n"
                f"CdA Std Dev: {std_cda:.3f}\n"
                f"Total Distance: {total_distance:.1f} km"
            )
            self.sim_figure.text(0.45, 0.015, summary, ha='center', va='bottom', fontsize=9, fontweight='bold',
                                     bbox=dict(facecolor='white', edgecolor='black', boxstyle='round,pad=0.5'))

            self.sim_figure.suptitle("Weather Simulation Plots", fontsize=12, fontweight='bold', y=0.99)
            self.sim_figure.subplots_adjust(top=0.96, bottom=0.08, left=0.05, right=0.98)

            # Create canvas once; then reuse it for future draws.
            if self.sim_canvas is None:
                self.sim_canvas = FigureCanvas(self.sim_figure)
                if self.sim_plot_label and self.sim_plot_label.parent() is not None:
                    self.sim_plot_label.setParent(None)
                layout = self.sim_plot_frame.layout()
                layout.addWidget(self.sim_canvas)

            self.sim_canvas.draw()

        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error generating plots: {str(e)}")

    def closeEvent(self, event):
        self._cleanup_results()
        event.accept()

    def _on_wind_effect_slider_moved(self, value):
        """Update the wind effect value label as slider moves"""
        new_factor = value / 100.0
        self.wind_effect_value_label.setText(f"{new_factor:.2f}")
        
        # Also update the parameter entry in the parameters tab
        if 'wind_effect_factor' in self.param_entries:
            self.param_entries['wind_effect_factor'].setText(f"{new_factor:.2f}")

    def _on_wind_effect_changed(self):
        """Handle wind effect slider release - re-run analysis with new factor"""
        if not self.analysis_results or self.ride_data is None or not self.preprocessed_segments:
            return

        try:
            # Store the old ride_info before we overwrite analysis_results
            old_ride_info = self.analysis_results.get('summary', {}).get('ride_info')

            # Get the new wind effect factor from slider
            new_factor = self.wind_effect_slider.value() / 100.0

            # Update analyzer parameter
            self.analyzer.update_parameters({'wind_effect_factor': new_factor})

            # Re-analyze with progress checkpoints so the bar visibly updates.
            self.progress.setRange(0, 100)
            self.progress.setValue(5)
            self.analysis_status.setText("Re-analyzing with new wind effect factor...")
            QApplication.processEvents()

            # Use the same segments from original analysis
            segment_results = self.analyzer._analyze_segments(self.preprocessed_segments)
            self.progress.setValue(55)
            self.analysis_status.setText("Re-analysis: calculating summary...")
            QApplication.processEvents()

            summary = self.analyzer._calculate_summary(segment_results)

            # RE-INSERT the ride_info back into the new summary
            if old_ride_info:
                summary['ride_info'] = old_ride_info

            # Update results
            self.analysis_results = {
                'segments': segment_results,
                'summary': summary,
                'parameters': self.analyzer.parameters
            }

            self.progress.setValue(70)
            self.analysis_status.setText("Re-analysis: updating summary...")
            QApplication.processEvents()

            # Display updated results
            self.summary_text.clear()
            self._display_analysis_results()

            self.progress.setValue(82)
            self.analysis_status.setText("Re-analysis: refreshing map...")
            QApplication.processEvents()
            self._generate_map()

            self.progress.setValue(92)
            self.analysis_status.setText("Re-analysis: refreshing plots...")
            QApplication.processEvents()
            self._generate_plots()

            self.progress.setValue(100)
            self.analysis_status.setText("Re-analysis complete!")
        except Exception as e:
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            self.analysis_status.setText("Re-analysis failed")
            QMessageBox.critical(self, "Error", f"Wind effect re-analysis failed: {str(e)}")

    def _set_window_icon(self):
        """Set window icon from logo.PNG"""
        try:
            logo_path = resource_path("icons/logo.PNG")
            self.setWindowIcon(QIcon(str(logo_path)))
        except Exception as e:
            _logger.warning("Could not set icon: %s", e)

def create_splash(app, logo_path, text):
    """Create a splash screen with a box around the logo and text below it."""
    splash = QWidget(flags=Qt.SplashScreen | Qt.FramelessWindowHint)

    # Set white background
    splash.setStyleSheet("background-color: white;")

    layout = QVBoxLayout(splash)
    layout.setContentsMargins(20, 20, 20, 20)
    layout.setSpacing(10)
    layout.setAlignment(Qt.AlignCenter)

    # Logo
    pixmap = QPixmap(logo_path)
    if not pixmap.isNull():
        pixmap = pixmap.scaled(300, 300, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        logo_label = QLabel()
        logo_label.setPixmap(pixmap)
        logo_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(logo_label)

    # Text label under logo
    text_label = QLabel(text)
    text_label.setFont(QFont("Arial", 12))
    text_label.setAlignment(Qt.AlignCenter)
    layout.addWidget(text_label)

    # Center splash on screen
    screen = app.desktop().screenGeometry()
    splash.resize(600, 450)
    splash.move((screen.width() - splash.width()) // 2,
                (screen.height() - splash.height()) // 2)
    splash.show()
    app.processEvents()
    return splash

def main(argv=None):
    """Bootstrapped entry point to prevent window flicker."""
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--file-log", action="store_true", help="Enable file-based crash logging")
    parser.add_argument("--log-file", help="Crash log file path (implies --file-log)")
    args, _ = parser.parse_known_args(argv if argv is not None else sys.argv[1:])

    enable_file_log = bool(args.file_log or args.log_file)

    app = QApplication(sys.argv)
    _install_global_error_reporting(
        app,
        enable_file_log=enable_file_log,
        crash_log_path=args.log_file,
    )

    # Use new splash function
    logo_path = resource_path("icons/logo.PNG")
    splash = create_splash(app, logo_path, "Analyzing bike aerodynamics...")

    # Create main window only after splash is shown
    def create_main_window():
        if splash:
            splash.close()
        # Keep a strong reference so the window is not garbage-collected.
        app.main_window = GUIInterface(app)
        app.main_window.show()

    # Delay window creation
    QTimer.singleShot(2500, create_main_window)
    try:
        sys.exit(app.exec_())
    except Exception:
        tb = traceback.format_exc()
        _append_crash_log(f"[APP LOOP EXCEPTION]\n{tb}")
        _logger.exception("Application event loop crashed")
        extra = f"\n\nSee log: {_CRASH_LOG_PATH}" if _FILE_LOG_ENABLED else ""
        _show_fatal_dialog("Fatal Error", f"The application crashed unexpectedly.{extra}")
        raise

if __name__ == "__main__":
    main()