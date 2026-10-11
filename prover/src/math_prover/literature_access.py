from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse
from contextlib import contextmanager
import asyncio
import base64
import fcntl
import hashlib
import json
import random
import re
import secrets
import threading
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

from .config import AppConfig


USER_AGENT = "MathLab-vNext/0.3 literature-controller"
ARXIV_ID_PATTERN = re.compile(r"^(?:arXiv:)?([0-9]{4}\.[0-9]{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})v(\d+)$", re.IGNORECASE)
ARXIV_ATOM = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
ARXIV_MIN_REQUEST_INTERVAL_SECONDS = 3.0
ARXIV_MAX_ATTEMPTS = 4
_CACHE_LOCKS: dict[str, threading.Lock] = {}
_CACHE_LOCKS_GUARD = threading.Lock()


class LiteratureAccessError(RuntimeError):
    pass


class LiteraturePreflightError(LiteratureAccessError):
    pass


class LiteratureNetworkError(LiteratureAccessError):
    """保留一次最终网络失败的可审计字段，供 fallback 与诊断使用。"""

    def __init__(
        self,
        message: str,
        *,
        url: str,
        attempts: int,
        status: int | None = None,
        headers: dict[str, str] | None = None,
        transient: bool = False,
    ) -> None:
        super().__init__(message)
        self.url = url
        self.attempts = attempts
        self.status = status
        self.headers = headers or {}
        self.transient = transient


class _AllowedRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: frozenset[str]) -> None:
        super().__init__()
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """只允许文献白名单域名之间的 HTTPS 重定向。"""
        parsed = urlparse(newurl)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or host not in self.allowed_hosts:
            raise PermissionError(f"文献请求禁止重定向到：{newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass(frozen=True)
class LiteratureLimits:
    theorem_searches: int = 1
    arxiv_searches: int = 2
    abstracts: int = 5
    papers: int = 1


def _allowed_hosts(config: AppConfig) -> frozenset[str]:
    hosts = {
        (urlparse(config.search.theorem_primary_endpoint).hostname or "").lower(),
        (urlparse(config.search.theorem_fallback_endpoint).hostname or "").lower(),
        (urlparse(config.search.arxiv_api_endpoint).hostname or "").lower(),
        "arxiv.org",
        "export.arxiv.org",
    }
    return frozenset(host for host in hosts if host)


def _read_response(response: Any, *, limit: int = MAX_RESPONSE_BYTES) -> bytes:
    payload = response.read(limit + 1)
    if len(payload) > limit:
        raise LiteratureAccessError(f"文献响应超过 {limit} bytes 上限")
    return payload


@contextmanager
def _arxiv_request_slot(config: AppConfig, host: str):
    """按 MathLab runtime 跨线程/进程串行 arXiv 请求，并在完成后保留三秒间隔。"""
    if host not in {"arxiv.org", "export.arxiv.org"}:
        yield
        return
    lock_path = config.runtime_dir / "_literature_cache" / ".arxiv-rate.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        stream.seek(0)
        raw = stream.read().strip()
        last = float(raw) if raw else 0.0
        delay = ARXIV_MIN_REQUEST_INTERVAL_SECONDS - (time.time() - last)
        if delay > 0:
            time.sleep(delay)
        try:
            yield
        finally:
            stream.seek(0)
            stream.truncate()
            stream.write(f"{time.time():.6f}\n")
            stream.flush()
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _retry_after_seconds(error: urllib.error.HTTPError, attempt: int) -> float:
    """优先尊重 Retry-After，否则使用 3/6/12 秒的有界退避。"""
    value = str(error.headers.get("Retry-After") or "").strip() if error.headers else ""
    try:
        return max(0.0, min(float(value), 60.0))
    except ValueError:
        return float(3 * (2 ** attempt)) + random.uniform(0.0, 0.75)


def _network_error(
    exc: BaseException,
    *,
    url: str,
    attempts: int,
    transient: bool,
) -> LiteratureNetworkError:
    """把 urllib/socket 异常压缩成稳定且不含大响应正文的审计错误。"""
    status = int(exc.code) if isinstance(exc, urllib.error.HTTPError) else None
    headers = {}
    if isinstance(exc, urllib.error.HTTPError) and exc.headers:
        for key in ("Retry-After", "Content-Type", "X-Request-Id"):
            value = exc.headers.get(key)
            if value:
                headers[key] = str(value)
    return LiteratureNetworkError(
        f"{type(exc).__name__}: {exc}",
        url=url,
        attempts=attempts,
        status=status,
        headers=headers,
        transient=transient,
    )


def _request_bytes(
    config: AppConfig,
    *,
    url: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: int | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """由 Controller 使用标准代理环境访问单个白名单 HTTPS 端点。"""
    parsed = urlparse(url)
    allowed = _allowed_hosts(config)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in allowed:
        raise PermissionError(f"文献访问域名未授权：{url}")
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json, application/atom+xml, application/pdf, */*"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(), _AllowedRedirect(allowed))
    attempts = ARXIV_MAX_ATTEMPTS if host in {"arxiv.org", "export.arxiv.org"} else 1
    for attempt in range(attempts):
        try:
            with _arxiv_request_slot(config, host):
                with opener.open(request, timeout=timeout or config.search.timeout_seconds) as response:
                    body = _read_response(response)
                    metadata = {
                        "status": int(getattr(response, "status", 200)),
                        "url": response.geturl(),
                        "content_type": response.headers.get("Content-Type", ""),
                        "bytes": len(body),
                        "attempts": attempt + 1,
                    }
            return body, metadata
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code <= 599
            if not retryable or attempt + 1 >= attempts:
                raise _network_error(
                    exc,
                    url=url,
                    attempts=attempt + 1,
                    transient=retryable,
                ) from exc
            time.sleep(_retry_after_seconds(exc, attempt))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt + 1 >= attempts:
                raise _network_error(
                    exc,
                    url=url,
                    attempts=attempt + 1,
                    transient=True,
                ) from exc
            time.sleep(float(3 * (2 ** attempt)) + random.uniform(0.0, 0.75))
    raise LiteratureAccessError("arXiv 请求在有界重试后仍未完成")


def _normalize_theorem_primary(body: Any) -> list[dict[str, Any]]:
    if not isinstance(body, dict) or not isinstance(body.get("theorems"), list):
        raise LiteratureAccessError("TheoremSearch response 缺少 theorems 列表")
    results: list[dict[str, Any]] = []
    for item in body["theorems"]:
        if not isinstance(item, dict):
            continue
        paper = item.get("paper") or {}
        if not isinstance(paper, dict) or str(paper.get("source", "")).lower() != "arxiv":
            continue
        arxiv_id = str(paper.get("paper_id") or "").strip()
        if not arxiv_id:
            continue
        results.append(
            {
                "title": str(paper.get("title") or ""),
                "statement": str(item.get("body") or ""),
                "slogan": str(item.get("slogan") or ""),
                "theorem_id": str(item.get("theorem_id") or ""),
                "theorem_name": str(item.get("name") or ""),
                "theorem_type": str(item.get("theorem_type") or ""),
                "arxiv_id": arxiv_id,
                "authors": paper.get("authors") or [],
                "year": paper.get("year"),
                "categories": paper.get("categories") or [],
                "link": str(item.get("link") or ""),
                "paper_link": str(paper.get("link") or ""),
                "score": item.get("score"),
                "similarity": item.get("similarity"),
            }
        )
    return results


def _normalize_theorem_fallback(body: Any) -> list[dict[str, Any]]:
    raw = body.get("results") if isinstance(body, dict) else body
    if not isinstance(raw, list):
        raise LiteratureAccessError("LeanSearch response 不是结果列表")
    results: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        results.append(
            {
                "title": str(item.get("title") or item.get("paper_title") or ""),
                "statement": str(item.get("theorem") or item.get("statement") or item.get("formal_statement") or ""),
                "theorem_id": str(item.get("theorem_id") or item.get("name") or ""),
                "arxiv_id": str(item.get("arxiv_id") or ""),
                "authors": item.get("authors") or [],
                "year": item.get("year"),
                "categories": item.get("categories") or [],
                "link": str(item.get("url") or item.get("link") or ""),
                "paper_link": str(item.get("paper_url") or ""),
            }
        )
    return results


def theorem_search(config: AppConfig, query: str, top_k: int) -> dict[str, Any]:
    """先调用 TheoremSearch，失败时才回退 LeanSearch，并返回规范化定理结果。"""
    errors: list[str] = []
    count = max(1, min(int(top_k), config.search.max_results))
    try:
        raw, _ = _request_bytes(
            config,
            url=config.search.theorem_primary_endpoint,
            method="POST",
            payload={"query": query, "n_results": count, "sources": ["arXiv"]},
        )
        results = _normalize_theorem_primary(json.loads(raw.decode("utf-8")))
        backend = "theoremsearch_arxiv"
    except Exception as exc:
        errors.append(f"theoremsearch_arxiv: {type(exc).__name__}: {exc}")
        try:
            raw, _ = _request_bytes(
                config,
                url=config.search.theorem_fallback_endpoint,
                method="POST",
                payload={
                    "query": query,
                    "task": "Retrieve mathematical theorems, lemmas and definitions useful for this query.",
                    "num_results": count,
                },
            )
            results = _normalize_theorem_fallback(json.loads(raw.decode("utf-8")))
            backend = "leansearch"
        except Exception as fallback_exc:
            errors.append(f"leansearch: {type(fallback_exc).__name__}: {fallback_exc}")
            raise LiteratureAccessError("；".join(errors)) from fallback_exc
    return {"query": query, "backend": backend, "count": len(results[:count]), "results": results[:count], "errors": errors}


def _atom_text(entry: ET.Element, path: str) -> str:
    node = entry.find(path, ARXIV_ATOM)
    return " ".join((node.text or "").split()) if node is not None else ""


def arxiv_search(
    config: AppConfig,
    query: str,
    max_results: int,
    *,
    abstract_limit: int | None = None,
) -> dict[str, Any]:
    """查询 arXiv Atom API，并保留条目的明确版本、摘要和可下载链接。"""
    count = max(
        1,
        min(
            int(max_results),
            config.search.max_results,
            int(abstract_limit or config.search.max_literature_abstracts),
        ),
    )
    params = urlencode({"search_query": query, "start": 0, "max_results": count})
    raw, _ = _request_bytes(config, url=f"{config.search.arxiv_api_endpoint}?{params}")
    root = ET.fromstring(raw)
    entries: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", ARXIV_ATOM)[:count]:
        identifier = _atom_text(entry, "atom:id").rstrip("/").split("/")[-1]
        links = {
            str(node.attrib.get("title") or node.attrib.get("rel") or ""): str(node.attrib.get("href") or "")
            for node in entry.findall("atom:link", ARXIV_ATOM)
        }
        entries.append(
            {
                "arxiv_id": identifier,
                "title": _atom_text(entry, "atom:title"),
                "summary": _atom_text(entry, "atom:summary"),
                "published": _atom_text(entry, "atom:published"),
                "updated": _atom_text(entry, "atom:updated"),
                "authors": [_atom_text(author, "atom:name") for author in entry.findall("atom:author", ARXIV_ATOM)],
                "categories": [str(node.attrib.get("term") or "") for node in entry.findall("atom:category", ARXIV_ATOM)],
                "pdf_url": links.get("pdf", f"https://arxiv.org/pdf/{identifier}"),
                "entry_url": _atom_text(entry, "atom:id"),
            }
        )
    return {"query": query, "count": len(entries), "results": entries}


def exact_arxiv_id(value: str) -> str:
    match = ARXIV_ID_PATTERN.fullmatch(value.strip())
    if not match:
        raise ValueError("必须使用带 vN 的明确 arXiv 版本，例如 1807.06751v1")
    return f"{match.group(1)}v{match.group(2)}"


def _cached_download(config: AppConfig, cache_root: Path, arxiv_id: str, kind: str) -> tuple[bytes, dict[str, Any]]:
    """按 exact arXiv version 对 source/PDF 做进程内 single-flight 与磁盘去重。"""
    safe_id = arxiv_id.replace("/", "_")
    target = cache_root / safe_id / ("source_download.bin" if kind == "source" else "paper.pdf")
    key = str(target.resolve())
    with _CACHE_LOCKS_GUARD:
        lock = _CACHE_LOCKS.setdefault(key, threading.Lock())
    with lock:
        if target.is_file():
            raw = target.read_bytes()
            return raw, {"status": 200, "url": "cache", "content_type": "application/octet-stream", "bytes": len(raw), "cache_hit": True}
        url = f"https://export.arxiv.org/e-print/{arxiv_id}" if kind == "source" else f"https://arxiv.org/pdf/{arxiv_id}"
        raw, meta = _request_bytes(config, url=url, timeout=min(180, config.search.timeout_seconds * 4))
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_bytes(raw)
        temporary.replace(target)
        return raw, {**meta, "cache_hit": False}


def run_literature_preflight(config: AppConfig) -> dict[str, Any]:
    """在任何付费模型调用前验证两种 theorem backend 与 arXiv metadata 网络。"""
    checks: list[dict[str, Any]] = []
    payloads = (
        (
            "theoremsearch",
            config.search.theorem_primary_endpoint,
            {"query": "wave equation energy identity", "n_results": 1, "sources": ["arXiv"]},
        ),
        (
            "leansearch",
            config.search.theorem_fallback_endpoint,
            {
                "query": "wave equation energy identity",
                "task": "Retrieve mathematical theorems useful for this query.",
                "num_results": 1,
            },
        ),
    )
    for label, endpoint, payload in payloads:
        try:
            raw, meta = _request_bytes(config, url=endpoint, method="POST", payload=payload)
            json.loads(raw.decode("utf-8"))
            checks.append({"name": label, "ok": True, **meta})
        except Exception as exc:
            raise LiteraturePreflightError(f"{label} 预检失败：{type(exc).__name__}: {exc}") from exc
    try:
        result = arxiv_search(config, "all:wave equation energy", 1)
        checks.append({"name": "arxiv_metadata", "ok": True, "count": result["count"]})
    except Exception as exc:
        raise LiteraturePreflightError(f"arXiv metadata 预检失败：{type(exc).__name__}: {exc}") from exc
    return {"ok": True, "checks": checks}


class LiteratureAccessServer:
    """让隔离 Agent 通过一次性回环 token 使用限额、可审计的文献能力。"""

    def __init__(
        self,
        config: AppConfig,
        operation_root: Path,
        *,
        cache_root: Path | None = None,
        limits: LiteratureLimits | None = None,
        archive_root: Path | None = None,
    ) -> None:
        self.config = config
        self.operation_root = operation_root.resolve()
        self.cache_root = (cache_root or operation_root.parent / "_literature_cache").resolve()
        self.limits = limits or LiteratureLimits(
            theorem_searches=config.search.max_literature_theorem_searches,
            arxiv_searches=config.search.max_literature_arxiv_searches,
            abstracts=config.search.max_literature_abstracts,
            papers=config.search.max_literature_papers,
        )
        self.archive_root = archive_root.resolve() if archive_root is not None else None
        self.controller_root = self.operation_root / "controller"
        self.controller_root.mkdir(parents=True, exist_ok=True)
        self.audit_path = self.controller_root / "network_audit.jsonl"
        self.state_path = self.controller_root / "access_state.json"
        self.token = "literature-" + secrets.token_urlsafe(32)
        self.endpoint = ""
        self.server: asyncio.AbstractServer | None = None
        self._lock = threading.Lock()
        self._state = self._load_state()

    async def __aenter__(self) -> LiteratureAccessServer:
        """启动只监听 127.0.0.1 的大响应回环服务。"""
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0, limit=96 * 1024 * 1024)
        host, port = self.server.sockets[0].getsockname()[:2]
        self.endpoint = f"{host}:{port}"
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    def client_payload(self) -> dict[str, Any]:
        """生成只写入 Agent 私有工作区的回环能力配置。"""
        return {
            "endpoint": self.endpoint,
            "token": self.token,
            "operation_root": str(self.operation_root),
            "limits": {
                "theorem_searches": self.limits.theorem_searches,
                "arxiv_searches": self.limits.arxiv_searches,
                "abstracts": self.limits.abstracts,
                "papers": self.limits.papers,
            },
        }

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            payload = json.loads((await reader.readline()).decode("utf-8"))
            response = await asyncio.to_thread(self.dispatch, payload)
        except Exception as exc:
            response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    def dispatch(self, payload: dict[str, Any]) -> dict[str, Any]:
        """核验 token 与语义预算，再由 Controller 网络环境完成一次动作。"""
        if payload.get("token") != self.token:
            raise PermissionError("文献访问 token 无效")
        action = str(payload.get("action") or "")
        if action in {"theorem_search", "arxiv_search"}:
            with self._lock:
                self._reserve_search(action)
        try:
            if action == "theorem_search":
                result = theorem_search(self.config, str(payload.get("query") or ""), int(payload.get("top_k") or 5))
            elif action == "arxiv_search":
                result = arxiv_search(
                    self.config,
                    str(payload.get("query") or ""),
                    int(payload.get("max_results") or 5),
                    abstract_limit=self.limits.abstracts,
                )
            elif action in {"fetch_arxiv_source", "fetch_arxiv_pdf"}:
                arxiv_id = exact_arxiv_id(str(payload.get("arxiv_id") or ""))
                with self._lock:
                    self._ensure_materialization_capacity(arxiv_id)
                kind = "source" if action.endswith("source") else "pdf"
                try:
                    raw, meta = _cached_download(self.config, self.cache_root, arxiv_id, kind)
                except (LiteratureNetworkError, urllib.error.HTTPError) as exc:
                    if isinstance(exc, urllib.error.HTTPError):
                        exc = _network_error(
                            exc,
                            url=exc.geturl(),
                            attempts=1,
                            transient=exc.code == 429 or 500 <= exc.code <= 599,
                        )
                    if kind != "source" or not (
                        exc.transient or exc.status in {403, 404, 429}
                    ):
                        raise
                    result = {
                        "arxiv_id": arxiv_id,
                        "available": False,
                        "status": exc.status,
                        "url": exc.url,
                        "bytes": 0,
                        "attempts": exc.attempts,
                        "response_headers": exc.headers,
                        "fallback_required": True,
                        "reason": "arXiv source package unavailable after bounded attempts; use PDF fallback",
                    }
                else:
                    with self._lock:
                        self._mark_materialized(arxiv_id)
                    self._archive_download(arxiv_id, kind, raw, meta)
                    result = {"arxiv_id": arxiv_id, "available": True, "body_base64": base64.b64encode(raw).decode("ascii"), **meta}
            else:
                raise ValueError(f"未知文献访问操作：{action}")
        except Exception as exc:
            self._audit(
                action,
                False,
                error=f"{type(exc).__name__}: {exc}",
                failure=exc,
            )
            raise
        self._audit(action, True, result=result)
        return {"ok": True, "result": result}

    def _load_state(self) -> dict[str, Any]:
        if self.state_path.is_file():
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
        return {"counts": {}, "materialized_papers": []}

    def _save_state(self) -> None:
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.state_path)

    def _reserve_search(self, action: str) -> None:
        limits = {
            "theorem_search": self.limits.theorem_searches,
            "arxiv_search": self.limits.arxiv_searches,
        }
        if action not in limits:
            raise ValueError(f"未知文献访问操作：{action}")
        counts = dict(self._state.get("counts") or {})
        used = int(counts.get(action) or 0)
        if used >= int(limits[action]):
            raise LiteratureAccessError(f"{action} 已达到本 operation 上限 {limits[action]}")
        counts[action] = used + 1
        self._state["counts"] = counts
        self._save_state()

    def _ensure_materialization_capacity(self, arxiv_id: str) -> None:
        materialized = list(self._state.get("materialized_papers") or [])
        if arxiv_id not in materialized and len(materialized) >= self.limits.papers:
            raise LiteratureAccessError(
                f"成功物化论文已达到本 operation 上限 {self.limits.papers}"
            )

    def _mark_materialized(self, arxiv_id: str) -> None:
        materialized = list(self._state.get("materialized_papers") or [])
        if arxiv_id not in materialized:
            materialized.append(arxiv_id)
        self._state["materialized_papers"] = materialized
        self._save_state()

    def _archive_download(
        self,
        arxiv_id: str,
        kind: str,
        raw: bytes,
        metadata: dict[str, Any],
    ) -> None:
        """下载成功即写入 Run 管理的 owner-scoped 目录，独立于模型最终报告。"""
        if self.archive_root is None:
            return
        paper_root = self.archive_root / "papers" / arxiv_id.replace("/", "_")
        paper_root.mkdir(parents=True, exist_ok=True)
        filename = "source_download.bin" if kind == "source" else "paper.pdf"
        target = paper_root / filename
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_bytes(raw)
        temporary.replace(target)
        record = {
            "arxiv_id": arxiv_id,
            "kind": kind,
            "path": filename,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "url": metadata.get("url"),
            "status": metadata.get("status"),
            "attempts": metadata.get("attempts", 1),
            "materialized_at": time.time(),
        }
        record_path = paper_root / f"materialization-{kind}.json"
        temp_record = record_path.with_suffix(".tmp")
        temp_record.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp_record.replace(record_path)

    def _audit(self, action: str, ok: bool, *, result: dict[str, Any] | None = None, error: str | None = None, failure: BaseException | None = None) -> None:
        record: dict[str, Any] = {"timestamp": time.time(), "action": action, "ok": ok}
        if result is not None:
            record.update(
                {
                    "arxiv_id": result.get("arxiv_id"),
                    "backend": result.get("backend"),
                    "count": result.get("count"),
                    "status": result.get("status"),
                    "bytes": result.get("bytes"),
                    "attempts": result.get("attempts"),
                    "fallback_required": result.get("fallback_required"),
                    "response_headers": result.get("response_headers"),
                }
            )
        if error:
            record["error"] = error
        if isinstance(failure, LiteratureNetworkError):
            record.update(
                {
                    "status": failure.status,
                    "attempts": failure.attempts,
                    "response_headers": failure.headers,
                    "transient": failure.transient,
                    "url": failure.url,
                }
            )
        with self.audit_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
