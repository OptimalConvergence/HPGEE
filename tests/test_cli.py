import json

from hpgee.cli.sampling import main


def test_catalog_stats_cli_needs_no_earth_engine_authentication(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text(
        f"""
auth:
  project: placeholder-project
run:
  output_dir: {tmp_path / "runs"}
  monitoring: {{enabled: false}}
catalog:
  enabled: true
  path: {tmp_path / "catalog.sqlite"}
""",
        encoding="utf-8",
    )
    assert main(["catalog", "stats", str(config)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scenes"] == 0
    assert payload["size_bytes"] > 0
    assert "auth" not in payload


def test_unified_cli_help_names_hpgee_commands(capsys):
    import pytest
    from hpgee.cli.main import main as hpgee_main

    for command, usage in [("metadata", "usage: hpgee metadata"), ("compute", "usage: hpgee compute"),
                           ("workflow", "usage: hpgee workflow"), ("benchmark", "usage: hpgee benchmark")]:
        with pytest.raises(SystemExit) as exit_info:
            hpgee_main([command, "--help"])
        assert exit_info.value.code == 0
        assert capsys.readouterr().out.startswith(usage)
