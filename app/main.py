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
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Trafficly</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    font-family: 'Segoe UI', sans-serif;
    background: #0f1117;
    color: #e2e8f0;
    height: 100vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  .header {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 12px 16px;
    background: #1a1d2e;
    border-bottom: 1px solid #2d3148;
    flex-shrink: 0;
  }
  .logo { font-size: 17px; font-weight: 700; color: #60a5fa; }
  .route-label {
    font-size: 13px; color: #94a3b8; flex:1;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .stats { display: flex; gap: 16px; flex-shrink: 0; }
  .stat { text-align: center; }
  .stat-value { font-size: 15px; font-weight: 700; color: #f1f5f9; }
  .stat-label { font-size: 10px; color: #64748b; text-transform: uppercase; letter-spacing: 0.5px; }
  .body { display: flex; flex: 1; overflow: hidden; }
  #map { flex: 1; min-width: 0; }
  .steps-panel {
    width: 270px; flex-shrink: 0;
    background: #1a1d2e; border-left: 1px solid #2d3148;
    display: flex; flex-direction: column; overflow: hidden;
  }
  .steps-header {
    padding: 12px 14px; font-size: 11px; font-weight: 600;
    color: #64748b; text-transform: uppercase; letter-spacing: 0.8px;
    border-bottom: 1px solid #2d3148; flex-shrink: 0;
  }
  .steps-list { overflow-y: auto; flex: 1; padding: 8px 0; }
  .steps-list::-webkit-scrollbar { width: 4px; }
  .steps-list::-webkit-scrollbar-thumb { background: #2d3148; border-radius: 2px; }
  .step {
    display: flex; gap: 10px; align-items: flex-start;
    padding: 10px 14px; border-bottom: 1px solid #1e2235;
  }
  .step:hover { background: #202336; }
  .step-num {
    min-width: 22px; height: 22px; border-radius: 50%;
    background: #2d3148; color: #94a3b8;
    font-size: 10px; font-weight: 700;
    display: flex; align-items: center; justify-content: center;
    flex-shrink: 0; margin-top: 1px;
  }
  .step-num.origin { background: #1d4ed8; color: #fff; }
  .step-num.dest   { background: #dc2626; color: #fff; }
  .step-body { flex: 1; min-width: 0; }
  .step-instruction { font-size: 12.5px; color: #cbd5e1; line-height: 1.4; word-break: break-word; }
  .step-dist { font-size: 11px; color: #475569; margin-top: 3px; }
  .loading {
    position: absolute; inset: 0; display: flex; align-items: center;
    justify-content: center; background: #0f1117; color: #64748b;
    font-size: 14px; z-index: 9999; flex-direction: column; gap: 12px;
  }
  .spinner {
    width: 32px; height: 32px; border: 3px solid #2d3148;
    border-top-color: #60a5fa; border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .leaflet-tile-pane { filter: brightness(0.85) saturate(0.8); }
  .leaflet-control-attribution {
    font-size: 9px !important;
    background: rgba(0,0,0,0.5) !important;
  }
</style>
</head>
<body>

<div class="loading" id="loading">
  <div class="spinner"></div>
  <span>Loading route...</span>
</div>

<div class="header" id="header" style="display:none">
  <span class="logo">⚡ Trafficly</span>
  <span class="route-label" id="route-label"></span>
  <div class="stats">
    <div class="stat">
      <div class="stat-value" id="stat-dist">—</div>
      <div class="stat-label">Distance</div>
    </div>
    <div class="stat">
      <div class="stat-value" id="stat-dur">—</div>
      <div class="stat-label">Est. time</div>
    </div>
  </div>
</div>

<div class="body" id="body" style="display:none">
  <div id="map" style="height: 600px;"></div>></div>
  <div class="steps-panel">
    <div class="steps-header">Directions</div>
    <div class="steps-list" id="steps-list"></div>
  </div>
</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script type="module">
import { App } from "https://unpkg.com/@modelcontextprotocol/ext-apps@0.4.0/app-with-deps";
console.log("Script is running!");
// Google encoded polyline decoder
function decodePolyline(encoded) {
  const pts = [];
  let idx = 0, lat = 0, lng = 0;
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

function makeIcon(label, bg) {
  return L.divIcon({
    className: '',
    html: `<div style="width:28px;height:28px;border-radius:50%;background:${bg};color:#fff;
           font-size:11px;font-weight:700;display:flex;align-items:center;justify-content:center;
           border:2px solid rgba(255,255,255,0.25);box-shadow:0 2px 8px rgba(0,0,0,0.5)">${label}</div>`,
    iconSize: [28, 28], iconAnchor: [14, 14],
  });
}

let mapInstance = null;

function renderMap(data) {
  document.getElementById('loading').style.display = 'none';
  document.getElementById('header').style.display  = 'flex';
  document.getElementById('body').style.display    = 'flex';

  document.getElementById('route-label').textContent =
    `${data.start_address} → ${data.end_address}`;
  document.getElementById('stat-dist').textContent = `${data.distance_km} km`;
  document.getElementById('stat-dur').textContent  = `${data.duration_min} min`;

  if (mapInstance) { mapInstance.remove(); }
  mapInstance = L.map('map', { zoomControl: true });

  L.tileLayer(
    'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
    { attribution: '&copy; CARTO', subdomains: 'abcd', maxZoom: 19 }
  ).addTo(mapInstance);

  let routeLine;
  if (data.encoded_polyline) {
    const coords = decodePolyline(data.encoded_polyline);
    routeLine = L.polyline(coords, {
      color: '#3b82f6', weight: 5, opacity: 0.9,
      lineCap: 'round', lineJoin: 'round',
    }).addTo(mapInstance);
  } else {
    const pts = [
      [data.origin_lat, data.origin_lng],
      ...(data.waypoints || []).map(w => [w.latitude, w.longitude]),
      [data.dest_lat, data.dest_lng],
    ];
    routeLine = L.polyline(pts, { color: '#3b82f6', weight: 4, opacity: 0.8 }).addTo(mapInstance);
  }
  mapInstance.fitBounds(routeLine.getBounds(), { padding: [32, 32] });

  L.marker([data.origin_lat, data.origin_lng], { icon: makeIcon('A', '#1d4ed8') })
    .addTo(mapInstance).bindPopup(`<b>Start</b><br>${data.start_address}`);

  (data.waypoints || []).forEach((wp, i) => {
    L.marker([wp.latitude, wp.longitude], { icon: makeIcon(i + 2, '#7c3aed') })
      .addTo(mapInstance).bindPopup(`<b>Stop ${i + 2}</b>`);
  });

  L.marker([data.dest_lat, data.dest_lng], { icon: makeIcon('B', '#dc2626') })
    .addTo(mapInstance).bindPopup(`<b>Destination</b><br>${data.end_address}`);

  const panel  = document.getElementById('steps-list');
  const allSteps = data.steps || [];
  const displaySteps = data.detail_level === 'detailed'
    ? allSteps
    : allSteps.filter(s => s.maneuver && !['DEPART','ARRIVE',''].includes(s.maneuver)).slice(0, 8);

  panel.innerHTML = `
    <div class="step">
      <div class="step-num origin">A</div>
      <div class="step-body">
        <div class="step-instruction">${data.start_address}</div>
        <div class="step-dist">Start</div>
      </div>
    </div>`;

  displaySteps.forEach((step, i) => {
    const d = step.distance_m > 1000
      ? (step.distance_m / 1000).toFixed(1) + ' km'
      : step.distance_m + ' m';
    panel.innerHTML += `
      <div class="step">
        <div class="step-num">${i + 1}</div>
        <div class="step-body">
          <div class="step-instruction">${step.instruction}</div>
          <div class="step-dist">${d}</div>
        </div>
      </div>`;
  });

  panel.innerHTML += `
    <div class="step">
      <div class="step-num dest">B</div>
      <div class="step-body">
        <div class="step-instruction">${data.end_address}</div>
        <div class="step-dist">Destination</div>
      </div>
    </div>`;
}

// Connect to the MCP Apps host bridge.
// ontoolresult fires when the host pushes the show_route_map result into the iframe.
const mcpApp = new App({ name: "Trafficly", version: "1.0.0" });

mcpApp.ontoolresult = ({ content }) => {
  const textBlock = content?.find(c => c.type === 'text');
  if (!textBlock) return;
  try {
    renderMap(JSON.parse(textBlock.text));
  } catch (e) {
    console.error('Trafficly: failed to parse tool result', e);
  }
};

await mcpApp.connect();
</script>
</body>
</html>"""


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


@mcp.tool(app=AppConfig(resource_uri=VIEW_URI))
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
        types.TextContent(type="text", text=json.dumps(payload))
    ])


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