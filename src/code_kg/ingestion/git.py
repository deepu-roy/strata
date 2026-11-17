"""Git helpers — clone, fetch, diff, and repo metadata.

Uses subprocess git commands; no extra Python dependency required.
"""

import base64
import hashlib
import logging
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ── exceptions ────────────────────────────────────────────────────────────────


class GitError(RuntimeError):
    """Raised when a git operation fails."""


# ── credential redaction ──────────────────────────────────────────────────────

_URL_CREDS_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/@\s]+@")
_AUTH_HEADER_RE = re.compile(r"(?i)(authorization:\s*(?:basic|bearer)\s+)\S+")


def redact(text: str, secrets: Optional[list[str]] = None) -> str:
    """Strip credentials from *text* so it is safe to log or return.

    Handles ``scheme://user:token@host`` URLs, ``Authorization:`` header
    values and any literal strings in *secrets*.
    """
    if not text:
        return text
    out = _URL_CREDS_RE.sub(r"\1***@", text)
    out = _AUTH_HEADER_RE.sub(r"\1***", out)
    for secret in secrets or []:
        if secret:
            out = out.replace(secret, "***")
    return out


def _auth_env(repo_url: str, token: Optional[str]) -> tuple[dict[str, str], list[str]]:
    """Build the extra environment that authenticates git over HTTP(S).

    The credential travels only in the process environment (never argv/URLs).
    Returns ``(env_additions, secrets_to_redact)``.
    """
    token = token or os.environ.get("CODEKG_GIT_TOKEN") or None
    if not token or not repo_url.lower().startswith(("http://", "https://")):
        return {}, []
    user = os.environ.get("CODEKG_GIT_USER") or "x-access-token"
    b64 = base64.b64encode(f"{user}:{token}".encode()).decode()
    env = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraheader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {b64}",
    }
    return env, [token, b64]


# ── low-level git runner ───────────────────────────────────────────────────────


def _exec(
    args: list[str],
    cwd: Optional[Path] = None,
    env: Optional[dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    cmd = ["git"] + args
    full_env = None
    if env:
        full_env = {**os.environ, **env}
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(cwd) if cwd else None,
            env=full_env,
            check=False,
        )
    except FileNotFoundError:
        raise GitError("git not found on PATH — install git and retry")


def _run(
    args: list[str],
    cwd: Optional[Path] = None,
    check: bool = True,
    env: Optional[dict[str, str]] = None,
    secrets: Optional[list[str]] = None,
) -> str:
    """Run a git command and return stdout.

    Args:
        args: Command arguments (git is prepended automatically).
        cwd: Working directory.
        check: If True, raise GitError on non-zero exit.
        env: Extra environment variables (used for credentials).
        secrets: Literal strings to scrub from error messages.

    Returns:
        Decoded stdout string.

    Raises:
        GitError: When *check* is True and the command fails.
    """
    result = _exec(args, cwd=cwd, env=env)
    if check and result.returncode != 0:
        raise GitError(redact(
            f"git {' '.join(args)} failed (exit {result.returncode}):\n"
            f"{result.stderr.strip()}",
            secrets,
        ))
    return result.stdout.strip()


# ── public helpers ────────────────────────────────────────────────────────────


def is_git_repo(path: Path) -> bool:
    """Return True if *path* is inside a git repository."""
    try:
        _run(["rev-parse", "--git-dir"], cwd=path)
        return True
    except GitError:
        return False


def get_head_sha(repo_path: Path) -> str:
    """Return the current HEAD commit SHA of *repo_path*.

    Raises:
        GitError: If the path is not a git repo or has no commits.
    """
    return _run(["rev-parse", "HEAD"], cwd=repo_path)


def _normalise_url(repo_url: str) -> str:
    url = _URL_CREDS_RE.sub(r"\1", repo_url.strip())
    if "://" not in url and not url.startswith("git@") and (
        url.startswith(("/", ".", "~")) or (len(url) > 1 and url[1] == ":")
    ):
        url = str(Path(url).expanduser().resolve())
    url = url.rstrip("/")
    url = url.removesuffix(".git")
    m = re.match(r"^([a-z][a-z0-9+.-]*://)([^/]+)(.*)$", url, re.IGNORECASE)
    if m:
        url = m.group(1).lower() + m.group(2).lower() + m.group(3)
    return url


def clone_dir_name(repo_url: str) -> str:
    """Return ``<sanitised-name>-<8 hex of sha1(normalised url)>`` for *repo_url*."""
    norm = _normalise_url(repo_url)
    base = re.split(r"[/:\\]", norm)[-1] or "repo"
    name = re.sub(r"[^\w.-]", "_", base).strip(".") or "repo"
    digest = hashlib.sha1(norm.encode()).hexdigest()[:8]
    return f"{name}-{digest}"


_SYNC_LOCKS: dict[str, threading.Lock] = {}
_SYNC_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    with _SYNC_LOCKS_GUARD:
        return _SYNC_LOCKS.setdefault(str(path), threading.Lock())


def _resolve_commit(repo: Path, ref: str) -> str:
    return _run(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=repo)


def is_ancestor(repo_path: Path, base_sha: str, head_sha: str) -> bool:
    """True if *base_sha* exists and is an ancestor of (or equal to) *head_sha*."""
    try:
        r = _exec(["merge-base", "--is-ancestor", base_sha, head_sha], cwd=repo_path)
    except GitError:
        return False
    return r.returncode == 0


def count_commits(repo_path: Path, base_sha: str, head_sha: str) -> int:
    """Number of commits in ``base..head``."""
    return int(_run(["rev-list", "--count", f"{base_sha}..{head_sha}"], cwd=repo_path) or 0)


def sync_repo(
    repo_url: str,
    workdir: Path,
    *,
    branch: Optional[str] = "main",
    commit_sha: Optional[str] = None,
    token: Optional[str] = None,
    checkout: bool = True,
) -> tuple[Path, str]:
    """Clone or fetch *repo_url* and move the working tree to the wanted commit.

    The clone lives in ``workdir/<name>-<8 hex of sha1(url)>`` so two repos with
    the same name never collide.  The target is *commit_sha* when given (must
    exist, else :class:`GitError`), otherwise ``origin/<branch>``.  A missing
    ``main`` falls back to the remote's default branch.  ``branch=None`` means
    the remote's default branch.

    Authentication: *token* (or env ``CODEKG_GIT_TOKEN``) is sent as an HTTP
    ``Authorization: Basic`` extra header passed through git's environment
    config, so it never appears in argv, URLs or logs.

    Args:
        repo_url: Remote URL (or any location ``git clone`` accepts).
        workdir: Parent directory for clones.
        branch: Branch to follow when *commit_sha* is not given.
        commit_sha: Exact commit to check out.
        token: Access token for private HTTP(S) repos.
        checkout: If False only fetch and resolve; the working tree is untouched
            (used by read-only change detection).

    Returns:
        ``(clone_path, head_sha)`` — head_sha is the resolved target and, with
        ``checkout=True``, verified to equal ``HEAD``.

    Raises:
        GitError: On clone/fetch/resolve/checkout failure.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    target = workdir / clone_dir_name(repo_url)
    env, secrets = _auth_env(repo_url, token)
    safe_url = redact(repo_url)

    def run(args: list[str], cwd: Optional[Path] = None, check: bool = True) -> str:
        return _run(args, cwd=cwd, check=check, env=env or None, secrets=secrets)

    with _lock_for(target):
        if (target / ".git").exists() and is_git_repo(target):
            logger.info(f"Fetching {safe_url} into {target}")
            run(["fetch", "--prune", "origin"], cwd=target)
        else:
            if target.exists():
                shutil.rmtree(target)
            logger.info(f"Cloning {safe_url} into {target}")
            run(["clone", "--filter=blob:none", "--no-checkout", repo_url, str(target)])

        if commit_sha:
            try:
                sha = _resolve_commit(target, commit_sha)
            except GitError:
                # May be a commit that no branch head contains; try fetching it
                run(["fetch", "origin", commit_sha], cwd=target, check=False)
                try:
                    sha = _resolve_commit(target, commit_sha)
                except GitError:
                    raise GitError(
                        f"commit {commit_sha} not found in {safe_url}"
                    ) from None
        else:
            sha = ""
            if branch:
                try:
                    sha = _resolve_commit(target, f"origin/{branch}")
                except GitError:
                    if branch != "main":
                        raise GitError(
                            f"branch '{branch}' not found in {safe_url}"
                        ) from None
            if not sha:
                try:
                    sha = _resolve_commit(target, "origin/HEAD")
                except GitError:
                    raise GitError(
                        f"cannot resolve a branch tip in {safe_url} (empty repo?)"
                    ) from None

        if checkout:
            run(["checkout", "--detach", "--force", sha], cwd=target)
            run(["clean", "-fdxq"], cwd=target)
            head = get_head_sha(target)
            if head != sha:
                raise GitError(f"checkout verification failed: HEAD {head} != {sha}")

    return target, sha


def clone_or_fetch(repo_url: str, workdir: Path) -> Path:
    """Clone *repo_url* (or fetch if already cloned) and check out its default branch.

    Back-compat wrapper around :func:`sync_repo`.

    Raises:
        GitError: On clone/fetch failure.
    """
    path, _ = sync_repo(repo_url, workdir, branch=None)
    return path


class FileDiff:
    """Result of a ``git diff`` between two commits."""

    def __init__(
        self,
        added: list[str],
        modified: list[str],
        deleted: list[str],
        renamed: list[tuple[str, str]],
    ) -> None:
        self.added = added
        self.modified = modified
        self.deleted = deleted
        self.renamed = renamed  # list of (old_path, new_path)

    @property
    def changed(self) -> list[str]:
        """All added + modified + new names of renamed files."""
        result = list(self.added) + list(self.modified)
        result += [new for _, new in self.renamed]
        return result

    def __repr__(self) -> str:
        return (
            f"FileDiff(added={len(self.added)}, modified={len(self.modified)}, "
            f"deleted={len(self.deleted)}, renamed={len(self.renamed)})"
        )


def get_diff(repo_path: Path, base_sha: str, head_sha: str) -> FileDiff:
    """Compute a file-level diff between two commits.

    Args:
        repo_path: Path to the git repository.
        base_sha: Base (older) commit SHA.
        head_sha: Head (newer) commit SHA.

    Returns:
        FileDiff with categorised file lists.

    Raises:
        GitError: On git failure.
    """
    raw = _run(
        ["diff", "--name-status", "-M90%", f"{base_sha}..{head_sha}"],
        cwd=repo_path,
    )

    added: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []
    renamed: list[tuple[str, str]] = []

    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0][:1]  # A / M / D / R / C / T

        if status == "A" and len(parts) >= 2:
            added.append(parts[1])
        elif status == "M" and len(parts) >= 2:
            modified.append(parts[1])
        elif status == "D" and len(parts) >= 2:
            deleted.append(parts[1])
        elif status == "R" and len(parts) >= 3:
            renamed.append((parts[1], parts[2]))
        elif status == "C" and len(parts) >= 3:
            # Copy treated like add
            added.append(parts[2])

    return FileDiff(added=added, modified=modified, deleted=deleted, renamed=renamed)


def get_file_list(
    repo_path: Path,
    patterns: list[str],
    exclude_dirs: frozenset[str] | None = None,
) -> list[str]:
    """Return all tracked files matching *patterns* in *repo_path*.

    Uses ``git ls-files`` so only tracked (non-ignored) files are returned.

    Args:
        repo_path: Repository root.
        patterns: Glob patterns relative to repo root (e.g. ``**/*.cs``).
        exclude_dirs: Directory names to exclude (e.g. ``node_modules``).

    Returns:
        Sorted list of relative file paths.
    """
    _EXCLUDE = exclude_dirs or frozenset({
        "node_modules", ".git", "dist", "build", "bin", "obj",
        ".next", "out", "coverage", "__pycache__", ".venv", "target",
    })

    args = ["ls-files"] + patterns
    raw = _run(args, cwd=repo_path)

    result = []
    for p in raw.splitlines():
        if p and not any(part in _EXCLUDE for part in Path(p).parts):
            result.append(p)

    return sorted(result)


def checkout_commit(repo_path: Path, commit_sha: str) -> None:
    """Detach HEAD to *commit_sha* (useful for inspecting a specific commit).

    Args:
        repo_path: Repository root.
        commit_sha: The commit to check out.

    Raises:
        GitError: On failure.
    """
    _run(["checkout", "--detach", commit_sha], cwd=repo_path)
