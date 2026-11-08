"""工程资料导入：把规格、代码符号与测试用例解析成可检索的文档。"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Iterable, Iterator

from .store import Document


SPEC_HEADING = re.compile(r"^##\s+(?P<key>\S+)\s*(?P<title>.*)$")


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def iter_spec_documents(project_root: Path) -> Iterator[Document]:
    specs_dir = project_root / "specs"
    if not specs_dir.is_dir():
        return
    for spec_path in sorted(specs_dir.glob("*.md")):
        lines = spec_path.read_text(encoding="utf-8").splitlines()
        rel = _relative(spec_path, project_root)
        stem = spec_path.stem

        sections: list[tuple[str, str, int, int]] = []
        current_key, current_title, current_start = "S0", stem, 1
        body: list[str] = []
        for index, line in enumerate(lines, start=1):
            match = SPEC_HEADING.match(line)
            if match:
                if body:
                    sections.append((current_key, current_title, current_start, index - 1))
                current_key = match.group("key")
                current_title = match.group("title").strip() or current_key
                current_start = index
                body = [line]
            else:
                body.append(line)
        if body:
            sections.append((current_key, current_title, current_start, len(lines)))

        for key, title, start, end in sections:
            content = "\n".join(lines[start - 1 : end]).strip()
            if not content:
                continue
            yield Document(
                doc_id=f"spec:{stem}#{key}",
                kind="spec",
                path=rel,
                symbol=key,
                title=title,
                content=content,
                line_start=start,
                line_end=end,
            )


def _iter_python_symbols(path: Path) -> Iterator[tuple[str, str, int, int, str]]:
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = min([node.lineno, *[d.lineno for d in node.decorator_list]]) if node.decorator_list else node.lineno
            end = getattr(node, "end_lineno", node.lineno)
            yield node.name, type(node).__name__, start, end, "\n".join(lines[start - 1 : end])


def iter_code_documents(project_root: Path) -> Iterator[Document]:
    code_root = project_root / "business" / "app"
    if not code_root.is_dir():
        return
    for path in sorted(code_root.rglob("*.py")):
        rel = _relative(path, project_root)
        for name, symbol_type, start, end, content in _iter_python_symbols(path):
            yield Document(
                doc_id=f"code:{rel}::{name}",
                kind="code",
                path=rel,
                symbol=name,
                title=f"{symbol_type} {name}",
                content=content,
                line_start=start,
                line_end=end,
            )


def iter_test_documents(project_root: Path) -> Iterator[Document]:
    tests_root = project_root / "business" / "tests"
    if not tests_root.is_dir():
        return
    for path in sorted(tests_root.glob("test_*.py")):
        rel = _relative(path, project_root)
        source_lines = path.read_text(encoding="utf-8").splitlines()
        for name, _symbol_type, start, end, content in _iter_python_symbols(path):
            if not name.startswith("test_"):
                continue
            markers = [f"tests/{rel}#{index}" for index, line in enumerate(source_lines, 1)
                       if line.strip().startswith("@pytest.mark.") and start <= index <= end]
            expectation = next(
                (line.strip().strip('"') for line in source_lines[start - 1 : end]
                 if line.strip().startswith('"""')),
                "",
            )
            yield Document(
                doc_id=f"test:{rel}::{name}",
                kind="test",
                path=rel,
                symbol=name,
                title=expectation or name,
                content=content,
                line_start=start,
                line_end=end,
            )


def iter_all_documents(project_root: Path) -> Iterable[Document]:
    yield from iter_spec_documents(project_root)
    yield from iter_code_documents(project_root)
    yield from iter_test_documents(project_root)
