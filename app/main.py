import os
import sys
import json
import logging
from typing import List, Optional
from contextlib import asynccontextmanager
from app.services.upstash_redis import UpstashRedis
from cryptography.fernet import Fernet
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastmcp import FastMCP
from fastmcp.dependencies import CurrentContext
from fastmcp.server.context import Context
from fastmcp.server.auth.providers.clerk import ClerkProvider
from key_value.aio.stores.redis import RedisStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from key_value.aio.wrappers.prefix_collections import PrefixCollectionsWrapper
from mcp.types import PromptMessage, TextContent
import redis.asyncio as aioredis
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential
import hashlib


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

upstash_redis = UpstashRedis(url=os.getenv("UPSTASH_REDIS_URL"), encryption_key=os.getenv("FASTMCP_ENCRYPTION_KEY"))


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

# ─── FastAPI wrapper ─────────────────────────────────────────────────────────


mcp_app = mcp.http_app(path="/mcp")
app = FastAPI(lifespan= mcp_app.lifespan )
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
app.mount("/", mcp_app)

# ─── Tools ───────────────────────────────────────────────────────────────────

@mcp.tool()
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=5))
async def get_route_info(
    start_address: str,
    end_address: str,
    intermediate_stops: Optional[List[str]] = None,
    departure_time: Optional[str] = "now",
    detail_level: Optional[str] = "summary",
    ctx:Context = CurrentContext(),
    
):
    
    """Calculate the optimal route between two addresses with optional intermediate stops.
        Now with caching and retry logic for improved reliability!
    Args:
    start_address: The starting point of the route (e.g. "1600 Amphithe atre Parkway, Mountain View, CA")
    end_address: The destination point of the route (e.g. "1 Infinite Loop, Cupertino, CA")
    intermediate_stops: Optional list of intermediate stops (e.g. ["TBS Lagos", "Union Bank Marina"])
    departure_time: Optional departure time (e.g. "now" or "2024-06-01T14:30:00")
    detail_level: "summary" for high-level overview, "detailed" for turn-by-turn directions
    Returns:
        The calculated route information, with description_detail level based on the input parameter. The result is cached in Redis for 1 hour to speed up repeated requests.
    """
    key = "route:" + hashlib.md5(
        f"{start_address.lower().strip()}|{end_address.lower().strip()}|{departure_time}|{",".join(intermediate_stops or []).lower().strip()}".encode()
    ).hexdigest()

    # Check cache first
    cached_route = await upstash_redis.base_redis_client.get(key)
    if cached_route:
        logger.info(f"[TOOL] get_route_info | {start_address} → {end_address} (cached)")
        return json.loads(cached_route)
    else: 
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
        upstash_redis.base_redis_client.set(key, json.dumps(route_data), ex=3600)  # Cache for 1 hour
        logger.info(f"[TOOL] get_route_info success | routes={len(route_data.get('routes', []))}")
    if detail_level == "detailed":
        additional_info = {"guidiance_prompt": 
                           ("The user requested detailed turn-by-turn directions. "
                           "Please include maneuvers, street names, and distances for each step, in an organized, friendly and helpful manner.")}
    else:
        additional_info = {"guidiance_prompt": 
                           ("The user requested a summary of the route. "
                            "Please provide a high-level overview mentioning only major roads or landmarks, and skip granular steps like turns."
                            "Provide the information in a clear and conversational manner, as if describing the route to a friend.")} 
    return route_data, additional_info

# ─── Prompts ─────────────────────────────────────────────────────────────────
##this one needs to go, not really doing anything.
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
        start: The starting address or location name.
        end: The destination address or location name.
        detail_level: 'summary' for high-level overview or 'detailed' for turn-by-turn.
        departure_time: Desired departure time e.g. 'now' or '2:30PM'.
        stops: Comma-separated intermediate stops e.g. 'TBS Lagos, Union Bank Marina'.
    """
    stops_list = [s.strip() for s in stops.split(",") if s.strip()] if stops else []
    stops_text = f" with stops at {', '.join(stops_list)}" if stops_list else ""
    stops_arg  = json.dumps(stops_list)

    prompt_text = f"""
        You are a navigation assistant called Trafficly.

        The user wants to travel from '{start}' to '{end}'{stops_text}, departing at {departure_time}.

        ## Step 1 — Fetch the route
        Call the get_route_info tool with EXACTLY these arguments:
        - start_address: "{start}"
        - end_address: "{end}"
        - intermediate_stops: {stops_arg}
        - departure_time: "{departure_time}"
        - detail_level: "{detail_level}"

        Do NOT proceed until you have the tool response.

        ## Step 2 — Use ONLY the tool data
        - Never infer, guess, or recall road names from memory.
        - Every road name, distance, and duration must come directly from the tool response.

        ## Step 3 — Present the route info while adharing to the additional instructions provided.
        """.strip()
    logger.info(f"[PROMPT] navigation_prompt | {start} → {end} stops={stops_list}")
    return prompt_text