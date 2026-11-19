"""Build the release wheel without invoking the scikit-build-core backend.

The ethercat_master wheel only needs the Python package plus the prebuilt
SOEM shims (``ethercat_master/libecm.{dll,so,dylib}``) — CI compiles them
beforehand for every target platform, so this script just zips everything
into a ``py3-none-any`` wheel. ``ethercat_master._soem`` picks the right
library at runtime based on sys.platform.

Usage:  python ci/make_wheel.py [dist_dir]     (default: dist/)
"""

import base64
import hashlib
import os
import sys
try:
    import tomllib
except ImportError:  # Python < 3.11
    import tomli as tomllib
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = "ethercat_master"


def _record_hash(data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest())
    return "sha256=" + digest.rstrip(b"=").decode()


def main():
    dist_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "dist")
    os.makedirs(dist_dir, exist_ok=True)

    meta = tomllib.load(open(os.path.join(ROOT, "pyproject.toml"), "rb"))["project"]
    name, version = meta["name"], meta["version"]
    dist_name = name.replace("-", "_")
    dist_info = f"{dist_name}-{version}.dist-info"
    out = os.path.join(dist_dir, f"{dist_name}-{version}-py3-none-any.whl")

    files = []
    pkg_root = os.path.join(ROOT, PKG)
    for root, dirs, names in os.walk(pkg_root):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for n in names:
            if not n.endswith((".pyc", ".pyo")):
                p = os.path.join(root, n)
                files.append(os.path.relpath(p, ROOT).replace(os.sep, "/"))

    natives = [f for f in files if f.rsplit(".", 1)[-1] in ("dll", "so", "dylib")]
    if not natives:
        sys.exit("no libecm.* found in ethercat_master/ — build the shim first")

    readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    deps = "".join(f"Requires-Dist: {d}\n" for d in meta.get("dependencies", []))
    urls = "".join(f"Project-URL: {k}, {v}\n" for k, v in meta.get("urls", {}).items())
    extras = meta.get("optional-dependencies", {})
    provides = "".join(f"Provides-Extra: {e}\n" for e in extras)
    extra_reqs = "".join(
        f'Requires-Dist: {d}; extra == "{e}"\n' for e, ds in extras.items() for d in ds)
    classifiers = "".join(f"Classifier: {c}\n" for c in meta.get("classifiers", []))
    metadata = f"""Metadata-Version: 2.1
Name: {name}
Version: {version}
Summary: {meta.get("description", "")}
License: {meta.get("license", "")}
Requires-Python: {meta.get("requires-python", ">=3.8")}
{deps}{urls}{provides}{extra_reqs}{classifiers}Description-Content-Type: text/markdown

{readme}"""

    wheel = ("Wheel-Version: 1.0\nGenerator: ci/make_wheel.py\n"
             "Root-Is-Purelib: true\nTag: py3-none-any\n")
    scripts = "".join(
        f"{k} = {v}\n" for k, v in meta.get("scripts", {}).items())
    entry_points = f"[console_scripts]\n{scripts}" if scripts else ""

    records = []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        def add(arc, data):
            z.writestr(arc, data)
            b = data if isinstance(data, bytes) else data.encode()
            records.append((arc, _record_hash(b), len(b)))

        for f in files:
            add(f, open(os.path.join(ROOT, f), "rb").read())
        add(f"{dist_info}/METADATA", metadata)
        add(f"{dist_info}/WHEEL", wheel)
        if entry_points:
            add(f"{dist_info}/entry_points.txt", entry_points)
        add(f"{dist_info}/top_level.txt", PKG + "\n")
        records.append((f"{dist_info}/RECORD", "", ""))
        z.writestr(f"{dist_info}/RECORD",
                   "".join(f"{a},{s},{n}\n" if s else f"{a},,\n"
                           for a, s, n in records))

    print(f"{out}  ({os.path.getsize(out)} bytes, natives: "
          f"{', '.join(os.path.basename(n) for n in natives)})")


if __name__ == "__main__":
    main()
