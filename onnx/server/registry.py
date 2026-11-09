"""Model registry: alias -> exported ONNX directory, loaded from `models.yaml`.

The registry is the sidecar's whole extensibility story. Adding a GLiNER2-family variant is a
config append plus a weights fetch — no code — because every member of the family shares one
architecture (DebertaV2 encoder + span head + count head) and therefore one runtime. The alias
is the API surface, so the same append-only law that governs `llama/config.yaml` governs this
file: never rename, never repurpose, never delete an alias. A rename does not error anywhere;
it silently forks whatever the consumer stored.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import gc
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .runtime import Gliner2OnnxRuntime
from .runtime_seqcls import SeqClsOnnxRuntime

# Runtime families. `gliner2` is the default so pre-existing entries need no `type:` key.
RUNTIME_TYPES: dict[str, type] = {"gliner2": Gliner2OnnxRuntime, "seqcls": SeqClsOnnxRuntime}
DEFAULT_TYPE = "gliner2"


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


def _require_idle_seconds(config_path: Path, alias: str, cfg: dict[str, Any]) -> int | None:
    """`idle_unload_seconds`: a positive integer, or absent (never unload). Never a coercion."""
    key = "idle_unload_seconds"
    if key not in cfg:
        return None
    value = cfg[key]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RegistryError(
            f"{config_path}: model '{alias}': '{key}' must be a positive integer, "
            f"got {_typename(value)} {value!r}"
        )
    return value


def _malloc_trim() -> None:
    """Return freed heap to the OS. Load-bearing: dropping a gliner2-pii session alone freed
    1,628 -> 682 MiB RSS; with `malloc_trim(0)` it fell to 29 MiB. Non-glibc: skip quietly."""
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
        libc.malloc_trim(0)
    except (OSError, AttributeError):
        pass


@dataclass
class ModelEntry:
    alias: str
    name: str
    source_model: str
    path: Path
    preload: bool = True
    notes: str = ""
    type: str = DEFAULT_TYPE
    idle_unload_seconds: int | None = None
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)

    # Runtime state
    runtime: Gliner2OnnxRuntime | SeqClsOnnxRuntime | None = None
    load_seconds: float | None = None
    loaded_at: float | None = None
    load_error: str | None = None
    in_use: int = 0
    last_used: float | None = None
    unloads: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def loaded(self) -> bool:
        return self.runtime is not None

    def _load_locked(self, intra_op_threads: int | None):
        if self.runtime is not None:
            return self.runtime
        started = time.time()
        try:
            runtime = RUNTIME_TYPES[self.type](self.path, intra_op_threads=intra_op_threads)
        except Exception as exc:  # surfaced through /health, not swallowed
            self.load_error = f"{type(exc).__name__}: {exc}"
            raise
        self.runtime = runtime
        self.load_seconds = round(time.time() - started, 2)
        self.loaded_at = time.time()
        self.load_error = None
        self.last_used = self.clock()
        return runtime

    def ensure_loaded(
        self, *, intra_op_threads: int | None = None
    ) -> Gliner2OnnxRuntime | SeqClsOnnxRuntime:
        """Load if cold (preload path). Request paths use `lease()` so an idle unload can never
        drop a runtime that a request is still using."""
        with self._lock:
            return self._load_locked(intra_op_threads)

    @contextlib.contextmanager
    def lease(
        self, *, intra_op_threads: int | None = None
    ) -> Iterator[Gliner2OnnxRuntime | SeqClsOnnxRuntime]:
        """Load on first use and pin the runtime for the duration of one request.

        `in_use` is counted under the same lock `unload_if_idle` takes, so an entry with an
        in-flight request is never unloaded; the idle clock restarts when the request ends.
        """
        with self._lock:
            runtime = self._load_locked(intra_op_threads)
            self.in_use += 1
        try:
            yield runtime
        finally:
            with self._lock:
                self.in_use -= 1
                self.last_used = self.clock()

    def unload_if_idle(self, now: float | None = None) -> bool:
        """Drop the runtime if this entry opted into idle unload and has been unused for its
        window. Preload entries are resident by contract and never unloaded here."""
        if self.preload or self.idle_unload_seconds is None:
            return False
        with self._lock:
            if self.runtime is None or self.in_use > 0 or self.last_used is None:
                return False
            if (self.clock() if now is None else now) - self.last_used < self.idle_unload_seconds:
                return False
            self.runtime = None
            self.unloads += 1
        gc.collect()
        _malloc_trim()
        return True

    def status(self) -> dict[str, Any]:
        with self._lock:  # one consistent snapshot; never re-read mutable state below
            runtime = self.runtime
            load_seconds, load_error = self.load_seconds, self.load_error
            in_use, unloads = self.in_use, self.unloads
        info: dict[str, Any] = {
            "loaded": runtime is not None,
            "type": self.type,
            "name": self.name,
            "source_model": self.source_model,
            "path": str(self.path),
            "preload": self.preload,
        }
        if self.idle_unload_seconds is not None:
            info["idle_unload_seconds"] = self.idle_unload_seconds
            info["in_use"] = in_use
            info["unloads"] = unloads
        if load_seconds is not None:
            info["load_seconds"] = load_seconds
        if load_error:
            info["error"] = load_error
        if isinstance(runtime, SeqClsOnnxRuntime):
            info["format"] = runtime.format
            info["labels"] = list(runtime.labels)
            info["max_len"] = runtime.max_len
        elif runtime is not None:
            manifest = runtime.manifest
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
            model_type = _require_str(config_path, alias, cfg, "type", default=DEFAULT_TYPE)
            if model_type not in RUNTIME_TYPES:
                raise RegistryError(
                    f"{config_path}: model '{alias}': 'type' must be one of "
                    f"{', '.join(RUNTIME_TYPES)}, got {model_type!r}"
                )
            preload = _require_bool(config_path, alias, cfg, "preload", default=True)
            idle = _require_idle_seconds(config_path, alias, cfg)
            if preload and idle is not None:
                # Preloaded means resident by contract; a window there would be silently dead.
                raise RegistryError(
                    f"{config_path}: model '{alias}': 'idle_unload_seconds' requires "
                    f"'preload: false' (preloaded models are never unloaded)"
                )
            entries[alias] = ModelEntry(
                alias=alias,
                name=_require_str(config_path, alias, cfg, "name", default=alias),
                source_model=_require_str(config_path, alias, cfg, "source_model", default=""),
                path=Path(model_path),
                preload=preload,
                notes=_require_str(config_path, alias, cfg, "notes", default=""),
                type=model_type,
                idle_unload_seconds=idle,
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
        the fleet compose.
        """
        for entry in self._entries.values():
            if not entry.preload:
                idle = entry.idle_unload_seconds
                log(f"[registry] {entry.alias}: lazy — loads on first request"
                    + (f", unloads after {idle}s idle" if idle else ""))
                continue
            log(f"[registry] {entry.alias}: loading {entry.path} ...")
            try:
                entry.ensure_loaded(intra_op_threads=self._intra_op_threads)
                log(f"[registry] {entry.alias}: ready in {entry.load_seconds}s")
            except Exception as exc:
                # A missing/broken model must not take the whole sidecar down: the other
                # aliases are still serveable, and /health reports this one as not loaded.
                log(f"[registry] {entry.alias}: FAILED to load: {exc}")

    def lease(self, alias: str):
        """Context manager: the alias's runtime, loaded if cold and pinned against idle unload
        until the `with` block exits. Unknown alias raises `UnknownModelError` (a 404)."""
        return self.get(alias).lease(intra_op_threads=self._intra_op_threads)

    def unload_idle(self, now: float | None = None, log=print) -> list[str]:
        """One reaper sweep: unload every lazy entry past its idle window."""
        dropped = [a for a, e in self._entries.items() if e.unload_if_idle(now)]
        for alias in dropped:
            log(f"[registry] {alias}: idle — unloaded (gc + malloc_trim)")
        return dropped

    def start_reaper(self, interval: float = 30.0, log=print) -> threading.Thread | None:
        """Daemon thread sweeping `unload_idle` every `interval` s; None if nothing opts in."""
        if not any(e.idle_unload_seconds and not e.preload for e in self._entries.values()):
            return None

        def loop() -> None:
            while True:
                time.sleep(interval)
                try:
                    self.unload_idle(log=log)
                except Exception as exc:  # the reaper must never die silently mid-run
                    log(f"[registry] reaper sweep failed: {type(exc).__name__}: {exc}")

        thread = threading.Thread(target=loop, name="onnx-idle-reaper", daemon=True)
        thread.start()
        return thread

    def status(self) -> dict[str, Any]:
        return {alias: entry.status() for alias, entry in self._entries.items()}

    def all_ready(self) -> bool:
        """True when every alias that asked to be preloaded actually is."""
        return all(entry.loaded for entry in self._entries.values() if entry.preload)
