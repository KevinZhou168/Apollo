"""
QUICKSTART GUIDE — Atlas Dynamic Toolbox
=========================================

Atlas is an agentic system with a dynamic toolbox that:
1. Takes a user prompt
2. Generates custom MCP tools on demand
3. Deploys them to Modal
4. Uses them to answer the prompt

SETUP (One-time)
----------------

1. Install dependencies:
   pip install modal anthropic openai python-dotenv requests

2. Setup Modal:
   modal setup
   
3. Create Modal secrets:
   # For Anthropic
   modal secret create anthropic-secret ANTHROPIC_API_KEY=sk-ant-...
   
   # For OpenAI
   modal secret create openai-secret OPENAI_API_KEY=sk-proj-...

4. Set environment variables in .env:
   LLM_PROVIDER=openai  # or "anthropic"
   OPENAI_API_KEY=sk-proj-...
   ANTHROPIC_API_KEY=sk-ant-...


USAGE — Full Workflow
----------------------

The supervisor runs locally and can automatically build tools if needed:

  python supervisor.py --prompt "plan a trip to spain"

This will:
  ✓ Check the registry for available tools
  ✓ Auto-build tools if registry is empty (optional)
  ✓ Use LLM to orchestrate tool calls
  ✓ Return a comprehensive answer


Manual Workflow (Step-by-Step)
-------------------------------

Step 1: Build Tools
-------------------
Generate and deploy MCP servers based on a goal:

  modal run tools_builder.py --goal "plan a trip to spain"

This will:
  ✓ Break down the goal into 2-6 specific MCP servers
  ✓ Generate them in parallel on Modal
  ✓ Deploy each one
  ✓ Auto-register in Modal.Dict registry
  ✓ Stop oldest apps if you hit the 8-endpoint free tier limit

Output: generated_mcps/1_*.py, 2_*.py, etc.


Step 2: Verify Registry
------------------------
Check what tools are available:

  modal run registry_manager.py --action list

Test connectivity to all MCPs:

  modal run registry_manager.py --action test


Step 3: Run Supervisor
-----------------------
Use the supervisor to answer questions with your dynamic tools:

  python supervisor.py --prompt "plan a 5-day trip to spain"

The supervisor will:
  ✓ Discover all tools from the registry
  ✓ Use LLM (OpenAI/Anthropic) with tool calling
  ✓ Execute tools via MCP JSON-RPC over HTTP
  ✓ Loop until it has a complete answer
  ✓ Return comprehensive response


OPTIONS
-------

Auto-build mode (default):
  python supervisor.py --prompt "your question"
  # Automatically builds tools if registry is empty

Skip auto-build:
  python supervisor.py --prompt "your question" --no-auto-build
  # Only uses existing tools from registry

Allow empty registry:
  python supervisor.py --prompt "your question" --allow-empty
  # Proceeds even if no tools are available


REGISTRY MANAGEMENT

List all registered MCPs:
  modal run registry_manager.py --action list

Add an MCP manually:
  modal run registry_manager.py --action add \\
    --name "my-mcp" \\
    --url "https://workspace--my-mcp-web.modal.run"

Remove an MCP:
  modal run registry_manager.py --action remove --name "my-mcp"

Auto-register from generated_mcps/ folder:
  modal run registry_manager.py --action auto-register

Clear entire registry (use with caution):
  modal run registry_manager.py --action clear


TESTING
-------

Run supervisor tests:
  python test_supervisor.py

Or with pytest:
  pytest test_supervisor.py -v

Test supervisor locally (without Modal):
  python supervisor.py --prompt "hello" --test


ARCHITECTURE
------------

┌─────────────────────────────────────────────────────────────┐
│                         USER PROMPT                         │
│              "plan a trip to spain"                         │
└───────────────────────┬─────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│                   SUPERVISOR AGENT                          │
│  • Discovers tools from Modal.Dict registry                 │
│  • Breaks down prompt into subtasks                         │
│  • Calls tools dynamically                                  │
│  • Synthesizes final answer                                 │
└───────────┬────────────────────────┬────────────────────────┘
            │                        │
            │ If new tools needed    │ If tools exist
            ▼                        ▼
┌───────────────────────┐  ┌────────────────────────────────┐
│   TOOL BUILDER        │  │   MCP TOOL REGISTRY            │
│                       │  │   (Modal.Dict)                 │
│ • Plans MCP servers   │  │                                │
│ • Generates code      │  │ {                              │
│ • Deploys to Modal    │  │   "weather-mcp": "https://..." │
│ • Registers in Dict   │  │   "hotels-mcp": "https://..."  │
└───────────────────────┘  │   ...                          │
                           │ }                              │
                           └────────┬───────────────────────┘
                                    │
                                    ▼
                           ┌────────────────────────────────┐
                           │   DEPLOYED MCP SERVERS         │
                           │   (Modal Web Endpoints)        │
                           │                                │
                           │ • Stateless HTTP               │
                           │ • JSON-RPC protocol            │
                           │ • FastMCP framework            │
                           └────────────────────────────────┘


FILES
-----

Core System:
  supervisor.py           - Main agentic supervisor with ReAct loop
  tools_builder.py        - Generate & deploy MCP servers from goals
  mcp_builder.py          - Code generation library (used by workers)
  mcp_template.py         - Template for all generated MCPs
  
Registry Management:
  registry_manager.py     - CLI for Modal.Dict registry CRUD operations
  
Testing:
  test_supervisor.py      - Comprehensive tests for supervisor

Generated Output:
  generated_mcps/         - Directory of generated MCP server files


ENVIRONMENT VARIABLES
---------------------

Required in .env:
  LLM_PROVIDER            - "openai" or "anthropic"
  OPENAI_API_KEY          - Your OpenAI API key
  ANTHROPIC_API_KEY       - Your Anthropic API key

Optional:
  REGISTRY_NAME           - Name of Modal.Dict (default: "mcp-tool-registry")
  MAX_ITERATIONS          - Max supervisor loops (default: 10)
  REQUEST_TIMEOUT         - MCP HTTP timeout in seconds (default: 30)


TROUBLESHOOTING
---------------

"Registry not found":
  → The Modal.Dict hasn't been created yet
  → Run tools_builder.py first, or create manually:
    modal run registry_manager.py --action add --name "test" --url "https://..."

"No tools available":
  → Registry is empty
  → Run: modal run tools_builder.py --goal "your goal"

"MCP endpoint timeout":
  → Endpoint may be cold-starting (first request is slow)
  → Wait ~10 seconds and retry
  → Check if app is deployed: modal app list

"Max iterations reached":
  → Supervisor couldn't complete task in 10 loops
  → Tools may be returning unexpected formats
  → Check MCP tool implementations
  → Increase MAX_ITERATIONS in supervisor.py

"Endpoint limit reached":
  → Free tier has 8 web endpoint limit
  → tools_builder.py auto-stops oldest apps to make room
  → Manually stop apps: modal app stop <app-id>


NEXT STEPS
----------

1. Integration: Connect supervisor as the main entry point
   - Currently tools_builder.py is the entry for testing
   - In production, route user prompt → supervisor → tool builder if needed

2. Validation Layer: Add tool specification validation
   - Your teammates are working on this
   - Will ensure generated tools meet quality standards

3. Caching: Cache tool discovery results
   - Registry lookups can be expensive
   - Cache for ~1 minute, invalidate on updates

4. Monitoring: Add logging and observability
   - Track which tools are used most
   - Monitor MCP endpoint health
   - Log supervisor decision paths

5. Advanced Orchestration: Consider LangGraph later
   - Only if you need multi-agent workflows
   - Human-in-the-loop approval
   - Complex branching logic


COST OPTIMIZATION
-----------------

• Modal free tier: 8 web endpoints, $30/month credits
• LLM costs vary by provider:
  - OpenAI GPT-4 Turbo: ~$0.01-0.03 per query
  - Anthropic Claude Opus: ~$0.015-0.075 per query
• Optimize by:
  - Using smaller models for tool generation
  - Caching registry lookups
  - Stopping unused MCPs: modal app stop <app-id>


SUPPORT
-------

Check logs:
  modal app logs <app-name>

List running apps:
  modal app list

View app details:
  modal app show <app-name>

Stop an app:
  modal app stop <app-name>
"""

if __name__ == "__main__":
    print(__doc__)
