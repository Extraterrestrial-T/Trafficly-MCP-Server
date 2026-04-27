import os
import sys
import json
import logging
from typing import List, Optional
from contextlib import asynccontextmanager
from app.services.upstash_redis import UpstashRedis
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastmcp import FastMCP
from fastmcp.dependencies import CurrentContext
from fastmcp.server.context import Context
from fastmcp.server.auth.providers.clerk import ClerkProvider
from fastmcp.apps import AppConfig, ResourceCSP
from fastmcp.tools import ToolResult
from mcp import types
import hashlib
from tenacity import retry, stop_after_attempt, wait_exponential

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

# ─── UI resource URI ──────────────────────────────────────────────────────────

VIEW_URI = "ui://trafficly/map"


# ─── Static map resource ──────────────────────────────────────────────────────
# This is served ONCE — it is completely static HTML.
# Data flows in via the MCP Apps postMessage bridge (ontoolresult).
# No f-string injection. Works on Claude, ChatGPT, and any MCP Apps host.

@mcp.resource(
    VIEW_URI,
    app=AppConfig(
        csp=ResourceCSP(
            resource_domains=[
                "https://unpkg.com",
                "https://a.basemaps.cartocdn.com",
                "https://b.basemaps.cartocdn.com",
                "https://c.basemaps.cartocdn.com",
                "https://d.basemaps.cartocdn.com",
                "https://basemaps.cartocdn.com",
            ],
        )
    ),
)
def map_view() -> str:
    return r"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"/>
    <title>Trafficly Map</title>
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body, html { height: 100%; width: 100%; background: #0f1117; color: #e2e8f0; font-family: sans-serif; overflow: hidden; }
        #app-container { display: flex; flex-direction: column; height: 100vh; width: 100vw; }
        #header { height: 50px; background: #1a1d2e; display: none; align-items: center; padding: 0 15px; border-bottom: 1px solid #2d3148; justify-content: space-between; }
        #map { flex: 1; width: 100%; background: #111; }
        .loading-overlay { position: absolute; inset: 0; background: #0f1117; display: flex; align-items: center; justify-content: center; z-index: 1000; flex-direction: column; gap: 10px; }
        .spinner { width: 30px; height: 30px; border: 3px solid #2d3148; border-top-color: #60a5fa; border-radius: 50%; animation: spin 1s linear infinite; }
        @keyframes spin { to { transform: rotate(360deg); } }
        .stat-text { font-size: 12px; font-weight: bold; }
    </style>
</head>
<body>
    <div id="loading" class="loading-overlay">
        <div class="spinner"></div>
        <div style="color: #64748b; font-size: 12px;">Initializing Map...</div>
    </div>

    <div id="app-container">
        <div id="header">
            <span style="color: #60a5fa; font-weight: bold;">⚡ Trafficly</span>
            <div id="stats" class="stat-text"></div>
        </div>
        <div id="map"></div>
    </div>

    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script>
        let mapInstance = null;

        function decodePolyline(encoded) {
            let pts = [], idx = 0, lat = 0, lng = 0;
            while (idx < encoded.length) {
                let b, shift = 0, result = 0;
                do { b = encoded.charCodeAt(idx++) - 63; result |= (b & 0x1f) << shift; shift += 5; } while (b >= 0x20);
                lat += (result & 1) ? ~(result >> 1) : result >> 1;
                shift = 0; result = 0;
                do { b = encoded.charCodeAt(idx++) - 63; result |= (b & 0x1f) << shift; shift += 5; } while (b >= 0x20);
                lng += (result & 1) ? ~(result >> 1) : result >> 1;
                pts.push([lat / 1e5, lng / 1e5]);
            }
            return pts;
        }

        function initMap(data) {
            // Guard against Leaflet not being loaded yet
            if (typeof L === 'undefined') {
                setTimeout(() => initMap(data), 100);
                return;
            }

            document.getElementById('loading').style.display = 'none';
            document.getElementById('header').style.display = 'flex';
            document.getElementById('stats').innerText = `${data.distance_km}km • ${data.duration_min}mins`;

            if (mapInstance) mapInstance.remove();

            mapInstance = L.map('map', { zoomControl: false });
            
            L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
                attribution: '&copy; CARTO'
            }).addTo(mapInstance);

            const coords = data.encoded_polyline ? decodePolyline(data.encoded_polyline) : [[data.origin_lat, data.origin_lng], [data.dest_lat, data.dest_lng]];
            const route = L.polyline(coords, { color: '#3b82f6', weight: 5, opacity: 0.8 }).addTo(mapInstance);
            
            L.circleMarker([data.origin_lat, data.origin_lng], { color: '#1d4ed8', radius: 6, fillOpacity: 1 }).addTo(mapInstance);
            L.circleMarker([data.dest_lat, data.dest_lng], { color: '#dc2626', radius: 6, fillOpacity: 1 }).addTo(mapInstance);

            mapInstance.fitBounds(route.getBounds(), { padding: [30, 30] });

            // CRITICAL: Force Leaflet to recalculate container size inside the sandbox
            setTimeout(() => { mapInstance.invalidateSize(); }, 300);
        }

        // --- MCP BRIDGE ---
        window.addEventListener("message", (event) => {
            const msg = event.data;
            if (msg.method === "ui/notifications/tool-result") {
                const content = msg.params.result.content.find(c => c.type === 'text');
                if (content) {
                    try {
                        initMap(JSON.parse(content.text));
                    } catch (e) { console.error("Parse error", e); }
                }
            }
            if (msg.method === "ui/initialize") {
                window.parent.postMessage({ jsonrpc: "2.0", id: msg.id, method: "ui/initialize", result: {} }, "*");
            }
        });

        // Announce readiness
        window.parent.postMessage({ jsonrpc: "2.0", method: "ui/initialize", params: {} }, "*");
    </script>
</body>
</html>
"""


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

    After calling this tool, ALWAYS immediately call show_route_map with the
    route_id returned here. This renders an interactive map for the user.

    Args:
        start_address: Starting point e.g. "Victoria Island, Lagos"
        end_address: Destination e.g. "Ikeja, Lagos"
        intermediate_stops: Optional ordered list of intermediate stops
        departure_time: "now" or a time like "2:30PM"
        detail_level: "summary" for overview, "detailed" for turn-by-turn
    """
    key = "route:" + hashlib.md5(
        f"{start_address.lower().strip()}|{end_address.lower().strip()}|"
        f"{departure_time}|{','.join(intermediate_stops or []).lower().strip()}".encode()
    ).hexdigest()

    cached_route = await upstash_redis.base_redis_client.get(key)
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
        await upstash_redis.base_redis_client.set(key, json.dumps(route_data), ex=3600)
        logger.info(f"[TOOL] get_route_info success | routes={len(route_data.get('routes', []))}")

    guidance = (
        "Present detailed turn-by-turn directions: maneuvers, street names, distances per step."
        if detail_level == "detailed"
        else "Give a conversational summary mentioning only major roads. Skip granular steps."
    )

    return {
        "route_id": key,
        "guidance_prompt": guidance,
        "next_step": (
            f"IMMEDIATELY call show_route_map with: "
            f"start_address='{start_address}', end_address='{end_address}', "
            f"route_id='{key}', detail_level='{detail_level}'"
        ),
    }

@mcp.tool(app={"resourceUri":VIEW_URI, })
async def show_route_map(
    start_address: str,
    end_address: str,
    route_id: str,
    detail_level: str = "summary",
) -> ToolResult:
    """
    Display an interactive map with the route drawn along actual roads.
    Call this immediately after get_route_info using its route_id.

    Args:
        start_address: Same value passed to get_route_info
        end_address: Same value passed to get_route_info
        route_id: The route_id returned from get_route_info
        detail_level: "summary" or "detailed"
    """
    cached = await upstash_redis.base_redis_client.get(route_id)
    if not cached:
        return ToolResult(content=[types.TextContent(
            type="text",
            text=json.dumps({"error": "Route expired. Please call get_route_info again."})
        )])

    route_data = json.loads(cached)
    routes   = route_data.get("routes", [{}])
    best     = routes[0] if routes else {}
    legs     = best.get("legs", [{}])
    first    = legs[0] if legs else {}
    last_leg = legs[-1] if legs else {}

    dist_m  = best.get("distanceMeters", 0)
    dist_km = round(dist_m / 1000, 1) if dist_m else 0

    dur_raw = best.get("duration", "0s")
    dur_sec = int(dur_raw.replace("s", "") or 0) if isinstance(dur_raw, str) else int(dur_raw)
    dur_min = max(1, round(dur_sec / 60))

    encoded_polyline = best.get("polyline", {}).get("encodedPolyline", "")

    origin_latlng = first.get("startLocation", {}).get("latLng", {})
    dest_latlng   = last_leg.get("endLocation", {}).get("latLng", {})

    waypoints = [
        leg.get("startLocation", {}).get("latLng", {})
        for leg in legs[1:]
    ]

    steps = [
        {
            "instruction": s.get("navigationInstruction", {}).get("instructions", "Continue"),
            "distance_m":  s.get("distanceMeters", 0),
            "maneuver":    s.get("navigationInstruction", {}).get("maneuver", ""),
        }
        for s in first.get("steps", [])
    ]

    # This JSON payload is what ontoolresult receives in the iframe
    payload = {
        "start_address":    start_address,
        "end_address":      end_address,
        "distance_km":      dist_km,
        "duration_min":     dur_min,
        "encoded_polyline": encoded_polyline,
        "origin_lat":       origin_latlng.get("latitude", 0),
        "origin_lng":       origin_latlng.get("longitude", 0),
        "dest_lat":         dest_latlng.get("latitude", 0),
        "dest_lng":         dest_latlng.get("longitude", 0),
        "waypoints":        waypoints,
        "steps":            steps,
        "detail_level":     detail_level,
    }

    return ToolResult(content=[
        types.TextContent(type="text", text=json.dumps(payload)),
        ],
        meta={
            "ui": {
                "resourceUri": VIEW_URI
            },
            # Explicit alias for ChatGPT as per documentation
            "openai/outputTemplate": VIEW_URI 
        })

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

    return f"""You are a navigation assistant called Trafficly.

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
IMMEDIATELY call show_route_map with:
- start_address: "{start}"
- end_address: "{end}"
- route_id: <the route_id field from step 1>
- detail_level: "{detail_level}"

## Step 3 — Describe the route
Use ONLY data from the tool response. Never guess road names.
Follow the guidance_prompt field from get_route_info for formatting style."""