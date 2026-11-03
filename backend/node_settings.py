"""Local RPC secret-file storage. No database changes and no encryption theatre.

Saved settings are a complete snapshot; environment is used only without a file.
Activation is at process startup so all workers use one immutable configuration.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import stat
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from backend.config import Settings

CHAINS = {"mainnet": "main", "testnet": "test", "signet": "signet", "regtest": "regtest"}
INVALID = "Invalid node settings. Check host, port, username, password and network."
STORAGE_ERROR = "Node settings file is unreadable, invalid or insecure. Repair it or save complete settings."


class NodeSettingsError(ValueError):
    pass


class NodeConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    username: str = Field(min_length=1, max_length=256)
    password: SecretStr = Field(min_length=1, max_length=4096)
    network: Literal["mainnet", "testnet", "signet", "regtest"]
    tls: bool = False

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        # Host only: never allow credentials, schemes, paths or query strings.
        host = value[1:-1] if value.startswith("[") and value.endswith("]") else value
        if "%" in host:
            raise ValueError("Scoped IPv6 is not supported")
        try:
            return str(ipaddress.ip_address(host))
        except ValueError:
            if ":" in host or "%" in host:
                raise ValueError("Invalid host") from None
        if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
            raise ValueError("Invalid host")
        labels = host.rstrip(".").split(".")
        if any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
               for label in labels):
            raise ValueError("Invalid host")
        return host.lower()

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        if ":" in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Invalid username")
        return value

    def private_dict(self) -> dict:
        data = self.model_dump()
        data["password"] = self.password.get_secret_value()
        return data

    def public_dict(self) -> dict:
        return self.model_dump(exclude={"password"}) | {"password_configured": True}

    def apply(self, settings: Settings) -> Settings:
        return replace(settings, btc_rpc_host=self.host, btc_rpc_port=self.port,
                       btc_rpc_user=self.username, btc_rpc_password=self.password.get_secret_value(),
                       btc_network=self.network, btc_rpc_tls=self.tls, rpc_configuration_error="")

    @classmethod
    def from_settings(cls, settings: Settings) -> "NodeConfiguration":
        if settings.rpc_configuration_error:
            raise NodeSettingsError(settings.rpc_configuration_error)
        try:
            return cls(host=settings.btc_rpc_host, port=settings.btc_rpc_port,
                       username=settings.btc_rpc_user, password=settings.btc_rpc_password,
                       network={"main": "mainnet", "test": "testnet"}.get(
                           settings.btc_network, settings.btc_network), tls=settings.btc_rpc_tls)
        except ValueError:
            raise NodeSettingsError(INVALID) from None


class NodeSettingsStore:
    def __init__(self, environment: Settings):
        self.environment = environment
        self.path = Path(environment.data_dir) / "node-settings.json"

    def load(self) -> tuple[NodeConfiguration, str]:
        try:
            # Do not follow a secret-file symlink or accept group/world access.
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return NodeConfiguration.from_settings(self.environment), "environment"
        except OSError:
            raise NodeSettingsError(STORAGE_ERROR) from None
        try:
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                        or info.st_uid != os.geteuid()):
                    raise ValueError()
                return NodeConfiguration.model_validate_json(stream.read(16385)), "saved"
        except (OSError, ValueError):
            # An invalid saved file must not silently activate a different node.
            raise NodeSettingsError(STORAGE_ERROR) from None

    def effective_settings(self) -> Settings:
        try:
            node, _ = self.load()
            return node.apply(self.environment)
        except NodeSettingsError as exc:
            return replace(self.environment, rpc_configuration_error=str(exc))

    def view(self, active: Settings) -> dict:
        try:
            node, source = self.load()
            try:
                restart = node != NodeConfiguration.from_settings(active)
            except NodeSettingsError:
                restart = True
            return node.public_dict() | {"source": source, "restart_required": restart,
                                         "configuration_error": None}
        except NodeSettingsError as exc:
            return {"host": "", "port": 8332, "username": "", "network": "mainnet",
                    "tls": False, "password_configured": False, "source": "unconfigured",
                    "restart_required": False, "configuration_error": str(exc)}

    def candidate(self, payload: dict) -> NodeConfiguration:
        # Shared by Test and Save: only missing/null/empty retains the secret.
        # Nonempty strings are opaque (including whitespace and trailing '=').
        # The UI's write-only hint is a placeholder, never a password sentinel.
        data = dict(payload)
        if data.get("password") in (None, ""):
            try:
                current, _ = self.load()
            except NodeSettingsError:
                raise NodeSettingsError("Enter an RPC password.") from None
            # A saved secret must never be forwarded to a new destination by a
            # client that does not know it. Host/user/TLS changes need re-entry.
            try:
                trial = NodeConfiguration.model_validate(
                    data | {"password": current.password.get_secret_value()})
            except ValueError:
                raise NodeSettingsError(INVALID) from None
            if any(getattr(trial, key) != getattr(current, key)
                   for key in ("host", "port", "username", "tls")):
                raise NodeSettingsError("Re-enter the password when changing host, port, username or TLS.")
            return trial
        try:
            return NodeConfiguration.model_validate(data)
        except ValueError:
            raise NodeSettingsError(INVALID) from None

    def save(self, node: NodeConfiguration) -> None:
        temporary = None
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".node-settings-", dir=self.path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(node.private_dict(), stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
            fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            raise NodeSettingsError("Cannot save node settings. Check data-directory ownership and permissions.") from None
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
