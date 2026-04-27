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


# ─── Map UI resource ──────────────────────────────────────────────────────────
#
# ARCHITECTURE NOTES (read before editing):
#
# 1. This HTML is served ONCE as a static resource. It MUST NOT contain any
#    route data — data arrives via the MCP Apps postMessage bridge after render.
#
# 2. The bridge works like this:
#    a. App sends ui/initialize  → host acknowledges
#    b. Host sends ui/notifications/tool-result → app renders map
#
# 3. We CANNOT use `import { App } from "https://unpkg.com/..."` because:
#    - The package has no pre-built CDN bundle (it's Vite-only)
#    - ESM imports from unpkg are blocked by the sandbox CSP
#    Instead, we inline a ~30-line faithful reimplementation of the App bridge
#    that speaks the same ui/* JSON-RPC 2.0 wire protocol.
#
# 4. ChatGPT compatibility: ChatGPT also implements this same iframe+postMessage
#    model. The tool result must carry _meta.ui.resourceUri (standard) AND
#    _meta["openai/outputTemplate"] (ChatGPT alias). Both are set in show_route_map.
#
# 5. Leaflet is loaded via <script src="..."> (classic script, NOT type=module).
#    This works under the sandbox CSP because unpkg.com is in resource_domains.
#
# 6. Race condition fix: map init is deferred until BOTH the bridge fires AND
#    Leaflet has loaded. A simple flag + retry loop handles the ordering.

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
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"/>
  <meta name="color-scheme" content="dark"/>
  <title>Trafficly</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    :root {
      --bg:        #0f1117;
      --surface:   #1a1d2e;
      --border:    #2d3148;
      --accent:    #3b82f6;
      --accent-dk: #1d4ed8;
      --danger:    #dc2626;
      --text:      #e2e8f0;
      --muted:     #64748b;
    }

    html, body {
      height: 100%; width: 100%;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      overflow: hidden;
    }

    #shell {
      display: flex;
      flex-direction: column;
      height: 100vh;
      width: 100vw;
    }

    /* ── Header ── */
    #header {
      display: none;           /* shown once data arrives */
      flex-shrink: 0;
      height: 52px;
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      align-items: center;
      padding: 0 16px;
      gap: 12px;
    }
    #header.visible { display: flex; }

    #logo {
      font-weight: 700;
      font-size: 15px;
      color: var(--accent);
      letter-spacing: -0.02em;
      white-space: nowrap;
    }

    #route-label {
      flex: 1;
      font-size: 12px;
      color: var(--muted);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    #stats {
      font-size: 12px;
      font-weight: 600;
      color: var(--text);
      white-space: nowrap;
      background: var(--border);
      padding: 4px 10px;
      border-radius: 999px;
    }

    /* ── Map ── */
    #map {
      flex: 1;
      width: 100%;
      background: #111;
    }

    /* ── Loading overlay ── */
    #loading {
      position: absolute;
      inset: 0;
      background: var(--bg);
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 14px;
      z-index: 9999;
      transition: opacity 0.3s ease;
    }
    #loading.hidden {
      opacity: 0;
      pointer-events: none;
    }

    .spinner {
      width: 32px; height: 32px;
      border: 3px solid var(--border);
      border-top-color: var(--accent);
      border-radius: 50%;
      animation: spin 0.9s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }

    #loading-text {
      font-size: 13px;
      color: var(--muted);
    }

    /* ── Leaflet overrides ── */
    .leaflet-control-attribution {
      font-size: 9px !important;
      background: rgba(15,17,23,0.7) !important;
      color: #475569 !important;
    }
    .leaflet-control-attribution a { color: #475569 !important; }

    /* ── Step drawer (detail mode) ── */
    #steps-drawer {
      display: none;
      position: absolute;
      bottom: 0; left: 0; right: 0;
      max-height: 38%;
      background: var(--surface);
      border-top: 1px solid var(--border);
      overflow-y: auto;
      z-index: 800;
      padding: 10px 0 16px;
    }
    #steps-drawer.visible { display: block; }
    .step-row {
      display: flex;
      align-items: flex-start;
      gap: 10px;
      padding: 6px 16px;
      font-size: 12px;
      border-bottom: 1px solid var(--border);
    }
    .step-row:last-child { border-bottom: none; }
    .step-dist {
      flex-shrink: 0;
      color: var(--muted);
      width: 52px;
      text-align: right;
    }
  </style>
</head>
<body>

  <!-- Loading overlay -->
  <div id="loading">
    <div class="spinner"></div>
    <div id="loading-text">Connecting to Trafficly…</div>
  </div>

  <!-- Main shell -->
  <div id="shell">
    <div id="header">
      <span id="logo">⚡ Trafficly</span>
      <span id="route-label"></span>
      <span id="stats"></span>
    </div>
    <div id="map"></div>
  </div>

  <!-- Step-by-step drawer (hidden by default, shown in detailed mode) -->
  <div id="steps-drawer"></div>

  <!-- Leaflet — classic script tag, compatible with sandbox CSP -->
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>

  <script>
  // ═══════════════════════════════════════════════════════════════════════════
  // MCP APPS BRIDGE  (inline reimplementation of @modelcontextprotocol/ext-apps)
  //
  // Protocol: JSON-RPC 2.0 over window.postMessage
  // Handshake:
  //   1. App  → host:  { jsonrpc:"2.0", id:1, method:"ui/initialize", params:{name,version} }
  //   2. Host → app:   { jsonrpc:"2.0", id:1, result:{...} }             (ack)
  //   3. Host → app:   { jsonrpc:"2.0", method:"ui/notifications/tool-result",
  //                      params:{ result:{ content:[...] } } }
  //
  // Both Claude and ChatGPT implement this bridge. No external SDK needed.
  // ═══════════════════════════════════════════════════════════════════════════

  (function () {
    "use strict";

    var _reqId = 1;
    var _origin = "*";        // tightened after first trusted message
    var _initialized = false;

    /** Send a JSON-RPC message to the host */
    function rpc(method, params, id) {
      var msg = { jsonrpc: "2.0", method: method };
      if (id !== undefined) msg.id = id;
      if (params !== undefined) msg.params = params;
      window.parent.postMessage(msg, _origin);
    }

    /** Called when host pushes a tool result into this frame */
    function onToolResult(result) {
      if (!result || !result.content) return;
      var textBlock = null;
      for (var i = 0; i < result.content.length; i++) {
        if (result.content[i].type === "text") {
          textBlock = result.content[i];
          break;
        }
      }
      if (!textBlock) return;
      try {
        var data = JSON.parse(textBlock.text);
        if (typeof window.__ontoolresult === "function") {
          window.__ontoolresult(data);
        }
      } catch (e) {
        console.error("[trafficly] Failed to parse tool result:", e);
      }
    }

    /** Handle all inbound postMessage frames */
    window.addEventListener("message", function (event) {
      var msg = event.data;
      if (!msg || typeof msg !== "object" || msg.jsonrpc !== "2.0") return;

      // Trust the origin of the first legitimate message we receive
      if (_origin === "*" && event.origin) {
        _origin = event.origin;
      }

      // ── Ack to our ui/initialize ──
      if (msg.id === 1 && msg.result !== undefined && !_initialized) {
        _initialized = true;
        document.getElementById("loading-text").textContent = "Waiting for route data…";
        return;
      }

      // ── Tool result notification ──
      if (msg.method === "ui/notifications/tool-result" && msg.params) {
        onToolResult(msg.params.result);
        return;
      }

      // ── ChatGPT also sends toolOutput via window.openai (extension) ──
      // Handled separately below via window.openai feature-detection.
    });

    /** Send ui/initialize as soon as DOM is interactive */
    function connect() {
      rpc("ui/initialize", { name: "trafficly-map", version: "1.0.0" }, _reqId++);
    }

    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", connect);
    } else {
      connect();
    }

    // ── ChatGPT extension: window.openai.toolOutput ──
    // Feature-detect; do not throw if not present (other hosts won't have it).
    if (typeof window !== "undefined") {
      window.addEventListener("load", function () {
        var openai = window.openai;
        if (openai && typeof openai.toolOutput !== "undefined") {
          try {
            openai.toolOutput.then
              ? openai.toolOutput.then(onToolResult)   // Promise form
              : onToolResult(openai.toolOutput);        // Direct value form
          } catch (e) {
            console.warn("[trafficly] window.openai.toolOutput error:", e);
          }
        }
      });
    }
  })();


  // ═══════════════════════════════════════════════════════════════════════════
  // MAP RENDERER
  // ═══════════════════════════════════════════════════════════════════════════

  var _mapInstance = null;
  var _pendingData = null;
  var _leafletReady = false;

  /** Google-encoded polyline decoder */
  function decodePolyline(encoded) {
    var pts = [], idx = 0, lat = 0, lng = 0;
    while (idx < encoded.length) {
      var b, shift = 0, result = 0;
      do {
        b = encoded.charCodeAt(idx++) - 63;
        result |= (b & 0x1f) << shift;
        shift += 5;
      } while (b >= 0x20);
      lat += (result & 1) ? ~(result >> 1) : (result >> 1);
      shift = 0; result = 0;
      do {
        b = encoded.charCodeAt(idx++) - 63;
        result |= (b & 0x1f) << shift;
        shift += 5;
      } while (b >= 0x20);
      lng += (result & 1) ? ~(result >> 1) : (result >> 1);
      pts.push([lat / 1e5, lng / 1e5]);
    }
    return pts;
  }

  function formatDist(m) {
    return m >= 1000 ? (m / 1000).toFixed(1) + " km" : m + " m";
  }

  function renderMap(data) {
    // ── Header ──
    var header = document.getElementById("header");
    header.classList.add("visible");

    document.getElementById("route-label").textContent =
      data.start_address + "  →  " + data.end_address;

    document.getElementById("stats").textContent =
      data.distance_km + " km • " + data.duration_min + " min";

    // ── Tear down previous map ──
    if (_mapInstance) {
      _mapInstance.remove();
      _mapInstance = null;
    }

    // ── Init Leaflet ──
    _mapInstance = L.map("map", {
      zoomControl: false,
      attributionControl: true,
    });

    L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
      attribution: "&copy; <a href='https://carto.com/'>CARTO</a>",
      subdomains: "abcd",
      maxZoom: 19,
    }).addTo(_mapInstance);

    // ── Route polyline ──
    var coords;
    if (data.encoded_polyline) {
      coords = decodePolyline(data.encoded_polyline);
    } else {
      coords = [
        [data.origin_lat, data.origin_lng],
        [data.dest_lat,   data.dest_lng],
      ];
    }

    var routeLine = L.polyline(coords, {
      color: "#3b82f6",
      weight: 5,
      opacity: 0.85,
      lineJoin: "round",
    }).addTo(_mapInstance);

    // ── Origin / destination markers ──
    var iconStyle = "width:14px;height:14px;border-radius:50%;border:2px solid #fff;";
    var originIcon = L.divIcon({
      html: "<div style='" + iconStyle + "background:#1d4ed8'></div>",
      className: "", iconSize: [14, 14], iconAnchor: [7, 7],
    });
    var destIcon = L.divIcon({
      html: "<div style='" + iconStyle + "background:#dc2626'></div>",
      className: "", iconSize: [14, 14], iconAnchor: [7, 7],
    });

    L.marker([data.origin_lat, data.origin_lng], { icon: originIcon })
      .bindTooltip(data.start_address, { direction: "top", sticky: false })
      .addTo(_mapInstance);

    L.marker([data.dest_lat, data.dest_lng], { icon: destIcon })
      .bindTooltip(data.end_address, { direction: "top", sticky: false })
      .addTo(_mapInstance);

    // ── Intermediate waypoints ──
    if (Array.isArray(data.waypoints)) {
      data.waypoints.forEach(function (wp) {
        if (wp && wp.latitude && wp.longitude) {
          L.circleMarker([wp.latitude, wp.longitude], {
            radius: 5, color: "#f59e0b", fillColor: "#f59e0b", fillOpacity: 1,
          }).addTo(_mapInstance);
        }
      });
    }

    // ── Fit bounds ──
    _mapInstance.fitBounds(routeLine.getBounds(), { padding: [32, 32] });

    // ── CRITICAL: invalidateSize handles the sandboxed-iframe render quirk ──
    //    Leaflet calculates container dimensions at init time, but inside an
    //    iframe the layout may still be settling. Two invalidations — one
    //    immediate, one deferred — cover both fast and slow host renders.
    _mapInstance.invalidateSize();
    setTimeout(function () {
      if (_mapInstance) _mapInstance.invalidateSize();
    }, 400);

    // ── Step-by-step drawer (detail mode only) ──
    if (data.detail_level === "detailed" && Array.isArray(data.steps) && data.steps.length > 0) {
      var drawer = document.getElementById("steps-drawer");
      drawer.innerHTML = "";
      data.steps.forEach(function (step) {
        var row = document.createElement("div");
        row.className = "step-row";
        row.innerHTML =
          "<span class='step-dist'>" + formatDist(step.distance_m || 0) + "</span>" +
          "<span>" + (step.instruction || "Continue") + "</span>";
        drawer.appendChild(row);
      });
      drawer.classList.add("visible");
    }

    // ── Hide loading overlay ──
    var loading = document.getElementById("loading");
    loading.classList.add("hidden");
    setTimeout(function () { loading.style.display = "none"; }, 350);
  }

  // Called by the bridge when the host delivers the parsed payload
  window.__ontoolresult = function (data) {
    if (!data || typeof data.origin_lat === "undefined") {
      console.warn("[trafficly] Received unexpected payload:", data);
      return;
    }
    _pendingData = data;
    tryRender();
  };

  function tryRender() {
    if (!_pendingData) return;
    if (typeof L === "undefined" || !_leafletReady) {
      // Leaflet script hasn't finished executing yet — retry shortly
      setTimeout(tryRender, 80);
      return;
    }
    var data = _pendingData;
    _pendingData = null;
    renderMap(data);
  }

  // Mark Leaflet as ready once its <script> has loaded
  // We do this by polling — onload doesn't fire reliably inside sandboxed iframes
  // for dynamically injected scripts, but it does for static <script> tags in body.
  document.addEventListener("DOMContentLoaded", function () {
    function checkLeaflet() {
      if (typeof L !== "undefined") {
        _leafletReady = true;
        tryRender(); // in case data already arrived
      } else {
        setTimeout(checkLeaflet, 50);
      }
    }
    checkLeaflet();
  });
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
        (
            f"{start_address.lower().strip()}|{end_address.lower().strip()}|"
            f"{departure_time}|{','.join(intermediate_stops or []).lower().strip()}"
        ).encode()
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

    # ─── _meta wiring ──────────────────────────────────────────────────────
    #
    # MCP Apps standard:      _meta.ui.resourceUri
    # ChatGPT alias:          _meta["openai/outputTemplate"]
    #
    # FastMCP's ToolResult(meta={...}) serialises to _meta on the wire.
    # Both keys must be present so the tool result is recognized by both
    # Claude (standard) and ChatGPT (alias).
    #
    # IMPORTANT: The tool annotation @mcp.tool(app=AppConfig(resource_uri=VIEW_URI))
    # handles the tool-level _meta.ui declaration at registration time.
    # The meta here on the *result* is what tells the host which resource to
    # render for THIS specific call — required by both hosts.
    # ─────────────────────────────────────────────────────────────────────

    return ToolResult(
        content=[
            types.TextContent(type="text", text=json.dumps(payload)),
        ],
        meta={
            "ui": {
                "resourceUri": VIEW_URI,
            },
            # ChatGPT compatibility alias (per OpenAI MCP Apps docs)
            "openai/outputTemplate": VIEW_URI,
        },
    )


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