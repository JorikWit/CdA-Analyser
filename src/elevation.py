"""Elevation data retrieval module"""

import requests
import logging
import json
from config import OPEN_ELEVATION_URL


class ElevationService:
    """Fetch elevation data from Open-Elevation API in batch"""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.session = requests.Session()  # Reuse TCP connection across API calls

    def _fetch_chunk(self, coords_chunk, retry_count=0, max_retries=3, status_callback=None):
        """Fetch one chunk of coordinates from Open-Elevation API with exponential backoff."""
        import time
        locations = [{'latitude': lat, 'longitude': lon} for lat, lon in coords_chunk]
        payload = {'locations': locations}
        self.logger.info(f"Open-Elevation request: chunk_size={len(coords_chunk)}")

        try:
            response = self.session.post(
                OPEN_ELEVATION_URL,
                json=payload,
                timeout=30,
                headers={'Content-Type': 'application/json', 'Accept': 'application/json'}
            )
            response.raise_for_status()

            data = response.json()
            if status_callback:
                raw = json.dumps(data, ensure_ascii=True, separators=(',', ':'))
                status_callback(f"Elevation API raw response: {raw[:3000]}")
            results = data.get('results', [])
            if results:
                sample = results[0]
                self.logger.info(
                    "Open-Elevation response: "
                    f"results={len(results)} sample=(lat={sample.get('latitude')}, "
                    f"lon={sample.get('longitude')}, elev={sample.get('elevation')})"
                )
            else:
                self.logger.warning("Open-Elevation response: results=0")

            # Use the *original* coordinates as keys (not the API echo) because
            # the API may return lower-precision lat/lon that won't match the
            # float keys built from the ride DataFrame, causing all lookups to
            # miss and fall back to FIT altitude.
            elevation_map = {}
            for i, result in enumerate(results):
                elev = result.get('elevation')
                if i < len(coords_chunk) and elev is not None:
                    elevation_map[coords_chunk[i]] = elev
            return elevation_map
        except requests.HTTPError as e:
            if e.response.status_code == 429 and retry_count < max_retries:
                # Rate limited: exponential backoff
                wait_time = (2 ** retry_count) + (retry_count * 0.1)  # 1s, 2s, 4s
                self.logger.info(f"Rate limited (429). Retrying in {wait_time:.1f}s (attempt {retry_count + 1}/{max_retries})")
                time.sleep(wait_time)
                return self._fetch_chunk(coords_chunk, retry_count + 1, max_retries, status_callback=status_callback)
            raise
    
    def get_elevations_batch(self, coordinates, chunk_size=500, status_callback=None):
        """
        Fetch elevations from Open-Elevation API with automatic chunking.

        Args:
            coordinates (list): List of tuples (latitude, longitude)
            chunk_size (int): Coordinates per API request to avoid 413 payload errors

        Returns:
            dict: Maps (lat, lon) tuples to elevation values (meters), or None on total failure
        """
        if not coordinates:
            return {}

        # Deduplicate while preserving order to minimize API calls
        unique_coords = list(dict.fromkeys(coordinates))

        merged_map = {}
        failed_chunks = 0

        for i in range(0, len(unique_coords), chunk_size):
            chunk = unique_coords[i:i + chunk_size]
            try:
                chunk_map = self._fetch_chunk(chunk, status_callback=status_callback)
                merged_map.update(chunk_map)
            except requests.HTTPError as e:
                status = getattr(e.response, 'status_code', None)
                # Retry once with smaller chunks for payload-too-large errors
                if status == 413 and len(chunk) > 50:
                    self.logger.info(f"Chunk too large ({len(chunk)}), retrying with smaller chunks")
                    sub_size = max(50, len(chunk) // 2)
                    for j in range(0, len(chunk), sub_size):
                        sub_chunk = chunk[j:j + sub_size]
                        try:
                            sub_map = self._fetch_chunk(sub_chunk, status_callback=status_callback)
                            merged_map.update(sub_map)
                        except Exception as sub_e:
                            failed_chunks += 1
                            self.logger.warning(f"Failed sub-chunk {j//sub_size + 1} in chunk {i//chunk_size + 1}: {sub_e}")
                elif status == 429:
                    # Rate limited: already handled by _fetch_chunk with exponential backoff
                    failed_chunks += 1
                    self.logger.warning(f"Failed chunk {i//chunk_size + 1} after retries: {e}")
                else:
                    failed_chunks += 1
                    self.logger.warning(f"Failed chunk {i//chunk_size + 1}: {e}")
            except Exception as e:
                failed_chunks += 1
                self.logger.warning(f"Failed chunk {i//chunk_size + 1}: {e}")

        if not merged_map and failed_chunks > 0:
            self.logger.warning("Failed to fetch elevations from Open-Elevation API for all chunks")
            return None

        self.logger.info(
            f"Successfully fetched {len(merged_map)} elevations from Open-Elevation API "
            f"({len(unique_coords)} unique coords, {failed_chunks} failed chunks)"
        )
        return merged_map
