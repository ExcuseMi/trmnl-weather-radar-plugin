import os
import asyncio
import aiosqlite
import httpx
import json
import logging
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from functools import wraps

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Configuration
CACHE_MINUTES = 15
FORECAST_CACHE_MINUTES = 60
DB_PATH = '/data/weather_cache.db'
ENABLE_IP_WHITELIST = os.getenv('ENABLE_IP_WHITELIST', 'false').lower() == 'true'

# TRMNL server IPs (fetched from https://usetrmnl.com/api/ips on startup)
TRMNL_IPS = set()


def get_client_ip():
    """Get the real client IP, accounting for proxies and Cloudflare"""
    # Check Cloudflare headers first
    cf_connecting_ip = request.headers.get('CF-Connecting-IP')
    if cf_connecting_ip:
        return cf_connecting_ip

    # Fall back to X-Forwarded-For
    x_forwarded_for = request.headers.get('X-Forwarded-For')
    if x_forwarded_for:
        return x_forwarded_for.split(',')[0].strip()

    # Fall back to direct connection
    return request.remote_addr


def require_trmnl_ip(f):
    """Decorator to require requests from TRMNL IPs"""

    @wraps(f)
    async def decorated_function(*args, **kwargs):
        if not ENABLE_IP_WHITELIST:
            return await f(*args, **kwargs)

        client_ip = get_client_ip()

        if client_ip not in TRMNL_IPS:
            logger.warning(f"Blocked request from non-TRMNL IP: {client_ip}")
            return jsonify({'error': 'Access denied'}), 403

        return await f(*args, **kwargs)

    return decorated_function


async def fetch_trmnl_ips():
    """Fetch TRMNL server IPs from official API"""
    global TRMNL_IPS
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get('https://usetrmnl.com/api/ips')
            if response.status_code == 200:
                data = response.json()
                TRMNL_IPS = set(data.get('ips', []))
                logger.info(f"Loaded {len(TRMNL_IPS)} TRMNL IPs")
            else:
                logger.error(f"Failed to fetch TRMNL IPs: {response.status_code}")
    except Exception as e:
        logger.error(f"Error fetching TRMNL IPs: {e}")


async def init_db():
    """Initialize SQLite database"""
    async with aiosqlite.connect(DB_PATH) as db:
        # Geocoding cache
        await db.execute('''
            CREATE TABLE IF NOT EXISTS geocode_cache (
                address TEXT PRIMARY KEY,
                lat REAL,
                lon REAL,
                display_name TEXT,
                cached_at TEXT
            )
        ''')

        # Weather cache
        await db.execute('''
            CREATE TABLE IF NOT EXISTS weather_cache (
                lat REAL,
                lon REAL,
                weather_json TEXT,
                cached_at TEXT,
                PRIMARY KEY (lat, lon)
            )
        ''')

        # Forecast cache
        await db.execute('''
            CREATE TABLE IF NOT EXISTS forecast_cache (
                lat REAL,
                lon REAL,
                forecast_json TEXT,
                cached_at TEXT,
                PRIMARY KEY (lat, lon)
            )
        ''')

        await db.commit()


async def geocode_address(address):
    """Convert address to coordinates using Nominatim (OpenStreetMap)"""
    # Check cache first
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            'SELECT lat, lon, display_name FROM geocode_cache WHERE address = ?',
            (address.lower(),)
        )
        row = await cursor.fetchone()
        if row:
            logger.info(f"Geocoding cache hit for: {address}")
            return {
                'lat': row[0],
                'lon': row[1],
                'display_name': row[2]
            }

    # Fetch from Nominatim
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                'https://nominatim.openstreetmap.org/search',
                params={
                    'q': address,
                    'format': 'json',
                    'limit': 1
                },
                headers={'User-Agent': 'TRMNL-Weather-Plugin/1.0'}
            )

            if response.status_code != 200:
                logger.error(f"Nominatim error: {response.status_code}")
                return None

            data = response.json()
            if not data:
                logger.info(f"No results for address: {address}")
                return None

            result = data[0]
            lat = float(result['lat'])
            lon = float(result['lon'])
            display_name = result['display_name']

            # Cache the result
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    'INSERT OR REPLACE INTO geocode_cache (address, lat, lon, display_name, cached_at) VALUES (?, ?, ?, ?, ?)',
                    (address.lower(), lat, lon, display_name, datetime.now().isoformat())
                )
                await db.commit()

            logger.info(f"Geocoded '{address}' to ({lat}, {lon})")
            return {
                'lat': lat,
                'lon': lon,
                'display_name': display_name
            }

    except Exception as e:
        logger.error(f"Geocoding error: {e}")
        return None


async def fetch_weather(lat, lon):
    """Fetch current weather from Open-Meteo"""
    # Round coordinates for better cache hits
    lat_rounded = round(lat, 2)
    lon_rounded = round(lon, 2)

    # Check cache first
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            'SELECT weather_json, cached_at FROM weather_cache WHERE lat = ? AND lon = ?',
            (lat_rounded, lon_rounded)
        )
        row = await cursor.fetchone()

        if row:
            weather_json, cached_at = row
            cached_time = datetime.fromisoformat(cached_at)
            age = (datetime.now() - cached_time).total_seconds() / 60  # age in minutes

            if age < CACHE_MINUTES:
                logger.info(f"Weather cache hit for ({lat_rounded}, {lon_rounded}), age: {age:.1f}m")
                return json.loads(weather_json)
            else:
                logger.info(f"Weather cache expired for ({lat_rounded}, {lon_rounded}), age: {age:.1f}m")

    # Fetch from Open-Meteo
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                'https://api.open-meteo.com/v1/forecast',
                params={
                    'latitude': lat,
                    'longitude': lon,
                    'current': 'temperature_2m,relative_humidity_2m,precipitation,weather_code,cloud_cover,wind_speed_10m,wind_direction_10m',
                    'hourly': 'temperature_2m,precipitation_probability,weather_code',
                    'forecast_days': 1,
                    'timezone': 'auto'
                }
            )

            if response.status_code != 200:
                logger.error(f"Open-Meteo error: {response.status_code}")
                return None

            data = response.json()

            # Extract current conditions
            current = data.get('current', {})

            result = {
                'temperature': current.get('temperature_2m'),
                'humidity': current.get('relative_humidity_2m'),
                'precipitation': current.get('precipitation', 0),
                'weather_code': current.get('weather_code'),
                'cloud_cover': current.get('cloud_cover'),
                'wind_speed': current.get('wind_speed_10m'),
                'wind_direction': current.get('wind_direction_10m'),
                'time': current.get('time'),
                'lat': lat_rounded,
                'lon': lon_rounded
            }

            # Cache the result
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    'INSERT OR REPLACE INTO weather_cache (lat, lon, weather_json, cached_at) VALUES (?, ?, ?, ?)',
                    (lat_rounded, lon_rounded, json.dumps(result), datetime.now().isoformat())
                )
                await db.commit()

            logger.info(f"Fetched weather for ({lat}, {lon}): {result['temperature']}°C")
            return result

    except Exception as e:
        logger.error(f"Error fetching weather: {e}")
        return None


async def fetch_forecast(lat, lon):
    """Fetch hourly forecast from Open-Meteo"""
    # Round coordinates for better cache hits
    lat_rounded = round(lat, 2)
    lon_rounded = round(lon, 2)

    # Check cache first
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            'SELECT forecast_json, cached_at FROM forecast_cache WHERE lat = ? AND lon = ?',
            (lat_rounded, lon_rounded)
        )
        row = await cursor.fetchone()

        if row:
            forecast_json, cached_at = row
            cached_time = datetime.fromisoformat(cached_at)
            age = (datetime.now() - cached_time).total_seconds() / 60  # age in minutes

            if age < FORECAST_CACHE_MINUTES:
                logger.info(f"Forecast cache hit for ({lat_rounded}, {lon_rounded}), age: {age:.1f}m")
                return json.loads(forecast_json)
            else:
                logger.info(f"Forecast cache expired for ({lat_rounded}, {lon_rounded}), age: {age:.1f}m")

    # Fetch from Open-Meteo
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                'https://api.open-meteo.com/v1/forecast',
                params={
                    'latitude': lat,
                    'longitude': lon,
                    'hourly': 'temperature_2m,precipitation_probability,precipitation,weather_code',
                    'forecast_days': 2,
                    'timezone': 'auto'
                }
            )

            if response.status_code != 200:
                logger.error(f"Open-Meteo forecast error: {response.status_code}")
                return None

            data = response.json()
            hourly = data.get('hourly', {})

            # Get next 24 hours
            times = hourly.get('time', [])[:24]
            temps = hourly.get('temperature_2m', [])[:24]
            precip_prob = hourly.get('precipitation_probability', [])[:24]
            precip = hourly.get('precipitation', [])[:24]
            weather_codes = hourly.get('weather_code', [])[:24]

            result = {
                'hours': [
                    {
                        'time': times[i],
                        'temperature': temps[i],
                        'precipitation_probability': precip_prob[i],
                        'precipitation': precip[i],
                        'weather_code': weather_codes[i]
                    }
                    for i in range(len(times))
                ]
            }

            # Cache the result
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    'INSERT OR REPLACE INTO forecast_cache (lat, lon, forecast_json, cached_at) VALUES (?, ?, ?, ?)',
                    (lat_rounded, lon_rounded, json.dumps(result), datetime.now().isoformat())
                )
                await db.commit()

            logger.info(f"Fetched forecast for ({lat}, {lon}): {len(times)} hours")
            return result

    except Exception as e:
        logger.error(f"Error fetching forecast: {e}")
        return None


@app.route('/health')
def health():
    """Health check endpoint"""
    return jsonify({'status': 'healthy', 'timestamp': datetime.now().isoformat()})


@app.route('/api/weather')
@require_trmnl_ip
async def get_weather():
    """
    Get weather data for a location
    Query params:
      - address: location address
    """
    address = request.args.get('address', type=str)

    if not address:
        return jsonify({'error': 'Missing required parameter: address'}), 400

    # Geocode address
    geocode_result = await geocode_address(address)
    if not geocode_result:
        return jsonify({'error': f'Could not find location for address: {address}'}), 400

    lat = geocode_result['lat']
    lon = geocode_result['lon']
    location_name = geocode_result['display_name']

    logger.info(f"Using geocoded coordinates: {lat}, {lon}")

    # Fetch weather and forecast in parallel
    weather_data, forecast_data = await asyncio.gather(
        fetch_weather(lat, lon),
        fetch_forecast(lat, lon),
        return_exceptions=True
    )

    # Handle exceptions
    if isinstance(weather_data, Exception):
        logger.error(f"Error fetching weather: {weather_data}")
        weather_data = None
    if isinstance(forecast_data, Exception):
        logger.error(f"Error fetching forecast: {forecast_data}")
        forecast_data = None

    if not weather_data:
        return jsonify({'error': 'Failed to fetch weather data'}), 500

    # Combine results
    response = {
        'location': location_name,
        'lat': lat,
        'lon': lon,
        'current': weather_data,
        'forecast': forecast_data.get('hours', []) if forecast_data else []
    }

    return jsonify(response)


def create_app():
    """Application factory"""
    # Run startup tasks
    asyncio.run(startup())
    return app


if __name__ == '__main__':
    # For development only
    asyncio.run(startup())
    app.run(host='0.0.0.0', port=5000, debug=False)