import pytest

from agentic_search.errors import ResumesError
from agentic_search.index.embed import CachedEmbedder, OpenAIEmbedder, _key


def test_cached_text_needs_no_key_and_a_miss_says_what_is_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    emb = CachedEmbedder(OpenAIEmbedder(dim=4), tmp_path)
    emb._store([_key("known")], [[0.5, 0.5, 0.5, 0.5]])
    assert emb.embed(["known"]) == [[0.5, 0.5, 0.5, 0.5]]
    with pytest.raises(ResumesError) as e:
        emb.embed(["known", "new text"])
    assert e.value.code == "KEY_MISSING" and "OPENAI_API_KEY" in e.value.message


def test_the_llm_step_stops_before_calling_when_resumes_are_not_cached_and_there_is_no_key(tmp_path, monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace

    from agentic_search.config import load
    from agentic_search.extract.llm import run_llm_extraction
    from agentic_search.vocab.resolve import Vocabulary

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = load()
    cfg = replace(cfg, index=replace(cfg.index, cache=tmp_path))
    doc = SimpleNamespace(doc_no=1, id="r000001", fm={"hash": "0123456789abcdef"}, body="# Resume\n\n## Skills\n\nPython")
    with pytest.raises(ResumesError) as e:
        run_llm_extraction([doc], Vocabulary.load(cfg.index.vocab), cfg, "openai:gpt-5-mini", log=lambda *a: None)
    assert e.value.code == "KEY_MISSING" and "--extractor none" in e.value.message
