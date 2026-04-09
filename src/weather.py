"""Weather data retrieval module"""

import requests
import pandas as pd
import numpy as np
import json
from datetime import datetime, timedelta
import logging
from config import OPEN_METEO_URL_FORCAST, OPEN_METEO_URL_ARCIVE

class WeatherService:
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.session = requests.Session()  # Reuse TCP connection across API calls

    @staticmethod
    def _to_local_timestamp(timestamp):
        """Return a timezone-naive timestamp interpreted in local wall-clock time."""
        ts = pd.Timestamp(timestamp)
        if ts.tzinfo is not None:
            ts = ts.tz_localize(None)
        return ts

    @staticmethod
    def _closest_index_from_sorted(values, target):
        """Return index of nearest value in sorted 1D numpy array."""
        pos = np.searchsorted(values, target)
        if pos <= 0:
            return 0
        if pos >= len(values):
            return len(values) - 1
        prev_i = pos - 1
        next_i = pos
        if abs(values[next_i] - target) < abs(target - values[prev_i]):
            return next_i
        return prev_i
    
    def get_weather_data(self, latitude, longitude, timestamp, status_callback=None):
        """
        Get weather data for a specific location and time
        
        Args:
            latitude (float): Latitude in degrees
            longitude (float): Longitude in degrees
            timestamp (datetime): Local timestamp
            
        Returns:
            dict: Weather data including temperature, wind speed, etc.
        """
        try:
            ts_local = self._to_local_timestamp(timestamp)

            # Format date for API request
            date_str = ts_local.strftime('%Y-%m-%d')

            # Determine which API endpoint to use based on the timestamp
            # The archive API is for data older than ~1 month.
            one_month_ago = datetime.now().date() - timedelta(days=30)
            if ts_local.date() >= one_month_ago:
                OPEN_METEO_URL = OPEN_METEO_URL_FORCAST
            else:
                OPEN_METEO_URL = OPEN_METEO_URL_ARCIVE

            params = {
                'latitude': latitude,
                'longitude': longitude,
                'hourly': ','.join([
                    'temperature_2m',
                    'wind_speed_10m',
                    'wind_direction_10m',
                    'pressure_msl'
                ]),
                'wind_speed_unit': 'ms',
                'start_date': date_str,
                'end_date': date_str,
                'timezone': 'auto'
            }
            
            response = self.session.get(OPEN_METEO_URL, params=params, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            if status_callback:
                raw = json.dumps(data, ensure_ascii=True, separators=(',', ':'))
                status_callback(f"Weather API raw response: {raw[:3000]}")
            
            # Extract hourly data
            hourly_data = data['hourly']
            timestamps = pd.to_datetime(hourly_data['time'])
            
            # Find closest weather timestamp to our local timestamp.
            # TimedeltaIndex.abs() is not available in all pandas versions.
            delta_seconds = np.array(
                [abs((ts - ts_local).total_seconds()) for ts in timestamps],
                dtype=float,
            )
            closest_idx = int(delta_seconds.argmin())
            
            weather_data = {
                'temperature': hourly_data['temperature_2m'][closest_idx],
                'wind_speed': hourly_data['wind_speed_10m'][closest_idx],
                'wind_direction': hourly_data['wind_direction_10m'][closest_idx],
                'pressure': hourly_data['pressure_msl'][closest_idx] if 'pressure_msl' in hourly_data else 1013.25
            }
            
            return weather_data
            
        except Exception as e:
            self.logger.warning(f"Could not retrieve weather data: {e}")
            # Return default values
            return {
                'temperature': 20.0,  # Celsius
                'wind_speed': 0.0,    # m/s
                'wind_direction': 0.0, # degrees
                'pressure': 1013.25   # hPa
            }

    def prefetch_weather_for_ride(self, ride_df, sample_distance_m=3000.0, status_callback=None):
        """Prefetch weather for a ride using local-time 3km distance sampling.

        Returns a list of weather samples that can be reused for segment analysis.
        """
        required_cols = {'distance', 'latitude', 'longitude', 'timestamp'}
        if not required_cols.issubset(set(ride_df.columns)):
            return {
                'samples': [],
                'sample_count': 0,
                'grouped_request_count': 0,
            }

        frame = ride_df.dropna(subset=['distance', 'latitude', 'longitude', 'timestamp']).copy()
        if frame.empty:
            return {
                'samples': [],
                'sample_count': 0,
                'grouped_request_count': 0,
            }

        frame = frame.sort_values('distance')
        distances = frame['distance'].to_numpy(dtype=float)
        if len(distances) == 0:
            return {
                'samples': [],
                'sample_count': 0,
                'grouped_request_count': 0,
            }

        max_distance = float(np.nanmax(distances))
        if max_distance <= 0:
            sample_distances = np.array([0.0], dtype=float)
        else:
            step = max(float(sample_distance_m), 1.0)
            sample_distances = np.arange(0.0, max_distance + step, step, dtype=float)
            if sample_distances[-1] > max_distance:
                sample_distances[-1] = max_distance

        samples = []
        grouped_points = {}

        for sample_distance in sample_distances:
            idx = self._closest_index_from_sorted(distances, sample_distance)
            row = frame.iloc[idx]
            ts_local = self._to_local_timestamp(row['timestamp'])
            lat = float(row['latitude'])
            lon = float(row['longitude'])
            key = (round(lat, 3), round(lon, 3), ts_local.date(), int(ts_local.hour))

            sample = {
                'distance': float(row['distance']),
                'latitude': lat,
                'longitude': lon,
                'timestamp': ts_local,
                'group_key': key,
                'weather_data': None,
            }
            samples.append(sample)

            if key not in grouped_points:
                grouped_points[key] = (lat, lon, ts_local)

        if status_callback:
            status_callback(
                f"Weather API request: sample_points={len(samples)}, grouped_calls={len(grouped_points)}"
            )

        grouped_results = {}
        for key, (lat, lon, ts_local) in grouped_points.items():
            grouped_results[key] = self.get_weather_data(
                lat,
                lon,
                ts_local,
                status_callback=status_callback,
            )

        for sample in samples:
            sample['weather_data'] = grouped_results.get(sample['group_key'])

        return {
            'samples': samples,
            'sample_count': len(samples),
            'grouped_request_count': len(grouped_points),
        }
    
    def calculate_air_density(self, temperature, pressure, humidity=50):
        """
        Calculate air density based on temperature and pressure
        
        Args:
            temperature (float): Temperature in Celsius
            pressure (float): Pressure in hPa
            humidity (float): Relative humidity in %
            
        Returns:
            float: Air density in kg/m³
        """
        if temperature is None:
            self.logger.warning("temperature was None in calculate_air_density, defaulting to 20.0°C")
            temperature = 20.0

        if pressure is None:
            self.logger.warning("pressure was None in calculate_air_density, defaulting to 1013.25 hPa")
            pressure = 1013.25

        if humidity is None:
            self.logger.warning("humidity was None in calculate_air_density, defaulting to 50%")
            humidity = 50
        
        # Convert to Kelvin
        temp_kelvin = temperature + 273.15
        
        # Simplified air density calculation
        # More accurate would require humidity data
        air_density = (pressure * 100) / (287.05 * temp_kelvin)  # kg/m³
        
        return air_density