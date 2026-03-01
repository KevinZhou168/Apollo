#!/usr/bin/env python3
"""
demo.py — End-to-end demonstration of the Atlas dynamic toolbox.

This script demonstrates the full workflow:
1. Generate tools from a goal
2. Verify registry
3. Run supervisor to answer a question

Usage:
    python demo.py
"""

import subprocess
import sys
import time

def run_command(cmd: list[str], description: str) -> bool:
    """Run a command and display output."""
    print(f"\n{'='*70}")
    print(f"  {description}")
    print(f"{'='*70}\n")
    print(f"$ {' '.join(cmd)}\n")
    
    result = subprocess.run(cmd, capture_output=False, text=True)
    
    if result.returncode != 0:
        print(f"\n✗ Command failed with exit code {result.returncode}")
        return False
    
    print(f"\n✓ Success")
    return True


def main():
    """Run the full Atlas demo."""
    
    print("""
╔══════════════════════════════════════════════════════════════════════╗
║                                                                      ║
║                         ATLAS DEMO                                   ║
║                   Dynamic Agentic Toolbox                            ║
║                                                                      ║
╚══════════════════════════════════════════════════════════════════════╝

This demo will:
  1. Generate MCP tools for a sample goal
  2. Deploy them to Modal
  3. Register them in Modal.Dict
  4. Use the supervisor to answer a question with those tools

Press Ctrl+C at any time to cancel.
""")
    
    input("Press Enter to continue...")
    
    # ── Step 1: Generate and deploy tools ──────────────────────────────────────
    
    goal = "provide information about weather and geography"
    
    print(f"\n\nSTEP 1: Generate tools for the goal:")
    print(f'  "{goal}"')
    print()
    
    response = input("Proceed with tool generation? (y/N): ").strip().lower()
    if response != 'y':
        print("Demo cancelled.")
        return
    
    success = run_command(
        ["modal", "run", "tools_builder.py", "--goal", goal],
        "Generating and deploying MCP tools"
    )
    
    if not success:
        print("\n✗ Tool generation failed. Check the error above.")
        return
    
    # Wait for deployments to be fully ready
    print("\n⏳ Waiting 5 seconds for deployments to stabilize...")
    time.sleep(5)
    
    # ── Step 2: Verify registry ────────────────────────────────────────────────
    
    print("\n\nSTEP 2: Verify the registry")
    print()
    
    success = run_command(
        ["modal", "run", "registry_manager.py", "--action", "list"],
        "Listing registered MCP tools"
    )
    
    if not success:
        print("\n✗ Registry check failed.")
        return
    
    # ── Step 3: Test endpoints ─────────────────────────────────────────────────
    
    print("\n\nSTEP 3: Test MCP endpoints")
    print()
    
    response = input("Test connectivity to all MCPs? (y/N): ").strip().lower()
    if response == 'y':
        success = run_command(
            ["modal", "run", "registry_manager.py", "--action", "test"],
            "Testing MCP endpoint connectivity"
        )
        
        if not success:
            print("\n⚠ Some endpoints may be offline or cold-starting.")
            print("   This is normal for the first request. Continuing anyway...")
    
    # ── Step 4: Run supervisor ─────────────────────────────────────────────────
    
    print("\n\nSTEP 4: Run the supervisor")
    print()
    
    prompts = [
        "What's the weather like in San Francisco?",
        "Tell me about the geography of Japan",
        "Compare the climates of London and Sydney",
    ]
    
    print("Sample prompts you can try:")
    for i, p in enumerate(prompts, 1):
        print(f"  {i}. {p}")
    print()
    
    user_prompt = input("Enter your prompt (or press Enter for prompt #1): ").strip()
    
    if not user_prompt:
        user_prompt = prompts[0]
    
    print(f"\nUsing prompt: \"{user_prompt}\"")
    print()
    
    success = run_command(
        ["python", "supervisor.py", "--prompt", user_prompt],
        "Running supervisor agent"
    )
    
    if not success:
        print("\n✗ Supervisor failed. This may be due to:")
        print("   • MCP endpoints still cold-starting (wait and retry)")
        print("   • LLM API issues (check your API keys)")
        print("   • Network connectivity")
        return
    
    # ── Summary ────────────────────────────────────────────────────────────────
    
    print(f"""

╔══════════════════════════════════════════════════════════════════════╗
║                                                                      ║
║                         DEMO COMPLETE! 🎉                            ║
║                                                                      ║
╚══════════════════════════════════════════════════════════════════════╝

You've just seen the full Atlas workflow:

  ✓ Generated custom MCP tools from a natural language goal
  ✓ Deployed them to Modal serverless infrastructure
  ✓ Registered them in the Modal.Dict tool registry
  ✓ Used the supervisor agent to orchestrate tool execution
  ✓ Got a comprehensive answer to your question

NEXT STEPS:

1. Try different prompts:
   python supervisor.py --prompt "your question here"

2. Generate tools for different domains:
   modal run tools_builder.py --goal "your goal here"

3. Manage the registry:
   modal run registry_manager.py --action list
   modal run registry_manager.py --action test

4. Check the generated code:
   ls -la generated_mcps/
   
5. View deployment logs:
   modal app list
   modal app logs <app-name>

6. Clean up (stop all apps to free endpoints):
   modal app list
   modal app stop <app-id>

Read QUICKSTART.md for detailed documentation.

""")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n✗ Demo cancelled by user.")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n✗ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
