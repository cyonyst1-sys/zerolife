#!/usr/bin/env python3
"""Build the CardputerZero store package for Zero Life.

    python build_deb.py

Needs nothing but Python (no dpkg), so it works on Windows, macOS and Linux.
Reads app-builder.json and writes dist/zerolife_<version>-<rev>_<arch>.deb with
the layout the APPLaunch launcher expects:

    /usr/share/APPLaunch/bin/zerolife                 the game (executable)
    /usr/share/APPLaunch/applications/zerolife.desktop
    /usr/share/APPLaunch/share/images/zerolife.png    launcher icon
"""
import gzip
import hashlib
import io
import json
import os
import sys
import tarfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
meta = json.load(open(os.path.join(HERE, "app-builder.json"), encoding="utf-8"))
PKG = meta["package_name"]
VER = "%s-%s" % (meta["version"], meta.get("revision", "1"))
ARCH = meta.get("architecture", "all")
NOW = int(time.time())

DESKTOP = """[Desktop Entry]
Name=%s
Exec=/usr/share/APPLaunch/bin/%s
Terminal=false
Icon=share/images/%s.png
Type=Application
""" % (meta["app_name"], PKG, PKG)


def read(path):
    with open(os.path.join(HERE, path), "rb") as f:
        return f.read()


game = read("zerolife.py").replace(b"\r\n", b"\n")
files = [  # (path in package, bytes, mode)
    ("usr/share/APPLaunch/bin/" + PKG, game, 0o755),
    ("usr/share/APPLaunch/applications/%s.desktop" % PKG, DESKTOP.encode(), 0o644),
    ("usr/share/APPLaunch/share/images/%s.png" % PKG, read(meta["store"]["icon"]), 0o644),
    ("usr/share/doc/%s/copyright" % PKG, read("LICENSE").replace(b"\r\n", b"\n"), 0o644),
]


def tar_gz(entries):
    """entries: (name, bytes, mode); parent directories are added automatically."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as t:
        done = set()

        def add(name, data, mode, isdir=False):
            ti = tarfile.TarInfo("./" + name if name else "./")
            ti.mtime, ti.uid, ti.gid, ti.uname, ti.gname, ti.mode = NOW, 0, 0, "root", "root", mode
            if isdir:
                ti.type = tarfile.DIRTYPE
                t.addfile(ti)
            else:
                ti.size = len(data)
                t.addfile(ti, io.BytesIO(data))

        add("", b"", 0o755, True)
        for name, data, mode in entries:
            parts = name.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                d = "/".join(parts[:i])
                if d not in done:
                    done.add(d)
                    add(d + "/", b"", 0o755, True)
            add(name, data, mode)
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=NOW) as g:
        g.write(raw.getvalue())
    return out.getvalue()


size_kb = max(1, sum(len(d) for _, d, _ in files) // 1024)
control = """Package: {pkg}
Version: {ver}
Architecture: {arch}
Maintainer: {maint}
Installed-Size: {size}
Depends: {deps}
Conflicts: tinyloft
Replaces: tinyloft
Section: APPLaunch
Priority: optional
Homepage: {home}
Description: {summary}
 {desc}
""".format(pkg=PKG, ver=VER, arch=ARCH, maint=meta["maintainer"], size=size_kb, deps=meta["depends"],
           home=meta["store"]["source_repo"], summary=meta["store"]["summary"], desc=meta["description"])
md5 = "".join("%s  %s\n" % (hashlib.md5(d).hexdigest(), n) for n, d, _ in files)

parts = [
    ("debian-binary", b"2.0\n"),
    ("control.tar.gz", tar_gz([("control", control.encode(), 0o644), ("md5sums", md5.encode(), 0o644)])),
    ("data.tar.gz", tar_gz(files)),
]
deb = b"!<arch>\n"
for name, data in parts:
    deb += ("%-16s%-12d%-6d%-6d%-8s%-10d`\n" % (name, NOW, 0, 0, "100644", len(data))).encode()
    deb += data + (b"\n" if len(data) % 2 else b"")

os.makedirs(os.path.join(HERE, "dist"), exist_ok=True)
out = os.path.join(HERE, "dist", "%s_%s_%s.deb" % (PKG, VER, ARCH))
with open(out, "wb") as f:
    f.write(deb)
print("wrote %s  (%d bytes, sha256 %s)" % (os.path.relpath(out, HERE), len(deb), hashlib.sha256(deb).hexdigest()))

todo = [k for k, v in (("maintainer", meta["maintainer"]), ("store.source_repo", meta["store"]["source_repo"]),
                       ("store.author.github", meta["store"]["author"]["github"]),
                       ("store.author.display_name", meta["store"]["author"]["display_name"]))
        if "YOUR" in v or "example.com" in v]
if todo:
    print("\nNOT READY FOR THE STORE: fill these in app-builder.json, then rebuild:")
    for k in todo:
        print("  - " + k)
    sys.exit(0)
