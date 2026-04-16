"""
Test script for the Map_client service
Tests geocoding, timezone lookup, and route calculation functionality
"""

import asyncio
import os
from dotenv import load_dotenv
from app.services.map_service import Map_client

# Load environment variables
load_dotenv()

async def test_geocode():
    """Test the geocoding functionality"""
    print("\n" + "="*60)
    print("TEST 1: Geocoding")
    print("="*60)
    
    api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not api_key:
        print("❌ FAILED: GOOGLE_MAPS_API_KEY not found in .env")
        return False
    
    client = Map_client(api_key)
    
    # Test address
    test_address = "1600 Amphitheatre Parkway, Mountain View, CA"
    print(f"Testing geocoding for: {test_address}")
    
    result = await client.get_geocode(test_address)
    
    if result:
        lat, lng = result
        print(f"✅ SUCCESS: Got coordinates - Lat: {lat}, Lng: {lng}")
        return True
    else:
        print(f"❌ FAILED: Could not geocode address")
        return False


async def test_timezone():
    """Test the timezone lookup functionality"""
    print("\n" + "="*60)
    print("TEST 2: Timezone Lookup")
    print("="*60)
    
    api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not api_key:
        print("❌ FAILED: GOOGLE_MAPS_API_KEY not found in .env")
        return False
    
    client = Map_client(api_key)
    
    # Test location (San Francisco)
    test_location = (37.7749, -122.4194)
    print(f"Testing timezone for location: {test_location}")
    
    result = await client.get_timezone(test_location)
    
    if result:
        print(f"✅ SUCCESS: Got timezone - {result}")
        return True
    else:
        print(f"❌ FAILED: Could not get timezone for location")
        return False


async def test_calculate_route():
    """Test the route calculation functionality"""
    print("\n" + "="*60)
    print("TEST 3: Route Calculation")
    print("="*60)
    
    api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not api_key:
        print("❌ FAILED: GOOGLE_MAPS_API_KEY not found in .env")
        return False
    
    client = Map_client(api_key)
    
    # Test route from San Francisco to Los Angeles
    origin = (37.7749, -122.4194)  # San Francisco
    destination = (34.0522, -118.2437)  # Los Angeles
    
    print(f"Testing route calculation:")
    print(f"  Origin: San Francisco {origin}")
    print(f"  Destination: Los Angeles {destination}")
    print(f"  Mode: drive")
    
    result = await client.calculate_route(origin, destination, mode="drive")
    
    if result:
        print(f"✅ SUCCESS: Got route information")
        # Print some basic route details
        if "routes" in result and len(result["routes"]) > 0:
            route = result["routes"][0]
            if "duration" in route:
                print(f"   Duration: {route['duration']}")
            if "distanceMeters" in route:
                distance_km = route['distanceMeters'] / 1000
                print(f"   Distance: {distance_km:.2f} km")
        return True
    else:
        print(f"❌ FAILED: Could not calculate route")
        return False


async def test_route_with_stops():
    """Test route calculation with intermediate stops"""
    print("\n" + "="*60)
    print("TEST 4: Route with Intermediate Stops")
    print("="*60)
    
    api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not api_key:
        print("❌ FAILED: GOOGLE_MAPS_API_KEY not found in .env")
        return False
    
    client = Map_client(api_key)
    
    # Test route with stops
    origin = (37.7749, -122.4194)  # San Francisco
    destination = (34.0522, -118.2437)  # Los Angeles
    stops = [(35.5795, -120.6625)]  # Bakersfield as intermediate stop
    
    print(f"Testing route with stops:")
    print(f"  Origin: San Francisco {origin}")
    print(f"  Stop: Bakersfield {stops[0]}")
    print(f"  Destination: Los Angeles {destination}")
    
    result = await client.calculate_route(origin, destination, stops=stops, mode="drive")
    
    if result:
        print(f"✅ SUCCESS: Got route with stops")
        return True
    else:
        print(f"❌ FAILED: Could not calculate route with stops")
        return False


async def main():
    """Run all tests"""
    print("\n" + "#"*60)
    print("# MAP SERVICE TEST SUITE")
    print("#"*60)
    
    results = {}
    
    try:
        results["Geocoding"] = await test_geocode()
    except Exception as e:
        print(f"❌ EXCEPTION in Geocoding test: {e}")
        results["Geocoding"] = False
    
    try:
        results["Timezone"] = await test_timezone()
    except Exception as e:
        print(f"❌ EXCEPTION in Timezone test: {e}")
        results["Timezone"] = False
    
    try:
        results["Route Calculation"] = await test_calculate_route()
    except Exception as e:
        print(f"❌ EXCEPTION in Route Calculation test: {e}")
        results["Route Calculation"] = False
    
    try:
        results["Route with Stops"] = await test_route_with_stops()
    except Exception as e:
        print(f"❌ EXCEPTION in Route with Stops test: {e}")
        results["Route with Stops"] = False
    
    # Summary
    print("\n" + "#"*60)
    print("# TEST SUMMARY")
    print("#"*60)
    
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    
    for test_name, passed_test in results.items():
        status = "✅ PASSED" if passed_test else "❌ FAILED"
        print(f"{test_name}: {status}")
    
    print(f"\nTotal: {passed}/{total} tests passed")
    print("#"*60 + "\n")
    
    return passed == total


if __name__ == "__main__":
    # Run the async main function
    success = asyncio.run(main())
    exit(0 if success else 1)
