#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from literature_tool_common import request, write_json


def main() -> int:
    """查询最多五篇 arXiv 摘要，并把每次查询作为独立证据保存。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", required=True)
    parser.add_argument("--max-results", type=int, default=5)
    parser.add_argument("--config", default="input/literature_access.json")
    args = parser.parse_args()
    result, operation_root = request(
        "arxiv_search", {"query": args.query, "max_results": args.max_results}, args.config
    )
    search_root = operation_root / "literature" / "search"
    index = 1 + len(list(search_root.glob("arxiv_search_*.json"))) if search_root.exists() else 1
    output = search_root / f"arxiv_search_{index:02d}.json"
    write_json(output, result)
    print(json.dumps({"output": str(output), "count": result.get("count")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
