"""
supervisor.py — Agentic supervisor that discovers and orchestrates MCP tools dynamically.

Usage:
    python supervisor.py --prompt "plan a trip to spain"
    python supervisor.py --prompt "what's the weather in madrid?" --test

How it works:
    1. Discovers available MCP tools from Modal.Dict registry
    2. Sends user prompt + available tools to LLM
    3. Executes tool calls via MCP JSON-RPC over HTTP
    4. Loops until LLM returns final answer (max 10 iterations)
    5. Returns comprehensive response to user

Prerequisites:
    - Modal.Dict named "mcp-tool-registry" with {mcp_name: endpoint_url} entries
    - Deployed MCP servers registered in the dictionary
    - ANTHROPIC_API_KEY or OPENAI_API_KEY set (based on LLM_PROVIDER)
"""

import json
import os
import sys
import subprocess
import time
from typing import Any
from pathlib import Path

import dotenv
import requests

dotenv.load_dotenv()

# Import modal only when needed for registry access
try:
    import modal
    MODAL_AVAILABLE = True
except ImportError:
    MODAL_AVAILABLE = False
    print("Warning: modal package not installed. Registry access will not work.")

# ── Configuration ──────────────────────────────────────────────────────────────

#LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai").lower()
LLM_PROVIDER = "anthropic"
if LLM_PROVIDER not in ("anthropic", "openai"):
    print(f"Error: LLM_PROVIDER must be 'anthropic' or 'openai', got '{LLM_PROVIDER}'", file=sys.stderr)
    sys.exit(1)

MAX_ITERATIONS = 10  # Prevent infinite loops
REGISTRY_NAME = "mcp-tool-registry"
REQUEST_TIMEOUT = 30  # seconds for MCP HTTP calls

# ── MCP Communication ──────────────────────────────────────────────────────────

def parse_sse_response(response_text: str) -> dict:
    """
    Parse Server-Sent Events (SSE) response from MCP servers.
    
    Args:
        response_text: Raw SSE response text (e.g., "event: message\ndata: {...}")
        
    Returns:
        Parsed JSON data from the SSE data field.
    """
    import json
    
    # SSE format: "event: message\ndata: {...}\n\n"
    lines = response_text.strip().split('\n')
    for line in lines:
        if line.startswith('data: '):
            data_json = line[6:]  # Remove "data: " prefix
            return json.loads(data_json)
    
    # If no SSE format, try parsing as plain JSON
    return json.loads(response_text)


def list_mcp_tools(endpoint_url: str) -> list[dict]:
    """
    Call the MCP server's tools/list endpoint to discover available tools.
    
    Args:
        endpoint_url: Base URL of the MCP server (e.g., https://...--my-mcp-web.modal.run)
        
    Returns:
        List of tool definitions in MCP format.
        
    Raises:
        requests.RequestException: On HTTP errors.
    """
    response = requests.post(
        f"{endpoint_url}/mcp/",
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
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    
    # Parse response (may be SSE or plain JSON)
    data = parse_sse_response(response.text)
    
    if "error" in data:
        raise RuntimeError(f"MCP error from {endpoint_url}: {data['error']}")
    
    return data.get("result", {}).get("tools", [])


def call_mcp_tool(endpoint_url: str, tool_name: str, arguments: dict) -> Any:
    """
    Execute a tool on an MCP server via JSON-RPC.
    
    Args:
        endpoint_url: Base URL of the MCP server.
        tool_name: Name of the tool to call.
        arguments: Dictionary of arguments to pass to the tool.
        
    Returns:
        The tool's return value (parsed from JSON).
        
    Raises:
        requests.RequestException: On HTTP errors.
        RuntimeError: On MCP protocol errors.
    """
    response = requests.post(
        f"{endpoint_url}/mcp/",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments
            }
        },
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    
    # Parse response (may be SSE or plain JSON)
    data = parse_sse_response(response.text)
    
    if "error" in data:
        error_msg = data["error"].get("message", str(data["error"]))
        raise RuntimeError(f"MCP tool error ({tool_name}): {error_msg}")
    
    # MCP returns result.content as a list of content blocks
    result = data.get("result", {})
    content = result.get("content", [])
    
    if not content:
        return None
    
    # Extract text from first content block
    if isinstance(content, list) and len(content) > 0:
        return content[0].get("text", str(content))
    
    return str(content)


# ── Tool Discovery & Format Conversion ─────────────────────────────────────────

def mcp_to_openai_tool(mcp_tool: dict, tool_id: str) -> dict:
    """
    Convert MCP tool definition to OpenAI function calling format.
    
    Args:
        mcp_tool: Tool definition from MCP tools/list response.
        tool_id: Unique tool ID for routing (format: "mcp_<server>_<tool>").
        
    Returns:
        OpenAI-compatible tool definition.
    """
    return {
        "type": "function",
        "function": {
            "name": tool_id,
            "description": mcp_tool.get("description", ""),
            "parameters": mcp_tool.get("inputSchema", {"type": "object", "properties": {}})
        }
    }


def mcp_to_anthropic_tool(mcp_tool: dict, tool_id: str) -> dict:
    """
    Convert MCP tool definition to Anthropic tool calling format.
    
    Args:
        mcp_tool: Tool definition from MCP tools/list response.
        tool_id: Unique tool ID for routing (format: "mcp_<server>_<tool>").
        
    Returns:
        Anthropic-compatible tool definition.
    """
    return {
        "name": tool_id,
        "description": mcp_tool.get("description", ""),
        "input_schema": mcp_tool.get("inputSchema", {"type": "object", "properties": {}})
    }


# Tool routing is now done via endpoint_map dictionary (removed decode_tool_call)


def discover_tools_from_registry(test_mode: bool = False, allow_empty: bool = False) -> tuple[list[dict], dict[str, str]]:
    """
    Query Modal.Dict registry and discover all available MCP tools.
    
    Args:
        test_mode: If True, uses mock registry for local testing.
        allow_empty: If True, returns empty list instead of raising error when registry is empty.
        
    Returns:
        Tuple of (tools_list, endpoint_map) where:
        - tools_list: List of tools in LLM-specific format (OpenAI or Anthropic)
        - endpoint_map: Mapping of {encoded_function_name: endpoint_url} for routing
        
    Raises:
        RuntimeError: If registry is inaccessible (not if empty and allow_empty=True).
    """
    if test_mode:
        # Mock registry for local testing without Modal
        registry_items = {
            "test-mcp": "https://test--test-mcp-web.modal.run"
        }
        print(f"[TEST MODE] Using mock registry with {len(registry_items)} entries")
    else:
        if not MODAL_AVAILABLE:
            raise RuntimeError(
                "Modal package not installed. Install it with: pip install modal"
            )
        
        try:
            registry = modal.Dict.from_name(REGISTRY_NAME, create_if_missing=True)
            registry_items = dict(registry)
        except Exception as e:
            raise RuntimeError(f"Could not access registry '{REGISTRY_NAME}': {e}")
    
    if not registry_items:
        if allow_empty:
            print(f"\n Registry '{REGISTRY_NAME}' is empty (no tools available yet)")
            return [], {}
        raise RuntimeError(f"Registry '{REGISTRY_NAME}' is empty. Deploy some MCP servers first.")
    
    print(f"\n Discovered {len(registry_items)} MCP server(s) in registry:")
    
    all_tools = []
    endpoint_map = {}  # Maps tool_id -> (endpoint_url, original_tool_name)
    
    for mcp_name, endpoint_url in registry_items.items():
        print(f"   • {mcp_name}: {endpoint_url}")
        
        try:
            mcp_tools = list_mcp_tools(endpoint_url)
            print(f"     → {len(mcp_tools)} tool(s) available")
            
            for mcp_tool in mcp_tools:
                # Create shorter tool ID: mcp_<server-slug>_<tool-name>
                # Extract server name from endpoint (e.g., barcelona-weather-forecast)
                server_slug = mcp_name.replace("-", "_")[:20]  # Limit to 20 chars
                tool_name_slug = mcp_tool['name'].replace("-", "_").replace(" ", "_")[:30]
                tool_id = f"mcp_{server_slug}_{tool_name_slug}"
                
                # Store mapping: tool_id -> (endpoint_url, original_tool_name)
                endpoint_map[tool_id] = (endpoint_url, mcp_tool['name'])
                
                if LLM_PROVIDER == "openai":
                    tool_def = mcp_to_openai_tool(mcp_tool, tool_id)
                    all_tools.append(tool_def)
                else:  # anthropic
                    tool_def = mcp_to_anthropic_tool(mcp_tool, tool_id)
                    all_tools.append(tool_def)
                
        except Exception as e:
            print(f"     ⚠ Error listing tools: {e}")
            continue
    
    if not all_tools:
        raise RuntimeError("No tools available from any MCP server.")
    
    return all_tools, endpoint_map


# ── LLM Call Abstraction ───────────────────────────────────────────────────────

def call_llm_with_tools(messages: list[dict], tools: list[dict]) -> dict:
    """
    Call LLM (OpenAI or Anthropic) with tool support.
    
    Args:
        messages: Conversation history in LLM-specific format.
        tools: Available tools in LLM-specific format.
        
    Returns:
        LLM response with normalized structure:
        {
            "finish_reason": "stop" | "tool_calls",
            "content": str | None,
            "tool_calls": [...] | None
        }
    """
    if LLM_PROVIDER == "openai":
        import openai
        client = openai.OpenAI()
        
        response = client.chat.completions.create(
            model="gpt-4-turbo",
            messages=messages,
            tools=tools if tools else None,
            tool_choice="auto" if tools else None,
        )
        
        choice = response.choices[0]
        
        return {
            "finish_reason": choice.finish_reason,
            "content": choice.message.content,
            "tool_calls": choice.message.tool_calls,
            "message": choice.message,  # For appending to history
        }
    
    else:  # anthropic
        import anthropic
        client = anthropic.Anthropic()
        
        # Anthropic doesn't use system message in messages array
        system_msgs = [m["content"] for m in messages if m["role"] == "system"]
        system = system_msgs[0] if system_msgs else None
        
        anthropic_messages = [m for m in messages if m["role"] != "system"]
        
        create_kwargs = dict(
            model="claude-opus-4-6",
            max_tokens=4096,
            messages=anthropic_messages,
        )
        if system:
            create_kwargs["system"] = system
        if tools:
            create_kwargs["tools"] = tools
        response = client.messages.create(**create_kwargs)
        
        # Normalize Anthropic response to match OpenAI structure
        finish_reason = "tool_calls" if response.stop_reason == "tool_use" else "stop"
        
        # Extract text content
        text_content = None
        for block in response.content:
            if block.type == "text":
                text_content = block.text
                break
        
        # Extract tool calls
        tool_calls = []
        for block in response.content:
            if block.type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "type": "function",
                    "function": {
                        "name": block.name,
                        "arguments": json.dumps(block.input)
                    }
                })
        
        return {
            "finish_reason": finish_reason,
            "content": text_content,
            "tool_calls": tool_calls if tool_calls else None,
            "raw_response": response,  # For appending to history
        }


# ── Tool Builder Integration ───────────────────────────────────────────────────

def call_tool_builder(user_goal: str, verbose: bool = True) -> bool:
    """
    Call the tool builder to generate and deploy MCP servers for a goal.
    
    Args:
        user_goal: The high-level goal to build tools for.
        verbose: If True, prints progress updates.
        
    Returns:
        True if tools were successfully built and deployed, False otherwise.
    """
    if verbose:
        print(f"\n{'='*70}")
        print(f" TOOL BUILDER")
        print(f"{'='*70}")
        print(f" No suitable tools found in registry.")
        print(f" Generating tools for: {user_goal}")
        print(f"{'='*70}\n")
    
    try:
        # Check if modal CLI is available
        try:
            subprocess.run(
                ["modal", "--version"],
                capture_output=True,
                check=True,
                timeout=5
            )
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            if verbose:
                print(f"\n✗ Modal CLI not found. Please install it:")
                print(f"   pip install modal")
            return False
        
        # Run tools_builder.py via modal
        result = subprocess.run(
            ["modal", "run", "tools_builder.py", "--goal", user_goal],
            capture_output=not verbose,
            text=True,
            timeout=600,  # 10 minute timeout
        )
        
        if result.returncode == 0:
            if verbose:
                print(f"\n{'='*70}")
                print(f" ✓ Tool builder completed successfully")
                print(f" Waiting 10 seconds for deployments to stabilize...")
                print(f"{'='*70}\n")
            
            # Wait for deployments to be ready
            time.sleep(10)
            return True
        else:
            if verbose:
                print(f"\n{'='*70}")
                print(f" ✗ Tool builder failed")
                if result.stderr:
                    print(f" Error: {result.stderr}")
                print(f"{'='*70}\n")
            return False
            
    except subprocess.TimeoutExpired:
        if verbose:
            print(f"\n✗ Tool builder timed out after 10 minutes")
        return False
    except Exception as e:
        if verbose:
            print(f"\n✗ Error calling tool builder: {e}")
        return False


# ── Main Supervisor Loop ───────────────────────────────────────────────────────

def supervisor(user_prompt: str, test_mode: bool = False, verbose: bool = True, auto_build_tools: bool = True) -> str:
    """
    Main supervisor orchestration loop.
    
    Args:
        user_prompt: The user's question/request.
        test_mode: If True, uses mock data for local testing.
        verbose: If True, prints progress updates.
        auto_build_tools: If True, automatically calls tool builder when registry is empty.
        
    Returns:
        The LLM's final answer as a string.
        
    Raises:
        RuntimeError: On errors during tool discovery or execution.
    """
    if verbose:
        print(f"\n{'='*70}")
        print(f" SUPERVISOR — {LLM_PROVIDER.upper()}")
        print(f"{'='*70}")
        print(f" Prompt: {user_prompt}")
    
    # 1. Discover tools from registry (allow empty if auto-building)
    tools, endpoint_map = discover_tools_from_registry(
        test_mode=test_mode,
        allow_empty=auto_build_tools
    )
    
    # 2. If no tools available and auto-build is enabled, call tool builder
    if not tools and auto_build_tools and not test_mode:
        if verbose:
            print(f"\n No tools available in registry.")
            print(f" Auto-building tools for this task...")
        
        # Call tool builder with the user prompt as the goal
        success = call_tool_builder(user_prompt, verbose=verbose)
        
        if not success:
            return (
                "I couldn't generate the necessary tools to answer your question. "
                "Please try running the tool builder manually:\n"
                f"  modal run tools_builder.py --goal \"{user_prompt}\""
            )
        
        # Re-discover tools after building
        if verbose:
            print(f"\n Re-discovering tools from registry...")
        
        tools, endpoint_map = discover_tools_from_registry(test_mode=test_mode)
        
        if not tools:
            return (
                "Tools were generated but couldn't be discovered in the registry. "
                "The endpoints may still be cold-starting. Please wait 30 seconds and try again."
            )
    
    elif not tools:
        # No tools and auto-build is disabled
        return (
            "No tools available in registry. Please generate tools first:\n"
            f"  modal run tools_builder.py --goal \"{user_prompt}\""
        )
    
    if verbose:
        print(f"\n Total tools available: {len(tools)}")
    
    # 3. Initialize conversation
    if LLM_PROVIDER == "openai":
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a helpful assistant with access to dynamic tools. "
                    "Use the available tools to answer the user's question comprehensively. "
                    "If you need information from multiple tools, call them in sequence."
                )
            },
            {"role": "user", "content": user_prompt}
        ]
    else:  # anthropic
        messages = [
            {
                "role": "user",
                "content": user_prompt
            }
        ]
    
    # 4. Agentic loop
    for iteration in range(1, MAX_ITERATIONS + 1):
        if verbose:
            print(f"\n Iteration {iteration}/{MAX_ITERATIONS}")
        
        # Call LLM
        response = call_llm_with_tools(messages, tools)
        
        if verbose:
            print(f"   Finish reason: {response['finish_reason']}")
        
        # If LLM is done, return final answer
        if response["finish_reason"] == "stop":
            final_answer = response["content"] or "(No response)"
            if verbose:
                print(f"\n {'='*70}")
                print(f" FINAL ANSWER")
                print(f" {'='*70}")
                print(f" {final_answer}")
                print(f" {'='*70}\n")
            return final_answer
        
        # If LLM wants to use tools, execute them
        if response["finish_reason"] == "tool_calls" and response["tool_calls"]:
            if LLM_PROVIDER == "openai":
                # Append assistant message with tool calls
                messages.append({
                    "role": "assistant",
                    "content": response["content"],
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments
                            }
                        }
                        for tc in response["tool_calls"]
                    ]
                })
                
                # Execute each tool call
                for tool_call in response["tool_calls"]:
                    function_name = tool_call.function.name
                    arguments = json.loads(tool_call.function.arguments)
                    
                    if verbose:
                        print(f"   🔧 Calling: {function_name}")
                        print(f"      Args: {arguments}")
                    
                    try:
                        # Look up endpoint and original tool name from endpoint_map
                        if function_name not in endpoint_map:
                            raise ValueError(f"Unknown tool: {function_name}")
                        
                        endpoint_url, original_tool_name = endpoint_map[function_name]
                        result = call_mcp_tool(endpoint_url, original_tool_name, arguments)
                        result_str = json.dumps(result) if not isinstance(result, str) else result
                        
                        if verbose:
                            preview = result_str[:200] + "..." if len(result_str) > 200 else result_str
                            print(f"      ✓ Result: {preview}")
                        
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": result_str
                        })
                    
                    except Exception as e:
                        error_msg = f"Error executing {function_name}: {str(e)}"
                        if verbose:
                            print(f"      ✗ {error_msg}")
                        
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": error_msg
                        })
            
            else:  # anthropic
                # Append assistant message
                messages.append({
                    "role": "assistant",
                    "content": response["raw_response"].content
                })
                
                # Execute each tool call and collect results
                tool_results = []
                
                for tool_call in response["tool_calls"]:
                    function_name = tool_call["function"]["name"]
                    arguments = json.loads(tool_call["function"]["arguments"])
                    
                    if verbose:
                        print(f"   🔧 Calling: {function_name}")
                        print(f"      Args: {arguments}")
                    
                    try:
                        # Look up endpoint and original tool name from endpoint_map
                        if function_name not in endpoint_map:
                            raise ValueError(f"Unknown tool: {function_name}")
                        
                        endpoint_url, original_tool_name = endpoint_map[function_name]
                        result = call_mcp_tool(endpoint_url, original_tool_name, arguments)
                        result_str = json.dumps(result) if not isinstance(result, str) else result
                        
                        if verbose:
                            preview = result_str[:200] + "..." if len(result_str) > 200 else result_str
                            print(f"      ✓ Result: {preview}")
                        
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_call["id"],
                            "content": result_str
                        })
                    
                    except Exception as e:
                        error_msg = f"Error: {str(e)}"
                        if verbose:
                            print(f"      ✗ {error_msg}")
                        
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_call["id"],
                            "content": error_msg,
                            "is_error": True
                        })
                
                # Append tool results as user message
                messages.append({
                    "role": "user",
                    "content": tool_results
                })
        else:
            # Unexpected finish reason
            if verbose:
                print(f"   ⚠ Unexpected finish reason, returning current content")
            return response["content"] or "(No response)"
    
    # Max iterations reached
    final_msg = "Maximum iterations reached. Partial answer: " + (response["content"] or "(incomplete)")
    if verbose:
        print(f"\n ⚠ {final_msg}")
    return final_msg


# ── CLI Entrypoint ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Run supervisor agent to answer questions using dynamic MCP tools",
    )
    parser.add_argument("--test", action="store_true", help="Run in test mode (mock registry)")
    parser.add_argument("--no-auto-build", action="store_true",
                       help="Disable automatic tool building (error if registry empty)")
    parser.add_argument("--prompt", type=str, default=None, help="Run with this prompt and exit (non-interactive)")

    args = parser.parse_args()

    if args.prompt:
        try:
            supervisor(
                args.prompt,
                test_mode=args.test,
                verbose=True,
                auto_build_tools=not args.no_auto_build,
            )
        except Exception as e:
            import traceback
            print(f"\n  ✗ Error: {e}", file=sys.stderr)
            traceback.print_exc()
        sys.exit(0)

    print("\n  Atlas Supervisor  —  type your prompt and press Enter, or 'quit' to exit.\n")

    while True:
        try:
            prompt = input("  › ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  bye!")
            sys.exit(0)

        if not prompt:
            continue
        if prompt.lower() in ("quit", "exit", "q"):
            print("  bye!")
            sys.exit(0)

        try:
            supervisor(
                prompt,
                test_mode=args.test,
                verbose=True,
                auto_build_tools=not args.no_auto_build,
            )
        except Exception as e:
            import traceback
            print(f"\n  ✗ Error: {e}", file=sys.stderr)
            traceback.print_exc()
        print()
