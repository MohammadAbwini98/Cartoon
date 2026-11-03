"""Fail if any git-tracked file looks like it contains a credential.

  python3 scripts/check_secrets.py        (exit 1 on findings)

Checks tracked files only (what a push would publish): AWS/R2-style key ids,
private keys, bearer tokens, long hex secrets assigned to key-like names, and
files that should never be tracked (secrets.json, *.key, .env).
"""
import os
import re
import subprocess
import sys

PATTERNS = [
    ("aws/r2 access key id", re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("bearer token", re.compile(r"Bearer\s+[A-Za-z0-9._-]{30,}")),
    ("assigned secret", re.compile(r"(?i)(secret|token|api[_-]?key|password)[\"']?\s*[:=]\s*[\"'][A-Za-z0-9/+=_-]{24,}[\"']")),
]
BAD_NAMES = re.compile(r"(^|/)(secrets\.json|\.env(\..*)?|[^/]*\.key|id_rsa[^/]*)$")
ALLOW = {"tests/test_hybrid.py", "tests/test_providers.py", "scripts/check_secrets.py", ".env.example"}
# AWS's published documentation example key (used as a signing test vector) is not a secret.
DOC_EXAMPLE = {"AKIAIOSFODNN7EXAMPLE"}


def main(root="."):
    files = subprocess.run(["git", "-C", root, "ls-files"], capture_output=True, text=True, check=True).stdout.split()
    findings = []
    for f in files:
        if BAD_NAMES.search(f) and not f.endswith(".example"):
            findings.append((f, "file type that must never be tracked"))
        if f in ALLOW:
            continue
        try:
            text = open(os.path.join(root, f), encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        for name, rx in PATTERNS:
            for m in rx.finditer(text):
                if m.group(0) not in DOC_EXAMPLE:
                    findings.append((f, name))
    for f, what in findings:
        print(f"{f}: {what}")
    print(f"scanned {len(files)} tracked files, {len(findings)} findings")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
