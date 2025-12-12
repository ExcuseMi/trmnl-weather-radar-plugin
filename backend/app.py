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
    """Get translation for key in specified language, supports nested keys with dot notation"""
    if lang not in TRANSLATIONS:
        lang = 'en'

    # Handle nested keys like 'weather_codes.0'
    keys = key.split('.')
    value = TRANSLATIONS.get(lang, TRANSLATIONS['en'])

    for k in keys:
        if isinstance(value, dict):
            value = value.get(k)
            if value is None:
                # Try English fallback
                value = TRANSLATIONS['en']
                for fk in keys:
                    if isinstance(value, dict):
                        value = value.get(fk)
                        if value is None:
                            return key
                    else:
                        return key
                return value if value is not None else key
        else:
            return key

    return value if value is not None else key


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

        # Overpass API cache (for nearby cities)
        await db.execute('''
            CREATE TABLE IF NOT EXISTS overpass_cache (
                lat REAL,
                lon REAL,
                radius_km INTEGER,
                cities_json TEXT,
                cached_at TEXT,
                PRIMARY KEY (lat, lon, radius_km)
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
    Radius scales based on zoom level to fill most of the visible map:
    - zoom 7: ~320km view → 250km radius (fills 78% of map)
    - zoom 8: ~160km view → 125km radius (fills 78% of map)
    - zoom 9: ~80km view → 62km radius (fills 78% of map)
    - zoom 10: ~40km view → 31km radius (fills 78% of map)
    """
    try:
        # Calculate radius based on zoom level
        # Map view distance at each zoom: ~320km / (2^(zoom-7))
        # Use 78% of visible area for city spread
        # Formula: radius = (map_view_distance * 0.78) / 2
        base_radius = 125  # Base radius at zoom 8 (fills ~78% of 160km view)
        radius_km = int(base_radius * (2 ** (8 - zoom_level)))

        # Clamp radius to reasonable bounds
        radius_km = max(30, min(280, radius_km))

        # Scale minimum distances based on radius
        # Reduce from center minimum to allow cities closer (better coverage)
        min_distance_from_center = int(radius_km * 0.12)  # 12% of radius (was 20%)
        min_distance_between_cities = int(radius_km * 0.18)  # 18% of radius (was 15%)

        logger.info(
            f"Zoom {zoom_level}: using radius {radius_km}km, min_from_center {min_distance_from_center}km, min_between {min_distance_between_cities}km")

        # Check cache first (cache for 24 hours since city data doesn't change often)
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                'SELECT cities_json, cached_at FROM overpass_cache WHERE lat = ? AND lon = ? AND radius_km = ?',
                (round(lat, 2), round(lon, 2), radius_km)
            )
            row = await cursor.fetchone()

            if row:
                cached_json, cached_at = row
                cache_time = datetime.fromisoformat(cached_at)
                cache_age = (datetime.now() - cache_time).total_seconds() / 60

                # Use cache if less than 24 hours old (1440 minutes)
                if cache_age < 1440:
                    logger.info(f"Overpass cache hit for ({lat}, {lon}, {radius_km}km), age: {cache_age:.1f}m")
                    cities_with_pop = json.loads(cached_json)

                    # Skip to filtering step
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

                        # Stop when we have enough cities
                        if len(selected_cities) >= 20:
                            break

                    # Fetch weather for cached cities
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
                        f"Fetched weather for {len(nearby_weather)} nearby cities (from cache): {[c['name'] for c in nearby_weather]}")
                    return nearby_weather

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

                    # Cache the Overpass results (24 hour cache)
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute('''
                            INSERT OR REPLACE INTO overpass_cache (lat, lon, radius_km, cities_json, cached_at)
                            VALUES (?, ?, ?, ?, ?)
                        ''', (
                            round(lat, 2),
                            round(lon, 2),
                            radius_km,
                            json.dumps(cities_with_pop),
                            datetime.now().isoformat()
                        ))
                        await db.commit()

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

                        # Stop when we have enough cities
                        if len(selected_cities) >= 20:  # Increased from 8 to 20
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


@app.route('/api/weather-grid', methods=['POST'])
@require_trmnl_ip
async def get_weather_grid():
    """
    Get weather data for multiple locations and calculate optimal map bounds

    Request body:
    {
      "locations": ["Brussels", "Paris", "Amsterdam", "London"],
      "lang": "en" (optional)
    }

    Response:
    {
      "locations": [
        {"name": "Brussels", "lat": 50.85, "lon": 4.35, "temperature": 7, "weather_code": 0},
        ...
      ],
      "map": {
        "center_lat": 51.2,
        "center_lon": 2.5,
        "zoom": 7,
        "bounds": {"north": 52.5, "south": 48.8, "east": 6.1, "west": -0.1}
      }
    }
    """
    try:
        # Log raw request details
        logger.info(f"Weather grid request received")
        logger.info(f"Content-Type: {request.content_type}")
        logger.info(f"Headers: {dict(request.headers)}")

        raw_data = request.data.decode('utf-8') if request.data else 'None'
        logger.info(f"Raw data: {raw_data}")

        # Try to parse JSON
        data = None
        try:
            data = request.get_json(force=True)
            logger.info(f"Parsed JSON data: {data}")
        except Exception as e:
            logger.info(f"Failed to parse as JSON directly: {e}")

            # Try to fix malformed JSON where locations isn't properly quoted
            # Raw: {"locations": [Brussels, Paris], "lang": "en"}
            # Fixed: {"locations": ["Brussels", "Paris"], "lang": "en"}
            try:
                import re
                # Fix the locations array - add quotes around unquoted items
                fixed_data = re.sub(
                    r'\[([^\]]+)\]',  # Find content in brackets
                    lambda m: '[' + ', '.join(f'"{item.strip()}"' for item in m.group(1).split(',')) + ']',
                    raw_data
                )
                logger.info(f"Attempting to parse fixed JSON: {fixed_data}")
                data = json.loads(fixed_data)
                logger.info(f"Successfully parsed fixed JSON: {data}")
            except Exception as e2:
                logger.error(f"Failed to parse fixed JSON: {e2}")
                return jsonify({'error': f'Invalid JSON format. Raw data: {raw_data}'}), 400

        if not data or 'locations' not in data:
            return jsonify({'error': 'Missing required field: locations'}), 400

        locations = data.get('locations', [])
        lang = data.get('lang', 'en')
        temp_unit = data.get('temperature_unit', 'celsius')  # celsius or fahrenheit

        logger.info(f"Locations received: {locations} (type: {type(locations)})")
        logger.info(f"Language: {lang}")
        logger.info(f"Temperature unit: {temp_unit}")

        if not locations or not isinstance(locations, list):
            return jsonify({'error': 'locations must be a non-empty array'}), 400

        if len(locations) > 50:
            return jsonify({'error': 'Maximum 50 locations allowed'}), 400

        logger.info(f"Weather grid request for {len(locations)} locations")

        # Geocode all locations in parallel
        geocode_tasks = [geocode_address(loc) for loc in locations]
        geocode_results = await asyncio.gather(*geocode_tasks, return_exceptions=True)

        # Filter successful geocodes
        valid_locations = []
        for i, result in enumerate(geocode_results):
            if isinstance(result, Exception) or result is None:
                logger.warning(f"Failed to geocode: {locations[i]}")
                continue
            valid_locations.append({
                'name': locations[i],
                'lat': result['lat'],
                'lon': result['lon']
            })

        if not valid_locations:
            return jsonify({'error': 'Could not geocode any locations'}), 400

        logger.info(f"Successfully geocoded {len(valid_locations)}/{len(locations)} locations")

        # Fetch weather for all locations in parallel
        weather_tasks = [fetch_weather(loc['lat'], loc['lon']) for loc in valid_locations]
        weather_results = await asyncio.gather(*weather_tasks, return_exceptions=True)

        # Combine location data with weather
        location_data = []
        for i, weather in enumerate(weather_results):
            if isinstance(weather, Exception) or weather is None:
                continue

            loc = valid_locations[i]
            weather_code = weather.get('weather_code')

            # Get translated weather description
            weather_description = translate(f'weather_codes.{weather_code}', lang) if weather_code is not None else ''

            # Get temperature and convert if needed
            temperature = weather.get('temperature')
            if temperature is not None and temp_unit == 'fahrenheit':
                temperature = (temperature * 9 / 5) + 32

            location_data.append({
                'name': loc['name'],
                'lat': round(loc['lat'], 4),
                'lon': round(loc['lon'], 4),
                'temperature': round(temperature, 1) if temperature is not None else None,
                'temperature_unit': temp_unit,
                'weather_code': weather_code,
                'weather_description': weather_description,
                'humidity': weather.get('humidity'),
                'wind_speed': weather.get('wind_speed'),
                'precipitation': weather.get('precipitation')
            })

        # Calculate optimal map bounds and zoom
        lats = [loc['lat'] for loc in location_data]
        lons = [loc['lon'] for loc in location_data]

        min_lat, max_lat = min(lats), max(lats)
        min_lon, max_lon = min(lons), max(lons)

        # Center point
        center_lat = (min_lat + max_lat) / 2
        center_lon = (min_lon + max_lon) / 2

        # Calculate distance span to determine zoom level
        lat_span = max_lat - min_lat
        lon_span = max_lon - min_lon
        max_span = max(lat_span, lon_span)

        # Estimate zoom level based on span
        # Add 20% padding
        max_span_with_padding = max_span * 1.2

        if max_span_with_padding > 45:
            zoom = 4
        elif max_span_with_padding > 22:
            zoom = 5
        elif max_span_with_padding > 11:
            zoom = 6
        elif max_span_with_padding > 5.5:
            zoom = 7
        elif max_span_with_padding > 2.7:
            zoom = 8
        elif max_span_with_padding > 1.4:
            zoom = 9
        else:
            zoom = 10

        logger.info(f"Generated weather grid for {len(location_data)} locations (zoom: {zoom})")

        response = {
            'locations': location_data,
            'map': {
                'center_lat': round(center_lat, 4),
                'center_lon': round(center_lon, 4),
                'zoom': zoom,
                'bounds': {
                    'north': round(max_lat, 4),
                    'south': round(min_lat, 4),
                    'east': round(max_lon, 4),
                    'west': round(min_lon, 4)
                }
            }
        }

        return jsonify(response)

    except Exception as e:
        logger.error(f"Error in weather grid endpoint: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500


@app.route('/api/forecast', methods=['GET'])
@require_trmnl_ip
async def get_forecast():
    """
    Get weather forecast for a single location (current + daily forecast)

    Query parameters:
    - address: Location address (required)
    - days: Number of forecast days (default: 7, max: 7)
    - lang: Language code (default: en)
    - temp_unit: celsius or fahrenheit (default: celsius)

    Response:
    {
      "location": "Brussels, Belgium",
      "lat": 50.85,
      "lon": 4.35,
      "current": {
        "temperature": 11,
        "weather_code": 0,
        "weather_description": "Clear sky"
      },
      "daily_forecast": [
        {
          "date": "2025-12-13",
          "day_name": "Friday",
          "weather_code": 0,
          "weather_description": "Clear sky",
          "temp_max": 11,
          "temp_min": 5,
          "precipitation_probability": 0
        },
        ...
      ]
    }
    """
    try:
        address = request.args.get('address')
        days = min(int(request.args.get('days', 7)), 7)  # Max 7 days
        lang = request.args.get('lang', 'en')
        temp_unit = request.args.get('temp_unit', 'celsius')

        if not address:
            return jsonify({'error': translate('error_missing_address', lang)}), 400

        logger.info(f"Forecast request for: {address}, days: {days}, lang: {lang}, unit: {temp_unit}")

        # Geocode address
        location = await geocode_address(address)
        if not location:
            return jsonify({'error': translate('error_location_not_found', lang)}), 404

        lat, lon = location['lat'], location['lon']
        location_name = location.get('display_name', address)

        # Fetch weather from Open-Meteo
        async with httpx.AsyncClient() as client:
            # Get current + daily forecast
            url = f"https://api.open-meteo.com/v1/forecast"
            params = {
                'latitude': lat,
                'longitude': lon,
                'current': 'temperature_2m,weather_code,relative_humidity_2m,wind_speed_10m,precipitation',
                'daily': 'weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max',
                'timezone': 'auto',
                'forecast_days': days
            }

            response = await client.get(url, params=params, timeout=10.0)
            response.raise_for_status()
            data = response.json()

        # Process current weather
        current = data.get('current', {})
        current_temp = current.get('temperature_2m')
        current_code = current.get('weather_code')

        # Convert temperature if needed
        if current_temp is not None and temp_unit == 'fahrenheit':
            current_temp = (current_temp * 9 / 5) + 32

        current_weather = {
            'temperature': round(current_temp, 1) if current_temp is not None else None,
            'weather_code': current_code,
            'weather_description': translate(f'weather_codes.{current_code}', lang) if current_code is not None else '',
            'humidity': current.get('relative_humidity_2m'),
            'wind_speed': current.get('wind_speed_10m'),
            'precipitation': current.get('precipitation')
        }

        # Process daily forecast
        daily = data.get('daily', {})
        dates = daily.get('time', [])
        weather_codes = daily.get('weather_code', [])
        temp_max = daily.get('temperature_2m_max', [])
        temp_min = daily.get('temperature_2m_min', [])
        precip_prob = daily.get('precipitation_probability_max', [])

        from datetime import datetime

        daily_forecast = []
        for i in range(len(dates)):
            # Parse date and get day name
            date_obj = datetime.fromisoformat(dates[i])
            day_index = date_obj.weekday()  # 0=Monday, 6=Sunday
            day_names = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
            day_key = day_names[day_index]

            # Get translated day name
            day_name = translate(f'days_short.{day_key}', lang)

            # Get temps and convert if needed
            t_max = temp_max[i] if i < len(temp_max) else None
            t_min = temp_min[i] if i < len(temp_min) else None

            if temp_unit == 'fahrenheit':
                if t_max is not None:
                    t_max = (t_max * 9 / 5) + 32
                if t_min is not None:
                    t_min = (t_min * 9 / 5) + 32

            w_code = weather_codes[i] if i < len(weather_codes) else None

            daily_forecast.append({
                'date': dates[i],
                'day_name': day_name,
                'weather_code': w_code,
                'weather_description': translate(f'weather_codes.{w_code}', lang) if w_code is not None else '',
                'temp_max': round(t_max, 1) if t_max is not None else None,
                'temp_min': round(t_min, 1) if t_min is not None else None,
                'precipitation_probability': precip_prob[i] if i < len(precip_prob) else 0
            })

        result = {
            'location': location_name,
            'lat': round(lat, 4),
            'lon': round(lon, 4),
            'current': current_weather,
            'daily_forecast': daily_forecast
        }

        logger.info(f"Forecast generated for {location_name}: {len(daily_forecast)} days")
        return jsonify(result)

    except Exception as e:
        logger.error(f"Error in forecast endpoint: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500


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