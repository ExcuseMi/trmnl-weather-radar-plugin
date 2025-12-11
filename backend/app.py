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

# Load translations
TRANSLATIONS = {}
try:
    with open('/app/translations.json', 'r', encoding='utf-8') as f:
        TRANSLATIONS = json.load(f)
except Exception as e:
    logger.warning(f"Could not load translations: {e}")
    TRANSLATIONS = {"en": {}}


def translate(key, lang='en'):
    """Get translation for key in specified language, fallback to English"""
    return TRANSLATIONS.get(lang, {}).get(key) or TRANSLATIONS.get('en', {}).get(key, key)


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


async def fetch_nearby_cities(lat, lon, zoom_level=8):
    """
    Fetch actual major cities using Overpass API and get their weather
    Radius scales based on zoom level:
    - zoom 7: ~320km view → 150km radius
    - zoom 8: ~160km view → 100km radius
    - zoom 9: ~80km view → 60km radius
    - zoom 10: ~40km view → 30km radius
    """
    try:
        # Calculate radius based on zoom level
        # Formula: radius = base_distance / (2 ^ (zoom - 8))
        # This ensures cities spread across the visible area at any zoom
        base_radius = 100  # Base radius at zoom 8
        radius_km = int(base_radius * (2 ** (8 - zoom_level)))

        # Clamp radius to reasonable bounds
        radius_km = max(30, min(200, radius_km))

        # Scale minimum distances based on radius
        min_distance_from_center = int(radius_km * 0.20)  # 20% of radius
        min_distance_between_cities = int(radius_km * 0.15)  # 15% of radius

        logger.info(
            f"Zoom {zoom_level}: using radius {radius_km}km, min_from_center {min_distance_from_center}km, min_between {min_distance_between_cities}km")

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Use Overpass API to find cities/towns within radius
            # Query for places with population data or are marked as cities/towns
            overpass_query = f"""
            [out:json][timeout:25];
            (
              node["place"="city"](around:{radius_km * 1000},{lat},{lon});
              node["place"="town"](around:{radius_km * 1000},{lat},{lon});
            );
            out body;
            """

            try:
                response = await client.post(
                    'https://overpass-api.de/api/interpreter',
                    data={'data': overpass_query},
                    headers={'User-Agent': 'TRMNL-Weather-Plugin/1.0'}
                )

                if response.status_code == 200:
                    data = response.json()
                    elements = data.get('elements', [])

                    logger.info(f"Found {len(elements)} cities/towns from Overpass API")

                    # Sort by population if available, otherwise just take first 8
                    cities_with_pop = []
                    for elem in elements:
                        tags = elem.get('tags', {})
                        name = tags.get('name')
                        population = tags.get('population', '0')

                        if name:
                            try:
                                pop_int = int(population.replace(',', '').replace('.', ''))
                            except:
                                pop_int = 0

                            cities_with_pop.append({
                                'name': name,
                                'lat': elem.get('lat'),
                                'lon': elem.get('lon'),
                                'population': pop_int
                            })

                    # Sort by population descending
                    cities_with_pop.sort(key=lambda x: x['population'], reverse=True)

                    # Filter cities to ensure good spread across map
                    # Distances scale with zoom level

                    def distance_km(lat1, lon1, lat2, lon2):
                        """Calculate distance between two points in km"""
                        from math import radians, sin, cos, sqrt, atan2
                        R = 6371  # Earth radius in km

                        lat1_rad = radians(lat1)
                        lat2_rad = radians(lat2)
                        delta_lat = radians(lat2 - lat1)
                        delta_lon = radians(lon2 - lon1)

                        a = sin(delta_lat / 2) ** 2 + cos(lat1_rad) * cos(lat2_rad) * sin(delta_lon / 2) ** 2
                        c = 2 * atan2(sqrt(a), sqrt(1 - a))

                        return R * c

                    selected_cities = []
                    for city in cities_with_pop:
                        # Skip if too close to center
                        dist_from_center = distance_km(lat, lon, city['lat'], city['lon'])
                        if dist_from_center < min_distance_from_center:
                            continue

                        # Skip if too close to already selected cities
                        too_close = False
                        for selected in selected_cities:
                            dist = distance_km(selected['lat'], selected['lon'], city['lat'], city['lon'])
                            if dist < min_distance_between_cities:
                                too_close = True
                                break

                        if not too_close:
                            selected_cities.append(city)

                        # Stop when we have 8 cities
                        if len(selected_cities) >= 8:
                            break

                    logger.info(f"Selected {len(selected_cities)} well-spread cities by population")

                    # Fetch weather for all cities in parallel
                    weather_tasks = []
                    for city in selected_cities:
                        weather_tasks.append(fetch_weather(city['lat'], city['lon']))

                    weather_results = await asyncio.gather(*weather_tasks, return_exceptions=True)

                    # Combine city names with weather data
                    nearby_weather = []
                    for i, result in enumerate(weather_results):
                        if isinstance(result, Exception) or result is None:
                            continue

                        city = selected_cities[i]
                        nearby_weather.append({
                            'lat': round(city['lat'], 2),
                            'lon': round(city['lon'], 2),
                            'name': city['name'],
                            'temperature': result.get('temperature'),
                            'weather_code': result.get('weather_code'),
                            'population': city['population']
                        })

                    logger.info(
                        f"Fetched weather for {len(nearby_weather)} nearby cities: {[c['name'] for c in nearby_weather]}")
                    return nearby_weather

            except Exception as e:
                logger.warning(f"Overpass API failed: {e}, falling back to directional method")
                # Fall back to old method if Overpass fails
                pass

            # Fallback: Use the old directional reverse geocoding method
            return await fetch_nearby_cities_fallback(lat, lon, radius_km)

    except Exception as e:
        logger.error(f"Error fetching nearby cities: {e}")
        return []


async def fetch_nearby_cities_fallback(lat, lon, radius_km):
    """Fallback method using directional reverse geocoding"""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Use Nominatim's reverse geocoding to find nearby places
            # We'll search in 8 directions at ~80km distance to get major cities

            nearby_cities = []
            distance_offset = 0.72  # ~80km (0.72 degrees lat ≈ 80km)

            directions = [
                (0, distance_offset, 'N'),  # North
                (distance_offset, distance_offset, 'NE'),  # Northeast
                (distance_offset, 0, 'E'),  # East
                (distance_offset, -distance_offset, 'SE'),  # Southeast
                (0, -distance_offset, 'S'),  # South
                (-distance_offset, -distance_offset, 'SW'),  # Southwest
                (-distance_offset, 0, 'W'),  # West
                (-distance_offset, distance_offset, 'NW'),  # Northwest
            ]

            # For each direction, do reverse geocoding to find the city/town name
            for offset_lat, offset_lon, direction in directions:
                point_lat = lat + offset_lat
                point_lon = lon + offset_lon

                try:
                    # Reverse geocode to get place name
                    # Use zoom=8 for larger cities (zoom=10 was too granular)
                    geocode_response = await client.get(
                        'https://nominatim.openstreetmap.org/reverse',
                        params={
                            'lat': point_lat,
                            'lon': point_lon,
                            'format': 'json',
                            'zoom': 8,  # Larger area = bigger cities
                            'addressdetails': 1
                        },
                        headers={'User-Agent': 'TRMNL-Weather-Plugin/1.0'}
                    )

                    if geocode_response.status_code == 200:
                        place_data = geocode_response.json()

                        # Check if this is a valid land location (not water)
                        # Nominatim returns error or empty address for water
                        if 'error' in place_data:
                            logger.info(f"Skipping {direction}: over water/no data")
                            continue

                        address = place_data.get('address', {})

                        # Skip if no valid address components (likely water)
                        if not address:
                            logger.info(f"Skipping {direction}: no address data (likely water)")
                            continue

                        # Check for water-related types
                        place_type = place_data.get('type', '')
                        if place_type in ['sea', 'ocean', 'bay', 'water', 'waterway']:
                            logger.info(f"Skipping {direction}: water body detected")
                            continue

                        # Prefer larger administrative divisions
                        place_name = (
                                address.get('city') or
                                address.get('town') or
                                address.get('state') or
                                address.get('county') or
                                address.get('region') or
                                place_data.get('name')
                        )

                        # Skip if we couldn't get a valid name
                        if not place_name or place_name == direction:
                            logger.info(f"Skipping {direction}: no valid place name")
                            continue

                        nearby_cities.append({
                            'lat': point_lat,
                            'lon': point_lon,
                            'name': place_name,
                            'direction': direction
                        })

                    # Rate limit: sleep briefly between requests
                    await asyncio.sleep(0.2)

                except Exception as e:
                    logger.warning(f"Failed to geocode {direction}: {e}")
                    continue

            # Now fetch weather for all cities in parallel
            weather_tasks = []
            for city in nearby_cities:
                weather_tasks.append(fetch_weather(city['lat'], city['lon']))

            weather_results = await asyncio.gather(*weather_tasks, return_exceptions=True)

            # Combine city names with weather data
            nearby_weather = []
            for i, result in enumerate(weather_results):
                if isinstance(result, Exception) or result is None:
                    continue

                city = nearby_cities[i]
                nearby_weather.append({
                    'lat': round(city['lat'], 2),
                    'lon': round(city['lon'], 2),
                    'name': city['name'],
                    'temperature': result.get('temperature'),
                    'weather_code': result.get('weather_code'),
                    'direction': city['direction']
                })

            logger.info(
                f"Fetched weather for {len(nearby_weather)} nearby cities: {[c['name'] for c in nearby_weather]}")
            return nearby_weather

    except Exception as e:
        logger.error(f"Error fetching nearby cities: {e}")
        return []


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
      - lang: language code (optional, default: en)
      - zoom_level: map zoom level (optional, default: 8, range: 1-18)
    """
    address = request.args.get('address', type=str)
    lang = request.args.get('lang', 'en', type=str)
    zoom_level = request.args.get('zoom_level', 8, type=int)

    # Clamp zoom level to valid range
    zoom_level = max(1, min(18, zoom_level))

    if not address:
        return jsonify({'error': translate('error_missing_address', lang)}), 400

    # Geocode address
    geocode_result = await geocode_address(address)
    if not geocode_result:
        return jsonify({'error': f"{translate('error_location_not_found', lang)}: {address}"}), 400

    lat = geocode_result['lat']
    lon = geocode_result['lon']
    location_name = geocode_result['display_name']

    logger.info(f"Using geocoded coordinates: {lat}, {lon} (zoom: {zoom_level})")

    # Fetch weather, forecast, and nearby cities in parallel
    weather_data, forecast_data, nearby_data = await asyncio.gather(
        fetch_weather(lat, lon),
        fetch_forecast(lat, lon),
        fetch_nearby_cities(lat, lon, zoom_level),
        return_exceptions=True
    )

    # Handle exceptions
    if isinstance(weather_data, Exception):
        logger.error(f"Error fetching weather: {weather_data}")
        weather_data = None
    if isinstance(forecast_data, Exception):
        logger.error(f"Error fetching forecast: {forecast_data}")
        forecast_data = None
    if isinstance(nearby_data, Exception):
        logger.error(f"Error fetching nearby cities: {nearby_data}")
        nearby_data = []

    if not weather_data:
        return jsonify({'error': translate('error_fetch_weather', lang)}), 500

    # Combine results
    response = {
        'location': location_name,
        'lat': lat,
        'lon': lon,
        'current': weather_data,
        'forecast': forecast_data.get('hours', []) if forecast_data else [],
        'nearby': nearby_data if nearby_data else []
    }

    return jsonify(response)


async def startup():
    """Startup tasks"""
    global TRMNL_IPS

    logger.info("Starting TRMNL Weather Radar Plugin...")
    logger.info(f"Cache duration: {CACHE_MINUTES} minutes (weather), {FORECAST_CACHE_MINUTES} minutes (forecast)")
    logger.info(f"IP Whitelist enabled: {ENABLE_IP_WHITELIST}")

    # Initialize database
    await init_db()
    logger.info("Database initialized")

    # Fetch TRMNL IPs if whitelist enabled
    if ENABLE_IP_WHITELIST:
        await fetch_trmnl_ips()
        logger.info(f"Loaded {len(TRMNL_IPS)} TRMNL server IPs")

    logger.info("Weather Radar API ready!")


def create_app():
    """Application factory"""
    return app


# Run startup on import (before gunicorn forks workers)
import sys

if 'gunicorn' in sys.argv[0] or __name__ != '__main__':
    asyncio.run(startup())

if __name__ == '__main__':
    # For development only
    asyncio.run(startup())
    app.run(host='0.0.0.0', port=5000, debug=False)