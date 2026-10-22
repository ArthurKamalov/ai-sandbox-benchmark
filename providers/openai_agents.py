# providers/openai_agents.py

import asyncio
import base64
import logging
import os
import shlex
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import openai

from metrics import BenchmarkTimingMetrics

logger = logging.getLogger(__name__)

# The OpenAI Agents API runs a session's commands in a sandbox OpenAI hosts, but
# has no call that runs a command in it: only the agent's shell does. So the
# agent is told to run each command it is sent verbatim, and every step costs a
# model turn. Setup Time and Code Execution are the command's own duration, as
# the server reports it, which leaves the model out. Workspace Creation is
# wall-clock from creating the session until its sandbox is up, by the
# environment's status, with no model turn. Sandboxes suspend on their own when
# idle, but there is no call to suspend or resume one.

DEFAULT_MODEL = "gpt-6-astra"
# The agent only relays commands, so it needs little thought.
DEFAULT_EFFORT = "low"

# Where the run's files are written; the sandbox's working directory is /workspace.
WORKDIR = "/workspace/bench"

# Packages installed when a test config does not list its own.
DEFAULT_PACKAGES = ["numpy", "scipy"]

COMMAND_TIMEOUT_MS = 600_000
STEP_TIMEOUT_SECONDS = COMMAND_TIMEOUT_MS / 1000 + 120
PROVISION_TIMEOUT_SECONDS = 600
SETTLE_TIMEOUT_SECONDS = 10
DELETE_ATTEMPTS = 10

INSTRUCTIONS = """\
You run shell commands for a benchmark harness. Each message gives one command \
and a timeout. Run exactly that command once, with that timeout. Do not change, \
add to or retry the command, and run nothing else. Then reply with the single \
word: done."""

EXIT_MARKER = "__bench_exit_status="

# Runs one step of a run, so each command the agent is sent is a short one.
RUN_SCRIPT = f"""\
here="$(dirname "$0")"
cd "$here"
# The sandbox's Python may be the system's, which pip otherwise refuses to install into.
export PIP_BREAK_SYSTEM_PACKAGES=1
case "$1" in
  setup) python3 check_deps.py && if [ -f custom_setup.py ]; then python3 custom_setup.py; fi ;;
  code) if [ -f env.sh ]; then . ./env.sh; fi; python3 code.py ;;
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

_client: Optional[openai.AsyncOpenAI] = None


def log_info(message):
    logger.info(f"[OpenAI] {message}")


def log_error(message):
    logger.error(f"[OpenAI] {message}")


def log_warning(message):
    logger.warning(f"[OpenAI] {message}")


def _agent_config() -> Dict[str, Any]:
    """The agent's model and instructions; OPENAI_AGENT_MODEL and OPENAI_AGENT_EFFORT override the model's."""
    return {
        "model": os.getenv("OPENAI_AGENT_MODEL", "").strip() or DEFAULT_MODEL,
        "reasoning": {"effort": os.getenv("OPENAI_AGENT_EFFORT", "").strip() or DEFAULT_EFFORT},
        "instructions": INSTRUCTIONS,
    }


def _run_files(code: str, test_config: Dict[str, Any], env_vars: Optional[Dict[str, str]]) -> List[Dict[str, str]]:
    """The files a run writes under WORKDIR, as the environment's inline files."""
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
    return [
        {"type": "inline", "path": f"{WORKDIR}/{path}", "data": base64.b64encode(content).decode()}
        for path, content in files.items()
    ]


async def _wait_provisioned(client: openai.AsyncOpenAI, environment_id: str) -> None:
    """Wait for the session's sandbox to finish provisioning."""
    deadline = time.monotonic() + PROVISION_TIMEOUT_SECONDS
    while True:
        environment = await client.beta.agents.environments.retrieve(environment_id)
        if environment.status in ("ready", "connected"):
            return
        if environment.status in ("failed", "expired"):
            raise RuntimeError(f"Environment {environment_id} is {environment.status}")
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Environment {environment_id} still {environment.status} after {PROVISION_TIMEOUT_SECONDS}s")
        await asyncio.sleep(0.2)


async def _run_step(client: openai.AsyncOpenAI, session_id: str, step: str) -> Tuple[str, float]:
    """
    Have the agent run one step of run.sh.

    Returns:
        The step's output and the command's duration in seconds.

    Raises:
        RuntimeError: If the agent ran anything but the step's command, or the step failed.
    """
    command = f"bash {WORKDIR}/run.sh {step}"
    execution = None
    async with client.beta.agents.sessions.stream(
        session_id, input=f"command: {command}\ntimeout_ms: {COMMAND_TIMEOUT_MS}"
    ) as stream:
        async for event in stream:
            if event.type == "agent.session.turn.item.done" and event.item.type == "command_execution":
                # The harness may wrap the command in a shell invocation of its own.
                if execution is not None or command not in event.item.command:
                    raise RuntimeError(f"Agent ran something other than {command!r}: {event.item.command!r}")
                execution = event.item
            elif event.type in ("agent.session.turn.failed", "agent.session.turn.cancelled"):
                error = event.turn.error
                raise RuntimeError(f"Turn {event.turn.status}" + (f": {error.code}: {error.message}" if error else ""))
            elif event.type == "agent.session.failed":
                raise RuntimeError(f"Session failed: {event.session.error}")
            elif event.type == "error":
                raise RuntimeError(f"Session error: {event.error.type}: {event.error.message}")

    if execution is None:
        raise RuntimeError(f"Agent did not run {command!r}")
    if execution.duration_ms is None:
        raise RuntimeError(f"Step {step} reported no duration (status {execution.status})")
    text = execution.output or ""
    output, marker, rest = text.rpartition(EXIT_MARKER)
    if not marker:
        raise RuntimeError(f"Step {step} printed no exit status (exit code {execution.exit_code}): {text.strip()[-500:]}")
    status, _, stderr = rest.partition("\n")
    if status.strip() != "0":
        raise RuntimeError(f"Step {step} exited with code {status.strip()}" + (f": {stderr.strip()}" if stderr.strip() else ""))
    return output, execution.duration_ms / 1000


async def _settle(client: openai.AsyncOpenAI, session_id: str) -> Any:
    """Wait for the session's turn to end, as a session in progress cannot be deleted."""
    deadline = time.monotonic() + SETTLE_TIMEOUT_SECONDS
    while True:
        session = await client.beta.agents.sessions.retrieve(session_id)
        if session.status != "in_progress" or time.monotonic() >= deadline:
            return session
        await asyncio.sleep(0.2)


async def _delete(client: openai.AsyncOpenAI, session_id: str) -> None:
    """Delete the session, retrying while it is still setting up or running, which the API answers with 409."""
    for attempt in range(DELETE_ATTEMPTS):
        try:
            await client.beta.agents.sessions.delete(session_id)
            return
        except openai.ConflictError:
            if attempt == DELETE_ATTEMPTS - 1:
                raise
            await asyncio.sleep(0.5)


async def execute(code: Any, env_vars: Optional[Dict[str, str]] = None) -> Tuple[str, BenchmarkTimingMetrics]:
    """
    Run test code in a new OpenAI Agents API session and time each phase.

    The run's files are written into the session's OpenAI-hosted sandbox, the
    agent runs each step through its shell, and the session, with its sandbox,
    is always deleted afterwards.

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

    session_id = None
    failed = False
    try:
        if _client is None:
            _client = openai.AsyncOpenAI()
        client = _client
        files = _run_files(code, test_config, env_vars)

        log_info("Creating session...")
        start = time.time()
        environment = {"type": "openai_hosted", "network": {"access": "enabled"}, "files": files}
        # small, medium or large; unset, OpenAI's default, medium. See
        # https://developers.openai.com/api/docs/guides/agents-api/environments/openai-hosted
        container_size = os.getenv("OPENAI_CONTAINER_SIZE", "").strip()
        if container_size:
            environment["container_size"] = container_size
        session = await client.beta.agents.sessions.create(
            agent=_agent_config(),
            environment=environment,
            metadata={"title": "ai-sandbox-benchmark"},
        )
        session_id = session.id
        await _wait_provisioned(client, session.environment.id)
        metrics.add_metric("Workspace Creation", time.time() - start)
        log_info(f"Session {session_id} provisioned")

        output, seconds = await asyncio.wait_for(_run_step(client, session_id, "setup"), timeout=STEP_TIMEOUT_SECONDS)
        log_info(f"Setup output: {output.strip()[-2000:]}")
        metrics.add_metric("Setup Time", seconds)

        if test_config.get("measure_lifecycle"):
            log_warning("Agents API sandboxes cannot be suspended or resumed on request; skipping lifecycle measurement")

        log_info("Running code in sandbox...")
        output, seconds = await asyncio.wait_for(_run_step(client, session_id, "code"), timeout=STEP_TIMEOUT_SECONDS)
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
            if failed:
                try:
                    await client.beta.agents.sessions.events.create(
                        session_id, events=[{"type": "agent.session.input.cancel"}]
                    )
                except Exception as e:
                    # The session may have no turn left to cancel.
                    log_warning(f"Could not cancel session {session_id}: {type(e).__name__}: {e}")
            try:
                session = await _settle(client, session_id)
                log_info(f"Session {session_id} usage: {session.usage}")
                cleanup_start = time.time()
                await _delete(client, session_id)
                metrics.add_metric("Cleanup", time.time() - cleanup_start)
                log_info("Session deleted")
            except Exception as e:
                log_error(f"Cleanup error for session {session_id}: {type(e).__name__}: {e}")
