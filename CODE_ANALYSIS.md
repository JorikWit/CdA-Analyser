# CdA Analyzer - Current Code Analysis

## Project Overview

CdA Analyzer is a Python application that analyzes cycling FIT files to estimate aerodynamic drag area (CdA) from steady riding segments. It supports both a CLI and a PyQt5 GUI, uses weather and optional elevation APIs, and now computes CdA through a sub-segment pipeline instead of treating each steady segment as one uniform block.

Purpose:
- Parse FIT ride data.
- Detect steady-state riding sections.
- Estimate rolling, gradient, inertial, and aerodynamic power components.
- Compute CdA per sub-segment, then aggregate to segment-level results.
- Present results in CLI, GUI, map, plots, and simulation views.

Status of this document:
- This file was refreshed against the current code in src/ and scripts/.
- It intentionally reflects current behavior, not older behavior preserved in previous analysis notes.

---

## High-Level Architecture

Current data flow:

```text
FIT File
  -> FITParser
     -> unit conversion / distance / altitude preparation
     -> optional Open-Elevation batch fetch
  -> optional weather prefetch at file load
  -> CDAAnalyzer preprocess
     -> derived metrics
     -> steady-mask filtering
     -> steady segment grouping
     -> per-segment weather attachment
  -> CDAAnalyzer analysis
     -> split steady segment into sub-segments
     -> compute local wind / slope / acceleration per sub-segment
     -> compute CdA values per point
     -> aggregate sub-segment results to segment result
  -> summary / plots / map / export / simulation
```

Primary modules:
1. src/main.py - Entry point and interface selection.
2. src/config.py - Default parameters and API endpoints.
3. src/fit_parser.py - FIT parsing, unit conversion, distance calculation, optional elevation mapping.
4. src/analyzer.py - Core preprocessing, CdA computation, summary statistics.
5. src/weather.py - Open-Meteo client and route weather prefetch.
6. src/elevation.py - Open-Elevation batch client.
7. src/segment_splitter.py - Splits steady segments into analysis sub-segments.
8. src/qt_gui.py - Main GUI implementation.
9. src/cli.py - CLI entry point and reporting.
10. src/gui.py - Legacy Tkinter GUI retained in the repository.
11. src/utils.py - Small shared helpers.
12. scripts/*.py - Standalone utilities.
13. src/icon.py - Embedded base64 icon asset.

---

## Current Default Parameters

The previous analysis document contained older defaults. The current defaults in src/config.py are:

```python
DEFAULT_PARAMETERS = {
    'min_segment_length': 50,
    'min_duration': 15,
    'max_slope_variation': 1.0,
    'min_speed': 5.0,
    'max_speed': 20.0,
    'speed_steady_threshold': 0.35,
    'power_steady_threshold': 150.0,
    'slope_steady_threshold': 3.0,
    'cda_keep_percent': 75.0,
    'subsegment_min_duration_s': 5.0,
    'subsegment_min_points': 10,
    'rider_mass': 75.0,
    'bike_mass': 11.0,
    'rolling_resistance': 0.003,
    'drivetrain_loss': 0.0275,
    'wind_effect_factor': 0.40,
    'use_weather_api': True,
    'use_open_elevation_api': False,
    'weather_sample_distance_m': 3000.0,
}
```

Important changes versus the older document:
- Lower minimum segment length and duration.
- Lower minimum speed.
- Much lower power stability threshold.
- Different default bike mass.
- Different default drivetrain loss.
- Higher default wind effect factor.
- New sub-segment controls.
- New booleans for weather and elevation API usage.
- New weather prefetch sampling distance.

API endpoint constants:
- OPEN_METEO_URL_FORCAST
- OPEN_METEO_URL_ARCIVE
- OPEN_ELEVATION_URL

Note:
- The names FORCAST and ARCIVE are misspelled in code, but used consistently.

---

## File-by-File Analysis

### 1. src/main.py

Purpose:
- Select GUI or CLI mode.

Behavior:
- Parses only --gui and --cli first using parse_known_args().
- If --gui is passed, imports and starts qt_gui.main.
- If --cli is passed, rewrites sys.argv for the CLI and starts cli.main().
- If neither is passed, it tries GUI first and falls back to CLI on ImportError.

Notes:
- The primary GUI is PyQt5, not Tkinter.
- Remaining CLI arguments are forwarded.

---

### 2. src/config.py

Purpose:
- Store runtime defaults and API URLs.

Contains:
- DEFAULT_PARAMETERS with current defaults.
- Open-Meteo forecast and archive URLs.
- Open-Elevation URL.
- Large multiline historical notes with rider and route tuning examples.

Notable current parameters not described in the old document:
- subsegment_min_duration_s
- subsegment_min_points
- use_weather_api
- use_open_elevation_api
- weather_sample_distance_m

Observation:
- max_slope_variation exists in config but is not part of the current steady-mask filtering logic in analyzer.py.

---

### 3. src/fit_parser.py

Purpose:
- Parse Garmin FIT records into a Pandas DataFrame.
- Normalize columns.
- Optionally enrich altitude using Open-Elevation.

Class:
- FITParser

Key methods:
- parse_fit_file(file_path, use_open_elevation_api=False, elevation_service=None, status_callback=None)
- _process_data(df, use_open_elevation_api=False, elevation_service=None, status_callback=None)
- apply_open_elevation_to_dataframe(df, elevation_service=None, status_callback=None)
- _calculate_distance(df)

Current behavior:
1. Reads all record messages via fitparse.FitFile.
2. Builds a DataFrame from non-empty records.
3. Converts timestamp to pandas datetime.
4. Converts position_lat and position_long from semicircles to degrees.
5. Converts speed from mm/s to m/s if values are clearly too large.
6. Preserves FIT altitude in altitude_fit when present.
7. Optionally applies Open-Elevation and writes altitude_api plus active altitude.
8. Computes cumulative distance with a vectorized haversine implementation if missing.
9. Forward/backward fills only safe non-critical columns.

Important current detail:
- Power and speed are not forward-filled anymore.
- This preserves dropout gaps instead of smearing the last observed reading through missing periods.

Elevation handling:
- elevation_source is tracked on the parser instance.
- Open-Elevation is skipped when GPS coordinates are missing or invalid.
- Status messages can be streamed back to the GUI through status_callback.

Open-Elevation integration:
- Delegates batch retrieval to ElevationService.
- Uses original (lat, lon) tuples as lookup keys.
- Writes both altitude_api and active altitude when API data is available.

Distance calculation:
- Uses vectorized NumPy haversine calculations.
- Zeros segments with invalid coordinate endpoints.
- Returns cumulative distance as a Python list.

---

### 4. src/elevation.py

Purpose:
- Fetch altitude data from the Open-Elevation API in batches.

Class:
- ElevationService

Key methods:
- _fetch_chunk(coords_chunk, retry_count=0, max_retries=3, status_callback=None)
- get_elevations_batch(coordinates, chunk_size=500, status_callback=None)

Behavior:
- Uses a persistent requests.Session().
- Sends JSON payloads with coordinate batches.
- Logs raw API responses through status_callback when provided.
- Retries on HTTP 429 with exponential backoff.
- Retries oversized 413 batches using smaller sub-chunks.
- Returns a dictionary mapping original coordinate tuples to elevation values.

Design detail:
- The service deliberately uses original coordinate tuples as keys rather than echoed API coordinates to avoid precision mismatch during lookup.

This module was missing from the previous analysis document and is now part of the main data-loading path.

---

### 5. src/segment_splitter.py

Purpose:
- Split a steady segment into smaller analysis chunks.

Function:
- split_into_subsegments(segment_df, min_duration_s=20.0, min_points=10)

Current role in the pipeline:
- The analyzer no longer computes CdA only once per steady segment.
- It first splits each steady segment into sub-segments to improve local direction, slope, and acceleration accuracy.

Behavior:
- Requires both minimum duration and minimum point count.
- Returns the full segment unchanged if it is too small to split.
- Merges tiny trailing remainders into the previous sub-segment.
- Preserves original DataFrame indices.

This module was also missing from the previous analysis document.

---

### 6. src/analyzer.py

Purpose:
- Core ride preprocessing, physics calculations, CdA estimation, and summary generation.

Class:
- CDAAnalyzer

#### Initialization and state

__init__(parameters=None) sets:
- parameters
- logger
- _total_mass
- _drivetrain_efficiency
- weather_cache
- preloaded_weather_samples
- allow_runtime_weather_fetch
- elevation_source

update_parameters(new_parameters) recalculates total mass and drivetrain efficiency.

#### Main public flow

analyze_ride(df, weather_service=None, preprocessed_segments=None):
1. Uses provided preprocessed segments or preprocesses the ride.
2. Analyzes every steady segment.
3. Builds a summary.
4. Detects whether GPS coordinates exist.
5. Extracts ride metadata and stores it inside summary['ride_info'].
6. Returns:

```python
{
    'segments': [...],
    'summary': {...},
    'parameters': {...},
}
```

Important correction:
- ride_info is not a top-level field anymore.
- It is nested under summary when available.

#### Preprocessing

preprocess_ride_data(df, weather_service=None):
1. Calls identify_steady_segments(df).
2. Gets weather for each segment.
3. Stores weather data directly on each segment DataFrame as a weather_data column.

identify_steady_segments(df):
1. Calculates slope and acceleration.
2. Creates the steady-state mask.
3. Groups consecutive steady rows into segments.
4. Filters segments by duration and distance.

#### Derived metrics

_calculate_derived_metrics(df) calls:
- _calculate_slope(df)
- _calculate_acceleration(df)

Slope:
- Uses altitude.diff() and distance.diff().
- Stores degrees in slope_degrees.

Acceleration:
- Uses speed.diff() / timestamp.diff().dt.total_seconds().
- Preserves index alignment when assigning back into sliced DataFrames.

#### Steady-state detection

_create_steady_mask(df) applies:
- _apply_speed_filter(df, mask, initial_count)
- _apply_stability_filters(df, mask, initial_count)

Current filters:
- Speed within [min_speed, max_speed]
- Rolling standard deviation over 10 samples for:
  - power
  - speed
  - slope_degrees

Current thresholds come from:
- power_steady_threshold
- speed_steady_threshold
- slope_steady_threshold

Grouping logic:
- _group_into_segments(df, steady_mask) walks the mask and slices consecutive steady blocks.
- _is_valid_segment(segment_df) currently only checks len(segment_df) >= 10 before the duration/distance filter.
- _filter_segments_by_criteria(segments) then enforces min_duration and min_segment_length.

#### Sub-segment CdA pipeline

This is the biggest functional difference from the previous analysis.

calculate_cda_for_segment(segment_df, weather_data=None) now:
1. Builds one set of environmental conditions for the full steady segment.
2. Splits the segment into sub-segments via split_into_subsegments().
3. Computes one result per sub-segment with _calculate_cda_for_subsegment().
4. Aggregates sub-segment results with _aggregate_subsegment_results().

_calculate_cda_for_subsegment(sub_df, env_conditions):
1. Creates rolling averages.
2. Computes power components using local chunk data.
3. Computes CdA per point.
4. Filters per-point CdA outliers using IQR.
5. Returns a sub-result containing CdA, power components, duration, distance, and wind outputs.

_aggregate_subsegment_results(segment_df, sub_results, env_conditions):
- Uses duration-weighted averages for scalar outputs.
- Uses a circular weighted mean for wind_angle.
- Computes weighted yaw.
- Returns a segment result that includes both current and backward-compatible speed fields.

#### Rolling averages

_prepare_averaged_data(segment_df, window_size=5) builds:
- speeds
- powers
- accelerations
- slopes

It then filters invalid rows where speed or key inputs are missing.

Fallback details:
- Missing acceleration falls back to an index-aligned zero series.
- Missing slope falls back to a zero series.

#### Environmental conditions

_get_environmental_conditions(weather_data) returns:
- air_density
- wind_speed
- wind_direction

_calculate_air_density(weather_data) is a static method that uses the ideal gas law directly.

#### Power model

_calculate_power_components(averaged_data, env_conditions, segment_df) computes:
- effective_powers
- rolling_powers
- gradient_powers
- inertial_powers
- aero_powers
- wind_effects

Current formulas:

```text
effective_power = measured_power * drivetrain_efficiency

rolling_power  = mass * g * cos(slope) * speed * Crr
gradient_power = mass * g * sin(slope) * speed
inertial_power = mass * acceleration * speed

aero_power = effective_power - rolling_power - gradient_power - inertial_power
```

Negative aerodynamic power is clamped to 0.0.

#### Wind model

_calculate_wind_effects(segment_df, wind_speed, wind_direction, bike_speed):
- Uses GPS-based direction when possible.
- Falls back to a conservative partial-headwind model when GPS direction cannot be computed.

_calculate_wind_from_coordinates(...):
- Derives segment bearing from first and last valid coordinates.
- Computes relative wind angle normalized to [-180, 180].
- Scales along-travel wind by wind_effect_factor.
- Computes air speed as bike_speed + effective_wind.
- Clamps very low air speed to 0.1.

_calculate_wind_fallback(wind_speed, bike_speed):
- Uses wind_speed * 0.3 as a conservative effective wind component.

#### Yaw calculation

_calculate_yaw_angle(wind_speed, wind_angle_deg, v_ground, effective_wind):
- Computes the crosswind angle from the rider's perspective.
- Returns yaw in degrees.

Meaning:
- 0 deg = pure headwind.
- positive yaw = crosswind from one side.
- negative yaw = crosswind from the opposite side.

Yaw is stored in segment results and used throughout CLI and GUI reporting.

#### CdA formula

The old document stated the simplified formula using v_air^3. The current implementation uses:

```text
P_aero = 0.5 * rho * CdA * v_air^2 * v_ground

CdA = (2 * P_aero) / (rho * v_air^2 * v_ground)
```

Implemented in _calculate_single_cda(speed, aero_power, effective_wind, air_density).

This is one of the key corrections from the old analysis.

#### Per-point outlier removal

_filter_cda_outliers(cda_values):
- Uses the IQR method.
- Keeps values inside [Q1 - 1.5 * IQR, Q3 + 1.5 * IQR].

This acts at the per-point or per-sub-segment level.

#### Segment results

Current segment result fields can include:

```python
{
    'segment_id': int,
    'cda': float,
    'cda_std': float,
    'cda_points': int,
    'residual': float,
    'duration': float,
    'distance': float,
    'air_density': float,
    'v_ground': float,
    'v_wind': float,
    'v_air': float,
    'effective_wind': float,
    'air_speed': float,
    'wind_angle': float,
    'yaw': float,
    'subsegments': list,
    'speed': float,
    'power': float,
    'effective_power': float,
    'acceleration': float,
    'slope': float,
    'aero_power': float,
    'rolling_power': float,
    'gradient_power': float,
    'inertial_power': float,
    'start_time': Timestamp,
    'end_time': Timestamp,
    'start_elevation': float | None,
    'start_elevation_fit': float | None,
    'start_elevation_api': float | None,
    'temperature': float | None,
    'pressure': float | None,
    'wind_speed': float | None,
    'wind_direction': float | None,
}
```

Important differences from the old document:
- Explicit v_ground, v_wind, and v_air are now first-class outputs.
- yaw is included.
- subsegments are included.
- Starting elevation fields are included.

#### Ride information extraction

_extract_ride_info(df) now returns:
- date
- start_time
- end_time
- duration_seconds
- duration_hms
- total_distance_m
- average_speed_mps
- average_speed_kmh
- average_power_w
- average_heart_rate_bpm
- normalized_power_w
- elevation_gain_m

Notes:
- Timestamps are localized to local wall-clock time through _to_local_time().
- Normalized power is computed from a 30-sample rolling mean followed by fourth-power averaging and fourth-root extraction.

#### Weather handling

_get_weather_data_for_segment(segment, weather_service, segment_id) currently supports:
1. use_weather_api == False -> return a default no-wind weather dict.
2. Already attached weather_data on the segment -> reuse it.
3. Preloaded route weather samples -> nearest match by timestamp, then distance fallback.
4. In-memory cache -> reuse it.
5. Runtime weather fetch if allowed.

Related helpers:
- _store_weather_data()
- _default_no_wind_weather()
- _get_preloaded_weather_for_segment()

#### Summary calculation

_calculate_weighted_cda_metrics(segment_results):
- Computes duration-weighted CdA across all segments.
- Iteratively removes the segment with the largest absolute deviation from the current weighted mean.
- Stops when cda_keep_percent of segments remain.
- Returns:
  - weighted_cda_all
  - weighted_cda_kept
  - keep_percent
  - kept_segments_used

_calculate_summary(segment_results) returns a dict with fields including:

```python
{
    'total_segments': int,
    'weighted_cda': float,
    'weighted_cda_all': float,
    'weighted_cda_kept': float,
    'keep_percent': float,
    'kept_segments_used': int,
    'average_cda': float,
    'cda_std': float,
    'wind_coefficients': list | None,
    'min_cda': float,
    'max_cda': float,
    'total_duration': float,
    'total_distance': float,
    'avg_wind_speed': float,
    'avg_ground_speed': float,
    'avg_wind_component': float,
    'avg_air_speed': float,
    'avg_acceleration': float,
    'avg_temp': float,
    'avg_press': float,
    'avg_wind_direction': float,
    'elevation_source': str | None,
    'has_gps_coordinates': bool,
    'ride_info': dict,
}
```

Wind coefficient fitting:
- _calculate_wind_angle_coefficients(segment_results) fits a quadratic polynomial of CdA versus wind angle.
- Requires at least three valid points and non-zero angle spread.

Summary edge case handling:
- If no segments exist, _calculate_summary() returns {}.
- analyze_ride() only inserts ride info when summary is truthy.

---

### 7. src/weather.py

Purpose:
- Retrieve weather data from Open-Meteo.
- Prefetch route weather samples for later reuse.

Class:
- WeatherService

Key methods:
- get_weather_data(latitude, longitude, timestamp, status_callback=None)
- prefetch_weather_for_ride(ride_df, sample_distance_m=3000.0, status_callback=None)
- calculate_air_density(temperature, pressure, humidity=50)

Current behavior:
- Uses a persistent requests.Session().
- Selects forecast or archive endpoint depending on whether the requested date is within about 30 days.
- Requests:
  - temperature
  - wind speed
  - wind direction
  - pressure
- Requests wind speed in meters per second by passing wind_speed_unit='ms'.
- Selects the closest hourly weather sample.
- Falls back to defaults on any exception.

Helpers:
- _to_local_timestamp(timestamp) strips timezone info and interprets time as local wall clock.
- _closest_index_from_sorted(values, target) supports nearest sampled distance lookups.

Route weather prefetch:
- Samples the ride by distance, default every 3000 m.
- Groups near-identical requests by rounded lat/lon/date/hour.
- Fetches one weather call per grouped bucket.
- Returns:

```python
{
    'samples': [...],
    'sample_count': int,
    'grouped_request_count': int,
}
```

This prefetch behavior was not documented in the previous analysis.

---

### 8. src/cli.py

Purpose:
- Command-line interface for loading one FIT file, running analysis, showing results, and optionally exporting JSON.

Key functions:
- main()
- _load_parameters(param_file)
- _display_results(results)
- _save_results(results, output_file)

Current flow:
1. Parse CLI arguments.
2. Parse the FIT file.
3. Display the elevation source used during parsing.
4. Create the analyzer and weather service.
5. Enter an interactive loop:
   - load parameters
   - run analysis
   - display results
   - optionally export JSON
   - wait for Enter to re-run or Ctrl-C to exit

Current CLI output fields:
- v_g, v_w, v_a
- Angle
- Yaw
- Slope
- Power
- CdA

Summary output includes:
- GPS availability
- elevation source
- weighted CdA for all segments
- weighted CdA for kept subset
- keep percent and kept segment count
- average wind speed
- average ground speed
- average wind component
- average air speed
- total duration
- total analyzed distance

Differences from the old document:
- CLI now reports yaw.
- CLI now reports explicit ground/wind/air speeds.
- CLI now reports elevation source and GPS availability.

---

### 9. src/qt_gui.py

Purpose:
- Main production GUI.
- Supports file loading, parameter editing, result display, route map, plots, and weather simulation.

Top-level features:
- Crash logging and global exception hooks.
- Splash screen.
- File-load-time weather and elevation prefetch options.
- Background analysis thread.
- Automatic map and plot generation after analysis.
- Post-analysis wind effect slider re-analysis.
- Weather simulation tab.

#### Global crash reporting

Current helper functions:
- _append_crash_log(message)
- _mark_stage(stage)
- _show_fatal_dialog(title, message)
- _python_excepthook(exc_type, exc_value, exc_tb)
- _threading_excepthook(args)
- _qt_message_handler(mode, context, message)
- _install_global_error_reporting(app, enable_file_log=False, crash_log_path=None)

Behavior:
- Can enable file-based crash logging via CLI flags to the GUI entry point.
- Installs Python and thread exception hooks.
- Enables faulthandler.
- Optionally captures Qt messages.
- Tracks major execution stages in a stage log.

#### Worker thread

Class:
- WorkerThread(QThread)

Signals:
- finished(results, error, preprocessed_segments)
- status(message)

Behavior:
- Selects the active altitude source for analysis.
- Preprocesses the ride.
- Runs the analyzer.
- Emits both the final results and the preprocessed segments.

#### Main window

Class:
- GUIInterface(QMainWindow)

Important instance state:
- ride_data
- analysis_results
- preprocessed_segments
- simulation_results
- segment_data_map
- preloaded_weather_samples
- weather_api_loaded
- elevation_api_loaded
- load_weather_api_on_file_load
- load_elevation_api_on_file_load
- _map_html_path

#### File tab

Features:
- Browse FIT file.
- Automatically load after browsing.
- Two file-load checkboxes:
  - weather API on file load
  - elevation API on file load
- About dialog.
- Status text area with API debug output.

Load behavior:
- Saves parameters first.
- Clears all previous state via _clear_all_loaded_data_for_reload().
- Parses the FIT file, optionally with fresh elevation API calls.
- Optionally prefetches weather for the whole route.
- Rebuilds the analyzer with the refreshed parameter set.
- Disables runtime weather fetch on the analyzer and prefers preloaded route weather.

Helper methods involved:
- _can_load_new_file()
- _clear_all_loaded_data_for_reload()
- _prefetch_weather_api_on_load()
- _sync_api_parameter_checkbox_state()

#### Parameters tab

Features:
- Scrollable parameter form.
- Boolean parameters rendered as checkboxes.
- weather_sample_distance_m is intentionally hidden from the form.
- Run Analysis button.

#### Results tab

Contains:
- Progress bar.
- Status label.
- Global wind effect factor slider.
- Results notebook with:
  - Summary
  - Map
  - Plots

Summary view includes:
- Parameters used.
- Ride information.
- Average weather conditions.
- Segment table with yaw and elevation columns.
- Weighted and average CdA stats.

Map behavior:
- Uses QWebEngineView.
- Enables LocalContentCanAccessRemoteUrls for Leaflet/Folium assets.
- Saves Folium output to a temporary HTML file and loads it via setUrl().
- Draws:
  - full route in gray
  - colored steady segments
  - numbered start markers
  - popup with segment CdA, speed, power, and slope

Plot behavior:
- Generates six plots:
  1. speed and power vs distance
  2. CdA by segment
  3. CdA vs air speed
  4. speed vs power
  5. CdA vs yaw
  6. CdA vs wind angle
- Adds summary text below the plots.
- Reuses persistent Matplotlib canvases.

Automatic post-analysis visuals:
- _auto_generate_visuals() regenerates map and plots automatically after successful analysis.

Wind effect slider re-analysis:
- _on_wind_effect_changed() reruns analysis using existing preprocessed segments.
- Preserves old ride_info and injects it into the refreshed summary.

#### Simulation tab

Controls:
- wind speed slider
- wind angle slider
- wind effect factor slider
- temperature entry
- pressure entry

Workflow:
- Uses the original preprocessed_segments.
- Re-runs per-segment CdA with simulated weather inputs.
- Temporarily changes wind_effect_factor, then restores the original value.
- Displays a summary and generates simulation plots.

Simulation outputs mirror the main analysis view and include yaw.

#### Extra helper not on primary path

_fetch_missing_elevation_data():
- Fetches altitude_api for already prepared segments.
- Sets analyzer elevation source to Open-Elevation API (fetched during simulation).
- This helper exists in code but is not part of the default analysis flow.

#### GUI entry point

main(argv=None):
- Supports --file-log and --log-file.
- Creates QApplication.
- Installs global error reporting.
- Shows splash screen for 2.5 seconds.
- Creates and shows the main window after the splash.

---

### 10. src/gui.py

Purpose:
- Legacy Tkinter GUI retained in the repository.

Status:
- Not the primary GUI.
- main.py targets qt_gui.py for GUI mode.

Features still present:
- Splash screen.
- File, parameter, and results tabs.
- Threaded analysis.
- External map generation.
- Matplotlib plotting.

Notable differences versus PyQt GUI:
- No crash-reporting infrastructure.
- No weather simulation tab.
- No route weather prefetch logic.
- No QWebEngine-based inline map.
- Older UI architecture.

It is best understood as a compatibility or legacy implementation.

---

### 11. src/utils.py

Purpose:
- Small helper utilities.

Functions:
- calculate_distance(lat1, lon1, lat2, lon2)
- interpolate_missing_data(df, columns)
- calculate_slope(distance, altitude)
- format_duration(seconds)
- validate_parameters(parameters)

Current role:
- These are generic helpers.
- Core ride distance calculation in the parser now uses a vectorized implementation instead of relying on these helpers.

Validation currently checks only:
- required keys present
- positive rider mass
- positive bike mass
- non-negative rolling resistance

---

### 12. src/icon.py

Purpose:
- Store the application icon as an embedded base64 string.

Role:
- Used by both GUI implementations for splash/about/icon display.

This file was not mentioned in the previous analysis document.

---

### 13. scripts/estimate_power_speed_or_cda.py

Purpose:
- Standalone calculator for power, speed, and CdA relationships.

Functions:
- power_required(v, cda, crr, mass=85, air_density=1.225, gravity=9.81)
- speed_from_power(power, cda, crr, mass=85, air_density=1.225, gravity=9.81)
- cda_from_power_speed(power, speed, crr, mass=85, air_density=1.225, gravity=9.81)
- main()

Behavior:
- Uses a simple rolling + aero power model.
- Applies drivetrain adjustments via 1.03 and 0.97 factors.
- Uses scipy.optimize.fsolve() to recover speed from power.
- Offers a small interactive console menu.

Defaults in this script:
- mass = 86
- gravity = 9.81
- air_density = 1.225

---

### 14. scripts/bestbikesplit_to_intervals.py

Purpose:
- Convert a BestBikeSplit-style tab-separated race plan into training interval text.

Functions:
- parse_time_to_seconds(time_str)
- format_seconds_to_minsec_dash(total_seconds)
- convert_distance_based(line, watt_delta)
- convert_time_based(line, watt_delta, time_percent)
- main()

Modes:
- distance
- time

Arguments:
- input_file
- --mode
- --time-percent
- --watt-delta

Output naming:
- <basename>_distance_<delta>w.txt
- <basename>_time_<pct>pct_<delta>w.txt

---

### 15. scripts/generate_icon.py

Purpose:
- Convert logo_blue.ico into a Python module containing base64 icon data.

Behavior:
- Reads logo_blue.ico.
- Base64 encodes the binary bytes.
- Writes icon.py with a LOGO_BASE64 constant.

This script was already mentioned in the older document and still behaves the same.

---

## Current Result Structures

### Segment result

Representative structure:

```python
{
    'segment_id': 0,
    'cda': 0.2901,
    'cda_std': 0.0123,
    'cda_points': 42,
    'residual': 5.4,
    'duration': 38.0,
    'distance': 402.0,
    'air_density': 1.21,
    'v_ground': 10.7,
    'v_wind': 0.3,
    'v_air': 11.0,
    'effective_wind': 0.3,
    'air_speed': 11.0,
    'wind_angle': -22.0,
    'yaw': -6.1,
    'subsegments': [...],
    'speed': 10.7,
    'power': 265.0,
    'effective_power': 257.7,
    'acceleration': 0.01,
    'slope': 0.1,
    'aero_power': 190.0,
    'rolling_power': 27.0,
    'gradient_power': 2.0,
    'inertial_power': 1.0,
    'start_time': Timestamp(...),
    'end_time': Timestamp(...),
    'start_elevation': 14.0,
    'start_elevation_fit': 13.8,
    'start_elevation_api': 14.2,
    'temperature': 16.1,
    'pressure': 1015.3,
    'wind_speed': 3.1,
    'wind_direction': 240.0,
}
```

### Top-level analysis result

```python
{
    'segments': [...],
    'summary': {
        'total_segments': int,
        'weighted_cda': float,
        'weighted_cda_all': float,
        'weighted_cda_kept': float,
        'keep_percent': float,
        'kept_segments_used': int,
        'average_cda': float,
        'cda_std': float,
        'wind_coefficients': [a, b, c] | None,
        'min_cda': float,
        'max_cda': float,
        'total_duration': float,
        'total_distance': float,
        'avg_wind_speed': float,
        'avg_ground_speed': float,
        'avg_wind_component': float,
        'avg_air_speed': float,
        'avg_acceleration': float,
        'avg_temp': float,
        'avg_press': float,
        'avg_wind_direction': float,
        'elevation_source': str | None,
        'has_gps_coordinates': bool,
        'ride_info': {
            'date': date,
            'start_time': Timestamp,
            'end_time': Timestamp,
            'duration_seconds': int,
            'duration_hms': str,
            'total_distance_m': float | None,
            'average_speed_mps': float | None,
            'average_speed_kmh': float | None,
            'average_power_w': float | None,
            'average_heart_rate_bpm': float | None,
            'normalized_power_w': float | None,
            'elevation_gain_m': float,
        },
    },
    'parameters': dict,
}
```

Important correction:
- ride_info is inside summary, not beside it.

---

## Key Design Patterns In Current Code

### 1. Sub-segment analysis instead of pure segment analysis

Steady segments are now split into smaller chunks before CdA is computed. This reduces bias when one steady segment still contains local changes in direction, slope, or acceleration.

### 2. Two-level outlier handling

There are two separate outlier stages:
- IQR filtering of per-point CdA values within a sub-segment.
- Iterative removal of segment-level CdA outliers to produce the kept-percent weighted summary value.

### 3. Route weather prefetch

The GUI can load weather for the full ride at file-load time. Analysis then reuses those samples instead of calling the weather API separately for every segment.

### 4. Flexible elevation source switching

Altitude can come from:
- FIT file altitude
- Open-Elevation API altitude fetched during file load

The active altitude source is selected immediately before analysis in the worker thread.

### 5. Backward-compatible output aliases

The analyzer now exposes v_ground, v_wind, and v_air, but still keeps effective_wind and air_speed for compatibility with older code paths.

### 6. GUI crash observability

The PyQt GUI now includes dedicated crash, thread, and stage logging to make hard-to-reproduce GUI failures diagnosable.

---

## Dependencies and External Services

Current requirements.txt lists:
- fitparse==1.2.0
- folium==0.15.1
- folium==0.20.0
- geopy==2.4.1
- matplotlib==3.10.5
- numpy==2.3.2
- pandas==2.3.2
- Pillow==11.3.0
- PyQt5==5.15.11
- PyQt5_sip==12.17.0
- Requests==2.32.5
- scipy==1.16.1

Observations:
- folium appears twice with different pinned versions.
- geopy is still listed even though current core distance calculation in fit_parser.py uses a vectorized NumPy haversine implementation.

External services:
- Open-Meteo
- Open-Elevation

---

## Current Notable Behaviors And Edge Cases

1. If weather API usage is disabled, the analyzer uses a no-wind default weather dict rather than returning None.

2. If no steady segments are found, the analyzer returns an empty summary dict instead of raising.

3. The GUI disables runtime weather fetch and relies on route prefetch performed during file load.

4. The GUI can reload a new FIT file only when the analysis worker is not running.

5. The parser preserves altitude_fit even when API altitude becomes active.

6. Yaw is now a first-class reported metric in the analyzer, CLI, GUI summary tables, and plots.

7. The simulation tab reuses preprocessed steady segments rather than re-detecting them.

8. _fetch_missing_elevation_data() exists but is not part of the normal analysis path.

---

## Code That Was Previously Not Mentioned

The following code areas existed in the repository but were missing or under-described in the earlier analysis and are now explicitly covered above:

1. src/elevation.py
2. src/segment_splitter.py
3. src/icon.py
4. GUI crash-reporting hooks in src/qt_gui.py
5. Weather route prefetch in src/weather.py
6. Yaw calculation in src/analyzer.py
7. Explicit v_ground, v_wind, v_air outputs
8. Simulation reuse of preprocessed segments in src/qt_gui.py
9. ride_info enrichment with average power, heart rate, normalized power, and elevation gain
10. Reload cleanup logic and worker-safety guards in the PyQt GUI

---

## Summary For Maintainers

The current codebase still serves the same overall purpose, but the implementation is no longer the same as described in the previous analysis document.

Most important changes:
- default parameters changed substantially
- analysis now works through sub-segments
- yaw is calculated and displayed
- route weather is prefetched during file load
- elevation is handled through a dedicated batch service
- result structures are richer and ride_info moved under summary
- the PyQt GUI gained crash reporting, safer reload behavior, and simulation/reporting improvements

This document should now be treated as the current source-level overview for the repository state reflected in src/ and scripts/.
