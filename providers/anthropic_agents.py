# providers/anthropic_agents.py

import asyncio
import hashlib
import json
import logging
import os
import shlex
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import anthropic

from metrics import BenchmarkTimingMetrics

logger = logging.getLogger(__name__)

# Claude Managed Agents runs a session's tools in a container Anthropic hosts, but
# has no call that runs a command in it: only the agent's bash tool does. So the
# agent here has bash (and read, which mounted files need) and is told to run
# each command it is sent verbatim with bash,
# and every step costs a model turn. Setup Time and Code Execution are the time
# from the agent's bash call to its result, by the server's event timestamps,
# which leave the model out. Workspace Creation is wall-clock from creating the
# session until its first command answers, model turn included, as the container
# only comes up once the session first needs it. There is no suspend or resume.

ENVIRONMENT_NAME = "ai-sandbox-benchmark"
AGENT_NAME = "ai-sandbox-benchmark-runner"
DEFAULT_MODEL = "claude-opus-5"
# The agent only relays commands, so it needs little thought.
DEFAULT_EFFORT = "low"

# Where the run's files are asked to be mounted. The session mounts them,
# read-only, under /mnt/session/uploads followed by this path.
MOUNT_DIR = "/bench"

# Packages installed when a test config does not list its own.
DEFAULT_PACKAGES = ["numpy", "scipy"]

BASH_TIMEOUT_MS = 600_000
STEP_TIMEOUT_SECONDS = BASH_TIMEOUT_MS / 1000 + 120
SETTLE_TIMEOUT_SECONDS = 10

SYSTEM_PROMPT = """\
You run shell commands for a benchmark harness. Each message gives one command \
and a timeout. Call the bash tool exactly once, with exactly that command and \
timeout_ms set to that timeout. Do not change, add to or retry the command, and \
run nothing else. Then reply with the single word: done."""

EXIT_MARKER = "__bench_exit_status="

# Runs one step of a run, so each command the agent is sent is a short one.
RUN_SCRIPT = f"""\
# The run's files are read-only, so its steps run in a directory of their own.
here="$(dirname "$0")"
mkdir -p /tmp/bench && cd /tmp/bench
# The container's Python is the system's, which pip otherwise refuses to install into.
export PIP_BREAK_SYSTEM_PACKAGES=1
case "$1" in
  probe) true ;;
  setup) python3 "$here/check_deps.py" && if [ -f "$here/custom_setup.py" ]; then python3 "$here/custom_setup.py"; fi ;;
  code) if [ -f "$here/env.sh" ]; then . "$here/env.sh"; fi; python3 "$here/code.py" ;;
  *) echo "unknown step $1" >&2; false ;;
esac 2>/tmp/bench_stderr
status=$?
echo "{EXIT_MARKER}$status"
if [ "$status" -ne 0 ]; then tail -c 500 /tmp/bench_stderr; fi
"""

DEPENDENCY_CHECK_SCRIPT = """
import os
import sys
here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, here)
from providers.utils import check_and_install_dependencies
with open(os.path.join(here, "code.py")) as f:
    code = f.read()
installed = check_and_install_dependencies(code, always_install={packages!r})
print(f"Installed packages: {{installed}}")
"""

_client: Optional[anthropic.AsyncAnthropic] = None
_resources: Optional[Tuple[str, str]] = None
_resources_lock = asyncio.Lock()


def log_info(message):
    logger.info(f"[Anthropic] {message}")


def log_error(message):
    logger.error(f"[Anthropic] {message}")


def log_warning(message):
    logger.warning(f"[Anthropic] {message}")


def _agent_config() -> Dict[str, Any]:
    """The agent's model, prompt and tools; ANTHROPIC_AGENT_MODEL and ANTHROPIC_AGENT_EFFORT override the model's."""
    return {
        "model": {
            "id": os.getenv("ANTHROPIC_AGENT_MODEL", "").strip() or DEFAULT_MODEL,
            "effort": os.getenv("ANTHROPIC_AGENT_EFFORT", "").strip() or DEFAULT_EFFORT,
        },
        "system": SYSTEM_PROMPT,
        "tools": [
            {
                "type": "agent_toolset_20260401",
                "default_config": {"enabled": False},
                "configs": [
                    {"name": "bash", "enabled": True, "permission_policy": {"type": "always_allow"}},
                    # Required by file resources; a run in which the agent calls it fails.
                    {"name": "read", "enabled": True, "permission_policy": {"type": "always_allow"}},
                ],
            }
        ],
    }


async def _environment_and_agent(client: anthropic.AsyncAnthropic) -> Tuple[str, str]:
    """
    Find or create the environment and agent every session uses.

    The agent's name carries a hash of its configuration, so a changed model or
    prompt gets an agent of its own instead of reusing one configured otherwise.
    """
    global _resources
    async with _resources_lock:
        if _resources is None:
            environment_id = None
            async for environment in client.beta.environments.list():
                if environment.name == ENVIRONMENT_NAME:
                    environment_id = environment.id
                    break
            if environment_id is None:
                environment = await client.beta.environments.create(
                    name=ENVIRONMENT_NAME,
                    config={"type": "cloud", "networking": {"type": "unrestricted"}},
                )
                environment_id = environment.id
                log_info(f"Created environment {environment_id}")

            config = _agent_config()
            digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:12]
            name = f"{AGENT_NAME}-{digest}"
            agent_id = None
            async for agent in client.beta.agents.list():
                if agent.name == name:
                    agent_id = agent.id
                    break
            if agent_id is None:
                agent_id = (await client.beta.agents.create(name=name, **config)).id
                log_info(f"Created agent {name} ({agent_id})")
            _resources = (environment_id, agent_id)
    return _resources


def _run_files(code: str, test_config: Dict[str, Any], env_vars: Optional[Dict[str, str]]) -> Dict[str, bytes]:
    """The files a run mounts, by their path under MOUNT_DIR."""
    packages = test_config.get("packages") or DEFAULT_PACKAGES
    files = {
        "run.sh": RUN_SCRIPT.encode(),
        "code.py": code.encode(),
        "check_deps.py": DEPENDENCY_CHECK_SCRIPT.format(packages=packages).encode(),
        # Imported as a namespace package, without an __init__.py.
        "providers/utils.py": Path(__file__).with_name("utils.py").read_bytes(),
    }
    if test_config.get("setup_script"):
        files["custom_setup.py"] = test_config["setup_script"].encode()
    if env_vars:
        files["env.sh"] = "".join(f"export {k}={shlex.quote(v)}\n" for k, v in env_vars.items()).encode()
    return files


async def _run_step(client: anthropic.AsyncAnthropic, session_id: str, workdir: str, step: str) -> Tuple[str, float]:
    """
    Have the agent run one step of run.sh.

    Returns:
        The step's stdout and the seconds from the agent's bash call to its result.

    Raises:
        RuntimeError: If the agent ran anything but the step's command, or the step failed.
    """
    command = f"bash {workdir}/run.sh {step}"
    stream = await client.beta.sessions.events.stream(session_id)
    async with stream:
        # Stream first, so none of the turn's events are missed.
        await client.beta.sessions.events.send(
            session_id,
            events=[{
                "type": "user.message",
                "content": [{"type": "text", "text": f"command: {command}\ntimeout_ms: {BASH_TIMEOUT_MS}"}],
            }],
        )
        running = False
        tool_use = None
        result = None
        async for event in stream:
            if event.type == "session.status_running":
                running = True
            elif event.type == "agent.tool_use":
                ran = (event.input or {}).get("command", "")
                if tool_use is not None or event.name != "bash" or ran.strip() != command:
                    raise RuntimeError(f"Agent ran something other than {command!r}: {event.name} {ran!r}")
                tool_use = event
            elif event.type == "agent.tool_result" and tool_use is not None and event.tool_use_id == tool_use.id:
                result = event
            elif event.type == "session.error":
                error = f"{event.error.type}: {getattr(event.error, 'message', '')}"
                retry = getattr(getattr(event.error, "retry_status", None), "type", None)
                if retry == "retrying":
                    log_warning(f"Session error, retrying: {error}")
                    continue
                raise RuntimeError(f"Session error: {error}")
            elif event.type == "session.status_terminated":
                raise RuntimeError("Session terminated")
            elif event.type == "session.status_idle" and running:
                if event.stop_reason.type != "end_turn":
                    raise RuntimeError(f"Agent stopped with {event.stop_reason.type}")
                break

    if result is None:
        raise RuntimeError(f"Agent did not run {command!r}")
    text = "".join(block.text for block in result.content or [] if block.type == "text")
    output, marker, rest = text.rpartition(EXIT_MARKER)
    if not marker:
        raise RuntimeError(f"Step {step} printed no exit status: {text.strip()[-500:]}")
    status, _, stderr = rest.partition("\n")
    if status.strip() != "0":
        raise RuntimeError(f"Step {step} exited with code {status.strip()}" + (f": {stderr.strip()}" if stderr.strip() else ""))
    return output, (result.processed_at - tool_use.processed_at).total_seconds()


async def _settle(client: anthropic.AsyncAnthropic, session_id: str) -> Any:
    """Wait for the session to leave running, as its status trails the stream's idle event and a running session cannot be deleted."""
    deadline = time.monotonic() + SETTLE_TIMEOUT_SECONDS
    while True:
        session = await client.beta.sessions.retrieve(session_id)
        if session.status != "running" or time.monotonic() >= deadline:
            return session
        await asyncio.sleep(0.2)


async def execute(code: Any, env_vars: Optional[Dict[str, str]] = None) -> Tuple[str, BenchmarkTimingMetrics]:
    """
    Run test code in a new Claude Managed Agents session and time each phase.

    The run's files are uploaded and mounted into the session, the agent runs
    each step through its bash tool, and the session, with its container, is
    always deleted afterwards.

    Args:
        code: The test code, or a dict with 'code' and 'config' keys.
        env_vars: Environment variables to set for the test code.

    Returns:
        A tuple of the code's stdout (or an "Error: ..." message) and the collected metrics.
    """
    global _client
    metrics = BenchmarkTimingMetrics()

    test_config: Dict[str, Any] = {}
    if isinstance(code, dict) and "code" in code:
        test_config = code.get("config", {}) or {}
        code = code["code"]

    file_ids: List[str] = []
    session_id = None
    failed = False
    try:
        if _client is None:
            _client = anthropic.AsyncAnthropic()
        client = _client
        environment_id, agent_id = await _environment_and_agent(client)

        # Uploaded before timing, as other providers write their files after creation.
        resources = []
        for path, content in _run_files(code, test_config, env_vars).items():
            uploaded = await client.files.upload(file=(os.path.basename(path), content, "text/plain"))
            file_ids.append(uploaded.id)
            resources.append({"type": "file", "file_id": uploaded.id, "mount_path": f"{MOUNT_DIR}/{path}"})

        log_info("Creating session...")
        start = time.time()
        session = await client.beta.sessions.create(
            agent=agent_id, environment_id=environment_id, resources=resources, title="ai-sandbox-benchmark"
        )
        session_id = session.id
        workdir = next(
            os.path.dirname(r.mount_path) for r in session.resources if os.path.basename(r.mount_path) == "run.sh"
        )
        await asyncio.wait_for(_run_step(client, session_id, workdir, "probe"), timeout=STEP_TIMEOUT_SECONDS)
        metrics.add_metric("Workspace Creation", time.time() - start)
        log_info(f"Session {session_id} serving")

        output, seconds = await asyncio.wait_for(_run_step(client, session_id, workdir, "setup"), timeout=STEP_TIMEOUT_SECONDS)
        log_info(f"Setup output: {output.strip()[-2000:]}")
        metrics.add_metric("Setup Time", seconds)

        if test_config.get("measure_lifecycle"):
            log_warning("Managed Agents sessions cannot be suspended or resumed; skipping lifecycle measurement")

        log_info("Running code in sandbox...")
        output, seconds = await asyncio.wait_for(_run_step(client, session_id, workdir, "code"), timeout=STEP_TIMEOUT_SECONDS)
        metrics.add_metric("Code Execution", seconds)
        return output, metrics

    except Exception as e:
        failed = True
        message = f"{type(e).__name__}: {e}"
        metrics.add_error(message)
        log_error(f"Execution error: {message}")
        return f"Error: {message}", metrics

    finally:
        if session_id is not None:
            try:
                if failed:
                    await client.beta.sessions.events.send(session_id, events=[{"type": "user.interrupt"}])
                session = await _settle(client, session_id)
                log_info(f"Session {session_id} usage: {session.usage}")
                cleanup_start = time.time()
                await client.beta.sessions.delete(session_id)
                metrics.add_metric("Cleanup", time.time() - cleanup_start)
                log_info("Session deleted")
            except Exception as e:
                log_error(f"Cleanup error for session {session_id}: {type(e).__name__}: {e}")
        # The session holds its own copies of the files.
        for file_id in file_ids:
            try:
                await client.files.delete(file_id)
            except Exception as e:
                log_warning(f"Could not delete file {file_id}: {type(e).__name__}: {e}")
