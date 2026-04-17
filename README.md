# CdA Analyser

<p align="center">
	<img src="src/icons/logo.PNG" alt="CdA Analyser logo" width="140">
</p>

Estimate cycling aerodynamic drag area (CdA) from FIT ride files using a steady-segment pipeline with weather and elevation support.

## What It Does

CdA Analyser reads ride data and computes CdA from stable riding portions. The current implementation:

- Detects steady segments from speed, power, and slope stability.
- Splits each steady segment into sub-segments for more local CdA estimation.
- Models rolling, gradient, inertial, and aerodynamic power.
- Uses weather-aware wind modeling and reports yaw angle.
- Produces weighted CdA summaries with outlier rejection.
- Supports both CLI and a full PyQt5 GUI with map, plots, and simulation.

## Data Flow

```text
FIT file
	-> parse and normalize ride records
	-> optional elevation enrichment (Open-Elevation / Open-Meteo)
	-> optional route weather prefetch
	-> steady-segment detection
	-> sub-segment CdA analysis
	-> weighted summary + visuals + export
```

## Main Components

- `src/main.py`: Entry point, GUI/CLI selection.
- `src/fit_parser.py`: FIT parsing, unit normalization, distance computation.
- `src/analyzer.py`: Segment detection, CdA calculation, summary metrics.
- `src/segment_splitter.py`: Steady-segment splitting into analysis chunks.
- `src/weather.py`: Open-Meteo weather fetch and route prefetch.
- `src/elevation.py`: Open-Elevation and Open-Meteo elevation batch fetch.
- `src/qt_gui.py`: Main GUI, analysis workflow, map, plots, simulation.
- `src/cli.py`: Command-line analysis and JSON export.

## Quick Start

### 1. Create and activate a virtual environment

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Linux/macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Run the app

GUI:

```bash
cd src
python main.py --gui
```

CLI:

```bash
cd src
python main.py --cli --file ../data/your_ride.fit
```

## GUI Highlights

- Optional weather prefetch at file load.
- Optional elevation enrichment at file load (Open-Elevation / Open-Meteo).
- Automatic map and plot generation after analysis.
- Wind effect slider for fast re-analysis on preprocessed segments.
- Weather simulation tab for what-if analysis.

## CLI Highlights

- Segment-level metrics including CdA, yaw, wind angle, and speeds.
- Summary with weighted CdA (all vs kept), weather averages, and ride stats.
- Optional JSON export for downstream processing.

## Output Metrics

Segment outputs include:

- CdA mean and spread.
- Ground speed, wind component, and air speed.
- Effective wind, wind angle, and yaw.
- Power decomposition (aero, rolling, gradient, inertial).
- Start/end times, duration, distance, and weather conditions.

Summary outputs include:

- Weighted CdA (all segments and kept subset).
- Keep percentage and used segment count.
- Total analyzed duration and distance.
- Average weather and speed metrics.
- Ride information (time span, distance, speed, power, elevation, NP).

## Build Executable (PyInstaller)

Single-file:

```powershell
.\.venv\Scripts\Activate.ps1
cd src
python -m PyInstaller --onefile --windowed --icon=icons/logo_blue.ico --name="CdA-Analyser" --noupx --add-data "icons;icons" main.py
```

Multi-file:

```powershell
.\.venv\Scripts\Activate.ps1
cd src
python -m PyInstaller --onedir --windowed --icon=icons/logo_blue.ico --name="CdA-Analyser" --noupx --add-data "icons;icons" main.py
```

## Notes

- Weather/elevation APIs are optional and have safe fallbacks.
- FIT-native altitude is preserved and can be used directly for analysis.
- The detailed implementation overview is documented in `CODE_ANALYSIS.md`.

## License

Licensed under MIT. See [LICENSE](LICENSE).

## Credits

- App icon includes "Time Trial Bike" by [Izwar Muis](https://www.flaticon.com/authors/izwar-muis), from [Flaticon](https://www.flaticon.com/free-icon/time-trial-bike_17736701).
- Flaticon content is used under the Flaticon license (free for personal and commercial use with attribution).