import asyncio
import json
import logging
import os
import time
from collections.abc import Callable
from typing import Any, Tuple

from fastapi import HTTPException
from uber_rides.client import UberRidesClient
from uber_rides.session import OAuth2Credential, Session


logger = logging.getLogger("trafficly.uber")

SENSITIVE_KEYS = {
    "access_token",
    "refresh_token",
    "authorization",
    "code",
    "client_secret",
    "uber_auth_url",
}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if key.lower() in SENSITIVE_KEYS:
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = _redact(item)
        return redacted
    if isinstance(value, list):
        return [_redact(item) for item in value[:5]]
    return value


def _payload_preview(value: Any, limit: int = 900) -> str:
    try:
        preview = json.dumps(_redact(value), default=str)[:limit]
    except TypeError:
        preview = str(value)[:limit]
    return preview


def _payload_keys(value: Any) -> list[str]:
    return sorted(value.keys()) if isinstance(value, dict) else []


def serialize_oauth_credentials(credential: OAuth2Credential) -> dict[str, Any]:
    """Convert SDK credentials into a JSON-safe storage shape.

    The SDK mutates `expires_in_seconds` into an absolute epoch timestamp,
    so store it as `expires_at` to avoid double-adding time on restore.
    """
    return {
        "access_token": credential.access_token,
        "refresh_token": credential.refresh_token,
        "expires_at": credential.expires_in_seconds,
        "scopes": sorted(credential.scopes or []),
        "grant_type": credential.grant_type,
    }


def _remaining_expiry_seconds(oauth_credentials: dict[str, Any]) -> int:
    if oauth_credentials.get("expires_at") is not None:
        return max(0, int(float(oauth_credentials["expires_at"]) - time.time()))

    # Backward compatibility for older stored values. The SDK constructor
    # expects relative seconds, so only use this when no absolute expiry exists.
    if oauth_credentials.get("expires_in_seconds") is not None:
        return max(0, int(oauth_credentials["expires_in_seconds"]))
    if oauth_credentials.get("expires_in") is not None:
        return max(0, int(oauth_credentials["expires_in"]))
    return 0


def build_oauth_credential(oauth_credentials: dict[str, Any]) -> OAuth2Credential:
    access_token = oauth_credentials.get("access_token")
    refresh_token = oauth_credentials.get("refresh_token")
    if not access_token:
        raise ValueError("Stored Uber credentials are missing access_token.")

    scopes = oauth_credentials.get("scopes") or []
    if isinstance(scopes, str):
        scopes = scopes.split()

    return OAuth2Credential(
        client_id=os.getenv("UBER_CLIENT_ID"),
        access_token=access_token,
        expires_in_seconds=_remaining_expiry_seconds(oauth_credentials),
        scopes=set(scopes),
        grant_type=oauth_credentials.get("grant_type", "authorization_code"),
        redirect_url=os.getenv("UBER_REDIRECT_URI"),
        client_secret=os.getenv("UBER_CLIENT_SECRET"),
        refresh_token=refresh_token,
    )


def _response_json(response: Any) -> dict[str, Any]:
    payload = getattr(response, "json", None)
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        return json.loads(payload)
    return {"value": payload}


def _with_client(
    oauth_credentials: dict[str, Any],
    action: Callable[[UberRidesClient], dict[str, Any]],
    label: str,
) -> dict[str, Any]:
    credential = build_oauth_credential(oauth_credentials)
    client = UberRidesClient(Session(oauth2credential=credential), sandbox_mode=True)
    payload = action(client)
    payload["_oauth_credentials"] = serialize_oauth_credentials(client.session.oauth2credential)
    logger.info(
        "[UBER] %s response | keys=%s preview=%s",
        label,
        _payload_keys(payload),
        _payload_preview(payload),
    )
    return payload


async def get_ride_estimate(
    start: Tuple[float, float],
    end: Tuple[float, float],
    oauth_credentials: dict[str, Any],
) -> dict[str, Any]:
    def action(client: UberRidesClient) -> dict[str, Any]:
        price_response = client.get_price_estimates(
            start_latitude=start[0],
            start_longitude=start[1],
            end_latitude=end[0],
            end_longitude=end[1],
        )
        time_response = client.get_pickup_time_estimates(
            start_latitude=start[0],
            start_longitude=start[1],
        )
        return {
            "price_response": _response_json(price_response),
            "time_response": _response_json(time_response),
            "status_codes": {
                "price": getattr(price_response, "status_code", None),
                "time": getattr(time_response, "status_code", None),
            },
        }

    return await asyncio.to_thread(_with_client, oauth_credentials, action, "estimate")


async def book_ride(
    start: Tuple[float, float],
    end: Tuple[float, float],
    oauth_credentials: dict[str, Any],
) -> dict[str, Any]:
    def action(client: UberRidesClient) -> dict[str, Any]:
        response = client.request_ride(
            start_latitude=start[0],
            start_longitude=start[1],
            end_latitude=end[0],
            end_longitude=end[1],
        )
        payload = _response_json(response)
        payload["status_code"] = getattr(response, "status_code", None)
        return payload

    try:
        return await asyncio.to_thread(_with_client, oauth_credentials, action, "book")
    except Exception as exc:
        logger.exception("[UBER] book failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


async def get_ride_status(request_id: str, oauth_credentials: dict[str, Any]) -> dict[str, Any]:
    def action(client: UberRidesClient) -> dict[str, Any]:
        response = client.get_ride_details(ride_id=request_id)
        payload = _response_json(response)
        payload["status_code"] = getattr(response, "status_code", None)
        return payload

    try:
        return await asyncio.to_thread(_with_client, oauth_credentials, action, "status")
    except Exception as exc:
        logger.exception("[UBER] status failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def extract_map_url(payload: dict[str, Any]) -> str | None:
    for key in ("map_url", "href", "url"):
        value = payload.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value

    for value in payload.values():
        if isinstance(value, dict):
            nested = extract_map_url(value)
            if nested:
                return nested
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    nested = extract_map_url(item)
                    if nested:
                        return nested
    return None


async def get_ride_map(request_id: str, oauth_credentials: dict[str, Any]) -> dict[str, Any]:
    def action(client: UberRidesClient) -> dict[str, Any]:
        response = client.get_ride_map(ride_id=request_id)
        payload = _response_json(response)
        payload["status_code"] = getattr(response, "status_code", None)
        map_url = extract_map_url(payload)
        if map_url:
            payload["map_url"] = map_url
        return payload

    try:
        return await asyncio.to_thread(_with_client, oauth_credentials, action, "map")
    except Exception as exc:
        logger.exception("[UBER] map failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


async def cancel_ride(request_id: str, oauth_credentials: dict[str, Any]) -> dict[str, Any]:
    def action(client: UberRidesClient) -> dict[str, Any]:
        response = client.cancel_ride(ride_id=request_id)
        payload = _response_json(response)
        payload["status_code"] = getattr(response, "status_code", None)
        return payload

    try:
        return await asyncio.to_thread(_with_client, oauth_credentials, action, "cancel")
    except Exception as exc:
        logger.exception("[UBER] cancel failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
