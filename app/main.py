from typing import List
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.services.map_service import Map_client 
from dotenv import load_dotenv
 
import asyncio
from contextlib import asynccontextmanager

load_dotenv() 
from mcp.server import FastMCP

my_maps_client = Map_client(os.getenv("GOOGLE_MAPS_API_KEY"))

@asynccontextmanager
async def lifespan(server):
    # startup
    yield
    # shutdown
    await my_maps_client.aclose()
app = FastMCP("navigation and route planning", lifespan=lifespan)
@app.tool()
async def get_route_info(start_address: str, end_address: str, intermediate_stops:List[str]= None,  departure_time: str = "now"):
    """
    Calculate the optimal route between two addresses with optional intermediate stops.
    
    Uses the Google Maps Routes API to compute a traffic-aware route from a starting point 
    to a destination. Automatically geocodes all provided addresses and returns detailed 
    route information including distance, duration, and turn-by-turn instructions.
    
    Args:
        start_address (str): The starting address for the route. Can be a full address 
            (e.g., "1600 Amphitheatre Parkway, Mountain View, CA") or any valid address format.
        end_address (str): The destination address for the route. Must be a valid address.
        intermediate_stops (List[str], optional): A list of addresses for intermediate stops 
            along the route. Defaults to None (no intermediate stops).
        departure_time (str, optional): The desired departure time. Accepts:
            - "now": Depart immediately (default)
            - "HH:MM" or "HH:MMAM/PM" format (e.g., "14:30", "2:30PM")
            Defaults to "now".
    
    Returns:
        dict: A dictionary containing route information with the following structure:
            {
                "routes": [
                    {
                        "duration": "duration string",
                        "distanceMeters": int,
                        "legs": [
                            {
                                "duration": "leg duration string",
                                "distanceMeters": int,
                                "startLocation": {"latitude": float, "longitude": float},
                                "endLocation": {"latitude": float, "longitude": float},
                                "steps": [...]
                            }
                        ]
                    }
                ]
            }
            Returns None if the route calculation fails.
    
    Raises:
        Exception: May raise exceptions if geocoding fails or API calls encounter errors.
    
    Examples:
        >>> # Simple route from SF to LA
        >>> route = await get_route_info("San Francisco, CA", "Los Angeles, CA")
        
        >>> # Route with a stop in Bakersfield
        >>> route = await get_route_info(
        ...     "San Francisco, CA",
        ...     "Los Angeles, CA",
        ...     intermediate_stops=["Bakersfield, CA"]
        ... )
        
        >>> # Route departing at 2:30 PM
        >>> route = await get_route_info(
        ...     "New York, NY",
        ...     "Boston, MA",
        ...     departure_time="2:30PM"
        ... )
    
    Note:
        - All addresses are automatically geocoded to latitude/longitude coordinates
        - The route is traffic-aware and may provide multiple alternative routes
        - Departure time is interpreted in the timezone of the origin location
        - Requires valid Google Maps API credentials in the environment
    """
    geocode_a = await my_maps_client.get_geocode(start_address)
    print(f"Start geocode: {geocode_a}")
    print(await my_maps_client.get_timezone(geocode_a))
    geocode_b = await my_maps_client.get_geocode(end_address)
    print(f"End geocode: {geocode_b}")
    #intermediat stops are expected to be a list of addresses, so we need to geocode them as well
    intermediate_stops = intermediate_stops or []
    for i, stop in enumerate(intermediate_stops):
        geocode_stop = await my_maps_client.get_geocode(stop)
        print(f"Intermediate stop {i} geocode: {geocode_stop}")
        intermediate_stops[i] = geocode_stop

    route_data = await my_maps_client.calculate_route(geocode_a, geocode_b, stops=intermediate_stops, departure_time=departure_time)
    return route_data

def main():
    app.run(transport="stdio")
if __name__ =="__main__":
    main()

