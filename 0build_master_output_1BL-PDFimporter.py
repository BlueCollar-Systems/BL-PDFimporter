
#!/usr/bin/env python3
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from repo_context_builder_core import main_with_preset

PRESET = {
  "title": "LLM Context Pack \u2014 Blender PDF Importer",
  "config_paths": [
    "README.md",
    "pyproject.toml",
    "requirements-dev.txt",
    ".gitignore"
  ],
  "script_paths": [
    "0build_master_output.py",
    "0build_master_output.cmd",
    "build_release.py"
  ],
  "source_roots": [
    "pdf_vector_importer",
    "blender_pdf_vector_importer"
  ],
  "test_roots": [
    "tests"
  ],
  "dependency_files": [
    "pyproject.toml",
    "requirements-dev.txt"
  ],
  "expected_files": {
    "expected_everywhere": [
      "README.md",
      "pyproject.toml"
    ],
    "expected_some_envs": [
      ".github/workflows",
      "dist"
    ]
  },
  "exclude_dir_names": [
    ".git",
    "__pycache__",
    ".ruff_cache",
    "dist",
    "dev_logs",
    ".venv",
    "venv",
    ".pytest_cache",
    "benchmarks",
    "*.egg-info"
  ],
  "exclude_file_names": [],
  "exclude_suffixes": [
    ".pyc"
  ],
  "include_extensions": [
    ".bat",
    ".c",
    ".cfg",
    ".cmake",
    ".cmd",
    ".conf",
    ".cpp",
    ".css",
    ".dart",
    ".go",
    ".gradle",
    ".h",
    ".hpp",
    ".htm",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".kt",
    ".kts",
    ".lua",
    ".md",
    ".php",
    ".plist",
    ".ps1",
    ".py",
    ".r",
    ".rb",
    ".rs",
    ".sample",
    ".scss",
    ".sh",
    ".sql",
    ".svg",
    ".swift",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml"
  ],
  "tree_full_depth_roots": [
    "pdf_vector_importer",
    "blender_pdf_vector_importer",
    "tests",
    ".github"
  ],
  "tree_shallow_depth_roots": {
    "benchmarks": 2,
    "dist": 2,
    ".git": 1,
    ".ruff_cache": 1
  },
  "default_tree_depth": 2,
  "navigation_grep_patterns": [
    "\\b(bpy\\.ops|register\\(|menu_func|append\\()"
  ],
  "navigation_roots": [
    "pdf_vector_importer",
    "blender_pdf_vector_importer"
  ],
  "check_commands": [
    [
      "python",
      "-m",
      "pytest",
      "-q"
    ]
  ]
}

if __name__ == "__main__":
    raise SystemExit(main_with_preset(PRESET))
