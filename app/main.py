import os
import sys
import json
import logging
from typing import List
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastmcp import FastMCP
from mcp.types import PromptMessage, TextContent
from fastapi import FastAPI
from fastmcp.server.auth.providers.clerk import ClerkProvider
from key_value.aio.stores.redis import RedisStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper


#logging.getLogger("fastmcp").setLevel(logging.DEBUG)
load_dotenv()

# Anchor log file to project directory, not cwd
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trafficly.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("trafficly")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.services.map_service import Map_client

my_maps_client = Map_client(os.getenv("GOOGLE_MAPS_API_KEY"))


@asynccontextmanager
async def lifespan(server):
    yield
    await my_maps_client.client.aclose()

redis_store = RedisStore.from_url(os.environ["UPSTASH_REDIS_URL"])
# Upstash requires SSL, so make sure your URL starts with rediss://

encrypted_store = FernetEncryptionWrapper(
    key_value=redis_store,
    encryption_key=os.environ["FASTMCP_ENCRYPTION_KEY"]
)

auth = ClerkProvider(
    domain=os.environ["CLERK_DOMAIN"],           
    client_id=os.environ["CLERK_CLIENT_ID"],
    client_secret=os.environ["CLERK_CLIENT_SECRET"],
    base_url=os.environ["MCP_SERVER_URL"],
    client_storage =  encrypted_store   
)
mcp = FastMCP("trafficly", lifespan=lifespan, auth=auth)
app = FastAPI()
@app.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource():
    return {
        "resource": os.environ["MCP_SERVER_URL"],
        "authorization_servers": [f"https://{os.environ['CLERK_DOMAIN']}"],
    }
app.mount("/",mcp.http_app(path="/mcp"))


@mcp.tool()
async def get_route_info(
    start_address: str,
    end_address: str,
    intermediate_stops: List[str] = None,
    departure_time: str = "now"
):
    """Calculate the optimal route between two addresses with optional intermediate stops."""
    logger.info(f"[TOOL] get_route_info called | start={start_address} end={end_address} stops={intermediate_stops} departure={departure_time}")
    
    geocode_a = await my_maps_client.get_geocode(start_address)
    geocode_b = await my_maps_client.get_geocode(end_address)

    intermediate_stops = intermediate_stops or []
    for i, stop in enumerate(intermediate_stops):
        intermediate_stops[i] = await my_maps_client.get_geocode(stop)

    route_data = await my_maps_client.calculate_route(
        geocode_a, geocode_b,
        stops=intermediate_stops,
        departure_time=departure_time
    )
    logger.info(f"[TOOL] get_route_info success | routes={len(route_data.get('routes', []))}")
    return route_data

@mcp.prompt()
def navigation_prompt(
    start: str,
    end: str,
    detail_level: str = "summary",
    departure_time: str = "now",
    stops: str = ""          # always str — MCP sends all prompt args as strings
) -> list[PromptMessage]:
    """
    Generate a navigation prompt for Trafficly.

    Args:
        start: The starting address or location name.
        end: The destination address or location name.
        detail_level: Either 'summary' for high-level overview or 'detailed' for turn-by-turn.
        departure_time: Desired departure time e.g. 'now' or '2:30PM'.
        stops: Comma-separated intermediate stops e.g. 'TBS Lagos, Union Bank Marina'.
    """
    # Parse the comma-separated string into a list
    stops_list = [s.strip() for s in stops.split(",") if s.strip()] if stops else []
    stops_text = f" with stops at {', '.join(stops_list)}" if stops_list else ""
    stops_arg = json.dumps(stops_list)

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
            "Mention only major roads or landmarks to pass through, skip granular steps. "
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
- If a road name is not in the tool response, do not mention it.

## Step 3 — Present the route
{presentation_instruction}
""".strip()

    logger.info(f"[PROMPT] navigation_prompt rendered | start={start} end={end} stops={stops_list} detail={detail_level}")

    return [
        PromptMessage(
            role="user",
            content=TextContent(type="text", text=prompt_text)
        )
    ]


#def main():
    #mcp.run(transport="http", host="0.0.0.0", port=8000)

#if __name__ == "__main__":
    #main()