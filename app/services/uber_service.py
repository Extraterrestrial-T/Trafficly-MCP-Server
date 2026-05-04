import json
import logging
import os
import time
from typing import Any, Optional, Tuple

import httpx


logger = logging.getLogger("trafficly.uber")

AUTH_URL = "https://auth.uber.com/oauth/v2/token"
PRODUCTION_ROOT = "https://api.uber.com/v1/guests"
SANDBOX_ROOT = "https://sandbox-api.uber.com/v1/guests"
DEFAULT_SCOPE = "guests.trips"
DEFAULT_SANDBOX_PARENT_PRODUCT_TYPE_ID = "6a8e56b8-914e-4b48-a387-e6ad21d9c00c"
TOKEN_REFRESH_SKEW_SECONDS = 300

_token_cache: dict[str, Any] = {}
_sandbox_run_cache: dict[str, dict[str, Any]] = {}

SENSITIVE_KEYS = {
    "access_token",
    "authorization",
    "client_secret",
    "token",
}


class UberGuestRidesError(RuntimeError):
    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        payload: Optional[dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload or {}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower() in SENSITIVE_KEYS else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value[:5]]
    return value


def _payload_preview(value: Any, limit: int = 900) -> str:
    try:
        return json.dumps(_redact(value), default=str)[:limit]
    except TypeError:
        return str(value)[:limit]


def _payload_keys(value: Any) -> list[str]:
    return sorted(value.keys()) if isinstance(value, dict) else []


def _guest_env() -> str:
    env = (os.getenv("UBER_GUEST_ENV") or "sandbox").strip().lower()
    return "production" if env == "production" else "sandbox"


def _api_root() -> str:
    return PRODUCTION_ROOT if _guest_env() == "production" else SANDBOX_ROOT


def _client_id() -> str:
    value = os.getenv("UBER_CLIENT_ID")
    if not value:
        raise UberGuestRidesError("UBER_CLIENT_ID is not configured.")
    return value


def _client_secret() -> str:
    value = os.getenv("UBER_CLIENT_SECRET")
    if not value:
        raise UberGuestRidesError("UBER_CLIENT_SECRET is not configured.")
    return value


def _sandbox_run_ttl_seconds() -> int:
    try:
        return max(60, int(os.getenv("UBER_SANDBOX_RUN_TTL_SECONDS", "25200")))
    except ValueError:
        return 25200


def _sandbox_parent_product_type_id() -> str:
    return os.getenv(
        "UBER_SANDBOX_PARENT_PRODUCT_TYPE_ID",
        DEFAULT_SANDBOX_PARENT_PRODUCT_TYPE_ID,
    )


def _point_payload(point: Tuple[float, float], address: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"latitude": point[0], "longitude": point[1]}
    if address:
        payload["address"] = address
    return payload


def _run_cache_key(start: Tuple[float, float], end: Tuple[float, float]) -> str:
    return f"{start[0]:.5f},{start[1]:.5f}:{end[0]:.5f},{end[1]:.5f}"


async def _read_response(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        payload = {"text": response.text}
    if isinstance(payload, dict):
        return payload
    return {"value": payload}


async def _raise_for_uber_error(response: httpx.Response, label: str) -> dict[str, Any]:
    payload = await _read_response(response)
    logger.info(
        "[UBER] %s response | status=%s keys=%s preview=%s",
        label,
        response.status_code,
        _payload_keys(payload),
        _payload_preview(payload),
    )
    if response.status_code >= 400:
        message = (
            payload.get("message")
            or payload.get("error_description")
            or payload.get("error")
            or f"Uber {label} request failed."
        )
        raise UberGuestRidesError(message, response.status_code, payload)
    return payload


async def get_guest_access_token() -> str:
    now = int(time.time())
    cached_token = _token_cache.get("access_token")
    cached_expiry = int(_token_cache.get("expires_at", 0) or 0)
    if cached_token and cached_expiry - TOKEN_REFRESH_SKEW_SECONDS > now:
        return cached_token

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            AUTH_URL,
            data={
                "client_id": _client_id(),
                "client_secret": _client_secret(),
                "grant_type": "client_credentials",
                "scope": DEFAULT_SCOPE,
            },
            headers={"content-type": "application/x-www-form-urlencoded"},
        )

    payload = await _raise_for_uber_error(response, "token")
    access_token = payload.get("access_token")
    expires_in = int(payload.get("expires_in", 0) or 0)
    if not access_token or expires_in <= 0:
        raise UberGuestRidesError("Uber token response did not include a usable access token.")

    _token_cache.clear()
    _token_cache.update(
        {
            "access_token": access_token,
            "expires_at": now + expires_in,
            "scope": payload.get("scope"),
            "token_type": payload.get("token_type"),
        }
    )
    logger.info("[UBER] app token cached | expires_in=%s scope=%s", expires_in, payload.get("scope"))
    return access_token


async def _auth_headers() -> dict[str, str]:
    token = await get_guest_access_token()
    return {
        "authorization": f"Bearer {token}",
        "content-type": "application/json",
    }


async def ensure_sandbox_run(start: Tuple[float, float], end: Tuple[float, float]) -> str | None:
    if _guest_env() != "sandbox":
        return None

    now = int(time.time())
    key = _run_cache_key(start, end)
    cached = _sandbox_run_cache.get(key)
    if cached and int(cached.get("expires_at", 0) or 0) > now:
        return str(cached["run_id"])

    body = {
        "driver_locations": [_point_payload(start)],
        "pickup_location": _point_payload(start),
        "dropoff_location": _point_payload(end),
        "parent_product_type_id": _sandbox_parent_product_type_id(),
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{SANDBOX_ROOT}/sandbox/run",
            headers=await _auth_headers(),
            json=body,
        )

    payload = await _raise_for_uber_error(response, "sandbox_run")
    run_id = payload.get("run_id") or payload.get("run_uuid")
    if not run_id:
        raise UberGuestRidesError("Uber sandbox run response did not include a run_id.", response.status_code, payload)

    ttl = min(_sandbox_run_ttl_seconds(), 7 * 60 * 60)
    _sandbox_run_cache[key] = {
        "run_id": run_id,
        "expires_at": now + ttl,
        "pickup": body["pickup_location"],
        "dropoff": body["dropoff_location"],
    }
    logger.info("[UBER] sandbox run cached | run_id_present=true ttl=%s", ttl)
    return str(run_id)


async def _request_headers(
    start: Tuple[float, float] | None = None,
    end: Tuple[float, float] | None = None,
    include_sandbox_run: bool = False,
) -> dict[str, str]:
    headers = await _auth_headers()
    if include_sandbox_run and start and end:
        run_id = await ensure_sandbox_run(start, end)
        if run_id:
            headers["x-uber-sandbox-runuuid"] = run_id
    return headers


def _format_seconds(seconds: Any) -> str:
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    minutes = max(1, round(total / 60))
    return f"{minutes} min"


def _format_distance(value: Any, unit: str | None) -> str:
    if value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if unit:
        return f"{number:g} {unit}"
    return f"{number:g}"


def normalize_estimates(raw: dict[str, Any], start: Tuple[float, float], end: Tuple[float, float]) -> dict[str, Any]:
    estimates = []
    for item in raw.get("product_estimates", []) or []:
        if not isinstance(item, dict):
            continue
        product = item.get("product", {}) or {}
        estimate_info = item.get("estimate_info", {}) or {}
        fare = estimate_info.get("fare", {}) or {}
        trip = estimate_info.get("trip", {}) or {}
        pickup_estimate = estimate_info.get("pickup_estimate")
        estimates.append(
            {
                "product_id": product.get("product_id"),
                "fare_id": estimate_info.get("fare_id") or fare.get("fare_id"),
                "display_name": product.get("display_name") or product.get("short_description"),
                "product_name": product.get("display_name") or product.get("short_description"),
                "price": fare.get("display"),
                "fare": fare.get("display"),
                "display_price": fare.get("display"),
                "currency_code": fare.get("currency_code"),
                "fare_value": fare.get("value"),
                "pickup_estimate": f"{pickup_estimate} min" if pickup_estimate is not None else None,
                "pickup_estimate_minutes": pickup_estimate,
                "duration": _format_seconds(trip.get("duration_estimate")),
                "duration_seconds": trip.get("duration_estimate"),
                "distance": _format_distance(
                    trip.get("distance_estimate") or trip.get("travel_distance_estimate"),
                    trip.get("distance_unit"),
                ),
                "capacity": product.get("capacity"),
                "no_cars_available": estimate_info.get("no_cars_available", False),
                "fulfillment_indicator": item.get("fulfillment_indicator"),
                "raw": item,
            }
        )

    return {
        "pickup": _point_payload(start),
        "dropoff": _point_payload(end),
        "estimates": estimates,
        "etas_unavailable": raw.get("etas_unavailable"),
        "fares_unavailable": raw.get("fares_unavailable"),
        "raw": raw,
    }


async def get_guest_trip_estimates(
    start: Tuple[float, float],
    end: Tuple[float, float],
    waypoints: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "pickup": _point_payload(start),
        "dropoff": _point_payload(end),
    }
    if waypoints:
        body["waypoints"] = waypoints

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{_api_root()}/trips/estimates",
            headers=await _request_headers(start, end, include_sandbox_run=True),
            json=body,
        )

    raw = await _raise_for_uber_error(response, "estimates")
    return normalize_estimates(raw, start, end)


def _find_url(value: Any) -> str | None:
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return value
    if isinstance(value, dict):
        for key in ("rider_tracking_url", "tracking_url", "map_url", "href", "url"):
            found = value.get(key)
            if isinstance(found, str) and found.startswith(("http://", "https://")):
                return found
        for item in value.values():
            nested = _find_url(item)
            if nested:
                return nested
    if isinstance(value, list):
        for item in value:
            nested = _find_url(item)
            if nested:
                return nested
    return None


def normalize_trip_status(raw: dict[str, Any]) -> dict[str, Any]:
    driver = raw.get("driver", {}) or {}
    vehicle = raw.get("vehicle", {}) or {}
    product = raw.get("product", {}) or {}
    pickup = raw.get("pickup", {}) or {}
    destination = raw.get("destination", {}) or raw.get("dropoff", {}) or {}
    tracking_url = raw.get("rider_tracking_url") or _find_url(raw)

    return {
        "request_id": raw.get("request_id") or raw.get("id"),
        "ride_id": raw.get("request_id") or raw.get("id"),
        "status": raw.get("status"),
        "status_detail": raw.get("status_detail"),
        "driver": driver.get("name") or driver,
        "driver_name": driver.get("name"),
        "vehicle": vehicle.get("make") or vehicle.get("model") or vehicle,
        "vehicle_name": " ".join(
            str(part)
            for part in (vehicle.get("make"), vehicle.get("model"))
            if part
        ),
        "license_plate": vehicle.get("license_plate"),
        "product_name": product.get("display_name") or product.get("name"),
        "pickup": pickup,
        "dropoff": destination,
        "tracking_url": tracking_url,
        "rider_tracking_url": raw.get("rider_tracking_url"),
        "client_fare": raw.get("client_fare"),
        "currency_code": raw.get("currency_code"),
        "raw": raw,
    }


async def get_guest_trip_status(request_id: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{_api_root()}/trips/{request_id}",
            headers=await _request_headers(),
        )

    raw = await _raise_for_uber_error(response, "status")
    return normalize_trip_status(raw)
