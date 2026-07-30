from gt_generation.gt_toolkit.validate import Report, _check_edges


def test_later_referenced_independent_root_does_not_warn():
    fine_trace = [
        {"step": 1},
        {"step": 2},
        {
            "step": 3,
            "depends_on": [
                {"on": 1, "type": "data", "via": "destination"},
                {"on": 2, "type": "data", "via": "attacker_input"},
                {"on": 2, "type": "control", "via": "length > capacity"},
            ],
        },
    ]
    report = Report("ground_truth.json")

    _check_edges(fine_trace, report)

    assert report.errors == []
    assert report.warnings == []


def test_unreferenced_nonfirst_root_still_warns():
    fine_trace = [
        {"step": 1},
        {"step": 2},
        {
            "step": 3,
            "depends_on": [
                {"on": 1, "type": "data", "via": "value"},
                {"on": 1, "type": "control", "via": "length > capacity"},
            ],
        },
    ]
    report = Report("ground_truth.json")

    _check_edges(fine_trace, report)

    assert report.errors == []
    assert report.warnings == [
        "fine_trace[1] has no depends_on edge "
        "(source->sink association missing)"
    ]
