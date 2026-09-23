import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aiact_onto.package import app, copy, size_of, wanted


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A miniature project: one real file per kind, plus the things that must not be copied."""
    (tmp_path / "ontology").mkdir()
    (tmp_path / "ontology/aiact-merged.owl").write_text("<owl/>", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/documentation.pdf").write_bytes(b"%PDF-1.4 ...")
    (tmp_path / "docs/documentation.aux").write_text("latex build file", encoding="utf-8")
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts/extract.md").write_text("prompt", encoding="utf-8")
    (tmp_path / "data/extracted/.cache").mkdir(parents=True)
    (tmp_path / "data/extracted/art_005.jsonl").write_text('{"term": "x"}\n', encoding="utf-8")
    (tmp_path / "data/extracted/.cache/abc.json").write_text("[]", encoding="utf-8")
    (tmp_path / "data/gold").mkdir(parents=True)
    (tmp_path / "data/gold/.gitkeep").write_text("", encoding="utf-8")
    (tmp_path / "src/aiact_onto/__pycache__").mkdir(parents=True)
    (tmp_path / "src/aiact_onto/extract.py").write_text("code", encoding="utf-8")
    (tmp_path / "src/aiact_onto/__pycache__/extract.pyc").write_bytes(b"\x00")
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports/metrics.json").write_text("", encoding="utf-8")   # empty placeholder
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_caches_build_files_and_placeholders_are_left_behind():
    assert wanted(Path("src/aiact_onto/extract.py"))
    assert not wanted(Path("data/extracted/.cache/abc.json"))
    assert not wanted(Path("src/aiact_onto/__pycache__/extract.pyc"))
    assert not wanted(Path("docs/documentation.aux"))
    assert not wanted(Path("data/gold/.gitkeep"))


def test_copying_a_directory_skips_what_is_not_wanted(tree):
    written = copy(Path("data"), tree / "out" / "data")
    assert [p.name for p in written] == ["art_005.jsonl"]
    assert not (tree / "out/data/extracted/.cache").exists()


def test_empty_files_are_not_copied(tree):
    assert copy(Path("reports"), tree / "out" / "reports") == []


def test_size_is_reported_in_kb_and_mb(tree):
    big = tree / "big.bin"
    big.write_bytes(b"0" * 2_097_152)
    assert size_of([big]) == "2.0 MB"
    assert size_of([tree / "prompts/extract.md"]).endswith("KB")


def test_bundle_holds_the_required_outputs_and_a_manifest(tree):
    result = CliRunner().invoke(app, ["--force"])
    assert result.exit_code == 0, result.output
    out = tree / "output"
    assert (out / "ontology/aiact-merged.owl").is_file()
    assert (out / "docs/documentation.pdf").is_file()
    assert (out / "prompts/extract.md").is_file()
    assert (out / "data/extracted/art_005.jsonl").is_file()
    manifest = (out / "MANIFEST.md").read_text(encoding="utf-8")
    assert "docs/documentation.pdf" in manifest and "ontology/aiact-merged.owl" in manifest


def test_existing_bundle_is_only_replaced_when_asked(tree):
    CliRunner().invoke(app, ["--force"])
    assert CliRunner().invoke(app, []).exit_code != 0                     # refuses to overwrite
    assert CliRunner().invoke(app, ["--force"]).exit_code == 0


def test_zip_mirrors_the_folder(tree):
    CliRunner().invoke(app, ["--force", "--zip"])
    with zipfile.ZipFile(tree / "output.zip") as bundle:
        names = bundle.namelist()
    assert "output/ontology/aiact-merged.owl" in names
    assert not [n for n in names if ".cache" in n or n.endswith(".pyc")]
