"""H-OC agent worktrees and the serialized verification gate (plan sections 2.3 and 3.2)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
import threading
from uuid import uuid4

from ..agents.developer.hoc_merge_queue import GateResult
from ..core.config import Settings
from ..core.errors import HarnessError
from ..profiles import VerificationStagePolicy
from ..storage.runtime import RuntimeStore
from ..verification.runner import VerificationRunner, VerificationStageResult
from .manager import RunWorkspaceManager, unlink_directory_links

_LOCAL_DOCS = Path("docs/sdlc-local")
_GATE_LOCK = threading.Lock()


@dataclass(frozen=True)
class AgentWorktree:
    root: Path
    branch: str

    @property
    def ref(self) -> str:
        return f"refs/heads/{self.branch}"


class AgentWorktrees:
    """Operate on this run's linked worktrees through H."""

    def __init__(self, settings: Settings, *, run_id: str):
        self.run_id = run_id
        self.manager = RunWorkspaceManager(settings)

    def create(self, agent: int, *, base_sha: str) -> AgentWorktree:
        """Create or reuse the agent worktree without moving an existing tip."""

        with self.manager.registry_lock():
            h, worktree, registry = self._context(agent)
            base = self._commit(h, base_sha)
            if worktree.root in registry:
                self._require_registered(worktree, registry)
                if not self._ancestor(h, base, worktree.ref):
                    raise self._error("Existing agent worktree is not based on the requested commit.")
            else:
                if worktree.root.exists():
                    raise self._error("Agent worktree path already exists outside Git's registry.")
                worktree.root.parent.mkdir(parents=True, exist_ok=True)
                self._git(h, "worktree", "add", "-b", worktree.branch, str(worktree.root), base)
            return worktree

    def merge(self, agent: int, *, sha: str, attributes_file: Path, keep_conflict: bool = False) -> tuple[str, ...]:
        """Merge into a clean agent worktree, optionally preserving conflicts for a repair turn."""

        _, worktree, registry = self._context(agent)
        self._require_registered(worktree, registry)
        root = worktree.root
        self._require_clean(root)
        commit = self._commit(root, sha)
        if not attributes_file.is_file():
            raise self._error("The run attributes file does not exist.")
        original = self._git(root, "rev-parse", "--verify", "HEAD").stdout.decode("ascii").strip()
        result = self._git(
            root, "-c", f"core.attributesFile={attributes_file.resolve()}", "-c", "rerere.enabled=false",
            "merge", "--no-ff", "--no-edit", "--no-autostash", commit, check=False,
        )
        if not result.returncode:
            return ()
        aborted = False
        try:
            paths = self._git(root, "diff", "--name-only", "-z", "--diff-filter=U", "--").stdout
            if self._has_merge(root):
                if not (paths and keep_conflict):
                    self._git(root, "merge", "--abort")
                aborted = True
        finally:
            if not aborted:
                self._git(root, "reset", "--hard", original)
        if not paths:
            raise self._git_error("merge", result)
        return tuple(sorted(path.decode("utf-8", errors="surrogateescape") for path in paths.split(b"\0") if path))

    def stage_local_file(self, agent: int | None, *, name: str, text: str) -> str:
        """Copy a read-only snapshot into an ignored local path. None selects H."""

        root = self._root(agent)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
            raise self._error(f"Invalid local snapshot name {name!r}.")
        local = root / _LOCAL_DOCS
        ignore, path = local / ".gitignore", local / name
        if any(entry.resolve() != entry or entry.is_symlink() for entry in (local, ignore, path)):
            raise self._error("Local snapshot paths must stay inside the worktree.")
        pending = local / f".{uuid4().hex}.tmp"
        try:
            local.mkdir(parents=True, exist_ok=True)
            try:
                with ignore.open("x", encoding="utf-8") as handle:
                    handle.write("*\n")
            except FileExistsError:
                if not ignore.is_file():
                    raise
            ref = path.relative_to(root).as_posix()
            self._git(root, "check-ignore", "-q", "--", ref)
            if path.exists() and not path.is_file():
                raise self._error("The local snapshot path is not a file.")
            with pending.open("xb") as handle:
                handle.write(text.encode("utf-8"))
            if path.exists():
                path.chmod(0o644)
            try:
                pending.replace(path)
            finally:
                if path.is_file():
                    path.chmod(0o444)
            return ref
        except (OSError, UnicodeError) as exc:
            raise self._error("Could not stage the local snapshot.") from exc
        finally:
            pending.unlink(missing_ok=True)

    def remove(self, agent: int, *, bundle_unmerged: bool = False) -> Path | None:
        """Remove a clean agent worktree and the verified local ref, retaining unmerged work in an audit bundle."""

        with self.manager.registry_lock():
            return self._remove_locked(agent, bundle_unmerged=bundle_unmerged)

    def remove_all_locked(self, *, restart_audit: Path | None = None) -> None:
        """Remove this run's agent branches while the registry lock is held."""

        integration, _ = self._integration()
        pattern = re.compile(re.escape(integration.ref) + r"-agent-([1-9][0-9]*)")
        refs = self._git(integration.root, "for-each-ref", "--format=%(refname)", "refs/heads").stdout
        agents = set()
        for ref in refs.decode("utf-8").splitlines():
            match = pattern.fullmatch(ref)
            if match:
                agents.add(int(match[1]))
        for agent in sorted(agents):
            self._remove_locked(agent, bundle_unmerged=True, restart_audit=restart_audit)

    def restart_locked(self, *, approval_commit: str, archived_tip: str, audit: Path) -> None:
        """Preserve this run's work, then return H to the approved Plan commit."""

        integration, _ = self._integration()
        root = integration.root
        baseline = self._commit(root, approval_commit)
        archived_tip = self._commit(root, archived_tip)
        tip = self._git(root, "rev-parse", "HEAD").stdout.decode("ascii").strip()
        if tip not in {baseline, archived_tip} or not self._ancestor(root, baseline, archived_tip):
            raise self._error("The approved Plan commit is not an ancestor of H.")
        self._bundle(integration, audit / "integration.bundle", archived_tip)
        self._save_dirty(root, audit, "integration")
        self.remove_all_locked(restart_audit=audit)
        self._git(root, "reset", "--hard", baseline)
        self._git(root, "clean", "-fd", "--")
        self._require_clean(root)

    def _remove_locked(
        self, agent: int, *, bundle_unmerged: bool, restart_audit: Path | None = None,
    ) -> Path | None:
        h, worktree, registry = self._context(agent)
        if any(path != worktree.root and entry.get("branch") == worktree.ref for path, entry in registry.items()):
            raise self._error("The agent branch is checked out at a different path.")
        registered = worktree.root in registry
        if registered:
            self._require_registered(worktree, registry)
            if restart_audit is None:
                self._require_clean(worktree.root)
        elif worktree.root.exists():
            raise self._error("Agent worktree path exists outside Git's registry.")

        exists = self._git(h, "show-ref", "--verify", "--quiet", worktree.ref, check=False)
        if exists.returncode == 1 and not registered:
            return None
        if exists.returncode:
            raise self._git_error("show-ref", exists)
        tip = self._git(h, "rev-parse", "--verify", worktree.ref).stdout.decode("ascii").strip()
        bundle = None
        if restart_audit is not None or not self._ancestor(h, tip, "HEAD"):
            if not bundle_unmerged:
                raise self._error("The agent tip is unmerged. An audit bundle is required before removal.")
            audit = self.manager.store.run_dir(self.run_id).resolve() / "audit"
            if audit.resolve() != audit:
                raise self._error("The run audit directory must not be redirected.")
            bundle = (restart_audit / f"agent-{agent}.bundle" if restart_audit is not None
                      else audit / f"agent-{agent}-{tip}-{uuid4().hex}.bundle")
            self._bundle(AgentWorktree(h, worktree.branch), bundle, tip)
        if registered:
            if restart_audit is not None:
                self._save_dirty(worktree.root, restart_audit, f"agent-{agent}")
                unlink_directory_links(worktree.root)
                self._git(h, "worktree", "remove", "--force", str(worktree.root))
            else:
                unlink_directory_links(worktree.root)
                self._git(h, "worktree", "remove", str(worktree.root))
        self._git(h, "update-ref", "-d", worktree.ref, tip)
        return bundle

    def _bundle(self, worktree: AgentWorktree, bundle: Path, tip: str) -> None:
        bundle.parent.mkdir(parents=True, exist_ok=True)
        if not bundle.exists():
            self._git(worktree.root, "bundle", "create", str(bundle), worktree.ref)
        evidence = self._git(worktree.root, "bundle", "list-heads", str(bundle), worktree.ref).stdout
        if evidence.decode("utf-8").strip() != f"{tip} {worktree.ref}":
            raise self._error("The audit bundle does not preserve the branch tip.")

    def _save_dirty(self, root: Path, audit: Path, name: str) -> None:
        diff = self._git(root, "diff", "--binary", "HEAD", "--").stdout
        index = self._git(root, "diff", "--cached", "--binary", "HEAD", "--").stdout
        untracked = self._git(root, "ls-files", "--others", "--exclude-standard", "--").stdout
        if not (diff or index or untracked or self._has_merge(root)):
            return
        snapshots = {"diff": diff, "untracked.txt": untracked}
        if index and index != diff:
            snapshots["index.diff"] = index
        for suffix, data in snapshots.items():
            path = audit / f"{name}.{suffix}"
            if not path.exists():
                temporary = path.with_name(path.name + ".tmp")
                temporary.write_bytes(data)
                temporary.replace(path)

    def prepare(
        self,
        agent: int | None,
        *,
        verification_policy: VerificationStagePolicy,
        preparation_ids: tuple[str, ...] | None = None,
    ) -> tuple[VerificationStageResult, ...]:
        """Prepare H or an agent through the runner's fingerprinted preparations."""

        return VerificationRunner().prepare_worktree(
            target_path=self._root(agent), run_id=self.run_id, verification_policy=verification_policy,
            preparation_ids=preparation_ids,
        )

    def _integration(self) -> tuple[AgentWorktree, dict[Path, dict[str, str]]]:
        record = self.manager.read(self.run_id)
        h = Path(record.worktree_path).resolve()
        canonical = Path(record.canonical_repo).resolve()
        expected = self.manager.settings.target_worktree_root.resolve() / self.run_id
        if h != expected or h == canonical or not h.is_dir():
            raise self._error("The run record does not identify a linked integration worktree.")
        registry = self.manager._worktree_registry(h, run_id=self.run_id)
        if canonical not in registry:
            raise self._error("The integration worktree is not linked to the run repository.")
        integration = AgentWorktree(h, record.branch)
        self._require_registered(integration, registry)
        return integration, registry

    def _context(self, agent: int) -> tuple[Path, AgentWorktree, dict[Path, dict[str, str]]]:
        if type(agent) is not int or agent < 1:
            raise self._error("The agent number must be a positive integer.")
        integration, registry = self._integration()
        path = integration.root.parent / f"{self.run_id}-agent-{agent}"
        if path.resolve() != path or path.is_symlink():
            raise self._error("The agent worktree path must not be redirected.")
        return integration.root, AgentWorktree(path, f"{integration.branch}-agent-{agent}"), registry

    def _root(self, agent: int | None) -> Path:
        if agent is None:
            return self._integration()[0].root
        _, worktree, registry = self._context(agent)
        self._require_registered(worktree, registry)
        return worktree.root

    def root(self, agent: int | None) -> Path:
        """Resolve and verify a worktree. None selects H."""

        return self._root(agent)

    def _require_registered(self, worktree: AgentWorktree, registry: dict[Path, dict[str, str]]) -> None:
        if registry.get(worktree.root, {}).get("branch") != worktree.ref or not worktree.root.is_dir():
            raise self._error(f"Git worktree identity does not match branch {worktree.branch!r}.")

    def _has_merge(self, root: Path) -> bool:
        result = self._git(root, "rev-parse", "--verify", "--quiet", "MERGE_HEAD", check=False)
        if result.returncode not in (0, 1):
            raise self._git_error("rev-parse MERGE_HEAD", result)
        return result.returncode == 0

    def _require_clean(self, root: Path) -> None:
        dirty = self._git(
            root, "--no-optional-locks", "status", "--porcelain=v1", "-z",
            "--untracked-files=all", "--ignore-submodules=none",
        ).stdout
        if dirty or self._has_merge(root):
            raise self._error("Expected a clean agent worktree with no merge in progress.")

    def _commit(self, root: Path, sha: str) -> str:
        if not re.fullmatch(r"[0-9a-fA-F]{7,64}", sha):
            raise self._error(f"Invalid commit SHA {sha!r}.")
        return self._git(root, "rev-parse", "--verify", f"{sha}^{{commit}}").stdout.decode("ascii").strip()

    def _ancestor(self, root: Path, ancestor: str, tip: str) -> bool:
        result = self._git(root, "merge-base", "--is-ancestor", ancestor, tip, check=False)
        if result.returncode not in (0, 1):
            raise self._git_error("merge-base --is-ancestor", result)
        return result.returncode == 0

    def _git(self, root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        try:
            result = subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)
        except OSError as exc:
            raise self._error(f"Could not start Git ({args[0]}).") from exc
        if check and result.returncode:
            raise self._git_error(args[0], result)
        return result

    def _git_error(self, operation: str, result: subprocess.CompletedProcess[bytes]) -> HarnessError:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        return self._error(f"git {operation} exited {result.returncode}: {detail}")

    def _error(self, message: str) -> HarnessError:
        return HarnessError("agent_worktree_failed", message, run_id=self.run_id)


def make_merge_gate(
    *,
    store: RuntimeStore,
    run_id: str,
    verification_policy: VerificationStagePolicy,
    extra_stage_ids: tuple[str, ...] = (),
) -> Callable[[Path], GateResult]:
    """Adapt the pinned verification runner to the merge queue and retain its evidence."""

    runner = VerificationRunner()

    def gate(root: Path) -> GateResult:
        extras = set(extra_stage_ids)
        selected = {"typecheck", "unit"} | extras
        if (extras - {"database_ready", "migrate_and_seed"} or len(extras) != len(extra_stage_ids)
                or not selected <= set(verification_policy.stage_ids)):
            raise HarnessError(
                "verification_stage_policy_invalid", "Invalid H-OC merge gate stages for the run policy.", run_id=run_id,
            )
        with _GATE_LOCK:
            result = runner.run_harness_stages(
                target_path=root, run_id=run_id, verification_policy=verification_policy,
                stage_ids=tuple(stage for stage in verification_policy.stage_ids if stage in selected),
            )
            log = store.write_json_artifact(run_id, f"hoc_gate_{uuid4().hex}.json", result.to_dict())
        failed = next(
            (step for step in (*result.preparation_results, *result.stage_results) if step.status != "passed"),
            None,
        )
        stage = failed.stage_id if failed else result.stage_results[-1].stage_id if result.stage_results else "preflight"
        return GateResult(result.status == "passed" and failed is None, stage, result.reason, store.relative(log))

    return gate
