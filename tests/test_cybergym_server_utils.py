from cybergym.server.server_utils import _post_process_result


def test_post_process_keeps_normal_exit_code():
    result = _post_process_result({"exit_code": 0, "output": "ok"})

    assert result == {"exit_code": 0, "output": "ok"}


def test_post_process_maps_custom_timeout_without_enum_membership_error():
    result = _post_process_result({"exit_code": 300, "output": ""})

    assert result == {
        "exit_code": 0,
        "output": "Timeout waiting for the target binary, not crashed",
    }
