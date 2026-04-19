import os
import sys
import json
import logging
from typing import List
from contextlib import asynccontextmanager

from cryptography.fernet import Fernet
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastmcp import FastMCP
from fastmcp.server.auth.providers.clerk import ClerkProvider
from key_value.aio.stores.redis import RedisStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from key_value.aio.wrappers.prefix_collections import PrefixCollectionsWrapper
from mcp.types import PromptMessage, TextContent
import redis.asyncio as aioredis
import httpx
from fastmcp.utilities.lifespan import combine_lifespans


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

redis_client = aioredis.from_url(
    os.environ["UPSTASH_REDIS_URL"],
    ssl_cert_reqs=None,
    decode_responses=False,
)
base_store = RedisStore(client=redis_client)

encrypted_oauth_store = FernetEncryptionWrapper(
    key_value=base_store,
    fernet=Fernet(os.environ["FASTMCP_ENCRYPTION_KEY"]),
)
#base_store    = RedisStore(client=redis_client)
#oauth_store   = PrefixCollectionsWrapper(base_store, prefix="oauth:")



# ─── Auth ───────────────────────────────────────────────────────────────────

CLERK_DOMAIN   = os.environ["CLERK_DOMAIN"]
MCP_SERVER_URL = os.environ["MCP_SERVER_URL"]

auth = ClerkProvider(
    domain=CLERK_DOMAIN,
    client_id=os.environ["CLERK_CLIENT_ID"],
    client_secret=os.environ["CLERK_CLIENT_SECRET"],
    base_url=MCP_SERVER_URL,
    #client_storage=encrypted_oauth_store,
)

# ─── Lifespan ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(server):
    await redis_client.initialize()
    print("✅ Redis client initialized")
    yield
    await my_maps_client.client.aclose()
    await redis_client.aclose()
    print("✅ Resources cleaned up, shutting down")

# ─── MCP server ─────────────────────────────────────────────────────────────

mcp = FastMCP("trafficly", lifespan=lifespan, auth=auth)

# ─── FastAPI wrapper ─────────────────────────────────────────────────────────



app = FastAPI(
    lifespan= mcp.http_app(path="/mcp").lifespan )
# ClerkProvider already exposes /.well-known/oauth-authorization-server
# internally. We only need to add the protected-resource doc manually
# because FastMCP mounts it under /mcp, not at root.
@app.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource():
    return JSONResponse({
        "resource": MCP_SERVER_URL,
        "authorization_servers": [f"https://{CLERK_DOMAIN}"],
    })

# Mount FastMCP last — catches everything else including its own
# /.well-known/oauth-authorization-server and /oauth/callback routes.
app.mount("/", mcp.http_app(path="/mcp"))

# ─── Tools ───────────────────────────────────────────────────────────────────

@mcp.tool()
async def get_route_info(
    start_address: str,
    end_address: str,
    intermediate_stops: List[str] = None,
    departure_time: str = "now",
):
    """Calculate the optimal route between two addresses with optional intermediate stops."""
    logger.info(f"[TOOL] get_route_info | {start_address} → {end_address}")

    geocode_a = await my_maps_client.get_geocode(start_address)
    geocode_b = await my_maps_client.get_geocode(end_address)

    intermediate_stops = intermediate_stops or []
    for i, stop in enumerate(intermediate_stops):
        intermediate_stops[i] = await my_maps_client.get_geocode(stop)

    route_data = await my_maps_client.calculate_route(
        geocode_a, geocode_b,
        stops=intermediate_stops,
        departure_time=departure_time,
    )
    logger.info(f"[TOOL] get_route_info success | routes={len(route_data.get('routes', []))}")
    return route_data

# ─── Prompts ─────────────────────────────────────────────────────────────────

@mcp.prompt()
def navigation_prompt(
    start: str,
    end: str,
    detail_level: str = "summary",
    departure_time: str = "now",
    stops: str = "",
) -> list[PromptMessage]:
    """
    Generate a navigation prompt for Trafficly.

    Args:
        start: The starting address or location name.
        end: The destination address or location name.
        detail_level: 'summary' for high-level overview or 'detailed' for turn-by-turn.
        departure_time: Desired departure time e.g. 'now' or '2:30PM'.
        stops: Comma-separated intermediate stops e.g. 'TBS Lagos, Union Bank Marina'.
    """
    stops_list = [s.strip() for s in stops.split(",") if s.strip()] if stops else []
    stops_text = f" with stops at {', '.join(stops_list)}" if stops_list else ""
    stops_arg  = json.dumps(stops_list)

    if detail_level == "detailed":
        presentation_instruction = (
            "Present the result as clear turn-by-turn directions. "
            "For each step include the maneuver, street name, and distance. "
            "Group steps by leg if there are multiple stops. "
            "End with total distance and estimated travel time."
        )
    else:
        presentation_instruction = (
            "Summarise the route in plain conversational language — like how a friend would describe it. "
            "Mention only major roads or landmarks, skip granular steps. "
            "End with total distance and estimated travel time."
        )

    prompt_text = f"""
You are a navigation assistant for Trafficly.

The user wants to travel from '{start}' to '{end}'{stops_text}, departing at {departure_time}.

## Step 1 — Fetch the route
Call the get_route_info tool with EXACTLY these arguments:
- start_address: "{start}"
- end_address: "{end}"
- intermediate_stops: {stops_arg}
- departure_time: "{departure_time}"

Do NOT proceed until you have the tool response.

## Step 2 — Use ONLY the tool data
- Never infer, guess, or recall road names from memory.
- Every road name, distance, and duration must come directly from the tool response.

## Step 3 — Present the route
{presentation_instruction}
""".strip()

    logger.info(f"[PROMPT] navigation_prompt | {start} → {end} stops={stops_list}")
    return [PromptMessage(role="user", content=TextContent(type="text", text=prompt_text))]