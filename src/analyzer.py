"""Main analysis module for CdA calculation """

import numpy as np
import pandas as pd
import logging
import math
from config import DEFAULT_PARAMETERS
from segment_splitter import split_into_subsegments

class CDAAnalyzer:
    """Analyzes cycling data to calculate coefficient of drag and frontal area (CdA)"""
    
    def __init__(self, parameters=None):
        self.parameters = parameters or DEFAULT_PARAMETERS.copy()
        self.logger = logging.getLogger(__name__)
        self._total_mass = self.parameters['rider_mass'] + self.parameters['bike_mass']
        self._drivetrain_efficiency = 1.0 - self.parameters['drivetrain_loss']  # 3% drivetrain loss
        self.weather_cache = {}
        self.elevation_source = None  # Track elevation source (FIT file or Open-Elevation API)

    def update_parameters(self, new_parameters):
        """Update analyzer parameters"""
        self.logger.info("Updating analyzer parameters")
        self.parameters.update(new_parameters)
        self._total_mass = self.parameters['rider_mass'] + self.parameters['bike_mass']
        self._drivetrain_efficiency = 1.0 - self.parameters['drivetrain_loss']  # 3% drivetrain loss
        self.logger.info(f"Parameters updated. New total mass: {self._total_mass:.1f}kg")
    
    # ============ Main Analysis Methods ============

    def analyze_ride(self, df, weather_service=None, preprocessed_segments=None):
        """Complete analysis of a ride"""
        self.logger.info("Starting ride analysis")
        
        if preprocessed_segments is not None:
            segments = preprocessed_segments
            self.logger.info(f"Using {len(segments)} preprocessed segments")
        else:
            segments = self.preprocess_ride_data(df, weather_service)
        
        segment_results = self._analyze_segments(segments)
        summary = self._calculate_summary(segment_results)

        has_gps = (
            'latitude' in df.columns and
            'longitude' in df.columns and
            len(df[['latitude', 'longitude']].dropna()) > 0
        )
        if summary:
            summary['has_gps_coordinates'] = bool(has_gps)

        ride_info = self._extract_ride_info(df)
        if summary and ride_info is not None:  # empty dict is falsy — avoids KeyError in GUI
            summary['ride_info'] = ride_info
        
        self.logger.info(f"Analysis complete: {len(segment_results)} segments analyzed")
        if summary:
            self.logger.info(f"Weighted CdA: {summary['weighted_cda']:.4f} ± {summary['cda_std']:.4f}")
        
        return {
            'segments': segment_results,
            'summary': summary,
            'parameters': self.parameters
        }
    
    
    def preprocess_ride_data(self, df, weather_service=None):
        """Preprocess ride data - detect segments and fetch weather data"""
        self.logger.info("Starting ride preprocessing")
        self.logger.info(f"Input data: {len(df)} points with columns: {list(df.columns)}")
        
        segments = self.identify_steady_segments(df)
        
        # Fetch and store weather data for all segments
        for i, segment in enumerate(segments):
            weather_data = self._get_weather_data_for_segment(segment, weather_service, i)
            if weather_data:
                # Add weather data to segment DataFrame
                segment['weather_data'] = [weather_data] * len(segment)
        
        self.logger.info(f"Preprocessing complete: {len(segments)} segments identified")
        return segments

        
    def identify_steady_segments(self, df):
        """Identify steady-state segments suitable for CdA calculation"""
        self.logger.info(f"Identifying steady segments from {len(df)} data points")
        
        if len(df) < 10:
            self.logger.warning("Insufficient data points for analysis")
            return []
        
        df = self._calculate_derived_metrics(df)
        steady_mask = self._create_steady_mask(df)
        segments = self._group_into_segments(df, steady_mask)
        
        self.logger.info(f"Found {len(segments)} steady segments")
        return segments
    
    def calculate_cda_for_segment(self, segment_df, weather_data=None):
        """Calculate CdA for a steady segment using hidden sub-segments.

        Weather data (temperature, pressure, wind speed, wind direction from the
        API) is shared across all sub-segments — it is fetched once per segment
        in ``preprocess_ride_data``.

        Each sub-segment uses its own GPS bearing to derive the *local* relative
        wind angle and its own slope / acceleration averages.  Sub-segment CdA
        values are aggregated (duration-weighted) to produce the segment result.
        """
        self.logger.debug(f"Calculating CdA for segment with {len(segment_df)} points")

        # Environmental conditions are shared for the whole segment
        env_conditions = self._get_environmental_conditions(weather_data)

        # Split into sub-segments for local accuracy
        subsegments = split_into_subsegments(
            segment_df,
            min_duration_s=self.parameters.get('subsegment_min_duration_s', 20.0),
            min_points=int(self.parameters.get('subsegment_min_points', 10)),
        )

        sub_results = []
        for sub_df in subsegments:
            sub_result = self._calculate_cda_for_subsegment(sub_df, env_conditions)
            if sub_result is not None:
                sub_results.append(sub_result)

        if not sub_results:
            self.logger.warning("No valid CdA values calculated for any sub-segment")
            return None

        return self._aggregate_subsegment_results(segment_df, sub_results, env_conditions)

    # ------------------------------------------------------------------
    # Sub-segment helpers
    # ------------------------------------------------------------------

    def _calculate_cda_for_subsegment(self, sub_df, env_conditions):
        """Calculate CdA for one sub-segment.

        Uses sub_df for its own GPS bearing (local relative wind angle) and
        its own slope / acceleration averages.  env_conditions carries the
        parent segment's air density and API wind values.
        """
        averaged_data = self._prepare_averaged_data(sub_df)
        if averaged_data is None:
            return None

        # Power components — segment_df is sub_df → local bearing
        power_components = self._calculate_power_components(
            averaged_data, env_conditions, sub_df
        )

        cda_values = self._calculate_cda_values(
            averaged_data, power_components, env_conditions
        )
        if not cda_values:
            return None

        filtered_cda = self._filter_cda_outliers(cda_values)
        if not filtered_cda:
            return None

        averages = self._calculate_segment_averages(averaged_data, power_components)
        cda_mean = float(np.mean(filtered_cda))
        cda_std  = float(np.std(filtered_cda)) if len(filtered_cda) > 1 else 0.0

        return {
            'cda':        cda_mean,
            'cda_std':    cda_std,
            'cda_points': len(filtered_cda),
            'duration':   self._get_segment_duration(sub_df),
            'distance':   self._get_segment_distance(sub_df),
            **averages,
            **power_components['wind_effects'],
        }

    def _aggregate_subsegment_results(self, segment_df, sub_results, env_conditions):
        """Aggregate per-sub-segment results into a single segment result dict.

        All scalar fields are computed as duration-weighted averages.
        Wind angle uses a circular weighted mean so that cross-winds near
        ±180° are handled correctly.
        """
        durations   = [r['duration'] for r in sub_results]
        total_dur   = sum(durations)
        weights     = [d / total_dur for d in durations]

        def wavg(key):
            return float(sum(r[key] * w for r, w in zip(sub_results, weights)))

        # Duration-weighted CdA
        cda_values  = np.array([r['cda'] for r in sub_results])
        final_cda   = float(np.average(cda_values, weights=weights))
        # Weighted population std-dev across sub-segment means
        cda_std     = float(np.sqrt(
            np.average((cda_values - final_cda) ** 2, weights=weights)
        ))
        total_pts   = sum(r['cda_points'] for r in sub_results)

        # Circular weighted mean for wind angle
        angles_rad  = np.radians([r['wind_angle'] for r in sub_results])
        avg_wind_angle = float(np.degrees(np.arctan2(
            np.average(np.sin(angles_rad), weights=weights),
            np.average(np.cos(angles_rad), weights=weights),
        )))

        averages = {
            'speed':          wavg('speed'),
            'power':          wavg('power'),
            'effective_power': wavg('effective_power'),
            'acceleration':   wavg('acceleration'),
            'slope':          wavg('slope'),
            'aero_power':     wavg('aero_power'),
            'rolling_power':  wavg('rolling_power'),
            'gradient_power': wavg('gradient_power'),
            'inertial_power': wavg('inertial_power'),
        }

        residual = self._calculate_residual(
            averages,
            final_cda,
            env_conditions,
            {'air_speed': wavg('air_speed')},
        )

        v_ground = averages['speed']
        v_wind   = wavg('effective_wind')
        v_air    = wavg('air_speed')
        
        # Calculate yaw angle using weighted averages
        yaw_angles = [self._calculate_yaw_angle(
            r.get('wind_speed', 0),
            r.get('wind_angle', 0),
            r.get('speed', 0),
            r.get('effective_wind', 0)
        ) for r in sub_results]
        
        # Weight-average the yaw values (circular mean not needed for yaw since it's typically small)
        avg_yaw = float(np.average(yaw_angles, weights=weights)) if yaw_angles else 0.0

        return {
            'cda':          final_cda,
            'cda_std':      cda_std,
            'cda_points':   total_pts,
            'residual':     residual,
            'duration':     self._get_segment_duration(segment_df),
            'distance':     self._get_segment_distance(segment_df),
            'air_density':  env_conditions['air_density'],
            # Three explicit speeds: ground / wind-component / air
            'v_ground':     v_ground,   # GPS ground speed (m/s)
            'v_wind':       v_wind,     # wind component along travel (+headwind, m/s)
            'v_air':        v_air,      # air speed = v_ground + v_wind (m/s)
            # Legacy aliases kept for backward compatibility
            'effective_wind': v_wind,
            'air_speed':    v_air,
            'wind_angle':   avg_wind_angle,
            'yaw':          avg_yaw,    # Crosswind angle from rider perspective
            'subsegments':  sub_results,
            **averages,
        }
    
    # ============ Data Preparation Methods ============

    def _extract_ride_info(self, df):
            """
            Extract basic ride-level information from the FIT dataframe
            """
            if 'timestamp' not in df.columns or df.empty:
                return None

            start_time = df['timestamp'].iloc[0]
            end_time = df['timestamp'].iloc[-1]
            duration_seconds = (end_time - start_time).total_seconds()

            total_distance = None
            avg_speed = None
            total_elevation_gain = 0.0

            if 'distance' in df.columns:
                total_distance = df['distance'].iloc[-1] - df['distance'].iloc[0]

            if 'speed' in df.columns and duration_seconds > 0:
                avg_speed = df['speed'].mean()
                
            # Calculate Elevation Gain
            if 'altitude' in df.columns:
                # Calculate difference between consecutive points
                alt_diffs = df['altitude'].diff().dropna()
                # Sum only positive differences (climbing)
                total_elevation_gain = alt_diffs[alt_diffs > 0].sum()

            return {
                'date': start_time.date(),
                'start_time': start_time,
                'end_time': end_time,
                'duration_seconds': int(duration_seconds),
                'duration_hms': self._format_seconds(duration_seconds),
                'total_distance_m': round(total_distance, 1) if total_distance is not None else None,
                'average_speed_mps': round(avg_speed, 2) if avg_speed is not None else None,
                'average_speed_kmh': round(avg_speed * 3.6, 2) if avg_speed is not None else None,
                'elevation_gain_m': round(total_elevation_gain, 1) # Added field
            }

    
    def _calculate_derived_metrics(self, df):
        """Calculate derived metrics for analysis"""
        self.logger.debug("Calculating derived metrics")
        df = df.copy()
        
        df = self._calculate_slope(df)
        df = self._calculate_acceleration(df)
        
        return df
    
    def _calculate_slope(self, df):
        """Calculate slope from altitude and distance data"""
        if 'altitude' in df.columns and 'distance' in df.columns:
            distance_diff = df['distance'].diff()
            altitude_diff = df['altitude'].diff()
            
            slope_rad = np.where(
                (distance_diff > 0) & (distance_diff.notna()) & (altitude_diff.notna()),
                np.arctan2(altitude_diff, distance_diff), 
                0
            )
            df['slope_degrees'] = np.degrees(slope_rad)
            self.logger.debug(f"Calculated slope for {len(df)} points")
        
        return df
    
    def _calculate_acceleration(self, df):
        """Calculate acceleration from speed and timestamp data"""
        if 'speed' in df.columns and 'timestamp' in df.columns:
            time_diff = df['timestamp'].diff().dt.total_seconds()
            speed_diff = df['speed'].diff()
            
            acceleration = np.where(
                (time_diff > 0) & (time_diff.notna()) & (speed_diff.notna()),
                speed_diff / time_diff,
                0
            )
            # Use df.index so assignment aligns correctly when df is a non-default-indexed slice
            df['acceleration'] = pd.Series(acceleration, index=df.index).fillna(0)
            self.logger.debug(f"Calculated acceleration for {len(df)} points")
        
        return df
    
    def _prepare_averaged_data(self, segment_df, window_size=5):
        """Prepare data with rolling averages"""
        self.logger.debug(f"Using rolling window size: {window_size}")
        
        # Extract base data
        speeds = segment_df['speed']
        powers = segment_df.get('power')
        # Provide index-aligned fallback so rolling averages and masks work correctly
        accelerations = segment_df.get(
            'acceleration',
            pd.Series([0] * len(segment_df), index=segment_df.index)
        )
        
        if powers is None:
            self.logger.warning("No power data available for segment")
            return None
            
        if speeds.isna().all() or (speeds <= 0).all():
            self.logger.warning("Invalid speed data for segment")
            return None
        
        # Apply rolling averages
        averaged_data = {
            'speeds': self._rolling_average(speeds, window_size),
            'powers': self._rolling_average(powers, window_size),
            'accelerations': self._rolling_average(accelerations, window_size),
            'slopes': self._get_averaged_slopes(segment_df, window_size)
        }
        
        # Filter valid data points
        valid_mask = self._get_valid_data_mask(averaged_data)
        if valid_mask.sum() == 0:
            self.logger.warning("No valid data points in segment after rolling average")
            return None
        
        # Apply mask to all data
        for key in averaged_data:
            averaged_data[key] = averaged_data[key][valid_mask]
        
        valid_count = len(averaged_data['speeds'])
        total_count = len(segment_df)
        self.logger.debug(f"Valid data points: {valid_count}/{total_count} ({valid_count/total_count*100:.1f}%)")
        
        return averaged_data
    
    def _rolling_average(self, series, window_size):
        """Apply rolling average to a series"""
        return series.rolling(window=window_size, min_periods=1, center=True).mean()
    
    def _get_averaged_slopes(self, segment_df, window_size):
        """Get averaged slope data with fallback handling"""
        if 'slope_degrees' in segment_df.columns:
            slopes_raw = segment_df['slope_degrees']
            slopes_rolling = self._rolling_average(slopes_raw, window_size)
            
            # Fill NaN slopes with segment average
            if slopes_rolling.isna().any():
                segment_slope_avg = slopes_raw.mean()
                slopes_rolling = slopes_rolling.fillna(segment_slope_avg)
                nan_count = slopes_rolling.isna().sum()
                self.logger.debug(f"Filled {nan_count} NaN slope values with average: {segment_slope_avg:.2f}°")
            
            return slopes_rolling
        else:
            return pd.Series([0] * len(segment_df))
    
    def _get_valid_data_mask(self, averaged_data):
        """Create mask for valid data points after averaging"""
        return (
            averaged_data['speeds'].notna() & 
            averaged_data['powers'].notna() & 
            averaged_data['accelerations'].notna() & 
            (averaged_data['speeds'] > 0)
        )
    
    # ============ Steady State Detection Methods ============
    
    def _create_steady_mask(self, df):
        """Create boolean mask for steady-state conditions"""
        self.logger.debug("Creating steady state mask")
        mask = pd.Series([True] * len(df), index=df.index)
        initial_count = len(df)
        
        # Apply various filters
        mask = self._apply_speed_filter(df, mask, initial_count)
        mask = self._apply_stability_filters(df, mask, initial_count)
        
        final_count = mask.sum()
        self.logger.info(f"Steady mask: {initial_count} → {final_count} points ({final_count/initial_count*100:.1f}%)")
        return mask
    
    def _apply_speed_filter(self, df, mask, initial_count):
        """Apply speed range filter"""
        if 'speed' in df.columns:
            speed_mask = (
                (df['speed'] >= self.parameters['min_speed']) & 
                (df['speed'] <= self.parameters['max_speed'])
            )
            mask &= speed_mask
            self.logger.debug(f"Speed filter removed {initial_count - speed_mask.sum()} points")
        return mask
    
    def _apply_stability_filters(self, df, mask, initial_count):
        """Apply stability filters for power, speed, and slope"""
        stability_filters = [
            ('power', 'power_steady_threshold'),
            ('speed', 'speed_steady_threshold'),
            ('slope_degrees', 'slope_steady_threshold')
        ]
        
        for column, threshold_key in stability_filters:
            if column in df.columns:
                rolling_std = df[column].rolling(window=10, min_periods=1).std()
                stability_mask = rolling_std <= self.parameters[threshold_key]
                mask &= stability_mask
                removed_count = initial_count - stability_mask.sum()
                self.logger.debug(f"{column.title()} stability filter removed {removed_count} points")
        
        return mask
    
    def _group_into_segments(self, df, steady_mask):
        """Group consecutive steady points into segments"""
        self.logger.debug("Grouping points into segments")
        segments = []
        current_segment = None
        
        for i, is_steady in enumerate(steady_mask):
            if is_steady and current_segment is None:
                current_segment = {'start': i}
                self.logger.debug(f"Starting segment at index {i}")
            elif not is_steady and current_segment is not None:
                current_segment['end'] = i
                segment_df = df.iloc[current_segment['start']:current_segment['end']].copy()
                if self._is_valid_segment(segment_df):
                    segments.append(segment_df)
                current_segment = None
        
        # Handle final segment
        if current_segment is not None:
            segment_df = df.iloc[current_segment['start']:].copy()
            if self._is_valid_segment(segment_df):
                segments.append(segment_df)
        
        # Filter by duration and distance
        filtered_segments = self._filter_segments_by_criteria(segments)
        
        self.logger.info(f"Segment grouping: {len(segments)} raw → {len(filtered_segments)} valid segments")
        return filtered_segments
    
    def _is_valid_segment(self, segment_df):
        """Check if segment meets minimum size requirements"""
        return len(segment_df) >= 10
    
    def _filter_segments_by_criteria(self, segments):
        """Filter segments by minimum duration and distance"""
        filtered_segments = []
        
        for i, segment in enumerate(segments):
            duration = (segment['timestamp'].iloc[-1] - segment['timestamp'].iloc[0]).total_seconds()
            distance = segment['distance'].iloc[-1] - segment['distance'].iloc[0]
            
            is_valid = (
                duration >= self.parameters['min_duration'] and 
                distance >= self.parameters['min_segment_length']
            )
            
            if is_valid:
                filtered_segments.append(segment)
                self.logger.debug(f"Segment {i}: {duration:.0f}s, {distance:.0f}m - ACCEPTED")
            else:
                self.logger.debug(f"Segment {i}: {duration:.0f}s, {distance:.0f}m - REJECTED")
        
        return filtered_segments
    
    # ============ Environmental Conditions Methods ============
    
    def _get_environmental_conditions(self, weather_data):
        """Get environmental conditions for calculations"""
        conditions = {
            'air_density': 1.225,
            'wind_speed': 0.0,
            'wind_direction': 0.0
        }
        
        if weather_data:
            conditions['air_density'] = self._calculate_air_density(weather_data)
            conditions['wind_speed'] = weather_data.get('wind_speed', 0.0)
            conditions['wind_direction'] = weather_data.get('wind_direction', 0.0)
            self.logger.debug(f"Weather: density={conditions['air_density']:.3f}, "
                            f"wind={conditions['wind_speed']:.1f}m/s @ {conditions['wind_direction']:.0f}°")
        
        return conditions
    
    @staticmethod
    def _calculate_air_density(weather_data):
        """Calculate air density from weather data using ideal gas law.
        Avoids instantiating WeatherService (and a new HTTP session) on every segment.
        """
        temp = weather_data.get('temperature', 20.0)
        pressure = weather_data.get('pressure', 1013.25)
        if temp is None:
            temp = 20.0
        if pressure is None:
            pressure = 1013.25
        temp_kelvin = temp + 273.15
        return (pressure * 100.0) / (287.05 * temp_kelvin)
    
    # ============ Power and Force Calculation Methods ============
    
    def _calculate_power_components(self, averaged_data, env_conditions, segment_df):
        """Calculate all power components"""
        speeds = averaged_data['speeds']
        powers = averaged_data['powers']
        accelerations = averaged_data['accelerations']
        slopes = averaged_data['slopes']
        
        # Account for drivetrain losses
        effective_powers = powers * self._drivetrain_efficiency
        
        # Calculate individual power components
        rolling_powers = self._calculate_rolling_power(speeds, slopes)
        gradient_powers = self._calculate_gradient_power(speeds, slopes)
        inertial_powers = self._calculate_inertial_power(speeds, accelerations)
        
        # Calculate aerodynamic power
        aero_powers = effective_powers - rolling_powers - gradient_powers - inertial_powers
        aero_powers = np.maximum(aero_powers, 0.0)  # Ensure non-negative
        
        self._log_power_components(rolling_powers, gradient_powers, inertial_powers, aero_powers)
        
        # Calculate wind effects
        wind_effects = self._calculate_wind_effects(
            segment_df, env_conditions['wind_speed'], 
            env_conditions['wind_direction'], speeds.mean()
        )
        
        return {
            'effective_powers': effective_powers,
            'aero_powers': aero_powers,
            'rolling_powers': rolling_powers,
            'gradient_powers': gradient_powers,
            'inertial_powers': inertial_powers,
            'wind_effects': wind_effects
        }
    
    def _calculate_rolling_power(self, speeds, slopes=None):
        """Calculate rolling resistance power"""
        if slopes is None:
            slopes = 0.0
        return (self._total_mass * 9.81 * 
                np.cos(np.radians(slopes)) *
                speeds * self.parameters['rolling_resistance'])
    
    def _calculate_gradient_power(self, speeds, slopes):
        """Calculate gradient power"""
        return (self._total_mass * 9.81 * 
                speeds * np.sin(np.radians(slopes)))
    
    def _calculate_inertial_power(self, speeds, accelerations):
        """Calculate inertial power from acceleration"""
        return self._total_mass * accelerations * speeds
    
    def _log_power_components(self, rolling, gradient, inertial, aero):
        """Log average power components for debugging"""
        self.logger.debug(f"Power components (averaged) - Aero: {aero.mean():.1f}W, "
                        f"Rolling: {rolling.mean():.1f}W, "
                        f"Gradient: {gradient.mean():.1f}W, "
                        f"Inertial: {inertial.mean():.1f}W")
    
    def _calculate_yaw_angle(self, wind_speed, wind_angle_deg, v_ground, effective_wind):
        """Calculate yaw angle (crosswind angle from rider's perspective).
        
        Yaw is the angle at which the rider perceives the wind coming from relative
        to their forward direction. For example:
        - 0° = pure headwind (straight ahead)
        - 90° = pure crosswind from right
        - -90° = pure crosswind from left
        
        Args:
            wind_speed: magnitude of wind in m/s
            wind_angle_deg: angle of wind relative to direction of travel (-180 to 180)
            v_ground: ground speed in m/s
            effective_wind: wind component along travel direction (includes wind_effect_factor)
        
        Returns:
            yaw angle in degrees
        """
        if wind_speed <= 0:
            return 0.0
        
        wind_angle_rad = math.radians(wind_angle_deg)
        
        # Forward velocity from rider perspective (relative to air)
        v_forward = v_ground + effective_wind
        if v_forward < 0.1:
            v_forward = 0.1  # Avoid division issues
        
        # Crosswind velocity: perpendicular component of actual wind
        # effective_wind already includes wind_effect_factor, so we need to recover true wind speed
        wind_effect_factor = self.parameters.get('wind_effect_factor', 1.0)
        
        if abs(math.cos(wind_angle_rad)) < 0.01:  # Near ±90°
            wind_speed_eff = abs(effective_wind) / wind_effect_factor if wind_effect_factor > 0 else wind_speed
        else:
            wind_speed_eff = effective_wind / (math.cos(wind_angle_rad) * wind_effect_factor)
        
        # Crosswind component from the original wind
        v_cross = wind_speed_eff * math.sin(wind_angle_rad)
        
        # Yaw angle
        yaw_rad = math.atan2(v_cross, v_forward)
        yaw_deg = math.degrees(yaw_rad)
        
        return yaw_deg
    
    # ============ Wind Effects Calculation ============
    
    def _calculate_wind_effects(self, segment_df, wind_speed, wind_direction, bike_speed):
        """Calculate wind effects on air speed"""
        if wind_speed <= 0:
            return {'effective_wind': 0.0, 'air_speed': bike_speed, 'wind_angle': 0.0}
        
        # Try to calculate from coordinates
        wind_effects = self._calculate_wind_from_coordinates(
            segment_df, wind_speed, wind_direction, bike_speed
        )
        
        if wind_effects:
            return wind_effects
        
        # Fallback calculation
        return self._calculate_wind_fallback(wind_speed, bike_speed)
    
    def _calculate_wind_from_coordinates(self, segment_df, wind_speed, wind_direction, bike_speed):
        """Calculate wind effects using GPS coordinates"""
        if not self._has_valid_coordinates(segment_df):
            return None
        
        try:
            segment_direction = self._calculate_segment_direction(segment_df)
            if segment_direction is None:
                return None
            
            wind_angle_rad = math.radians(wind_direction - segment_direction)
            wind_angle_deg = math.degrees(wind_angle_rad)
            wind_angle_deg = (wind_angle_deg + 180) % 360 - 180  # Normalize to -180 to 180
            effective_wind = wind_speed * math.cos(wind_angle_rad) * self.parameters['wind_effect_factor']

            if bike_speed is None:
                bike_speed = 0.0
            if not isinstance(effective_wind, (int, float)) or not math.isfinite(effective_wind):
                self.logger.debug("effective_wind invalid, defaulting to 0")
                effective_wind = 0.0

            air_speed = bike_speed + effective_wind
            if air_speed < 0.1:
                self.logger.debug(f"air_speed {air_speed:.2f} m/s below minimum (strong tailwind?), clamping to 0.1")
                air_speed = 0.1

            self.logger.debug(f"Wind: rider={segment_direction:.1f}°, wind={wind_direction:.1f}°, "
                            f"angle={wind_angle_deg:.1f}°, effective={effective_wind:.2f}m/s")
            
            return {
                'effective_wind': effective_wind,
                'air_speed': air_speed,
                'wind_angle': wind_angle_deg,
                'wind_speed': wind_speed
            }
        
        except Exception as e:
            self.logger.warning(f"Error calculating wind effects: {e}")
            return None
    
    def _has_valid_coordinates(self, segment_df):
        """Check if segment has valid GPS coordinates"""
        return ('latitude' in segment_df.columns and 'longitude' in segment_df.columns and 
                len(segment_df) > 1)
    
    def _calculate_segment_direction(self, segment_df):
        """Calculate the direction of travel for the segment using proper bearing calculation"""
        valid_coords = segment_df.dropna(subset=['latitude', 'longitude'])
        if len(valid_coords) < 2:
            return None
        
        lat1, lon1 = valid_coords['latitude'].iloc[0], valid_coords['longitude'].iloc[0]
        lat2, lon2 = valid_coords['latitude'].iloc[-1], valid_coords['longitude'].iloc[-1]
        
        # Check for no movement
        if abs(lat2 - lat1) < 1e-6 and abs(lon2 - lon1) < 1e-6:
            return None
        
        # Convert to radians
        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        lon1_rad = math.radians(lon1)
        lon2_rad = math.radians(lon2)
        
        # Calculate bearing using proper formula
        dlon = lon2_rad - lon1_rad
        
        x = math.sin(dlon) * math.cos(lat2_rad)
        y = (math.cos(lat1_rad) * math.sin(lat2_rad) - 
            math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(dlon))
        
        bearing_rad = math.atan2(x, y)
        bearing_deg = (math.degrees(bearing_rad) + 360) % 360
        
        return bearing_deg
    
    def _calculate_wind_fallback(self, wind_speed, bike_speed):
        """Fallback wind calculation when coordinates unavailable"""
        effective_wind = wind_speed * 0.3  # Conservative partial headwind

        if bike_speed is None:
            bike_speed = 0.0

        air_speed = max(bike_speed + effective_wind, 0.1)
        self.logger.debug("Using fallback wind calculation")
        
        return {
            'effective_wind': effective_wind,
            'air_speed': air_speed,
            'wind_angle': 0.0,
            'wind_speed': wind_speed
        }
    
    # ============ CdA Calculation Methods ============
    
    def _calculate_cda_values(self, averaged_data, power_components, env_conditions):
        """Calculate individual CdA values for each data point"""
        cda_values = []
        speeds = averaged_data['speeds']
        aero_powers = power_components['aero_powers']
        wind_effects = power_components['wind_effects']
        air_density = env_conditions['air_density']
        
        for i in range(len(speeds)):
            cda = self._calculate_single_cda(
                speeds.iloc[i], aero_powers.iloc[i], 
                wind_effects['effective_wind'], air_density
            )
            
            if cda is not None:
                cda_values.append(cda)
                self.logger.debug(f"Point {i}: speed={speeds.iloc[i]:.2f}, "
                                f"aero_power={aero_powers.iloc[i]:.1f}, cda={cda:.4f}")
        
        self.logger.info(f"Calculated {len(cda_values)} valid CdA values from {len(speeds)} points")
        return cda_values
    
    def _calculate_estimated_power(self, segment_df, fixed_cda, weather_data=None):
        """
        Estimate the power required for a segment given a fixed CdA.
        """
        self.logger.debug(f"Estimating power for segment with fixed CdA: {fixed_cda:.4f}")

        # Prepare rolling averaged data
        averaged_data = self._prepare_averaged_data(segment_df)
        if averaged_data is None:
            self.logger.warning("Could not prepare averaged data for power estimation.")
            return None

        # Get environmental conditions
        env_conditions = self._get_environmental_conditions(weather_data)

        speeds = averaged_data['speeds']
        accelerations = averaged_data['accelerations']
        slopes = averaged_data['slopes']

        # Calculate individual force components
        rolling_force = self._total_mass * 9.81 * self.parameters['rolling_resistance'] * np.cos(np.radians(slopes))
        gradient_force = self._total_mass * 9.81 * np.sin(np.radians(slopes))
        inertial_force = self._total_mass * accelerations

        # Calculate wind effects
        wind_effects = self._calculate_wind_effects(
            segment_df, env_conditions['wind_speed'],
            env_conditions['wind_direction'], speeds.mean()
        )

        air_speed = speeds + wind_effects['effective_wind']

        # Calculate aerodynamic force using the fixed CdA
        aero_force = 0.5 * env_conditions['air_density'] * fixed_cda * air_speed**2

        # Total force
        total_force = rolling_force + gradient_force + inertial_force + aero_force

        # Estimated power (Force * Speed) and account for drivetrain efficiency
        estimated_power = (total_force * speeds) / self._drivetrain_efficiency

        self.logger.debug(f"Estimated average power: {estimated_power.mean():.1f}W")
        return estimated_power.mean()

    def _calculate_single_cda(self, speed, aero_power, effective_wind, air_density):
        """Calculate CdA for a single data point"""
        if speed is None:
            self.logger.debug("speed was None in _calculate_single_cda, defaulting to 0")
            speed = 0.0
        if effective_wind is None:
            self.logger.debug("effective_wind was None in _calculate_single_cda, defaulting to 0")
            effective_wind = 0.0

        air_speed = speed + effective_wind

        if speed <= 0.1 or abs(air_speed) <= 0.1:  # Avoid division by near-zero
            return None

        try:
            # P_aero = 0.5 * rho * CdA * v_air^2 * v_ground  →  CdA = 2*P / (rho * v_air^2 * v_g)
            cda = (2 * aero_power) / (air_density * air_speed ** 2 * speed)
            return cda if 0.0 <= cda <= 1.0 else None  # Reasonable range check
        except Exception:
            return None
    
    def _filter_cda_outliers(self, cda_values):
        """Remove outliers using IQR method"""
        if len(cda_values) <= 2:
            return cda_values
        
        cda_array = np.array(cda_values)
        q75, q25 = np.percentile(cda_array, [75, 25])
        iqr = q75 - q25
        lower_bound = q25 - 1.5 * iqr
        upper_bound = q75 + 1.5 * iqr
        
        filtered_cda = cda_array[(cda_array >= lower_bound) & (cda_array <= upper_bound)]
        
        if len(filtered_cda) > 0:
            outliers_removed = len(cda_array) - len(filtered_cda)
            self.logger.debug(f"Removed {outliers_removed} outliers")
            return filtered_cda.tolist()
        
        return cda_values
    
    # ============ Result Compilation Methods ============
    
    def _compile_segment_result(self, segment_df, averaged_data, cda_values, power_components, env_conditions):
        """Compile final result for a segment"""
        # Filter outliers and calculate statistics
        filtered_cda = self._filter_cda_outliers(cda_values)
        final_cda = np.mean(filtered_cda)
        cda_std = np.std(filtered_cda) if len(filtered_cda) > 1 else 0
        
        # Calculate segment averages
        segment_averages = self._calculate_segment_averages(averaged_data, power_components)
        
        # Calculate quality metrics
        residual = self._calculate_residual(
            segment_averages, final_cda, env_conditions, power_components['wind_effects']
        )
        
        self.logger.info(f"Segment analysis complete - CdA: {final_cda:.4f} ± {cda_std:.4f}")
        
        wind_effects = power_components['wind_effects']
        v_ground = segment_averages['speed']
        v_wind   = wind_effects['effective_wind']
        v_air    = wind_effects['air_speed']

        return {
            'cda': final_cda,
            'cda_std': cda_std,
            'cda_points': len(filtered_cda),
            'residual': residual,
            'duration': self._get_segment_duration(segment_df),
            'distance': self._get_segment_distance(segment_df),
            'air_density': env_conditions['air_density'],
            # Three explicit speeds
            'v_ground':     v_ground,
            'v_wind':       v_wind,
            'v_air':        v_air,
            # Legacy aliases
            'effective_wind': v_wind,
            'air_speed':    v_air,
            **segment_averages,
            **wind_effects,
        }
    
    def _calculate_segment_averages(self, averaged_data, power_components):
        """Calculate average values for the segment"""
        return {
            'speed': averaged_data['speeds'].mean(),
            'power': averaged_data['powers'].mean(),
            'effective_power': averaged_data['powers'].mean() * self._drivetrain_efficiency,
            'acceleration': averaged_data['accelerations'].mean(),
            'slope': averaged_data['slopes'].mean(),
            'aero_power': power_components['aero_powers'].mean(),
            'rolling_power': power_components['rolling_powers'].mean(),
            'gradient_power': power_components['gradient_powers'].mean(),
            'inertial_power': power_components['inertial_powers'].mean()
        }
    
    def _calculate_residual(self, averages, cda, env_conditions, wind_effects):
        """Calculate power residual for quality assessment"""
        # P_aero = 0.5 * rho * CdA * v_air^2 * v_ground  (consistent with _calculate_single_cda)
        calculated_power = (
            averages['rolling_power'] +
            averages['gradient_power'] +
            averages['inertial_power'] +
            0.5 * env_conditions['air_density'] * cda * wind_effects['air_speed'] ** 2 * averages['speed']
        ) / self._drivetrain_efficiency
        
        return abs(calculated_power - averages['power'])
    
    def _get_segment_duration(self, segment_df):
        """Get segment duration in seconds"""
        return (segment_df['timestamp'].iloc[-1] - segment_df['timestamp'].iloc[0]).total_seconds()
    
    def _get_segment_distance(self, segment_df):
        """Get segment distance in meters"""
        return segment_df['distance'].iloc[-1] - segment_df['distance'].iloc[0]
    
    # ============ Analysis Orchestration Methods ============

    def _analyze_segments(self, segments):
        """Analyze all segments and collect results"""
        segment_results = []
        
        for i, segment in enumerate(segments):
            self.logger.info(f"Analyzing segment {i+1}/{len(segments)}")
            
            # Get weather data from segment or cache
            weather_data = None
            if 'weather_data' in segment.columns:
                weather_data = segment['weather_data'].iloc[0]
                if not (pd.notna(weather_data) and isinstance(weather_data, dict)):
                    weather_data = None
            
            if not weather_data and i in self.weather_cache:
                weather_data = self.weather_cache[i]
            
            result = self.calculate_cda_for_segment(segment, weather_data)
            
            if result:
                start_elev = float(segment['altitude'].iloc[0]) if 'altitude' in segment.columns and not segment['altitude'].isna().all() else None
                start_elev_fit = float(segment['altitude_fit'].iloc[0]) if 'altitude_fit' in segment.columns and not segment['altitude_fit'].isna().all() else None
                start_elev_api = float(segment['altitude_api'].iloc[0]) if 'altitude_api' in segment.columns and not segment['altitude_api'].isna().all() else None
                result.update({
                    'segment_id': i,
                    'start_time': segment['timestamp'].iloc[0],
                    'end_time': segment['timestamp'].iloc[-1],
                    'start_elevation': start_elev,
                    'start_elevation_fit': start_elev_fit,
                    'start_elevation_api': start_elev_api,
                })
                if weather_data:
                    result['temperature'] = weather_data.get('temperature')
                    result['pressure'] = weather_data.get('pressure')
                    result['wind_speed'] = weather_data.get('wind_speed')
                    result['wind_direction'] = weather_data.get('wind_direction')
                segment_results.append(result)
            else:
                self.logger.warning(f"Segment {i}: Failed to calculate CdA")
        
        return segment_results
    
    def _store_weather_data(self, segment_id, weather_data):
        """Store weather data for a segment"""
        if weather_data:
            self.weather_cache[segment_id] = weather_data.copy()
    
    def _get_weather_data_for_segment(self, segment, weather_service, segment_id):
        """Get weather data for a specific segment"""
        # Check if weather data is already stored in segment
        if 'weather_data' in segment.columns:
            weather_data = segment['weather_data'].iloc[0]
            if pd.notna(weather_data) and isinstance(weather_data, dict):
                self.logger.debug(f"Segment {segment_id}: Using cached weather data")
                return weather_data
        
        # Check cache
        if segment_id in self.weather_cache:
            self.logger.debug(f"Segment {segment_id}: Using cached weather data")
            return self.weather_cache[segment_id]
        
        # Fetch new weather data
        if not (weather_service and self._has_valid_coordinates(segment)):
            return None
        
        try:
            valid_coords = segment.dropna(subset=['latitude', 'longitude'])
            if len(valid_coords) == 0:
                return None
            
            lat = valid_coords['latitude'].iloc[0]
            lon = valid_coords['longitude'].iloc[0]
            timestamp = valid_coords['timestamp'].iloc[0]
            
            weather_data = weather_service.get_weather_data(lat, lon, timestamp)
            temp = weather_data.get('temperature', 'N/A')
            self.logger.debug(f"Segment {segment_id}: Weather data retrieved - temp={temp}°C")
            
            # Store in cache
            self._store_weather_data(segment_id, weather_data)
            
            return weather_data
            
        except Exception as e:
            self.logger.warning(f"Could not get weather data for segment {segment_id}: {e}")
            return None
        
    def _calculate_wind_angle_coefficients(self, segment_results):
        """Calculate polynomial coefficients for CdA vs wind angle relationship"""
        self.logger.info("Calculating wind angle coefficients")
        
        # Extract wind angles and CdA values from segments with valid wind data
        wind_angles = []
        cda_vals = []
        
        for segment in segment_results:
            if ('wind_angle' in segment and 
                'cda' in segment and 
                segment['wind_angle'] is not None and 
                segment['cda'] is not None):
                wind_angles.append(segment['wind_angle'])
                cda_vals.append(segment['cda'])
        
        if len(wind_angles) < 3:
            self.logger.warning("Insufficient data points for wind angle coefficient calculation")
            return None
        
        for i in range(len(wind_angles)):
            if wind_angles[i] > 180:
                wind_angles[i] -= 360
            if wind_angles[i] < -180:
                wind_angles[i] += 360
        
        try:
            # Fit second-order polynomial: cda = a*angle² + b*angle + c
            coeffs = np.polyfit(wind_angles, cda_vals, 2)
            self.logger.info(f"Wind angle coefficients calculated: {coeffs}")
            
            return coeffs.tolist()

        except Exception as e:
            self.logger.error(f"Error calculating wind angle coefficients: {e}")
            return None
        
    @staticmethod
    def _format_seconds(seconds):
        seconds = int(seconds)
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    def _calculate_weighted_cda_metrics(self, segment_results):
        """Return weighted CdA metrics for all segments and kept subset.

        The kept subset is created by repeatedly removing the segment with the
        largest absolute deviation from the current duration-weighted mean CdA,
        until only ``cda_keep_percent`` of segments remain.
        """
        keep_percent = self.parameters.get('cda_keep_percent')
        if keep_percent is None:
            # Backward-compatibility for older configs that still provide trim
            # values instead of a single keep percentage.
            trim_low = float(self.parameters.get('cda_trim_low_percent', 0.0))
            trim_high = float(self.parameters.get('cda_trim_high_percent', 20.0))
            keep_percent = 100.0 - max(0.0, trim_low) - max(0.0, trim_high)
        keep_percent = float(keep_percent)
        keep_percent = max(1.0, min(100.0, keep_percent))

        # Filter segments with usable CdA values
        valid_segments = [s for s in segment_results if s.get('cda') is not None]
        if not valid_segments:
            return {
                'weighted_cda_all': float('nan'),
                'weighted_cda_kept': float('nan'),
                'keep_percent': keep_percent,
                'kept_segments_used': 0,
            }

        # Weighted CdA across all segments (duration-weighted)
        all_cda = [s['cda'] for s in valid_segments]
        all_weights = [max(float(s.get('duration', 0.0)), 0.0) for s in valid_segments]
        if sum(all_weights) > 0:
            weighted_all = float(np.average(all_cda, weights=all_weights))
        else:
            weighted_all = float(np.mean(all_cda))

        # Iteratively remove the largest absolute outlier from the current
        # duration-weighted mean until the target keep size is reached.
        current_segments = list(valid_segments)
        n = len(current_segments)
        target_keep_count = max(1, int(np.ceil(n * keep_percent / 100.0)))

        while len(current_segments) > target_keep_count:
            current_cda = [s['cda'] for s in current_segments]
            current_weights = [max(float(s.get('duration', 0.0)), 0.0) for s in current_segments]
            if sum(current_weights) > 0:
                center = float(np.average(current_cda, weights=current_weights))
            else:
                center = float(np.mean(current_cda))

            remove_idx = int(np.argmax([abs(s['cda'] - center) for s in current_segments]))
            current_segments.pop(remove_idx)

        kept_cda = [s['cda'] for s in current_segments]
        kept_weights = [max(float(s.get('duration', 0.0)), 0.0) for s in current_segments]
        if sum(kept_weights) > 0:
            weighted_kept = float(np.average(kept_cda, weights=kept_weights))
        else:
            weighted_kept = float(np.mean(kept_cda))

        return {
            'weighted_cda_all': weighted_all,
            'weighted_cda_kept': weighted_kept,
            'keep_percent': keep_percent,
            'kept_segments_used': len(current_segments),
        }


    
    def _calculate_summary(self, segment_results):
        """Calculate summary statistics from segment results"""
        if not segment_results:
            self.logger.warning("No segments to summarize")
            return {}
        
        self.logger.debug(f"Calculating summary from {len(segment_results)} segments")
        
        cda_values = [s['cda'] for s in segment_results]
        weighted_metrics = self._calculate_weighted_cda_metrics(segment_results)
        weighted_cda_all = weighted_metrics['weighted_cda_all']
        weighted_cda_kept = weighted_metrics['weighted_cda_kept']
        
        wind_coefficients = self._calculate_wind_angle_coefficients(segment_results)

        wind_directions = [s['wind_direction'] for s in segment_results if 'wind_direction' in s]

        if wind_directions:
            angles_rad = np.radians(wind_directions)
            sin_sum = np.sum(np.sin(angles_rad))
            cos_sum = np.sum(np.cos(angles_rad))
            avg_wind_direction = np.degrees(np.arctan2(sin_sum, cos_sum))
        else:
            avg_wind_direction = np.nan

        # Pre-compute optional per-segment values in a single pass to avoid multiple iterations
        wind_speeds = [s['wind_speed'] for s in segment_results if 'wind_speed' in s]
        temperatures = [s['temperature'] for s in segment_results if 'temperature' in s]
        pressures = [s['pressure'] for s in segment_results if 'pressure' in s]

        summary = {
            'total_segments': len(segment_results),
            # Primary weighted CdA uses iterative outlier removal
            'weighted_cda': weighted_cda_kept,
            'weighted_cda_all': weighted_cda_all,
            'weighted_cda_kept': weighted_cda_kept,
            'keep_percent': weighted_metrics['keep_percent'],
            'kept_segments_used': weighted_metrics['kept_segments_used'],
            'average_cda': np.mean(cda_values),
            'cda_std': np.std(cda_values),
            'wind_coefficients': wind_coefficients,
            'min_cda': np.min(cda_values),
            'max_cda': np.max(cda_values),
            'total_duration': sum(s['duration'] for s in segment_results),
            'total_distance': sum(s['distance'] for s in segment_results),
            'avg_wind_speed': float(np.mean(wind_speeds)) if wind_speeds else 0.0,
            # Three speed averages across all segments
            'avg_ground_speed':   float(np.mean([s.get('v_ground', s.get('speed', 0))   for s in segment_results])),
            'avg_wind_component': float(np.mean([s.get('v_wind',   s.get('effective_wind', 0)) for s in segment_results])),
            'avg_air_speed':      float(np.mean([s.get('v_air',    s.get('air_speed', 0)) for s in segment_results])),
            'avg_acceleration': np.mean([s['acceleration'] for s in segment_results]),
            'avg_temp': float(np.mean(temperatures)) if temperatures else float('nan'),
            'avg_press': float(np.mean(pressures)) if pressures else float('nan'),
            'avg_wind_direction': avg_wind_direction,
            # Data source tracking
            'elevation_source': self.elevation_source,
            'has_gps_coordinates': False  # Set in analyze_ride from input data
        }
        
        self.logger.debug(
            f"Summary calculated: weighted_all={weighted_cda_all:.4f}, "
            f"weighted_kept={weighted_cda_kept:.4f}, "
            f"keep_percent={weighted_metrics['keep_percent']:.1f}%"
        )
        return summary