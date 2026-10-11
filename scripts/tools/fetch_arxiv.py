#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath
import argparse
import base64
import gzip
import hashlib
import io
import json
import re
import shutil
import subprocess
import tarfile

from literature_tool_common import request, write_json


MAX_ARCHIVE_MEMBERS = 10_000
MAX_EXTRACTED_BYTES = 128 * 1024 * 1024


def _safe_target(root: Path, name: str) -> Path:
    """把 tar 成员映射进 source 根并拒绝绝对路径、Windows 路径与父跳转。"""
    posix = PurePosixPath(name)
    windows = PureWindowsPath(name)
    if not name or "\x00" in name or posix.is_absolute() or windows.is_absolute() or windows.drive or ".." in posix.parts:
        raise ValueError(f"unsafe archive path: {name!r}")
    target = root.joinpath(*(part for part in posix.parts if part not in {"", "."})).resolve(strict=False)
    if target != root and root not in target.parents:
        raise ValueError(f"archive path escapes destination: {name!r}")
    return target


def safe_extract_source(raw: bytes, destination: Path) -> list[Path]:
    """安全解包 arXiv source；只接受普通目录/文件，并限制成员数与展开总量。"""
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    try:
        archive = tarfile.open(fileobj=io.BytesIO(raw), mode="r:*")
    except tarfile.ReadError:
        try:
            decompressed = gzip.decompress(raw)
        except (OSError, EOFError):
            decompressed = raw
        try:
            archive = tarfile.open(fileobj=io.BytesIO(decompressed), mode="r:*")
        except tarfile.ReadError:
            (root / "source.tex").write_bytes(decompressed)
            return [root / "source.tex"]
    with archive:
        members = archive.getmembers()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise ValueError("arXiv source archive 文件数过多")
        total = sum(member.size for member in members if member.isfile())
        if total > MAX_EXTRACTED_BYTES:
            raise ValueError("arXiv source archive 展开后超过 128 MiB")
        validated: list[tuple[tarfile.TarInfo, Path]] = []
        for member in members:
            target = _safe_target(root, member.name)
            if member.issym() or member.islnk():
                raise ValueError(f"arXiv source archive 不接受链接成员：{member.name}")
            if not (member.isdir() or member.isfile()):
                raise ValueError(f"arXiv source archive 成员类型不支持：{member.name}")
            validated.append((member, target))
        for member, target in validated:
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"无法读取 archive member：{member.name}")
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
    return sorted(root.rglob("*.tex"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_pdf_text(pdf: Path, target: Path) -> str:
    """优先 pdftotext，缺失时使用项目依赖 pypdf 生成页码锚点文本。"""
    binary = shutil.which("pdftotext")
    if binary:
        completed = subprocess.run([binary, "-layout", str(pdf), str(target)], check=False, capture_output=True, text=True)
        if completed.returncode == 0 and target.is_file():
            return "pdftotext"
    from pypdf import PdfReader

    chunks: list[str] = []
    for index, page in enumerate(PdfReader(str(pdf)).pages, start=1):
        chunks.append(f"\n=== Page {index} ===\n{page.extract_text() or ''}")
    target.write_text("".join(chunks), encoding="utf-8")
    return "pypdf"


def _search_metadata(operation_root: Path, arxiv_id: str) -> dict[str, object]:
    """从本 operation 已冻结的搜索结果恢复真实 bibliographic metadata。"""
    base_id = arxiv_id.rsplit("v", 1)[0]
    for path in sorted((operation_root / "literature" / "search").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for item in list(payload.get("results") or []):
            candidate = str(dict(item).get("arxiv_id") or "")
            if candidate.rsplit("v", 1)[0] != base_id:
                continue
            record = dict(item)
            published = str(record.get("published") or "")
            return {
                "title": str(record.get("title") or ""),
                "authors": list(record.get("authors") or []),
                "published": published,
                "updated": str(record.get("updated") or ""),
                "year": int(published[:4]) if published[:4].isdigit() else record.get("year"),
                "categories": list(record.get("categories") or []),
                "abstract": str(record.get("summary") or ""),
                "canonical_url": f"https://arxiv.org/abs/{base_id}",
            }
    return {"canonical_url": f"https://arxiv.org/abs/{base_id}"}


def normalize_arxiv_id(arxiv_id: str, config_path: str) -> str:
    """用本 operation 的搜索结果补回旧式 arXiv ID 被模型省略的 archive 前缀。"""
    value = arxiv_id.strip()
    if "/" in value or not re.fullmatch(r"\d{7}v\d+", value):
        return value
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    operation_root = Path(str(config["operation_root"]))
    matches: set[str] = set()
    for path in sorted((operation_root / "literature" / "search").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for item in list(payload.get("results") or []):
            candidate = str(dict(item).get("arxiv_id") or "")
            if candidate.endswith("/" + value):
                matches.add(candidate)
    return next(iter(matches)) if len(matches) == 1 else value


def main() -> int:
    """冻结一个明确 arXiv 版本的 source-first 原文包和 PDF 页码锚点。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--arxiv-id", required=True)
    parser.add_argument("--config", default="input/literature_access.json")
    args = parser.parse_args()
    requested_arxiv_id = normalize_arxiv_id(args.arxiv_id, args.config)
    try:
        source_result, operation_root = request(
            "fetch_arxiv_source", {"arxiv_id": requested_arxiv_id}, args.config
        )
    except RuntimeError as exc:
        print(json.dumps({
            "available": False,
            "arxiv_id": requested_arxiv_id,
            "error": str(exc),
        }, ensure_ascii=False))
        return 0
    pdf_result: dict[str, object] | None = None
    pdf_error = ""
    try:
        pdf_result, _ = request(
            "fetch_arxiv_pdf", {"arxiv_id": requested_arxiv_id}, args.config
        )
    except RuntimeError as exc:
        pdf_error = str(exc)
        if not source_result.get("available", True):
            raise
    arxiv_id = str(source_result["arxiv_id"])
    paper_root = operation_root / "literature" / "papers" / arxiv_id.replace("/", "_")
    paper_root.mkdir(parents=True, exist_ok=True)
    source_archive = paper_root / "source_download.bin"
    tex_files: list[Path] = []
    if source_result.get("available", True):
        source_archive.write_bytes(base64.b64decode(source_result["body_base64"]))
        tex_files = safe_extract_source(source_archive.read_bytes(), paper_root / "source")
    merged = paper_root / "source_merged.tex"
    if tex_files:
        chunks: list[str] = []
        for tex_file in tex_files:
            chunks.append(f"\n% ===== {tex_file.relative_to(paper_root / 'source')} =====\n")
            chunks.append(tex_file.read_text(encoding="utf-8", errors="replace"))
        merged.write_text("".join(chunks), encoding="utf-8")
    pdf = paper_root / "paper.pdf"
    extractor = None
    if pdf_result is not None:
        pdf.write_bytes(base64.b64decode(str(pdf_result["body_base64"])))
        if not pdf.read_bytes()[:5].startswith(b"%PDF"):
            raise ValueError("下载结果不是 PDF")
        extractor = _extract_pdf_text(pdf, paper_root / "paper.txt")
    if not tex_files and not pdf.is_file():
        raise RuntimeError("TeX source 与 PDF 均未成功物化")
    metadata = {
        "arxiv_id": arxiv_id,
        "exact_version": f"arXiv:{arxiv_id}",
        "source_status": "available" if tex_files else ("downloaded_no_tex" if source_archive.is_file() else "unavailable"),
        "source_url": source_result.get("url"),
        "pdf_url": pdf_result.get("url") if pdf_result is not None else None,
        "source_sha256": sha256(source_archive) if source_archive.is_file() else None,
        "pdf_sha256": sha256(pdf) if pdf.is_file() else None,
        "tex_file_count": len(tex_files),
        "tex_files": [str(path.relative_to(paper_root)) for path in tex_files],
        "source_merged_sha256": sha256(merged) if merged.is_file() else None,
        "pdf_text_extractor": extractor,
        "pdf_error": pdf_error or None,
        "full_text_kinds": [
            kind for kind, available in (("tex", bool(tex_files)), ("pdf", pdf.is_file())) if available
        ],
        **_search_metadata(operation_root, arxiv_id),
    }
    write_json(paper_root / "metadata.json", metadata)
    print(json.dumps({"paper_root": str(paper_root), **metadata}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
