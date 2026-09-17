"""Repository-level invariant checks.

These are guards, not unit tests. They fail if the project drifts away from the
properties CLAUDE.md calls non-negotiable — including the one that is easiest to
break by accident under schedule pressure: fabricating data so that something
runs.
"""

from fnmatch import fnmatch
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

    #: The only file names scripts/extract_samples.py produces.
    SAMPLE_PATTERNS = ("nbi_*.txt", "nbe_*_*.xml", "codebrim_annotation.xml")

    def test_samples_directory_contains_no_invented_records(self):
        """samples/ holds real excerpts only.

        Before the data was on disk this asserted the directory was empty. Now
        that it is populated, emptiness is the wrong test: what matters is that
        every file here came out of a real source via
        ``scripts/extract_samples.py`` and not out of someone's head. So the name
        must be one the extractor produces, and — when the corresponding source
        is on this machine — the content must actually be found in it.
        """
        samples = REPO / "samples"
        if not samples.exists():
            return
        files = [p for p in samples.iterdir() if p.name != "README.md"]
        unexplained = [
            p.name for p in files
            if not any(fnmatch(p.name, pattern) for pattern in self.SAMPLE_PATTERNS)
        ]
        assert unexplained == [], (
            f"unexplained files in samples/: {unexplained}. Every sample must be "
            "produced by scripts/extract_samples.py from a real source."
        )

    def test_every_nbi_sample_line_appears_in_the_real_source(self):
        """A committed NBI sample must be a verbatim excerpt, not a paraphrase."""
        for sample in sorted((REPO / "samples").glob("nbi_*.txt")):
            year = sample.stem.split("_")[1]
            directory = REPO / "data" / "raw" / "nbi" / year
            sources = sorted(directory.glob("*.txt")) if directory.exists() else []
            if not sources:
                pytest.skip(f"NBI {year} is not on this machine; cannot verify")
            lines = sample.read_text(encoding="utf-8").splitlines()
            # The extractor takes the header plus the first N records, so the
            # excerpt must match the head of the source line for line.
            with open(sources[0], encoding="utf-8", errors="replace") as handle:
                head = [next(handle).rstrip("\r\n") for _ in range(len(lines))]
            assert lines == head, f"{sample.name} is not a verbatim excerpt of {sources[0].name}"

    def test_every_nbe_sample_record_appears_in_the_real_source(self):
        """Same for NBE, compared field by field.

        The extractor re-serialises the records it keeps, so the bytes differ
        from the source even though the content does not. Each record is
        therefore matched as a field dictionary against the real file.
        """
        import zipfile
        from xml.etree import ElementTree as ET

        def fields(node):
            return {child.tag: (child.text or "").strip() for child in node}

        for sample in sorted((REPO / "samples").glob("nbe_*_*.xml")):
            _, year, state = sample.stem.split("_")
            directory = REPO / "data" / "raw" / "nbe" / year / state
            archives = sorted(directory.glob("*.zip")) if directory.exists() else []
            if not archives:
                pytest.skip(f"NBE {year} {state} is not on this machine; cannot verify")
            with zipfile.ZipFile(archives[0]) as archive:
                name = [n for n in archive.namelist() if n.lower().endswith(".xml")][0]
                source_root = ET.fromstring(archive.read(name))
            real = [fields(node) for node in source_root]
            sample_records = [fields(node) for node in ET.parse(sample).getroot()]
            assert sample_records, f"{sample.name} contains no records"
            for record in sample_records:
                assert record in real, (
                    f"{sample.name} contains a record that is not in "
                    f"{archives[0].name}: {record}"
                )


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
