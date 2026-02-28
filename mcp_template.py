"""
================================================================================
MODAL MCP SERVER TEMPLATE
================================================================================
This template lets you deploy any MCP (Model Context Protocol) server on Modal.
MCP allows AI models like Claude to call your custom tools over HTTP.

DEPLOY COMMAND:
    modal deploy mcp_template.py

AFTER DEPLOYING, your server is live at:
    https://{your-modal-workspace}--{app-name}-web.modal.run/mcp/

HOW TO ADAPT THIS TEMPLATE:
  1. Change the app name                     (Step 1)
  2. Add/remove Python dependencies          (Step 2)
  3. Change the MCP server display name      (Step 3)
  4. Define your tools inside make_mcp_server (Step 4)
  5. Do NOT touch the web() function         (Step 5)
================================================================================
"""

import modal

# ==============================================================================
# STEP 1 — NAME YOUR MODAL APP
# ==============================================================================
# Rules:
#   - Lowercase letters, numbers, and hyphens only. No underscores, no spaces.
#   - Must be unique within your Modal workspace.
#   - This name appears in your public URL:
#       https://{workspace}--{app-name}-web.modal.run/mcp/
#
# Examples:
#   modal.App("weather-mcp-server")
#   modal.App("github-tools-mcp")
#   modal.App("my-company-mcp-server")
# ==============================================================================
app = modal.App("your-mcp-server-name")  # <-- CHANGE THIS


# ==============================================================================
# STEP 2 — DEFINE YOUR CONTAINER IMAGE (DEPENDENCIES)
# ==============================================================================
# This defines the Python environment your tools run in.
# fastapi, fastmcp, and pydantic are REQUIRED — do not remove them.
# Add any other pip packages your tools need.
#
# Rules:
#   - Always pin versions (e.g. "requests==2.31.0") for reproducibility.
#   - Find the latest version of any package at: https://pypi.org
#   - Add each package as a separate string in the list.
#
# Examples of packages you might add:
#   "requests==2.31.0"         # HTTP calls to external APIs
#   "boto3==1.34.0"            # AWS SDK
#   "openai==1.30.0"           # OpenAI SDK
#   "anthropic==0.30.0"        # Anthropic SDK
#   "sqlalchemy==2.0.30"       # Database ORM
#   "beautifulsoup4==4.12.3"   # HTML parsing / web scraping
#   "pandas==2.2.2"            # Data manipulation
# ==============================================================================
image = modal.Image.debian_slim(python_version="3.12").uv_pip_install(
    # --- REQUIRED (do not remove) ---
    "fastapi==0.115.14",
    "fastmcp==2.10.6",
    "pydantic==2.11.10",
    # --- ADD YOUR PACKAGES BELOW ---
    # "requests==2.31.0",
    # "boto3==1.34.0",
)


# ==============================================================================
# STEP 3 & 4 — DEFINE YOUR MCP SERVER AND TOOLS
# ==============================================================================
# All tools must be defined inside make_mcp_server().
# All imports used by your tools must be inside the functions that use them
# (not at the top of this file), because they run inside the Modal container.
#
# TOOL RULES:
#   - Every tool must be an async function decorated with @mcp.tool()
#   - Every parameter must have a type annotation
#   - Every tool must have a return type annotation
#   - Every tool must have a docstring (this is what the AI reads to understand
#     the tool — write it clearly)
#   - Supported parameter types: str, int, float, bool
#     For optional params, provide a default value (e.g. timezone: str = "UTC")
#   - Supported return types: str, int, float, bool, list, dict
#   - Raise ValueError for invalid inputs — the AI will see the error message
#     and can retry with corrected arguments
#   - Import libraries inside the tool function body, not at the top of the file
#
# DOCSTRING FORMAT (required):
#   """One-line summary of what the tool does.
#
#   Args:
#       param_name: Description of what this parameter is.
#       param_name: Description. Defaults to X.
#
#   Returns:
#       Description of what is returned.
#   """
# ==============================================================================
def make_mcp_server():
    from fastmcp import FastMCP

    # --------------------------------------------------------------------------
    # STEP 3 — NAME YOUR MCP SERVER
    # --------------------------------------------------------------------------
    # This is the display name shown to AI clients. It does not affect the URL.
    # Use a human-readable name describing what this server does.
    #
    # Examples:
    #   FastMCP("Weather Tools")
    #   FastMCP("GitHub Repository Assistant")
    #   FastMCP("Company Knowledge Base")
    # --------------------------------------------------------------------------
    mcp = FastMCP("Your MCP Server Name")  # <-- CHANGE THIS

    # --------------------------------------------------------------------------
    # STEP 4 — DEFINE YOUR TOOLS
    # --------------------------------------------------------------------------
    # Add as many tools as you need by copying the pattern below.
    # Each tool becomes a callable function the AI can invoke.
    #
    # EXAMPLE TOOL 1 — Simple tool, no external API, required parameter:
    #
    #   @mcp.tool()
    #   async def word_count(text: str) -> int:
    #       """Count the number of words in a string.
    #
    #       Args:
    #           text: The text to count words in.
    #
    #       Returns:
    #           The number of words in the text.
    #       """
    #       return len(text.split())
    #
    #
    # EXAMPLE TOOL 2 — External HTTP API call, optional parameter:
    #
    #   @mcp.tool()
    #   async def get_weather(city: str, units: str = "metric") -> str:
    #       """Get the current weather for a city.
    #
    #       Args:
    #           city: The name of the city (e.g. "Chicago", "London").
    #           units: Unit system to use — "metric" or "imperial". Defaults to "metric".
    #
    #       Returns:
    #           A description of the current weather including temperature and conditions.
    #       """
    #       import requests
    #
    #       if units not in ("metric", "imperial"):
    #           raise ValueError(f"Invalid units '{units}'. Must be 'metric' or 'imperial'.")
    #
    #       response = requests.get(
    #           "https://api.openweathermap.org/data/2.5/weather",
    #           params={"q": city, "units": units, "appid": "YOUR_API_KEY"},
    #       )
    #       data = response.json()
    #       return f"{data['weather'][0]['description']}, {data['main']['temp']}°"
    #
    #
    # EXAMPLE TOOL 3 — Returns structured data as a dict:
    #
    #   @mcp.tool()
    #   async def parse_url(url: str) -> dict:
    #       """Parse a URL into its components.
    #
    #       Args:
    #           url: The full URL to parse (e.g. "https://example.com/path?q=1").
    #
    #       Returns:
    #           A dict with keys: scheme, netloc, path, query, fragment.
    #       """
    #       from urllib.parse import urlparse, parse_qs
    #
    #       parsed = urlparse(url)
    #       return {
    #           "scheme": parsed.scheme,
    #           "netloc": parsed.netloc,
    #           "path": parsed.path,
    #           "query": parse_qs(parsed.query),
    #           "fragment": parsed.fragment,
    #       }
    #
    #
    # EXAMPLE TOOL 4 — Boolean parameter, input validation:
    #
    #   @mcp.tool()
    #   async def format_number(value: float, as_currency: bool = False) -> str:
    #       """Format a number for display.
    #
    #       Args:
    #           value: The number to format.
    #           as_currency: If True, formats as USD currency. Defaults to False.
    #
    #       Returns:
    #           The formatted number as a string.
    #       """
    #       if as_currency:
    #           return f"${value:,.2f}"
    #       return f"{value:,}"
    # --------------------------------------------------------------------------

    @mcp.tool()
    async def your_tool_name(param_one: str, param_two: int = 0) -> str:
        """One-line description of what this tool does.

        Args:
            param_one: Description of param_one.
            param_two: Description of param_two. Defaults to 0.

        Returns:
            Description of the return value.
        """
        # Import libraries here, not at the top of the file
        # Write your tool logic here
        # Raise ValueError for bad inputs — the AI will see the message
        raise NotImplementedError("Replace this with your tool logic.")

    # Add more tools here by repeating the @mcp.tool() pattern above

    return mcp


# ==============================================================================
# STEP 5 — DO NOT MODIFY BELOW THIS LINE
# ==============================================================================
# This wires up the MCP server to Modal's ASGI hosting layer.
# It never needs to change regardless of what tools you build.
# ==============================================================================
@app.function(image=image)
@modal.asgi_app()
def web():
    """ASGI web endpoint for the MCP server."""
    from fastapi import FastAPI

    mcp = make_mcp_server()
    mcp_app = mcp.http_app(transport="streamable-http", stateless_http=True)

    fastapi_app = FastAPI(lifespan=mcp_app.router.lifespan_context)
    fastapi_app.mount("/", mcp_app, "mcp")

    return fastapi_app
