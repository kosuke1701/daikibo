import os
from pathlib import Path
import subprocess
import sys
import textwrap


def test_cpp_index_coordinates_survive_large_rows_in_subprocess():
    repository = Path(__file__).resolve().parents[1]
    source_root = repository / "src"
    script = textwrap.dedent(
        """
        import gc
        import os
        from pathlib import Path

        import daikibo
        from daikibo.indexing import Indexer

        source_root = Path(os.environ["DAIKIBO_SOURCE_ROOT"]).resolve()
        module_path = Path(daikibo.__file__).resolve()
        if os.environ.get("DAIKIBO_TEST_INSTALLED") == "1":
            assert not module_path.is_relative_to(source_root), module_path
        else:
            assert module_path.is_relative_to(source_root), module_path

        filler_lines = 1000
        source_lines = ["// stdlib Point row regression"] * filler_lines
        source_lines.extend(
            [
                "#include <vector>",
                "int helper(int x) {",
                "    return x;",
                "}",
                "int main() {",
                "    return helper(1);",
                "}",
                "",
            ]
        )
        source = "\\n".join(source_lines).encode()
        assert len(source.splitlines()) > 256

        indexer = Indexer.__new__(Indexer)
        indexer.parsers = {}
        expected_symbols = [
            ("helper", "function_definition", 1002, 1004, "int helper(int x) {"),
            ("main", "function_definition", 1005, 1007, "int main() {"),
        ]
        expected_references = [
            ("vector", 1001, "import_candidate"),
            ("helper", 1006, "call_candidate"),
        ]

        for _ in range(100):
            symbols, references, unknown, language = indexer.parse("fixture.cpp", source)
            assert symbols == expected_symbols
            assert references == expected_references
            assert unknown == []
            assert language == "cpp"
            gc.collect()
        """
    )
    environment = os.environ.copy()
    environment["DAIKIBO_SOURCE_ROOT"] = str(source_root)
    source_entry = str(source_root.resolve())
    inherited_pythonpath = environment.get("PYTHONPATH", "")
    if environment.get("DAIKIBO_TEST_INSTALLED") == "1":
        entries = [
            entry
            for entry in inherited_pythonpath.split(os.pathsep)
            if entry and Path(entry).resolve() != source_root.resolve()
        ]
        if entries:
            environment["PYTHONPATH"] = os.pathsep.join(entries)
        else:
            environment.pop("PYTHONPATH", None)
    else:
        entries = [source_entry]
        if inherited_pythonpath:
            entries.append(inherited_pythonpath)
        environment["PYTHONPATH"] = os.pathsep.join(entries)

    result = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", script],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"subprocess exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
