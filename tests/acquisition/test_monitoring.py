from types import SimpleNamespace


def test_usage_run_accepts_bounded_client_and_keeps_original_start(tmp_path):
    from hpgee.accounting.run import UsageRun

    client = SimpleNamespace()
    first = UsageRun(
        tmp_path, project="test-project", tag_prefix="df-test-", credentials=None, client=client
    )
    again = UsageRun(
        tmp_path, project="test-project", tag_prefix="df-test-", credentials=None, client=client
    )
    assert again.reader.start == first.reader.start
    assert again.reader._client is client
