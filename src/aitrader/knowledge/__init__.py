"""Local knowledge base: the books and notes the agent draws on, searchable offline.

``build_index`` reads PDFs, Markdown and text files, splits them into passages and saves them to
a JSON index (``data/kb/index.json`` by default, git-ignored because the books are copyrighted).
``KnowledgeBase.search`` ranks passages with BM25, so the Phase 4 AI layer can quote a source and
page when it explains a trade or answers a question. Nothing here calls the network.
"""

from .index import DEFAULT_INDEX, Chunk, Hit, KnowledgeBase, build_index, chunk_text, read_document

__all__ = [
    "DEFAULT_INDEX",
    "Chunk",
    "Hit",
    "KnowledgeBase",
    "build_index",
    "chunk_text",
    "read_document",
]
