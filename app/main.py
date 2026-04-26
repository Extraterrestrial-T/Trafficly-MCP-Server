import os
import sys
import json
import logging
from typing import List, Optional
from contextlib import asynccontextmanager
from app.services.upstash_redis import UpstashRedis
from cryptography.fernet import Fernet
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastmcp import FastMCP
from fastmcp.dependencies import CurrentContext
from fastmcp.server.context import Context
from fastmcp.server.auth.providers.clerk import ClerkProvider
from fastmcp.apps import AppConfig, ResourceCSP
from fastmcp.tools import ToolResult
from key_value.aio.stores.redis import RedisStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from key_value.aio.wrappers.prefix_collections import PrefixCollectionsWrapper
from mcp.types import PromptMessage, TextContent
import redis.asyncio as aioredis
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential
import hashlib

load_dotenv()

# ─── Logging ────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("trafficly")

# ─── App services ───────────────────────────────────────────────────────────

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.services.map_service import Map_client

my_maps_client = Map_client(os.getenv("GOOGLE_MAPS_API_KEY"))

# ─── Redis ──────────────────────────────────────────────────────────────────

upstash_redis = UpstashRedis(
    url=os.getenv("UPSTASH_REDIS_URL"),
    encryption_key=os.getenv("FASTMCP_ENCRYPTION_KEY"),
)

# ─── Auth ───────────────────────────────────────────────────────────────────

CLERK_DOMAIN   = os.environ["CLERK_DOMAIN"]
MCP_SERVER_URL = os.environ["MCP_SERVER_URL"]

auth = ClerkProvider(
    domain=CLERK_DOMAIN,
    client_id=os.environ["CLERK_CLIENT_ID"],
    client_secret=os.environ["CLERK_CLIENT_SECRET"],
    base_url=MCP_SERVER_URL,
    client_storage=upstash_redis.oauth_store,
)

# ─── Lifespan ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(server):
    await upstash_redis.base_redis_client.initialize()
    print("✅ Redis client initialized")
    yield
    await my_maps_client.client.aclose()
    await upstash_redis.base_redis_client.aclose()
    print("✅ Resources cleaned up, shutting down")

# ─── MCP server ─────────────────────────────────────────────────────────────

mcp = FastMCP("trafficly", lifespan=lifespan, auth=auth)

# ─── FastAPI wrapper ──────────────────────────────────────────────────────────

mcp_app = mcp.http_app(path="/mcp")
app = FastAPI(lifespan=mcp_app.lifespan)

@app.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource():
    return JSONResponse({
        "resource": MCP_SERVER_URL,
        "authorization_servers": [f"https://{CLERK_DOMAIN}"],
    })

app.mount("/", mcp_app)


# ─── Map HTML builder ─────────────────────────────────────────────────────────
# This is a module-level cache — the HTML is built once per show_route_map call
# and stored here so the resource endpoint can serve it.
_map_html_cache: str = ""

def _build_map_html(
    encoded_polyline: str,
    start_address: str,
    end_address: str,
    origin_lat: float,
    origin_lng: float,
    dest_lat: float,
    dest_lng: float,
    waypoints: list,
    steps: list,
    distance_km: float,
    duration_min: int,
    detail_level: str,
) -> str:
    waypoints_js = json.dumps(waypoints)
    steps_js     = json.dumps(steps)
    detail_js    = json.dumps(detail_level)

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Trafficly</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{
    font-family: 'DM Sans', 'Segoe UI', sans-serif;
    background: #0f1117;
    color: #e2e8f0;
    height: 100vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }}
  .header {{
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 12px 16px;
    background: #1a1d2e;
    border-bottom: 1px solid #2d3148;
    flex-shrink: 0;
  }}
  .logo {{ font-size: 18px; font-weight: 700; color: #60a5fa; letter-spacing: -0.5px; }}
  .route-label {{ font-size: 13px; color: #94a3b8; flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
  .stats {{ display: flex; gap: 16px; flex-shrink:0; }}
  .stat {{ text-align:center; }}
  .stat-value {{ font-size: 15px; font-weight: 700; color: #f1f5f9; }}
  .stat-label {{ font-size: 10px; color: #64748b; text-transform: uppercase; letter-spacing: 0.5px; }}
  .body {{ display: flex; flex: 1; overflow: hidden; }}
  #map {{ flex: 1; min-width: 0; }}
  .steps-panel {{
    width: 280px;
    flex-shrink: 0;
    background: #1a1d2e;
    border-left: 1px solid #2d3148;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }}
  .steps-header {{
    padding: 12px 14px;
    font-size: 11px;
    font-weight: 600;
    color: #64748b;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    border-bottom: 1px solid #2d3148;
    flex-shrink: 0;
  }}
  .steps-list {{ overflow-y: auto; flex: 1; padding: 8px 0; }}
  .steps-list::-webkit-scrollbar {{ width: 4px; }}
  .steps-list::-webkit-scrollbar-track {{ background: transparent; }}
  .steps-list::-webkit-scrollbar-thumb {{ background: #2d3148; border-radius: 2px; }}
  .step {{
    display: flex; gap: 10px; align-items: flex-start;
    padding: 10px 14px; border-bottom: 1px solid #1e2235;
    transition: background 0.15s;
  }}
  .step:hover {{ background: #202336; }}
  .step-num {{
    min-width: 22px; height: 22px; border-radius: 50%;
    background: #2d3148; color: #94a3b8; font-size: 10px; font-weight: 700;
    display: flex; align-items: center; justify-content: center;
    flex-shrink: 0; margin-top: 1px;
  }}
  .step-num.origin {{ background: #1d4ed8; color: #fff; }}
  .step-num.dest   {{ background: #dc2626; color: #fff; }}
  .step-body {{ flex:1; min-width:0; }}
  .step-instruction {{ font-size: 12.5px; color: #cbd5e1; line-height: 1.4; word-break: break-word; }}
  .step-dist {{ font-size: 11px; color: #475569; margin-top: 3px; }}
  .leaflet-tile-pane {{ filter: brightness(0.85) saturate(0.8); }}
  .leaflet-control-attribution {{ font-size: 9px !important; background: rgba(0,0,0,0.5) !important; color: #64748b !important; }}
  .leaflet-control-attribution a {{ color: #475569 !important; }}
</style>
</head>
<body>
<div class="header">
  <span class="logo">⚡ Trafficly</span>
  <span class="route-label">{start_address} → {end_address}</span>
  <div class="stats">
    <div class="stat">
      <div class="stat-value">{distance_km} km</div>
      <div class="stat-label">Distance</div>
    </div>
    <div class="stat">
      <div class="stat-value">{duration_min} min</div>
      <div class="stat-label">Est. time</div>
    </div>
  </div>
</div>
<div class="body">
  <div id="map"></div>
  <div class="steps-panel">
    <div class="steps-header">Directions</div>
    <div class="steps-list" id="steps-list"></div>
  </div>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script type="module">
import {{ App }} from "https://unpkg.com/@modelcontextprotocol/ext-apps@0.4.0/app-with-deps";

const mcpApp = new App({{ name: "Trafficly Map", version: "1.0.0" }});

const ENCODED_POLYLINE = {json.dumps(encoded_polyline)};
const ORIGIN    = {{ lat: {origin_lat}, lng: {origin_lng} }};
const DEST      = {{ lat: {dest_lat},   lng: {dest_lng}   }};
const WAYPOINTS = {waypoints_js};
const STEPS     = {steps_js};
const DETAIL    = {detail_js};

function decodePolyline(encoded) {{
  const points = [];
  let index = 0, lat = 0, lng = 0;
  while (index < encoded.length) {{
    let b, shift = 0, result = 0;
    do {{ b = encoded.charCodeAt(index++) - 63; result |= (b & 0x1f) << shift; shift += 5; }} while (b >= 0x20);
    lat += (result & 1) ? ~(result >> 1) : result >> 1;
    shift = 0; result = 0;
    do {{ b = encoded.charCodeAt(index++) - 63; result |= (b & 0x1f) << shift; shift += 5; }} while (b >= 0x20);
    lng += (result & 1) ? ~(result >> 1) : result >> 1;
    points.push([lat / 1e5, lng / 1e5]);
  }}
  return points;
}}

const map = L.map('map', {{ zoomControl: true, attributionControl: true }});
L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
  attribution: '© <a href="https://carto.com/">CARTO</a>',
  subdomains: 'abcd', maxZoom: 19,
}}).addTo(map);

let routeLine;
if (ENCODED_POLYLINE) {{
  const coords = decodePolyline(ENCODED_POLYLINE);
  routeLine = L.polyline(coords, {{ color: '#3b82f6', weight: 5, opacity: 0.9, lineCap: 'round', lineJoin: 'round' }}).addTo(map);
  map.fitBounds(routeLine.getBounds(), {{ padding: [32, 32] }});
}} else {{
  const coords = [ORIGIN, ...WAYPOINTS.map(w => ({{lat: w.latitude, lng: w.longitude}})), DEST];
  routeLine = L.polyline(coords.map(c => [c.lat, c.lng]), {{ color: '#3b82f6', weight: 4, opacity: 0.8, dashArray: '8 6' }}).addTo(map);
  map.fitBounds(routeLine.getBounds(), {{ padding: [32, 32] }});
}}

function makeMarker(label, bgColor) {{
  return L.divIcon({{
    className: '',
    html: `<div style="width:28px;height:28px;border-radius:50%;background:${{bgColor}};color:#fff;font-size:11px;font-weight:700;display:flex;align-items:center;justify-content:center;border:2px solid rgba(255,255,255,0.3);box-shadow:0 2px 8px rgba(0,0,0,0.5);">${{label}}</div>`,
    iconSize: [28, 28], iconAnchor: [14, 14],
  }});
}}

L.marker([ORIGIN.lat, ORIGIN.lng], {{ icon: makeMarker('A', '#1d4ed8') }}).addTo(map).bindPopup(`<b>Start</b><br/>{start_address}`);
WAYPOINTS.forEach((wp, i) => {{
  L.marker([wp.latitude, wp.longitude], {{ icon: makeMarker(i + 2, '#7c3aed') }}).addTo(map).bindPopup(`<b>Stop ${{i + 2}}</b>`);
}});
L.marker([DEST.lat, DEST.lng], {{ icon: makeMarker('B', '#dc2626') }}).addTo(map).bindPopup(`<b>Destination</b><br/>{end_address}`);

const panel = document.getElementById('steps-list');
const displaySteps = DETAIL === 'detailed'
  ? STEPS
  : STEPS.filter(s => s.maneuver && !['DEPART','ARRIVE',''].includes(s.maneuver)).slice(0, 8);

panel.innerHTML += `<div class="step"><div class="step-num origin">A</div><div class="step-body"><div class="step-instruction">{start_address}</div><div class="step-dist">Start</div></div></div>`;

displaySteps.forEach((step, i) => {{
  const distText = step.distance_m > 1000 ? (step.distance_m / 1000).toFixed(1) + ' km' : step.distance_m + ' m';
  panel.innerHTML += `<div class="step"><div class="step-num">${{i + 1}}</div><div class="step-body"><div class="step-instruction">${{step.instruction}}</div><div class="step-dist">${{distText}}</div></div></div>`;
}});

panel.innerHTML += `<div class="step"><div class="step-num dest">B</div><div class="step-body"><div class="step-instruction">{end_address}</div><div class="step-dist">Destination</div></div></div>`;

await mcpApp.connect();
</script>
</body>
</html>"""


# ─── UI resource — serves the map HTML ───────────────────────────────────────

VIEW_URI = "ui://trafficly/map.html"

@mcp.resource(
    VIEW_URI,
    app=AppConfig(
        csp=ResourceCSP(
            resource_domains=[
                "https://unpkg.com",
                "https://basemaps.cartocdn.com",
                "https://a.basemaps.cartocdn.com",
                "https://b.basemaps.cartocdn.com",
                "https://c.basemaps.cartocdn.com",
                "https://d.basemaps.cartocdn.com",
            ],
        )
    ),
)
def map_view() -> str:
    """Serves the current route map HTML."""
    return _map_html_cache


# ─── Tools ───────────────────────────────────────────────────────────────────

@mcp.tool()
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=5))
async def get_route_info(
    start_address: str,
    end_address: str,
    intermediate_stops: Optional[List[str]] = None,
    departure_time: Optional[str] = "now",
    detail_level: Optional[str] = "summary",
    ctx: Context = CurrentContext(),
):
    """Calculate the optimal route between two addresses with optional intermediate stops.

    After fetching the route, ALWAYS call show_route_map with the same arguments
    plus the route_data returned here. This renders an interactive map for the user.

    Args:
        start_address: Starting point (e.g. "Victoria Island, Lagos")
        end_address: Destination (e.g. "Ikeja, Lagos")
        intermediate_stops: Optional ordered list of stops between start and end
        departure_time: "now" or a time like "2:30PM"
        detail_level: "summary" for overview, "detailed" for turn-by-turn
    Returns:
        Route data including polyline, steps, distance, duration. Pass this
        directly to show_route_map to render the interactive map.
    """
    key = "route:" + hashlib.md5(
        f"{start_address.lower().strip()}|{end_address.lower().strip()}|"
        f"{departure_time}|{','.join(intermediate_stops or []).lower().strip()}".encode()
    ).hexdigest()

    cached_route = await upstash_redis.cache_store.get(key)
    if cached_route:
        logger.info(f"[TOOL] get_route_info | {start_address} → {end_address} (cached)")
        route_data = json.loads(cached_route)
    else:
        logger.info(f"[TOOL] get_route_info | {start_address} → {end_address}")

        geocode_a = await my_maps_client.get_geocode(start_address)
        geocode_b = await my_maps_client.get_geocode(end_address)

        stops = intermediate_stops or []
        for i, stop in enumerate(stops):
            stops[i] = await my_maps_client.get_geocode(stop)

        route_data = await my_maps_client.calculate_route(
            geocode_a, geocode_b,
            stops=stops,
            departure_time=departure_time,
        )
        await upstash_redis.cache_store.set(key, json.dumps(route_data), ex=3600)
        logger.info(f"[TOOL] get_route_info success | routes={len(route_data.get('routes', []))}")

    if detail_level == "detailed":
        guidance = ("Present detailed turn-by-turn directions: maneuvers, street names, "
                    "distances per step, grouped by leg if stops exist.")
    else:
        guidance = ("Give a conversational summary mentioning only major roads or landmarks. "
                    "Skip granular steps. Be friendly and concise.")

    return {
        "route_data": route_data,
        "guidance_prompt": guidance,
        "next_step": "Call show_route_map with start_address, end_address, route_data, and detail_level to display the interactive map.",
    }


@mcp.tool(app=AppConfig(resource_uri=VIEW_URI))
def show_route_map(
    start_address: str,
    end_address: str,
    route_data: dict,
    detail_level: str = "summary",
) -> str:
    """
    Display an interactive Leaflet map with the route drawn along actual roads.
    Call this immediately after get_route_info using its route_data output.

    Args:
        start_address: Starting location label
        end_address: Destination label
        route_data: The route_data dict returned by get_route_info
        detail_level: "summary" or "detailed"
    """
    global _map_html_cache

    routes   = route_data.get("routes", [{}])
    best     = routes[0] if routes else {}
    legs     = best.get("legs", [{}])
    first    = legs[0] if legs else {}
    last_leg = legs[-1] if legs else {}

    dist_m   = best.get("distanceMeters", 0)
    dist_km  = round(dist_m / 1000, 1) if dist_m else 0

    dur_raw  = best.get("duration", "0s")
    dur_sec  = int(dur_raw.replace("s", "") or 0) if isinstance(dur_raw, str) else int(dur_raw)
    dur_min  = max(1, round(dur_sec / 60))

    encoded_polyline = best.get("polyline", {}).get("encodedPolyline", "")

    origin_latlng = first.get("startLocation", {}).get("latLng", {})
    dest_latlng   = last_leg.get("endLocation", {}).get("latLng", {})
    origin_lat = origin_latlng.get("latitude", 0)
    origin_lng = origin_latlng.get("longitude", 0)
    dest_lat   = dest_latlng.get("latitude", 0)
    dest_lng   = dest_latlng.get("longitude", 0)

    waypoints = [
        leg.get("startLocation", {}).get("latLng", {})
        for leg in legs[1:]
    ]

    raw_steps = first.get("steps", [])
    steps = []
    for s in raw_steps:
        nav = s.get("navigationInstruction", {})
        steps.append({
            "instruction": nav.get("instructions", "Continue"),
            "distance_m":  s.get("distanceMeters", 0),
            "maneuver":    nav.get("maneuver", ""),
        })

    # Build the HTML and cache it so the resource endpoint can serve it
    _map_html_cache = _build_map_html(
        encoded_polyline=encoded_polyline,
        start_address=start_address,
        end_address=end_address,
        origin_lat=origin_lat,
        origin_lng=origin_lng,
        dest_lat=dest_lat,
        dest_lng=dest_lng,
        waypoints=waypoints,
        steps=steps,
        distance_km=dist_km,
        duration_min=dur_min,
        detail_level=detail_level,
    )

    return f"Route map ready: {start_address} → {end_address} ({dist_km} km, {dur_min} min)"


# ─── Prompts ─────────────────────────────────────────────────────────────────

@mcp.prompt()
def navigation_prompt(
    start: str,
    end: str,
    detail_level: str = "summary",
    departure_time: str = "now",
    stops: str = "",
) -> str:
    """
    Generate a navigation prompt for Trafficly.

    Args:
        start: Starting address or location name.
        end: Destination address or location name.
        detail_level: 'summary' or 'detailed'.
        departure_time: e.g. 'now' or '2:30PM'.
        stops: Comma-separated intermediate stops.
    """
    stops_list = [s.strip() for s in stops.split(",") if s.strip()] if stops else []
    stops_text = f" with stops at {', '.join(stops_list)}" if stops_list else ""
    stops_arg  = json.dumps(stops_list)

    prompt_text = f"""
You are a navigation assistant called Trafficly.

The user wants to travel from '{start}' to '{end}'{stops_text}, departing at {departure_time}.

## Step 1 — Fetch the route
Call get_route_info with EXACTLY these arguments:
- start_address: "{start}"
- end_address: "{end}"
- intermediate_stops: {stops_arg}
- departure_time: "{departure_time}"
- detail_level: "{detail_level}"

Do NOT proceed until you have the tool response.

## Step 2 — Display the map
Immediately call show_route_map with:
- start_address: "{start}"
- end_address: "{end}"
- route_data: <the route_data field from step 1>
- detail_level: "{detail_level}"

## Step 3 — Describe the route
Use ONLY data from the tool response. Never guess road names.
Follow the guidance_prompt field from get_route_info for formatting style.
""".strip()

    logger.info(f"[PROMPT] navigation_prompt | {start} → {end} stops={stops_list}")
    return prompt_text