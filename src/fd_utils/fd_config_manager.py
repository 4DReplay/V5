# ─────────────────────────────────────────────────────────────────────────────
# fd_config_manager.py
# fd_utils/fd_config_manager.py
# Final manager: single initialization, flat attribute access by last key,
# property overlay, json5 support (with fallback), path rebasing, and runtime set/persist.
# - Runtime access pattern: from fd_utils.fd_config_manager import conf, mgr
#     conf._output_width, conf._codec_h264, ...
# - Handles: JSON5 with graceful fallback, merge, conflict-free flatten, alias map
# - Supports: runtime set/get, persistence to property, remote payload export/apply
# - Author: Hongsu Jung (spec), implementation by assistant
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import os
import re
import json
import copy
import threading
import builtins as _bi  # ← 내장 set() 충돌 방지용

from datetime import datetime
from typing import Any, Dict, List, Tuple, Optional

__all__ = [
    "FDConfigManager",
    "conf",
    "setup",
    "is_ready",
    "get",
    "cfg_set",
    "reload_config",
    "export_payload",
    "import_payload",
]

# 내장 set 별칭 (모듈의 cfg_set 함수와 이름 충돌 방지)
_BUILTIN_SET = _bi.set

# ------------------------------
# Optional JSON5 support
# ------------------------------
def _load_json5(text: str) -> Dict[str, Any]:
    """
    Try json5; if unavailable, fall back to a permissive comment stripper.
    This fallback removes // and /* */ comments and trailing commas heuristically.
    """
    try:
        import json5  # type: ignore
        return json5.loads(text)
    except Exception:
        # Fallback: strip // and /* */ comments and dangling commas conservatively.
        # 1) Remove // comments
        text_no_slash = re.sub(r"(?m)//.*?$", "", text)
        # 2) Remove /* */ comments
        text_no_block = re.sub(r"/\*.*?\*/", "", text_no_slash, flags=re.S)
        # 3) Remove trailing commas before } or ]
        text_no_trailing = re.sub(r",\s*([}\]])", r"\1", text_no_block)
        return json.loads(text_no_trailing)

# ------------------------------
# Helpers
# ------------------------------
def _is_probably_path(key: str, val: Any) -> bool:
    """Heuristic to detect path-like strings for rebasing."""
    if not isinstance(val, str):
        return False
    if val.strip() == "":
        return False
    key_l = key.lower()
    pathish_key = any(
        k in key_l
        for k in [
            "path",
            "file",
            "dir",
            "folder",
            "image",
            "logo",
            "png",
            "jpg",
            "jpeg",
            "ttf",
            "otf",
            "ffmpeg",
            "model",
            "weight",
            "names",
            "cfg",
        ]
    )
    has_sep = ("/" in val) or ("\\" in val)
    looks_rel = val.startswith("./") or val.startswith(".\\") or (has_sep and not os.path.isabs(val))
    return pathish_key and (has_sep or looks_rel)

def _join_if_relative(base_dir: str, p: str) -> str:
    """Join base to relative path; leave absolute unchanged."""
    if not p:
        return p
    if os.path.isabs(p):
        return p
    return os.path.normpath(os.path.join(base_dir, p))

def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deep merge override into base (without mutating inputs).
    - Dicts are merged recursively
    - Other types fully replaced by override
    """
    out = copy.deepcopy(base)
    stack: List[Tuple[Dict[str, Any], Dict[str, Any]]] = [(out, override)]
    while stack:
        dst, src = stack.pop()
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                stack.append((dst[k], v))
            else:
                dst[k] = copy.deepcopy(v)
    return out

def _walk_tree(tree: Any, path: Tuple[str, ...] = ()):
    results = []
    if isinstance(tree, dict):
        if not tree:  # 빈 dict → 리프로 취급
            results.append((path, {}))
        else:
            for k, v in tree.items():
                results.extend(_walk_tree(v, path + (k,)))
    elif isinstance(tree, list):
        if not tree:  # (선택) 빈 리스트도 리프로 취급하려면 추가
            results.append((path, []))
        else:
            for i, v in enumerate(tree):
                results.extend(_walk_tree(v, path + (f"[{i}]",)))
    else:
        results.append((path, tree))
    return results

def _last_key_of(path: Tuple[str, ...]) -> str:
    """Return the last path segment ignoring list indices like [0]."""
    for seg in reversed(path):
        if not seg.startswith("["):
            return seg
    return "".join(path)

# ------------------------------
# Public namespace bag
# ------------------------------
class _ConfNamespace:
    def __init__(self):
        self._values = {}
        self._runtime = {}  # runtime-only attributes

    def __getattr__(self, key):
        # runtime 우선
        if key in self._runtime:
            return self._runtime[key]
        if key in self._values:
            return self._values[key]
        raise AttributeError(f"{key} not found in config or runtime")

    def __setattr__(self, key, value):
        if key.startswith("_"):
            super().__setattr__(key, value)
        else:
            # 동적 추가 값은 runtime 공간에 저장
            self._runtime[key] = value

    def _set_config_dict(self, d: dict):
        """Called by FDConfigManager.init()"""
        self._values = d

# ------------------------------
# Core Manager
# ------------------------------
class FDConfigManager:
    """
    Manager that:
      - Loads 'aid_config.json5' and optional 'aid_property.json'
      - Merges to a single runtime tree
      - Exposes flat attributes by *last leaf name* onto `conf`
      - Keeps full tree at `conf._tree`
      - Supports path rebasing
      - Supports runtime set() and persist to property file
    """

    def __init__(self, conf_module: _ConfNamespace):
        self._conf = conf_module
        self._lock = threading.RLock()
        self._ready = False

        # Files
        self._config_path: Optional[str] = None
        self._property_path: Optional[str] = None
        self._config_base_dir: Optional[str] = None

        # Data
        self._tree: Dict[str, Any] = {}
        self._base_tree: Dict[str, Any] = {}
        self._prop_tree: Dict[str, Any] = {}

        # Indexes
        self._lastkey_index: Dict[str, List[Tuple[str, ...]]] = {}
        self._attr_name_map: Dict[str, str] = {}
        self._collisions: Dict[str, List[str]] = {}

        self._persist_cache: Dict[str, Any] = {}

        self._exposed_attr_names: set[str] = _BUILTIN_SET()

    # --------------- Initialization ---------------
    def init(
        self,
        config_private_path: str,
        *,
        config_public_path: Optional[str] = None,
        rebase_paths: bool = True,
        force: bool = False,
    ) -> None:
        """
        Load and index configuration.
        - config_private_path: main json5 (authoritative base + runtime sections)
        - config_public_path: optional overrides (end-user)
        - rebase_paths: convert relative paths to absolute based on config file dir
        - force: reload even if already ready
        """
        with self._lock:
            if self._ready and not force:
                return

            self._config_path = os.path.abspath(config_private_path)
            self._config_base_dir = os.path.dirname(self._config_path)

            if config_public_path is None:
                maybe_prop = os.path.join(self._config_base_dir, "aid_property.json")
                self._property_path = maybe_prop if os.path.exists(maybe_prop) else None
            else:
                self._property_path = os.path.abspath(config_public_path)

            # 1) Load config json5
            with open(self._config_path, "r", encoding="utf-8") as f:
                config_text = f.read()
            self._base_tree = _load_json5(config_text)

            # 2) Load property (json or json5)
            if self._property_path and os.path.exists(self._property_path):
                with open(self._property_path, "r", encoding="utf-8") as f:
                    prop_text = f.read()
                try:
                    self._prop_tree = _load_json5(prop_text)
                except Exception:
                    self._prop_tree = json.loads(prop_text)
            else:
                self._prop_tree = {}

            # 3) Merge
            merged = _deep_merge(self._base_tree, self._prop_tree)

            # 4) Optional path rebasing (in-place walk)
            if rebase_paths and self._config_base_dir:
                def rebase(node: Any, path: Tuple[str, ...] = ()) -> Any:
                    if isinstance(node, dict):
                        out = {}
                        for k, v in node.items():
                            if isinstance(v, (dict, list)):
                                out[k] = rebase(v, path + (k,))
                            else:
                                if _is_probably_path(k, v):
                                    out[k] = _join_if_relative(self._config_base_dir, v)
                                else:
                                    out[k] = v
                        return out
                    elif isinstance(node, list):
                        return [rebase(v, path + (f"[{i}]",)) for i, v in enumerate(node)]
                    else:
                        return node

                merged = rebase(merged)

            self._tree = merged

            # 5) Build flat attribute exposure by last-key
            self._index_and_expose()

            self._ready = True

    def _index_and_expose(self) -> None:
        # 0) 이전에 노출한 속성 제거
        for name in getattr(self._conf, "_exposed_attr_names", []):
            if hasattr(self._conf, name):
                try:
                    delattr(self._conf, name)
                except Exception:
                    pass
        self._exposed_attr_names.clear()

        # 디버그 정보
        self._conf._tree = self._tree
        self._conf._source_info = {
            "config_private_path": self._config_path,
            "config_public_path": self._property_path,
            "base_dir": self._config_base_dir,
        }

        # 1) 인덱싱 초기화
        self._lastkey_index.clear()
        self._attr_name_map.clear()
        self._collisions.clear()

        # 2) 리프 수집
        leaves = _walk_tree(self._tree)
        for full_path, value in leaves:
            last = _last_key_of(full_path)
            self._lastkey_index.setdefault(last, []).append(full_path)

        # 3) 충돌 해결 + 속성 부착
        for last, paths in self._lastkey_index.items():
            if len(paths) == 1:
                p = paths[0]
                val = self._read_by_path(p)
                setattr(self._conf, last, val)
                self._exposed_attr_names.add(last)
                self._attr_name_map[last] = last
            else:
                self._collisions[last] = [".".join(p) for p in paths]
                for idx, p in enumerate(paths):
                    val = self._read_by_path(p)
                    segments = [seg for seg in p if not seg.startswith("[")]
                    prefix = "_".join(segments[-3:-1]) if len(segments) >= 3 else (segments[0] if segments else "root")
                    attr_name = f"{prefix}_{last}"
                    # 중복 방지
                    counter, base = 2, attr_name
                    while hasattr(self._conf, attr_name):
                        attr_name = f"{base}_{counter}"
                        counter += 1
                    setattr(self._conf, attr_name, val)
                    self._exposed_attr_names.add(attr_name)
                    # 첫 번째 경로에는 last 이름도 매핑(하위호환)
                    if idx == 0 and not hasattr(self._conf, last):
                        setattr(self._conf, last, val)
                        self._exposed_attr_names.add(last)
                        self._attr_name_map[last] = last
                if last not in self._attr_name_map:
                    self._attr_name_map[last] = attr_name  # 마지막 attr_name

        self._conf._collisions = copy.deepcopy(self._collisions)
        self._conf._exposed_attr_names = tuple(self._exposed_attr_names)

    def _read_by_path(self, path: Tuple[str, ...]) -> Any:
        node = self._tree
        for seg in path:
            if seg.startswith("["):
                idx = int(seg.strip("[]"))
                node = node[idx]
            else:
                node = node[seg]
        return node

    def _write_by_path(self, path: Tuple[str, ...], value: Any) -> None:
        node = self._tree
        for seg in path[:-1]:
            if seg.startswith("["):  # ← 오타 수정: "[]"
                idx = int(seg.strip("[]"))
                node = node[idx]
            else:
                node = node[seg]
        last = path[-1]
        if last.startswith("["):
            idx = int(last.strip("[]"))
            node[idx] = value
        else:
            node[last] = value

    # --------------- Public API ---------------
    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            if hasattr(self._conf, key):
                return getattr(self._conf, key)
            return default

    def set(self, key: str, value: Any, *, persist: bool = False) -> Any:
        with self._lock:
            paths: List[Tuple[str, ...]] = []

            if key in self._lastkey_index:
                paths = self._lastkey_index[key]
            else:
                guessed_last = None
                for last, attr_name in self._attr_name_map.items():
                    if attr_name == key or last == key:
                        guessed_last = last
                        break
                if guessed_last and guessed_last in self._lastkey_index:
                    paths = self._lastkey_index[guessed_last]

            if not paths:
                setattr(self._conf, key, value)  # runtime-only
                return value

            for p in paths:
                self._write_by_path(p, value)

            self._index_and_expose()

            if persist:
                self._persist_lastkey(paths[0], value)

            return getattr(self._conf, key, value)

    def _persist_lastkey(self, path: Tuple[str, ...], value: Any) -> None:
        if not self._property_path:
            base_dir = self._config_base_dir or os.getcwd()
            self._property_path = os.path.join(base_dir, "aid_property.json")

        if os.path.exists(self._property_path):
            with open(self._property_path, "r", encoding="utf-8") as f:
                text = f.read()
            try:
                overlay = _load_json5(text)
            except Exception:
                overlay = json.loads(text)
        else:
            overlay = {}

        cur = overlay
        for seg in path[:-1]:
            if seg.startswith("["):
                continue
            if seg not in cur or not isinstance(cur.get(seg), dict):
                cur[seg] = {}
            cur = cur[seg]

        last = path[-1]
        if last.startswith("["):
            cur["__array_value__"] = value
        else:
            cur[last] = value

        with open(self._property_path, "w", encoding="utf-8") as f:
            json.dump(overlay, f, ensure_ascii=False, indent=2)

    def reload(self) -> None:
        if not self._config_path:
            raise RuntimeError("Config path is not set; call init/setup first.")
        self.init(self._config_path, config_public_path=self._property_path, rebase_paths=True, force=True)

    def export_payload(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._tree)

    def import_payload(self, payload: Dict[str, Any]) -> None:
        with self._lock:
            self._tree = copy.deepcopy(payload)
            self._index_and_expose()
            self._ready = True

    def ready(self) -> bool:
        return self._ready

    # --------------- HTTP Editor ---------------
    def get_public_overlay(self) -> Dict[str, Any]:
        """aid_config_public.json 현재 오버레이(메모리 상)를 딕셔너리로 반환"""
        with self._lock:
            return copy.deepcopy(self._prop_tree)

    def replace_public_overlay(self, new_overlay: Dict[str, Any]) -> None:
        """
        오버레이 전체를 통째로 교체 후 디스크에 저장하고 런타임 재적용.
        외부 에디터가 통째로 보내는 경우에 사용.
        """
        with self._lock:
            self._prop_tree = copy.deepcopy(new_overlay)
            self._save_public_overlay_unlocked()
            # 베이스와 머지 → 런타임 반영
            self._tree = _deep_merge(self._base_tree, self._prop_tree)
            self._index_and_expose()

    def patch_public_overlay(self, patch: Dict[str, Any]) -> None:
        """
        오버레이에 부분 병합(깊은 머지) → 저장 → 런타임 재적용.
        """
        with self._lock:
            self._prop_tree = _deep_merge(self._prop_tree, patch)
            self._save_public_overlay_unlocked()
            self._tree = _deep_merge(self._base_tree, self._prop_tree)
            self._index_and_expose()

    def set_public_by_path(self, dotted_path: str, value: Any) -> None:
        """
        'Runtime.OutputVideo._output_width' 같은 dotted path로 오버레이 값을 설정.
        없으면 생성. 저장 + 런타임 재적용까지 수행.
        """
        with self._lock:
            _ensure_path_set(self._prop_tree, dotted_path, value)
            self._save_public_overlay_unlocked()
            self._tree = _deep_merge(self._base_tree, self._prop_tree)
            self._index_and_expose()

    def get_public_by_path(self, dotted_path: str, default: Any=None) -> Any:
        """오버레이에서 dotted path로 값 읽기(없으면 default)."""
        with self._lock:
            return _read_by_dotted(self._prop_tree, dotted_path, default)

    def _save_public_overlay_unlocked(self) -> None:
        """(락 보유 가정) 오버레이를 디스크에 원자적으로 저장."""
        if not self._property_path:
            base_dir = self._config_base_dir or os.getcwd()
            self._property_path = os.path.join(base_dir, "aid_config_public.json5")

        os.makedirs(os.path.dirname(self._property_path), exist_ok=True)
        tmp = self._property_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._prop_tree, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._property_path)

# ------------------------------
# Module-level singleton + facade
# ------------------------------
_conf_singleton = _ConfNamespace()
_manager = FDConfigManager(conf_module=_conf_singleton)
_conf_lock = threading.Lock()
_conf_ready = False

# Public handle that other modules import
conf = _conf_singleton
mgr = _manager  # 필요 시 외부에서 매니저 직접 접근

def setup(
    config_private_path: Optional[str] = None,
    config_public_path: Optional[str] = None,
    *,
    rebase_paths: bool = True,
    runtime_factories: Optional[dict] = None,      # 추가: conf를 받아 동적 생성하는 팩토리    
) -> _ConfNamespace:
    """
    Initialize once at app start; afterwards, other files can simply:
        from fd_utils.fd_config_manager import conf
    """
    global _conf_ready
    if _conf_ready:
        return conf
    with _conf_lock:
        if _conf_ready:
            return conf

        cfg_private = config_private_path
        cfg_public  = config_public_path
        _manager.init(cfg_private, config_public_path=cfg_public, rebase_paths=rebase_paths, force=True)
        
        import types
        if not hasattr(conf, "_runtime_namespace"):
            conf._runtime_namespace = types.SimpleNamespace()

        if runtime_factories:
            for k, factory in runtime_factories.items():
                val = factory(conf)
                setattr(conf, k, val)
                setattr(conf._runtime_namespace, k, val)

        _conf_ready = True
        return conf

def read_latest_release_from_md(md_path: str):
    """
    Reads the most recent release version and date from a markdown file.
    Expected format in the markdown (example):
        ## [5.0.0] - 2025-10-20
    Returns:
        (version: str, date: str)
    """
    try:
        with open(md_path, "r", encoding="utf-8") as f:
            text = f.read()
        match = re.search(r"\[\s*([\d.]+)\s*\]\s*-\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", text)
        if match:
            ver, date = match.groups()
            return ver, date
        match = re.search(r"Version[:\s]*([\d.]+).*?Date[:\s]*([0-9]{4}-[0-9]{2}-[0-9]{2})", text, re.DOTALL)
        if match:
            return match.groups()
    except Exception as e:
        print(f"[WARN] read_latest_release_from_md failed: {e}")
    return "0.0.0", datetime.now().strftime("%Y-%m-%d")

def is_ready() -> bool:
    return _manager.ready()

def get(key: str, default: Any = None) -> Any:
    return _manager.get(key, default)

# 모듈 함수 이름을 cfg_set로 두고, 'set' 이름은 하위호환 별칭으로 제공
def cfg_set(key: str, value: Any, *, persist: bool = False) -> Any:
    return _manager.set(key, value, persist=persist)

# Module helper
def public_get() -> Dict[str, Any]:
    return mgr.get_public_overlay()

def public_replace(new_overlay: Dict[str, Any]) -> None:
    mgr.replace_public_overlay(new_overlay)

def public_patch(patch: Dict[str, Any]) -> None:
    mgr.patch_public_overlay(patch)

def public_get_by_path(path: str, default: Any=None) -> Any:
    return mgr.get_public_by_path(path, default)

def public_set_by_path(path: str, value: Any) -> None:
    mgr.set_public_by_path(path, value)

def reload_config() -> None:
    _manager.reload()

def export_payload() -> Dict[str, Any]:
    return _manager.export_payload()

def import_payload(payload: Dict[str, Any]) -> None:
    _manager.import_payload(payload)

def _ensure_path_set(root: Dict[str, Any], dotted_path: str, value: Any) -> None:
    """
    딕셔너리 root에 dotted_path ('A.B.C')를 따라가며 dict를 생성하고 값을 설정.
    리스트 인덱스는 지원하지 않고, 퍼블릭 오버레이는 dict만 생성하도록 단순화.
    """
    cur = root
    parts = [p for p in dotted_path.split(".") if p]
    for key in parts[:-1]:
        if key not in cur or not isinstance(cur[key], dict):
            cur[key] = {}
        cur = cur[key]
    cur[parts[-1]] = value

def _read_by_dotted(root: Dict[str, Any], dotted_path: str, default: Any=None) -> Any:
    cur = root
    for key in [p for p in dotted_path.split(".") if p]:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur

# 유틸 함수 노출
setattr(conf, "read_latest_release_from_md", read_latest_release_from_md)
# 하위호환: 외부 코드에서 import set 를 사용하고 있으면 계속 동작
set = cfg_set