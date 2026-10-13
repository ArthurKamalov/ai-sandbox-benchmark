# providers/substrate.py

import asyncio
import importlib
import logging
import os
import shlex
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List, Optional, Tuple

from metrics import BenchmarkTimingMetrics

logger = logging.getLogger(__name__)

# Agent Substrate is driven through agent-substrate/env (the ate-env-api service).
# Its Python client, ate-env-client (import package ate_env), is not a dependency
# of this repository; it is installed separately from the env commit below, which
# requires Substrate at or after f03327bd on the cluster.
ENV_COMMIT = "f76fe8bbdbd5230aaac0e048309f58a4f6561f29"
CLIENT_MODULE = "ate_env"
CLIENT_INSTALL = (
    f"pip install 'ate-env-client @ git+https://github.com/agent-substrate/env@{ENV_COMMIT}"
    "#subdirectory=clients/python'"
)

# Written with a shell command rather than write_file, so it works even when the
# template confines the guest's file API to a workspace root.
VERIFY_PATH = "/tmp/suspend_resume_verify.txt"
VERIFY_CONTENT = "Stateful Sandbox Persistence Test OK"

# Packages installed when a test config does not list its own.
DEFAULT_PACKAGES = ["numpy", "scipy"]

CONNECT_TIMEOUT_SECONDS = 30
RPC_TIMEOUT_SECONDS = 300
READY_TIMEOUT_SECONDS = 180
PROBE_TIMEOUT_SECONDS = 60
READY_POLL_INTERVAL_SECONDS = 0.05
EXEC_TIMEOUT_SECONDS = 1800

DEPENDENCY_CHECK_SCRIPT = """
import sys
sys.path.insert(0, {workdir!r})
from providers.utils import check_and_install_dependencies
with open({code_path!r}) as f:
    code = f.read()
installed = check_and_install_dependencies(code, always_install={packages!r})
print(f"Installed packages: {{installed}}")
"""

_client: Optional[ModuleType] = None


class SubstrateClientNotInstalled(ImportError):
    """Raised when the ate_env client for Agent Substrate environments cannot be imported."""


@dataclass
class SubstrateConfig:
    """Settings for reaching ate-env-api and creating environments, read from the environment."""

    api_addr: str
    atespace: str
    template_name: Optional[str]
    template_atespace: Optional[str]
    rootfs_image: Optional[str]
    workdir: str


def log_info(message):
    logger.info(f"[Substrate] {message}")


def log_error(message):
    logger.error(f"[Substrate] {message}")


def log_warning(message):
    logger.warning(f"[Substrate] {message}")


def load_client() -> ModuleType:
    """
    Import the ate_env client on first use.

    Importing lazily keeps the benchmark usable for every other provider when
    the client is not installed.

    Returns:
        The ate_env module.

    Raises:
        SubstrateClientNotInstalled: If ate_env or one of its dependencies (grpcio, protobuf) is missing.
    """
    global _client
    if _client is None:
        try:
            _client = importlib.import_module(CLIENT_MODULE)
        except ImportError as e:
            raise SubstrateClientNotInstalled(
                f"Agent Substrate env client not found ({e}). Install it with: {CLIENT_INSTALL}"
            ) from e
        log_info(f"Loaded {CLIENT_MODULE} client")
    return _client


def load_config() -> SubstrateConfig:
    """
    Read the Substrate env settings from the environment.

    SUBSTRATE_TEMPLATE is '<name>' or '<atespace>/<name>'; when unset, ate-env-api
    uses its default template.

    Returns:
        The parsed configuration.
    """
    template = os.getenv("SUBSTRATE_TEMPLATE", "").strip()
    template_atespace, _, template_name = template.rpartition("/")
    workdir = os.getenv("SUBSTRATE_WORKDIR", "").strip() or "/workspace"
    return SubstrateConfig(
        api_addr=os.getenv("SUBSTRATE_ENV_API_ADDR", "").strip() or "localhost:7777",
        atespace=os.getenv("SUBSTRATE_ATESPACE", "").strip() or "ate-env",
        template_name=template_name or None,
        template_atespace=template_atespace or None,
        rootfs_image=os.getenv("SUBSTRATE_ROOTFS_IMAGE", "").strip() or None,
        workdir=workdir.rstrip("/") or "/",
    )


def _describe_error(e: Exception) -> str:
    """Render an exception as one line, prefixed with its gRPC status code or ate_env error type."""
    message = str(e) or type(e).__name__
    name = getattr(getattr(e, "code", None), "name", None)
    if not name and type(e).__module__.startswith(CLIENT_MODULE):
        name = type(e).__name__
    return f"{name}: {message}" if name and name not in message else message


def _exit_error(what: str, result: Any) -> RuntimeError:
    """Build the error for a command that exited non-zero, with its stderr tail when present."""
    stderr = result.stderr.strip()[-500:]
    return RuntimeError(f"{what} exited with code {result.exit_code}" + (f": {stderr}" if stderr else ""))


async def _open_channel(grpc: ModuleType, config: SubstrateConfig) -> Any:
    """Open the plaintext gRPC channel to ate-env-api and wait until it connects."""
    target = config.api_addr.replace("http://", "").replace("https://", "")
    channel = grpc.aio.insecure_channel(target)
    try:
        await asyncio.wait_for(channel.channel_ready(), timeout=CONNECT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        await channel.close()
        raise ConnectionError(
            f"Could not connect to ate-env-api at {config.api_addr} within "
            f"{CONNECT_TIMEOUT_SECONDS}s; check SUBSTRATE_ENV_API_ADDR and the port-forward"
        ) from None
    return channel


async def _wait_ready(client: ModuleType, env: Any) -> None:
    """
    Run a trivial command until the guest answers it.

    A new or suspended environment is woken by its first guest call, which fails
    or blocks while the actor boots, so failures are retried until the deadline.
    """
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    last_error = "no probe attempted"
    while True:
        try:
            result = await asyncio.wait_for(env.shell("true"), timeout=PROBE_TIMEOUT_SECONDS)
            if result.exit_code == 0:
                return
            last_error = f"probe exited {result.exit_code}: {result.stderr.strip()[:200]}"
        except client.EnvError as e:
            last_error = _describe_error(e)
        except asyncio.TimeoutError:
            last_error = f"probe did not answer within {PROBE_TIMEOUT_SECONDS}s"
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Environment {env.id} not ready after {READY_TIMEOUT_SECONDS}s: {last_error}"
            )
        await asyncio.sleep(READY_POLL_INTERVAL_SECONDS)


async def _shell(env: Any, command: str, what: str, **kwargs: Any) -> Any:
    """Run a shell command in the environment, raising if it exits non-zero."""
    result = await asyncio.wait_for(
        env.shell(command, timeout=EXEC_TIMEOUT_SECONDS, **kwargs),
        timeout=EXEC_TIMEOUT_SECONDS + 30,
    )
    if result.exit_code != 0:
        raise _exit_error(what, result)
    return result


async def _install_dependencies(env: Any, workdir: str, code_path: str, packages: List[str]) -> None:
    """Install the test's third-party imports using the shared providers.utils helpers."""
    utils_source = Path(__file__).with_name("utils.py").read_bytes()
    await env.write_file(f"{workdir}/providers/__init__.py", b"")
    await env.write_file(f"{workdir}/providers/utils.py", utils_source)

    script = DEPENDENCY_CHECK_SCRIPT.format(workdir=workdir, code_path=code_path, packages=packages)
    result = await asyncio.wait_for(
        env.shell("python3 -", stdin=script, cwd=workdir, timeout=EXEC_TIMEOUT_SECONDS),
        timeout=EXEC_TIMEOUT_SECONDS + 30,
    )
    log_info(f"Dependency check output: {result.stdout.strip()}")
    if result.exit_code != 0:
        log_warning(f"Dependency check failed: {result.stderr.strip()[-500:]}")


async def execute(code: Any, env_vars: Optional[Dict[str, str]] = None) -> Tuple[str, BenchmarkTimingMetrics]:
    """
    Run test code in a new Agent Substrate environment and time each lifecycle phase.

    The environment is created through ate-env-api, woken by its first command,
    optionally suspended and woken again when the test sets measure_lifecycle,
    and always deleted afterwards.

    Args:
        code: The test code, or a dict with 'code' and 'config' keys.
        env_vars: Environment variables to set for the test code.

    Returns:
        A tuple of the code's stdout (or an "Error: ..." message) and the collected metrics.
    """
    metrics = BenchmarkTimingMetrics()

    test_config: Dict[str, Any] = {}
    if isinstance(code, dict) and "code" in code:
        test_config = code.get("config", {}) or {}
        code = code["code"]

    channel = None
    env = None
    try:
        client = load_client()
        import grpc  # Installed with ate_env.

        config = load_config()
        code_path = f"{config.workdir}/code.py"

        # Connect before timing so channel setup is not counted as workspace creation.
        channel = await _open_channel(grpc, config)
        ate = client.Client(channel=channel)

        env_id = f"bench-{uuid.uuid4().hex[:12]}"
        log_info(f"Creating environment {config.atespace}/{env_id}...")
        start = time.time()
        env = await asyncio.wait_for(
            ate.create(
                env_id,
                atespace=config.atespace,
                template_name=config.template_name,
                template_atespace=config.template_atespace,
                rootfs_image=config.rootfs_image,
            ),
            timeout=RPC_TIMEOUT_SECONDS,
        )
        await _wait_ready(client, env)
        metrics.add_metric("Workspace Creation", time.time() - start)
        log_info("Environment serving")

        await env.write_file(code_path, code.encode())

        setup_start = time.time()
        await _install_dependencies(
            env, config.workdir, code_path, test_config.get("packages") or DEFAULT_PACKAGES
        )
        if test_config.get("setup_script"):
            log_info("Running custom setup script from test config...")
            setup_path = f"{config.workdir}/custom_setup.py"
            await env.write_file(setup_path, test_config["setup_script"].encode())
            setup = await _shell(
                env, f"python3 {shlex.quote(setup_path)}", "Custom setup script", cwd=config.workdir
            )
            log_info(f"Custom setup script stdout: {setup.stdout}")
        metrics.add_metric("Setup Time", time.time() - setup_start)

        if test_config.get("measure_lifecycle"):
            log_info("Measuring suspend/resume lifecycle latency...")
            await _shell(
                env, f"cat > {shlex.quote(VERIFY_PATH)}", "Writing verification file",
                stdin=VERIFY_CONTENT,
            )

            suspend_start = time.time()
            await asyncio.wait_for(env.suspend(), timeout=RPC_TIMEOUT_SECONDS)
            metrics.add_metric("Suspend", time.time() - suspend_start)
            log_info("Environment suspended")

            # env has no resume call: the first guest call wakes the environment.
            resume_start = time.time()
            await _wait_ready(client, env)
            metrics.add_metric("Resume", time.time() - resume_start)
            log_info("Environment resumed")

        log_info("Running code in sandbox...")
        exec_start = time.time()
        result = await asyncio.wait_for(
            env.shell(
                f"python3 {shlex.quote(code_path)}",
                cwd=config.workdir,
                env=env_vars or None,
                timeout=EXEC_TIMEOUT_SECONDS,
            ),
            timeout=EXEC_TIMEOUT_SECONDS + 30,
        )
        metrics.add_metric("Code Execution", time.time() - exec_start)

        if result.stderr:
            log_info(f"Stderr from execution: {result.stderr}")
        if result.exit_code != 0:
            raise _exit_error("Code", result)
        return result.stdout, metrics

    except Exception as e:
        message = _describe_error(e)
        metrics.add_error(message)
        log_error(f"Execution error: {message}")
        return f"Error: {message}", metrics

    finally:
        if env is not None:
            # ate-env-api suspends the environment before deleting it, so this includes a snapshot.
            cleanup_start = time.time()
            try:
                await asyncio.wait_for(env.delete(), timeout=RPC_TIMEOUT_SECONDS)
                metrics.add_metric("Cleanup", time.time() - cleanup_start)
                log_info("Environment deleted")
            except Exception as e:
                log_error(f"Cleanup error for {env.atespace}/{env.id}: {_describe_error(e)}")
        if channel is not None:
            await channel.close()
