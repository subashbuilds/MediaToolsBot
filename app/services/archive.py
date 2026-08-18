from __future__ import annotations

import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path


def _safe_member(base: Path, member: str) -> Path:
    target = (base / member).resolve()
    base = base.resolve()
    if target != base and base not in target.parents:
        raise RuntimeError(f"Unsafe archive path: {member}")
    return target


def extract_archive(path: Path, outdir: Path) -> Path:
    outdir.mkdir(parents=True, exist_ok=True)
    suffix = path.name.lower()
    if suffix.endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                _safe_member(outdir, n)
            z.extractall(outdir)
        return outdir
    if any(suffix.endswith(x) for x in (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
        with tarfile.open(path) as t:
            for m in t.getmembers():
                _safe_member(outdir, m.name)
            t.extractall(outdir, filter="data")
        return outdir
    exe = shutil.which("7zz") or shutil.which("7z") or shutil.which("7za")
    if not exe:
        exe = shutil.which("unar")
        if exe:
            subprocess.run([exe, "-f", "-o", str(outdir), str(path)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return outdir
        raise RuntimeError("No 7z/unar extractor is installed")
    subprocess.run([exe, "x", "-y", f"-o{outdir}", str(path)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return outdir
