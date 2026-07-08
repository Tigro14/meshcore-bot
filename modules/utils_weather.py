#!/usr/bin/env python3
"""
Weather utilities for MeshCore Bot
Provides Open-Meteo API integration with SQLite caching
"""

import json
import logging
import time
from typing import Any, Optional, Tuple

import requests


logger = logging.getLogger(__name__)


def get_weather_code_emoji(code: int) -> str:
    """Convert WMO weather code to emoji.
    
    Args:
        code: WMO weather code.
        
    Returns:
        str: Weather emoji.
    """
    emoji_map = {
        0: "☀️",      # Clear
        1: "🌤️",     # Mostly Clear
        2: "⛅",     # Partly Cloudy
        3: "☁️",      # Overcast
        45: "🌫️",    # Fog
        48: "🌫️",    # Fog
        51: "🌦️",    # Drizzle
        53: "🌦️",    # Drizzle
        55: "🌧️",    # Heavy Drizzle
        56: "🌧️",    # Freezing Drizzle
        57: "🌧️",    # Freezing Drizzle
        61: "🌧️",    # Rain
        63: "🌧️",    # Rain
        65: "🌧️",    # Heavy Rain
        66: "🌧️",    # Freezing Rain
        67: "🌧️",    # Freezing Rain
        71: "❄️",     # Snow
        73: "❄️",     # Snow
        75: "❄️",     # Heavy Snow
        77: "❄️",     # Snow Grains
        80: "🌦️",    # Showers
        81: "🌦️",    # Showers
        82: "🌧️",    # Heavy Showers
        85: "🌨️",    # Snow Showers
        86: "🌨️",    # Snow Showers
        95: "⛈️",     # Thunderstorm
        96: "⛈️",     # Thunderstorm with Hail
        99: "⛈️"      # Severe Thunderstorm
    }
    
    return emoji_map.get(code, "🌤️")


def get_weather_openmeteo(
    lat: float,
    lon: float,
    persistence: Any,
    config: Optional[Any] = None,
    forecast_hours: int = 48,
    model: str = "meteofrance_arome_france_hd",
    timezone: str = "Europe/Paris"
) -> Tuple[Optional[dict], Optional[str]]:
    """Get weather forecast from Open-Meteo API with SQLite caching.
    
    Uses a two-tier caching strategy:
    - Fresh data (< 5 minutes): Return immediately
    - Stale data (5 min - 1 hour): Return cached data, trigger background refresh
    - Very stale data (> 1 hour): Force refresh
    
    Args:
        lat: Latitude coordinate.
        lon: Longitude coordinate.
        persistence: DBManager instance for caching.
        config: Optional config parser for additional settings.
        forecast_hours: Number of forecast hours (default: 48).
        model: Weather model to use (default: meteofrance_arome_france_hd).
        timezone: Timezone for forecast (default: Europe/Paris).
        
    Returns:
        Tuple[Optional[dict], Optional[str]]: Weather data dict and error message.
        Returns (data, None) on success, (None, error_msg) on failure.
    """
    # Create cache key from coordinates
    cache_key = f"weather_{lat:.4f}_{lon:.4f}"
    cache_type = "openmeteo_weather"
    
    # Check cache first
    now = time.time()
    cached_data = persistence.get_cached_json(cache_key, cache_type)
    
    if cached_data and 'timestamp' in cached_data:
        cache_age = now - cached_data['timestamp']
        
        # Fresh data (< 5 minutes)
        if cache_age < 300:  # 5 minutes
            logger.debug(f"Using fresh cached weather data (age: {cache_age:.0f}s)")
            return cached_data.get('data'), None
        
        # Stale data (5-60 minutes) - return it but we'll refresh in background
        # For now, just return stale data since background refresh is complex
        if cache_age < 3600:  # 1 hour
            logger.debug(f"Using stale cached weather data (age: {cache_age:.0f}s)")
            return cached_data.get('data'), None
    
    # Fetch fresh data from API
    url = "https://api.open-meteo.com/v1/forecast"
    
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m,precipitation,precipitation_probability,wind_speed_10m,weather_code",
        "timezone": timezone,
        "forecast_hours": forecast_hours,
    }
    
    # Add model if specified (empty string means auto-select)
    if model:
        params["models"] = model
    
    try:
        response = requests.get(url, params=params, timeout=10)
        
        if not response.ok:
            error_msg = f"Open-Meteo API error: {response.status_code}"
            logger.warning(error_msg)
            
            # Return stale cache if available as fallback
            if cached_data and 'data' in cached_data:
                logger.info("Falling back to stale cached data due to API error")
                return cached_data['data'], None
            
            return None, error_msg
        
        data = response.json()
        
        # Cache the result with timestamp
        cache_entry = {
            'timestamp': now,
            'data': data
        }
        
        # Cache for 2 hours (data will be considered stale after 1 hour)
        persistence.cache_json(cache_key, cache_entry, cache_type, cache_hours=2)
        
        logger.debug(f"Fetched and cached fresh weather data for ({lat:.4f}, {lon:.4f})")
        return data, None
        
    except requests.exceptions.Timeout:
        error_msg = "Open-Meteo API timeout"
        logger.warning(error_msg)
        
        # Return stale cache if available as fallback
        if cached_data and 'data' in cached_data:
            logger.info("Falling back to stale cached data due to timeout")
            return cached_data['data'], None
        
        return None, error_msg
        
    except requests.exceptions.RequestException as e:
        error_msg = f"Open-Meteo API request failed: {e}"
        logger.warning(error_msg)
        
        # Return stale cache if available as fallback
        if cached_data and 'data' in cached_data:
            logger.info("Falling back to stale cached data due to request error")
            return cached_data['data'], None
        
        return None, error_msg
        
    except Exception as e:
        error_msg = f"Unexpected error fetching weather: {e}"
        logger.error(error_msg)
        
        # Return stale cache if available as fallback
        if cached_data and 'data' in cached_data:
            logger.info("Falling back to stale cached data due to unexpected error")
            return cached_data['data'], None
        
        return None, error_msg
