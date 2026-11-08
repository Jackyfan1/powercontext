import hashlib
import re
from pathlib import Path
from typing import Optional
import pdfplumber
from app.config import CHUNK_MAX_CHARS, CHUNK_OVERLAP


def compute_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()[:16]


def parse_pdf(file_path: Path) -> list[dict]:
    chunks = []
    with pdfplumber.open(str(file_path)) as pdf:
        para_idx = 0
        for page_num, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            paragraphs = re.split(r'\n\s*\n', text.strip())
            for para in paragraphs:
                para = para.strip()
                if not para or len(para) < 20:
                    continue
                sub_chunks = split_long_text(para)
                for sc in sub_chunks:
                    chunks.append({
                        "text": sc,
                        "page_number": page_num,
                        "paragraph_index": para_idx,
                    })
                    para_idx += 1
    return chunks


def parse_markdown(file_path: Path) -> list[dict]:
    text = file_path.read_text(encoding="utf-8")
    sections = re.split(r'\n#{1,6}\s+', text)
    chunks = []
    para_idx = 0
    for section in sections:
        lines = section.strip().split("\n")
        current_text = []
        for line in lines:
            line = line.strip()
            if not line:
                if current_text:
                    chunk_text = " ".join(current_text)
                    for sc in split_long_text(chunk_text):
                        chunks.append({
                            "text": sc,
                            "page_number": None,
                            "paragraph_index": para_idx,
                        })
                        para_idx += 1
                    current_text = []
            else:
                clean = re.sub(r'[*_\[\]()`#>]', '', line).strip()
                if clean:
                    current_text.append(clean)
        if current_text:
            chunk_text = " ".join(current_text)
            for sc in split_long_text(chunk_text):
                chunks.append({
                    "text": sc,
                    "page_number": None,
                    "paragraph_index": para_idx,
                })
                para_idx += 1
    return chunks


def parse_txt(file_path: Path) -> list[dict]:
    text = file_path.read_text(encoding="utf-8")
    paragraphs = re.split(r'\n\s*\n', text.strip())
    chunks = []
    para_idx = 0
    for para in paragraphs:
        para = para.strip()
        if not para or len(para) < 20:
            continue
        for sc in split_long_text(para):
            chunks.append({
                "text": sc,
                "page_number": None,
                "paragraph_index": para_idx,
            })
            para_idx += 1
    return chunks


def split_long_text(text: str, max_chars: int = CHUNK_MAX_CHARS,
                    overlap: int = CHUNK_OVERLAP) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = start + max_chars
        if end < len(text):
            split_pos = text.rfind(" ", start + max_chars // 2, end)
            if split_pos > start:
                end = split_pos
        chunks.append(text[start:end].strip())
        start = end - overlap if end < len(text) else end
    return [c for c in chunks if c]


def parse_file(file_path: Path) -> tuple[list[dict], str]:
    suffix = file_path.suffix.lower()
    content = file_path.read_bytes()
    content_hash = compute_hash(content)

    if suffix == ".pdf":
        chunks = parse_pdf(file_path)
    elif suffix in (".md", ".markdown"):
        chunks = parse_markdown(file_path)
    elif suffix == ".txt":
        chunks = parse_txt(file_path)
    else:
        raise ValueError(f"Unsupported file type: {suffix}")

    return chunks, content_hash


def parse_file_from_bytes(filename: str, content: bytes) -> tuple[list[dict], str]:
    content_hash = compute_hash(content)
    suffix = Path(filename).suffix.lower()

    if suffix == ".pdf":
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)
        try:
            chunks = parse_pdf(tmp_path)
        finally:
            tmp_path.unlink()
    elif suffix in (".md", ".markdown", ".txt"):
        text = content.decode("utf-8")
        if suffix in (".md", ".markdown"):
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w", encoding="utf-8") as tmp:
                tmp.write(text)
                tmp_path = Path(tmp.name)
            try:
                chunks = parse_markdown(tmp_path)
            finally:
                tmp_path.unlink()
        else:
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w", encoding="utf-8") as tmp:
                tmp.write(text)
                tmp_path = Path(tmp.name)
            try:
                chunks = parse_txt(tmp_path)
            finally:
                tmp_path.unlink()
    else:
        raise ValueError(f"Unsupported file type: {suffix}")

    return chunks, content_hash
