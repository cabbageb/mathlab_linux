#!/usr/bin/env python3
"""Run one explicitly approved local sanity-check command with a hard timeout.

This is a deployment helper, not a proof oracle.  It intentionally defaults to 30
seconds.  Transport success is separated from the child experiment status: the
wrapper exits zero after reporting an explicit bounded_compute_status, while the
child traceback, timeout, and return code remain visible to the model.
"""
from __future__ import annotations
import argparse, json, subprocess, sys

p=argparse.ArgumentParser()
p.add_argument('--timeout', type=float, default=30.0)
p.add_argument('command', nargs=argparse.REMAINDER)
a=p.parse_args()
if a.command and a.command[0] == '--':
    a.command = a.command[1:]
if not a.command:
    p.error('command required after --')
try:
    r=subprocess.run(
        a.command,
        timeout=max(0.1,a.timeout),
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
    )
except subprocess.TimeoutExpired as exc:
    if exc.stdout:
        sys.stdout.write(exc.stdout if isinstance(exc.stdout, str) else exc.stdout.decode(errors="replace"))
    if exc.stderr:
        sys.stderr.write(exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode(errors="replace"))
    print(f'resource_limit_exceeded: command exceeded {a.timeout:g}s', file=sys.stderr)
    print(json.dumps({
        "bounded_compute_status": "resource_limit_exceeded",
        "returncode": 124,
        "timeout_seconds": a.timeout,
    }))
    raise SystemExit(0)
sys.stdout.write(r.stdout)
sys.stderr.write(r.stderr)
print(json.dumps({
    "bounded_compute_status": "ok" if r.returncode == 0 else "command_failed",
    "returncode": r.returncode,
}))
raise SystemExit(0)
