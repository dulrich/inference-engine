"""Model registry: alias -> exported ONNX directory, loaded from `models.yaml`.

The registry is the sidecar's whole extensibility story. Adding a GLiNER2-family variant is a
config append plus a weights fetch — no code — because every member of the family shares one
architecture (DebertaV2 encoder + span head + count head) and therefore one runtime. The alias
is the API surface, so the same append-only law that governs `llama/config.yaml` governs this
file: never rename, never repurpose, never delete an alias. A rename does not error anywhere;
it silently forks whatever the consumer stored.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .runtime import Gliner2OnnxRuntime


class RegistryError(RuntimeError):
    """models.yaml is malformed or points somewhere that does not exist."""


class UnknownModelError(KeyError):
    """A request named an alias the registry does not define."""


def _typename(value: object) -> str:
    return "null" if value is None else type(value).__name__


def _require_str(
    config_path: Path,
    alias: str,
    cfg: dict[str, Any],
    key: str,
    *,
    default: str = "",
    required: bool = False,
) -> str:
    """A string config value, or `default` when the key is absent. Never a coercion."""
    if key not in cfg:
        if required:
            raise RegistryError(f"{config_path}: model '{alias}' has no '{key}'")
        return default
    value = cfg[key]
    if not isinstance(value, str) or (required and not value):
        raise RegistryError(
            f"{config_path}: model '{alias}': '{key}' must be a "
            f"{'non-empty ' if required else ''}string, got {_typename(value)}"
        )
    return value


def _require_bool(
    config_path: Path, alias: str, cfg: dict[str, Any], key: str, *, default: bool
) -> bool:
    """A real YAML boolean. A quoted `"false"` is a config error, not `True`."""
    if key not in cfg:
        return default
    value = cfg[key]
    if not isinstance(value, bool):
        raise RegistryError(
            f"{config_path}: model '{alias}': '{key}' must be true or false, "
            f"got {_typename(value)}"
        )
    return value


@dataclass
class ModelEntry:
    alias: str
    name: str
    source_model: str
    path: Path
    preload: bool = True
    notes: str = ""

    # Runtime state
    runtime: Gliner2OnnxRuntime | None = None
    load_seconds: float | None = None
    loaded_at: float | None = None
    load_error: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def loaded(self) -> bool:
        return self.runtime is not None

    def ensure_loaded(self, *, intra_op_threads: int | None = None) -> Gliner2OnnxRuntime:
        """Load on first use if it was not preloaded. Resident forever after.

        Nothing here is ever unloaded. The GPU tier next door unloads on a ttl because VRAM is
        contended; on this tier the scarce resource is cold-load latency, and a loaded model
        costs only RAM that nothing else is competing for. See `AGENTS.md` (residency policy).
        """
        with self._lock:
            if self.runtime is not None:
                return self.runtime
            started = time.time()
            try:
                runtime = Gliner2OnnxRuntime(self.path, intra_op_threads=intra_op_threads)
            except Exception as exc:  # surfaced through /health, not swallowed
                self.load_error = f"{type(exc).__name__}: {exc}"
                raise
            self.runtime = runtime
            self.load_seconds = round(time.time() - started, 2)
            self.loaded_at = time.time()
            self.load_error = None
            return runtime

    def status(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "loaded": self.loaded,
            "name": self.name,
            "source_model": self.source_model,
            "path": str(self.path),
            "preload": self.preload,
        }
        if self.load_seconds is not None:
            info["load_seconds"] = self.load_seconds
        if self.load_error:
            info["error"] = self.load_error
        if self.runtime is not None:
            manifest = self.runtime.manifest
            info["max_width"] = manifest.get("max_width")
            info["hidden_size"] = manifest.get("hidden_size")
            info["counting_layer"] = manifest.get("counting_layer")
            info["encoder_model"] = manifest.get("encoder_model")
            info["exported_at"] = manifest.get("exported_at")
        if self.notes:
            info["notes"] = self.notes
        return info


class ModelRegistry:
    def __init__(self, entries: dict[str, ModelEntry], *, intra_op_threads: int | None = None):
        self._entries = entries
        self._intra_op_threads = intra_op_threads

    @classmethod
    def from_yaml(cls, path: str | Path, *, intra_op_threads: int | None = None) -> "ModelRegistry":
        config_path = Path(path)
        if not config_path.exists():
            raise RegistryError(f"model registry not found: {config_path}")
        data = yaml.safe_load(config_path.read_text()) or {}
        raw_models = data.get("models")
        if not isinstance(raw_models, dict) or not raw_models:
            raise RegistryError(f"{config_path}: no 'models' mapping")

        entries: dict[str, ModelEntry] = {}
        for alias, cfg in raw_models.items():
            # Validate, never coerce. `str()`/`bool()` around config values is how `path: true`
            # becomes `Path("True")` and surfaces much later as a missing directory, and how a
            # quoted `preload: "false"` silently preloads (`bool("false")` is True). A config
            # mistake must read as a config error, here, at load.
            if not isinstance(alias, str) or not alias:
                raise RegistryError(
                    f"{config_path}: model alias {alias!r} must be a non-empty string"
                )
            if not isinstance(cfg, dict):
                raise RegistryError(f"{config_path}: model '{alias}' must be a mapping")
            model_path = _require_str(config_path, alias, cfg, "path", required=True)
            entries[alias] = ModelEntry(
                alias=alias,
                name=_require_str(config_path, alias, cfg, "name", default=alias),
                source_model=_require_str(config_path, alias, cfg, "source_model", default=""),
                path=Path(model_path),
                preload=_require_bool(config_path, alias, cfg, "preload", default=True),
                notes=_require_str(config_path, alias, cfg, "notes", default=""),
            )
        return cls(entries, intra_op_threads=intra_op_threads)

    def __contains__(self, alias: object) -> bool:
        return alias in self._entries

    @property
    def aliases(self) -> list[str]:
        return list(self._entries)

    def get(self, alias: str) -> ModelEntry:
        try:
            return self._entries[alias]
        except KeyError as exc:
            raise UnknownModelError(alias) from exc

    def preload_all(self, log=print) -> None:
        """Load every `preload: true` entry before the listener opens.

        This is the residency decision made concrete: a request must never be the thing that
        discovers a model is cold. The compose healthcheck's `start_period` has to cover the
        sum of these loads — see the `inference-engine-onnx` service block in
        `fleet-tools/compose.fleet.yml`.
        """
        for entry in self._entries.values():
            if not entry.preload:
                log(f"[registry] {entry.alias}: preload disabled — will load on first request")
                continue
            log(f"[registry] {entry.alias}: loading {entry.path} ...")
            try:
                entry.ensure_loaded(intra_op_threads=self._intra_op_threads)
                log(f"[registry] {entry.alias}: ready in {entry.load_seconds}s")
            except Exception as exc:
                # A missing/broken model must not take the whole sidecar down: the other
                # aliases are still serveable, and /health reports this one as not loaded.
                log(f"[registry] {entry.alias}: FAILED to load: {exc}")

    def runtime_for(self, alias: str) -> Gliner2OnnxRuntime:
        return self.get(alias).ensure_loaded(intra_op_threads=self._intra_op_threads)

    def status(self) -> dict[str, Any]:
        return {alias: entry.status() for alias, entry in self._entries.items()}

    def all_ready(self) -> bool:
        """True when every alias that asked to be preloaded actually is."""
        return all(entry.loaded for entry in self._entries.values() if entry.preload)
