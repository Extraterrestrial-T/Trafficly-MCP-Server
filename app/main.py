import hashlib
import json
import logging
import os
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastmcp import FastMCP
from fastmcp.apps import AppConfig, ResourceCSP
from fastmcp.dependencies import CurrentContext
from fastmcp.server.auth.providers.clerk import ClerkProvider
from fastmcp.server.context import Context
from fastmcp.tools import ToolResult
from mcp import types
from tenacity import retry, stop_after_attempt, wait_exponential

from app.services.upstash_redis import UpstashRedis

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.services.map_service import Map_client


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("trafficly")


my_maps_client = Map_client(os.getenv("GOOGLE_MAPS_API_KEY"))

upstash_redis = UpstashRedis(
    url=os.getenv("UPSTASH_REDIS_URL"),
    encryption_key=os.getenv("FASTMCP_ENCRYPTION_KEY"),
)

CLERK_DOMAIN = os.environ["CLERK_DOMAIN"]
MCP_SERVER_URL = os.environ["MCP_SERVER_URL"]

auth = ClerkProvider(
    domain=CLERK_DOMAIN,
    client_id=os.environ["CLERK_CLIENT_ID"],
    client_secret=os.environ["CLERK_CLIENT_SECRET"],
    base_url=MCP_SERVER_URL,
    client_storage=upstash_redis.oauth_store,
)


@asynccontextmanager
async def lifespan(server):
    await upstash_redis.base_redis_client.initialize()
    logger.info("Redis client initialized")
    yield
    await my_maps_client.client.aclose()
    await upstash_redis.base_redis_client.aclose()
    logger.info("Resources cleaned up, shutting down")


mcp = FastMCP("trafficly", lifespan=lifespan, auth=auth)

mcp_app = mcp.http_app(path="/mcp")
app = FastAPI(lifespan=mcp_app.lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource():
    return JSONResponse(
        {
            "resource": MCP_SERVER_URL,
            "authorization_servers": [f"https://{CLERK_DOMAIN}"],
        }
    )


app.mount("/", mcp_app)


VIEW_URI = "ui://trafficly/map.html"
MAP_HTML_PATH = Path(__file__).parent / "map.html"
MAP_RESOURCE_CSP = ResourceCSP(
    resource_domains=[
        "https://unpkg.com",
        "https://a.basemaps.cartocdn.com",
        "https://b.basemaps.cartocdn.com",
        "https://c.basemaps.cartocdn.com",
        "https://d.basemaps.cartocdn.com",
        "https://basemaps.cartocdn.com",
    ],
)


def _duration_to_minutes(duration: Any) -> int:
    if duration is None:
        return 1
    if isinstance(duration, (int, float)):
        return max(1, round(float(duration) / 60))
    if not isinstance(duration, str):
        return 1

    duration = duration.strip()
    if duration.endswith("s"):
        try:
            return max(1, round(float(duration[:-1] or 0) / 60))
        except ValueError:
            return 1

    hours = re.search(r"(\d+(?:\.\d+)?)\s*h", duration)
    minutes = re.search(r"(\d+(?:\.\d+)?)\s*m", duration)
    total = 0.0
    if hours:
        total += float(hours.group(1)) * 60
    if minutes:
        total += float(minutes.group(1))
    return max(1, round(total)) if total else 1


def _lat_lng(location: dict[str, Any]) -> dict[str, Any]:
    return location.get("latLng", {}) if isinstance(location, dict) else {}


def _normalize_route_payload(
    route_data: dict[str, Any],
    start_address: str,
    end_address: str,
    detail_level: str,
) -> dict[str, Any]:
    routes = route_data.get("routes", []) if isinstance(route_data, dict) else []
    best = routes[0] if routes else {}
    legs = best.get("legs", []) if isinstance(best, dict) else []
    first_leg = legs[0] if legs else {}
    last_leg = legs[-1] if legs else {}

    origin_latlng = _lat_lng(first_leg.get("startLocation", {}))
    dest_latlng = _lat_lng(last_leg.get("endLocation", {}))
    dist_m = best.get("distanceMeters", 0)
    dist_km = round(dist_m / 1000, 1) if dist_m else 0

    return {
        "start_address": start_address,
        "end_address": end_address,
        "distance_km": dist_km,
        "duration_min": _duration_to_minutes(best.get("duration")),
        "encoded_polyline": best.get("polyline", {}).get("encodedPolyline", ""),
        "origin_lat": origin_latlng.get("latitude", 0),
        "origin_lng": origin_latlng.get("longitude", 0),
        "dest_lat": dest_latlng.get("latitude", 0),
        "dest_lng": dest_latlng.get("longitude", 0),
        "waypoints": [_lat_lng(leg.get("startLocation", {})) for leg in legs[1:]],
        "steps": [
            {
                "instruction": step.get("navigationInstruction", {}).get(
                    "instructions", "Continue"
                ),
                "distance_m": step.get("distanceMeters", 0),
                "maneuver": step.get("navigationInstruction", {}).get("maneuver", ""),
            }
            for step in first_leg.get("steps", [])
        ],
        "detail_level": detail_level,
    }


@mcp.resource(
    VIEW_URI,
    app=AppConfig(csp=MAP_RESOURCE_CSP),
)
def map_view() -> str:
    return MAP_HTML_PATH.read_text(encoding="utf-8")


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
    stops = intermediate_stops or []
    key = "route:" + hashlib.md5(
        (
            f"{start_address.lower().strip()}|{end_address.lower().strip()}|"
            f"{departure_time}|{','.join(stops).lower().strip()}"
        ).encode()
    ).hexdigest()

    cached_route = await upstash_redis.base_redis_client.get(key)
    if cached_route:
        logger.info("[TOOL] get_route_info | %s -> %s (cached)", start_address, end_address)
        route_data = json.loads(cached_route)
    else:
        logger.info("[TOOL] get_route_info | %s -> %s", start_address, end_address)

        geocode_a = await my_maps_client.get_geocode(start_address)
        geocode_b = await my_maps_client.get_geocode(end_address)
        geocoded_stops = [await my_maps_client.get_geocode(stop) for stop in stops]

        route_data = await my_maps_client.calculate_route(
            geocode_a,
            geocode_b,
            stops=geocoded_stops,
            departure_time=departure_time,
        )
        await upstash_redis.base_redis_client.set(key, json.dumps(route_data), ex=3600)
        route_count = len(route_data.get("routes", [])) if isinstance(route_data, dict) else 0
        logger.info("[TOOL] get_route_info success | routes=%s", route_count)

    guidance = (
        "Present detailed turn-by-turn directions: maneuvers, street names, distances per step."
        if detail_level == "detailed"
        else "Give a conversational summary mentioning only major roads. Skip granular steps."
    )

    return {
        "route_id": key,
        "guidance_prompt": guidance,
        "next_step": (
            "IMMEDIATELY call show_route_map with: "
            f"start_address='{start_address}', end_address='{end_address}', "
            f"route_id='{key}', detail_level='{detail_level}'"
        ),
    }


@mcp.tool(
    app=AppConfig(resource_uri=VIEW_URI),
    meta={"openai/outputTemplate": VIEW_URI},
)
async def show_route_map(
    start_address: str,
    end_address: str,
    route_id: str,
    detail_level: str = "summary",
) -> ToolResult:
    """Display an interactive map with the route drawn along actual roads.

    Call this immediately after get_route_info using its route_id.

    Args:
        start_address: Same value passed to get_route_info
        end_address: Same value passed to get_route_info
        route_id: The route_id returned from get_route_info
        detail_level: "summary" or "detailed"
    """
    cached = await upstash_redis.base_redis_client.get(route_id)
    if not cached:
        error_payload = {
            "error": "Route expired. Please call get_route_info again.",
            "route_id": route_id,
        }
        return ToolResult(
            content=[
                types.TextContent(
                    type="text",
                    text=error_payload["error"],
                )
            ],
            structured_content=error_payload,
            meta={
                "ui": {"resourceUri": VIEW_URI},
                "openai/outputTemplate": VIEW_URI,
            },
        )

    route_data = json.loads(cached)
    payload = _normalize_route_payload(
        route_data=route_data,
        start_address=start_address,
        end_address=end_address,
        detail_level=detail_level,
    )
    summary = (
        f"Showing route from {start_address} to {end_address}: "
        f"{payload['distance_km']} km, about {payload['duration_min']} min."
    )

    return ToolResult(
        content=[types.TextContent(type="text", text=summary)],
        structured_content=payload,
        meta={
            "ui": {"resourceUri": VIEW_URI},
            "openai/outputTemplate": VIEW_URI,
        },
    )


@mcp.prompt()
def navigation_prompt(
    start: str,
    end: str,
    detail_level: str = "summary",
    departure_time: str = "now",
    stops: str = "",
) -> str:
    """Generate a navigation prompt for Trafficly."""
    stops_list = [s.strip() for s in stops.split(",") if s.strip()] if stops else []
    stops_text = f" with stops at {', '.join(stops_list)}" if stops_list else ""
    stops_arg = json.dumps(stops_list)

    return f"""You are a navigation assistant called Trafficly.

The user wants to travel from '{start}' to '{end}'{stops_text}, departing at {departure_time}.

## Step 1 - Fetch the route
Call get_route_info with EXACTLY these arguments:
- start_address: "{start}"
- end_address: "{end}"
- intermediate_stops: {stops_arg}
- departure_time: "{departure_time}"
- detail_level: "{detail_level}"

Do NOT proceed until you have the tool response.

## Step 2 - Display the map
IMMEDIATELY call show_route_map with:
- start_address: "{start}"
- end_address: "{end}"
- route_id: <the route_id field from step 1>
- detail_level: "{detail_level}"

## Step 3 - Describe the route
Use ONLY data from the tool response. Never guess road names.
Follow the guidance_prompt field from get_route_info for formatting style."""
