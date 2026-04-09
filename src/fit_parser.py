"""FIT file parser for bike ride data"""

import numpy as np
import pandas as pd
from fitparse import FitFile
import logging
from elevation import ElevationService

class FITParser:
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.elevation_source = None  # Track which elevation source was used
    
    def parse_fit_file(self, file_path, use_open_elevation_api=False, elevation_service=None, status_callback=None):
        """
        Parse a FIT file and extract relevant ride data
        
        Args:
            file_path (str): Path to FIT file
            use_open_elevation_api (bool): If True and GPS available, use Open-Elevation API for elevations
            elevation_service: ElevationService instance (will be created if needed and use_open_elevation_api=True)
        
        Returns:
            pandas.DataFrame: DataFrame with ride data including:
                - timestamp
                - latitude (degrees)
                - longitude (degrees)
                - altitude (meters) [from FIT or Open-Elevation API]
                - speed (m/s)
                - power (watts)
                - heart_rate (bpm)
                - cadence (rpm)
                - distance (meters)
        """
        try:
            fitfile = FitFile(file_path)
            records = []
            
            # Extract data from FIT file
            for record in fitfile.get_messages('record'):
                data = {}
                for data_point in record:
                    if data_point.value is not None:
                        data[data_point.name] = data_point.value
                if data:  # Only add non-empty records
                    records.append(data)
            
            # Convert to DataFrame
            df = pd.DataFrame(records)
            
            # Initialize elevation_source to default (will be updated in _process_data)
            self.elevation_source = 'FIT file'
            
            # Create ElevationService if needed
            if use_open_elevation_api and elevation_service is None:
                elevation_service = ElevationService()
            
            # Process and clean data
            df = self._process_data(df, use_open_elevation_api, elevation_service, status_callback=status_callback)
            
            return df
            
        except Exception as e:
            self.logger.error(f"Error parsing FIT file: {e}")
            raise
    
    def _process_data(self, df, use_open_elevation_api=False, elevation_service=None, status_callback=None):
        """Process raw FIT data into usable format
        
        Args:
            df (DataFrame): Raw FIT data
            use_open_elevation_api (bool): If True, fetch elevations from Open-Elevation API
            elevation_service: ElevationService instance for elevation API access
        """
        # Convert units and handle missing data
        if 'timestamp' in df.columns:
            df['timestamp'] = pd.to_datetime(df['timestamp'])
        
        # Convert position from semicircles to degrees
        if 'position_lat' in df.columns:
            df['latitude'] = df['position_lat'] * (180 / 2**31)
        if 'position_long' in df.columns:
            df['longitude'] = df['position_long'] * (180 / 2**31)
        
        # Convert speed from mm/s to m/s if needed
        if 'speed' in df.columns and df['speed'].dtype != 'object':
            if df['speed'].max() > 50:  # Likely in mm/s
                df['speed'] = df['speed'] / 1000
        
        # Preserve original FIT altitude in a separate column for comparison
        if 'altitude' in df.columns:
            df['altitude_fit'] = df['altitude'].copy()
        
        # Try to fetch elevations from Open-Elevation API
        if use_open_elevation_api:
            df = self.apply_open_elevation_to_dataframe(
                df,
                elevation_service=elevation_service,
                status_callback=status_callback,
            )
        else:
            self.elevation_source = 'FIT file'
        
        # Calculate distance if not present
        if 'distance' not in df.columns:
            df['distance'] = self._calculate_distance(df)
        
        # Handle missing values only for non-critical channels.
        # Keep power/speed gaps explicit so dropout periods are not smeared.
        safe_fill_columns = [
            col for col in ['altitude', 'heart_rate', 'cadence', 'distance', 'temperature']
            if col in df.columns
        ]
        if safe_fill_columns:
            df[safe_fill_columns] = df[safe_fill_columns].ffill().bfill()
        
        return df

    def apply_open_elevation_to_dataframe(self, df, elevation_service=None, status_callback=None):
        """Apply Open-Elevation API data onto an existing DataFrame.

        This is used both during FIT parsing and at analysis start in the GUI.
        """
        if 'altitude' in df.columns and 'altitude_fit' not in df.columns:
            df['altitude_fit'] = df['altitude'].copy()

        if 'latitude' not in df.columns or 'longitude' not in df.columns:
            self.elevation_source = 'FIT file (no GPS coordinates)'
            self.logger.info("No GPS columns available for Open-Elevation API, using FIT altitude")
            if status_callback:
                status_callback("Elevation API debug: no GPS columns, using FIT altitude")
            return df

        valid_coords = df[['latitude', 'longitude']].dropna()
        if len(valid_coords) == 0:
            self.elevation_source = 'FIT file (no GPS coordinates)'
            self.logger.info("No valid GPS coordinates for Open-Elevation API, using FIT altitude")
            if status_callback:
                status_callback("Elevation API debug: 0 valid coordinates, using FIT altitude")
            return df

        if elevation_service is None:
            elevation_service = ElevationService()

        try:
            coordinates = list(dict.fromkeys(zip(valid_coords['latitude'], valid_coords['longitude'])))
            if status_callback:
                status_callback(f"Elevation API request: unique_coords={len(coordinates)}")

            elevation_map = elevation_service.get_elevations_batch(
                coordinates,
                status_callback=status_callback,
            )

            if elevation_map:
                def get_elevation(row):
                    key = (row['latitude'], row['longitude'])
                    fit_alt = row.get('altitude_fit', row.get('altitude', np.nan))
                    return elevation_map.get(key, fit_alt)

                df['altitude_api'] = df.apply(get_elevation, axis=1)
                df['altitude'] = df['altitude_api']
                self.elevation_source = 'Open-Elevation API'

                first_key = next(iter(elevation_map)) if elevation_map else None
                if first_key is not None and status_callback:
                    sample_elev = elevation_map.get(first_key)
                    status_callback(
                        "Elevation API response: "
                        f"mapped={len(elevation_map)} sample=({first_key[0]:.6f},{first_key[1]:.6f},{sample_elev})"
                    )
                self.logger.info(f"Using elevations from Open-Elevation API for {len(elevation_map)} unique points")
            else:
                self.elevation_source = 'FIT file (Open-Elevation API failed)'
                self.logger.warning("Open-Elevation API failed, falling back to FIT altitude")
                if status_callback:
                    status_callback("Elevation API response: no elevations returned, using FIT altitude")
        except Exception as e:
            self.elevation_source = 'FIT file (Open-Elevation API error)'
            self.logger.warning(f"Error fetching elevations from Open-Elevation API: {e}, using FIT altitude")
            if status_callback:
                status_callback(f"Elevation API error: {e}")

        return df
    
    def _calculate_distance(self, df):
        """Calculate cumulative distance from GPS coordinates using vectorized haversine formula"""
        if 'latitude' not in df.columns or 'longitude' not in df.columns:
            self.logger.warning("No GPS coordinates available for distance calculation")
            return [0.0] * len(df)

        lat = np.radians(df['latitude'].values.astype(float))
        lon = np.radians(df['longitude'].values.astype(float))

        dlat = np.diff(lat)
        dlon = np.diff(lon)

        # Haversine formula
        a = np.sin(dlat / 2.0) ** 2 + np.cos(lat[:-1]) * np.cos(lat[1:]) * np.sin(dlon / 2.0) ** 2
        a = np.clip(a, 0.0, 1.0)  # Numerical safety clamp
        segment_distances = 6371000.0 * 2.0 * np.arcsin(np.sqrt(a))  # Earth radius in meters

        # Zero out segments where either endpoint has NaN coordinates
        valid = np.isfinite(lat[:-1]) & np.isfinite(lon[:-1]) & np.isfinite(lat[1:]) & np.isfinite(lon[1:])
        segment_distances = np.where(valid, segment_distances, 0.0)

        return np.concatenate([[0.0], np.cumsum(segment_distances)]).tolist()