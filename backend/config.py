"""Environment configuration; get_settings overlays the local RPC secret file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_DEFAULT_DATA_DIR = str(Path(__file__).resolve().parent.parent / "data")

# Upper bound for MAX_TRACE_DEPTH (L-14). The forward engine walks recursively
# (one nested coroutine frame per hop); under the default recursion limit a
# synthetic linear forward trace raised RecursionError between 960 and 980
# hops instead of ending FORWARD_LIMIT_REACHED. 400 keeps a wide margin for
# the surrounding server / worker call stack.
MAX_TRACE_DEPTH_CAP = 400


@dataclass(frozen=True)
class Settings:
    # Bitcoin Core RPC connection
    btc_rpc_host: str = "127.0.0.1"
    btc_rpc_port: int = 8332
    btc_rpc_user: str = ""
    btc_rpc_password: str = field(default="", repr=False)
    btc_network: str = "mainnet"
    btc_rpc_tls: bool = False
    rpc_configuration_error: str = ""

    # Application
    # Direct start: local only (OF-1). Docker sets APP_HOST=0.0.0.0 inside the
    # container; exposure is decided by the published address in compose.yaml.
    app_host: str = "127.0.0.1"
    app_port: int = 8080
    data_dir: str = _DEFAULT_DATA_DIR
    database_path: str = os.path.join(_DEFAULT_DATA_DIR, "lineage.db")

    # Tracing safety limits
    max_trace_depth: int = 100
    max_auto_trace_hops: int = 100

    # Blockchain Analysis safety limits.
    # The analysis walk is spend-state-driven and has no natural ownership
    # bound, so these caps keep a hostile/wide transaction graph from running
    # away. Reaching a cap is a terminating, partial state (ANALYSIS_LIMIT_REACHED).
    analysis_max_hops: int = 8
    analysis_max_transactions: int = 200
    analysis_max_outpoints: int = 500

    # Blockchain Analysis background execution.
    # Bounded concurrency for the background
    # analysis worker. Default 1: a single whale analysis holds the worker;
    # further analyses queue as jobs instead of spawning unlimited threads.
    analysis_max_concurrent_jobs: int = 1

    # Reverse Provenance safety limits.
    # Ownership-gated reverse
    # traversal still needs hard caps; reaching one is a terminating partial
    # state (REVERSE_LIMIT_REACHED), never misreported as NEEDS_CLASSIFICATION.
    reverse_provenance_max_hops: int = 32
    reverse_provenance_max_transactions: int = 1000
    reverse_provenance_max_outpoints: int = 2500

    # RPC timeouts (seconds)
    rpc_timeout: float = 30.0
    # Long per-request timeout for wallet-discovery scans (scanblocks and the
    # discovery scantxoutset fallback). Bounded chain scans can take minutes,
    # so they are allowed far more time than ordinary RPC calls.
    discovery_rpc_timeout: float = 600.0

    # Derived reports: hard request budgets and small live-RPC worker pools.
    coverage_timeout: float = 30.0
    coverage_rpc_concurrency: int = 8
    coverage_max_outpoints: int = 100_000
    coverage_max_transactions: int = 10_000
    position_timeout: float = 10.0
    position_spend_ttl: float = 5.0

    # Local Electrs fallback for reverse-mode address discovery
    # (ELECTRS_URL, e.g. tcp://electrs_app:50001). Optional: address
    # discovery uses Bitcoin Core scanblocks against the local blockfilter
    # index first and only falls back to Electrs when Core has no index and
    # ELECTRS_URL is set. Empty means Electrs is never contacted.
    electrs_url: str = ""
    electrs_timeout: float = 10.0

    # Caching: how long factor for refreshing confirmations etc.
    cache_enabled: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        def _int(name: str, default: int) -> int:
            raw = os.environ.get(name)
            if raw is None or raw == "":
                return default
            return int(raw)

        def _bool(name: str, default: bool) -> bool:
            raw = os.environ.get(name)
            if raw is None or raw == "":
                return default
            return raw.strip().lower() in ("1", "true", "yes", "on")

        host = os.environ.get("BTC_RPC_HOST") or "127.0.0.1"
        rpc_error = ""
        try:
            port = int(os.environ.get("BTC_RPC_PORT") or "8332")
        except ValueError:
            port = 8332
            rpc_error = "Invalid RPC environment configuration. Open Node Settings."
        use_tls = _bool("BTC_RPC_TLS", False)

        user = os.environ.get("BTC_RPC_USER") or ""
        password = os.environ.get("BTC_RPC_PASSWORD") or ""

        # Allow fully formed RPC URL to be supplied instead.
        # Format: http[s]://user:pass@host:port
        rpc_url = os.environ.get("BTC_RPC_URL")
        if rpc_url:
            from urllib.parse import unquote, urlparse

            try:
                parsed = urlparse(rpc_url)
                if (parsed.scheme not in ("http", "https") or not parsed.hostname
                        or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
                    raise ValueError()
                host = parsed.hostname
                if parsed.port is not None:
                    port = parsed.port
                    rpc_error = ""  # Explicit URL port overrides the environment port.
                if parsed.username is not None:
                    user = unquote(parsed.username)
                if parsed.password is not None:
                    password = unquote(parsed.password)
                use_tls = parsed.scheme == "https"
            except ValueError:
                rpc_error = "Invalid RPC environment configuration. Open Node Settings."

        data_dir = os.environ.get("DATA_DIR") or _DEFAULT_DATA_DIR
        db_path = os.environ.get("DATABASE_PATH", os.path.join(data_dir, "lineage.db"))

        return cls(
            btc_rpc_host=host,
            btc_rpc_port=port,
            btc_rpc_user=user,
            btc_rpc_password=password,
            btc_network=os.environ.get("BTC_NETWORK") or "mainnet",
            btc_rpc_tls=use_tls,
            rpc_configuration_error=rpc_error,
            app_host=os.environ.get("APP_HOST") or "127.0.0.1",
            app_port=_int("APP_PORT", 8080),
            data_dir=data_dir,
            database_path=db_path,
            max_trace_depth=min(_int("MAX_TRACE_DEPTH", 100), MAX_TRACE_DEPTH_CAP),
            max_auto_trace_hops=_int("MAX_AUTO_TRACE_HOPS", 100),
            analysis_max_hops=_int("ANALYSIS_MAX_HOPS", 8),
            analysis_max_transactions=_int("ANALYSIS_MAX_TRANSACTIONS", 200),
            analysis_max_outpoints=_int("ANALYSIS_MAX_OUTPOINTS", 500),
            analysis_max_concurrent_jobs=_int("ANALYSIS_MAX_CONCURRENT_JOBS", 1),
            reverse_provenance_max_hops=_int("REVERSE_PROVENANCE_MAX_HOPS", 32),
            reverse_provenance_max_transactions=_int(
                "REVERSE_PROVENANCE_MAX_TRANSACTIONS", 1000
            ),
            reverse_provenance_max_outpoints=_int(
                "REVERSE_PROVENANCE_MAX_OUTPOINTS", 2500
            ),
            rpc_timeout=float(os.environ.get("BTC_RPC_TIMEOUT", "30") or 30),
            discovery_rpc_timeout=float(
                os.environ.get("BTC_DISCOVERY_RPC_TIMEOUT", "600") or 600
            ),
            coverage_timeout=float(os.environ.get("COVERAGE_TIMEOUT", "30") or 30),
            coverage_rpc_concurrency=max(1, min(32, _int("COVERAGE_RPC_CONCURRENCY", 8))),
            coverage_max_outpoints=_int("COVERAGE_MAX_OUTPOINTS", 100_000),
            coverage_max_transactions=_int("COVERAGE_MAX_TRANSACTIONS", 10_000),
            position_timeout=float(os.environ.get("POSITION_TIMEOUT", "10") or 10),
            position_spend_ttl=float(os.environ.get("POSITION_SPEND_TTL", "5") or 5),
            electrs_url=os.environ.get("ELECTRS_URL", "") or "",
            electrs_timeout=float(os.environ.get("ELECTRS_TIMEOUT", "10") or 10),
            cache_enabled=_bool("CACHE_ENABLED", True),
        )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        from backend.node_settings import NodeSettingsStore
        environment = Settings.from_env()
        _settings = NodeSettingsStore(environment).effective_settings()
    return _settings
