"""Тесты проверяльщика code_check: всё в процессе, исходники - строки."""
import ast
import io
import json
import sys
from pathlib import Path

import code_check
import pytest
from code_check import Config, Layer, check_source, main

HERE = Path(__file__).parent
BODY = "    x = 1\n"


def rules(path, source, config=None):
    return [v["rule"] for v in check_source(path, source, config or Config())]


def run(argv, stdin_text=None):
    out = io.StringIO()
    code = main(argv, stdin=io.StringIO(stdin_text or ""), stdout=out)
    return code, out.getvalue()


def ifs(n):
    return "def f(a):\n" + "    if a:\n        pass\n" * n


def nested_for(n):
    lines = ["def f(a):"]
    for i in range(n):
        lines.append("    " * (i + 1) + "for i in a:")
    lines.append("    " * (n + 1) + "pass")
    return "\n".join(lines) + "\n"


CASES = [
    ("function-length", "x.py", "def f():\n" + BODY * 80),
    ("complexity", "x.py", ifs(20)),
    ("nesting", "x.py", nested_for(5)),
    ("params", "x.py", "def f(a, b, c, d, e, g):\n    pass\n"),
    ("test-conditional", "test_x.py", "def test_a(a):\n    if a:\n        pass\n"),
    ("test-loop", "test_x.py", "def test_b(a):\n    for i in a:\n        pass\n"),
    ("test-asserts", "test_x.py", "def test_c():\n" + "    assert 1\n" * 6),
    ("test-skip-reason", "test_x.py", "@pytest.mark.skip\ndef test_d():\n    pass\n"),
]


@pytest.mark.parametrize(("rule", "path", "source"), CASES)
def test_each_rule_catches_its_violation(rule, path, source):
    assert rules(path, source) == [rule]


def test_clean_source_gives_no_violations_and_code_0():
    source = "def f(a):\n    return a\n"
    assert check_source("x.py", source, Config()) == []
    assert run(["--stdin-filename", "x.py"], source) == (0, "[]\n")


def test_function_length_skips_docstring_comments_and_blanks():
    padding = '    """doc\n\n    more\n    """\n' + "    # c\n\n" * 8
    long = "def f():\n" + padding + BODY * 80
    short = "def f():\n" + padding + BODY * 79
    message = check_source("x.py", long, Config())[0]["message"]
    assert message == "функция f длиннее 80 строк (81)"
    assert check_source("x.py", short, Config()) == []


def test_complexity_limit_and_nested_def_counted_separately():
    inner = "def f(a):\n    def g(b):\n" + "        if b:\n            pass\n" * 25
    assert rules("x.py", ifs(19)) == []
    assert check_source("x.py", ifs(20), Config())[0]["message"] == "функция f: сложность 21 при лимите 20"
    assert [v["message"] for v in check_source("x.py", inner, Config())] == [
        "функция g: сложность 26 при лимите 20"]


def test_nesting_five_fors_flagged_elif_chain_not():
    elif_chain = "def f(a):\n    if a == 0:\n        pass\n" + "    elif a == 1:\n        pass\n" * 5
    assert check_source("x.py", nested_for(5), Config())[0]["message"] == "функция f: вложенность 5 при лимите 4"
    assert rules("x.py", elif_chain) == []


def test_params_ignore_self_and_varargs():
    method = "class A:\n    def f(self, a, b, c, d, e):\n        pass\n"
    star = "def f(a, b, c, d, e, *args, **kw):\n    pass\n"
    plain = "def f(a, b, c, d, e, g):\n    pass\n"
    assert rules("x.py", method) == []
    assert rules("x.py", star) == []
    assert check_source("x.py", plain, Config())[0]["message"] == "функция f: 6 параметров при лимите 5"


SKIPS = [
    ("@pytest.mark.skip(reason='flaky API')\ndef test_a():\n    pass\n", []),
    ("@pytest.mark.skipif(True)\ndef test_a():\n    pass\n", ["test-skip-reason"]),
    ("@unittest.skip('')\ndef test_a():\n    pass\n", ["test-skip-reason"]),
    ("def test_a():\n    pytest.skip()\n", ["test-skip-reason"]),
]


@pytest.mark.parametrize(("source", "expected"), SKIPS)
def test_skip_reason_required(source, expected):
    assert rules("test_x.py", source) == expected


def test_test_rules_apply_only_to_test_files():
    source = "def test_a(a):\n    if a:\n        pass\n"
    assert rules("x.py", source) == []
    assert rules("tests/helper.py", source) == ["test-conditional"]
    assert rules("pkg/x_test.py", source) == ["test-conditional"]


LAYER = Config(layers=(Layer(files="guard/**", forbid=("hooks",)),))


@pytest.mark.parametrize("source", ["import hooks\n", "from hooks import x\n", "from hooks.sub import x\n"])
def test_layer_import_forbidden_inside_layer(source):
    found = check_source("guard/a.py", source, LAYER)
    assert [v["rule"] for v in found] == ["layer-import"]
    assert found[0]["message"].startswith("guard.a не импортирует hooks")
    assert found[0]["message"].endswith(": запрет слоя guard/**")


def test_layer_allows_same_import_outside_and_similar_prefix():
    assert check_source("tools/a.py", "import hooks\n", LAYER) == []
    assert check_source("guard/a.py", "import hooks_extra\n", LAYER) == []


def test_layer_resolves_relative_import():
    cfg = Config(layers=(Layer(files="pkg/**", forbid=("pkg.hooks",)),))
    assert rules("pkg/a.py", "from . import hooks\n", cfg) == ["layer-import"]
    assert rules("pkg/a.py", "from . import other\n", cfg) == []
    assert rules("pkg/a.py", "from .hooks import x\n", cfg) == ["layer-import"]


def test_stdin_filename_reports_given_path_and_exit_codes():
    code, out = run(["--stdin-filename", "pkg/mod.py"], ifs(20))
    found = json.loads(out)
    assert code == 1
    assert [(v["path"], v["rule"], v["line"]) for v in found] == [("pkg/mod.py", "complexity", 1)]
    assert run(["--stdin-filename", "pkg/mod.py"], "x = 1\n")[0] == 0


def test_paths_are_read_from_disk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.py").write_text(ifs(20), encoding="utf-8")
    code, out = run(["a.py"])
    assert (code, json.loads(out)[0]["path"]) == (1, "a.py")


@pytest.mark.parametrize(("name", "source"), [
    ("ui/app.mjs", "export const x = 1;\n"),
    ("install.sh", "echo hi\n"),
    ("ui/APP.MJS", "export const x = 1;\n"),
])
def test_non_python_stdin_is_skipped(name, source):
    assert run(["--stdin-filename", name], source) == (0, "[]\n")


def test_non_python_paths_are_skipped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.sh").write_text("echo hi\n", encoding="utf-8")
    (tmp_path / "b.py").write_text(ifs(20), encoding="utf-8")
    code, out = run(["a.sh", "b.py"])
    assert (code, [v["path"] for v in json.loads(out)]) == (1, ["b.py"])


def test_config_limits_and_layers_loaded(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"limits": {"params": 2}, "layers": [{"files": "g/**", "forbid": ["h"]}]}))
    cfg = code_check.load_config(str(path))
    assert (cfg.params, cfg.layers) == (2, (Layer("g/**", ("h",)),))
    assert code_check.load_config(None) == Config()


def test_example_config_loads():
    cfg = code_check.load_config(str(HERE / "code_check.example.json"))
    assert cfg.layers == (Layer("guard/**", ("hooks", "runner")),)


def test_bad_inputs_give_code_2(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert run(["--config", str(bad), "--stdin-filename", "x.py"], "x=1\n")[0] == 2
    assert run(["--stdin-filename", "x.py", "a.py"], "x=1\n")[0] == 2
    assert run([])[0] == 2
    assert run([str(tmp_path / "missing.py")])[0] == 2


def test_syntax_error_is_parse_error_with_code_1():
    code, out = run(["--stdin-filename", "x.py"], "def f(:\n")
    found = json.loads(out)
    assert (code, found[0]["rule"], found[0]["line"]) == (1, "parse-error", 1)
    assert found[0]["message"].startswith("файл не разбирается: ")


def test_hints_cover_every_rule():
    hints = json.loads((HERE / "hints.json").read_text(encoding="utf-8"))
    expected = {rule for rule, _, _ in CASES} | {"layer-import", "parse-error", "*"}
    assert expected <= set(hints)


def import_roots(tree):
    imports = [n for n in ast.walk(tree) if isinstance(n, ast.Import)]
    froms = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    names = [a.name for n in imports for a in n.names] + [n.module or "" for n in froms]
    return {name.split(".")[0] for name in names}


def test_module_imports_only_stdlib():
    tree = ast.parse((HERE / "code_check.py").read_text(encoding="utf-8"))
    assert import_roots(tree) <= set(sys.stdlib_module_names)
