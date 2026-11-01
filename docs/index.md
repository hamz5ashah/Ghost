---
title: Ghost
description: Personal AI, On Personal Devices
search:
  boost: 2
hide:
  - navigation
---

# Personal AI, On Personal Devices

<p class="hero-tagline">
Ghost is a framework for composable, on-device AI systems.
Build personal AI that runs on your hardware. Cloud APIs are optional.
</p>


## Get Started

=== "Browser App"

    Run the full chat UI locally with one script:

    ```bash
    git clone https://github.com/hamz5ashah/Ghost.git
    cd Ghost
    ./scripts/quickstart.sh
    ```

    This installs dependencies, starts Ollama + a local model, launches the backend
    and frontend, and opens `http://localhost:5173` in your browser.

=== "Desktop App"

    The desktop app is a native window for the Ghost UI.
    The backend (Ollama + inference) runs on your machine — start it first, then open the app.

    **Step 1.** Start the backend:

    ```bash
    git clone https://github.com/hamz5ashah/Ghost.git
    cd Ghost
    ./scripts/quickstart.sh
    ```

    **Step 2.** Download and open the desktop app:

    [Download for macOS](https://github.com/hamz5ashah/Ghost/releases/download/desktop-v1.0.2/Ghost_1.0.1_universal.dmg){ .md-button .md-button--primary }

    Also available for [Windows](https://github.com/hamz5ashah/Ghost/releases/download/desktop-v1.0.2/Ghost_1.0.1_x64-setup.exe), [Linux (DEB)](https://github.com/hamz5ashah/Ghost/releases/download/desktop-v1.0.2/Ghost_1.0.1_amd64.deb), and [Linux (RPM)](https://github.com/hamz5ashah/Ghost/releases/download/desktop-v1.0.2/Ghost-1.0.1-1.x86_64.rpm). See the [Downloads](downloads.md) page for details.

    The app connects to `http://localhost:8000` automatically.

    !!! warning "macOS first launch"

        Run `xattr -cr /Applications/Ghost.app` if the app shows as "damaged".

=== "Python SDK"

    ```python
    from ghost import Ghost

    j = Ghost()                              # auto-detect engine
    response = j.ask("Explain quicksort.")
    print(response)
    ```

    For more control, use `ask_full()` to get usage stats, model info, and tool results:

    ```python
    result = j.ask_full(
        "What is 2 + 2?",
        agent="orchestrator",
        tools=["calculator"],
    )
    print(result["content"])       # "4"
    print(result["tool_results"])  # [{tool_name: "calculator", ...}]
    ```

=== "CLI"

    ```bash
    ghost ask "What is the capital of France?"

    ghost ask --agent orchestrator --tools calculator "What is 137 * 42?"

    ghost serve --port 8000

    ghost memory index ./docs/
    ghost memory search "configuration options"
    ```

---

## Five Primitives for Personal AI

Ghost is built around five composable layers. Each has a clean interface and can be swapped independently.

1. **Intelligence** — Pick a model, or let Ghost pick one for your hardware. Manages the full catalog of local models across providers.
2. **Engine** — The inference runtime: [Ollama](https://ollama.com), [vLLM](https://github.com/vllm-project/vllm), [SGLang](https://github.com/sgl-project/sglang), [llama.cpp](https://github.com/ggerganov/llama.cpp), cloud APIs, and more. Auto-detects your hardware and recommends the best fit.
3. **Agents** — Multi-step reasoning with tool use. Eight built-in agent types from simple chat to orchestrated workflows.
4. **Tools & Memory** — Web search, calculator, file I/O, code interpreter, retrieval, persistent local state, and any external MCP server.
5. **Learning** — Your AI gets better over time. Every interaction generates traces that drive automatic improvements to model weights, prompts, and agent behavior.

---

## Key Features

<div class="grid cards" markdown>

-   **10+ Engine Backends**

    ---

    [Ollama](https://ollama.com), [vLLM](https://github.com/vllm-project/vllm), [SGLang](https://github.com/sgl-project/sglang), [llama.cpp](https://github.com/ggerganov/llama.cpp), [MLX](https://github.com/ml-explore/mlx), [Exo](https://github.com/exo-explore/exo), [LiteLLM](https://github.com/BerriAI/litellm), cloud (OpenAI/Anthropic/Google), and more. Same `InferenceEngine` interface, swap freely.

-   **Automated Workflows**

    ---

    Cron-based agents that monitor, summarize, and act. Code review, email triage, research digests — running 24/7 on your hardware.

-   **Hardware-Aware**

    ---

    Auto-detects GPU vendor, model, and VRAM. Recommends the optimal engine for your hardware.

-   **Offline-First**

    ---

    All core functionality works without a network connection. Cloud APIs are optional extras.

-   **OpenAI-Compatible API**

    ---

    `ghost serve` starts a FastAPI server with SSE streaming. Drop-in replacement for OpenAI clients.

-   **Energy & Cost Tracking**

    ---

    Built-in telemetry for GPU power draw, token costs, and latency. See exactly what each query costs in watts and dollars.

</div>

---

## Documentation

<div class="grid cards" markdown>

-   **[Getting Started](getting-started/installation.md)**

    ---

    Install Ghost, configure your first engine, and run your first query.

-   **[User Guide](user-guide/cli.md)**

    ---

    CLI, Python SDK, and guides for [Morning Digest](user-guide/morning-digest.md), [Deep Research](user-guide/deep-research.md), [Code Assistant](user-guide/code-assistant.md), [Scheduled Monitor](user-guide/scheduled-monitor.md), [Simple Chat](user-guide/chat-simple.md), [Evaluations](user-guide/evaluations.md), agents, memory, tools, and telemetry.

-   **[Architecture](architecture/overview.md)**

    ---

    Five-primitive design, registry pattern, query flow, and cross-cutting learning.

-   **[API Reference](api-reference/ghost/index.md)**

    ---

    Auto-generated reference for every module.

-   **[Deployment](deployment/docker.md)**

    ---

    Docker, systemd, launchd. GPU-accelerated container images.

</div>
