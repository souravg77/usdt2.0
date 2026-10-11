from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_INDEX = "data/kb/index.json"
DEFAULT_NOTES = "docs/knowledge"
DEFAULT_BOOKS = "data/books"
TEXT_SUFFIXES = {".md", ".txt"}

_STOP = frozenset(
    "a an and are as at be been but by can could do does for from had has have how i if in into is "
    "it its may might more most no not of on or our so such than that the their them then there "
    "these they this those to was we were what when where which while who why will with would you "
    "your".split()
)
_WORD = re.compile(r"[a-z0-9]+(?:[.'][a-z0-9]+)*")


def tokenize(text: str) -> list[str]:
    out = []
    for w in _WORD.findall(text.lower()):
        w = w.replace("'", "")
        if w in _STOP or len(w) < 2:
            continue
        if len(w) > 4 and w.endswith("ies"):
            w = w[:-3] + "y"
        elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return out


@dataclass
class Chunk:
    source: str  # document title
    path: str
    page: str  # PDF page number, or the Markdown section heading
    text: str


@dataclass
class Hit:
    score: float
    chunk: Chunk

    def cite(self) -> str:
        return f"{self.chunk.source}, {self.chunk.page}"


def _title_from_name(path: Path) -> str:
    name = re.sub(r"^[0-9a-f]{8}-", "", path.stem)  # upload id prefix
    return re.sub(r"[-_]+", " ", name).strip() or path.stem


def _is_pdf(path: Path) -> bool:
    with open(path, "rb") as f:
        return f.read(5) == b"%PDF-"


def _clean(text: str) -> str:
    text = text.replace("\u200b", "").replace("\u00ad", "")  # zero-width space, soft hyphen
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text)  # stray control bytes
    text = re.sub(r"-\n(?=[a-z])", "", text)  # re-join hyphenated line breaks
    return re.sub(r"\s+", " ", text).strip()


def read_document(path: str | Path) -> tuple[str, list[tuple[str, str]]]:
    """Return (title, [(page label, text), ...]). PDFs are detected by content, so uploads
    saved without an extension work too."""
    path = Path(path)
    if not _is_pdf(path):
        if path.suffix and path.suffix.lower() not in TEXT_SUFFIXES:
            raise ValueError(f"unsupported file type {path.suffix} (use PDF, .md or .txt)")
        raw = path.read_bytes().decode("utf-8")  # raises on binary files
        title = path.stem
        sections: list[tuple[str, str]] = []
        heading, buf = "start", []
        for line in raw.splitlines():
            m = re.match(r"#{1,6}\s+(.*)", line)
            if m:
                if buf:
                    sections.append((heading, "\n".join(buf)))
                heading, buf = m.group(1).strip(), []
                if title == path.stem and line.startswith("# "):
                    title = heading
            else:
                buf.append(line)
        if buf:
            sections.append((heading, "\n".join(buf)))
        return title, [(f"section \"{h}\"", _clean(t)) for h, t in sections if _clean(t)]
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - pypdf is a dependency
        raise RuntimeError("pypdf is needed to read PDFs: pip install pypdf") from exc
    logging.getLogger("pypdf").setLevel(logging.ERROR)  # font-encoding chatter
    reader = PdfReader(str(path))
    meta_title = (reader.metadata.title if reader.metadata else None) or ""
    pages = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = _clean(page.extract_text() or "")
        except Exception as exc:  # a broken page shouldn't stop the whole book
            log.warning("%s page %d unreadable: %s", path.name, i, exc)
            continue
        if text:
            pages.append((f"p. {i}", text))
    title = meta_title.strip() or _title_from_name(path)
    return title, pages


def chunk_text(text: str, words: int = 180, overlap: int = 40) -> list[str]:
    toks = text.split()
    if len(toks) <= words:
        return [" ".join(toks)] if toks else []
    step = max(1, words - overlap)
    return [" ".join(toks[i:i + words]) for i in range(0, max(1, len(toks) - overlap), step)]


def _files_under(p: Path) -> list[Path]:
    if p.is_dir():
        return sorted(q for q in p.rglob("*") if q.is_file() and not q.name.startswith("."))
    return [p] if p.is_file() else []


def build_index(paths: list[str | Path] | None = None, out: str | Path = DEFAULT_INDEX,
                include_notes: bool = True, words: int = 180, overlap: int = 40) -> dict:
    """Index ``paths`` (files or folders; default ``data/books``) plus, unless
    ``include_notes`` is off, the notes in ``docs/knowledge``. Identical files are indexed once.
    Returns a summary."""
    files: list[Path] = []
    missing = []
    for p in paths or [DEFAULT_BOOKS]:
        found = _files_under(Path(p))
        if not found and paths:
            missing.append((str(p), "not found or empty"))
        files += found
    if include_notes:
        files += sorted(Path(DEFAULT_NOTES).glob("*.md"))

    seen: dict[str, str] = {}
    docs, chunks, duplicates, skipped = [], [], [], missing
    for f in files:
        digest = hashlib.sha256(f.read_bytes()).hexdigest()
        if digest in seen:
            duplicates.append((str(f), seen[digest]))
            continue
        seen[digest] = str(f)
        try:
            title, pages = read_document(f)
        except Exception as exc:
            skipped.append((str(f), str(exc)))
            continue
        n0 = len(chunks)
        for label, text in pages:
            chunks += [Chunk(title, str(f), label, c) for c in chunk_text(text, words, overlap)]
        docs.append({"title": title, "path": str(f), "sha256": digest, "pages": len(pages),
                     "chunks": len(chunks) - n0})

    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "documents": docs, "chunks": [asdict(c) for c in chunks]}, fh)
    return {"index": str(out), "documents": docs, "chunks": len(chunks),
            "duplicates": duplicates, "skipped": skipped}


class KnowledgeBase:
    """BM25 search over indexed passages."""

    def __init__(self, chunks: list[Chunk], documents: list[dict] | None = None,
                 k1: float = 1.5, b: float = 0.75):
        self.chunks = chunks
        self.documents = documents or []
        self.k1, self.b = k1, b
        self._tf = [Counter(tokenize(c.text + " " + c.page)) for c in chunks]
        self._len = [sum(tf.values()) for tf in self._tf]
        self._avg = (sum(self._len) / len(self._len)) if self._len else 0.0
        df: Counter = Counter()
        for tf in self._tf:
            df.update(tf.keys())
        n = len(chunks)
        self._idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    @classmethod
    def load(cls, path: str | Path = DEFAULT_INDEX) -> "KnowledgeBase":
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return cls([Chunk(**c) for c in data["chunks"]], data.get("documents"))

    def search(self, query: str, k: int = 5, source: str | None = None) -> list[Hit]:
        terms = [t for t in tokenize(query) if t in self._idf]
        if not terms:
            return []
        hits = []
        for i, tf in enumerate(self._tf):
            if source and source.lower() not in self.chunks[i].source.lower():
                continue
            norm = self.k1 * (1 - self.b + self.b * self._len[i] / (self._avg or 1))
            score = sum(self._idf[t] * tf[t] * (self.k1 + 1) / (tf[t] + norm) for t in terms if tf[t])
            if score > 0:
                hits.append(Hit(score, self.chunks[i]))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]
