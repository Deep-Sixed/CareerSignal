import sys

from system import entrypoint


def test_version_command_reports_runtime_without_database(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["careersignal", "version"])
    entrypoint.main()
    output = capsys.readouterr().out
    assert "CareerSignal " in output
    assert "Python " in output
    assert "SQLite " in output
    assert "Minimum SQLite: 3.38.0" in output
    assert "WAL-reset fix: " in output
    assert not (tmp_path / "var").exists()
