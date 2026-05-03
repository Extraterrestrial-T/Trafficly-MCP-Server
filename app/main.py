import asyncio
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
from typing import Tuple
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastmcp import FastMCP
from fastmcp.apps import AppConfig, ResourceCSP
from fastmcp.dependencies import CurrentContext
from fastmcp.server.auth.providers.clerk import ClerkProvider
from fastmcp.server.context import Context
from fastmcp.tools import ToolResult
from mcp import types
import secrets
from tenacity import retry, stop_after_attempt, wait_exponential
from uber_rides.auth import AuthorizationCodeGrant
from app.services.upstash_redis import UpstashRedis
from app.services.uber_service import (
    book_ride,
    cancel_ride,
    get_ride_estimate,
    get_ride_map,
    get_ride_status,
    serialize_oauth_credentials,
)
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


   

@app.get("/uber/auth/")
async def callback(request: Request):
    state = request.query_params.get("state")
    code_present = bool(request.query_params.get("code"))
    if not state or not code_present:
        logger.warning("[UBER] callback rejected | missing_state=%s code_present=%s", not state, code_present)
        return HTMLResponse(
            "<h1>Uber authorization failed</h1><p>Missing authorization code or state.</p>",
            status_code=400,
        )

    state_record = await upstash_redis.oauth_uber_store.get(f"state:{state}")
    if not state_record or not state_record.get("client_id"):
        logger.warning("[UBER] callback rejected | state_missing=true")
        return HTMLResponse(
            "<h1>Uber authorization expired</h1><p>Please return to Trafficly and connect Uber again.</p>",
            status_code=400,
        )

    auth_flow = AuthorizationCodeGrant(
        os.getenv("UBER_CLIENT_ID"),
        {"profile", "email", "address", "payment_method", "request"},
        os.getenv("UBER_CLIENT_SECRET"),
        os.getenv("UBER_REDIRECT_URI"),
        state_token=state,
    )
    try:
        session = await asyncio.to_thread(auth_flow.get_session, str(request.url))
        credentials = serialize_oauth_credentials(session.oauth2credential)
        client_id = state_record["client_id"]
        await upstash_redis.oauth_uber_store.put(f"token:{client_id}", credentials)
        await upstash_redis.oauth_uber_store.delete(f"state:{state}")
        logger.info("[UBER] callback success | client_id_present=%s", bool(client_id))
    except Exception:
        logger.exception("[UBER] callback token exchange failed")
        return HTMLResponse(
            "<h1>Uber authorization failed</h1><p>Trafficly could not complete the token exchange.</p>",
            status_code=500,
        )

    return HTMLResponse(
        """
        <!doctype html>
        <html lang="en">
        <head><meta charset="utf-8"><title>Uber connected</title></head>
        <body style="font-family: system-ui, -apple-system, Segoe UI, sans-serif; padding: 32px;">
          <h1>Uber connected</h1>
          <p>You can close this window and return to Trafficly.</p>
        </body>
        </html>
        """
    )

app.mount("/", mcp_app)


VIEW_URI = "ui://trafficly/map.html"
UBER_VIEW_URI = "ui://trafficly/uber.html"
MAP_HTML_PATH = Path(__file__).parent / "map.html"
UBER_HTML_PATH = Path(__file__).parent / "uber.html"
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
UBER_RESOURCE_CSP = ResourceCSP(
    resource_domains=["https://unpkg.com"],
    frame_domains=[
        "https://m.uber.com",
        "https://trip.uber.com",
        "https://riders.uber.com",
        "https://www.uber.com",
        "https://uber.com",
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

@mcp.resource(
    UBER_VIEW_URI,
    app=AppConfig(csp=UBER_RESOURCE_CSP),
)
def uber_view() -> str:
    return UBER_HTML_PATH.read_text(encoding="utf-8")


def _json_from_store(raw_value: Any) -> dict[str, Any]:
    if raw_value is None:
        return {}
    if isinstance(raw_value, bytes):
        raw_value = raw_value.decode("utf-8")
    if isinstance(raw_value, str):
        return json.loads(raw_value)
    if isinstance(raw_value, dict):
        return raw_value
    return {}


def _strip_internal_fields(payload: Any) -> Any:
    if isinstance(payload, dict):
        return {
            key: _strip_internal_fields(value)
            for key, value in payload.items()
            if not key.startswith("_")
        }
    if isinstance(payload, list):
        return [_strip_internal_fields(item) for item in payload]
    return payload


def _payload_keys(payload: Any) -> list[str]:
    return sorted(payload.keys()) if isinstance(payload, dict) else []


async def _persist_refreshed_uber_credentials(client_id: str, result: Any) -> Any:
    if isinstance(result, dict) and isinstance(result.get("_oauth_credentials"), dict):
        await upstash_redis.oauth_uber_store.put(
            f"token:{client_id}",
            result["_oauth_credentials"],
        )
        logger.debug("[UBER] refreshed credentials persisted | client_id_present=%s", bool(client_id))
    return _strip_internal_fields(result)


def _uber_tool_result(payload: dict[str, Any], fallback_text: str | None = None) -> ToolResult:
    text = fallback_text or payload.get("message") or "Trafficly returned an Uber result."
    return ToolResult(
        content=[types.TextContent(type="text", text=text)],
        structured_content=payload,
        meta={
            "ui": {"resourceUri": UBER_VIEW_URI},
            "openai/outputTemplate": UBER_VIEW_URI,
        },
    )


def _uber_error(message: str, **extra: Any) -> ToolResult:
    payload = {
        "intent": "error",
        "message": message,
        **extra,
    }
    return _uber_tool_result(payload, message)


def _normalize_uber_estimate_result(result: Any) -> dict[str, Any]:
    price_data: dict[str, Any] = {}
    time_data: dict[str, Any] = {}

    if isinstance(result, dict) and ("price_response" in result or "time_response" in result):
        price_data = result.get("price_response", {}) or {}
        time_data = result.get("time_response", {}) or {}
    elif isinstance(result, tuple):
        if len(result) > 0 and isinstance(result[0], dict):
            price_data = result[0]
        if len(result) > 1 and isinstance(result[1], dict):
            time_data = result[1]
    elif isinstance(result, dict):
        price_data = result

    logger.info(
        "[UBER] estimate normalize | price_keys=%s time_keys=%s",
        _payload_keys(price_data),
        _payload_keys(time_data),
    )

    estimates = (
        price_data.get("prices")
        or price_data.get("estimates")
        or price_data.get("price_estimates")
        or []
    )
    pickup_estimates = (
        time_data.get("times")
        or time_data.get("estimates")
        or time_data.get("pickup_estimates")
        or []
    )
    pickup_by_product = {
        item.get("product_id"): item
        for item in pickup_estimates
        if isinstance(item, dict) and item.get("product_id")
    }

    merged_estimates = []
    for estimate in estimates:
        if not isinstance(estimate, dict):
            continue
        product_id = estimate.get("product_id")
        pickup = pickup_by_product.get(product_id, {})
        merged_estimate = {**estimate}
        if pickup:
            merged_estimate["pickup_estimate"] = pickup.get("estimate")
        merged_estimates.append(merged_estimate)

    return {
        "estimates": merged_estimates or estimates,
        "pickup_estimates": pickup_estimates,
        "raw_price_response": price_data,
        "raw_time_response": time_data,
    }


@mcp.tool(
    app=AppConfig(resource_uri=UBER_VIEW_URI),
    meta={"openai/outputTemplate": UBER_VIEW_URI},
)
async def Uber_tool(
    start: Tuple[float, float],
    end: Tuple[float, float],
    ctx: Context,
    intent: str,
) -> ToolResult:
    """Estimate, book, inspect, or cancel Uber rides for the current Trafficly user.

    If the user has not connected Uber yet, this returns an auth UI with a
    Continue to Uber button. Otherwise it returns structured Uber data for
    the requested intent.
    """
    client_id = ctx.client_id
    if not client_id:
        logger.warning("[UBER] tool rejected | missing ctx.client_id")
        return _uber_error("Trafficly could not identify the current user for Uber authorization.")

    normalized_intent = intent.lower().strip()
    uber_raw = await upstash_redis.oauth_uber_store.get(f"token:{client_id}") or None
    logger.info(
        "[UBER] tool call | intent=%s token_present=%s client_id_present=%s",
        normalized_intent,
        bool(uber_raw),
        bool(client_id),
    )

    if not uber_raw:
        state = secrets.token_urlsafe(32)
        scopes = {"profile", "email", "address", "payment_method", "request"}
        auth_flow = AuthorizationCodeGrant(
            os.getenv("UBER_CLIENT_ID"),
            scopes,
            os.getenv("UBER_CLIENT_SECRET"),
            os.getenv("UBER_REDIRECT_URI"),
            state_token=state,
        )
        oauth_url = auth_flow.get_authorization_url()
        await upstash_redis.oauth_uber_store.put(
            f"state:{state}",
            {"client_id": client_id, "created_at": int(time.time())},
            ttl=600,
        )
        logger.info("[UBER] auth required | state_created=true")
        return _uber_tool_result(
            {
                "intent": "auth_required",
                "uber_auth_url": oauth_url,
                "message": (
                    "Connect your Uber account to let Trafficly fetch estimates "
                    "and manage ride requests."
                ),
                "data": {"scopes": sorted(scopes)},
            }
        )

    try:
        oauth_credentials = _json_from_store(uber_raw)
    except json.JSONDecodeError:
        logger.exception("[TOOL] Uber_tool failed to decode stored credentials")
        return _uber_error(
            "Trafficly could not read your stored Uber credentials. Please reconnect Uber."
        )

    try:
        if normalized_intent in {"estimate", "price_estimate", "ride_estimate"}:
            result = await get_ride_estimate(start, end, oauth_credentials)
            result = await _persist_refreshed_uber_credentials(client_id, result)
            data = _normalize_uber_estimate_result(result)
            payload = {
                "intent": "price_estimate",
                "message": "Here are the Uber ride estimates Trafficly found.",
                "data": {
                    **data,
                    "pickup": {"latitude": start[0], "longitude": start[1]},
                    "dropoff": {"latitude": end[0], "longitude": end[1]},
                },
            }
            return _uber_tool_result(payload)

        ride_id = await ctx.get_state("ride_id")

        if normalized_intent == "book":
            if ride_id:
                return _uber_error(
                    "There is already an active Uber ride request in this Trafficly session.",
                    data={"ride_id": ride_id},
                )
            result = await book_ride(start, end, oauth_credentials)
            result = await _persist_refreshed_uber_credentials(client_id, result)
            ride_id = result.get("request_id") or result.get("ride_id") or result.get("id")
            if ride_id:
                await ctx.set_state("ride_id", ride_id)
            logger.info("[UBER] book result | keys=%s ride_id_present=%s", _payload_keys(result), bool(ride_id))
            return _uber_tool_result(
                {
                    "intent": "ride_booked",
                    "message": "Trafficly created the Uber ride request.",
                    "data": {
                        **result,
                        "ride_id": ride_id,
                        "pickup": {"latitude": start[0], "longitude": start[1]},
                        "dropoff": {"latitude": end[0], "longitude": end[1]},
                    },
                }
            )

        if normalized_intent == "status":
            if not ride_id:
                return _uber_error("There is no active Uber ride request in this session.")
            result = await get_ride_status(request_id=ride_id, oauth_credentials=oauth_credentials)
            result = await _persist_refreshed_uber_credentials(client_id, result)
            logger.info("[UBER] status result | keys=%s", _payload_keys(result))
            return _uber_tool_result(
                {
                    "intent": "ride_status",
                    "message": "Here is the latest Uber ride status.",
                    "data": {**result, "ride_id": ride_id},
                }
            )

        if normalized_intent == "map":
            if not ride_id:
                return _uber_error("There is no active Uber ride request to map in this session.")
            result = await get_ride_map(request_id=ride_id, oauth_credentials=oauth_credentials)
            result = await _persist_refreshed_uber_credentials(client_id, result)
            logger.info(
                "[UBER] map result | keys=%s map_url_present=%s",
                _payload_keys(result),
                bool(result.get("map_url")),
            )
            return _uber_tool_result(
                {
                    "intent": "ride_map",
                    "message": "Here are the latest Uber ride map details.",
                    "data": {**result, "ride_id": ride_id},
                }
            )

        if normalized_intent == "cancel":
            if not ride_id:
                return _uber_error("There is no active Uber ride request to cancel in this session.")
            result = await cancel_ride(request_id=ride_id, oauth_credentials=oauth_credentials)
            result = await _persist_refreshed_uber_credentials(client_id, result)
            await ctx.set_state("ride_id", None)
            logger.info("[UBER] cancel result | keys=%s", _payload_keys(result))
            return _uber_tool_result(
                {
                    "intent": "ride_status",
                    "message": "Trafficly cancelled the Uber ride request.",
                    "data": {**result, "ride_id": ride_id, "status": "cancelled"},
                }
            )

        return _uber_error(
            "Unsupported Uber intent. Use estimate, book, status, map, or cancel.",
            data={"intent": intent},
        )
    except Exception as exc:
        logger.exception("[TOOL] Uber_tool failed | intent=%s", normalized_intent)
        return _uber_error(
            "Trafficly could not complete the Uber request.",
            error=str(exc),
            data={"intent": intent},
        )

    
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
