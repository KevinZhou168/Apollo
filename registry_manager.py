"""
registry_manager.py — Manage the Modal.Dict MCP tool registry.

Usage:
    # List all registered MCPs
    modal run registry_manager.py --action list
    
    # Add an MCP to the registry
    modal run registry_manager.py --action add --name "weather-mcp" --url "https://..."
    
    # Remove an MCP from the registry
    modal run registry_manager.py --action remove --name "weather-mcp"
    
    # Clear the entire registry (use with caution!)
    modal run registry_manager.py --action clear
    
    # Test connectivity to all registered MCPs
    modal run registry_manager.py --action test

The registry is a Modal.Dict with the following structure:
    {
        "mcp-server-name": "https://workspace--app-name-web.modal.run",
        ...
    }
"""

import sys
import json
import modal
import requests

REGISTRY_NAME = "mcp-tool-registry"

app = modal.App("registry-manager")


# ── Registry Operations ────────────────────────────────────────────────────────

def list_registry():
    """List all MCPs in the registry."""
    try:
        registry = modal.Dict.from_name(REGISTRY_NAME, create_if_missing=False)
        items = dict(registry)
    except modal.exception.NotFoundError:
        print(f"Registry '{REGISTRY_NAME}' does not exist yet.")
        print(f"It will be created automatically when you add the first MCP.")
        return
    
    if not items:
        print(f"Registry '{REGISTRY_NAME}' is empty.")
        return
    
    print(f"\n{'='*70}")
    print(f" MCP TOOL REGISTRY ({len(items)} entries)")
    print(f"{'='*70}\n")
    
    for name, url in sorted(items.items()):
        print(f"  {name}")
        print(f"    → {url}")
    
    print(f"\n{'='*70}\n")


def add_to_registry(name: str, url: str):
    """Add an MCP to the registry."""
    if not name or not url:
        print("Error: Both --name and --url are required for add action.")
        sys.exit(1)
    
    # Validate URL format
    if not url.startswith("https://"):
        print("Error: URL must start with https://")
        sys.exit(1)
    
    registry = modal.Dict.from_name(REGISTRY_NAME, create_if_missing=True)
    
    # Check if already exists
    if name in registry:
        print(f"Warning: '{name}' already exists in registry with URL:")
        print(f"  {registry[name]}")
        response = input(f"Overwrite? (y/N): ").strip().lower()
        if response != 'y':
            print("Cancelled.")
            return
    
    registry[name] = url
    print(f"\n✓ Added to registry:")
    print(f"  {name} → {url}\n")


def remove_from_registry(name: str):
    """Remove an MCP from the registry."""
    if not name:
        print("Error: --name is required for remove action.")
        sys.exit(1)
    
    try:
        registry = modal.Dict.from_name(REGISTRY_NAME, create_if_missing=False)
    except modal.exception.NotFoundError:
        print(f"Registry '{REGISTRY_NAME}' does not exist.")
        return
    
    if name not in registry:
        print(f"Error: '{name}' not found in registry.")
        return
    
    url = registry[name]
    del registry[name]
    
    print(f"\n✓ Removed from registry:")
    print(f"  {name} (was: {url})\n")


def clear_registry():
    """Clear the entire registry."""
    try:
        registry = modal.Dict.from_name(REGISTRY_NAME, create_if_missing=False)
        count = len(dict(registry))
    except modal.exception.NotFoundError:
        print(f"Registry '{REGISTRY_NAME}' does not exist.")
        return
    
    if count == 0:
        print(f"Registry is already empty.")
        return
    
    print(f"Warning: This will delete all {count} entries from the registry.")
    response = input("Are you sure? (yes/N): ").strip().lower()
    
    if response != 'yes':
        print("Cancelled.")
        return
    
    registry.clear()
    print(f"\n✓ Cleared {count} entries from registry.\n")


def test_registry():
    """Test connectivity to all registered MCPs."""
    try:
        registry = modal.Dict.from_name(REGISTRY_NAME, create_if_missing=False)
        items = dict(registry)
    except modal.exception.NotFoundError:
        print(f"Registry '{REGISTRY_NAME}' does not exist.")
        return
    
    if not items:
        print(f"Registry is empty.")
        return
    
    print(f"\n{'='*70}")
    print(f" TESTING {len(items)} MCP ENDPOINTS")
    print(f"{'='*70}\n")
    
    success_count = 0
    fail_count = 0
    
    for name, url in sorted(items.items()):
        print(f"Testing: {name}")
        print(f"  URL: {url}")
        
        try:
            # Try to list tools
            response = requests.post(
                f"{url}/mcp/",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/list",
                    "params": {}
                },
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream"
                },
                timeout=10,
            )
            response.raise_for_status()
            
            # Parse response (may be SSE or plain JSON)
            response_text = response.text.strip()
            
            # Check if it's SSE format
            if response_text.startswith('event:'):
                # SSE format: "event: message\ndata: {...}"
                lines = response_text.split('\n')
                for line in lines:
                    if line.startswith('data: '):
                        data = json.loads(line[6:])
                        break
                else:
                    data = response.json()
            else:
                data = response.json()
            
            if "error" in data:
                print(f"  ✗ MCP Error: {data['error']}")
                fail_count += 1
            else:
                tools = data.get("result", {}).get("tools", [])
                print(f"  ✓ Online — {len(tools)} tool(s) available")
                for tool in tools:
                    print(f"    • {tool['name']}")
                success_count += 1
        
        except requests.exceptions.Timeout:
            print(f"  ✗ Timeout (endpoint may be cold-starting)")
            fail_count += 1
        
        except requests.exceptions.ConnectionError:
            print(f"  ✗ Connection failed (endpoint may be stopped)")
            fail_count += 1
        
        except Exception as e:
            print(f"  ✗ Error: {e}")
            fail_count += 1
        
        print()
    
    print(f"{'='*70}")
    print(f" Results: {success_count} online, {fail_count} offline")
    print(f"{'='*70}\n")


# ── Auto-register from tools_builder output ────────────────────────────────────

def auto_register_from_generated():
    """
    Auto-discover and register MCPs from the generated_mcps/ folder.
    This reads the app names from the Python files and constructs URLs.
    """
    import re
    from pathlib import Path
    
    generated_dir = Path(__file__).parent / "generated_mcps"
    
    if not generated_dir.exists():
        print(f"Directory not found: {generated_dir}")
        return
    
    mcp_files = list(generated_dir.glob("*_mcp.py"))
    
    if not mcp_files:
        print(f"No MCP files found in {generated_dir}")
        return
    
    print(f"\n{'='*70}")
    print(f" AUTO-REGISTERING MCPs FROM generated_mcps/")
    print(f"{'='*70}\n")
    
    # Get Modal workspace name
    import subprocess
    result = subprocess.run(
        ["modal", "profile", "current"],
        capture_output=True,
        text=True,
    )
    
    if result.returncode != 0:
        print("Error: Could not determine Modal workspace. Run 'modal profile current'")
        return
    
    # The output is just the workspace name (e.g., "kevinzhou168")
    workspace = result.stdout.strip()
    
    if not workspace:
        print("Error: Workspace name required")
        return
    
    registry = modal.Dict.from_name(REGISTRY_NAME, create_if_missing=True)
    registered = 0
    
    for filepath in sorted(mcp_files):
        # Extract app name from file
        content = filepath.read_text()
        
        # Match the actual app definition, not comments
        # Look for: app = modal.App("name")
        match = re.search(r'^app\s*=\s*modal\.App\(["\']([^"\']+)["\']\)', content, re.MULTILINE)
        
        if not match:
            print(f"  ⚠ Could not find app name in {filepath.name}")
            continue
        
        app_name = match.group(1)
        
        # Construct URL
        # Format: https://{workspace}--{app-name}-web.modal.run
        url = f"https://{workspace}--{app_name}-web.modal.run"
        
        # Use app name as registry key
        registry_key = app_name
        
        print(f"  {registry_key}")
        print(f"    → {url}")
        
        registry[registry_key] = url
        registered += 1
    
    print(f"\n✓ Registered {registered} MCP(s)\n")


# ── Modal Entrypoint ───────────────────────────────────────────────────────────

@app.local_entrypoint()
def main(
    action: str = "",
    name: str = "",
    url: str = "",
):
    """
    Manage the MCP tool registry.
    
    Args:
        action: Action to perform (list, add, remove, clear, test, auto-register)
        name: MCP name (for add/remove)
        url: MCP endpoint URL (for add)
    """
    if not action:
        print(__doc__)
        return
    
    action = action.lower()
    
    if action == "list":
        list_registry()
    elif action == "add":
        add_to_registry(name, url)
    elif action == "remove":
        remove_from_registry(name)
    elif action == "clear":
        clear_registry()
    elif action == "test":
        test_registry()
    elif action == "auto-register":
        auto_register_from_generated()
    else:
        print(f"Unknown action: {action}")
        print("Valid actions: list, add, remove, clear, test, auto-register")
        sys.exit(1)


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Manage MCP tool registry")
    parser.add_argument("--action", required=True, 
                       choices=["list", "add", "remove", "clear", "test", "auto-register"])
    parser.add_argument("--name", default="")
    parser.add_argument("--url", default="")
    
    args = parser.parse_args()
    
    # For direct CLI usage (without Modal)
    print("Note: Run with 'modal run registry_manager.py ...' for proper Modal Dict access")
