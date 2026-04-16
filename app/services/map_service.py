import requests
import httpx
import json
import asyncio
from urllib.parse import quote, quote_plus
from typing import List, Tuple, Optional
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo



class Map_client():
    """ A wrapper for some of google maps web service apis, just to make my life easier.
        Requires an API key to work, also the most common services , like calculate route, 
        get distance between two points, and get the geocode of an address are implemented.
    """
    def __init__(self, API_KEY):
        self.api_key = API_KEY if API_KEY else None
        self.route_base_url = "https://routes.googleapis.com/directions/v2:computeRoutes"
        self.timezone_base_url = "https://maps.googleapis.com/maps/api/timezone/json"
        self.geocode_base_url = "https://geocode.googleapis.com/v4/geocode/address/"
        self.client = httpx.AsyncClient()

    async def get_geocode(self, address: str) -> Optional[Tuple[float, float]]:
        """
        Convert a human-readable address into geographic coordinates (latitude, longitude).
        
        Uses the Google Maps Geocoding API to resolve a street address to precise latitude 
        and longitude coordinates. This is essential for initializing route calculations and 
        timezone lookups.
        
        Args:
            address (str): The address to geocode. Accepts flexible formats such as:
                - "1600 Amphitheatre Parkway, Mountain View, CA"
                - "Times Square, New York"
                - "Eiffel Tower, Paris, France"
        
        Returns:
            Optional[Tuple[float, float]]: A tuple of (latitude, longitude) if the geocoding 
                is successful. Returns None if:
                - The address cannot be found
                - The API request fails
                - No results are returned from the API
        
        Raises:
            Exception: May raise exceptions if the HTTP request fails or the API key is invalid.
        
        Examples:
            >>> coords = await client.get_geocode("San Francisco, CA")
            >>> print(coords)
            (37.7749, -122.4194)
            
            >>> coords = await client.get_geocode("1600 Amphitheatre Parkway, Mountain View, CA")
            >>> if coords:
            ...     lat, lng = coords
            ...     print(f"Location: {lat}, {lng}")
        
        Note:
            - Requires a valid Google Maps API key
            - Results return the primary match for the address
            - API credentials must have Geocoding API enabled
        """
        #cleaning the address for url encoding
        address_fragment = quote_plus(address)
        #api_key_fragment = f"?key={self.api_key}"
        headers = {
            "X-Goog-Api-Key": self.api_key,
            "content-type": "application/json",
            "X-Goog-FieldMask": "results.location",
            "Accept": "application/json"
            }
        
        url = self.geocode_base_url + address_fragment  #should look like this sample: https://geocode.googleapis.com/v4/geocode/address/formatted_address?key=API_KEY 
        #print(f"Geocoding URL: {url}") #PLEASE REMOVE THIS DEBUG STATEMENT BEFORE DEPLOYING TO PRODUCTION
        try:
            response = await self.client.get(url, headers=headers)
            #print(response)
        except Exception as e:
            print(f"Error occurred while fetching geocode for {address}: {e}")
            return None
        if response.status_code == 200:
            data = response.json()
            #print(data)
            if "results" in data and len(data["results"]) > 0:
                location = data["results"][0]["location"]
                return (location["latitude"], location["longitude"])
            else:
                print(f"No geocode results found for address: {address}")
                return None

    async def get_timezone(self, location: Tuple[float, float]) -> Optional[str]:
        """
        Retrieve the timezone identifier for a given geographic location.
        
        Uses the Google Maps Timezone API to determine the IANA timezone ID for a specific 
        latitude/longitude pair. This is critical for correctly interpreting departure times 
        and scheduling routes in the local timezone.
        
        Args:
            location (Tuple[float, float]): A tuple of (latitude, longitude) representing the 
                geographic point for which to retrieve the timezone. Example: (37.7749, -122.4194) 
                for San Francisco.
        
        Returns:
            Optional[str]: The IANA timezone identifier (e.g., "America/Los_Angeles", 
                "Europe/London", "Asia/Tokyo") if successful. Returns None if:
                - The location is invalid
                - The API request fails
                - No timezone information is available for the location
        
        Raises:
            Exception: May raise exceptions if the HTTP request fails or the API key is invalid.
        
        Examples:
            >>> tz = await client.get_timezone((37.7749, -122.4194))  # San Francisco
            >>> print(tz)
            'America/Los_Angeles'
            
            >>> tz = await client.get_timezone((40.7128, -74.0060))  # New York
            >>> print(tz)
            'America/New_York'
        
        Note:
            - Requires a valid Google Maps API key with Timezone API enabled
            - Returns IANA timezone identifiers (standard format)
            - Useful for converting times to local timezone before route calculations
            - May be called automatically by calculate_route()
        """
        lat, lng = location
        location_fragment = quote_plus(f"{lat},{lng}")
        timestamp = int(asyncio.get_event_loop().time()) #current time in seconds since epoch
        url = f"{self.timezone_base_url}?location={location_fragment}&timestamp={timestamp}&key={self.api_key}"
        #print(f"Timezone URL: {url}") #PLEASE REMOVE THIS DEBUG STATEMENT BEFORE DEPLOYING TO PRODUCTION 
        #curl -L -X GET 'https://maps.googleapis.com/maps/api/timezone/json?location=39.6034810%2C-119.6822510×tamp=1733428634&key=YOUR_API_KEY'
        try:
            response = await self.client.get(url)
            #print(response)
        except Exception as e:
            print(f"Error occurred while fetching timezone for location {location}: {e}")
            return None
        if response.status_code == 200:
            data = response.json()
            #print(data)
            if "timeZoneId" in data:
                return data["timeZoneId"]
            else:
                print(f"No timezone found for location: {location}")
                return None
    

    async def calculate_route(self, origin:Tuple[float, float], destination:Tuple[float, float], stops:List[Tuple[float, float]] = None, mode:str = "drive",  departure_time:str = "now") -> Optional[dict]:
        """
        Compute an optimal traffic-aware route between two points with optional intermediate stops.
        
        Uses the Google Maps Routes API (v2) to calculate routes with real-time traffic awareness. 
        Automatically handles timezone conversion for the origin location and provides detailed 
        information including duration, distance, and turn-by-turn instructions.
        
        Args:
            origin (Tuple[float, float]): Starting point as (latitude, longitude). 
                Example: (37.7749, -122.4194) for San Francisco.
            destination (Tuple[float, float]): End point as (latitude, longitude).
                Example: (34.0522, -118.2437) for Los Angeles.
            stops (List[Tuple[float, float]], optional): List of intermediate waypoints as 
                (latitude, longitude) tuples. The route will pass through these points in order. 
                Defaults to None (direct route).
            mode (str, optional): Travel mode. Accepts:
                - "drive" (default): Car/vehicle routing
                - "walk": Pedestrian routing
                - "bike": Cycling routing
                Invalid values default to "drive".
            departure_time (str, optional): Desired departure time. Accepts:
                - "now" (default): Depart immediately (adds 2 minute buffer)
                - "14:30": 24-hour format time
                - "2:30PM": 12-hour format with AM/PM
                Times are interpreted in the origin location's timezone.
        
        Returns:
            Optional[dict]: A comprehensive route dictionary with structure:
                {
                    "routes": [
                        {
                            "duration": "4h 30m",
                            "distanceMeters": 559234,
                            "legs": [
                                {
                                    "duration": "1h 15m",
                                    "distanceMeters": 120000,
                                    "startLocation": {"latitude": 37.7749, "longitude": -122.4194},
                                    "endLocation": {"latitude": 35.5795, "longitude": -120.6625},
                                    "steps": [...]
                                }
                            ]
                        }
                    ]
                }
                Returns None if route calculation fails.
        
        Raises:
            Exception: May raise exceptions if:
                - Timezone lookup fails
                - HTTP API request fails
                - API key is invalid or unauthorized
        
        Examples:
            >>> # Simple route from SF to LA
            >>> route = await client.calculate_route(
            ...     (37.7749, -122.4194), 
            ...     (34.0522, -118.2437)
            ... )
            
            >>> # Route with intermediate stop
            >>> route = await client.calculate_route(
            ...     (37.7749, -122.4194),
            ...     (34.0522, -118.2437),
            ...     stops=[(35.5795, -120.6625)]  # Bakersfield
            ... )
            
            >>> # Route at specific departure time
            >>> route = await client.calculate_route(
            ...     (37.7749, -122.4194),
            ...     (34.0522, -118.2437),
            ...     departure_time="2:30PM"
            ... )
        
        Note:
            - Route is TRAFFIC_AWARE and provides real-time traffic conditions
            - Multiple alternative routes may be returned
            - Departure time uses a 2-minute buffer from specified time
            - Automatically fetches timezone for origin location
            - Invalid departure times default to "now"
            - Requires Google Maps API key with Routes API v2 enabled
        """
        allowed_modes = ["drive", "walk", "bike"]
        if mode not in allowed_modes:
           mode = "drive" #default to drive if invalid mode is provided
        #time shenanigans
        tz = await self.get_timezone(origin)
        tz_info = ZoneInfo(tz)

        if departure_time == "now":
            dt = datetime.now(tz_info) + timedelta(minutes=2)

        else:
            try:
                # handle "8:00pm", "20:00", "8:30 AM" style inputs
                today = datetime.now(tz_info).date()
                parsed_time = datetime.strptime(departure_time.strip().upper(), "%I:%M%p").time()  # e.g "8:00PM"
                dt = datetime.combine(today, parsed_time, tzinfo=tz_info) + timedelta(minutes=2)
            except ValueError:
                try:
                    # fallback: try 24hr format e.g "20:00"
                    today = datetime.now(tz_info).date()
                    parsed_time = datetime.strptime(departure_time.strip(), "%H:%M").time()
                    dt = datetime.combine(today, parsed_time, tzinfo=tz_info) + timedelta(minutes=2)
                except ValueError:
                    print(f"Invalid departure time: {departure_time}. Defaulting to now.")
                    dt = datetime.now(tz_info) + timedelta(minutes=2)

        departure_time = dt.isoformat()        
        #construct the request body
        body = {
                "origin": 
                {
                    "location": 
                    {
                        "latLng": 
                            {
                                "latitude": origin[0], 
                                "longitude": origin[1]
                            }
                    }
                },
                "destination": 
                {
                    "location": 
                    {
                        "latLng": 
                        {
                            "latitude": destination[0], 
                            "longitude": destination[1]
                        }
                    }
                },
                        
            "travelMode": mode.upper(),
            "routingPreference":"TRAFFIC_AWARE",
            "computeAlternativeRoutes": True,
            "departureTime": departure_time
        }
        if stops:
            points  = []
            for i in range(len(stops)):
                points.append({
                    "location": 
                    {
                        "latLng": 
                        {
                            "latitude": stops[i][0], 
                            "longitude": stops[i][1]
                        }
                    }
                })
            body["intermediates"] = points 

        headers = {
                    "Content-Type": "application/json",
                    "X-Goog-Api-Key": self.api_key,
                    "X-Goog-FieldMask": (
                        "routes.duration,"
                        "routes.distanceMeters,"
                        "routes.legs.duration,"
                        "routes.legs.distanceMeters,"
                        "routes.legs.startLocation,"
                        "routes.legs.endLocation,"
                        "routes.legs.steps.navigationInstruction,"
                        "routes.legs.steps.distanceMeters,"
                        "routes.legs.steps.localizedValues"
                            )
                    }
        response = await self.client.post(self.route_base_url, headers=headers, json=body)
        if response.status_code == 200:
            print(response)
            return response.json()
        else:
            print(f"Error calculating route: {response.status_code} - {response.text}")
            return None
        
    