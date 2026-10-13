# providers/substrate.py

import importlib
import logging
from types import ModuleType
from typing import Optional

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

_client: Optional[ModuleType] = None


class SubstrateClientNotInstalled(ImportError):
    """Raised when the ate_env client for Agent Substrate environments cannot be imported."""


def log_info(message):
    logger.info(f"[Substrate] {message}")


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
