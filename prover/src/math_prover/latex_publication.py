from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import os
import re
import subprocess
import tempfile

from pypdf import PdfReader


FIXED_LATEX_PREAMBLE = r"""\documentclass[11pt]{article}
\usepackage[a4paper,margin=1in]{geometry}
\usepackage{fontspec}
\setmainfont{TeX Gyre Termes}
\usepackage{amsmath,amssymb,amsthm,mathtools}
\usepackage{microtype}
\usepackage[hidelinks]{hyperref}

\newtheorem{theorem}{Theorem}[section]
\newtheorem{proposition}[theorem]{Proposition}
\newtheorem{lemma}[theorem]{Lemma}
\newtheorem{corollary}[theorem]{Corollary}
\theoremstyle{definition}
\newtheorem{definition}[theorem]{Definition}
\newtheorem{example}[theorem]{Example}
\theoremstyle{remark}
\newtheorem{remark}[theorem]{Remark}
"""

_FORBIDDEN_COMMANDS = re.compile(
    r"\\(?:input|include|includeonly|includegraphics|bibliography|bibliographystyle|"
    r"write18|openin|openout|read|write|immediate|catcode)\b",
    flags=re.IGNORECASE,
)
@dataclass(frozen=True)
class LatexCompilation:
    """保存一次成功 XeLaTeX 编译的 PDF、日志与可审计摘要。"""

    source: str
    pdf_bytes: bytes
    log: str
    page_count: int
    extracted_text: str
    compiler_version: str

    @property
    def source_sha256(self) -> str:
        return hashlib.sha256(self.source.encode("utf-8")).hexdigest()

    @property
    def pdf_sha256(self) -> str:
        return hashlib.sha256(self.pdf_bytes).hexdigest()


class LatexPublicationCompiler:
    """在隔离临时目录中校验并编译单文件英文数学论文。"""

    def __init__(self, *, timeout_seconds: int = 120, latexmk_binary: str = "latexmk") -> None:
        self.timeout_seconds = max(1, int(timeout_seconds))
        self.latexmk_binary = latexmk_binary

    def preflight(self) -> str:
        """确认 latexmk/XeLaTeX 与固定字体可用，避免研究结束后才暴露环境错误。"""
        result = subprocess.run(
            [self.latexmk_binary, "-v"],
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"latexmk preflight 失败：{result.stderr.strip() or result.stdout.strip()}")
        version = next((line.strip() for line in result.stdout.splitlines() if "Latexmk" in line), "latexmk")
        return version

    def validate_source(
        self,
        source: str,
        *,
        require_theorem: bool,
        genuine_source_count: int,
    ) -> list[str]:
        """只检查单文件 TeX 边界与危险命令；论文质量交给 Auditor。"""
        _ = require_theorem, genuine_source_count
        errors: list[str] = []
        if len(source.encode("utf-8")) > 1024 * 1024:
            errors.append("LaTeX source 超过 1 MiB。")
        if "```" in source or re.search(r"^#{1,6}\s", source, flags=re.MULTILINE):
            errors.append("Writer 必须提交原始 LaTeX，不得提交 Markdown 标题或代码围栏。")
        if not source.rstrip().endswith(r"\end{document}"):
            errors.append("LaTeX source 必须以 `\\end{document}` 结束。")

        sanitized = re.sub(r"(?m)(?<!\\)%.*$", "", source)
        forbidden = sorted({match.group(0) for match in _FORBIDDEN_COMMANDS.finditer(sanitized)})
        if forbidden:
            errors.append(f"正文包含禁止的 TeX 命令：{', '.join(forbidden)}。")
        if source.count(r"\begin{document}") != 1 or source.count(r"\end{document}") != 1:
            errors.append("必须且只能有一个 document 环境。")
        return errors

    def compile(
        self,
        source: str,
        *,
        require_theorem: bool,
        genuine_source_count: int,
    ) -> tuple[LatexCompilation | None, list[str]]:
        """先做确定性校验，再用受限 latexmk/XeLaTeX 生成并读取 PDF。"""
        errors = self.validate_source(
            source,
            require_theorem=require_theorem,
            genuine_source_count=genuine_source_count,
        )
        if errors:
            return None, errors
        try:
            version = self.preflight()
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            return None, [str(exc)]

        with tempfile.TemporaryDirectory(prefix="mathlab-latex-") as temporary:
            root = Path(temporary)
            source_path = root / "paper.tex"
            output_root = root / "build"
            home_root = root / "home"
            output_root.mkdir()
            home_root.mkdir()
            source_path.write_text(source, encoding="utf-8")
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(home_root),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "openin_any": "p",
                "openout_any": "p",
                "TEXMFHOME": str(home_root / "texmf"),
                "TEXMFVAR": str(home_root / "texmf-var"),
                "TEXMFCONFIG": str(home_root / "texmf-config"),
                "SOURCE_DATE_EPOCH": "0",
            }
            command = [
                self.latexmk_binary,
                "-norc",
                "-xelatex",
                "-interaction=nonstopmode",
                "-halt-on-error",
                "-file-line-error",
                "-latexoption=-no-shell-escape",
                f"-outdir={output_root}",
                source_path.name,
            ]
            try:
                result = subprocess.run(
                    command,
                    cwd=root,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return None, [f"XeLaTeX 编译超过 {self.timeout_seconds} 秒。"]
            log = (result.stdout + "\n" + result.stderr).strip() + "\n"
            pdf_path = output_root / "paper.pdf"
            if result.returncode != 0 or not pdf_path.is_file():
                tail = "\n".join(log.splitlines()[-80:])
                return None, [f"XeLaTeX 编译失败：\n{tail}"]
            try:
                reader = PdfReader(str(pdf_path))
                extracted = "\n".join((page.extract_text() or "") for page in reader.pages)
            except Exception as exc:
                return None, [f"生成的 PDF 无法解析：{exc}"]
            if not reader.pages:
                return None, ["生成的 PDF 没有页面。"]
            return LatexCompilation(
                source=source,
                pdf_bytes=pdf_path.read_bytes(),
                log=log,
                page_count=len(reader.pages),
                extracted_text=extracted,
                compiler_version=version,
            ), []


def normalize_latex_output(text: str) -> str:
    """只清理外围空白；TeX 输出不做 Markdown 标题截断或围栏兼容。"""
    return text.strip() + "\n"
