"""Browser-chunk reassembly: chunks made by the real JS core, rebuilt in Python."""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

from remake_studio.ingest.reassemble import TransferError, reassemble

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SMALL = 1 << 20   # 1 MiB chunks keep the tests fast; the format is size-independent


def make_chunks(src, out_dir, enc, chunk=SMALL):
    r = subprocess.run(["node", os.path.join(ROOT, "tests", "make_chunks.mjs"), src, out_dir, enc, str(chunk)],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


class Reassemble(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.d = self._t.name
        self.src = os.path.join(self.d, "دروب ريمي الحلقة 1 كاملة.mp4")
        with open(self.src, "wb") as f:
            f.write(os.urandom(5 * SMALL + 12345))
        self.sha = hashlib.sha256(open(self.src, "rb").read()).hexdigest()

    def tearDown(self):
        for root, dirs, files in os.walk(self.d):
            for n in files:
                os.chmod(os.path.join(root, n), 0o644)
        self._t.cleanup()

    def test_binary_chunks_round_trip(self):
        meta = make_chunks(self.src, os.path.join(self.d, "c"), "bin")
        self.assertEqual(meta["sha256"], self.sha, "JS whole-file hash must equal Python's")
        out = os.path.join(self.d, "media", "source", "episode.mp4")
        r = reassemble(os.path.join(self.d, "c"), out, expect_sha256=meta["sha256"], expect_size=meta["size"])
        self.assertEqual(r["sha256"], self.sha)
        self.assertEqual(r["chunks"], 6)
        self.assertFalse(os.stat(out).st_mode & stat.S_IWUSR, "output must be read-only")
        self.assertTrue(os.path.exists(out + ".transfer.json"))

    def test_base64_chunks_round_trip(self):
        meta = make_chunks(self.src, os.path.join(self.d, "c"), "b64")
        out = os.path.join(self.d, "o.mp4")
        self.assertEqual(reassemble(os.path.join(self.d, "c"), out, expect_sha256=meta["sha256"])["sha256"], self.sha)

    def test_corrupted_chunk_is_rejected_and_nothing_is_left(self):
        make_chunks(self.src, os.path.join(self.d, "c"), "bin")
        victim = sorted(os.listdir(os.path.join(self.d, "c")))[2]
        p = os.path.join(self.d, "c", victim)
        b = bytearray(open(p, "rb").read()); b[-100] ^= 0xFF
        open(p, "wb").write(b)
        out = os.path.join(self.d, "o.mp4")
        with self.assertRaisesRegex(TransferError, "SHA-256 mismatch"):
            reassemble(os.path.join(self.d, "c"), out)
        self.assertFalse(os.path.exists(out)); self.assertFalse(os.path.exists(out + ".part"))

    def test_missing_chunk_is_reported(self):
        make_chunks(self.src, os.path.join(self.d, "c"), "bin")
        os.remove(os.path.join(self.d, "c", sorted(os.listdir(os.path.join(self.d, "c")))[0]))
        with self.assertRaisesRegex(TransferError, "1 of 6 chunks missing"):
            reassemble(os.path.join(self.d, "c"), os.path.join(self.d, "o.mp4"))

    def test_truncated_chunk_is_rejected(self):
        make_chunks(self.src, os.path.join(self.d, "c"), "bin")
        p = os.path.join(self.d, "c", sorted(os.listdir(os.path.join(self.d, "c")))[1])
        b = open(p, "rb").read(); open(p, "wb").write(b[:-10])
        with self.assertRaisesRegex(TransferError, "header says"):
            reassemble(os.path.join(self.d, "c"), os.path.join(self.d, "o.mp4"))

    def test_duplicate_identical_upload_is_harmless(self):
        make_chunks(self.src, os.path.join(self.d, "c"), "bin")
        first = sorted(os.listdir(os.path.join(self.d, "c")))[0]
        shutil.copy(os.path.join(self.d, "c", first), os.path.join(self.d, "c", "retry-copy"))
        self.assertEqual(reassemble(os.path.join(self.d, "c"), os.path.join(self.d, "o.mp4"))["sha256"], self.sha)

    def test_wrong_expected_hash_fails(self):
        make_chunks(self.src, os.path.join(self.d, "c"), "bin")
        with self.assertRaisesRegex(TransferError, "does not match expected"):
            reassemble(os.path.join(self.d, "c"), os.path.join(self.d, "o.mp4"), expect_sha256="0" * 64)

    def test_two_transfers_need_an_id_and_unrelated_files_are_skipped(self):
        other = os.path.join(self.d, "studio.zip")
        open(other, "wb").write(os.urandom(SMALL + 7))
        a = make_chunks(self.src, os.path.join(self.d, "c"), "bin")
        b = make_chunks(other, os.path.join(self.d, "c"), "b64")
        open(os.path.join(self.d, "c", "notes.txt"), "w").write("not a chunk")
        with self.assertRaisesRegex(TransferError, "several transfers"):
            reassemble(os.path.join(self.d, "c"), os.path.join(self.d, "o.mp4"))
        r = reassemble(os.path.join(self.d, "c"), os.path.join(self.d, "z.zip"), transfer_id=b["transferId"],
                       expect_sha256=b["sha256"])
        self.assertEqual(r["skipped_files"], ["notes.txt"])
        self.assertEqual(reassemble(os.path.join(self.d, "c"), os.path.join(self.d, "o.mp4"),
                                    transfer_id=a["transferId"])["sha256"], self.sha)

    def test_never_overwrites_a_different_existing_file(self):
        make_chunks(self.src, os.path.join(self.d, "c"), "bin")
        out = os.path.join(self.d, "o.mp4")
        open(out, "wb").write(b"something else")
        with self.assertRaisesRegex(TransferError, "refusing to overwrite"):
            reassemble(os.path.join(self.d, "c"), out, expect_sha256=self.sha)
        self.assertEqual(open(out, "rb").read(), b"something else")

    def test_full_size_chunks(self):
        """Real 16 MiB chunk size on a 40 MiB file (3 chunks)."""
        big = os.path.join(self.d, "big.bin")
        open(big, "wb").write(os.urandom(40 * SMALL))
        meta = make_chunks(big, os.path.join(self.d, "c16"), "bin", 16 * SMALL)
        self.assertEqual(meta["total"], 3)
        r = reassemble(os.path.join(self.d, "c16"), os.path.join(self.d, "big.out"), expect_sha256=meta["sha256"])
        self.assertEqual(r["bytes"], 40 * SMALL)


if __name__ == "__main__":
    unittest.main()
