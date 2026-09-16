"""
Build the self-contained deployment bundle for the verified /query service.

Produces
    dist/genemco-query-service-<YYYYMMDD>-<commit>.tar.gz
    dist/genemco-query-service-<YYYYMMDD>-<commit>.tar.gz.sha256

Contents: only the project modules api.query_app actually imports, the runtime
requirements, the golden-record data the service reads, the deployment docs,
VERSION and MANIFEST.sha256. No .env, no credentials, no ingestion stack.

The build refuses to run if the bundled code differs from the last commit, or if
api.query_app imports a project module the bundle would leave out.

usage:  python -m scripts.build_query_bundle
"""
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PREFIX = "genemco-query-service"

CODE = [
    "api/__init__.py", "api/query.py", "api/query_app.py",
    "core/__init__.py", "core/config.py", "core/logging_utils.py", "core/schemas.py",
    "faq/__init__.py", "faq/validator.py",
    "ingestion/__init__.py", "ingestion/golden_record.py",
    "retrieval/__init__.py", "retrieval/verified_query.py",
]
DOCS = ["requirements-query.txt", "docs/DEPLOYMENT.md", "docs/QUERY_API.md"]
DATA = ["data/search_layer/catalog_golden_records.jsonl"]
DATA_GLOBS = ["data/golden_records/*.json"]
OPTIONAL = ["data/manual_urls.json"]
EMPTY_DIRS = ["logs", "data/shopify_raw"]    # core.config creates these at import


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True,
                          capture_output=True, text=True).stdout.strip()


def _imported_project_modules() -> set[str]:
    probe = (
        "import sys; from pathlib import Path; root = Path.cwd().resolve(); import api.query_app; "
        "print('\\n'.join(sorted({Path(m.__file__).resolve().relative_to(root).as_posix() "
        "for m in list(sys.modules.values()) if getattr(m, '__file__', None) "
        "and str(Path(m.__file__).resolve()).startswith(str(root)) and 'venv' not in m.__file__})))"
    )
    out = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, check=True,
                         capture_output=True, text=True).stdout
    return {line.strip() for line in out.splitlines() if line.strip().endswith(".py")}


def _info(name: str, size: int, is_dir: bool = False) -> tarfile.TarInfo:
    ti = tarfile.TarInfo(name)
    ti.size = 0 if is_dir else size
    ti.type = tarfile.DIRTYPE if is_dir else tarfile.REGTYPE
    ti.mode = 0o755 if is_dir else 0o644
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = "root"
    ti.mtime = int(time.time())
    return ti


def main() -> None:
    missing = sorted(_imported_project_modules() - set(CODE))
    if missing:
        sys.exit(f"ABORT: api.query_app imports modules not in the bundle: {missing}")

    dirty = _git("status", "--porcelain", "--", *CODE)
    if dirty:
        sys.exit(f"ABORT: bundled code differs from the last commit:\n{dirty}")

    files = CODE + DOCS + DATA
    for pattern in DATA_GLOBS:
        files += sorted(p.relative_to(ROOT).as_posix() for p in ROOT.glob(pattern))
    files += [f for f in OPTIONAL if (ROOT / f).is_file()]
    absent = [f for f in files if not (ROOT / f).is_file()]
    if absent:
        sys.exit(f"ABORT: missing files: {absent}")

    commit = _git("rev-parse", "--short", "HEAD")
    stamp = datetime.now(timezone.utc)
    name = f"{PREFIX}-{stamp:%Y%m%d}-{commit}"
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    tar_path = dist / f"{name}.tar.gz"

    catalog_records = sum(1 for _ in open(ROOT / DATA[0], encoding="utf-8"))
    authoritative = sum(1 for f in files if f.startswith("data/golden_records/"))
    version = (
        f"bundle        {name}\n"
        f"commit        {commit}\n"
        f"built_utc     {stamp.isoformat(timespec='seconds')}\n"
        f"python        3.12 (built and tested with {sys.version.split()[0]})\n"
        f"records       {catalog_records} catalog, {authoritative} authoritative\n"
        f"entrypoint    python -m api.query_app\n"
    )

    manifest_lines = []
    with tarfile.open(tar_path, "w:gz", compresslevel=9) as tar:
        root_dir = f"{PREFIX}/"
        tar.addfile(_info(root_dir, 0, is_dir=True))
        for d in EMPTY_DIRS:
            tar.addfile(_info(f"{root_dir}{d}/", 0, is_dir=True))
        for rel in files:
            data = (ROOT / rel).read_bytes()
            tar.addfile(_info(f"{root_dir}{rel}", len(data)), io.BytesIO(data))
            manifest_lines.append(f"{hashlib.sha256(data).hexdigest()}  {rel}")
        vbytes = version.encode()
        tar.addfile(_info(f"{root_dir}VERSION", len(vbytes)), io.BytesIO(vbytes))
        manifest_lines.append(f"{hashlib.sha256(vbytes).hexdigest()}  VERSION")
        mbytes = ("\n".join(manifest_lines) + "\n").encode()
        tar.addfile(_info(f"{root_dir}MANIFEST.sha256", len(mbytes)), io.BytesIO(mbytes))

    digest = hashlib.sha256(tar_path.read_bytes()).hexdigest()
    # Bytes, not text: a CRLF written on Windows makes `sha256sum -c` fail on Linux.
    (dist / f"{tar_path.name}.sha256").write_bytes(f"{digest}  {tar_path.name}\n".encode())

    print(json.dumps({
        "bundle": tar_path.name,
        "bytes": tar_path.stat().st_size,
        "sha256": digest,
        "files": len(files) + 2,
        "catalog_records": catalog_records,
        "authoritative_records": authoritative,
    }, indent=2))


if __name__ == "__main__":
    main()
