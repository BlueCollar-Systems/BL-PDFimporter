import ast
from pathlib import Path

tree = ast.parse(Path("pdf_vector_importer/bl_import_engine.py").read_text(encoding="utf-8"))
outer = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_iter_pages_for_import")
tries = [node for node in ast.walk(outer) if isinstance(node, ast.Try)]
assert tries, "streaming adapter has no try/finally"
assert any(
    isinstance(call.func, ast.Attribute) and call.func.attr == "close"
    for block in tries for statement in block.finalbody
    for call in ast.walk(statement) if isinstance(call, ast.Call)
), "streaming adapter does not close its owned iterator in finally"
