import hashlib
import json
import logging
import os
import re
import sys
import time
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
ROUTE_CACHE_TTL_SECONDS = 3600
CACHE_SCHEMA_VERSION = 1
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


def _stable_route_key(start_address: str, end_address: str, stops: list[str]) -> str:
    identity = "|".join(
        [
            start_address.lower().strip(),
            end_address.lower().strip(),
            ",".join(stop.lower().strip() for stop in stops),
        ]
    )
    return "route:" + hashlib.md5(identity.encode()).hexdigest()


def _route_cache_envelope(
    route_data: dict[str, Any],
    start_address: str,
    end_address: str,
    stops: list[str],
    departure_time: str | None,
    detail_level: str | None,
) -> dict[str, Any]:
    now = int(time.time())
    trafficly_meta = route_data.get("_trafficly", {}) if isinstance(route_data, dict) else {}
    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "created_at": now,
        "expires_at": now + ROUTE_CACHE_TTL_SECONDS,
        "request": {
            "start_address": start_address,
            "end_address": end_address,
            "intermediate_stops": stops,
            "raw_departure_time": departure_time,
            "resolved_departure_time": trafficly_meta.get("resolved_departure_time"),
            "timezone": trafficly_meta.get("timezone"),
            "detail_level": detail_level,
        },
        "route_data": route_data,
    }


def _unwrap_cached_route(
    cached_value: str,
    fallback_start_address: str,
    fallback_end_address: str,
    fallback_detail_level: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    cached_data = json.loads(cached_value)
    if isinstance(cached_data, dict) and "route_data" in cached_data:
        request = cached_data.get("request", {})
        return cached_data.get("route_data", {}), {
            "start_address": request.get("start_address", fallback_start_address),
            "end_address": request.get("end_address", fallback_end_address),
            "intermediate_stops": request.get("intermediate_stops", []),
            "raw_departure_time": request.get("raw_departure_time"),
            "resolved_departure_time": request.get("resolved_departure_time"),
            "timezone": request.get("timezone"),
            "detail_level": request.get("detail_level", fallback_detail_level),
            "cached_schema_version": cached_data.get("schema_version"),
            "created_at": cached_data.get("created_at"),
            "expires_at": cached_data.get("expires_at"),
        }

    trafficly_meta = cached_data.get("_trafficly", {}) if isinstance(cached_data, dict) else {}
    return cached_data, {
        "start_address": fallback_start_address,
        "end_address": fallback_end_address,
        "intermediate_stops": [],
        "raw_departure_time": trafficly_meta.get("raw_departure_time"),
        "resolved_departure_time": trafficly_meta.get("resolved_departure_time"),
        "timezone": trafficly_meta.get("timezone"),
        "detail_level": fallback_detail_level,
        "cached_schema_version": 0,
    }


def _route_data_has_routes(route_data: Any) -> bool:
    return (
        isinstance(route_data, dict)
        and isinstance(route_data.get("routes"), list)
        and len(route_data["routes"]) > 0
    )


def _point_payload(location: dict[str, Any], label: str = "") -> dict[str, Any]:
    lat_lng = _lat_lng(location)
    return {
        "latitude": lat_lng.get("latitude", 0),
        "longitude": lat_lng.get("longitude", 0),
        "label": label,
    }


def _stop_label(stop_labels: list[str], index: int) -> str:
    return stop_labels[index] if 0 <= index < len(stop_labels) else f"Stop {index + 1}"


def _normalize_route_option(
    route: dict[str, Any],
    index: int,
    start_address: str,
    end_address: str,
    stop_labels: list[str],
    detail_level: str,
) -> dict[str, Any]:
    legs = route.get("legs", []) if isinstance(route, dict) else []
    first_leg = legs[0] if legs else {}
    last_leg = legs[-1] if legs else {}
    dist_m = route.get("distanceMeters", 0)
    dist_km = round(dist_m / 1000, 1) if dist_m else 0

    leg_summaries = []
    steps = []
    waypoints = []

    for leg_index, leg in enumerate(legs):
        leg_start = _point_payload(
            leg.get("startLocation", {}),
            start_address if leg_index == 0 else _stop_label(stop_labels, leg_index - 1),
        )
        leg_end = _point_payload(
            leg.get("endLocation", {}),
            end_address if leg_index == len(legs) - 1 else _stop_label(stop_labels, leg_index),
        )
        leg_summaries.append(
            {
                "index": leg_index,
                "distance_m": leg.get("distanceMeters", 0),
                "duration_min": _duration_to_minutes(leg.get("duration")),
                "start": leg_start,
                "end": leg_end,
            }
        )
        if 0 < leg_index:
            waypoints.append(leg_start)

        for step_index, step in enumerate(leg.get("steps", [])):
            steps.append(
                {
                    "leg_index": leg_index,
                    "step_index": step_index,
                    "instruction": step.get("navigationInstruction", {}).get(
                        "instructions", "Continue"
                    ),
                    "distance_m": step.get("distanceMeters", 0),
                    "maneuver": step.get("navigationInstruction", {}).get("maneuver", ""),
                    "start": _point_payload(step.get("startLocation", {})),
                    "end": _point_payload(step.get("endLocation", {})),
                }
            )

    origin = _point_payload(first_leg.get("startLocation", {}), start_address)
    destination = _point_payload(last_leg.get("endLocation", {}), end_address)

    return {
        "index": index,
        "label": "Best route" if index == 0 else f"Route {index + 1}",
        "start_address": start_address,
        "end_address": end_address,
        "distance_km": dist_km,
        "duration_min": _duration_to_minutes(route.get("duration")),
        "encoded_polyline": route.get("polyline", {}).get("encodedPolyline", ""),
        "origin_lat": origin["latitude"],
        "origin_lng": origin["longitude"],
        "dest_lat": destination["latitude"],
        "dest_lng": destination["longitude"],
        "origin": origin,
        "destination": destination,
        "waypoints": waypoints,
        "legs": leg_summaries,
        "steps": steps,
        "detail_level": detail_level,
    }


def _normalize_route_payload(
    route_data: dict[str, Any],
    request_meta: dict[str, Any],
    detail_level: str,
) -> dict[str, Any]:
    routes = route_data.get("routes", []) if isinstance(route_data, dict) else []
    start_address = request_meta.get("start_address", "")
    end_address = request_meta.get("end_address", "")
    stop_labels = request_meta.get("intermediate_stops", []) or []
    normalized_routes = [
        _normalize_route_option(route, index, start_address, end_address, stop_labels, detail_level)
        for index, route in enumerate(routes)
    ]
    best = normalized_routes[0] if normalized_routes else _normalize_route_option(
        {}, 0, start_address, end_address, stop_labels, detail_level
    )

    return {
        "request": request_meta,
        "start_address": start_address,
        "end_address": end_address,
        "selected_route_index": 0,
        "routes": normalized_routes,
        "distance_km": best["distance_km"],
        "duration_min": best["duration_min"],
        "encoded_polyline": best["encoded_polyline"],
        "origin_lat": best["origin_lat"],
        "origin_lng": best["origin_lng"],
        "dest_lat": best["dest_lat"],
        "dest_lng": best["dest_lng"],
        "origin": best["origin"],
        "destination": best["destination"],
        "waypoints": best["waypoints"],
        "legs": best["legs"],
        "steps": best["steps"],
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
        intermediate_stops: Optional ordered list of named intermediate stops.
            Use specific place names when available, e.g. "Union Bank, Marina, Lagos".
        departure_time: "now" or a time like "2:30PM"
        detail_level: "summary" for overview, "detailed" for turn-by-turn
    """
    stops = intermediate_stops or []
    key = _stable_route_key(start_address, end_address, stops)

    cached_route = await upstash_redis.base_redis_client.get(key)
    if cached_route:
        cached_route_data, _cached_request = _unwrap_cached_route(
            cached_route,
            fallback_start_address=start_address,
            fallback_end_address=end_address,
            fallback_detail_level=detail_level,
        )
        if _route_data_has_routes(cached_route_data):
            logger.info(
                "[TOOL] get_route_info | %s -> %s (cached)",
                start_address,
                end_address,
            )
        else:
            logger.warning(
                "[TOOL] get_route_info | ignoring cached route with no routes | key=%s",
                key,
            )
            cached_route = None

    if not cached_route:
        logger.info("[TOOL] get_route_info | %s -> %s", start_address, end_address)

        geocode_a = await my_maps_client.get_geocode(start_address)
        geocode_b = await my_maps_client.get_geocode(end_address)
        if not geocode_a or not geocode_b:
            return {
                "error": "Could not geocode the start or destination address.",
                "route_id": key,
                "next_step": "Ask the user for more specific start and destination addresses.",
            }

        geocoded_stops = []
        successful_stops = []
        for stop in stops:
            geocoded_stop = await my_maps_client.get_geocode(stop)
            if geocoded_stop:
                geocoded_stops.append(geocoded_stop)
                successful_stops.append(stop)
            else:
                logger.warning("[TOOL] get_route_info | failed to geocode stop=%s", stop)

        route_data = await my_maps_client.calculate_route(
            geocode_a,
            geocode_b,
            stops=geocoded_stops,
            departure_time=departure_time,
        )
        if not _route_data_has_routes(route_data):
            logger.warning(
                "[TOOL] get_route_info failed | no routes returned | start=%s end=%s stops=%s",
                start_address,
                end_address,
                successful_stops,
            )
            return {
                "error": (
                    "Google Routes did not return a usable route. Try a later departure time "
                    "or more specific stop/destination names."
                ),
                "route_id": key,
                "next_step": "Do not call show_route_map for this route_id; ask for a corrected route request.",
            }

        envelope = _route_cache_envelope(
            route_data=route_data,
            start_address=start_address,
            end_address=end_address,
            stops=successful_stops,
            departure_time=departure_time,
            detail_level=detail_level,
        )
        await upstash_redis.base_redis_client.setex(
            key, ROUTE_CACHE_TTL_SECONDS, json.dumps(envelope)
        )
        ttl = await upstash_redis.base_redis_client.ttl(key)
        route_count = len(route_data.get("routes", [])) if isinstance(route_data, dict) else 0
        logger.info("[TOOL] get_route_info success | routes=%s ttl=%s", route_count, ttl)

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
        logger.warning("[TOOL] show_route_map cache miss | route_id=%s", route_id)
        error_payload = {
            "error": (
                "Route data was not found in cache. Please call get_route_info again "
                "for this route, then call show_route_map with the new route_id."
            ),
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

    route_data, request_meta = _unwrap_cached_route(
        cached,
        fallback_start_address=start_address,
        fallback_end_address=end_address,
        fallback_detail_level=detail_level,
    )
    if not _route_data_has_routes(route_data):
        logger.warning("[TOOL] show_route_map unusable cached route | route_id=%s", route_id)
        error_payload = {
            "error": (
                "This cached route does not contain usable map coordinates. "
                "Please call get_route_info again to refresh it."
            ),
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

    payload = _normalize_route_payload(
        route_data=route_data,
        request_meta=request_meta,
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

If the user mentions a business, landmark, or errand at a stop, preserve the full named stop
instead of shortening it. Example: use "Union Bank, Marina, Lagos", not just "Marina".

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
