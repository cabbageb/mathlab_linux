#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import argparse
import json
import re

from literature_tool_common import write_json


SECTION = re.compile(r"\\(part|chapter|section|subsection|subsubsection)\*?\s*\{([^{}]{1,300})\}")
STANDARD_THEOREM_ENVS = {"theorem", "lemma", "proposition", "corollary", "definition", "remark", "thm", "lem", "prop", "cor", "defn"}
NEW_THEOREM = re.compile(r"\\newtheorem\*?\s*\{([^{}]+)\}\s*(?:\[[^\]]+\]\s*)?\{([^{}]+)\}", re.IGNORECASE)


def _keywords(query: str) -> list[str]:
    return [word.lower() for word in re.findall(r"[A-Za-z]{4,}", query) if word.lower() not in {"that", "with", "from", "this", "theorem"}][:20]


def main() -> int:
    """从冻结 TeX 建立 overview、章节树、定理位置与局部依赖阅读证据。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--paper-dir", required=True)
    parser.add_argument("--query", required=True)
    args = parser.parse_args()
    paper_root = Path(args.paper_dir).resolve()
    source_root = paper_root / "source"
    tex_files = sorted(source_root.rglob("*.tex")) if source_root.is_dir() else []
    if not tex_files:
        pdf_text = paper_root / "paper.txt"
        if not pdf_text.is_file():
            raise RuntimeError("冻结材料既没有 TeX，也没有可读 PDF text")
        text = pdf_text.read_text(encoding="utf-8", errors="replace")
        keys = _keywords(args.query)
        hits: list[dict[str, object]] = []
        pattern = re.compile(r"(?im)^(?:theorem|lemma|proposition|corollary|definition)\s+([0-9A-Za-z.:-]+).*$")
        for match in pattern.finditer(text):
            context = text[max(0, match.start() - 500): min(len(text), match.start() + 1800)]
            hits.append(
                {
                    "file": "paper.txt",
                    "environment": match.group(0).split()[0].lower(),
                    "label": match.group(1),
                    "offset": match.start(),
                    "query_score": sum(context.lower().count(key) for key in keys),
                    "local_context": context,
                }
            )
        hits.sort(key=lambda item: (-int(item["query_score"]), int(item["offset"])))
        evidence = {
            "query": args.query,
            "evidence_depth": ["pdf_overview", "pdf_theorem_locations", "pdf_local_context"],
            "main_tex": None,
            "tex_file_count": 0,
            "section_tree": [],
            "theorem_hits": hits[:20],
            "dependency_scan": {"note": "arXiv source 不可用；依赖仅按 PDF 局部上下文展开。"},
        }
        write_json(paper_root / "reading_evidence.json", evidence)
        (paper_root / "reading_outline.md").write_text(
            "# 冻结论文 PDF 分层阅读证据\n\n"
            f"Query: {args.query}\n\n"
            f"识别 theorem-like locations：{len(hits)}。arXiv source 不可用，必须在报告中明确 PDF fallback evidence depth。\n",
            encoding="utf-8",
        )
        print(json.dumps({"reading_evidence": str(paper_root / "reading_evidence.json"), "theorem_hits": len(hits), "mode": "pdf_fallback"}, ensure_ascii=False))
        return 0
    main_candidates = []
    for path in tex_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        main_candidates.append(("\\documentclass" in text, len(text), path, text))
    _, _, main_path, main_text = max(main_candidates)
    sections = [
        {"level": match.group(1), "title": " ".join(match.group(2).split()), "offset": match.start()}
        for match in SECTION.finditer(main_text)
    ]
    keys = _keywords(args.query)
    theorem_hits: list[dict[str, object]] = []
    for tex_path in tex_files:
        text = tex_path.read_text(encoding="utf-8", errors="replace")
        environments = set(STANDARD_THEOREM_ENVS)
        environments.update(match.group(1).strip() for match in NEW_THEOREM.finditer(text))
        theorem_pattern = re.compile(
            r"\\begin\{(" + "|".join(re.escape(item) for item in sorted(environments, key=len, reverse=True)) + r")\}(?:\[([^\]]+)\])?",
            re.IGNORECASE,
        )
        for match in theorem_pattern.finditer(text):
            start = max(0, match.start() - 500)
            end = min(len(text), match.start() + 1800)
            context = text[start:end]
            score = sum(context.lower().count(key) for key in keys)
            theorem_hits.append(
                {
                    "file": str(tex_path.relative_to(paper_root)),
                    "environment": match.group(1),
                    "label": match.group(2) or "",
                    "offset": match.start(),
                    "query_score": score,
                    "local_context": context,
                }
            )
    theorem_hits.sort(key=lambda item: (-int(item["query_score"]), str(item["file"]), int(item["offset"])))
    evidence = {
        "query": args.query,
        "evidence_depth": ["overview", "section_tree", "exact_theorem_locations", "local_context", "direct_dependency_scan"],
        "main_tex": str(main_path.relative_to(paper_root)),
        "tex_file_count": len(tex_files),
        "section_tree": sections,
        "theorem_hits": theorem_hits[:20],
        "dependency_scan": {
            "input_commands": len(re.findall(r"\\(?:input|include)\s*\{", main_text)),
            "bibliography_commands": len(re.findall(r"\\bibliography\s*\{", main_text)),
            "note": "仅展开与所选 theorem slice 直接相邻的定义、引理和引用；是否足够由报告逐项说明。",
        },
    }
    write_json(paper_root / "reading_evidence.json", evidence)
    lines = [
        "# 冻结论文分层阅读证据",
        "",
        "## Query",
        "",
        args.query,
        "",
        "## Overview",
        "",
        f"主 TeX：`{evidence['main_tex']}`；TeX 文件数：{len(tex_files)}。",
        "",
        "## Section Tree",
        "",
    ]
    lines.extend(f"- {item['level']}: {item['title']}" for item in sections[:80])
    lines.extend(["", "## Candidate Theorem Locations", ""])
    for index, item in enumerate(theorem_hits[:10], start=1):
        lines.append(f"- {index}. `{item['file']}` offset {item['offset']} · {item['environment']} · score {item['query_score']}")
    lines.extend(
        [
            "",
            "## Result Card Checklist",
            "",
            "最终报告应给出明确版本、原文位置、完整假设与量词、精确结论、proof mechanism、直接依赖、未展开依赖和逐项 applicability matrix。",
            "",
        ]
    )
    (paper_root / "reading_outline.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"reading_evidence": str(paper_root / "reading_evidence.json"), "theorem_hits": len(theorem_hits)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
