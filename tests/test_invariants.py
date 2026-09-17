"""Repository-level invariant checks.

These are guards, not unit tests. They fail if the project drifts away from the
properties CLAUDE.md calls non-negotiable — including the one that is easiest to
break by accident under schedule pressure: fabricating data so that something
runs.
"""

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"


class TestNoFabricatedData:
    def test_no_data_files_are_committed(self):
        """`data/` holds real sources only, and none are committed."""
        import subprocess

        tracked = subprocess.run(
            ["git", "ls-files", "data"], cwd=REPO, capture_output=True, text=True
        ).stdout.split()
        assert tracked == [], f"files committed under data/: {tracked}"

    def test_data_is_gitignored_in_full(self):
        assert "data/" in (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()

    def test_no_mock_data_generators_in_the_source_tree(self):
        """No faker, no synthesised records, no sample generators."""
        banned = ("import faker", "from faker", "random.choice(", "np.random",
                  "def generate_sample_data", "def make_fake")
        offenders = []
        for path in SRC.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for token in banned:
                if token in text:
                    offenders.append(f"{path.relative_to(REPO)}: {token}")
        assert offenders == [], offenders

    def test_samples_directory_contains_no_invented_records(self):
        """samples/ holds real excerpts only, added once the data is on disk."""
        samples = REPO / "samples"
        if not samples.exists():
            return
        contents = [p.name for p in samples.iterdir() if p.name != "README.md"]
        # Nothing should be here yet; anything that is should have come from a
        # real source via scripts/extract_samples.py.
        assert contents == [], f"unexplained files in samples/: {contents}"


class TestPortableTextIO:
    """Text I/O must name its encoding.

    `Path.read_text()` and `open()` without an ``encoding`` use the platform
    default: UTF-8 on Linux and macOS, but **cp1252 on Windows**. Every document
    in this repository contains non-ASCII characters (em dashes, arrows), so code
    that relies on the default reads fine on one machine and raises
    `UnicodeDecodeError` on another. Python 3.15 changes the default to UTF-8,
    but this project supports 3.10, so it has to be explicit.

    This guard exists because exactly that bug shipped once: the documentation
    check below read its files with the platform encoding and failed on Windows.
    """

    #: Callables that are not text I/O, or whose encoding is not ours to set.
    EXEMPT_PREFIXES = ("Image", "urllib", "urlopen", "zipfile", "archive", "os")

    def _offenders(self, root: Path) -> list[str]:
        """Find text-I/O calls with no explicit encoding.

        Walks the AST rather than the source text, so a call named inside a
        docstring or a comment — such as the one in this class's own docstring —
        is not mistaken for a real one.
        """
        import ast

        found: list[str] = []
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = (node.func.attr if isinstance(node.func, ast.Attribute)
                        else getattr(node.func, "id", None))
                if name not in ("open", "read_text", "write_text"):
                    continue
                rendered = ast.unparse(node.func)
                if rendered.split(".")[0] in self.EXEMPT_PREFIXES:
                    continue
                # A binary mode carries no encoding, and must not be given one.
                if any(isinstance(a, ast.Constant) and isinstance(a.value, str)
                       and "b" in a.value for a in node.args):
                    continue
                if any(kw.arg == "encoding" for kw in node.keywords):
                    continue
                found.append(f"{path.relative_to(REPO)}:{node.lineno}: {rendered}(...)")
        return found

    def test_source_never_relies_on_the_platform_default_encoding(self):
        assert self._offenders(SRC) == []

    def test_tests_never_rely_on_the_platform_default_encoding(self):
        assert self._offenders(Path(__file__).parent) == []

    def test_scripts_never_rely_on_the_platform_default_encoding(self):
        assert self._offenders(REPO / "scripts") == []


class TestModuleBoundaries:
    def test_ingest_never_imports_from_generation(self):
        """The one-directional boundary from the working agreements."""
        offenders = []
        for path in (SRC / "ingest").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for banned in ("generate", "from ..ui", "from ..eval"):
                if f"from ..{banned}" in text or f"import src.{banned}" in text:
                    offenders.append(f"{path.relative_to(REPO)} imports {banned}")
        assert offenders == [], offenders

    def test_analysis_never_imports_from_generation_or_ui(self):
        offenders = []
        for path in (SRC / "analysis").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for banned in ("generate", "ui"):
                if f"from ..{banned}" in text:
                    offenders.append(f"{path.relative_to(REPO)} imports {banned}")
        assert offenders == [], offenders


class TestNoAutonomousClearance:
    def test_no_module_defines_a_clearance_concept(self):
        """Invariant 4: there is no code path to safe, cleared or scheduled."""
        banned = ("def clear_structure", "def mark_safe", "def authorise",
                  "def schedule_maintenance", "SAFETY_CLEARED")
        offenders = []
        for path in SRC.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for token in banned:
                if token in text:
                    offenders.append(f"{path.relative_to(REPO)}: {token}")
        assert offenders == [], offenders

    def test_the_schema_has_no_clearance_column(self):
        # Comments are skipped: the schema legitimately *says* that nothing marks a
        # structure cleared. What must not exist is a column that could.
        lines = [line.split("--", 1)[0].lower()
                 for line in (SRC / "schema.sql").read_text(encoding="utf-8").splitlines()]
        schema = "\n".join(lines)
        for token in ("cleared", "is_safe", "safety_status", "maintenance_scheduled"):
            assert token not in schema


class TestNoSecrets:
    def test_no_api_key_is_hardcoded(self):
        offenders = []
        for path in SRC.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "sk-ant-" in text:
                offenders.append(str(path.relative_to(REPO)))
        assert offenders == []

    def test_the_api_key_is_read_from_the_environment_only(self):
        text = (SRC / "generate" / "drafters.py").read_text(encoding="utf-8")
        assert 'os.environ.get("ANTHROPIC_API_KEY")' in text


class TestDocumentation:
    @pytest.mark.parametrize("name", ["README.md", "HANDOFF.md", "ASSUMPTIONS.md"])
    def test_the_deliverable_documents_exist_and_are_substantial(self, name):
        path = REPO / name
        assert path.exists(), f"{name} is missing"
        assert len(path.read_text(encoding="utf-8")) > 2000, f"{name} is a stub"

    def test_the_readme_states_that_nothing_has_been_run_against_real_data(self):
        text = (REPO / "README.md").read_text(encoding="utf-8")
        assert "No data is on disk" in text
