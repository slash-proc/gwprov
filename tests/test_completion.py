import os
import subprocess
from types import SimpleNamespace

from gwprov.cli.main import _completion_bash


def _bash_completion_script(capsys):
    assert _completion_bash(SimpleNamespace()) == 0
    return capsys.readouterr().out


def _run_bash(script, *arguments, env=None):
    command = f'{script}\n_gwprov_complete_dirs "$1"\nprintf \'%s\\n\' "${{COMPREPLY[@]}}"'
    return subprocess.run(
        ["bash", "-c", command, "bash", *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.splitlines()


def test_path_completion_normalizes_repeated_separators(tmp_path, capsys):
    root = tmp_path / "root"
    child = root / "directory with spaces" / "subdirectory"
    child.mkdir(parents=True)
    (child / "file.bin").touch()
    input_path = str(root / "directory with spaces") + "//sub"

    candidates = _run_bash(_bash_completion_script(capsys), input_path)

    assert candidates == [str(child)]
    assert "//" not in candidates[0]


def test_path_completion_preserves_intentional_double_root(tmp_path, capsys):
    child = tmp_path / "folder"
    child.mkdir()
    input_path = "//" + str(child).lstrip("/")[: -len("folder")] + "fol"

    candidates = _run_bash(_bash_completion_script(capsys), input_path)

    assert candidates == ["//" + str(child).lstrip("/")]


def test_path_completion_handles_paths_with_spaces(tmp_path, capsys):
    child = tmp_path / "a directory"
    child.mkdir()
    (child / "a file.zip").touch()

    candidates = _run_bash(_bash_completion_script(capsys), str(child) + "/a f")

    assert candidates == [str(child / "a file.zip")]


def test_bash_project_completion_emits_one_candidate_per_line(tmp_path, capsys):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gwprov = fake_bin / "gwprov"
    fake_gwprov.write_text(
        '#!/bin/sh\nprintf \'%s\' \'{"projects":[{"project":"smw"},' '{"project":"zelda3"}]}\'\n',
        encoding="utf-8",
    )
    fake_gwprov.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    script = _bash_completion_script(capsys)

    candidates = subprocess.run(
        ["bash", "-c", f"{script}\n_gwprov_project_names"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.splitlines()

    assert candidates == ["smw", "zelda3"]
