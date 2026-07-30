from pathlib import Path

from gt_generation.gt_toolkit import prepare


def test_public_arvo_source_predicate():
    assert prepare.is_arvo_sample(
        {"sample_id": "cybergym_10143", "source_family": "arvo"}
    )
    assert not prepare.is_arvo_sample(
        {"sample_id": "secbench_01", "source_family": "secbench"}
    )


def test_resume_source_reuses_existing_tree(tmp_path, monkeypatch):
    source = tmp_path / "_work" / "src"
    source.mkdir(parents=True)
    (source / "README").write_text("exact source")

    def unexpected_pull(_image):
        raise AssertionError("existing source must not pull an image")

    monkeypatch.setattr(prepare, "_pull", unexpected_pull)

    report = prepare.ensure_arvo_resume_source(
        {"sample_id": "arvo_10143", "source_family": "arvo"}, tmp_path
    )

    assert report == {
        "prepared": True,
        "source": str(source),
        "reused": True,
    }


def test_resume_source_is_atomic_and_removes_container(tmp_path, monkeypatch):
    commands = []

    def fake_pull(image):
        assert image == "n132/arvo:10143-vul"
        return True

    def fake_sh(command, timeout=None):
        del timeout
        commands.append(command)
        if command[:2] == ["docker", "create"]:
            return prepare.subprocess.CompletedProcess(command, 0, "container-id\n", "")
        if command[:2] == ["docker", "cp"]:
            destination = Path(command[-1])
            (destination / "lib").mkdir()
            (destination / "lib" / "source.c").write_text("vulnerable source")
        return prepare.subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(prepare, "_pull", fake_pull)
    monkeypatch.setattr(prepare, "_sh", fake_sh)

    report = prepare.ensure_arvo_resume_source(
        {"sample_id": "arvo_10143", "source_family": "arvo"}, tmp_path
    )

    assert report["prepared"] is True
    assert report["reused"] is False
    assert (tmp_path / "_work" / "src" / "lib" / "source.c").is_file()
    assert ["docker", "rm", "-f", "container-id"] in commands
    assert not list((tmp_path / "_work").glob(".resume-src-*"))
