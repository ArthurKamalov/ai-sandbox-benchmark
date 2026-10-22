# Provider Implementations for AI Sandbox Benchmark

This directory contains implementations for various sandbox providers supported by the AI Sandbox Benchmark project.

## Supported Providers

The benchmark currently supports the following execution environments:

- **Daytona** - Cloud-based code execution environment (daytona.py)
- **e2b** - Code interpreter environment (e2b.py)
- **CodeSandbox** - Browser-based sandbox environment (codesandbox.py)
- **Modal** - Serverless compute platform (modal.py)
- **Local** - Local machine execution for baseline comparison (local.py)
- **Substrate** - Agent Substrate environments on your own Kubernetes cluster via [agent-substrate/env](https://github.com/agent-substrate/env) (substrate.py; opt-in, not in the default provider list)
- **Anthropic** - Claude Managed Agents sessions, whose container runs commands only through the agent (anthropic_agents.py; opt-in, not in the default provider list)
- **OpenAI** - Agents API sessions in OpenAI-hosted sandboxes, which run commands only through the agent (openai_agents.py; opt-in, not in the default provider list)

## Provider Setup Instructions

### CodeSandbox

1. **Install Node.js Dependencies and Start the CodeSandbox Service**

   Before running benchmarks with CodeSandbox, start the CodeSandbox service:

   ```bash
   cd providers && npm install && node providers/codesandbox-service.js
   ```
   This service handles the communication between the benchmark and the CodeSandbox API.

### Daytona

Daytona requires API credentials to access their cloud-based execution environment:

1. Create a Daytona account at [daytona.io](https://daytona.io)
2. Get your API key from the Daytona dashboard
3. Add to your `.env` file:
   ```
   DAYTONA_API_KEY=your_daytona_api_key
   DAYTONA_SERVER_URL=your_daytona_server_url
   ```

#### Warm Pools Feature

The Daytona implementation includes support for their warm sandbox pools, which significantly improves workspace creation time:

- Automatically pings Daytona's API before creating workspaces to activate warm pools
- Measures and tracks warmup time in performance metrics
- Use `daytona.list_workspaces(target_region)` to manually activate warm pools

### e2b

e2b requires an API key for their code execution environment:

1. Create an account at [e2b.io](https://e2b.io)
2. Obtain your API key from your account settings
3. Add to your `.env` file:
   ```
   E2B_API_KEY=your_e2b_api_key
   ```

### Modal

Modal uses CLI-based authentication instead of API keys:

1. Create an account at [modal.com](https://modal.com)
2. Install the Modal Python package: `pip install modal`
3. Authenticate with Modal: `modal setup` (if this doesn't work, try `python -m modal setup`)

### Local Provider

The local provider runs tests directly on your machine without any additional setup, making it useful for establishing baseline performance.

### Agent Substrate

The Substrate provider creates one environment per run through `ate-env-api` from
[agent-substrate/env](https://github.com/agent-substrate/env) (alpha API), which runs each
environment as an [Agent Substrate](https://github.com/agent-substrate/substrate) actor.
It is opt-in: pass `--providers substrate` (or include it in your list).

**Versions.** The provider pins env commit `f76fe8bbdbd5230aaac0e048309f58a4f6561f29`
(`ENV_COMMIT` in `substrate.py`), which needs Substrate at or after
[`f03327bd`](https://github.com/agent-substrate/substrate/commit/f03327bd) on the cluster.

1. **Install the Python client.** It is not in `requirements.txt`:

   ```bash
   pip install 'ate-env-client @ git+https://github.com/agent-substrate/env@f76fe8bbdbd5230aaac0e048309f58a4f6561f29#subdirectory=clients/python'
   ```

   It installs alongside `requirements.txt` in the same virtual environment.

2. **Deploy env on a cluster that runs Substrate.** Install the CLI, then apply the namespace,
   worker pool and API service, following the
   [env quickstart](https://github.com/agent-substrate/env/blob/f76fe8bbdbd5230aaac0e048309f58a4f6561f29/README.md#quickstart)
   for the image digests:

   ```bash
   go install github.com/agent-substrate/env/cmd/ate-env@f76fe8bbdbd5230aaac0e048309f58a4f6561f29
   ate-env manifest --api-image <ate-env-api image> --worker-image <ateom image> | kubectl apply -f -
   kubectl get pods -n ate-env
   ```

3. **Register a Python template.** The tests need `python3`, so register a template from a stock
   `python:3.12` image once the `ate-env-api` pod is running:

   ```bash
   ate-env manifest template --template py312 \
     --rootfs-image docker.io/library/python@sha256:<digest> \
     --guest-image <ate-env-guest image> \
     --snapshots-bucket <object-storage-url> | kubectl-ate create actor-template -f -
   ```

   Prefer a registered template over `SUBSTRATE_ROOTFS_IMAGE`: the first environment created
   from a new rootfs image builds a derived template and its golden snapshot, which would be
   counted as Workspace Creation time.

4. **Reach the API.** `ate-env-api` serves plaintext gRPC without authentication; port-forward it:

   ```bash
   kubectl port-forward -n ate-env svc/ate-env-api 7777:7777
   ```

5. **Configure `.env`.** All settings are optional:

   ```
   SUBSTRATE_ENV_API_ADDR=localhost:7777   # ate-env-api address
   SUBSTRATE_TEMPLATE=py312                # '<name>' or '<atespace>/<name>'; default: the API's default template
   SUBSTRATE_ATESPACE=ate-env              # atespace for new environments
   SUBSTRATE_ROOTFS_IMAGE=                 # digest-pinned image, used instead of a template
   SUBSTRATE_WORKDIR=/workspace            # where test code is written
   ```

**Metrics.** Workspace Creation covers creating the environment and waiting for its first
command to succeed (creation does not wake the actor; the first guest call does). With
`measure_lifecycle` tests, Suspend times `suspend()` and Resume times the wait until the first
command after it succeeds, since env has no explicit resume call. Cleanup includes the snapshot
`ate-env-api` takes before deleting an environment.

**Dependencies.** Tests that import `numpy` or `scipy` install them with pip on every run, since
the stock Python image lacks them. That needs egress from the actor and adds to Setup Time; bake
them into a derived rootfs image to avoid it.

**Cleanup.** Each environment is deleted after its run. If a run crashes or deletion fails, the
error is logged with the environment ID (`bench-<hex>`); the env API cannot list environments,
so find and remove leftovers with the Substrate CLI:

```bash
kubectl ate get actors -a ate-env
ate-env delete <id>
```

### Anthropic (Claude Managed Agents)

Each run is a Managed Agents session in Anthropic's cloud. The API has no call that runs a
command in a session's container, so the provider gives its agent the bash tool (and read,
which mounted files require) and sends it one command per step to run verbatim; a run in
which the agent calls anything else fails. It is opt-in: pass `--providers anthropic`.

1. Get an API key from the [Claude Console](https://platform.claude.com) and add it to `.env`:
   ```
   ANTHROPIC_API_KEY=your_anthropic_api_key
   ANTHROPIC_AGENT_MODEL=claude-opus-5   # optional, the agent's model
   ANTHROPIC_AGENT_EFFORT=low            # optional, the agent's effort
   ```

2. The first run creates an environment named `ai-sandbox-benchmark` and an agent named
   `ai-sandbox-benchmark-runner-<hash of its configuration>`; later runs reuse them.

**Metrics.** Each step costs a model turn. Setup Time and Code Execution are the time from
the agent's bash call to its result, by the server's event timestamps, so they leave the
model out. Workspace Creation is wall-clock from creating the session until its first command
answers, one model turn included, as the container comes up only once the session needs it.
Sessions cannot be suspended or resumed, so `measure_lifecycle` tests run without it. Cleanup
deletes the session, and its container with it.

**Cost.** Every run is billed for its model turns and session runtime; the provider logs each
session's usage before deleting it.

### OpenAI (Agents API)

Each run is an Agents API session in an OpenAI-hosted sandbox. The API has no call that runs a
command in a session's sandbox, so the provider sends its agent one command per step to run
verbatim through its shell; a run in which the agent runs anything else fails. It is opt-in:
pass `--providers openai`.

1. Get an API key from the [OpenAI Platform](https://platform.openai.com) and add it to `.env`:
   ```
   OPENAI_API_KEY=your_openai_api_key
   OPENAI_AGENT_MODEL=gpt-6-astra   # optional, the agent's model
   OPENAI_AGENT_EFFORT=low          # optional, the agent's reasoning effort
   ```

2. Each session gets its own sandbox, of the default medium size (2 vCPU, 4 GB), with the run's
   files written under `/workspace/bench`. Nothing outlives the run.

**Metrics.** Each step costs a model turn. Setup Time and Code Execution are the command's own
duration, as the server reports it, so they leave the model out. Workspace Creation is
wall-clock from creating the session until its sandbox's environment reports it provisioned,
with no model turn. Sandboxes suspend on their own when idle but cannot be suspended or resumed
on request, so `measure_lifecycle` tests run without it. Cleanup deletes the session, and its
sandbox with it.

**Cost.** Every run is billed for its model turns and sandbox runtime; the provider logs each
session's usage before deleting it.

## Provider Configuration

Provider-specific settings can be configured in the `config.yml` file in the project root:

```yaml
# Environment variables to pass to sandboxes
env_vars:
  pass_to_sandbox:
    - OPENAI_API_KEY
    # Add other variables as needed

# Provider-specific settings
providers:
  daytona:
    default_region: eu
```

The Substrate provider reads its settings from `SUBSTRATE_*` environment variables rather than `config.yml`.

## Implementation Details

Each provider module implements an `execute(code, ...)` function that handles:

1. Creating and configuring the execution environment
2. Executing the provided code
3. Collecting and returning results
4. Cleaning up resources

The provider implementation handles measuring its own performance metrics including workspace creation time, execution time, and cleanup time.

## Adding New Providers

To add a new provider:

1. Create a new Python file in the `providers` directory (e.g., `newprovider.py`)
2. Implement the required interface, following the pattern of existing providers
3. Make sure to properly handle resource cleanup in finally blocks
4. Update `__init__.py` to expose your new provider
5. Add provider-specific documentation to this README