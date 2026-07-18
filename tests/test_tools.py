"""Tests for sakicode.tools. No network access involved."""

from sakicode import tools


def test_write_then_read_file_roundtrip(tmp_path):
    target = tmp_path / "hello.txt"
    result = tools.write_file(str(target), "line one\nline two\n")
    assert "Error" not in result
    content = tools.read_file(str(target))
    assert "1\tline one" in content
    assert "2\tline two" in content


def test_read_file_missing_returns_error(tmp_path):
    result = tools.read_file(str(tmp_path / "nope.txt"))
    assert result.startswith("Error:")


def test_edit_file_success(tmp_path):
    target = tmp_path / "code.py"
    target.write_text("def foo():\n    return 1\n")
    result = tools.edit_file(str(target), "return 1", "return 2")
    assert "Error" not in result
    assert target.read_text() == "def foo():\n    return 2\n"


def test_edit_file_old_string_not_found(tmp_path):
    target = tmp_path / "code.py"
    target.write_text("def foo():\n    return 1\n")
    result = tools.edit_file(str(target), "return 99", "return 2")
    assert "not found" in result
    assert target.read_text() == "def foo():\n    return 1\n"  # unchanged


def test_edit_file_old_string_not_unique(tmp_path):
    target = tmp_path / "code.py"
    target.write_text("x = 1\ny = 1\n")
    result = tools.edit_file(str(target), "1", "2")
    assert "must be unique" in result
    assert target.read_text() == "x = 1\ny = 1\n"  # unchanged


def test_glob_finds_expected_files(tmp_path):
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("")
    (tmp_path / "c.txt").write_text("")
    result = tools.glob("*.py", path=str(tmp_path))
    assert "a.py" in result
    assert "b.py" in result
    assert "c.txt" not in result


def test_grep_returns_file_and_line(tmp_path):
    (tmp_path / "one.py").write_text("hello\nneedle here\nbye\n")
    (tmp_path / "two.py").write_text("nothing\n")
    result = tools.grep("needle", path=str(tmp_path))
    assert "one.py:2: needle here" in result
    assert "two.py" not in result


def test_run_bash_echoes_output():
    result = tools.run_bash("echo hello saki")
    assert "hello saki" in result


def test_run_bash_nonzero_exit_code_does_not_raise():
    result = tools.run_bash("echo oops >&2; exit 3")
    assert "oops" in result
    assert "exit code: 3" in result
