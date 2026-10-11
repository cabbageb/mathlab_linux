from __future__ import annotations

from pathlib import Path
from pathlib import PurePosixPath
from typing import BinaryIO
import hashlib
import json
import os
import re
import shutil


_ALLOWED_UPLOAD_SUFFIXES = {".pdf", ".md", ".txt", ".tex"}


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def safe_filename(name: str) -> str:
    """移除路径成分和控制字符，只保留可展示的文件名。"""
    base = Path(name).name
    base = re.sub(r"[\x00-\x1f\\/:*?\"<>|]+", "_", base).strip(" .")
    return base or "upload.bin"


class ArtifactStore:
    """把大体积数学材料放到可迁移的数据根目录，而不是源码目录。"""

    def __init__(self, data_root: Path, *, create: bool = True) -> None:
        self.data_root = data_root.expanduser().resolve()
        self.users_root = self.data_root / "users"
        self.platform_root = self.data_root / "platform"
        self.artifacts_root = self.data_root / "artifacts"
        self.runs_root = self.data_root / "research_runs"
        self.runtime_root = self.data_root / "runtime"
        if create:
            for path in (
                self.users_root,
                self.platform_root,
                self.artifacts_root,
                self.runs_root,
                self.runtime_root,
            ):
                path.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _validate_storage_key(storage_key: str) -> str:
        if not re.fullmatch(r"user_\d{6}", storage_key):
            raise ValueError(f"非法用户存储键：{storage_key}")
        return storage_key

    def user_root(self, storage_key: str) -> Path:
        """返回用户物理根；数据库 user_id 不参与路径命名。"""
        return self.users_root / self._validate_storage_key(storage_key)

    def user_files_root(self, storage_key: str) -> Path:
        return self.user_root(storage_key) / "files"

    def conversation_root(self, storage_key: str, conversation_id: str) -> Path:
        return self.user_root(storage_key) / "contracts" / conversation_id

    def conversation_runtime_root(self, storage_key: str, conversation_id: str) -> Path:
        return self.conversation_root(storage_key, conversation_id) / "runtime"

    def user_run_root(self, storage_key: str, run_id: str) -> Path:
        return self.user_root(storage_key) / "research_runs" / run_id

    def calculate_durable_usage(self, storage_key: str) -> int:
        """统计用户长期资产字节数，并在遍历时剪枝所有 runtime 目录。"""
        root = self.user_root(storage_key)
        if not root.is_dir():
            return 0
        total = 0
        for current, directories, filenames in os.walk(root):
            directories[:] = [name for name in directories if name != "runtime"]
            base = Path(current)
            for name in filenames:
                path = base / name
                if path.is_file() and not path.is_symlink():
                    total += path.stat().st_size
        return total

    def run_runtime_root(self, run_root: str | Path) -> Path:
        return self.resolve(run_root) / "runtime"

    def run_final_root(self, run_root: str | Path) -> Path:
        return self.resolve(run_root) / "final"

    def reference(self, path: str | Path) -> str:
        """把数据根内路径编码成可迁移的 POSIX 相对引用。"""
        raw = Path(path)
        target = raw.resolve() if raw.is_absolute() else (self.data_root / raw).resolve()
        if not target.is_relative_to(self.data_root):
            raise ValueError("Artifact 路径越出 MATHLAB_DATA_ROOT")
        relative = target.relative_to(self.data_root)
        if relative == Path("."):
            raise ValueError("Artifact 引用不能指向数据根目录本身")
        return relative.as_posix()

    def resolve(self, reference: str | Path) -> Path:
        """把持久化相对引用解析到当前数据根，并兼容根内旧绝对路径。"""
        raw_text = str(reference).strip()
        if not raw_text:
            raise ValueError("Artifact 引用不能为空")
        raw = Path(raw_text)
        if raw.is_absolute():
            target = raw.resolve()
        else:
            pure = PurePosixPath(raw_text.replace("\\", "/"))
            if pure.is_absolute() or ".." in pure.parts:
                raise ValueError(f"非法 Artifact 相对引用：{raw_text}")
            target = (self.data_root / Path(*pure.parts)).resolve()
        if not target.is_relative_to(self.data_root):
            raise ValueError("Artifact 路径越出 MATHLAB_DATA_ROOT")
        return target

    def exists(self, reference: str | Path) -> bool:
        """检查当前数据根中的引用是否存在。"""
        return self.resolve(reference).exists()

    @staticmethod
    def validate_upload_name(name: str) -> str:
        cleaned = safe_filename(name)
        suffix = Path(cleaned).suffix.lower()
        if suffix not in _ALLOWED_UPLOAD_SUFFIXES:
            raise ValueError("只支持 .pdf、.md、.txt、.tex 文件")
        return cleaned

    def save_user_file(self, storage_key: str, file_id: str, name: str, payload: bytes) -> Path:
        cleaned = self.validate_upload_name(name)
        suffix = Path(cleaned).suffix.lower()
        if suffix == ".pdf":
            if not payload.startswith(b"%PDF-"):
                raise ValueError("上传内容不是有效 PDF 文件头")
        else:
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("Markdown/TeX/Text 文件必须使用 UTF-8") from exc
            if "\x00" in text:
                raise ValueError("文本文件不得包含 NUL 字节")
        directory = self.user_files_root(storage_key) / file_id
        directory.mkdir(parents=True, exist_ok=False)
        path = directory / cleaned
        path.write_bytes(payload)
        return path

    def delete_file(self, path: str | Path) -> None:
        target = self.resolve(path)
        relative = target.relative_to(self.data_root)
        parts = relative.parts
        if (
            len(parts) != 5
            or parts[0] != "users"
            or not re.fullmatch(r"user_\d{6}", parts[1])
            or parts[2] != "files"
            or not parts[3].startswith("file_")
            or target.parent.parent != self.user_files_root(parts[1])
        ):
            raise ValueError("文件删除路径不符合 users/<storage>/files/<file_id>/<name>")
        if target.exists():
            if not target.is_file() or target.is_symlink():
                raise ValueError("拒绝删除非普通上传文件")
            shutil.rmtree(target.parent)

    def write_text(self, relative: str | Path, content: str) -> Path:
        path = self.resolve(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content.rstrip() + "\n", encoding="utf-8")
        temporary.replace(path)
        return path

    def write_bytes(self, relative: str | Path, content: bytes) -> Path:
        """以同目录原子替换方式写入 PDF 等二进制 Artifact。"""
        path = self.resolve(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(content)
        temporary.replace(path)
        return path

    def write_json(self, relative: str | Path, payload: object) -> Path:
        return self.write_text(relative, json.dumps(payload, ensure_ascii=False, indent=2))

    def read_text(self, path: str | Path) -> str:
        target = self.resolve(path)
        return target.read_text(encoding="utf-8")

    def read_json(self, path: str | Path) -> object:
        return json.loads(self.read_text(path))

    def run_root(self, run_id: str) -> Path:
        """旧布局 resolver；仅供迁移脚本读取，正式业务使用 DB run_root。"""
        path = self.runs_root / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def materialize_run_sources(
        self,
        run_root: Path,
        manifest: list[dict[str, object]],
    ) -> tuple[Path, list[dict[str, object]]]:
        """把批准时仍存在的用户文件复制进不可变 Run 输入快照。"""
        run_root = run_root.resolve()
        if not run_root.is_relative_to(self.users_root):
            raise ValueError("Run 输入快照必须位于用户存储根")
        destination = run_root / "input" / "sources"
        destination.mkdir(parents=True, exist_ok=True)
        frozen: list[dict[str, object]] = []
        for index, item in enumerate(manifest, start=1):
            source_path_raw = item.get("artifact_path")
            record = dict(item)
            if not source_path_raw or not self.resolve(str(source_path_raw)).is_file():
                record["availability"] = "missing"
                frozen.append(record)
                continue
            source = self.resolve(str(source_path_raw))
            name = safe_filename(str(item.get("original_name") or source.name))
            target = destination / f"{index:03d}-{name}"
            shutil.copy2(source, target)
            record.update(
                {
                    "availability": "frozen",
                    "snapshot_path": self.reference(target),
                    "snapshot_sha256": sha256_file(target),
                }
            )
            frozen.append(record)
        self.write_json(
            run_root.relative_to(self.data_root) / "input/source_manifest.json",
            frozen,
        )
        return destination, frozen
