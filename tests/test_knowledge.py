import json

from aitrader.knowledge import KnowledgeBase, build_index, chunk_text, read_document
from aitrader.knowledge.index import tokenize


def test_tokenize_drops_stopwords_and_plurals():
    assert tokenize("The miners are confirming blocks; it's a 51% attack") == \
        ["miner", "confirming", "block", "51", "attack"]


def test_chunk_text_overlaps():
    words = " ".join(f"w{i}" for i in range(400))
    chunks = chunk_text(words, words=100, overlap=20)
    assert all(len(c.split()) <= 100 for c in chunks)
    assert chunks[0].split()[-20:] == chunks[1].split()[:20]
    assert chunks[-1].split()[-1] == "w399"
    assert chunk_text("short text") == ["short text"]


def test_markdown_is_split_by_heading(tmp_path):
    p = tmp_path / "note.md"
    p.write_text("# My note\n\nintro\n\n## Orders\n\nA stop-market order always fills.\n")
    title, parts = read_document(p)
    assert title == "My note"
    assert parts[-1] == ('section "Orders"', "A stop-market order always fills.")


def test_build_and_search(tmp_path):
    books = tmp_path / "books"
    books.mkdir()
    (books / "chain.txt").write_text(
        "Double spending means paying with the same coin twice. Waiting for confirmations "
        "makes a double spending attack costly because the attacker must win every block race.")
    (books / "orders.md").write_text(
        "# Orders\n\nA stop-limit order can be skipped in a crash; a stop-market order exits.")
    (books / "copy.txt").write_bytes((books / "chain.txt").read_bytes())
    (books / "picture.png").write_bytes(b"\x89PNG\r\n")
    out = tmp_path / "kb" / "index.json"
    res = build_index([books], out=out, include_notes=False)
    assert len(res["documents"]) == 2
    assert len(res["duplicates"]) == 1
    assert [p for p, _ in res["skipped"]] == [str(books / "picture.png")]
    assert json.loads(out.read_text())["version"] == 1

    kb = KnowledgeBase.load(out)
    hits = kb.search("how do confirmations stop double spending?")
    assert hits and "Double spending" in hits[0].chunk.text
    assert kb.search("stop market")[0].chunk.source == "Orders"
    assert kb.search("chain", source="orders") == []
    assert kb.search("zebra") == []


def test_repo_notes_index(tmp_path, monkeypatch):
    """The committed notes are searchable on their own (no books needed)."""
    import pathlib

    monkeypatch.chdir(pathlib.Path(__file__).resolve().parents[1])
    res = build_index([tmp_path / "none"], out=tmp_path / "i.json")
    assert len(res["documents"]) >= 6
    kb = KnowledgeBase.load(tmp_path / "i.json")
    assert kb.search("51% attack hash rate")[0].chunk.source.startswith("Blockchain")
