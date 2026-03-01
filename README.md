# Apollo

**Dynamic Agentic Toolbox** — An AI system that generates, deploys, and orchestrates custom tools on-demand.

## Overview

Apollo is an agentic framework that dynamically creates MCP (Model Context Protocol) tools based on user prompts. Instead of using a fixed set of tools, Apollo generates exactly what it needs, deploys them to Modal serverless infrastructure, and uses them to solve complex tasks.

### Key Features

- 🔧 **Dynamic Tool Generation** — LLM-powered code generation creates custom MCP servers
- ☁️ **Serverless Deployment** — Auto-deploy to Modal with zero infrastructure management  
- 🗂️ **Automatic Registry** — Modal.Dict-based tool discovery and management
- 🤖 **Smart Orchestration** — Supervisor agent with ReAct loop (no LangGraph overhead)
- 🔄 **Multi-Provider** — Works with OpenAI and Anthropic LLMs
- ✅ **Fully Tested** — Comprehensive test suite with mocked dependencies

## Quick Start

```bash
# 1. Setup
pip install modal anthropic openai python-dotenv requests
modal setup
modal secret create openai-secret OPENAI_API_KEY=sk-...

# 2. Generate tools for a task
modal run tools_builder.py --goal "plan a trip to spain"

# 3. Run supervisor
python supervisor.py --prompt "plan a 5-day trip to madrid"
```

See [QUICKSTART.md](QUICKSTART.md) for detailed instructions.

## Architecture

```
User Prompt → Supervisor Agent → Tool Builder → MCP Servers
                      ↓                              ↓
                Tool Registry ←──────────────────────┘
                      ↓
                 Execute Tools → Final Answer
```

**Components:**
- **Supervisor** ([supervisor.py](supervisor.py)) — Main orchestrator with agentic loop
- **Tool Builder** ([tools_builder.py](tools_builder.py)) — Generates & deploys MCP servers
- **Registry** (Modal.Dict) — Stores deployed tool endpoints
- **MCP Servers** — Individual tools as stateless HTTP endpoints

## Example Usage

```bash
# Build tools from a goal
$ modal run tools_builder.py --goal "research quantum computing papers"

Planning MCP servers …
Will build 3 MCP server(s):
  1. arxiv-search
  2. paper-summarizer  
  3. citation-tracker

Generating 3 MCP server(s) in parallel on Modal …
Deploying all servers …
  [deployed] [✓ registry] 1_arxiv-search_mcp.py
  [deployed] [✓ registry] 2_paper-summarizer_mcp.py
  [deployed] [✓ registry] 3_citation-tracker_mcp.py

# Run supervisor with those tools
$ python supervisor.py --prompt "find papers about quantum error correction from 2024"

Discovered 3 MCP server(s) in registry:
  • arxiv-search: https://...
  • paper-summarizer: https://...
  • citation-tracker: https://...

Iteration 1/10
  🔧 Calling: arxiv-search
     Args: {'query': 'quantum error correction', 'year': 2024}
     ✓ Result: Found 47 papers...

Iteration 2/10
  🔧 Calling: paper-summarizer
     Args: {'arxiv_id': '2401.12345'}
     ✓ Result: This paper presents...

FINAL ANSWER:
I found 47 recent papers on quantum error correction from 2024...
[detailed summary follows]
```

## Project Structure

```
Apollo/
├── supervisor.py              # Main agentic supervisor
├── tools_builder.py           # MCP generation & deployment  
├── mcp_builder.py            # Code generation library
├── mcp_template.py           # Template for generated MCPs
├── registry_manager.py       # CLI for registry management
├── test_supervisor.py        # Test suite
├── QUICKSTART.md             # Detailed usage guide
├── .env                      # Environment configuration
└── generated_mcps/           # Output directory
    ├── 1_destination_overview_mcp.py
    ├── 2_weather_forecast_mcp.py
    └── ...
```

## Development Status

**✅ Complete:**
- Tool generation from natural language
- Parallel deployment to Modal
- Automatic registry management
- Supervisor with tool discovery & execution
- OpenAI & Anthropic support
- Comprehensive testing

**🚧 In Progress (Team):**
- Tool validation layer
- Enhanced registry features
- Production integration

## Testing

```bash
# Run all tests
python test_supervisor.py

# Or with pytest
pytest test_supervisor.py -v

# Test locally without Modal
python supervisor.py --prompt "hello" --test
```

All 14 tests passing ✅

## Registry Management

```bash
# List registered tools
modal run registry_manager.py --action list

# Add a tool manually
modal run registry_manager.py --action add \
  --name "my-tool" --url "https://..."

# Test all endpoints
modal run registry_manager.py --action test

# Auto-register from generated_mcps/
modal run registry_manager.py --action auto-register
```

## Configuration

Set in `.env`:
```bash
LLM_PROVIDER=openai          # or "anthropic"
OPENAI_API_KEY=sk-proj-...
ANTHROPIC_API_KEY=sk-ant-...
```

## How It Works

1. **User submits a prompt** (e.g., "plan a trip to spain")

2. **Tool Builder plans tools:**
   - LLM decomposes goal into 2-6 specific MCP servers
   - Each server gets a detailed specification

3. **Parallel generation:**
   - Modal workers generate Python code from specs
   - Uses strict template adherence
   - Validates syntax, retries on errors

4. **Deployment & registration:**
   - `modal deploy` for each server
   - Auto-register endpoints in Modal.Dict
   - Manage free-tier endpoint limits

5. **Supervisor discovers tools:**
   - Query registry for available MCPs
   - Call `tools/list` on each endpoint
   - Convert to LLM tool format (OpenAI/Anthropic)

6. **Agentic execution loop:**
   - LLM decides which tools to call
   - Execute via MCP JSON-RPC over HTTP
   - Feed results back to LLM
   - Repeat until complete (max 10 iterations)

7. **Return final answer** to user

## Why Not LangGraph?

For this architecture, a simple ReAct loop is sufficient:
- ✅ Lighter weight, easier to debug
- ✅ Direct LLM tool calling (native API support)
- ✅ No additional dependencies
- ✅ Faster execution

LangGraph would add value for:
- Multi-agent collaboration
- Complex branching logic
- Human-in-the-loop workflows
- Persistent checkpointing

We can add it later if needed.

## Credits

Built for HackIllinois 2026 🎓

## License

MIT
