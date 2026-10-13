# providers/substrate.py

import asyncio
import importlib
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List, Optional, Tuple

import httpx

from metrics import BenchmarkTimingMetrics

logger = logging.getLogger(__name__)

# Agent Substrate release this provider is written against.
SUBSTRATE_RELEASE = "v0.4.0"

# The generated gRPC client is not part of this repository: it is generated from
# pkg/proto/ateapipb/ateapi.proto at SUBSTRATE_RELEASE and installed alongside
# grpcio. By default protoc emits top-level ateapi_pb2 / ateapi_pb2_grpc modules;
# set SUBSTRATE_CLIENT_PACKAGE when an installation places them in a package.
CLIENT_PACKAGE_ENV = "SUBSTRATE_CLIENT_PACKAGE"

# atenet-router selects the actor from this header, not from the URL or Host.
TARGET_ACTOR_HEADER = "ate-target-actor"

# Paths inside the sandbox image.
SANDBOX_DIR = "/sandbox"
CODE_PATH = f"{SANDBOX_DIR}/code.py"
SETUP_SCRIPT_PATH = f"{SANDBOX_DIR}/custom_setup.py"
VERIFY_PATH = "/tmp/suspend_resume_verify.txt"
VERIFY_CONTENT = "Stateful Sandbox Persistence Test OK"

# Packages installed when a test config does not list its own.
DEFAULT_PACKAGES = ["numpy", "scipy"]

CONNECT_TIMEOUT_SECONDS = 30
RPC_TIMEOUT_SECONDS = 300
READY_TIMEOUT_SECONDS = 60
EXEC_TIMEOUT_SECONDS = 1800

DEPENDENCY_CHECK_SCRIPT = """
import sys
sys.path.insert(0, {sandbox_dir!r})
from providers.utils import check_and_install_dependencies
with open({code_path!r}) as f:
    code = f.read()
installed = check_and_install_dependencies(code, always_install={packages!r})
print(f"Installed packages: {{installed}}")
"""

_client: Optional[Tuple[ModuleType, ModuleType]] = None


class SubstrateClientNotInstalled(ImportError):
    """Raised when the generated Agent Substrate gRPC client cannot be imported."""


@dataclass
class SubstrateConfig:
    """Connection settings for an Agent Substrate installation, read from the environment."""

    api_addr: str
    router_url: str
    template_atespace: str
    template_name: str
    atespace: str
    token_file: Optional[str]
    ca_file: Optional[str]
    api_server_name: str
    lifecycle_mode: str


def log_info(message):
    logger.info(f"[Substrate] {message}")


def log_error(message):
    logger.error(f"[Substrate] {message}")


def log_warning(message):
    logger.warning(f"[Substrate] {message}")


def load_client() -> Tuple[ModuleType, ModuleType]:
    """
    Import the generated Agent Substrate gRPC client on first use.

    Importing lazily keeps the benchmark usable for every other provider when
    the client is not installed.

    Returns:
        A tuple of the (ateapi_pb2, ateapi_pb2_grpc) modules.

    Raises:
        SubstrateClientNotInstalled: If the client or its grpcio dependency is missing.
    """
    global _client
    if _client is None:
        package = os.getenv(CLIENT_PACKAGE_ENV, "").strip().strip(".")
        prefix = f"{package}." if package else ""
        try:
            ateapi_pb2 = importlib.import_module(f"{prefix}ateapi_pb2")
            ateapi_pb2_grpc = importlib.import_module(f"{prefix}ateapi_pb2_grpc")
        except ImportError as e:
            raise SubstrateClientNotInstalled(
                f"Agent Substrate gRPC client not found ({e}). Install the Python client "
                f"generated from pkg/proto/ateapipb/ateapi.proto at {SUBSTRATE_RELEASE} "
                f"together with grpcio, or set {CLIENT_PACKAGE_ENV} to the package that "
                f"contains {prefix}ateapi_pb2 and {prefix}ateapi_pb2_grpc."
            ) from e
        log_info(f"Loaded gRPC client from {ateapi_pb2_grpc.__name__}")
        _client = (ateapi_pb2, ateapi_pb2_grpc)
    return _client


def load_config() -> SubstrateConfig:
    """
    Read the Substrate connection settings from the environment.

    Returns:
        The parsed configuration.

    Raises:
        ValueError: If SUBSTRATE_TEMPLATE is missing or malformed, or the lifecycle mode is unknown.
    """
    template = os.getenv("SUBSTRATE_TEMPLATE", "").strip()
    template_atespace, _, template_name = template.partition("/")
    if not template_atespace or not template_name:
        raise ValueError(
            f"SUBSTRATE_TEMPLATE must be '<atespace>/<actor-template>', got {template!r}"
        )

    lifecycle_mode = os.getenv("SUBSTRATE_LIFECYCLE_MODE", "suspend").strip().lower()
    if lifecycle_mode not in ("suspend", "pause"):
        raise ValueError(
            f"SUBSTRATE_LIFECYCLE_MODE must be 'suspend' or 'pause', got {lifecycle_mode!r}"
        )

    router_url = os.getenv("SUBSTRATE_ROUTER_ADDR", "localhost:8000").strip()
    if "://" not in router_url:
        router_url = f"http://{router_url}"

    return SubstrateConfig(
        api_addr=os.getenv("SUBSTRATE_API_ADDR", "localhost:8080").strip(),
        router_url=router_url.rstrip("/"),
        template_atespace=template_atespace,
        template_name=template_name,
        atespace=os.getenv("SUBSTRATE_ATESPACE", "").strip() or template_atespace,
        token_file=os.getenv("SUBSTRATE_TOKEN_FILE") or None,
        ca_file=os.getenv("SUBSTRATE_CA_FILE") or None,
        api_server_name=os.getenv("SUBSTRATE_API_SERVER_NAME", "api.ate-system.svc").strip(),
        lifecycle_mode=lifecycle_mode,
    )


def _open_api_channel(grpc: ModuleType, config: SubstrateConfig) -> Any:
    """Open a TLS channel to ate-api, authenticating with a bearer token when configured."""
    root_certificates = Path(config.ca_file).read_bytes() if config.ca_file else None
    credentials = grpc.ssl_channel_credentials(root_certificates=root_certificates)
    if config.token_file:
        token = Path(config.token_file).read_text().strip()
        credentials = grpc.composite_channel_credentials(
            credentials, grpc.access_token_call_credentials(token)
        )

    options = []
    if config.api_server_name:
        # A port-forwarded ate-api presents a certificate for its in-cluster name.
        options.append(("grpc.ssl_target_name_override", config.api_server_name))
    return grpc.aio.secure_channel(config.api_addr, credentials, options=options)


async def _wait_connected(channel: Any, config: SubstrateConfig) -> None:
    """Wait for the ate-api channel to connect, failing with the settings to check."""
    try:
        await asyncio.wait_for(channel.channel_ready(), timeout=CONNECT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        raise ConnectionError(
            f"Could not connect to ate-api at {config.api_addr} within {CONNECT_TIMEOUT_SECONDS}s; "
            "check SUBSTRATE_API_ADDR, SUBSTRATE_CA_FILE and SUBSTRATE_API_SERVER_NAME"
        ) from None


def _describe_error(e: Exception) -> str:
    """Render an exception as one line, flattening gRPC errors to 'CODE: details'."""
    code, details = getattr(e, "code", None), getattr(e, "details", None)
    if callable(code) and callable(details):
        return f"{code().name}: {details()}"
    return str(e) or type(e).__name__


async def _ensure_atespace(grpc: ModuleType, ateapi_pb2: ModuleType, stub: Any, name: str) -> None:
    """Create the atespace, treating ALREADY_EXISTS as success."""
    try:
        await stub.CreateAtespace(
            ateapi_pb2.CreateAtespaceRequest(
                atespace=ateapi_pb2.Atespace(metadata=ateapi_pb2.ResourceMetadata(name=name))
            ),
            timeout=RPC_TIMEOUT_SECONDS,
        )
        log_info(f"Created atespace {name}")
    except grpc.aio.AioRpcError as e:
        if e.code() != grpc.StatusCode.ALREADY_EXISTS:
            raise


async def _wait_ready(http: httpx.AsyncClient) -> None:
    """Poll the sandbox server through the router until it answers /healthz."""
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    last_error = ""
    while True:
        try:
            response = await http.get("/healthz")
            if response.status_code == 200:
                return
            last_error = f"HTTP {response.status_code}: {response.text[:200]}"
        except httpx.TransportError as e:
            last_error = str(e)
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"Sandbox not ready after {READY_TIMEOUT_SECONDS}s: {last_error}"
            )
        await asyncio.sleep(0.05)


async def _process(
    http: httpx.AsyncClient,
    command: List[str],
    stdin: Optional[str] = None,
    env_vars: Optional[Dict[str, str]] = None,
    cwd: Optional[str] = None,
) -> Dict[str, Any]:
    """Run a command in the sandbox through its /process endpoint and return the decoded response."""
    request: Dict[str, Any] = {"command": command, "timeout": f"{EXEC_TIMEOUT_SECONDS}s"}
    if stdin is not None:
        request["stdin"] = stdin
    if env_vars:
        request["envvars"] = env_vars
    if cwd:
        request["cwd"] = cwd

    response = await http.post("/process", json=request)
    if response.status_code != 200:
        raise RuntimeError(f"/process returned HTTP {response.status_code}: {response.text[:500]}")
    return response.json()


async def _write_file(http: httpx.AsyncClient, path: str, content: str) -> None:
    """Write a file in the sandbox, creating its parent directory."""
    result = await _process(
        http,
        ["sh", "-c", 'mkdir -p "$(dirname "$1")" && cat > "$1"', "sh", path],
        stdin=content,
    )
    if result.get("exitCode") != 0:
        raise RuntimeError(f"Failed to write {path}: {result.get('stderr') or result.get('error')}")


async def _install_dependencies(http: httpx.AsyncClient, packages: List[str]) -> None:
    """Install the test's third-party imports using the shared providers.utils helpers."""
    utils_source = Path(__file__).with_name("utils.py").read_text()
    await _write_file(http, f"{SANDBOX_DIR}/providers/__init__.py", "")
    await _write_file(http, f"{SANDBOX_DIR}/providers/utils.py", utils_source)

    script = DEPENDENCY_CHECK_SCRIPT.format(
        sandbox_dir=SANDBOX_DIR, code_path=CODE_PATH, packages=packages
    )
    result = await _process(http, ["python3", "-c", script], cwd=SANDBOX_DIR)
    log_info(f"Dependency check output: {result.get('stdout', '').strip()}")
    if result.get("exitCode") != 0:
        log_warning(f"Dependency check failed: {result.get('stderr') or result.get('error')}")


async def execute(code: Any, env_vars: Optional[Dict[str, str]] = None) -> Tuple[str, BenchmarkTimingMetrics]:
    """
    Run test code in a new Agent Substrate actor and time each lifecycle phase.

    The actor is created from SUBSTRATE_TEMPLATE, resumed, optionally suspended
    (or paused) and resumed again when the test sets measure_lifecycle, and
    always deleted afterwards.

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
    http: Optional[httpx.AsyncClient] = None
    stub = None
    actor_ref = None
    created = False
    try:
        ateapi_pb2, ateapi_pb2_grpc = load_client()
        import grpc  # Installed with the generated client.

        config = load_config()
        channel = _open_api_channel(grpc, config)
        stub = ateapi_pb2_grpc.ControlStub(channel)

        # Connect before timing so the TLS handshake is not counted as workspace creation.
        await _wait_connected(channel, config)

        if config.atespace != config.template_atespace:
            await _ensure_atespace(grpc, ateapi_pb2, stub, config.atespace)

        actor_ref = ateapi_pb2.ObjectRef(
            atespace=config.atespace, name=f"bench-{uuid.uuid4().hex[:12]}"
        )
        http = httpx.AsyncClient(
            base_url=config.router_url,
            headers={TARGET_ACTOR_HEADER: f"{actor_ref.atespace}/{actor_ref.name}"},
            timeout=httpx.Timeout(EXEC_TIMEOUT_SECONDS + 30, connect=10),
        )

        log_info(f"Creating actor {actor_ref.atespace}/{actor_ref.name}...")
        start = time.time()
        await stub.CreateActor(
            ateapi_pb2.CreateActorRequest(
                actor=ateapi_pb2.Actor(
                    metadata=ateapi_pb2.ResourceMetadata(
                        atespace=actor_ref.atespace, name=actor_ref.name
                    ),
                    actor_template=ateapi_pb2.ObjectRef(
                        atespace=config.template_atespace, name=config.template_name
                    ),
                )
            ),
            timeout=RPC_TIMEOUT_SECONDS,
        )
        created = True
        await stub.ResumeActor(
            ateapi_pb2.ResumeActorRequest(actor=actor_ref), timeout=RPC_TIMEOUT_SECONDS
        )
        await _wait_ready(http)
        metrics.add_metric("Workspace Creation", time.time() - start)
        log_info("Actor running")

        await _write_file(http, CODE_PATH, code)

        setup_start = time.time()
        await _install_dependencies(http, test_config.get("packages") or DEFAULT_PACKAGES)
        if test_config.get("setup_script"):
            log_info("Running custom setup script from test config...")
            await _write_file(http, SETUP_SCRIPT_PATH, test_config["setup_script"])
            setup = await _process(http, ["python3", SETUP_SCRIPT_PATH], cwd=SANDBOX_DIR)
            log_info(f"Custom setup script stdout: {setup.get('stdout', '')}")
            if setup.get("exitCode") != 0:
                raise RuntimeError(
                    f"Custom setup script failed with exit code {setup.get('exitCode')}: "
                    f"{setup.get('stderr') or setup.get('error')}"
                )
        metrics.add_metric("Setup Time", time.time() - setup_start)

        if test_config.get("measure_lifecycle"):
            log_info(f"Measuring {config.lifecycle_mode}/resume lifecycle latency...")
            await _write_file(http, VERIFY_PATH, VERIFY_CONTENT)

            suspend_start = time.time()
            if config.lifecycle_mode == "pause":
                await stub.PauseActor(
                    ateapi_pb2.PauseActorRequest(actor=actor_ref), timeout=RPC_TIMEOUT_SECONDS
                )
            else:
                await stub.SuspendActor(
                    ateapi_pb2.SuspendActorRequest(actor=actor_ref), timeout=RPC_TIMEOUT_SECONDS
                )
            metrics.add_metric("Suspend", time.time() - suspend_start)
            log_info(f"Actor {config.lifecycle_mode}d")

            resume_start = time.time()
            await stub.ResumeActor(
                ateapi_pb2.ResumeActorRequest(actor=actor_ref), timeout=RPC_TIMEOUT_SECONDS
            )
            await _wait_ready(http)
            metrics.add_metric("Resume", time.time() - resume_start)
            log_info("Actor resumed")

        log_info("Running code in sandbox...")
        exec_start = time.time()
        result = await _process(http, ["python3", CODE_PATH], env_vars=env_vars, cwd=SANDBOX_DIR)
        metrics.add_metric("Code Execution", time.time() - exec_start)

        if result.get("stderr"):
            log_info(f"Stderr from execution: {result['stderr']}")
        if result.get("exitCode") != 0:
            raise RuntimeError(
                f"Code exited with code {result.get('exitCode')}: "
                f"{result.get('stderr') or result.get('error')}"
            )
        return result.get("stdout", ""), metrics

    except Exception as e:
        message = _describe_error(e)
        metrics.add_error(message)
        log_error(f"Execution error: {message}")
        return f"Error: {message}", metrics

    finally:
        if created:
            cleanup_start = time.time()
            try:
                await stub.DeleteActor(
                    ateapi_pb2.DeleteActorRequest(actor=actor_ref, any_state=True),
                    timeout=RPC_TIMEOUT_SECONDS,
                )
                metrics.add_metric("Cleanup", time.time() - cleanup_start)
                log_info("Actor deleted")
            except Exception as e:
                log_error(
                    f"Cleanup error for {actor_ref.atespace}/{actor_ref.name}: {_describe_error(e)}"
                )
        if http:
            await http.aclose()
        if channel:
            await channel.close()
