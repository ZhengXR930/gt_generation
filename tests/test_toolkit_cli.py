from gt_generation.gt_toolkit import arvo_workspace, cli, context_trace


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


def test_context_subcommand_routes_to_context_trace(monkeypatch):
    seen = {}

    def fake_main(argv):
        seen["argv"] = argv
        return 0

    monkeypatch.setattr(context_trace, "main", fake_main)

    assert cli.main([
        "context", "--for-result-dir", "result", "--timeout", "5",
    ]) == 0
    assert seen["argv"] == ["--for-result-dir", "result", "--timeout", "5"]
