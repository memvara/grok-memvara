
"""Gates for the Grok plugin.

Every file the client will read is asserted here.
"""

from __future__ import annotations

import ast
import http.client
import io
import json
import os
import pathlib
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import types
import unittest
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugin"
SKILL = PLUGIN / "skills" / "memvara"
#: The module lives INSIDE the skill, so `skill.lock` vendors it to every host
#: through the sync that already exists. It sat at `plugin/auth/` as well for one
#: commit -- two byte-identical copies with nothing saying which the commands ran.
AUTH_SCRIPT = SKILL / "scripts" / "memvara_auth.py"
HOSTED = "https://app.memvara.dev/mcp"
REPO_NAME = "memvara/grok-memvara"


def _json(path: pathlib.Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


class LibraryUnreachable(Exception):
    """Neither a local checkout nor GitHub could answer. Raised, never swallowed.

    A drift check that quietly passes when it cannot look is the same as no drift check.
    This repository has already been caught by exactly that shape: `skill-sync.yml` failed
    on every scheduled run for days while nothing here went red, because the vendored copy
    and `skill.lock` stayed consistent with each other and the only thing that would have
    noticed was a scheduled job nobody read.
    """


def _trust() -> "ssl.SSLContext":
    """A context that trusts the same roots `curl` does.

    python.org's macOS build ignores the system trust store, so an unqualified `urlopen`
    raises CERTIFICATE_VERIFY_FAILED against a certificate `curl` accepts. Without this the
    drift check below does not fail on a Mac -- it *skips*, reporting the library as
    unreachable when the library is fine, which is the quiet half of the failure it was
    written to catch.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "memvara-tests"})
    with urllib.request.urlopen(request, timeout=30, context=_trust()) as resp:
        return bytes(resp.read())


def _library_bytes(sha: str, path: str) -> bytes:
    root = os.environ.get("MEMVARA_LIBRARY")
    if root:
        try:
            return subprocess.check_output(
                ["git", "-C", root, "show", f"{sha}:{path}"],
                stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            # The checkout has the sha `skill.lock` names and nothing else: CI clones the
            # library AT that sha, shallow, so the library's current HEAD is simply not an
            # object here. Falling back to the network rather than failing is what lets the
            # drift check below run on CI at all -- and it only matters when the lock is
            # stale, which is precisely when the check has something to say.
            pass
    return _fetch(f"https://raw.githubusercontent.com/memvara/memvara/{sha}/{path}")


def _library_head() -> str:
    """The library default branch's current sha, or raise `LibraryUnreachable`."""
    root = os.environ.get("MEMVARA_LIBRARY")
    if root:
        for ref in ("origin/main", "main"):
            try:
                return subprocess.check_output(
                    ["git", "-C", root, "rev-parse", ref],
                    stderr=subprocess.DEVNULL).decode().strip()
            except subprocess.CalledProcessError:
                continue
    try:
        body = _fetch("https://api.github.com/repos/memvara/memvara/commits/main")
        return str(json.loads(body)["sha"])
    except Exception as exc:
        raise LibraryUnreachable(str(exc)) from exc


def _library_skill_files(sha: str) -> "set[str]":
    """Every path under the packaged skill at `sha`, relative to it."""
    root = os.environ.get("MEMVARA_LIBRARY")
    prefix = "memvara/skills/memvara/"
    if root:
        try:
            out = subprocess.check_output(
                ["git", "-C", root, "ls-tree", "-r", "--name-only", sha,
                 "memvara/skills/memvara"], stderr=subprocess.DEVNULL).decode()
        except subprocess.CalledProcessError:
            # Not an object in this checkout -- see `_library_bytes`. Ask GitHub instead
            # of reporting the library unreachable, which would SKIP the check on the one
            # run that needed it.
            out = None
        if out is not None:
            return {line[len(prefix):] for line in out.splitlines()
                    if line.startswith(prefix)}
    try:
        tree = json.loads(_fetch(
            f"https://api.github.com/repos/memvara/memvara/git/trees/{sha}?recursive=1"))
    except Exception as exc:
        raise LibraryUnreachable(str(exc)) from exc
    return {entry["path"][len(prefix):] for entry in tree.get("tree", [])
            if entry.get("type") == "blob" and entry["path"].startswith(prefix)}


def _lock() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (ROOT / "skill.lock").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


class SkillTree(unittest.TestCase):
    def test_skill_has_front_matter_and_references(self) -> None:
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.splitlines()[0] == "---")
        named = set(re.findall(r"references/([a-z0-9-]+\.md)", text))
        self.assertTrue(named)
        for name in named:
            self.assertTrue((SKILL / "references" / name).is_file(), name)

    def test_matches_library_at_lock_sha(self) -> None:
        lock = _lock()
        self.assertEqual(lock["repo"], "memvara/memvara")
        sha = lock["sha"]
        self.assertEqual(len(sha), 40)
        for rel in ("SKILL.md", "references/hosted-mcp.md"):
            expected = _library_bytes(sha, f"memvara/skills/memvara/{rel}")
            self.assertEqual((SKILL / rel).read_bytes(), expected, rel)

    def test_the_vendored_skill_is_not_behind_the_library(self) -> None:
        """The whole tree, against the library's CURRENT default branch.

        `test_matches_library_at_lock_sha` cannot catch a stale sync and is not supposed
        to: it compares the copy against the sha the copy itself names, so a lock and a
        tree frozen together agree with each other forever. That is exactly how this repo
        shipped a skill five commits behind -- `skill-sync.yml` dying every night on a
        permission the organization pins, nothing here going red, and the agreement
        between the two stale files being the thing that hid it.

        Two deliberate choices about noise. It compares BYTES rather than shas, so the
        library moving does not fail this repository -- only the library's *skill* moving
        does, which is rare. And it compares the file SET as well, because a new reference
        file upstream is drift that a per-file comparison of the files we already have
        would never see.

        When the library cannot be reached this SKIPS rather than passes. A skip is
        visible in the run output; a pass is not, and a check that silently succeeds when
        it could not look is the failure it exists to prevent, one level up.
        """
        try:
            head = _library_head()
            upstream = _library_skill_files(head)
        except LibraryUnreachable as exc:
            raise unittest.SkipTest(
                f"library unreachable, drift NOT checked: {exc}") from exc

        self.assertTrue(upstream, "the library reported an empty skill tree")
        # `__pycache__` is on neither side: git never lists it, and it appears only
        # because this repository EXECUTES the vendored script -- deliberately, so a
        # syntactically broken upstream cannot ship. It could not arise while the skill
        # was markdown; it arrived when the module moved inside the skill. The five
        # sibling repos that do not import the script deliberately carry no such filter.
        ours = {str(path.relative_to(SKILL)) for path in SKILL.rglob("*")
                if path.is_file() and "__pycache__" not in path.parts}
        self.assertEqual(
            ours, upstream,
            f"the vendored skill's file set differs from the library at {head[:7]} — "
            "run scripts/sync_plugin_repos.py from the library and update skill.lock")

        drifted = []
        for rel in sorted(upstream):
            expected = _library_bytes(head, f"memvara/skills/memvara/{rel}")
            if (SKILL / rel).read_bytes() != expected:
                drifted.append(rel)
        self.assertEqual(
            drifted, [],
            f"vendored skill is behind memvara/memvara@{head[:7]}: {drifted} — "
            "sync it")


class License(unittest.TestCase):
    def test_apache(self) -> None:
        text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("Apache License", text)
        self.assertIn("Version 2.0", text)


class SharedInstructions(unittest.TestCase):
    """CLAUDE.md is shared across every plugin repo, and nothing used to carry it.

    It was hand-copied and it drifted: eleven of fourteen sections were byte-identical
    across all seven repositories while a section written in one of them reached none of
    the others. The canonical is `plugin-claude.md` in the library; `skill-sync.yml`
    composes this file from it and preserves the `local:` block, because two sections
    legitimately differ per repo — a repository's own runtime facts, and hook rules that
    only one plugin needs.

    Without this guard the sync would be a tidier way to drift rather than an end to it,
    which is the objection the section it carries makes about hand-maintained copies.
    """

    BEGIN = "<!-- local: begin"
    END = "<!-- local: end -->"

    def _text(self) -> str:
        return (ROOT / "CLAUDE.md").read_text(encoding="utf-8")

    def test_the_local_block_is_delimited_exactly_once(self) -> None:
        """Two of either marker and the splice takes the wrong span; none and the composer
        refuses rather than replacing this repository's sections with a placeholder.
        """
        text = self._text()
        self.assertEqual(text.count(self.BEGIN), 1)
        self.assertEqual(text.count(self.END), 1)
        self.assertLess(text.index(self.BEGIN), text.index(self.END))

    def test_the_shared_half_matches_the_library(self) -> None:
        """Compared against the LIBRARY, never against this file's own halves.

        A check that read both halves of one file would prove it internally consistent and
        nothing else — exactly how a vendored skill sat five commits behind while its own
        drift test passed.
        """
        lock = _lock()
        try:
            canonical = _library_bytes(lock["sha"], "plugin-claude.md").decode("utf-8")
        except Exception as exc:  # noqa: BLE001
            raise unittest.SkipTest(
                f"library has no plugin-claude.md at {lock['sha'][:7]}: {exc}") from exc
        text = self._text()
        head, rest = text.split(self.BEGIN, 1)
        _, tail = rest.split(self.END, 1)
        want_head, want_tail = canonical.split("@@LOCAL@@\n", 1)
        self.assertEqual(head, want_head,
                         "text above the local block drifted — edit plugin-claude.md in "
                         "memvara/memvara, not the copy here")
        self.assertEqual(tail.lstrip("\n"), want_tail.lstrip("\n"),
                         "text below the local block drifted from plugin-claude.md")

    def test_the_local_block_holds_what_only_this_repo_knows(self) -> None:
        """Not decorative: it carries the two sections that differ per repo. A sync that
        flattened it would lose them silently — the file would still read as a complete
        CLAUDE.md, just one belonging to a different repository.
        """
        local = self._text().split(self.BEGIN, 1)[1].split(self.END, 1)[0]
        self.assertIn("Runtime facts that cost hours to find", local)
        self.assertIn("If this repo ships hooks", local)


#: The heading the hook verdict is written under, and the issue holding its receipts.
#: Named here rather than searched for by keyword for the same reason as `_AUTH_HEADING`:
#: the guard reads only what sits beneath the heading, and a keyword scan of the whole
#: page passes on words scattered through sections about something else.
_HOOK_VERDICT_HEADING = "## What Memvara does not do on Grok"
_HOOK_VERDICT_ISSUE = "https://github.com/memvara/grok-memvara/issues/26"


class Hygiene(unittest.TestCase):
    def test_no_npx_in_json(self) -> None:
        """No JSON *this repo ships* may reach for npx.

        `_library` is skipped because it is not ours: CI checks the library out there, at
        `skill.lock`'s sha, so the drift test can run offline. The moment that lock moves
        to a sha where the library has an npm package, an unfiltered scan reads
        `_library/npm/memvara/package.json` -- whose description legitimately begins "npx
        memvara" -- and fails a sync PR for a string in another repository. That is not
        hypothetical: it happened in claude-memvara on 2026-08-25, and this lock bump is
        the one that would have done it here.

        The scan stays repo-wide rather than narrowing to `plugin/`: the rule is about
        anything shipped from here, and an allowlist of directories stops covering the
        next one added.
        """
        for path in ROOT.rglob("*.json"):
            if {"node_modules", "_library"} & set(path.parts):
                continue
            self.assertNotIn("npx", path.read_text(encoding="utf-8"), path)

    def test_this_host_states_why_it_ships_no_hooks(self) -> None:
        """Replaces `test_no_hooks`, which asserted an absence and nothing else.

        Two halves, and only together are they a guard.

        The absence half is the same assertion as before: no `hooks/`, no `.app.json`.
        On its own it is satisfied by a repository where somebody quietly deleted the
        explanation, which is indistinguishable from one that never had a reason.

        The explanation half is new and is the point. Grok was spiked on 1.0.13 on
        2026-08-30 and the answer was measured, not assumed: a `UserPromptSubmit` hook's
        `systemMessage` renders as a banner and its `additionalContext` is discarded, so
        the plugin would report a recall on screen every turn while the model received
        nothing. `hooks/` is absent here for that reason, and a reader who is looking at
        the Grok banner right now -- sourced from the Claude Code plugin cache, not from
        this repository -- needs the page to say so.

        A host that fails must fail out loud, in the shipped artifact, forever.

        The old `commands/` half of `test_no_hooks` is not repeated here; it was already
        replaced by `test_plugin_tree` and
        `Commands.test_every_command_this_plugin_declares_points_at_a_file_that_exists`.
        """
        self.assertFalse((PLUGIN / "hooks").exists(),
                         "this plugin now ships a hooks directory, so the README section "
                         "asserted below has become false and the verdict in "
                         f"{_HOOK_VERDICT_ISSUE} needs revisiting before it can ship")
        self.assertFalse((PLUGIN / ".app.json").exists())

        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(_HOOK_VERDICT_HEADING, text,
                      f"the README has no {_HOOK_VERDICT_HEADING!r} section, so this "
                      "plugin ships without memory on every turn and says nothing about "
                      "why")
        # Bounded to the section, like `AuthReadme._section`. Against the whole page each
        # substring below is satisfied by a word somewhere else -- `memory_recall` is
        # named in the skill discussion and `Grok` is in the install block.
        section = text.split(_HOOK_VERDICT_HEADING, 1)[1].split("\n## ", 1)[0]

        self.assertIn("UserPromptSubmit", section,
                      "the section does not name the event that was measured, so a "
                      "reader cannot tell which half of the hook surface was tested nor "
                      "check the finding themselves")
        self.assertIn("1.0.13", section,
                      "the section does not say which Grok version this was measured on, "
                      "so it cannot go stale and cannot be rechecked")
        self.assertIn("2026-08-30", section,
                      "the section does not say when this was measured")
        self.assertIn(_HOOK_VERDICT_ISSUE, section,
                      "the section does not link the issue holding the receipts, so the "
                      "evidence for this decision exists nowhere a reader can reach")

    def test_github_org(self) -> None:
        env = os.environ.get("GITHUB_REPOSITORY")
        if env:
            self.assertEqual(env, REPO_NAME)

class GrokManifest(unittest.TestCase):
    def test_manifest(self) -> None:
        body = _json(PLUGIN / "plugin.json")
        self.assertEqual(body["name"], "memvara")
        self.assertEqual(body["repository"], f"https://github.com/{REPO_NAME}")

    def test_marketplace(self) -> None:
        body = _json(ROOT / ".grok-plugin" / "marketplace.json")
        self.assertEqual(body["plugins"][0]["source"], "./plugin")
        self.assertEqual(body["metadata"]["version"], Version.VERSION)

    def test_root_manifest_so_grok_validate_dot_works(self) -> None:
        """`grok plugin validate .` reads this file and counts what it declares.

        The commands are asserted here as well as in `plugin/plugin.json` because this
        manifest is the one a person validating the repository root is shown. Without the
        entry `grok plugin validate .` reports `0 command dir(s)` on a repository that
        ships four -- a claim about the tree, made by the tree, disagreeing with it and
        going green.
        """
        body = _json(ROOT / "plugin.json")
        self.assertEqual(body["name"], "memvara")
        self.assertEqual(body["skills"], ["./plugin/skills/"])
        self.assertEqual(body["commands"], ["./plugin/commands/"])
        self.assertTrue((ROOT / "plugin" / "commands").is_dir())
        self.assertEqual(
            _json(ROOT / ".mcp.json")["mcpServers"]["memvara"]["url"],
            HOSTED,
        )

    def test_mcp(self) -> None:
        server = _json(PLUGIN / ".mcp.json")["mcpServers"]["memvara"]
        self.assertEqual(server["url"], HOSTED)
        self.assertEqual(server.get("type"), "http")
        self.assertNotIn("command", server)

    def test_readme(self) -> None:
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("grok plugin marketplace add memvara/grok-memvara", text)
        self.assertIn("grok plugin install memvara@memvara/grok-memvara --trust", text)
        self.assertIn(HOSTED, text)
        self.assertNotIn("npx ", text)
        self.assertNotIn("chatgpt", text.lower())

    def test_plugin_tree(self) -> None:
        """Every file the client receives, named one by one.

        The auth module and the four commands are listed individually rather than by
        directory: a wildcard for `auth/` would let a second module, a cache or a stray
        script ship from this plugin without anything going red, and this is the guard
        that replaced `test_no_hooks`'s assertion that `commands/` did not exist at all.
        """
        allowed = {
            pathlib.Path("plugin.json"),
            pathlib.Path(".mcp.json"),
            pathlib.Path("commands") / "authenticate.md",
            pathlib.Path("commands") / "login.md",
            pathlib.Path("commands") / "logout.md",
            pathlib.Path("commands") / "stats.md",
        }
        for path in SKILL.rglob("*"):
            if path.is_file():
                allowed.add(path.relative_to(PLUGIN))
        # Importing the auth module writes bytecode next to it, so `__pycache__` appears
        # inside `plugin/` the moment this suite runs. It is gitignored and is not shipped;
        # excluding it here is what keeps the guard about the tree rather than about
        # whichever test happened to run first.
        found = {p.relative_to(PLUGIN) for p in PLUGIN.rglob("*")
                 if p.is_file() and "__pycache__" not in p.parts}
        self.assertFalse(found - allowed, found - allowed)


class Version(unittest.TestCase):
    """Every version this repository states must be the same one, and none may hide.

    Five skill syncs shipped under 0.1.0. The vendored skill is the whole of what a client
    receives here, it changed five times, and the string a client compares never moved.
    `claude-memvara` was caught by the identical shape at larger scale -- twenty-one
    commits on main behind an unchanged version, `/plugin update` answering "already at
    the latest version" for every one of them.

    Three deliberate choices, each of them paid for by a sabotage run.

    Files are found by walking the tree, not by reading a list, so a manifest nobody
    remembered cannot go unchecked. `DECLARED` is then the completeness half -- it names
    the manifests that MUST carry a version, and it is compared against the walk in both
    directions, which is what keeps a hand-written list from quietly narrowing coverage.

    The file set comes from `git ls-files`, not from the filesystem. Two sweeps of the
    tree were tried first and both were wrong in a way a passing run could not show: one
    ignored directories by absolute path, which excluded the entire repository whenever the
    checkout was a worktree (those live under `.claude/worktrees/`, so `.claude` was in the
    parts of every path); the next was caught by CI dragging in six manifests from the
    library checkout under `_library/`. Git already knows which files this repository owns.

    And the assertions demand presence rather than absence of the wrong value. The
    coverage check was first written as a bare set comparison and passed on that broken
    walk because both sides were empty; the value check alone still passes when one
    manifest of several drops its version entirely. A guard an absence satisfies has
    stopped guarding.
    """

    VERSION = "0.2.4"
    DECLARED = {
        '.grok-plugin/marketplace.json',
        'plugin.json',
        'plugin/plugin.json',
    }

    @classmethod
    def _walk(cls, node: object, where: str = ""):
        """Every `version` string at any depth, with the pointer that reached it."""
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "version" and isinstance(value, str):
                    yield f"{where}.{key}", value
                else:
                    yield from cls._walk(value, f"{where}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                yield from cls._walk(value, f"{where}[{index}]")

    @classmethod
    def _candidates(cls) -> list:
        """Every JSON file this repository TRACKS -- asked of git, not of the filesystem.

        The filesystem is the wrong referent. CI checks the library out into `_library/`,
        which carries the sibling plugins' own manifests, and an `rglob` swept all six into
        the walk; a denylist would then have to grow a name for every scratch directory
        anyone ever creates, and the first one nobody thought of is a false failure. What
        the question actually means is "files this repository owns", and git is the thing
        that knows. Untracked checkouts and nested worktrees fall out for free.

        No fallback when git cannot answer. A fallback here would silently cover less than
        the caller believes, which is the failure this whole class exists to prevent.
        """
        listed = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z", "*.json"],
            check=True, capture_output=True, text=True).stdout
        return [
            ROOT / name for name in listed.split("\0")
            if name and pathlib.PurePath(name).name != "package-lock.json"
        ]

    def _stated(self) -> list:
        found = []
        for path in self._candidates():
            try:
                body = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                continue
            found.extend((path, where, value) for where, value in self._walk(body))
        return found

    def test_every_version_this_repo_states_is_the_released_one(self) -> None:
        stated = self._stated()
        self.assertTrue(
            stated, "no file states a version at all -- this guard has stopped guarding")
        for path, where, value in stated:
            self.assertEqual(
                value, self.VERSION,
                f"{path.relative_to(ROOT)}{where} says {value!r}; a partial bump is how a "
                "client gets told it is current while the contents moved underneath it")

    def test_exactly_the_manifests_that_must_declare_a_version_do(self) -> None:
        """Both directions, because each catches a mistake the other cannot see.

        A file the walk misses is a version nobody checks. A file that has stopped
        declaring one is a manifest shipping unversioned -- invisible to the value check
        above, which goes green as soon as any other file still says the right thing.
        Confirmed by sabotage: deleting the key from one of three manifests left it green.
        """
        reached = {str(path.relative_to(ROOT)) for path, _where, _value in self._stated()}
        by_text = {
            str(path.relative_to(ROOT)) for path in self._candidates()
            if '"version"' in path.read_text(encoding="utf-8")
        }
        self.assertEqual(by_text, self.DECLARED, "a manifest gained or lost its version")
        self.assertEqual(reached, self.DECLARED, "the JSON walk missed a stated version")

    def test_the_release_number_is_written_down_exactly_once_in_this_suite(self) -> None:
        """`VERSION` above is the only place the tests name it.

        Ported from claude-memvara, which learned it the same way this repository just
        did: another test asserted the release literally, so a bump had to be applied in
        two places and one of them was missed. Every extra place is the mechanism a
        partial bump needs, and a partial bump is what tells a client it is current while
        the contents moved underneath it.

        The duplicates that prompted this now read `Version.VERSION` instead, which is
        why they no longer count.
        """
        source = pathlib.Path(__file__).read_text(encoding="utf-8")
        self.assertEqual(
            source.count(f'"{self.VERSION}"'), 1,
            f"{self.VERSION} appears more than once in this file; VERSION is meant to be "
            "the single place the suite states the release")




#: Recorded from the live endpoint on 2026-08-30, reproduced here as they arrived. A
#: fixture composed by hand would agree with our assumptions about the server forever;
#: these agreed with the server once, which is the only way a fixture is evidence. The
#: opaque identifiers are the only edit -- `token_id` and the tenant were elided in the
#: recording and nothing here reads them.
_HEALTH_OK = {"status": "ok", "memvara_version": "0.9.0"}
_WHOAMI_OK = {
    "token_id": "tok_recorded",
    "scope": {"tenant": "prj_recorded", "user": None, "agent": None, "session": None},
    "granted_privilege": "write",
    "effective_privilege": "write",
    "expires_at": None,
    "read_only": False,
}
#: `/v1/*` refuses inside an `error` OBJECT.
_WHOAMI_EXPIRED = {"error": {
    "code": "unauthenticated",
    "message": "this token expired at 2026-08-30T16:15:44.695978+00:00"}}
#: `/mcp` and the RFC 8628 routes refuse with a flat pair instead. Code that parses one
#: envelope reads the other as saying nothing, which lands every refusal in the fallback
#: state -- so both shapes are fed to the same classifier here on purpose.
_REFUSED_REVOKED = {"error": "unauthorized",
                    "error_description": "this token has been disabled"}
_REFUSED_UNKNOWN = {"error": "unauthorized",
                    "error_description": "the bearer token is not recognised"}


class CredentialProbe(unittest.TestCase):
    """Six states a machine can be in, and the probe must never merge two of them.

    The incident these tests exist for, measured 2026-08-30: a host's own OAuth minted a
    token that lived 59 minutes, and when it died every surface said some version of "not
    authenticated". An evening went into re-authenticating a credential that had worked
    perfectly and then expired, because no message anywhere contained the word "expired".

    Every expected value below is written out here rather than read from the module under
    test. A guard that asks the implementation what it should expect shrinks when the
    implementation shrinks, and passes under exactly the sabotage it was written to catch
    -- which happened on this programme on 2026-08-30.
    """

    def setUp(self) -> None:
        self._env = {name: os.environ.get(name) for name in
                     ("HOME", "MEMVARA_API_KEY", "MEMVARA_SERVER_URL")}
        self._home = tempfile.mkdtemp(prefix="memvara-auth-home-")
        os.environ["HOME"] = self._home
        os.environ.pop("MEMVARA_API_KEY", None)
        os.environ.pop("MEMVARA_SERVER_URL", None)
        self.calls: "list[tuple[str, str, str | None]]" = []

    def tearDown(self) -> None:
        for name, value in self._env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(self._home, ignore_errors=True)

    def _auth(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("memvara_auth", AUTH_SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # The module holds a connection open across calls, so a fake one left in its cache
        # would be handed to the next test in this process. Dropped the moment this test
        # ends rather than at some later import.
        self.addCleanup(module.close)
        return module

    def _replies(self, module, script) -> None:
        """Replace the module's one network primitive with the recorded replies.

        `script` maps a path to `(status, body)`, or to an exception the call raises.
        The exception is the module's own `Unreachable` because that is what the real
        `request` raises for a transport failure -- asserted directly in
        `test_a_network_failure_is_not_reported_as_a_credential_failure`, so this fake
        cannot drift into rehearsing a failure the module never actually produces.
        """
        original = module.request
        self.addCleanup(setattr, module, "request", original)
        calls = self.calls

        def fake(method, path, *, body=None, auth=None, timeout=10.0):
            calls.append((method, path, auth))
            reply = script[path]
            if isinstance(reply, BaseException):
                raise reply
            return reply

        module.request = fake

    def _probe(self, module, *, whoami=None, key="mv_recorded", health=None):
        """One probe against the recorded replies, with the credential `key` in the env.

        Paths are written out here rather than taken from the module: a probe that stopped
        asking `/v1/health` must fail on the missing key, not silently be handed whatever
        it asks for instead.
        """
        script = {"/v1/health": (200, _HEALTH_OK) if health is None else health}
        if whoami is not None:
            script["/v1/whoami"] = whoami
        if key is None:
            os.environ.pop("MEMVARA_API_KEY", None)
        else:
            os.environ["MEMVARA_API_KEY"] = key
        self.calls = []
        self._replies(module, script)
        return module.probe()

    def test_each_credential_state_reports_itself_differently(self) -> None:
        """Six states, six distinct messages, driven by the recorded bodies.

        "expired" and "absent" collapsing into one string is the whole failure this
        command exists to end. The six names are stated here as literals; if the module
        ever learns to answer only five, this is the line that says so.
        """
        module = self._auth()
        results = {
            "authenticated": self._probe(module, whoami=(200, _WHOAMI_OK)),
            "expired": self._probe(module, whoami=(401, _WHOAMI_EXPIRED)),
            "revoked": self._probe(module, whoami=(401, _REFUSED_REVOKED)),
            "unknown": self._probe(module, whoami=(401, _REFUSED_UNKNOWN)),
            "absent": self._probe(module, whoami=(401, _REFUSED_UNKNOWN), key=None),
            "unreachable": self._probe(module, whoami=(200, _WHOAMI_OK),
                                       health=(503, {})),
        }
        self.assertEqual(
            {result["state"] for result in results.values()},
            {"authenticated", "expired", "revoked", "unknown", "absent", "unreachable"},
            "the six states must all be reachable and all be named")
        for expected, result in results.items():
            self.assertEqual(result["state"], expected,
                             f"the {expected} fixture reported {result['state']!r}")
        details = [result["detail"] for result in results.values()]
        for detail in details:
            self.assertTrue(detail.strip(), "a state with no message tells nobody anything")
        self.assertEqual(len(set(details)), 6,
                         "two states share a message, which is the failure itself: "
                         f"{sorted(details)}")
        self.assertIn(
            "2026-08-30T16:15:44.695978+00:00", results["expired"]["detail"],
            "the expired message must name the instant it expired -- 'expired' without a "
            "time is what sends a user to re-authenticate a token that was fine an hour ago")
        self.assertEqual(results["authenticated"]["privilege"], "write")
        self.assertIsNone(results["authenticated"]["expires_at"],
                          "the recorded key never expires and the probe must say so "
                          "rather than assume a TTL")
        self.assertIs(results["authenticated"]["read_only"], False)

    def test_only_a_401_is_read_as_a_verdict_on_the_credential(self) -> None:
        """A refusal that is not a 401 says nothing about the key, whatever words it uses.

        `expired` and `revoked` are claims about the credential, and only a 401 is the
        deployment making one. The wording checks matched on any status, so a 403 saying
        "writes are disabled on this plan" reported a live credential as revoked and sent
        the user into a device flow that could not help them -- the same error the
        `absent` fallback below is guarded against, arriving from the other side and
        worse, because here the key is fine.

        The bodies are refusals a deployment can plausibly send, each carrying a word this
        function looks for, none of them about the key.
        """
        module = self._auth()
        cases = (
            (403, "writes are disabled on this plan"),
            (503, "this project is disabled for maintenance"),
            (500, "the cache expired at 03:00, retry"),
        )
        for status, message in cases:
            with self.subTest(status=status, message=message):
                self._replies(module, {
                    module.HEALTH_PATH: (200, {"status": "ok"}),
                    module.WHOAMI_PATH: (status, {"error": {
                        "code": "refused", "message": message, "detail": None}}),
                })
                # Restored, because `_auth()` hands back the module `sys.modules` already
                # holds -- so a stub left on it is inherited by every later test in this
                # process. Left unrestored, this one silently rewrote what
                # `test_the_probe_names_where_the_credential_came_from` was reading.
                self.addCleanup(setattr, module, "credential", module.credential)
                module.credential = lambda: ("mv_live_and_working", module.ENV_KEY)
                state = module.probe()["state"]
                self.assertEqual(
                    state, "unknown",
                    f"HTTP {status} carrying {message!r} was read as {state!r}; only a "
                    "401 is the deployment making a claim about the credential")

    def test_a_network_failure_is_not_reported_as_a_credential_failure(self) -> None:
        """`/v1/health` takes no credential, so it separates "the deployment is down"
        from "your key is bad". Without it every outage reads as a login problem and the
        user is sent to fix something that was never broken.

        Asserted three ways: the transport failing, the deployment answering a health
        check with an error, and -- the load-bearing one -- that `/v1/whoami` is never
        reached at all in either case.
        """
        module = self._auth()

        # First, and before anything replaces it: the real primitive raises `Unreachable`
        # for a transport failure. Without this the fake below would be rehearsing an
        # exception type nothing ever throws -- and it very nearly was, because `_probe`
        # replaces `request` for the rest of the test.
        original = module._connect
        self.addCleanup(setattr, module, "_connect", original)

        def _dead(*_args, **_kwargs):
            raise ConnectionRefusedError("nothing is listening")

        module._connect = _dead
        with self.assertRaises(module.Unreachable):
            module.request("GET", "/v1/health")
        module._connect = original
        module.close()

        refused = self._probe(module, whoami=(200, _WHOAMI_OK),
                              health=module.Unreachable("connection refused"))
        self.assertEqual(refused["state"], "unreachable")
        self.assertEqual([path for _method, path, _auth in self.calls], ["/v1/health"],
                         "the probe asked about the credential after failing to reach "
                         "the deployment at all")

        sick = self._probe(module, whoami=(200, _WHOAMI_OK), health=(503, {}))
        self.assertEqual(sick["state"], "unreachable")
        self.assertEqual([path for _method, path, _auth in self.calls], ["/v1/health"])

    def test_the_request_carries_all_four_things_the_endpoint_requires(self) -> None:
        """User-Agent, a CA bundle, the CSRF header, and `http.client` rather than
        `urllib`. Each is asserted PRESENT and individually, because every one of them
        fails in a way that names something else:

        * the stock `Python-urllib/3.13` agent is refused by Cloudflare with error 1010,
          a 403 at the edge with nothing in it about the client's name;
        * `create_default_context()` alone loads no roots at all on python.org's macOS
          build, so verification fails on a certificate `curl` accepts;
        * a missing `X-Memvara-CSRF` is `403 csrf_failed`, which reads as an auth problem;
        * `urlopen` cannot hold a connection open, and this module makes two calls per
          probe and one every five seconds while polling.
        """
        module = self._auth()
        original_connect, original_context = module._connect, module._context
        original_http = module.http
        self.addCleanup(setattr, module, "_connect", original_connect)
        self.addCleanup(setattr, module, "_context", original_context)
        self.addCleanup(setattr, module, "http", original_http)

        sent = []

        class _Response:
            status = 200

            def read(self):
                return json.dumps(_WHOAMI_OK).encode("utf-8")

        class _Conn:
            def request(self, method, path, body, headers):
                sent.append((method, path, body, headers))

            def getresponse(self):
                return _Response()

            def close(self):
                pass

        module._connect = lambda *_args, **_kwargs: _Conn()
        status, body = module.request("GET", "/v1/whoami", auth="mv_recorded")
        self.assertEqual(status, 200)
        self.assertEqual(body, _WHOAMI_OK, "the parsed body is what a caller reads")

        self.assertTrue(sent, "the request never reached a connection")
        _method, _path, _payload, headers = sent[-1]
        lowered = {name.lower(): value for name, value in headers.items()}

        agent = lowered.get("user-agent", "")
        self.assertTrue(agent.strip(), "no User-Agent: Cloudflare answers 1010 at the edge")
        self.assertNotIn("urllib", agent.lower())
        self.assertNotIn("python-", agent.lower())

        self.assertEqual(lowered.get("x-memvara-csrf"), "cli",
                         "without this header the device routes answer 403 csrf_failed")
        self.assertEqual(lowered.get("authorization"), "Bearer mv_recorded",
                         "the credential must reach the endpoint it is being checked at")

        context = module._context()
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertTrue(context.check_hostname, "verification must never be relaxed")
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        try:
            import certifi  # noqa: PLC0415
        except ImportError:
            certifi = None
        if certifi is not None:
            self.assertTrue(
                context.get_ca_certs(),
                "certifi is importable here and the context loaded no roots at all, so "
                "the bare default context was used -- the exact configuration that fails "
                "CERTIFICATE_VERIFY_FAILED on python.org's macOS build")

        # http.client, not urllib -- and the real connection, not a stub.
        module._connect = original_connect
        real = module._connect("app.memvara.dev", None, 1.0)
        self.assertIsInstance(real, http.client.HTTPSConnection)
        real.close()
        # Asked of the import graph rather than of the text. A substring scan for
        # "urlopen" fails on a module that merely explains why it does not use urlopen,
        # which is the first thing this one does.
        imported = set()
        for node in ast.walk(ast.parse((AUTH_SCRIPT).read_text(
                encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        self.assertIn("http.client", imported,
                      "the connection this module holds open must come from http.client")
        self.assertNotIn("urllib.request", imported)

        # ...and the CA bundle actually reaches that connection, rather than being built
        # by a function nobody calls.
        marker = ssl.create_default_context()
        module._context = lambda: marker
        built = []

        class _Recorder:
            def __init__(self, host, port=None, timeout=None, context=None):
                built.append({"host": host, "port": port, "timeout": timeout,
                              "context": context})

        module.http = types.SimpleNamespace(
            client=types.SimpleNamespace(HTTPSConnection=_Recorder,
                                         HTTPConnection=_Recorder))
        module._connect("app.memvara.dev", None, 9.5)
        self.assertIs(built[-1]["context"], marker,
                      "the connection was built without the CA bundle this module resolves")
        self.assertEqual(built[-1]["timeout"], 9.5,
                         "a connection with no timeout can hold a command open forever")

    def test_an_unrecognised_401_is_unknown_and_never_absent(self) -> None:
        """Telling a user with a bad key that they have no key sends them into a
        re-login that cannot fix it -- and they will run it twice before doubting the
        message. The classifier matches wording, so wording is the thing most likely to
        rot; the fallback has to be the state that still describes a credential.
        """
        module = self._auth()
        unworded = self._probe(module, whoami=(401, {"error": {
            "code": "unauthenticated",
            "message": "a refusal nobody here has written a branch for"}}))
        self.assertEqual(unworded["state"], "unknown")

        bodyless = self._probe(module, whoami=(401, {}))
        self.assertEqual(bodyless["state"], "unknown",
                         "a 401 with nothing readable in it is still a credential the "
                         "deployment refused, not a credential this machine lacks")

        for result in (unworded, bodyless):
            self.assertNotEqual(result["state"], "absent")
            self.assertEqual(result["source"], "MEMVARA_API_KEY",
                             "the key that was refused came from somewhere, and saying "
                             "where is how a user with two of them finds the wrong one")

    def test_the_probe_names_where_the_credential_came_from(self) -> None:
        """Three sources, three answers, and the key reported is the key that was sent.

        "Which credential is the host actually using" is unanswerable to a user holding
        an environment variable, a credentials file and a host config that disagree --
        and holding all three is the normal state of a machine that has been logged in
        twice.
        """
        module = self._auth()
        home = pathlib.Path(self._home)

        from_env = self._probe(module, whoami=(200, _WHOAMI_OK), key="mv_from_env")
        self.assertEqual(from_env["source"], "MEMVARA_API_KEY")

        (home / ".memvara").mkdir(parents=True, exist_ok=True)
        (home / ".memvara" / "credentials.json").write_text(
            json.dumps({"api_key": "mv_from_file"}), encoding="utf-8")
        (home / ".claude.json").write_text(json.dumps({"projects": {"/somewhere": {
            "mcpServers": {"memvara": {"type": "http", "url": HOSTED,
                                       "headers": {"Authorization":
                                                   "Bearer mv_from_host"}}}}}}),
            encoding="utf-8")

        # The environment still wins while it is set: a machine that sets both must not
        # reach a different store depending on which client happened to read it.
        still_env = self._probe(module, whoami=(200, _WHOAMI_OK), key="mv_from_env")
        self.assertEqual(still_env["source"], "MEMVARA_API_KEY")
        self.assertEqual(self.calls[-1][2], "mv_from_env")

        from_file = self._probe(module, whoami=(200, _WHOAMI_OK), key=None)
        self.assertIn("credentials.json", from_file["source"])
        self.assertEqual(self.calls[-1][2], "mv_from_file",
                         "the source named must be the source of the key that was sent")

        (home / ".memvara" / "credentials.json").unlink()
        from_host = self._probe(module, whoami=(200, _WHOAMI_OK), key=None)
        self.assertIn(".claude.json", from_host["source"],
                      "a key living only in the host's own MCP config is the one case a "
                      "user cannot inspect for themselves")
        self.assertEqual(self.calls[-1][2], "mv_from_host")

        self.assertEqual(
            len({from_env["source"], from_file["source"], from_host["source"]}), 3,
            "three sources that report the same string answer nobody's question")


#: `POST /api/auth/device/authorize` on the live endpoint, 2026-08-31. The opaque halves
#: are the only edit: the recorded `device_code` was a real secret and is replaced with a
#: string of the same shape, chosen to be distinctive enough that a scan for it means
#: something.
_AUTHORIZE = "/api/auth/device/authorize"
_TOKEN = "/api/auth/device/token"
_DEVICE_AUTHORIZED = {
    "device_code": "dev_2f1c9b8a7e6d5c4b3a29180706f5e4d3",
    "user_code": "AGKP-V5HT",
    "verification_uri": "https://app.memvara.dev/device",
    "verification_uri_complete": "https://app.memvara.dev/device?code=AGKP-V5HT",
    "expires_in": 900,
    "interval": 5,
}

#: `POST /api/auth/device/token` is the one route on that surface that does NOT use the
#: `{"error": {"code", "message", "detail"}}` envelope: RFC 8628 §3.5 fixes the shape a
#: device-flow client parses, and the API answers the RFC's shape rather than its own.
#: Four outcomes, all at HTTP 400, and `approved` at 200 in the ordinary envelope.
_TOKEN_PENDING = (400, {"error": "authorization_pending"})
_TOKEN_SLOW_DOWN = (400, {"error": "slow_down"})
_TOKEN_DENIED = (400, {"error": "access_denied"})
_TOKEN_APPROVED = (200, {"status": "approved", "api_key": "mv_recorded_plaintext",
                         "privilege": "write", "project": "recorded-project"})

#: Measured against `https://app.memvara.dev`, 2026-08-31, with `curl`. Both are the
#: object envelope -- only the token route breaks it -- and the two statuses are
#: different, which the plan this task came from did not say.
#:
#: A slug is refused by the route itself:
_AUTHORIZE_SLUG_REFUSED = (400, {"error": {
    "code": "bad_request",
    "message": "project must be a project id (uuid). A slug cannot be resolved here: "
               "this route has no session, and a slug is only unique within one "
               "organization.",
    "detail": None}})
#: ...and a request naming no project at all is refused by the schema, before the route
#: runs, because `project` is still a required field on a deployment that has not taken
#: the change letting the approver choose. **422, not 400.**
_AUTHORIZE_NO_PROJECT_REFUSED = (422, {"error": {
    "code": "invalid_request",
    "message": "the request path, body or query string does not match the schema. "
               "`detail.problems[].loc` names which of the three, and where in it",
    "detail": {"problems": [{"type": "missing", "loc": ["body", "project"],
                             "msg": "Field required", "input": {}}]}}})

#: A project id as the console shows it. Written out rather than generated: the whole
#: point of the argument check is that this exact shape is the only one accepted.
_PROJECT_ID = "3c04449a-3d99-4c0e-9f0a-1b2c3d4e5f60"


class DeviceFlow(unittest.TestCase):
    """RFC 8628 from the plugin, with no browser, no CLI and nothing from PyPI.

    Every expected value here is written out in this file rather than read from the
    module under test. A guard that asks the implementation what to expect shrinks
    exactly when the implementation shrinks, and passes under the sabotage it was written
    to catch -- which happened on this programme on 2026-08-30.
    """

    def setUp(self) -> None:
        self._env = {name: os.environ.get(name) for name in
                     ("HOME", "MEMVARA_API_KEY", "MEMVARA_SERVER_URL")}
        self._home = tempfile.mkdtemp(prefix="memvara-device-home-")
        os.environ["HOME"] = self._home
        os.environ.pop("MEMVARA_API_KEY", None)
        os.environ.pop("MEMVARA_SERVER_URL", None)
        self.calls: "list[tuple[str, str, object, str | None]]" = []
        self.slept: "list[float]" = []
        self.opened: "list[str]" = []

    def tearDown(self) -> None:
        for name, value in self._env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(self._home, ignore_errors=True)

    @property
    def home(self) -> pathlib.Path:
        return pathlib.Path(self._home)

    def _auth(self):
        """The module, with its clock and its browser replaced.

        Both replacements are safety, not convenience. A real `time.sleep` would make a
        poll test wait the interval the server asks for, five seconds at a time; a real
        `webbrowser.open` would open a browser window on whoever ran the suite.
        """
        import importlib.util

        spec = importlib.util.spec_from_file_location("memvara_auth", AUTH_SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.addCleanup(module.close)

        clock = {"now": 0.0}

        def _sleep(seconds):
            self.slept.append(float(seconds))
            clock["now"] += float(seconds)
            # A poll loop with no deadline does not fail this suite, it hangs it -- which
            # is how a broken bound reaches CI as a stuck job rather than a red one.
            # Measured: sabotaging the deadline away wedged the runner for ten minutes.
            # An hour of simulated time is four times the ceiling any grant may have.
            if clock["now"] > 3600.0:
                raise AssertionError(
                    "the poll loop passed an hour of simulated time without stopping; "
                    "it has no working bound")

        self.addCleanup(setattr, module, "time", module.time)
        module.time = types.SimpleNamespace(sleep=_sleep,
                                            monotonic=lambda: clock["now"])
        self.addCleanup(setattr, module, "webbrowser", module.webbrowser)
        module.webbrowser = types.SimpleNamespace(open=self.opened.append)
        return module

    def _replies(self, module, script) -> None:
        """Replace the one network primitive with recorded replies, keyed by path.

        A value may be one `(status, body)` pair, a list of them consumed in order with
        the last repeating, or an exception to raise. A path the script does not name
        fails the test by name rather than by `KeyError`: "the flow called something it
        should not have" is the finding, and it should read like one.
        """
        original = module.request
        self.addCleanup(setattr, module, "request", original)
        queues = {path: (list(reply) if isinstance(reply, list) else [reply])
                  for path, reply in script.items()}
        calls = self.calls

        def fake(method, path, *, body=None, auth=None, timeout=10.0):
            calls.append((method, path, body, auth))
            if path not in queues:
                raise AssertionError(
                    f"the flow called {method} {path}, which this test did not expect it "
                    "to call at all")
            queue = queues[path]
            reply = queue[0] if len(queue) == 1 else queue.pop(0)
            if isinstance(reply, BaseException):
                raise reply
            return reply

        module.request = fake

    def _files_written(self) -> "list[str]":
        return sorted(str(path.relative_to(self.home))
                      for path in self.home.rglob("*") if path.is_file())

    def test_the_device_code_never_reaches_stdout_or_a_log(self) -> None:
        """`device_code` is the one secret this flow's unauthenticated half ever holds,
        and the API returns it exactly once. `user_code` is the half meant for a human:
        it is shown, typed, and pre-filled into a URL on purpose.

        Both halves are asserted. A test that only checks the secret is absent passes on
        a flow that prints nothing at all -- and a device flow that prints nothing is
        unusable on the headless machine it exists for, so silence must fail here too.

        Every byte the run emitted is scanned: standard output, the URL handed to the
        browser, and every file left under `$HOME` afterwards.
        """
        module = self._auth()
        secret = _DEVICE_AUTHORIZED["device_code"]
        out = io.StringIO()
        self._replies(module, {_AUTHORIZE: (201, _DEVICE_AUTHORIZED),
                               _TOKEN: [_TOKEN_PENDING, _TOKEN_APPROVED]})

        self.assertEqual(module.authenticate(out=out), 0,
                         "an approved grant is a successful login")

        printed = out.getvalue()
        self.assertNotIn(secret, printed, "the device code was printed to the terminal")
        self.assertIn("AGKP-V5HT", printed,
                      "the user code is what the person at the keyboard needs, and a "
                      "flow that does not show it cannot be completed by hand")
        self.assertIn("https://app.memvara.dev/device?code=AGKP-V5HT", printed,
                      "the verification URI must be printed as well as opened -- a "
                      "headless machine has no browser to open it in")

        self.assertEqual(self.opened,
                         ["https://app.memvara.dev/device?code=AGKP-V5HT"],
                         "the browser is handed the verification URI, and only that")
        for url in self.opened:
            self.assertNotIn(secret, url)

        self.assertEqual(self._files_written(), [".memvara/credentials.json"],
                         "this flow writes exactly one file and leaves no temporary "
                         "behind; anything else here is a second writer or a leak")
        written = (self.home / ".memvara" / "credentials.json")
        text = written.read_text(encoding="utf-8")
        self.assertNotIn(secret, text, "the device code was written to disk")
        self.assertIn("mv_recorded_plaintext", text,
                      "the minted key is the thing this file exists to hold")
        self.assertEqual(oct(written.stat().st_mode & 0o777), "0o600",
                         "a credential readable by anyone on the machine is a credential "
                         "shared with everyone on it")

    def test_a_slow_down_response_widens_the_interval(self) -> None:
        """RFC 8628 §3.5: `slow_down` means wait longer, by at least five seconds, and
        keep waiting longer. A client that treats it as one more `authorization_pending`
        polls at the same rate into a bucket that is already refusing it, and the flow
        never completes for a reason the user cannot see.

        The interval the server names is honoured too, and is checked with a grant whose
        interval is *not* the default -- otherwise an implementation that ignores the
        field entirely and hard-codes five seconds passes.
        """
        module = self._auth()
        self._replies(module, {_TOKEN: [_TOKEN_PENDING, _TOKEN_PENDING, _TOKEN_SLOW_DOWN,
                                        _TOKEN_PENDING, _TOKEN_APPROVED]})

        minted = module.poll(_DEVICE_AUTHORIZED)
        self.assertEqual(minted["api_key"], "mv_recorded_plaintext")

        self.assertEqual(len(self.slept), 5,
                         f"one wait per poll, five polls: {self.slept}")
        self.assertEqual(self.slept[:3], [5.0, 5.0, 5.0],
                         "the grant asked for five seconds and was answered normally "
                         "three times")
        self.assertGreaterEqual(
            self.slept[3], self.slept[2] + 5.0,
            "RFC 8628 §3.5 widens the interval by at least five seconds on slow_down; "
            f"this client went from {self.slept[2]} to {self.slept[3]}")
        self.assertEqual(self.slept[4], self.slept[3],
                         "the widened interval is the new interval, not a single longer "
                         "wait that then springs back")

        self.slept.clear()
        self.calls.clear()
        self._replies(module, {_TOKEN: [_TOKEN_PENDING, _TOKEN_APPROVED]})
        module.poll(dict(_DEVICE_AUTHORIZED, interval=7))
        self.assertEqual(self.slept, [7.0, 7.0],
                         "the server names the interval and this client waited "
                         f"{self.slept} instead")

    def test_a_poll_that_nobody_approves_stops_rather_than_running_forever(self) -> None:
        """A grant has a life and the loop must not outlast it. Two bounds, because
        either alone leaves a hole: the grant's own `expires_in`, and a ceiling of 900
        seconds for a deployment that answers with a life longer than any human will wait
        at a terminal.

        A denial stops immediately and says so, rather than being retried until the
        deadline -- `access_denied` is a one-way door on the server, so polling it again
        can only ever produce the same word.
        """
        module = self._auth()

        self._replies(module, {_TOKEN: _TOKEN_PENDING})
        with self.assertRaises(module.AuthError) as short:
            module.poll(dict(_DEVICE_AUTHORIZED, expires_in=30))
        self.assertLessEqual(sum(self.slept), 35.0,
                             f"a 30-second grant polled for {sum(self.slept)} seconds")
        self.assertTrue(str(short.exception).strip(), "a timeout owes the user a sentence")

        self.slept.clear()
        self._replies(module, {_TOKEN: _TOKEN_PENDING})
        with self.assertRaises(module.AuthError):
            module.poll(dict(_DEVICE_AUTHORIZED, expires_in=999999))
        self.assertLessEqual(sum(self.slept), 905.0,
                             "a deployment naming an absurd lifetime must not hold the "
                             f"terminal for {sum(self.slept)} seconds")

        self.slept.clear()
        self.calls.clear()
        self._replies(module, {_TOKEN: [_TOKEN_PENDING, _TOKEN_DENIED, _TOKEN_APPROVED]})
        with self.assertRaises(module.AuthError) as denied:
            module.poll(_DEVICE_AUTHORIZED)
        self.assertEqual(len(self.calls), 2,
                         "a denied grant was polled again after being denied")
        self.assertIn("deni", str(denied.exception).lower(),
                      "a denial must say it was denied, not time out silently: "
                      f"{denied.exception}")

    def test_the_flow_imports_nothing_outside_the_standard_library(self) -> None:
        """The plugin installs with no pip step and must keep doing so. `certifi` is the
        one exception and is optional: python.org's macOS build loads no roots at all, so
        `certifi` is used when it is importable and the default context otherwise --
        which means its import has to live inside a `try`, or the module it rescues stops
        importing on every machine that lacks it.

        Asked of the import graph rather than of the text. A substring scan for a package
        name matches this module's own prose explaining why it does not use that package,
        which is the first thing the file does; that mistake was made once already on
        this programme, on 2026-08-30.

        Both halves. The set of non-stdlib imports being empty is satisfied by a file
        with no imports in it at all, so the modules this flow actually needs are named
        and required to be present.
        """
        tree = ast.parse((AUTH_SCRIPT).read_text(encoding="utf-8"))
        imported: "set[str]" = set()
        guarded: "set[str]" = set()

        def _names(node) -> "set[str]":
            if isinstance(node, ast.Import):
                return {alias.name for alias in node.names}
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                return {node.module}
            return set()

        for node in ast.walk(tree):
            imported |= _names(node)
        for node in ast.walk(tree):
            if isinstance(node, ast.Try):
                for inner in ast.walk(node):
                    if inner is not node:
                        guarded |= _names(inner)

        roots = {name.split(".")[0] for name in imported}
        outside = roots - set(sys.stdlib_module_names) - {"certifi"}
        self.assertEqual(
            outside, set(),
            f"this module must import nothing but the standard library: {sorted(outside)}")

        for needed in ("http.client", "json", "os", "ssl", "tempfile", "time",
                       "webbrowser"):
            self.assertIn(needed, imported,
                          f"{needed} is what this flow runs on; a file that has stopped "
                          "importing it has stopped doing the thing it is imported for")
        self.assertNotIn("urllib.request", imported,
                         "urlopen cannot hold a connection open, and this flow makes one "
                         "call every five seconds for up to fifteen minutes")

        self.assertIn("certifi", roots,
                      "certifi is what makes verification work on python.org's macOS "
                      "build, where the default context loads zero roots")
        self.assertIn("certifi", {name.split(".")[0] for name in guarded},
                      "certifi is imported outside a try, so this module now stops "
                      "importing on any machine that does not have it -- which is every "
                      "machine that installed this plugin without a pip step")

    def test_an_undeployed_deployment_names_the_project_id_form(self) -> None:
        """A deployment that has not taken the change letting the approver choose the
        project refuses a request that names none. It does so two different ways --
        measured live, 2026-08-31 -- and the command has to survive both:

            400  the route's own refusal, `project must be a project id (uuid)`
            422  the schema's, because `project` is still a required field

        Either way the user needs the same sentence, and it has to name the form that
        works today rather than quoting a validation error at them. This check is
        permanent, not scaffolding: a self-hosted deployment can lag the console forever.

        The last third is the half that keeps the message honest -- an unrelated refusal
        must NOT claim the project-id form fixes it, or the advice is noise on every
        outage.
        """
        module = self._auth()
        for label, reply in (("422 from the schema", _AUTHORIZE_NO_PROJECT_REFUSED),
                             ("400 from the route", _AUTHORIZE_SLUG_REFUSED)):
            with self.subTest(refusal=label):
                out = io.StringIO()
                self._replies(module, {_AUTHORIZE: reply})
                code = module.authenticate(out=out)
                text = out.getvalue()
                self.assertNotEqual(code, 0, "a refused login is not a successful one")
                # What is pinned is that the user is told to supply a project id and shown
                # the shape of one -- not the syntax of any host's command line. This module
                # is vendored unchanged into the other plugin repos, where `/memvara ...`
                # is spelled differently or not at all, so a message quoting one client's
                # form is wrong everywhere else and right nowhere for long.
                self.assertIn("project id", text,
                              f"a {label} left the user with nothing to do next: {text}")
                self.assertIn(module.PROJECT_ID_EXAMPLE, text,
                              f"a {label} named no project id shape, so 'supply a project "
                              f"id' is advice the user cannot act on: {text}")
                self.assertNotIn("Traceback", text)
                self.assertEqual(self._files_written(), [],
                                 "a login that never happened wrote something")

        out = io.StringIO()
        self._replies(module, {_AUTHORIZE: (503, {"error": {
            "code": "unavailable", "message": "the store is not accepting writes"}})})
        self.assertNotEqual(module.authenticate(out=out), 0)
        self.assertNotIn("<project-id>", out.getvalue(),
                         "a deployment that is merely down was told to name a project "
                         "id, which will not help and will be tried anyway")

    def test_a_project_argument_must_be_a_dashed_uuid(self) -> None:
        """The endpoint refuses anything else with a 400 whose text a user cannot act on,
        so the refusal happens here, locally, naming the form that works.

        A tenant id -- `prj_` and thirty-two hex characters -- is close enough to a UUID
        to be pasted by mistake, and reshaping one into a UUID does work. It is refused
        anyway: guessing at an id format on someone's behalf turns a clear 400 into a
        credential minted against the wrong project, and that is not an error anyone ever
        sees.

        The accepted half matters as much: a well-formed id is sent through untouched,
        because normalising it here would be the same guess in a smaller coat.
        """
        module = self._auth()
        for bad in ("prj_3c04449a3d994c0e9f0a1b2c3d4e5f60",
                    "3c04449a3d994c0e9f0a1b2c3d4e5f60",
                    "my-project",
                    "3c04449a-3d99-4c0e-9f0a-1b2c3d4e5f6",
                    ""):
            with self.subTest(project=bad):
                self.calls = []
                out = io.StringIO()
                self._replies(module, {})
                code = module.authenticate(bad, out=out)
                text = out.getvalue()
                self.assertNotEqual(code, 0, f"{bad!r} was accepted as a project id")
                self.assertEqual(self.calls, [],
                                 "the argument was sent to the endpoint to be refused "
                                 "there, which spends a round trip to say the same thing "
                                 "less clearly")
                self.assertRegex(
                    text,
                    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                    r"-[0-9a-fA-F]{12}",
                    "the refusal must show the form that works, not merely say no: "
                    f"{text}")
                self.assertEqual(self._files_written(), [])

        self.calls = []
        self._replies(module, {_AUTHORIZE: (201, _DEVICE_AUTHORIZED),
                               _TOKEN: _TOKEN_APPROVED})
        self.assertEqual(module.authenticate(_PROJECT_ID, out=io.StringIO()), 0)
        bodies = [body for _method, path, body, _auth in self.calls if path == _AUTHORIZE]
        self.assertEqual(bodies, [{"project": _PROJECT_ID}],
                         "a well-formed project id must reach the endpoint exactly as "
                         f"the user typed it: {bodies}")

        self.calls = []
        self._replies(module, {_AUTHORIZE: (201, _DEVICE_AUTHORIZED),
                               _TOKEN: _TOKEN_APPROVED})
        self.assertEqual(module.authenticate(out=io.StringIO()), 0)
        bodies = [body for _method, path, body, _auth in self.calls if path == _AUTHORIZE]
        self.assertEqual(bodies, [{}],
                         "with no argument the request must carry no project at all, so "
                         "the approver is the one who chooses it")

    def test_a_crash_mid_write_cannot_truncate_a_working_credential(self) -> None:
        """The file being replaced is the only copy of a key the API will never show
        again. A plain `open(path, "w")` truncates it on the first byte, so a failure
        anywhere after that leaves the machine holding half a credential and no way back.

        Written to a temporary file and moved into place instead, which the filesystem
        does atomically: either the old key or the new one, never neither.
        """
        module = self._auth()
        credentials = self.home / ".memvara" / "credentials.json"
        credentials.parent.mkdir(parents=True, exist_ok=True)
        credentials.write_text(json.dumps({"api_key": "mv_the_one_that_works"}),
                               encoding="utf-8")
        os.chmod(credentials, 0o600)

        def _explode(*_args, **_kwargs):
            raise RuntimeError("the disk went away mid-write")

        # Replaced on the module rather than in `json` itself, so nothing else running in
        # this process is affected -- and the failure lands where a real one would, after
        # the destination has been opened and before anything has been committed to it.
        real_json = module.json
        self.addCleanup(setattr, module, "json", real_json)
        module.json = types.SimpleNamespace(
            dump=_explode, dumps=json.dumps, load=json.load, loads=json.loads)
        with self.assertRaises(RuntimeError):
            module.write_credentials({"api_key": "mv_the_new_one"})
        module.json = real_json

        # Read as bytes before parsing: a truncated file raises a JSON error, and an
        # error is a worse finding than a sentence saying the key is gone.
        survived = credentials.read_text(encoding="utf-8")
        self.assertTrue(
            survived.strip(),
            "the working credential was truncated by a write that then failed, so this "
            "machine now holds neither the old key nor the new one")
        self.assertEqual(
            json.loads(survived)["api_key"], "mv_the_one_that_works",
            "the working credential was overwritten by a write that then failed")
        self.assertEqual(self._files_written(), [".memvara/credentials.json"],
                         "the failed write left a temporary file behind")

        path = module.write_credentials({"api_key": "mv_the_new_one"})
        self.assertEqual(pathlib.Path(path), credentials)
        self.assertEqual(
            json.loads(credentials.read_text(encoding="utf-8"))["api_key"],
            "mv_the_new_one")
        self.assertEqual(oct(credentials.stat().st_mode & 0o777), "0o600")
        self.assertEqual(self._files_written(), [".memvara/credentials.json"])


#: Recorded from `GET /v1/stats` on the live deployment, 2026-08-31. The tenant is the
#: only edit -- it is an opaque identifier, nothing here reads it, and it is a real
#: project's. The shape is the point: `stats` reports these fields or it reports nothing
#: a user could not already get from `whoami`.
_STATS_OK = {
    "scope": {"tenant": "prj_recorded", "user": None, "agent": None, "session": None},
    "visible": 611,
    "tenant_counts": {"episodes": 1082, "claims": 716, "live_claims": 611,
                      "ended_claims": 82, "invalidated": 23, "embeddings": 1798,
                      "joinable_claims": 3},
    "extractor": "fast-path-only",
    "read_only": False,
}
_STATS_PATH = "/v1/stats"

#: The four commands, written out here rather than read from the manifest or globbed from
#: the tree. Both of those are the things under test, and a guard that asks them what to
#: expect agrees with them however wrong they get.
_COMMAND_NAMES = ("authenticate", "login", "logout", "stats")
_COMMANDS_DIR = PLUGIN / "commands"

#: A host configuration shaped the way a client writes one: the memvara server block is
#: nested per checkout under `projects`, not at the top level. Written with a key in it
#: because a file with no credential in it is not the file this guard is about.
#:
#: `.claude.json` rather than anything of Grok's, deliberately. `auth/memvara_auth.py` is
#: vendored byte-for-byte from `memvara/claude-memvara` and its `HOST_CONFIGS` names the
#: three Claude-family paths; Grok reads `~/.claude/settings.json` itself, so two of the
#: three are live here. Grok's own `~/.grok/config.toml` is TOML and the module's reader
#: is `json.load`, so a key living only there is invisible to it -- a host difference
#: recorded in the report for this change, not a reason to fork the module.
_HOST_CONFIG_NAME = ".claude.json"
_HOST_CONFIG = {
    "numStartups": 41,
    "projects": {
        "/a/checkout": {
            "mcpServers": {
                "memvara": {
                    "type": "http",
                    "url": "https://app.memvara.dev/mcp",
                    "headers": {"Authorization": "Bearer mv_the_host_wrote_this"},
                }
            }
        }
    },
}


class Commands(unittest.TestCase):
    """Four commands, and the two things among them that cannot be taken back.

    A minted key is returned exactly once, so replacing one is destroying one, and
    deleting the local copy leaves a live write-capable credential on the deployment with
    nothing on this machine pointing at it. Both acts have to be things the user asked
    for in the moment rather than things a command did on the way to something else.

    Every expected value here is written out in this file rather than read from the
    manifest, the tree, or the module under test. All three are what is being checked.
    """

    def setUp(self) -> None:
        self._env = {name: os.environ.get(name) for name in
                     ("HOME", "MEMVARA_API_KEY", "MEMVARA_SERVER_URL")}
        self._home = tempfile.mkdtemp(prefix="memvara-commands-home-")
        os.environ["HOME"] = self._home
        os.environ.pop("MEMVARA_API_KEY", None)
        os.environ.pop("MEMVARA_SERVER_URL", None)
        self.calls: "list[tuple[str, str, object, str | None]]" = []
        self.slept: "list[float]" = []
        self.opened: "list[str]" = []

    def tearDown(self) -> None:
        for name, value in self._env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(self._home, ignore_errors=True)

    @property
    def home(self) -> pathlib.Path:
        return pathlib.Path(self._home)

    def _auth(self):
        """The module, with its clock and its browser replaced -- see `DeviceFlow._auth`
        for why both replacements are safety rather than convenience."""
        import importlib.util

        spec = importlib.util.spec_from_file_location("memvara_auth", AUTH_SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.addCleanup(module.close)

        clock = {"now": 0.0}

        def _sleep(seconds):
            self.slept.append(float(seconds))
            clock["now"] += float(seconds)
            if clock["now"] > 3600.0:
                raise AssertionError(
                    "the poll loop passed an hour of simulated time without stopping; "
                    "it has no working bound")

        self.addCleanup(setattr, module, "time", module.time)
        module.time = types.SimpleNamespace(sleep=_sleep,
                                            monotonic=lambda: clock["now"])
        self.addCleanup(setattr, module, "webbrowser", module.webbrowser)
        module.webbrowser = types.SimpleNamespace(open=self.opened.append)
        return module

    def _replies(self, module, script) -> None:
        """Recorded replies keyed by path; a path the script does not name fails by name.

        "the command called something it should not have" is the finding in half the
        tests below, and it should read like one rather than like a `KeyError`.
        """
        original = module.request
        self.addCleanup(setattr, module, "request", original)
        queues = {path: (list(reply) if isinstance(reply, list) else [reply])
                  for path, reply in script.items()}
        calls = self.calls

        def fake(method, path, *, body=None, auth=None, timeout=10.0):
            calls.append((method, path, body, auth))
            if path not in queues:
                raise AssertionError(
                    f"the command called {method} {path}, which this test did not expect "
                    "it to call at all")
            queue = queues[path]
            reply = queue[0] if len(queue) == 1 else queue.pop(0)
            if isinstance(reply, BaseException):
                raise reply
            return reply

        module.request = fake

    def _paths(self) -> "list[str]":
        return [path for _method, path, _body, _auth in self.calls]

    def _snapshot(self) -> "dict[str, bytes]":
        return {str(path.relative_to(self.home)): path.read_bytes()
                for path in self.home.rglob("*") if path.is_file()}

    def _credential_file(self, key: str = "mv_local") -> pathlib.Path:
        path = self.home / ".memvara" / "credentials.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"api_key": key}), encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    def test_the_manifest_declares_command_directories_not_command_files(self) -> None:
        """Grok's `commands` field names directories to scan, not files to load.

        Declaring the four `.md` paths installed cleanly and reported "4 command dir(s)" --
        it counted the entries, and each one happened to be a file it then found nothing
        inside. `grok plugin details` said four, `grok inspect` listed no commands at all,
        and typing `/memvara:stats` reached the MCP tool instead of the script, which
        answers the same question well enough that nothing looked wrong.

        So this asserts the shape the host actually scans: every entry resolves to a
        directory that exists and holds at least one `.md`. A file passes the
        "points at something that exists" test above and still ships four commands
        nobody can invoke.
        """
        for manifest in (ROOT / "plugin.json", PLUGIN / "plugin.json"):
            declared = json.loads(manifest.read_text(encoding="utf-8")).get("commands")
            self.assertTrue(declared, f"{manifest.name} declares no commands")
            for entry in declared:
                where = (manifest.parent / entry).resolve()
                with self.subTest(manifest=manifest.name, entry=entry):
                    self.assertTrue(
                        where.is_dir(),
                        f"{manifest.name} declares {entry!r}, which is not a directory. "
                        "Grok scans these paths for command files; naming a file installs "
                        "cleanly and registers nothing.")
                    self.assertTrue(
                        list(where.glob("*.md")),
                        f"{entry!r} is a directory with no command files in it")

    def test_every_command_this_plugin_declares_points_at_a_file_that_exists(self) -> None:
        """Non-empty as well as resolvable, and checked in both directions.

        Non-empty because an emptied `commands/` directory otherwise satisfies "every
        declared command exists" perfectly, and a guard a deletion satisfies has stopped
        guarding. Both directions because declaring `commands` in `plugin.json` *replaces*
        the default directory scan rather than adding to it -- so a file added to the
        tree and not added to the list is a command the host never loads, and nothing
        about the repository looks wrong.

        The four names are asserted as literals. "whatever is in the tree matches whatever
        is in the manifest" is satisfied by a plugin that ships two commands, or four
        different ones.
        """
        manifest = _json(PLUGIN / "plugin.json")
        declared = manifest.get("commands")
        self.assertIsInstance(
            declared, list,
            "plugin.json must declare its commands as a list of paths; without the "
            "field the host scans commands/ by convention, which is a different promise "
            "and one this repository does not otherwise rely on")
        self.assertTrue(declared, "a plugin that declares no commands ships no commands")

        # Directories, not files. This assertion used to require a file, which is what
        # let four `.md` paths ship: `grok plugin install` accepted them and reported
        # "4 command dir(s)" -- it counted the entries and scanned each as a directory,
        # finding nothing in any of them. Every command was unreachable, and the only
        # symptom was `/memvara:stats` quietly reaching the MCP tool instead, which
        # answers a similar enough question that nothing looked wrong.
        resolved: "dict[str, pathlib.Path]" = {}
        for entry in declared:
            self.assertTrue(str(entry).startswith("./"),
                            f"{entry!r} must be relative to the plugin root and begin "
                            "'./', which is the only form the host resolves")
            where = PLUGIN / str(entry)[2:]
            self.assertTrue(where.is_dir(),
                            f"{entry} is declared and is not a directory; the host scans "
                            "these paths for command files, so naming a file registers "
                            "nothing and says nothing")
            for path in where.glob("*.md"):
                resolved[path.stem] = path

        self.assertEqual(sorted(resolved), sorted(_COMMAND_NAMES),
                         "these four commands are what this plugin is for; the manifest "
                         f"declares {sorted(resolved)}")
        self.assertEqual(sorted(path.stem for path in _COMMANDS_DIR.glob("*.md")),
                         sorted(_COMMAND_NAMES),
                         "the tree and the manifest disagree, and the manifest is what "
                         "the host reads -- a file here that nobody declared is a "
                         "command that silently does not exist")

        for name, path in sorted(resolved.items()):
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("---\n"),
                            f"{name} has no frontmatter block")
            front = text.split("---", 2)[1]
            self.assertRegex(front, r"(?m)^description:[ \t]*\S",
                             f"{name} states no description, so it is a blank line in "
                             "the picker and Grok has nothing to decide from")

    def test_every_command_reaches_the_module_through_a_placeholder_this_host_expands(
            self) -> None:
        """The one thing in this port that could not be copied, and the one that fails
        without saying anything.

        `claude-memvara`'s command files spell the plugin's own directory
        `${CLAUDE_PLUGIN_ROOT}`. Measured against grok 1.0.13 on 2026-08-31, by running a
        flat `commands/*.md` file and printing what arrived: this host substitutes
        `$ARGUMENTS`, `${SKILL_DIR}` and `${CLAUDE_SKILL_DIR}` into a command body and
        leaves `${CLAUDE_PLUGIN_ROOT}` and `${GROK_PLUGIN_ROOT}` as literal text. A copied
        command file therefore hands the shell `python3 "/auth/memvara_auth.py"`, because
        an unset variable expands to nothing -- an absolute path to a file that has never
        existed, on every machine, with the plugin sitting correctly on disk beside it.

        `${SKILL_DIR}` arrived as the directory holding the command file, so the module is
        one level up from it. That is asserted here by RESOLVING the path each command
        actually writes and requiring the file to be there, rather than by matching the
        string: a guard that checks the spelling agrees with a command that spells a
        working placeholder into the wrong directory.
        """
        placeholder = "${SKILL_DIR}"
        for name in _COMMAND_NAMES:
            with self.subTest(command=name):
                text = (_COMMANDS_DIR / f"{name}.md").read_text(encoding="utf-8")
                self.assertNotIn(
                    "PLUGIN_ROOT", text,
                    f"{name} names a plugin-root variable, which this host does not "
                    "substitute into a command body; it reaches the shell as literal "
                    "text and expands to nothing")
                invocations = [line for line in text.splitlines()
                               if placeholder in line and "memvara_auth.py" in line]
                self.assertTrue(
                    invocations,
                    f"{name} never invokes the auth module through {placeholder}, so "
                    "there is nothing here for the host to point at the plugin")
                for line in invocations:
                    quoted = line.split(placeholder, 1)[1]
                    suffix = quoted.split('"', 1)[0]
                    resolved = (_COMMANDS_DIR / suffix.lstrip("/")).resolve()
                    self.assertEqual(
                        resolved, (AUTH_SCRIPT).resolve(),
                        f"{name} resolves {placeholder}{suffix} to {resolved}, and the "
                        "module is not there")

    def test_the_module_ships_exactly_once(self) -> None:
        """One copy, and it is the one the library vendors.

        For the length of one commit this plugin held both `plugin/auth/memvara_auth.py`
        and `plugin/skills/memvara/scripts/memvara_auth.py`, byte for byte the same file,
        with nothing saying which the commands ran or which a fix should edit. Two copies
        that agree are fine right up until somebody edits one.

        An exact set rather than a count, so the wrong single copy fails as loudly as two.
        """
        copies = sorted(str(path.relative_to(PLUGIN))
                        for path in PLUGIN.rglob("memvara_auth.py")
                        if "__pycache__" not in path.parts)
        self.assertEqual(
            copies, [str(AUTH_SCRIPT.relative_to(PLUGIN))],
            "the auth module must ship once, inside the skill, where skill.lock vendors "
            "it to every host through the sync that already exists")

    def test_no_host_config_is_written_without_confirmation(self) -> None:
        """This host's own OAuth client already writes `~/.claude.json`. A command that
        writes there unprompted is a second writer to one file with nobody able to say
        whose token is live -- measured 2026-08-30, when exactly that ambiguity cost an
        evening.

        Every command is driven, including the two that do write, and every byte under
        `$HOME` is compared before and after. `~/.memvara/credentials.json` is the one
        file allowed to differ; anything else changing is the failure, whether it is a
        host configuration or something nobody thought of.

        The positive half is the last assertion: `logout` must *name* the host
        configuration that still holds a key. Silence about it satisfies "wrote nothing"
        and leaves the user believing they logged out.
        """
        module = self._auth()
        host = self.home / _HOST_CONFIG_NAME
        host.write_text(json.dumps(_HOST_CONFIG, indent=2), encoding="utf-8")
        self._credential_file()
        before = self._snapshot()
        self.assertIn(_HOST_CONFIG_NAME, before)

        flow = {"/v1/health": (200, _HEALTH_OK),
                "/v1/whoami": (401, _REFUSED_UNKNOWN),
                _AUTHORIZE: (201, _DEVICE_AUTHORIZED),
                _TOKEN: _TOKEN_APPROVED}
        working = {"/v1/health": (200, _HEALTH_OK), "/v1/whoami": (200, _WHOAMI_OK),
                   _STATS_PATH: (200, _STATS_OK)}

        logout = io.StringIO()
        runs = (
            (["authenticate"], flow, io.StringIO()),
            (["authenticate"], working, io.StringIO()),
            (["login"], working, io.StringIO()),
            (["login", "--confirm"], dict(working, **{_AUTHORIZE: (201, _DEVICE_AUTHORIZED),
                                                      _TOKEN: _TOKEN_APPROVED}),
             io.StringIO()),
            (["stats"], working, io.StringIO()),
            (["logout"], {}, logout),
        )
        for argv, script, out in runs:
            with self.subTest(command=" ".join(argv)):
                self.calls = []
                self._replies(module, script)
                module.main(list(argv), out=out)
                after = self._snapshot()
                for name, content in before.items():
                    if name == ".memvara/credentials.json":
                        continue
                    self.assertEqual(
                        after.get(name), content,
                        f"{' '.join(argv)} changed {name}, which it does not own")
                new = set(after) - set(before)
                self.assertFalse(
                    new - {".memvara/credentials.json"},
                    f"{' '.join(argv)} created {sorted(new)} under $HOME")

        self.assertIn(str(host), logout.getvalue(),
                      "logout said nothing about the host configuration that still "
                      "holds a memvara key, so the user believes they logged out")

    def test_logout_says_the_key_still_exists_until_it_is_revoked(self) -> None:
        """Deleting the local file is not revocation. A user who believes otherwise
        leaves a live write-capable credential behind, on a deployment they have stopped
        looking at.

        Both halves. The act must actually happen -- a command that says the right
        sentence and deletes nothing passes a scan for the sentence -- and the sentence
        must not be said when there was nothing to delete, or it becomes a line that
        appears whether or not there is a key it could be true of.
        """
        module = self._auth()
        credentials = self._credential_file()
        out = io.StringIO()
        self.assertEqual(module.main(["logout"], out=out), 0)
        text = out.getvalue()

        self.assertFalse(credentials.exists(),
                         "logout printed its message and left the credential in place")
        self.assertIn(str(credentials), text,
                      "logout must name the file it deleted; 'logged out' names nothing "
                      "a user can check")
        said = [line for line in text.splitlines() if "revoke" in line.lower()]
        self.assertTrue(said, f"logout never mentions revocation at all: {text!r}")
        self.assertTrue(
            any("still" in line.lower() for line in said),
            "logout names revocation without saying the key still works until then, "
            f"which reads as a claim that it was revoked: {said}")

        empty = io.StringIO()
        module.main(["logout"], out=empty)
        self.assertNotIn("revoke", empty.getvalue().lower(),
                         "logout told a machine holding no credential that its key "
                         "still works, which is a sentence about nothing")

    def test_authenticate_does_not_start_a_device_flow_when_the_credential_already_works(
            self) -> None:
        """The no-op is what makes the command safe to run when unsure.

        Without it `authenticate` mints a second key every time somebody wonders whether
        they are logged in, and the first one is still live on the deployment with
        nothing on this machine pointing at it.

        The second half is what stops the guard passing on a command that never does
        anything: with no credential, the flow must run.
        """
        module = self._auth()
        os.environ["MEMVARA_API_KEY"] = "mv_working"
        self._replies(module, {"/v1/health": (200, _HEALTH_OK),
                               "/v1/whoami": (200, _WHOAMI_OK)})
        out = io.StringIO()
        self.assertEqual(module.main(["authenticate"], out=out), 0,
                         "a machine that is already authenticated is not an error")
        self.assertNotIn(_AUTHORIZE, self._paths(),
                         "authenticate started a device flow against a credential that "
                         f"works: {self._paths()}")
        self.assertEqual(self.opened, [], "a browser was opened for a login nobody needed")
        self.assertEqual(self._snapshot(), {},
                         "authenticate wrote a file without minting anything")
        self.assertIn("write", out.getvalue(),
                      "the no-op must still report what the live credential is, or it "
                      "is indistinguishable from a command that did nothing")

        os.environ.pop("MEMVARA_API_KEY")
        self.calls = []
        self._replies(module, {"/v1/health": (200, _HEALTH_OK),
                               _AUTHORIZE: (201, _DEVICE_AUTHORIZED),
                               _TOKEN: _TOKEN_APPROVED})
        self.assertEqual(module.main(["authenticate"], out=io.StringIO()), 0)
        self.assertIn(_AUTHORIZE, self._paths(),
                      "with no credential on the machine, authenticate has to run the "
                      f"flow: {self._paths()}")

    def test_login_will_not_replace_a_working_credential_without_confirmation(self) -> None:
        """`login` exists to replace a credential, so the confirmation is the whole of
        its safety: the key it would overwrite was returned exactly once and this machine
        holds the only copy.

        Refusing is not the behaviour being bought -- the confirmed run must go through,
        or the command is broken in the more expensive direction.
        """
        module = self._auth()
        os.environ["MEMVARA_API_KEY"] = "mv_working"
        self._replies(module, {"/v1/health": (200, _HEALTH_OK),
                               "/v1/whoami": (200, _WHOAMI_OK)})
        out = io.StringIO()
        code = module.main(["login"], out=out)
        self.assertNotEqual(code, 0,
                            "an unconfirmed login that reports success is one the caller "
                            "cannot tell from a completed one")
        self.assertNotIn(_AUTHORIZE, self._paths(),
                         "login began replacing a working credential before anybody "
                         "agreed to it")
        self.assertEqual(self._snapshot(), {}, "the refused login wrote a file anyway")
        self.assertIn("write", out.getvalue(),
                      "the refusal must show what it declined to replace, or the user "
                      "is asked to confirm something they cannot see")

        self.calls = []
        self._replies(module, {"/v1/health": (200, _HEALTH_OK),
                               "/v1/whoami": (200, _WHOAMI_OK),
                               _AUTHORIZE: (201, _DEVICE_AUTHORIZED),
                               _TOKEN: _TOKEN_APPROVED})
        self.assertEqual(module.main(["login", "--confirm"], out=io.StringIO()), 0)
        self.assertIn(_AUTHORIZE, self._paths(),
                      "a confirmed login must actually replace the credential")
        self.assertEqual(sorted(self._snapshot()), [".memvara/credentials.json"])

    def test_a_mistyped_project_id_is_refused_before_the_credential_is_probed(self) -> None:
        """The no-op and the argument check are in tension, and the argument check has to
        win.

        `authenticate` and `login` both stop early on a credential that already works, so
        a project id checked inside the flow is never looked at on exactly the machines
        where somebody typed one. What that user sees is "you are already authenticated",
        exit 0, and no sign at all that the project they named was refused -- which is the
        same class of silence as a credential minted against the wrong project.

        Found by running the command rather than by reading it: `authenticate my-project`
        against a live working credential answered 0.
        """
        module = self._auth()
        os.environ["MEMVARA_API_KEY"] = "mv_working"
        for command in ("authenticate", "login"):
            with self.subTest(command=command):
                self.calls = []
                self._replies(module, {"/v1/health": (200, _HEALTH_OK),
                                       "/v1/whoami": (200, _WHOAMI_OK)})
                out = io.StringIO()
                self.assertNotEqual(
                    module.main([command, "my-project"], out=out), 0,
                    f"{command} reported success on a project id it never accepted")
                self.assertEqual(self.calls, [],
                                 "the deployment was asked anything at all before the "
                                 f"argument was looked at: {self._paths()}")
                self.assertRegex(
                    out.getvalue(),
                    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                    r"-[0-9a-fA-F]{12}",
                    "the refusal must show the form that works rather than only saying "
                    f"no: {out.getvalue()}")

        self.calls = []
        self._replies(module, {"/v1/health": (200, _HEALTH_OK),
                               "/v1/whoami": (200, _WHOAMI_OK)})
        self.assertEqual(module.main(["authenticate", _PROJECT_ID], out=io.StringIO()), 0,
                         "a well-formed project id must still reach the no-op that makes "
                         "this command safe to run when unsure")


#: The heading the auth section is written under. Named here rather than searched for by
#: keyword because the guard below reads only what sits beneath it: a keyword scan of the
#: whole README passes on four sentences scattered through a page about hooks, which is
#: not a section anybody can be pointed at.
_AUTH_HEADING = "## Four commands for the credential itself"


class AuthReadme(unittest.TestCase):
    """The README is where this plugin says what it runs on your machine.

    Until this change it ran nothing: the plugin shipped a manifest, an endpoint and a
    vendored skill, zero `.py` files, and the README said so in as many words. The four
    commands are a different promise -- they run `python3` here and one of them writes a
    key into the user's home directory -- so both halves of the correction belong in this
    repository. The positive half is below; the negative half is
    `test_the_sentence_that_promised_no_local_process_is_gone`, and either alone is
    satisfied by a README with nothing in it.
    """

    def _section(self) -> str:
        """Everything under the auth heading, up to the next one.

        Bounded deliberately. Every assertion below is a substring check, and against the
        whole README each one would be satisfied by a word elsewhere on the page -- the
        install block already says `memvara` and the migration section already says
        `python`.
        """
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(_AUTH_HEADING, text,
                      f"the README has no {_AUTH_HEADING!r} section, so the commands it "
                      "now ships are undocumented")
        return text.split(_AUTH_HEADING, 1)[1].split("\n## ", 1)[0]

    @staticmethod
    def _sentences(section: str) -> "list[str]":
        """Sentences, not lines. The README is hard-wrapped, so a claim about revocation
        is routinely split across two lines and neither half holds both words."""
        return re.split(r"(?<=[.!?])\s+", " ".join(section.split()))

    def test_the_readme_says_what_the_auth_commands_run_and_write(self) -> None:
        """Positive: names the four commands, python3, and ~/.memvara/credentials.json.

        Stated positively because a README that has simply stopped describing the auth
        surface is indistinguishable, to a negative-only guard, from one that never had it.

        The four names come from `_COMMAND_NAMES`, which is written out in this file. The
        README and the manifest are both things under test here and neither may be asked
        what to expect.
        """
        section = self._section()
        for name in _COMMAND_NAMES:
            self.assertIn(f"/memvara:{name}", section,
                          f"the section never names /memvara:{name}, so one of the four "
                          "commands ships undocumented")
        # The path, RESOLVED and required to be the module. It was edited by hand in the
        # same commit that repointed the four command files, and nothing held it there.
        # `is_file()` would not be enough: measured in claude-memvara, a path pointing at
        # SKILL.md passed a guard spelled that way while the command would hand python3 a
        # markdown file. Three sabotages missed it because each DELETED something, and a
        # mis-repointed path is wrong-but-present rather than absent.
        stated = "skills/memvara/scripts/memvara_auth.py"
        self.assertIn(stated, section,
                      "the section does not say which file the commands run")
        self.assertEqual((PLUGIN / stated).resolve(), AUTH_SCRIPT.resolve(),
                         f"the README says {stated}, which is not the auth module")

        self.assertIn("python3", section,
                      "the section does not say that python3 runs on this machine when a "
                      "command is invoked, which is the whole of what a reader is owed "
                      "before they type one")
        self.assertIn("~/.memvara/credentials.json", section,
                      "the section does not name the file these commands write; 'stores "
                      "your key' names nothing a user can find, check or delete")
        self.assertIn("0600", section,
                      "the section names the file it writes without naming the mode it "
                      "writes it at, and a key is what is in it")

    def test_the_sentence_that_promised_no_local_process_is_gone(self) -> None:
        """The README said "There is no local Python process". It was true until this
        commit and it is the reason both halves of this guard live in this repository
        rather than in `claude-memvara`, which never carried the sentence.

        Scanned across the whole file with its line breaks flattened, because the README
        is hard-wrapped and the claim spans two lines as written. Scanned across every
        markdown file this repository tracks, because a sentence moved into a doc is a
        sentence still being told to a reader.

        This half alone is satisfied by an empty README, which is why
        `test_the_readme_says_what_the_auth_commands_run_and_write` is required beside it.
        """
        listed = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z", "*.md"],
            check=True, capture_output=True, text=True).stdout
        paths = [ROOT / name for name in listed.split("\0") if name]
        self.assertIn(ROOT / "README.md", paths,
                      "the README is not tracked, so this scan covers nothing")
        for path in paths:
            flat = " ".join(path.read_text(encoding="utf-8").split())
            self.assertNotIn(
                "no local Python process", flat,
                f"{path.relative_to(ROOT)} still promises no local Python process, and "
                "this plugin now ships one and runs it whenever a command is typed")

    def test_the_readme_says_a_host_configuration_is_changed_only_by_agreement(self) -> None:
        """The one promise a user cannot verify after the fact.

        This host's own OAuth client writes `~/.claude.json`, so a second writer to it
        leaves nobody able to say whose token is live. The code refuses to be that writer;
        the README is where a reader learns they can run these commands without finding
        out afterwards.
        """
        said = [s for s in self._sentences(self._section())
                if "configuration" in s.lower()]
        self.assertTrue(said,
                        "the section never mentions the client configuration a key may "
                        "also be sitting in, so a user reads 'writes one file' as 'there "
                        "is one place a key can be'")
        self.assertTrue(
            any(word in s.lower() for s in said
                for word in ("say yes", "agree", "ask", "confirm", "approve")),
            "the section names the host configuration without saying it is changed only "
            f"if the user agrees, which is the guarantee being made: {said}")

    def test_the_readme_says_logout_is_local_and_the_key_outlives_it(self) -> None:
        """Deleting the local copy is not revocation, and a user who reads it as one
        walks away from a live write-capable key.

        The second assertion is the load-bearing one: a sentence naming revocation and
        not saying the key still works reads as a claim that revocation happened.
        """
        said = [s for s in self._sentences(self._section()) if "revoke" in s.lower()]
        self.assertTrue(said, "the section never mentions revocation, so logout reads as "
                              "the end of the key rather than the end of this copy of it")
        self.assertTrue(
            any("still" in s.lower() for s in said),
            f"the section names revocation without saying the key still works until "
            f"then: {said}")

    def test_the_readme_states_both_argument_forms_and_what_the_bare_one_needs(self) -> None:
        """Both forms, and the status the bare one answers today.

        The no-argument form depends on a console change that is not deployed everywhere,
        and a user who types it against a deployment without it gets `422`. Naming the
        status is what turns that from a wall into a sentence about which form to use.

        `400` is deliberately not the number here: it is what a *slug* gets from the
        route, while an omitted project fails schema validation first. The plan said 400
        until it was measured against the live endpoint on 2026-08-31.
        """
        section = self._section()
        self.assertIn("`/memvara:authenticate`", section,
                      "the section never shows the no-argument form on its own")
        self.assertIn("/memvara:authenticate <project-id>", section,
                      "the section never shows the form that works against a deployment "
                      "which has not taken the console change")
        self.assertIn("422", section,
                      "the section does not name the status a bare authenticate answers "
                      "until the console change is deployed, so the command's own advice "
                      "arrives with nothing in the README to match it against")


class ModuleShape(unittest.TestCase):
    """Nothing may be defined below `unittest.main()`.

    It happened twice in this family and neither run said so. A class appended after the
    `__main__` block is collected by `unittest discover` and NOT by
    `python3 test/test_plugin.py`, and both print OK -- 26 tests one way and 21 the other
    in codex-memvara, 23 and 18 in vscode-memvara. CI uses discover, so it stays green.

    This suite and claude-memvara's are the two largest, which is where a five-test
    shortfall is hardest to notice, and appending to the end of a file is the natural way
    to add a class. A passing run must not be able to mean "the check never ran".
    """

    def test_nothing_is_defined_after_the_main_block(self) -> None:
        import ast

        body = ast.parse(pathlib.Path(__file__).read_text(encoding="utf-8")).body
        guards = [i for i, node in enumerate(body)
                  if isinstance(node, ast.If) and "__main__" in ast.dump(node.test)]
        self.assertEqual(len(guards), 1, "expected exactly one __main__ block")
        after = [type(node).__name__ for node in body[guards[0] + 1:]]
        self.assertEqual(
            after, [],
            f"{after} is defined after `unittest.main()`, so "
            "`python3 test/test_plugin.py` runs without it and still prints OK")


if __name__ == "__main__":
    unittest.main()

