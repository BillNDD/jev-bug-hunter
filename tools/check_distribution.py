"""Offline install/resource/CLI checks plus source-archive test completeness."""
from pathlib import Path
import os
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile


def check(folder):
    folder = Path(folder).resolve()
    wheel, = folder.glob("*.whl")
    archive, = folder.glob("*.tar.gz")
    required = {"tests/__init__.py", "tests/support.py", "tests/smoke.py"}
    with tarfile.open(archive) as stream:
        members = {m.name.split("/", 1)[1] for m in stream.getmembers()
                   if m.isfile() and "/" in m.name}
        if not required <= members:
            raise AssertionError("source archive omits test support")
    with zipfile.ZipFile(wheel) as stream:
        if "bug_hunter/question_pack.json" not in stream.namelist():
            raise AssertionError("wheel omits question pack")
    env = dict(os.environ)
    for key in ("TYPESAFE_API_KEY", "PYTHONPATH", "PYTHONHOME"):
        env.pop(key, None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        install = root / "environment"
        venv.EnvBuilder(with_pip=True).create(install)
        python = install / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        def run(args, expected=0):
            result = subprocess.run([str(python), *map(str, args)], cwd=root, env=env,
                                    capture_output=True, timeout=120)
            if result.returncode != expected:
                raise AssertionError("installed-package check failed")
        run(["-m", "pip", "install", "--no-index", "--no-deps", "--no-compile", wheel])
        run(["-I", "-c", "from importlib.resources import files; import json, bug_hunter; "
             "json.loads(files('bug_hunter').joinpath('question_pack.json').read_text(encoding='utf-8')); "
             "assert bug_hunter.__version__ == '0.5.0b1'"])
        run(["-I", "-m", "bug_hunter", "--help"])
        run(["-I", "-m", "bug_hunter", "absent.py"], expected=3)
        run(["-I", "-m", "bug_hunter", "absent.py", "--scope", "standalone"], expected=2)
        target = root / "unit.py"
        target.write_text("raise RuntimeError('never execute target')\n", encoding="utf-8")
        run(["-I", "-m", "bug_hunter", target, "--scope", "standalone"], expected=2)
    print("Wheel installation, resources, CLI, and source-archive test files: passed; no provider calls.")


if __name__ == "__main__":
    check(sys.argv[1])
