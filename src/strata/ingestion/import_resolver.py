"""Resolve a JS/TS import specifier to a file in the repository.

Handles what a real TypeScript monorepo uses: relative paths (with ``.js`` written for a ``.ts``
file, directories with an ``index``), ``tsconfig`` ``paths`` / ``baseUrl`` through ``extends``,
``package.json`` ``imports`` (``#src/*``), and workspace package names (``@scope/pkg``). A
specifier that points outside the repository (an npm package) resolves to ``None`` and stays a
Module placeholder.
"""

from __future__ import annotations

import json
import logging
import os
import posixpath
import re
from pathlib import Path

__all__ = ["ImportResolver", "is_code_file"]

logger = logging.getLogger(__name__)

_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs")
_JS_TO_TS = {".js": (".ts", ".tsx"), ".jsx": (".tsx",), ".mjs": (".mts",), ".cjs": (".cts",)}
_COMMENTS = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/', re.S)
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")
_BUILD_DIRS = ("build", "lib", "dist", "out")
_SKIP_DIRS = {"node_modules", ".git", "build", "dist", "out", ".next", "coverage"}
_MAX_EXTENDS = 4


def is_code_file(path: str) -> bool:
    """Whether ``path`` is a JS/TS source file the resolver can target."""
    return path.endswith(_EXTS)


def _load_json(path: Path) -> dict:
    """Read a JSON file that may hold comments and trailing commas (tsconfig)."""
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return {}
    # keep string literals (they may contain "//"), drop comments
    text = _COMMENTS.sub(lambda m: m.group(0) if m.group(0).startswith('"') else "", text)
    try:
        data = json.loads(_TRAILING_COMMA.sub(r"\1", text))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


class ImportResolver:
    """Maps ``(importing file, specifier)`` to a repo-relative file path, or ``None``.

    Args:
        repo_root: Checkout directory. ``None`` limits resolution to relative specifiers matched
            against ``known_files``.
        known_files: Repo-relative paths already known to exist (the files of this run). Anything
            else is looked up on disk, so an incremental run still resolves to unchanged files.
    """

    def __init__(self, repo_root: Path | None, known_files: set[str] | None = None) -> None:
        """Index workspace packages up front; tsconfig and package.json are read on demand."""
        self._root = repo_root
        self._known = known_files or set()
        self._disk: dict[str, bool] = {}
        self._tsconfig: dict[str, list[tuple[str, list[str]]]] = {}
        self._pkg_imports: dict[str, dict[str, str]] = {}
        self._workspaces: dict[str, str] = self._find_workspaces() if repo_root else {}

    # ── file existence ───────────────────────────────────────────────────────

    def _exists(self, rel: str) -> bool:
        if rel in self._known:
            return True
        if self._root is None:
            return False
        if rel not in self._disk:
            self._disk[rel] = (self._root / rel).is_file()
        return self._disk[rel]

    def _first(self, base: str) -> str | None:
        stem, ext = posixpath.splitext(base)
        tries = [base]
        tries += [stem + e for e in _JS_TO_TS.get(ext, ())]
        tries += [base + e for e in _EXTS]
        tries += [posixpath.join(base, "index" + e) for e in _EXTS]
        # A type-only import (``./types/context``) points at a declaration file.
        tries += [base + ".d.ts", posixpath.join(base, "index.d.ts")]
        return next((t for t in tries if is_code_file(t) and self._exists(t)), None)

    # ── configuration ────────────────────────────────────────────────────────

    def _find_workspaces(self) -> dict[str, str]:
        found: dict[str, str] = {}
        assert self._root is not None  # noqa: S101
        for dirpath, dirnames, filenames in os.walk(self._root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
            if "package.json" not in filenames:
                continue
            rel = os.path.relpath(dirpath, self._root).replace(os.sep, "/")
            name = _load_json(Path(dirpath) / "package.json").get("name")
            if isinstance(name, str) and rel != ".":
                found[name] = rel
        return found

    def _package_dir(self, from_file: str) -> str:
        d = posixpath.dirname(from_file)
        while d and not (self._root and (self._root / d / "package.json").is_file()):
            d = posixpath.dirname(d)
        return d

    def _paths(self, pkg: str) -> list[tuple[str, list[str]]]:
        """``compilerOptions.paths`` as (pattern, repo-relative targets), following ``extends``."""
        if pkg in self._tsconfig:
            return self._tsconfig[pkg]
        out: list[tuple[str, list[str]]] = []
        assert self._root is not None  # noqa: S101
        cfg = self._root / pkg / "tsconfig.json"
        for _ in range(_MAX_EXTENDS):
            data = _load_json(cfg)
            opts = data.get("compilerOptions")
            if isinstance(opts, dict) and isinstance(opts.get("paths"), dict):
                cfg_dir = os.path.relpath(cfg.parent, self._root).replace(os.sep, "/")
                base = str(opts.get("baseUrl", "."))
                for pattern, targets in opts["paths"].items():
                    if isinstance(targets, list):
                        out.append((pattern, [
                            posixpath.normpath(posixpath.join(cfg_dir, base, str(t)))
                            for t in targets
                        ]))  # fmt: skip
            parent = data.get("extends")
            if not isinstance(parent, str) or not parent.startswith("."):
                break
            nxt = (cfg.parent / parent).resolve()
            # "./tsconfig.base" means tsconfig.base.json: the dot is not an extension
            cfg = nxt if nxt.name.endswith(".json") else nxt.with_name(nxt.name + ".json")
        self._tsconfig[pkg] = out
        return out

    def _imports_map(self, pkg: str) -> dict[str, str]:
        if pkg not in self._pkg_imports:
            assert self._root is not None  # noqa: S101
            raw = _load_json(self._root / pkg / "package.json").get("imports")
            self._pkg_imports[pkg] = (
                {k: v for k, v in raw.items() if isinstance(v, str)} if isinstance(raw, dict) else {}
            )
        return self._pkg_imports[pkg]

    # ── resolution ───────────────────────────────────────────────────────────

    def resolve(self, from_file: str, spec: str) -> str | None:
        """The repo-relative file ``spec`` names from ``from_file``, or ``None`` if external."""
        if spec.startswith("."):
            return self._first(posixpath.normpath(posixpath.join(posixpath.dirname(from_file), spec)))
        if self._root is None:
            return None
        pkg = self._package_dir(from_file)
        return (
            self._via_tsconfig(pkg, spec)
            or self._via_package_imports(pkg, spec)
            or self._via_workspace(spec)
        )

    def _via_tsconfig(self, pkg: str, spec: str) -> str | None:
        for pattern, targets in self._paths(pkg):
            star = pattern.endswith("*")
            if (star and spec.startswith(pattern[:-1])) or spec == pattern:
                rest = spec[len(pattern) - 1 :] if star else ""
                for target in targets:
                    hit = self._first(target.replace("*", rest))
                    if hit:
                        return hit
        return None

    def _via_package_imports(self, pkg: str, spec: str) -> str | None:
        for pattern, target in self._imports_map(pkg).items():
            if pattern.endswith("*") and spec.startswith(pattern[:-1]):
                mapped = posixpath.normpath(
                    posixpath.join(pkg, target.replace("*", spec[len(pattern) - 1 :]))
                )
                for build in _BUILD_DIRS:  # ``./build/x`` is compiled output of ``src/x``
                    mapped = mapped.replace(f"/{build}/", "/src/", 1)
                hit = self._first(mapped)
                if hit:
                    return hit
        return None

    def _via_workspace(self, spec: str) -> str | None:
        for name, root in self._workspaces.items():
            if spec == name or spec.startswith(name + "/"):
                sub = spec[len(name) + 1 :]
                for base in ("src", ""):
                    hit = self._first(posixpath.join(root, base, sub or "index"))
                    if hit:
                        return hit
        return None
