# Trafficly MCP Server

Trafficly is a remote MCP server that gives AI assistants traffic-aware route planning, interactive map rendering, and Uber ride handoff links.

It is built with FastAPI, FastMCP, Clerk OAuth, Upstash Redis, Google Maps APIs, and lightweight HTML/CSS/JavaScript MCP app views.

## What Trafficly Does

- Calculates traffic-aware driving routes between real addresses.
- Supports intermediate stopovers, including named stops.
- Preserves route alternatives returned by Google Routes.
- Renders routes in an MCP app iframe with Leaflet maps, markers, route options, step lists, stopover labels, and direction hints.
- Builds prefilled Uber deep links for pickup, stops, and destination.
- Works as a remote MCP server for compatible clients such as Claude and ChatGPT MCP/App integrations.
- Serves a public landing page at `/`.

## Live Endpoint

Public MCP endpoint:

```text
https://trafficly-mcp-server.onrender.com/mcp
```

Public landing page:

```text
https://trafficly-mcp-server.onrender.com/
```

## MCP Tools

### `get_route_info`

Calculates a route between two addresses.

Typical input:

```json
{
  "start_address": "Eko Hotel, Victoria Island, Lagos",
  "end_address": "Alagomeji, Yaba, Lagos",
  "intermediate_stops": ["Union Bank, Marina, Lagos"],
  "departure_time": "10:20 AM",
  "detail_level": "detailed"
}
```

Returns route metadata and a `route_id`. The assistant should then call `show_route_map` with that `route_id`.

### `show_route_map`

Displays the cached route in the Trafficly map UI.

It returns a proper MCP `ToolResult` with:

- human-readable fallback text
- structured route payload
- `ui://trafficly/map.v2.html` metadata
- OpenAI `openai/outputTemplate` metadata

### `Uber_tool`

Builds a prefilled Uber handoff link.

Trafficly does not book rides directly. Uber handles sign-in, product selection, pricing, payment, and final booking.

Typical input:

```json
{
  "start": [6.4266, 3.4301],
  "end": [6.5000, 3.3792],
  "start_label": "Eko Hotel",
  "end_label": "Alagomeji",
  "stops": [[6.4541, 3.3947]],
  "stop_labels": ["Union Bank, Marina"]
}
```

## MCP UI Resources

Trafficly exposes two MCP app resources:

```text
ui://trafficly/map.v2.html
ui://trafficly/uber.html
```

The map UI supports:

- Google encoded polyline rendering
- route alternative selection
- origin, destination, and stopover markers
- turn-by-turn steps
- direction hints along the selected route
- Claude and ChatGPT app bridge fallbacks

The Uber UI supports:

- prefilled ride handoff display
- pickup, stops, and destination summary
- external "Continue in Uber" action
- MCP structured output rendering

## Architecture

```text
FastAPI app
  |
  +-- Public landing page at /
  |
  +-- FastMCP mounted at /mcp
        |
        +-- Clerk OAuth provider
        +-- MCP tools
        +-- MCP app resources

Google Maps APIs
  |
  +-- Geocoding
  +-- Timezone lookup
  +-- Routes API

Upstash Redis
  |
  +-- OAuth client storage
  +-- route cache

Uber
  |
  +-- m.uber.com deep links
```

## Requirements

- Python 3.11+
- Google Maps API key with access to:
  - Geocoding API
  - Time Zone API
  - Routes API
- Clerk OAuth application
- Upstash Redis database
- CARTO Basemaps API key
- Uber client ID or deep-link client ID

## Environment Variables

Create a `.env` file for local development:

```env
GOOGLE_MAPS_API_KEY=your_google_maps_api_key
CARTO_BASEMAP_API_KEY=your_carto_basemaps_api_key

UPSTASH_REDIS_URL=your_upstash_redis_url
FASTMCP_ENCRYPTION_KEY=your_fernet_key

CLERK_DOMAIN=your-clerk-domain.clerk.accounts.dev
CLERK_CLIENT_ID=your_clerk_client_id
CLERK_CLIENT_SECRET=your_clerk_client_secret

MCP_SERVER_URL=http://localhost:8000

UBER_DEEPLINK_CLIENT_ID=your_uber_client_id
UBER_DEEPLINK_STYLE=looking
```

`UBER_CLIENT_ID` can be used instead of `UBER_DEEPLINK_CLIENT_ID`.

`CARTO_BASEMAP_API_KEY` is used by the Leaflet map UI for CARTO basemap tiles.

Supported Uber link styles:

```text
looking
product_selection
```

## Local Development

Install dependencies:

```bash
pip install -r requirements.txt
```

Run the server:

```bash
uvicorn app.main:app --reload
```

Open:

```text
http://localhost:8000/
```

MCP endpoint:

```text
http://localhost:8000/mcp
```

## Claude Setup

Use the remote MCP URL:

```text
https://trafficly-mcp-server.onrender.com/mcp
```

For Claude Web or Claude Desktop remote connectors:

1. Open Claude settings.
2. Go to connectors/custom integrations.
3. Add a custom connector.
4. Paste the Trafficly MCP URL.
5. Click connect.
6. Complete the Clerk OAuth sign-in flow.

For local Claude Desktop configuration with `mcp-remote`:

```json
{
  "mcpServers": {
    "trafficly": {
      "command": "npx",
      "args": ["mcp-remote", "https://trafficly-mcp-server.onrender.com/mcp"]
    }
  }
}
```

## ChatGPT Setup

Use the remote MCP URL:

```text
https://trafficly-mcp-server.onrender.com/mcp
```

For ChatGPT workspaces with Developer Mode/custom MCP connector access:

1. Enable Developer Mode for custom MCP connectors.
2. Create a custom MCP app/connector.
3. Paste the Trafficly MCP URL.
4. Complete the Clerk OAuth sign-in flow.
5. Enable Trafficly in the conversation.

Availability depends on the ChatGPT plan and workspace settings.

## Example Prompt

```text
I want to get from Eko Hotel, Victoria Island to Alagomeji, Yaba.
I need to stop at Union Bank, Marina for a quick errand.
If I leave at 10:20 AM, can I make it in 2 hours?
Render the route on the map.
```

## Deployment

The project is configured to run with:

```bash
uvicorn app.main:app
```

`railpack.toml` currently uses:

```toml
[build]
cmd = "uvicorn app.main:app"
```

Set all required environment variables in the deployment provider.

For production, `MCP_SERVER_URL` must match the public base URL of the deployed service, for example:

```env
MCP_SERVER_URL=https://trafficly-mcp-server.onrender.com
```

## Notes

- Route data is cached in Redis for faster map rendering.
- The map UI resource is served as raw HTML through FastMCP.
- Uber integration uses deep links, not the deprecated Uber Rides SDK and not Guest Rides API booking.
- Trafficly uses Clerk OAuth for MCP client authentication.
- The public landing page is separate from the authenticated MCP endpoint.

## License

No license has been specified yet.
