# providers/substrate.py

import importlib
import logging
import os
from types import ModuleType
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Agent Substrate release this provider is written against.
SUBSTRATE_RELEASE = "v0.4.0"

# The generated gRPC client is not part of this repository: it is generated from
# pkg/proto/ateapipb/ateapi.proto at SUBSTRATE_RELEASE and installed alongside
# grpcio. By default protoc emits top-level ateapi_pb2 / ateapi_pb2_grpc modules;
# set SUBSTRATE_CLIENT_PACKAGE when an installation places them in a package.
CLIENT_PACKAGE_ENV = "SUBSTRATE_CLIENT_PACKAGE"

_client: Optional[Tuple[ModuleType, ModuleType]] = None


class SubstrateClientNotInstalled(ImportError):
    """Raised when the generated Agent Substrate gRPC client cannot be imported."""


def log_info(message):
    logger.info(f"[Substrate] {message}")


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
