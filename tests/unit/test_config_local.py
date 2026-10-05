import shutil
from pathlib import Path

from agentic_search import config

ROOT = Path(__file__).resolve().parents[2]


def test_a_local_file_adds_to_the_shared_settings(tmp_path):
    shutil.copy(ROOT / "resumes.toml", tmp_path / "resumes.toml")
    shared = config.load(tmp_path)
    (tmp_path / "resumes.local.toml").write_text(
        '[[sources]]\nid = "mine"\nloader = "markdown_dir"\npath = "my/md"\n\n[web]\nstarters = ["Mine"]\n', encoding="utf-8")
    local = config.load(tmp_path)
    assert [s.id for s in local.sources] == ["mine"], "[[sources]] is replaced whole"
    assert local.web.starters == ["Mine"]
    assert local.web.port == shared.web.port and local.query.max_cards == shared.query.max_cards
    assert local.web.model == shared.web.model, "the rest of [web] stays"
