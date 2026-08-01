import ast
from pathlib import Path


def test_python_user_visible_strings_do_not_contain_replacement_question_marks():
    app_dir = Path(__file__).resolve().parent.parent / "app"
    failures = []
    for path in app_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "???" in node.value:
                    failures.append(f"{path.relative_to(app_dir)}:{node.lineno}")
    assert failures == [], "corrupted user-visible strings: " + ", ".join(failures)
