from gt_generation.gt_toolkit import arvo_workspace, cli


def test_subcommand_version_option_is_not_consumed_by_top_level(monkeypatch):
    seen = {}

    def fake_main(argv):
        seen["argv"] = argv
        return 0

    monkeypatch.setattr(arvo_workspace, "main", fake_main)

    assert cli.main([
        "arvo-workspace", "--result-dir", "result", "run",
        "--version", "vulnerable", "--expect", "crash",
    ]) == 0
    assert seen["argv"][-4:] == ["--version", "vulnerable", "--expect", "crash"]
