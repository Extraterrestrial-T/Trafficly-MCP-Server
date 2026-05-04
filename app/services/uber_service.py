import json
import logging
import os
from typing import Any, Optional, Tuple
from urllib.parse import urlencode


logger = logging.getLogger("trafficly.uber")

LOOKING_URL = "https://m.uber.com/looking"
PRODUCT_SELECTION_URL = "https://m.uber.com/go/product-selection"
SUPPORTED_STYLES = {"looking", "product_selection"}


class UberDeepLinkError(RuntimeError):
    pass


def _deeplink_client_id() -> str:
    value = os.getenv("UBER_DEEPLINK_CLIENT_ID") or os.getenv("UBER_CLIENT_ID")
    if not value:
        raise UberDeepLinkError("UBER_DEEPLINK_CLIENT_ID or UBER_CLIENT_ID is required.")
    return value


def _deeplink_style() -> str:
    style = (os.getenv("UBER_DEEPLINK_STYLE") or "looking").strip().lower().replace("-", "_")
    return style if style in SUPPORTED_STYLES else "looking"


def _location_payload(
    point: Tuple[float, float],
    label: str | None = None,
    address: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "latitude": point[0],
        "longitude": point[1],
    }
    if label:
        payload["addressLine1"] = label
    if address:
        payload["addressLine2"] = address
    elif label:
        payload["addressLine2"] = label
    return payload


def _encoded_location(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def _normalize_dropoffs(
    end: Tuple[float, float],
    end_label: str | None = None,
    end_address: str | None = None,
    stops: Optional[list[Tuple[float, float]]] = None,
    stop_labels: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    dropoffs = []
    for index, stop in enumerate(stops or []):
        label = stop_labels[index] if stop_labels and index < len(stop_labels) else f"Stop {index + 1}"
        dropoffs.append(_location_payload(stop, label=label))
    dropoffs.append(_location_payload(end, label=end_label or "Destination", address=end_address))
    return dropoffs


def build_uber_deeplink(
    start: Tuple[float, float],
    end: Tuple[float, float],
    start_label: str | None = None,
    end_label: str | None = None,
    start_address: str | None = None,
    end_address: str | None = None,
    stops: Optional[list[Tuple[float, float]]] = None,
    stop_labels: Optional[list[str]] = None,
    product_id: str | None = None,
) -> dict[str, Any]:
    """Build an Uber mobile-web handoff URL without calling Uber APIs."""
    style = _deeplink_style()
    client_id = _deeplink_client_id()
    pickup = _location_payload(start, label=start_label or "Pickup", address=start_address)
    dropoffs = _normalize_dropoffs(end, end_label, end_address, stops, stop_labels)

    params: list[tuple[str, str]] = []
    if style == "looking":
        base_url = LOOKING_URL
        params.append(("client_id", client_id))
    else:
        base_url = PRODUCT_SELECTION_URL
        params.append(("source_tag", client_id))

    params.append(("pickup", _encoded_location(pickup)))
    for index, dropoff in enumerate(dropoffs):
        params.append((f"drop[{index}]", _encoded_location(dropoff)))

    if product_id:
        params.append(("product_id", product_id))

    uber_url = f"{base_url}?{urlencode(params)}"
    logger.info(
        "[UBER] deeplink built | style=%s stops=%s product_id_present=%s",
        style,
        max(0, len(dropoffs) - 1),
        bool(product_id),
    )
    return {
        "uber_url": uber_url,
        "url_style": style,
        "pickup": pickup,
        "dropoffs": dropoffs,
        "product_id": product_id,
    }
