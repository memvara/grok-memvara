
"""Gates for the Grok plugin.

Every file the client will read is asserted here.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugin"
SKILL = PLUGIN / "skills" / "memvara"
HOSTED = "https://app.memvara.dev/mcp"
REPO_NAME = "memvara/grok-memvara"


def _json(path: pathlib.Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _library_bytes(sha: str, path: str) -> bytes:
    root = os.environ.get("MEMVARA_LIBRARY")
    if root:
        return subprocess.check_output(
            ["git", "-C", root, "show", f"{sha}:{path}"],
        )
    import urllib.request
    url = f"https://raw.githubusercontent.com/memvara/memvara/{sha}/{path}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read()


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


class License(unittest.TestCase):
    def test_apache(self) -> None:
        text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("Apache License", text)
        self.assertIn("Version 2.0", text)


class Hygiene(unittest.TestCase):
    def test_no_npx_in_json(self) -> None:
        for path in ROOT.rglob("*.json"):
            self.assertNotIn("npx", path.read_text(encoding="utf-8"), path)

    def test_no_hooks(self) -> None:
        self.assertFalse((PLUGIN / "hooks").exists())
        self.assertFalse((PLUGIN / ".app.json").exists())
        self.assertFalse((PLUGIN / "commands").exists())

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

    def test_mcp(self) -> None:
        server = _json(PLUGIN / ".mcp.json")["mcpServers"]["memvara"]
        self.assertEqual(server["url"], HOSTED)
        self.assertEqual(server.get("type"), "http")
        self.assertNotIn("command", server)

    def test_readme(self) -> None:
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("grok plugin marketplace add memvara/grok-memvara", text)
        self.assertIn("grok plugin install memvara --trust", text)
        self.assertIn(HOSTED, text)
        self.assertNotIn("npx ", text)
        self.assertNotIn("chatgpt", text.lower())

    def test_plugin_tree(self) -> None:
        allowed = {
            pathlib.Path("plugin.json"),
            pathlib.Path(".mcp.json"),
        }
        for path in SKILL.rglob("*"):
            if path.is_file():
                allowed.add(path.relative_to(PLUGIN))
        found = {p.relative_to(PLUGIN) for p in PLUGIN.rglob("*") if p.is_file()}
        self.assertFalse(found - allowed, found - allowed)


if __name__ == "__main__":
    unittest.main()
