from .test_contract import task


def test_preparation_emits_a_bounded_batch_before_freezing_the_whole_catalog(tmp_path, monkeypatch):
    from hpgee.acquisition import prepare

    calls = []
    original = prepare.freeze_unfiltered

    def freeze(t):
        calls.append(t["region"])
        return original(t)

    monkeypatch.setattr(prepare, "freeze_unfiltered", freeze)

    def write(path, header, records):
        iterator = iter(records)
        next(iterator)
        assert len(calls) <= 2
        return dict(tasks=3, required_cells=3)

    monkeypatch.setattr(prepare, "write_manifest", write)
    tasks = [dict(task(), region="r" + str(i), observation="o" + str(i)) for i in range(3)]
    prepare.prepare_tasks(
        None, tasks, tmp_path / "target.gz", tmp_path, project="test", quality_workers=2
    )
