from __future__ import annotations

from pathlib import Path
import argparse
import json
import socket


def main() -> None:
    """调用 Controller 的 Reviewer 只读证据服务，并把 JSON 结果写到标准输出。"""
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    listing = sub.add_parser("list")
    listing.add_argument("--kind", default="")
    opening = sub.add_parser("open")
    opening.add_argument("--artifact-id", required=True)
    args = parser.parse_args()

    config = json.loads((Path("input") / "reviewer_evidence_access.json").read_text(encoding="utf-8"))
    host, port = str(config["endpoint"]).rsplit(":", 1)
    payload = {"token": config["token"], "action": args.action}
    if args.action == "list":
        payload["kind"] = args.kind
    else:
        payload["artifact_id"] = args.artifact_id
    with socket.create_connection((host, int(port)), timeout=30) as connection:
        connection.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        response = b""
        while not response.endswith(b"\n"):
            chunk = connection.recv(65536)
            if not chunk:
                break
            response += chunk
    result = json.loads(response.decode("utf-8"))
    if not result.get("ok"):
        raise SystemExit(result.get("error") or "Reviewer evidence request failed")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
