"""Direct browser → S3/R2 multipart upload through presigned links, completion and
hash verification, against a stand-in that enforces SigV4 and link expiry."""
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.request

from remake_studio import storage
from remake_studio.hybrid.transport import S3Transport, presign_url
from tests.s3_standin import S3Standin

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MiB = 1 << 20


class DirectUpload(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.d = self._t.name
        self.s3 = S3Standin()
        self.store = S3Transport(self.s3.endpoint, "media", self.s3.ak, self.s3.sk, "auto")
        self.src = os.path.join(self.d, "دروب ريمي الحلقة 1 كاملة.mp4")
        with open(self.src, "wb") as f:
            f.write(os.urandom(13 * MiB + 777))
        with open(self.src, "rb") as f:
            self.sha = hashlib.sha256(f.read()).hexdigest()

    def tearDown(self):
        self.s3.close()
        for root, _, files in os.walk(self.d):
            for n in files:
                os.chmod(os.path.join(root, n), 0o644)
        self._t.cleanup()

    def browser(self, link, *flag):
        r = subprocess.run(["node", os.path.join(ROOT, "tests", "direct_upload_e2e.cjs"), link, self.src, *flag],
                           capture_output=True, text=True, timeout=180)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_browser_uploads_directly_and_cloud_verifies(self):
        state = os.path.join(self.d, "state")
        r = storage.prepare(self.store, os.path.basename(self.src), os.path.getsize(self.src), state, part_size=5 * MiB)
        self.assertEqual(r["parts"], 3)
        page = self.s3.objects[r["page_key"]]
        self.assertTrue(page[2].startswith("text/html"))
        self.assertNotIn(self.s3.sk.encode(), page[0], "the page never contains the secret key")
        self.assertNotIn(b"Authorization", page[0])
        out = self.browser(r["link"])
        self.assertEqual(out["sha"], self.sha, "browser hash = local hash")
        self.assertEqual(out["errors"], [])
        rec = storage.complete(self.store, r["id"], state, os.path.join(self.d, "media"))
        self.assertEqual(rec["status"], "verified")
        self.assertEqual(rec["sha256_storage"], self.sha)
        self.assertEqual(self.store.head(r["key"])["bytes"], os.path.getsize(self.src))
        self.assertNotIn(r["page_key"], self.s3.objects, "upload page removed after verification")
        self.assertEqual(self.s3.denied, [])

    def test_expired_links_are_refused(self):
        state = os.path.join(self.d, "state")
        r = storage.prepare(self.store, "ep.mp4", os.path.getsize(self.src), state, expires=1, part_size=5 * MiB)
        import time
        time.sleep(2)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(r["link"])
        self.assertEqual(cm.exception.code, 403)

    def test_presigned_link_cannot_be_reused_for_another_object(self):
        link = self.store.presign("PUT", "media/source/a/source.mp4", 600)
        other = link.replace("/a/source.mp4", "/b/source.mp4")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(urllib.request.Request(other, data=b"x", method="PUT"))
        self.assertEqual(cm.exception.code, 403)
        urllib.request.urlopen(urllib.request.Request(link, data=b"x", method="PUT"))
        self.assertEqual(self.s3.objects["media/source/a/source.mp4"][0], b"x")

    def test_wrong_credentials_refused(self):
        bad = S3Transport(self.s3.endpoint, "media", self.s3.ak, "wrong-secret", "auto")
        with self.assertRaises(IOError):
            bad.put("x", b"y")
        self.assertEqual(self.store.selftest(), [], "the real credentials pass the conditional-write self-test")

    def test_hash_mismatch_is_not_accepted(self):
        state = os.path.join(self.d, "state")
        r = storage.prepare(self.store, "ep.mp4", os.path.getsize(self.src), state, part_size=5 * MiB)
        self.browser(r["link"])
        m = json.loads(self.s3.objects[f"uploads/{r['id']}/manifest.json"][0])
        m["sha256"] = "0" * 64
        self.store.put(f"uploads/{r['id']}/manifest.json", json.dumps(m).encode())
        rec = storage.complete(self.store, r["id"], state, os.path.join(self.d, "media"))
        self.assertEqual(rec["status"], "hash_mismatch")
        self.assertIsNone(rec["local_copy"])


if __name__ == "__main__":
    unittest.main()
