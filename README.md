<p align="center">
  <img src="https://img.shields.io/badge/HackIllinois-2026-orange?style=for-the-badge" alt="HackIllinois 2026" />
  <img src="https://img.shields.io/badge/Modal-Serverless-blueviolet?style=for-the-badge" alt="Modal" />
  <img src="https://img.shields.io/badge/MCP-Protocol-00b4d8?style=for-the-badge" alt="MCP" />
</p>

# 🚀 Apollo

**The AI that builds its own tools.**

Apollo is an agentic system with a truly dynamic toolbox. Give it any goal and it will **design**, **generate**, **deploy**, and **orchestrate** custom [MCP](https://modelcontextprotocol.io/) tools on the fly. No predefined toolset, no manual wiring. Just a prompt.

> _"Plan a trip to Spain"_ → Apollo creates a weather API tool, a destination guide tool, and a local events tool, deploys them all in seconds, then uses them to deliver a comprehensive answer.

---

## ✨ Why Apollo?

Most AI agents are limited to a static set of tools chosen at development time. Apollo flips this model:

| Traditional Agents                | Apollo                                         |
| --------------------------------- | ---------------------------------------------- |
| Fixed tool set at build time      | Tools generated on demand from any prompt      |
| Manual tool integration           | Automatic deployment to serverless infra       |
| Limited to pre-built capabilities | Unlimited capabilities via dynamic MCP servers |

**Key capabilities:**

- 🔧 **Dynamic Tool Generation** — LLM-powered code generation creates custom MCP servers tailored to each task
- ☁️ **Serverless Deployment** — Auto-deploy to [Modal](https://modal.com) with zero infrastructure management
- 🗂️ **Automatic Registry** — Modal.Dict-based tool discovery and lifecycle management
- 🤖 **Smart Orchestration** — Supervisor agent with a ReAct loop for multi-step reasoning
- 📡 **Real API Integration** — Falls back to a curated knowledge base of 1,400+ public APIs to avoid hallucinated endpoints
- 🌌 **Live Visualization** — Real-time 3D solar system visualization of the tool-building pipeline

---

## 🏗️ Architecture

![Image](https://raw.githubusercontent.com/KevinZhou168/Apollo/jason/Apollo.png)

---

## 🛠️ Setup

### Prerequisites

- **Python 3.10+**
- A [Modal](https://modal.com) account (free tier works)
- An **OpenAI** and/or **Anthropic** API key

### 1. Clone & Install Dependencies

```bash
git clone https://github.com/your-team/apollo.git
cd apollo

pip install modal anthropic openai python-dotenv requests \
            uvicorn starlette sse-starlette
```

### 2. Configure Modal

```bash
# Authenticate with Modal
modal setup

# Store your API keys as Modal secrets (used by cloud workers)
modal secret create openai-secret OPENAI_API_KEY=sk-proj-...
modal secret create anthropic-secret ANTHROPIC_API_KEY=sk-ant-...
```

### 3. Set Environment Variables

Create a `.env` file in the project root:

```bash
LLM_PROVIDER=openai            # or "anthropic"
OPENAI_API_KEY=sk-proj-...
ANTHROPIC_API_KEY=sk-ant-...
```

### 4. Launch the Demo

```bash
python backend.py
```

Open **http://localhost:8080** — type a goal, hit **Run**, and watch Apollo build and use custom tools in real time.

---

## 🎮 Usage

### Web Interface (recommended)

```bash
python backend.py               # starts on http://localhost:8080
python backend.py --port 9000   # custom port
```

The web UI streams the full supervisor output live — you'll see tool planning, code generation, deployment, and the final answer rendered in markdown.

### CLI — Build Tools Manually

```bash
# Generate and deploy MCP tools for a goal
modal run tools_builder.py --goal "research quantum computing papers"
```

### CLI — Run the Supervisor

```bash
# Run with auto-build (builds tools if registry is empty)
python supervisor.py --prompt "plan a 5-day trip to Madrid"

# Use existing tools only
python supervisor.py --prompt "what's the weather in Tokyo?" --no-auto-build

# Local test mode (no Modal required)
python supervisor.py --prompt "hello" --test
```

### Registry Management

```bash
modal run registry_manager.py --action list          # list registered tools
modal run registry_manager.py --action test          # test all endpoints
modal run registry_manager.py --action clear         # clear registry
modal run registry_manager.py --action add \
  --name "my-tool" --url "https://..."               # add manually
```

### Live Visualization

Apollo includes a 3D solar system visualization that animates the tool-building pipeline in real time:

```bash
# Launch visualization server (opens browser automatically)
python viz_server.py

# Or run tools_builder with --viz flag
modal run tools_builder.py --goal "your goal" --viz
```

---

## 📂 Project Structure

```
Apollo/
├── backend.py              # Web server — serves UI + streams supervisor output
├── supervisor.py           # Main agentic supervisor with ReAct loop
├── tools_builder.py        # MCP generation & deployment engine (runs on Modal)
├── mcp_builder.py          # Code generation library (used by Modal workers)
├── mcp_template.py         # Template for all generated MCP servers
├── api_reference.py        # Public-APIs fallback knowledge base (1,400+ APIs)
├── registry_manager.py     # CLI for Modal.Dict registry CRUD
├── viz_server.py           # SSE server for live pipeline visualization
├── ui_server.py            # Alternative UI server
├── test_supervisor.py      # Test suite (14 tests)
├── notes.txt               # Developer notes & reference commands
├── .env                    # Local environment configuration
├── ui/
│   └── index.html          # Main chat interface
├── demo/
│   └── index.html          # 3D solar system visualization
├── api_reference_data/     # Cached public API database
│   └── public_apis.json
└── generated_mcps/         # Output directory for generated MCP servers
```

---

## ⚙️ How It Works

1. **User submits a prompt** → e.g. _"plan a trip to Spain"_

2. **Tool Builder plans MCP servers** → the LLM decomposes the goal into 2–6 specific, single-responsibility tools (e.g. `weather-forecast`, `destination-guide`, `local-events`)

3. **Parallel code generation** → Modal workers generate Python code from specs in parallel, using a strict MCP template and validated against real public APIs

4. **Deployment & registration** → each server is deployed with `modal deploy`, and its endpoint URL is registered in a shared `Modal.Dict` registry

5. **Supervisor discovers tools** → queries the registry, fetches `tools/list` from each MCP endpoint, and converts them to the LLM's native tool-calling format

6. **Agentic execution loop** → the LLM decides which tools to call, executes them via MCP JSON-RPC over HTTP, feeds results back, and repeats (up to 10 iterations)

7. **Final answer** → a comprehensive, synthesized response is returned to the user

---

## 🧪 Testing

```bash
# Run all tests
python test_supervisor.py

# With pytest
pytest test_supervisor.py -v

# Local test mode (no Modal, no API keys needed)
python supervisor.py --prompt "hello" --test
```

---

## 🔧 Configuration

| Variable            | Default             | Description                           |
| ------------------- | ------------------- | ------------------------------------- |
| `LLM_PROVIDER`      | `openai`            | LLM backend (`openai` or `anthropic`) |
| `OPENAI_API_KEY`    | —                   | Your OpenAI API key                   |
| `ANTHROPIC_API_KEY` | —                   | Your Anthropic API key                |
| `REGISTRY_NAME`     | `mcp-tool-registry` | Name of the Modal.Dict registry       |
| `MAX_ITERATIONS`    | `10`                | Max supervisor loop iterations        |
| `REQUEST_TIMEOUT`   | `30`                | MCP HTTP call timeout (seconds)       |

---

## 🤔 Design Decisions

**Why MCP?** The [Model Context Protocol](https://modelcontextprotocol.io/) gives us a standardized interface for tool discovery and execution. Every generated tool speaks the same language, making orchestration trivial.

**Why Modal?** Serverless deployment means we don't manage infrastructure. Generated tools go from code to live HTTPS endpoint in seconds, with automatic scaling and zero ops burden.

**Why not LangGraph?** A simple ReAct loop is lighter, faster to debug, and has no additional dependencies. The architecture can evolve to LangGraph later if multi-agent collaboration or complex branching is needed.
