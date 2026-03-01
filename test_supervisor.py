"""
test_supervisor.py — Tests for the supervisor agent.

Run with:
    pytest test_supervisor.py -v
    python test_supervisor.py  # runs basic tests without pytest
"""

import json
from unittest.mock import Mock, patch, MagicMock
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

import supervisor


# ── Test Fixtures ──────────────────────────────────────────────────────────────

def mock_mcp_tools_list_response():
    """Mock response from MCP tools/list endpoint."""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "tools": [
                {
                    "name": "get_weather",
                    "description": "Get current weather for a city",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "city": {"type": "string", "description": "City name"},
                            "units": {"type": "string", "description": "metric or imperial"}
                        },
                        "required": ["city"]
                    }
                },
                {
                    "name": "get_forecast",
                    "description": "Get weather forecast for a city",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "city": {"type": "string"},
                            "days": {"type": "integer"}
                        },
                        "required": ["city"]
                    }
                }
            ]
        }
    }


def mock_mcp_tool_call_response(tool_name: str, result_text: str):
    """Mock response from MCP tools/call endpoint."""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": result_text
                }
            ]
        }
    }


# ── Test Tool Discovery ────────────────────────────────────────────────────────

def test_list_mcp_tools():
    """Test listing tools from an MCP endpoint."""
    with patch('requests.post') as mock_post:
        mock_response = Mock()
        mock_response.json.return_value = mock_mcp_tools_list_response()
        mock_response.raise_for_status = Mock()
        mock_post.return_value = mock_response
        
        tools = supervisor.list_mcp_tools("https://test.modal.run")
        
        assert len(tools) == 2
        assert tools[0]["name"] == "get_weather"
        assert tools[1]["name"] == "get_forecast"
        
        # Verify correct JSON-RPC request was made
        call_args = mock_post.call_args
        assert call_args[0][0] == "https://test.modal.run/mcp/"
        request_body = call_args[1]["json"]
        assert request_body["method"] == "tools/list"


def test_list_mcp_tools_error():
    """Test error handling when MCP endpoint returns an error."""
    with patch('requests.post') as mock_post:
        mock_response = Mock()
        mock_response.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32601, "message": "Method not found"}
        }
        mock_response.raise_for_status = Mock()
        mock_post.return_value = mock_response
        
        try:
            supervisor.list_mcp_tools("https://test.modal.run")
            assert False, "Should have raised RuntimeError"
        except RuntimeError as e:
            assert "MCP error" in str(e)


# ── Test Tool Execution ────────────────────────────────────────────────────────

def test_call_mcp_tool():
    """Test calling a tool on an MCP endpoint."""
    with patch('requests.post') as mock_post:
        mock_response = Mock()
        mock_response.json.return_value = mock_mcp_tool_call_response(
            "get_weather",
            "Sunny, 22°C"
        )
        mock_response.raise_for_status = Mock()
        mock_post.return_value = mock_response
        
        result = supervisor.call_mcp_tool(
            "https://test.modal.run",
            "get_weather",
            {"city": "Madrid", "units": "metric"}
        )
        
        assert result == "Sunny, 22°C"
        
        # Verify request
        call_args = mock_post.call_args
        request_body = call_args[1]["json"]
        assert request_body["method"] == "tools/call"
        assert request_body["params"]["name"] == "get_weather"
        assert request_body["params"]["arguments"]["city"] == "Madrid"


def test_call_mcp_tool_error():
    """Test error handling when tool execution fails."""
    with patch('requests.post') as mock_post:
        mock_response = Mock()
        mock_response.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": -1,
                "message": "Invalid city name"
            }
        }
        mock_response.raise_for_status = Mock()
        mock_post.return_value = mock_response
        
        try:
            supervisor.call_mcp_tool(
                "https://test.modal.run",
                "get_weather",
                {"city": "InvalidCity"}
            )
            assert False, "Should have raised RuntimeError"
        except RuntimeError as e:
            assert "Invalid city name" in str(e)


# ── Test Format Conversion ─────────────────────────────────────────────────────

def test_mcp_to_openai_tool():
    """Test converting MCP tool format to OpenAI format."""
    mcp_tool = {
        "name": "get_weather",
        "description": "Get weather for a city",
        "inputSchema": {
            "type": "object",
            "properties": {
                "city": {"type": "string"}
            },
            "required": ["city"]
        }
    }
    
    openai_tool = supervisor.mcp_to_openai_tool(
        mcp_tool,
        "https://test.modal.run"
    )
    
    assert openai_tool["type"] == "function"
    assert "get_weather" in openai_tool["function"]["name"]
    assert openai_tool["function"]["description"] == "Get weather for a city"
    assert openai_tool["function"]["parameters"]["properties"]["city"]["type"] == "string"


def test_mcp_to_anthropic_tool():
    """Test converting MCP tool format to Anthropic format."""
    mcp_tool = {
        "name": "get_weather",
        "description": "Get weather for a city",
        "inputSchema": {
            "type": "object",
            "properties": {
                "city": {"type": "string"}
            }
        }
    }
    
    anthropic_tool = supervisor.mcp_to_anthropic_tool(
        mcp_tool,
        "https://test.modal.run"
    )
    
    assert "get_weather" in anthropic_tool["name"]
    assert anthropic_tool["description"] == "Get weather for a city"
    assert anthropic_tool["input_schema"]["properties"]["city"]["type"] == "string"


def test_decode_tool_call():
    """Test decoding encoded function names back to endpoint + tool name."""
    # First encode
    mcp_tool = {"name": "test_tool", "description": "Test"}
    openai_tool = supervisor.mcp_to_openai_tool(mcp_tool, "https://example.com")
    encoded_name = openai_tool["function"]["name"]
    
    # Then decode
    endpoint, tool_name = supervisor.decode_tool_call(encoded_name)
    
    assert endpoint == "https://example.com"
    assert tool_name == "test_tool"


# ── Test Tool Discovery from Registry ──────────────────────────────────────────

def test_discover_tools_from_registry_test_mode():
    """Test tool discovery in test mode (no actual Modal.Dict)."""
    with patch('supervisor.list_mcp_tools') as mock_list:
        mock_list.return_value = [
            {
                "name": "tool1",
                "description": "First tool",
                "inputSchema": {"type": "object", "properties": {}}
            },
            {
                "name": "tool2",
                "description": "Second tool",
                "inputSchema": {"type": "object", "properties": {}}
            }
        ]
        
        tools, endpoint_map = supervisor.discover_tools_from_registry(test_mode=True, allow_empty=False)
        
        assert len(tools) >= 2  # At least 2 tools from mock MCP
        assert len(endpoint_map) >= 2


# ── Test LLM Call Abstraction (OpenAI) ─────────────────────────────────────────

def test_call_llm_with_tools_openai():
    """Test calling OpenAI with tools."""
    with patch.dict('os.environ', {'LLM_PROVIDER': 'openai'}):
        # Reload supervisor to pick up env change
        import importlib
        importlib.reload(supervisor)
        
        with patch('openai.OpenAI') as mock_openai_cls:
            mock_client = Mock()
            mock_openai_cls.return_value = mock_client
            
            # Mock response without tool calls (final answer)
            mock_choice = Mock()
            mock_choice.finish_reason = "stop"
            mock_choice.message.content = "The weather is sunny"
            mock_choice.message.tool_calls = None
            
            mock_response = Mock()
            mock_response.choices = [mock_choice]
            
            mock_client.chat.completions.create.return_value = mock_response
            
            messages = [{"role": "user", "content": "What's the weather?"}]
            tools = []
            
            result = supervisor.call_llm_with_tools(messages, tools)
            
            assert result["finish_reason"] == "stop"
            assert result["content"] == "The weather is sunny"
            assert result["tool_calls"] is None


def test_call_llm_with_tool_calls_openai():
    """Test OpenAI response with tool calls."""
    with patch.dict('os.environ', {'LLM_PROVIDER': 'openai'}):
        import importlib
        importlib.reload(supervisor)
        
        with patch('openai.OpenAI') as mock_openai_cls:
            mock_client = Mock()
            mock_openai_cls.return_value = mock_client
            
            # Mock tool call
            mock_tool_call = Mock()
            mock_tool_call.id = "call_123"
            mock_tool_call.type = "function"
            mock_tool_call.function.name = "mcp__test__get_weather"
            mock_tool_call.function.arguments = '{"city": "Madrid"}'
            
            mock_choice = Mock()
            mock_choice.finish_reason = "tool_calls"
            mock_choice.message.content = None
            mock_choice.message.tool_calls = [mock_tool_call]
            
            mock_response = Mock()
            mock_response.choices = [mock_choice]
            
            mock_client.chat.completions.create.return_value = mock_response
            
            messages = [{"role": "user", "content": "Weather in Madrid?"}]
            tools = [{"type": "function", "function": {"name": "mcp__test__get_weather"}}]
            
            result = supervisor.call_llm_with_tools(messages, tools)
            
            assert result["finish_reason"] == "tool_calls"
            assert len(result["tool_calls"]) == 1


# ── Test Full Supervisor Flow ──────────────────────────────────────────────────

def test_supervisor_simple_answer():
    """Test supervisor with a simple question that doesn't need tools."""
    with patch.dict('os.environ', {'LLM_PROVIDER': 'openai'}):
        import importlib
        importlib.reload(supervisor)
        
        with patch('supervisor.discover_tools_from_registry') as mock_discover:
            mock_discover.return_value = ([], {})
            
            with patch('supervisor.call_llm_with_tools') as mock_llm:
                mock_llm.return_value = {
                    "finish_reason": "stop",
                    "content": "Hello! I'm here to help.",
                    "tool_calls": None
                }
                
                result = supervisor.supervisor(
                    "Hello",
                    test_mode=True,
                    verbose=False,
                    auto_build_tools=False  # Disable auto-build for this test
                )
                
                # Should get the helpful error message since no tools
                assert "No tools available" in result or "Hello" in result


def test_supervisor_with_tool_execution():
    """Test supervisor executing a tool and returning final answer."""
    with patch.dict('os.environ', {'LLM_PROVIDER': 'openai'}):
        import importlib
        importlib.reload(supervisor)
        
        # Mock tool discovery
        with patch('supervisor.discover_tools_from_registry') as mock_discover:
            mock_tools = [{
                "type": "function",
                "function": {
                    "name": "mcp__dGVzdA__get_weather",
                    "description": "Get weather"
                }
            }]
            mock_endpoint_map = {"mcp__dGVzdA__get_weather": "https://test"}
            mock_discover.return_value = (mock_tools, mock_endpoint_map)
            
            # Mock LLM calls
            with patch('supervisor.call_llm_with_tools') as mock_llm:
                # First call: LLM wants to use tool
                mock_tool_call = Mock()
                mock_tool_call.id = "call_1"
                mock_tool_call.function.name = "mcp__dGVzdA__get_weather"
                mock_tool_call.function.arguments = '{"city": "Madrid"}'
                
                first_response = {
                    "finish_reason": "tool_calls",
                    "content": None,
                    "tool_calls": [mock_tool_call]
                }
                
                # Second call: LLM returns final answer
                second_response = {
                    "finish_reason": "stop",
                    "content": "The weather in Madrid is sunny and 22°C.",
                    "tool_calls": None
                }
                
                mock_llm.side_effect = [first_response, second_response]
                
                # Mock tool execution
                with patch('supervisor.call_mcp_tool') as mock_tool:
                    mock_tool.return_value = "Sunny, 22°C"
                    
                    result = supervisor.supervisor(
                        "What's the weather in Madrid?",
                        test_mode=True,
                        verbose=False
                    )
                    
                    assert "sunny" in result.lower() or "22" in result
                    assert mock_tool.called


def test_supervisor_max_iterations():
    """Test that supervisor stops after max iterations."""
    with patch.dict('os.environ', {'LLM_PROVIDER': 'openai'}):
        import importlib
        importlib.reload(supervisor)
        
        with patch('supervisor.discover_tools_from_registry') as mock_discover:
            # Provide at least one tool so auto-build doesn't trigger
            mock_tools = [{
                "type": "function",
                "function": {"name": "mcp__test__tool", "description": "Test"}
            }]
            mock_discover.return_value = (mock_tools, {})
            
            with patch('supervisor.call_llm_with_tools') as mock_llm:
                # Always request tool calls (infinite loop scenario)
                mock_tool_call = Mock()
                mock_tool_call.id = "call_1"
                mock_tool_call.function.name = "mcp__test__tool"
                mock_tool_call.function.arguments = '{}'
                
                mock_llm.return_value = {
                    "finish_reason": "tool_calls",
                    "content": "Thinking...",
                    "tool_calls": [mock_tool_call]
                }
                
                with patch('supervisor.call_mcp_tool') as mock_tool:
                    mock_tool.return_value = "result"
                    
                    # Temporarily set max iterations to 3 for faster test
                    original_max = supervisor.MAX_ITERATIONS
                    supervisor.MAX_ITERATIONS = 3
                    
                    result = supervisor.supervisor(
                        "Test",
                        test_mode=True,
                        verbose=False
                    )
                    
                    supervisor.MAX_ITERATIONS = original_max
                    
                    assert "Maximum iterations" in result or "Thinking" in result


def test_supervisor_empty_registry_with_auto_build_disabled():
    """Test supervisor behavior when registry is empty and auto-build is disabled."""
    with patch.dict('os.environ', {'LLM_PROVIDER': 'openai'}):
        import importlib
        importlib.reload(supervisor)
        
        with patch('supervisor.discover_tools_from_registry') as mock_discover:
            # Return empty tools list
            mock_discover.return_value = ([], {})
            
            result = supervisor.supervisor(
                "Test prompt",
                test_mode=True,
                verbose=False,
                auto_build_tools=False
            )
            
            # Should return helpful error message
            assert "No tools available" in result
            assert "modal run tools_builder.py" in result


def test_supervisor_empty_registry_allow_empty():
    """Test that discover_tools_from_registry can return empty list when allow_empty=True."""
    with patch('supervisor.modal.Dict.from_name') as mock_dict:
        mock_dict.return_value = {}
        
        tools, endpoint_map = supervisor.discover_tools_from_registry(
            test_mode=False,
            allow_empty=True
        )
        
        assert tools == []
        assert endpoint_map == {}


# ── Test Error Handling ────────────────────────────────────────────────────────

def test_supervisor_tool_execution_error():
    """Test supervisor handling tool execution errors gracefully."""
    with patch.dict('os.environ', {'LLM_PROVIDER': 'openai'}):
        import importlib
        importlib.reload(supervisor)
        
        with patch('supervisor.discover_tools_from_registry') as mock_discover:
            mock_tools = [{
                "type": "function",
                "function": {"name": "mcp__test__failing_tool"}
            }]
            mock_discover.return_value = (mock_tools, {})
            
            with patch('supervisor.call_llm_with_tools') as mock_llm:
                # First: tool call
                mock_tool_call = Mock()
                mock_tool_call.id = "call_1"
                mock_tool_call.function.name = "mcp__test__failing_tool"
                mock_tool_call.function.arguments = '{}'
                
                first_response = {
                    "finish_reason": "tool_calls",
                    "content": None,
                    "tool_calls": [mock_tool_call]
                }
                
                # Second: final answer after error
                second_response = {
                    "finish_reason": "stop",
                    "content": "I encountered an error with that tool.",
                    "tool_calls": None
                }
                
                mock_llm.side_effect = [first_response, second_response]
                
                with patch('supervisor.call_mcp_tool') as mock_tool:
                    mock_tool.side_effect = RuntimeError("Tool failed")
                    
                    result = supervisor.supervisor(
                        "Test",
                        test_mode=True,
                        verbose=False
                    )
                    
                    # Should still return an answer despite tool error
                    assert isinstance(result, str)
                    assert len(result) > 0


# ── Run Tests ──────────────────────────────────────────────────────────────────

def run_all_tests():
    """Run all tests manually (without pytest)."""
    tests = [
        ("Test list MCP tools", test_list_mcp_tools),
        ("Test list MCP tools error", test_list_mcp_tools_error),
        ("Test call MCP tool", test_call_mcp_tool),
        ("Test call MCP tool error", test_call_mcp_tool_error),
        ("Test MCP to OpenAI format", test_mcp_to_openai_tool),
        ("Test MCP to Anthropic format", test_mcp_to_anthropic_tool),
        ("Test decode tool call", test_decode_tool_call),
        ("Test discover tools (test mode)", test_discover_tools_from_registry_test_mode),
        ("Test OpenAI LLM call", test_call_llm_with_tools_openai),
        ("Test OpenAI with tool calls", test_call_llm_with_tool_calls_openai),
        ("Test supervisor simple answer", test_supervisor_simple_answer),
        ("Test supervisor with tool execution", test_supervisor_with_tool_execution),
        ("Test supervisor max iterations", test_supervisor_max_iterations),
        ("Test empty registry (auto-build disabled)", test_supervisor_empty_registry_with_auto_build_disabled),
        ("Test empty registry (allow_empty=True)", test_supervisor_empty_registry_allow_empty),
        ("Test supervisor tool error handling", test_supervisor_tool_execution_error),
    ]
    
    passed = 0
    failed = 0
    
    print("\n" + "="*70)
    print(" RUNNING SUPERVISOR TESTS")
    print("="*70 + "\n")
    
    for name, test_func in tests:
        try:
            test_func()
            print(f"✓ {name}")
            passed += 1
        except Exception as e:
            print(f"✗ {name}")
            print(f"  Error: {e}")
            failed += 1
    
    print("\n" + "="*70)
    print(f" RESULTS: {passed} passed, {failed} failed")
    print("="*70 + "\n")
    
    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
