"""Bundle MinGW runtime DLLs beside the native PE executables; reject MSYS dependencies."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
TOOLCHAIN = ROOT / ".tools/msys64/ucrt64/bin"
DEST = ROOT / "library/bin"


def main():
    pending = [DEST / "scuff-rf.exe", ROOT / "test/bin/export_system.exe",
               ROOT / "antenna/bin/export_planar.exe"]
    manifest = {}
    while pending:
        binary = pending.pop()
        key = binary.name.lower()
        if key in manifest:
            continue
        if binary.read_bytes()[:2] != b"MZ":
            raise RuntimeError(f"Not a native Windows executable: {binary}")
        headers = subprocess.check_output([str(TOOLCHAIN / "objdump.exe"), "-p", str(binary)], text=True)
        if "pei-x86-64" not in headers:
            raise RuntimeError(f"Expected 64-bit Windows PE: {binary}")
        imports = re.findall(r"DLL Name:\s*(\S+)", headers)
        manifest[key] = {"format": "PE32+ x86-64", "imports": imports,
                         "sha256": hashlib.sha256(binary.read_bytes()).hexdigest()}
        for name in imports:
            if name.lower() in ("msys-2.0.dll", "cygwin1.dll"):
                raise RuntimeError(f"Non-native runtime dependency: {name}")
            runtime = TOOLCHAIN / name
            if runtime.is_file():
                target = DEST / name
                shutil.copy2(runtime, target)
                pending.append(target)
            elif not (Path(os.environ["SystemRoot"]) / "System32" / name).exists() and not name.lower().startswith(("api-ms-win-", "ext-ms-win-")):
                raise RuntimeError(f"Unresolved runtime DLL: {name}")
    (ROOT / "build/runtime.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print("Native PE executables and bundled runtime verified:", ", ".join(manifest))


if __name__ == "__main__":
    main()
