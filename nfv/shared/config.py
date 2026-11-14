#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
Toolchain discovery and shared configuration.

Dafny 4.11 requires a matching Z3 (4.12.1) and does NOT find it on this machine
automatically, so the solver path is resolved explicitly and passed on every
invocation. Repo convention (inherited from the legacy scripts) is that Z3 lives
under ~/z3/z3-<version>-<platform>/bin/.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

NFV_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = NFV_DIR.parent
WORK_DIR = NFV_DIR / "work"
RESULTS_DIR = NFV_DIR / "results"
DATASET = REPO_DIR / "paper" / "data" / "nl2post_safe.json"

# Dafny 4.11 expects z3 4.12.1; listed first. Others are fallbacks.
_Z3_PREFERRED = ("z3-4.12.1", "z3-4.13.4", "z3-4.11.2")

# Verifier exit codes (measured, not assumed).
DAFNY_EXIT_VERIFIED = 0
DAFNY_EXIT_VERIFICATION_ERROR = 4


def find_z3() -> str | None:
    """Locate a z3 executable compatible with Dafny."""
    env = os.environ.get("NFV_Z3_PATH")
    if env and Path(env).exists():
        return env

    z3_home = Path.home() / "z3"
    if z3_home.is_dir():
        candidates = sorted(z3_home.iterdir())
        for pref in _Z3_PREFERRED:
            for child in candidates:
                if child.name.startswith(pref):
                    exe = child / "bin" / ("z3.exe" if os.name == "nt" else "z3")
                    if exe.exists():
                        return str(exe)
        for child in reversed(candidates):
            exe = child / "bin" / ("z3.exe" if os.name == "nt" else "z3")
            if exe.exists():
                return str(exe)

    return shutil.which("z3")


def find_tool(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    candidate = Path.home() / ".dotnet" / "tools" / (name + (".exe" if os.name == "nt" else ""))
    return str(candidate) if candidate.exists() else None


def find_cbmc() -> str | None:
    """Locate CBMC, honoring an explicit path on either host platform.

    The repo already carries a CBMC install from earlier work; that copy is
    preferred over anything on PATH so the two agree on a version.
    """
    env = os.environ.get("NFV_CBMC_EXE")
    if env and Path(env).exists():
        return env
    if os.name == "nt":
        for cand in (
            Path.home() / "cbmc" / "cbmc" / "bin" / "cbmc.exe",
            Path(r"C:\Program Files\cbmc\bin\cbmc.exe"),
        ):
            if cand.exists():
                return str(cand)
    return shutil.which("cbmc")


def msvc_env() -> dict | None:
    """Environment in which CBMC can preprocess C on Windows.

    CBMC shells out to the platform C preprocessor. Measured: with no `cl.exe`
    reachable it reports `CL Preprocessing failed / PARSING ERROR` and exits 6,
    which is indistinguishable from a genuine syntax error in the artifact. The
    CBMC backend therefore refuses to run at all rather than record an arm's
    worth of phantom type errors.
    """
    global _MSVC_ENV_CACHE
    if _MSVC_ENV_CACHE is not _UNSET:
        return _MSVC_ENV_CACHE
    _MSVC_ENV_CACHE = _build_msvc_env()
    return _MSVC_ENV_CACHE


def _build_msvc_env() -> dict | None:
    if os.name != "nt":
        return os.environ.copy()
    vs_base = Path(r"C:\Program Files\Microsoft Visual Studio\2022")
    vs_path = next((vs_base / ed for ed in
                    ("Enterprise", "Professional", "Community", "BuildTools")
                    if (vs_base / ed).exists()), None)
    if vs_path is None:
        return None
    msvc_base = vs_path / "VC" / "Tools" / "MSVC"
    if not msvc_base.exists():
        return None
    versions = sorted(msvc_base.iterdir(), reverse=True)
    if not versions:
        return None
    msvc = versions[0]
    cl_dir = msvc / "bin" / "Hostx64" / "x64"
    if not (cl_dir / "cl.exe").exists():
        return None

    kit_base = Path(r"C:\Program Files (x86)\Windows Kits\10\Include")
    kit_inc = ""
    if kit_base.exists():
        kits = sorted(kit_base.iterdir(), reverse=True)
        if kits:
            kv = kits[0]
            kit_inc = f"{kv / 'ucrt'};{kv / 'shared'};{kv / 'um'}"

    env = os.environ.copy()
    env["PATH"] = f"{cl_dir};{env.get('PATH', '')}"
    env["INCLUDE"] = f"{msvc / 'include'};{kit_inc}"
    return env


_UNSET = object()
_MSVC_ENV_CACHE: object = _UNSET

Z3_PATH = find_z3()
DAFNY_PATH = os.environ.get("NFV_DAFNY_PATH") or find_tool("dafny")
CBMC_PATH = find_cbmc()


def _version(cmd: list[str]) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return (p.stdout or p.stderr).strip().splitlines()[0]
    except Exception as exc:  # pragma: no cover - diagnostic path
        return f"<error: {exc}>"


def check_toolchain(verbose: bool = True) -> bool:
    ok = True
    rows = []

    if DAFNY_PATH:
        rows.append(("dafny", DAFNY_PATH, _version([DAFNY_PATH, "--version"])))
    else:
        rows.append(("dafny", "NOT FOUND", ""))
        ok = False

    if Z3_PATH:
        rows.append(("z3", Z3_PATH, _version([Z3_PATH, "--version"])))
    else:
        rows.append(("z3", "NOT FOUND", ""))
        ok = False

    if CBMC_PATH:
        rows.append(("cbmc", CBMC_PATH, _version([CBMC_PATH, "--version"])))
    else:
        rows.append(("cbmc", "not found (optional for Dafny)", ""))

    if verbose:
        for name, path, ver in rows:
            print(f"  {name:<7} {path}")
            if ver:
                print(f"          {ver}")
    return ok


if __name__ == "__main__":
    print("nfv toolchain:")
    sys.exit(0 if check_toolchain() else 1)
