"""GPU worker definition: pins agree, model fetcher verifies every byte."""
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "infra", "comfyui"))
import fetch_models  # noqa: E402


class Pins(unittest.TestCase):
    def test_dockerfile_matches_lock(self):
        lock = json.load(open(os.path.join(ROOT, "infra/comfyui/nodes.lock.json")))
        docker = open(os.path.join(ROOT, "infra/comfyui/Dockerfile")).read()
        self.assertIn(f"ARG BASE_IMAGE={lock['base_image']}", docker)
        specs = dict((n, (r, c)) for n, r, c in re.findall(r'"([^|"]+)\|([^|"]+)\|([0-9a-f]{40})"', docker))
        self.assertEqual(set(specs), {n["name"] for n in lock["custom_nodes"]})
        for n in lock["custom_nodes"]:
            self.assertEqual(specs[n["name"]], (n["repo"], n["commit"]))

    def test_schemas_reference_locked_commits(self):
        lock = json.load(open(os.path.join(ROOT, "infra/comfyui/nodes.lock.json")))
        schemas = json.load(open(os.path.join(ROOT, "workflows/node_schemas.json")))
        short = {n["name"]: n["commit"][:7] for n in lock["custom_nodes"]}
        short["ComfyUI"] = lock["comfyui"]["commit"][:7]
        for node, s in schemas.items():
            if node.startswith("_"):
                continue
            pack, commit = re.match(r"([A-Za-z_-]+[A-Za-z])@([0-9a-f]{7})", s["source"]).groups()
            self.assertEqual(short[pack], commit, f"{node}: schema read at {pack}@{commit}, lock has {short[pack]}")

    def test_models_pinned_by_commit_and_hash(self):
        for m in json.load(open(os.path.join(ROOT, "infra/comfyui/models.json")))["models"]:
            if m["url"] is None:
                self.assertIsNone(m["sha256"]); continue
            self.assertRegex(m["url"], r"/resolve/[0-9a-f]{40}/", "URL must pin an immutable revision")
            self.assertRegex(m["sha256"], r"^[0-9a-f]{64}$")
            self.assertGreater(m["bytes"], 0)


class Fetch(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.root = self._t.name
        self.data = os.urandom(300_000)
        self.entry = {"folder": "controlnet", "name": "x.pth", "url": "https://example.test/x.pth",
                      "sha256": hashlib.sha256(self.data).hexdigest(), "bytes": len(self.data)}

    def tearDown(self):
        self._t.cleanup()

    def opener(self, payload):
        return lambda url: io.BytesIO(payload)

    def test_download_verify_and_skip(self):
        path, how = fetch_models.fetch(self.entry, self.root, self.opener(self.data))
        self.assertEqual(how, "downloaded")
        self.assertEqual(open(path, "rb").read(), self.data)
        self.assertEqual(fetch_models.fetch(self.entry, self.root, self.opener(b"never read"))[1], "present")

    def test_corrupt_download_leaves_nothing(self):
        bad = bytearray(self.data); bad[1000] ^= 1
        with self.assertRaisesRegex(ValueError, "sha256"):
            fetch_models.fetch(self.entry, self.root, self.opener(bytes(bad)))
        self.assertEqual(os.listdir(os.path.join(self.root, "controlnet")), [])

    def test_unpinned_entry_refused(self):
        with self.assertRaisesRegex(ValueError, "no pinned"):
            fetch_models.fetch({"folder": "checkpoints", "name": "c.safetensors", "url": None, "sha256": None}, self.root)


if __name__ == "__main__":
    unittest.main()
