#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from literature_tool_common import request, write_json


def main() -> int:
    """调用一次 Controller 托管的 theorem-first 检索并冻结规范化结果。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", required=True)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--config", default="input/literature_access.json")
    args = parser.parse_args()
    result, operation_root = request(
        "theorem_search", {"query": args.query, "top_k": args.top_k}, args.config
    )
    output = operation_root / "literature" / "search" / "theorem_search.json"
    write_json(output, result)
    print(json.dumps({"output": str(output), "backend": result.get("backend"), "count": result.get("count")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
