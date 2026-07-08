#!/usr/bin/env python3
"""
Unit tests for utils_weather module
"""

import json
import time
from unittest.mock import Mock, patch

import pytest

from modules.utils_weather import get_weather_code_emoji, get_weather_openmeteo


class TestWeatherCodeEmoji:
    """Test weather code to emoji mapping."""
    
    def test_clear_weather(self):
        """Test clear weather codes."""
        assert get_weather_code_emoji(0) == "☀️"  # Clear
        assert get_weather_code_emoji(1) == "🌤️"  # Mostly Clear
        assert get_weather_code_emoji(2) == "⛅"  # Partly Cloudy
        assert get_weather_code_emoji(3) == "☁️"  # Overcast
    
    def test_fog(self):
        """Test fog codes."""
        assert get_weather_code_emoji(45) == "🌫️"
        assert get_weather_code_emoji(48) == "🌫️"
    
    def test_rain(self):
        """Test rain codes."""
        assert get_weather_code_emoji(61) == "🌧️"  # Rain
        assert get_weather_code_emoji(63) == "🌧️"  # Rain
        assert get_weather_code_emoji(65) == "🌧️"  # Heavy Rain
    
    def test_snow(self):
        """Test snow codes."""
        assert get_weather_code_emoji(71) == "❄️"  # Snow
        assert get_weather_code_emoji(73) == "❄️"  # Snow
        assert get_weather_code_emoji(75) == "❄️"  # Heavy Snow
    
    def test_thunderstorm(self):
        """Test thunderstorm codes."""
        assert get_weather_code_emoji(95) == "⛈️"  # Thunderstorm
        assert get_weather_code_emoji(96) == "⛈️"  # Thunderstorm with Hail
        assert get_weather_code_emoji(99) == "⛈️"  # Severe Thunderstorm
    
    def test_unknown_code(self):
        """Test unknown weather code returns default."""
        assert get_weather_code_emoji(999) == "🌤️"
        assert get_weather_code_emoji(-1) == "🌤️"


class MockPersistence:
    """Mock persistence object for testing."""
    
    def __init__(self):
        self.cache = {}
    
    def get_cached_json(self, cache_key, cache_type):
        key = f"{cache_type}:{cache_key}"
        return self.cache.get(key)
    
    def cache_json(self, cache_key, cache_value, cache_type, cache_hours=24):
        key = f"{cache_type}:{cache_key}"
        self.cache[key] = cache_value


class TestGetWeatherOpenMeteo:
    """Test Open-Meteo weather API integration."""
    
    def test_successful_api_call(self):
        """Test successful API call and caching."""
        persistence = MockPersistence()
        
        # Mock successful API response
        mock_response = Mock()
        mock_response.ok = True
        mock_response.json.return_value = {
            'hourly': {
                'temperature_2m': [20, 21, 22],
                'weather_code': [1, 2, 3],
                'precipitation': [0, 0, 0],
                'precipitation_probability': [10, 15, 20],
                'wind_speed_10m': [5, 6, 7]
            }
        }
        
        with patch('modules.utils_weather.requests.get', return_value=mock_response):
            data, error = get_weather_openmeteo(
                lat=48.8566,
                lon=2.3522,
                persistence=persistence
            )
        
        assert error is None
        assert data is not None
        assert 'hourly' in data
        assert data['hourly']['temperature_2m'] == [20, 21, 22]
    
    def test_cache_hit(self):
        """Test that cached data is returned when fresh."""
        persistence = MockPersistence()
        
        # Pre-populate cache with fresh data
        cache_data = {
            'timestamp': time.time(),
            'data': {
                'hourly': {
                    'temperature_2m': [15, 16, 17],
                    'weather_code': [0, 1, 2]
                }
            }
        }
        persistence.cache_json(
            'weather_48.8566_2.3522',
            cache_data,
            'openmeteo_weather',
            cache_hours=2
        )
        
        # Should return cached data without making API call
        with patch('modules.utils_weather.requests.get') as mock_get:
            data, error = get_weather_openmeteo(
                lat=48.8566,
                lon=2.3522,
                persistence=persistence
            )
            
            # API should not be called
            mock_get.assert_not_called()
        
        assert error is None
        assert data is not None
        assert data['hourly']['temperature_2m'] == [15, 16, 17]
    
    def test_api_error_with_fallback(self):
        """Test fallback to stale cache when API fails."""
        persistence = MockPersistence()
        
        # Pre-populate cache with stale data (2 hours old)
        cache_data = {
            'timestamp': time.time() - 7200,  # 2 hours ago
            'data': {
                'hourly': {
                    'temperature_2m': [10, 11, 12],
                    'weather_code': [61, 63, 65]
                }
            }
        }
        persistence.cache_json(
            'weather_48.8566_2.3522',
            cache_data,
            'openmeteo_weather',
            cache_hours=3
        )
        
        # Mock API error
        mock_response = Mock()
        mock_response.ok = False
        mock_response.status_code = 500
        
        with patch('modules.utils_weather.requests.get', return_value=mock_response):
            data, error = get_weather_openmeteo(
                lat=48.8566,
                lon=2.3522,
                persistence=persistence
            )
        
        # Should return stale cache as fallback
        assert error is None  # No error because fallback worked
        assert data is not None
        assert data['hourly']['temperature_2m'] == [10, 11, 12]
    
    def test_timeout_with_fallback(self):
        """Test fallback to stale cache on timeout."""
        persistence = MockPersistence()
        
        # Pre-populate cache
        cache_data = {
            'timestamp': time.time() - 7200,
            'data': {'hourly': {'temperature_2m': [5, 6, 7]}}
        }
        persistence.cache_json(
            'weather_48.8566_2.3522',
            cache_data,
            'openmeteo_weather',
            cache_hours=3
        )
        
        # Mock timeout
        from requests.exceptions import Timeout
        with patch('modules.utils_weather.requests.get', side_effect=Timeout()):
            data, error = get_weather_openmeteo(
                lat=48.8566,
                lon=2.3522,
                persistence=persistence
            )
        
        # Should return cached data despite timeout
        assert error is None
        assert data is not None
        assert data['hourly']['temperature_2m'] == [5, 6, 7]
    
    def test_no_cache_api_error(self):
        """Test error when API fails and no cache available."""
        persistence = MockPersistence()
        
        # Mock API error with no cache
        mock_response = Mock()
        mock_response.ok = False
        mock_response.status_code = 503
        
        with patch('modules.utils_weather.requests.get', return_value=mock_response):
            data, error = get_weather_openmeteo(
                lat=48.8566,
                lon=2.3522,
                persistence=persistence
            )
        
        assert data is None
        assert error is not None
        assert '503' in error
    
    def test_model_parameter(self):
        """Test that model parameter is included in API call."""
        persistence = MockPersistence()
        
        mock_response = Mock()
        mock_response.ok = True
        mock_response.json.return_value = {'hourly': {}}
        
        with patch('modules.utils_weather.requests.get', return_value=mock_response) as mock_get:
            get_weather_openmeteo(
                lat=48.8566,
                lon=2.3522,
                persistence=persistence,
                model="meteofrance_arome_france_hd"
            )
            
            # Check that model was included in params
            call_args = mock_get.call_args
            params = call_args[1]['params']
            assert params['models'] == "meteofrance_arome_france_hd"
    
    def test_empty_model_parameter(self):
        """Test that empty model parameter is omitted."""
        persistence = MockPersistence()
        
        mock_response = Mock()
        mock_response.ok = True
        mock_response.json.return_value = {'hourly': {}}
        
        with patch('modules.utils_weather.requests.get', return_value=mock_response) as mock_get:
            get_weather_openmeteo(
                lat=48.8566,
                lon=2.3522,
                persistence=persistence,
                model=""  # Empty model should be omitted
            )
            
            # Check that model was NOT included in params
            call_args = mock_get.call_args
            params = call_args[1]['params']
            assert 'models' not in params
