from __future__ import annotations

import os
import re
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

ARCHIVE_SUFFIXES = (
    ".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz",
    ".7z", ".rar", ".cab", ".iso",
)

# Extraction is bounded so a decompression bomb cannot fill the container disk.
MAX_EXTRACT_BYTES = int(os.environ.get("MAX_EXTRACT_BYTES", str(8 * 1024 * 1024 * 1024)))
MAX_EXTRACT_MEMBERS = int(os.environ.get("MAX_EXTRACT_MEMBERS", "200000"))


def _safe_member(base: Path, member: str) -> Path:
    target = (base / member).resolve()
    base = base.resolve()
    if target != base and base not in target.parents:
        raise RuntimeError(f"Unsafe archive path: {member}")
    return target


def is_archive(path: Path) -> bool:
    name = path.name.lower()
    if name.endswith(ARCHIVE_SUFFIXES):
        return True
    return bool(re.search(r"(?:\.part\d+|\.\d{3})\.(?:rar|zip|7z)$", name)) or bool(re.search(r"\.(?:rar|zip|7z)\.\d{3}$", name))


def multipart_info(path: Path) -> tuple[str, int] | None:
    """Return (series key, part number) for common split archive naming schemes."""
    name = path.name
    lower = name.lower()
    m = re.match(r"^(.*)\.part(\d+)\.(rar|zip|7z)$", lower)
    if m:
        return (m.group(1), int(m.group(2)))
    m = re.match(r"^(.*)\.(rar|zip|7z)\.(\d{3})$", lower)
    if m:
        return (m.group(1), int(m.group(3)) + 1)
    m = re.match(r"^(.*)\.(\d{3})$", lower)
    if m and (m.group(2) == "001" or path.with_name(m.group(1) + ".001").exists()):
        return (m.group(1), int(m.group(2)))
    return None


def _seven_zip() -> str | None:
    return shutil.which("7zz") or shutil.which("7z") or shutil.which("7za")


def archive_requires_password(path: Path) -> bool:
    name = path.name.lower()
    if name.endswith(".zip"):
        try:
            with zipfile.ZipFile(path) as z:
                return any(bool(info.flag_bits & 0x1) for info in z.infolist())
        except Exception:
            return False
    exe = _seven_zip()
    if exe and (name.endswith((".7z", ".rar", ".001", ".part1.rar", ".part01.rar")) or multipart_info(path)):
        try:
            cp = subprocess.run(
                [exe, "l", "-slt", str(path)],
                check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace", timeout=60,
            )
            text = (cp.stdout or "").lower()
            # Parenthesised: `and` binds tighter than `or`, so the previous
            # form silently meant "... or 'headers error'", which also matched
            # perfectly normal unencrypted multi-volume archives.
            return "encrypted = +" in text or ("headers error" in text and "password" in text)
        except Exception:
            return False
    return False


def _check_zip_budget(infos: list[zipfile.ZipInfo]) -> None:
    if len(infos) > MAX_EXTRACT_MEMBERS:
        raise RuntimeError(f"Archive contains too many entries ({len(infos)})")
    declared = sum(max(0, i.file_size) for i in infos)
    if declared > MAX_EXTRACT_BYTES:
        raise RuntimeError(
            f"Archive expands to {declared / 1024**3:.1f} GiB which exceeds the "
            f"{MAX_EXTRACT_BYTES / 1024**3:.0f} GiB extraction limit"
        )


def _extract_zip(path: Path, outdir: Path, password: str | None) -> Path:
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        _check_zip_budget(infos)
        for info in infos:
            _safe_member(outdir, info.filename)
        pwd = password.encode() if password is not None else None
        z.extractall(outdir, pwd=pwd)
    return outdir


def _extract_tar(path: Path, outdir: Path) -> Path:
    with tarfile.open(path) as t:
        members = t.getmembers()
        if len(members) > MAX_EXTRACT_MEMBERS:
            raise RuntimeError(f"Archive contains too many entries ({len(members)})")
        declared = sum(max(0, m.size) for m in members)
        if declared > MAX_EXTRACT_BYTES:
            raise RuntimeError(
                f"Archive expands to {declared / 1024**3:.1f} GiB which exceeds the "
                f"{MAX_EXTRACT_BYTES / 1024**3:.0f} GiB extraction limit"
            )
        for m in members:
            _safe_member(outdir, m.name)
        try:
            # filter="data" blocks links/devices and unsafe modes on modern
            # Python. It only exists from 3.11.4/3.10.12 onwards.
            t.extractall(outdir, filter="data")
        except TypeError:
            t.extractall(outdir)
    return outdir


def _extract_with_7z(path: Path, outdir: Path, password: str | None) -> Path:
    exe = _seven_zip()
    if not exe:
        exe = shutil.which("unar")
        if exe:
            args = [exe, "-f", "-o", str(outdir), str(path)]
            if password:
                args.extend(["-p", password])
            cp = subprocess.run(args, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace", timeout=3600)
            if cp.returncode != 0:
                detail = (cp.stderr or cp.stdout or "Archive extraction failed")[-4000:]
                if "password" in detail.lower() or "encrypt" in detail.lower():
                    raise RuntimeError("Archive password is required or incorrect")
                raise RuntimeError(detail)
            return outdir
        raise RuntimeError("No 7z/unar extractor is installed on this host")
    args = [exe, "x", "-y", f"-o{outdir}"]
    if password is not None:
        args.append(f"-p{password}")
    args.append(str(path))
    cp = subprocess.run(args, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace", timeout=3600)
    if cp.returncode != 0:
        detail = (cp.stderr or cp.stdout or "Archive extraction failed")[-4000:]
        if "wrong password" in detail.lower() or "password" in detail.lower() or "encrypted" in detail.lower():
            raise RuntimeError("Archive password is required or incorrect")
        raise RuntimeError(detail)
    return outdir


def extract_archive(path: Path, outdir: Path, password: str | None = None) -> Path:
    outdir.mkdir(parents=True, exist_ok=True)
    name = path.name.lower()
    if name.endswith(".zip") and not multipart_info(path):
        return _extract_zip(path, outdir, password)
    if any(name.endswith(x) for x in (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
        return _extract_tar(path, outdir)
    return _extract_with_7z(path, outdir, password)
