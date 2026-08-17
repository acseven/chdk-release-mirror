#!/usr/bin/env python3
"""Mirror a CHDK stable release: fetch upstream builds, verify them, emit cameras.json.

Stdlib only. Three modes:

    sync.py check      print the upstream tag and whether it needs mirroring
    sync.py fetch      download + verify archives, export source, write cameras.json
    sync.py selftest   run the keyword-matching asserts

The release itself is cut by the workflow with `gh`, not from here.
"""

import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import urllib.request

BUILD_INFO = "https://build.chdk.photos/builds/release/meta/build_info.json"
BASE = "https://build.chdk.photos"
REPO = os.environ.get("MIRROR_REPO", "acseven/chdk-release-mirror")

WORK = "work"
BIN = os.path.join(WORK, "bin")
SRC = os.path.join(WORK, "src")

# Upstream is Subversion and needs a login. These are the well-known read-only
# credentials the CHDK community has used for years; there is no signup.
SVN_USER, SVN_PASS = "guest", "guest"


# --------------------------------------------------------------------------
# keyword matching
# --------------------------------------------------------------------------

def compact(name):
    """'SX220 HS' -> 'SX220HS'."""
    return re.sub(r"\s+", "", name)


def keywords(platform_id, desc, aka):
    """Every spelling a person might plausibly type for one camera.

    Every name ships, including the short ones -- 'G7', 'M3', plain 'N'. Some
    will match ordinary prose. That is a deliberate trade: a false link is
    visible and correctable in the forum, a missing link is invisible.
    Longest-match-first is the consumer's job, so that 'G7' inside 'G7 X' loses.
    """
    out = {platform_id, platform_id.upper()}
    for name in [desc, *aka]:
        name = name.strip()
        if not name:
            continue
        out |= {name, compact(name), compact(name).lower()}
    return sorted(filter(None, out), key=lambda k: (-len(k), k))


def drop_collisions(cameras):
    """Remove any keyword claimed by more than one camera.

    Camera-vs-camera clashes are the one case no manual fix can settle: the
    keyword has two right answers. Drop it from both rather than guess.
    """
    owners = {}
    for cam in cameras:
        for kw in cam["match"]:
            owners.setdefault(kw.lower(), set()).add(cam["id"])

    clashing = {kw for kw, ids in owners.items() if len(ids) > 1}
    if clashing:
        print(f"  dropped {len(clashing)} ambiguous keyword(s): "
              f"{', '.join(sorted(clashing)[:8])}", file=sys.stderr)
    for cam in cameras:
        cam["match"] = [kw for kw in cam["match"] if kw.lower() not in clashing]
    return cameras


# --------------------------------------------------------------------------
# upstream
# --------------------------------------------------------------------------

def fetch_json(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.load(r)


def tag_for(info):
    """Build the release tag, and refuse anything that isn't tag-shaped.

    version/revision come from a third party's JSON and end up in a git tag,
    a shell env var and GITHUB_OUTPUT. A newline in either would let upstream
    write arbitrary step outputs; anything exotic would produce an unusable
    tag. Reject rather than sanitise -- a weird upstream tag is a thing to
    look at, not to silently rewrite.
    """
    tag = f"{info['build']['version']}-{info['build']['revision']}"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.\-_]{0,62}", tag):
        raise SystemExit(f"refusing implausible upstream tag: {tag!r}")
    return tag


def release_exists(tag):
    return subprocess.run(
        ["gh", "release", "view", tag, "--repo", REPO],
        capture_output=True,
    ).returncode == 0


def each_file(info):
    """Yield (model, fw_id, kind, filespec) across the whole build."""
    for family in info["files"]:
        for model in family["models"]:
            for fw in model["fw"]:
                for kind in ("full", "small"):
                    if kind in fw:
                        yield model, fw["id"], kind, fw[kind]


def download(spec):
    """Download one archive and verify it against the published hash."""
    dest = os.path.join(BIN, spec["file"])
    if not os.path.exists(dest) or sha256(dest) != spec["sha256"]:
        url = f"{BASE}{spec['_path']}/{spec['file']}"
        urllib.request.urlretrieve(url, dest)
        got = sha256(dest)
        if got != spec["sha256"]:
            raise SystemExit(
                f"checksum mismatch for {spec['file']}\n"
                f"  expected {spec['sha256']}\n  got      {got}"
            )
    return spec["file"]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def export_source(info, tag):
    """Snapshot the exact revision the builds came from.

    `svn export`, not `git svn` -- the two existing CHDK mirrors on GitHub both
    died maintaining full history. A per-release snapshot is all the GPL needs.
    """
    subprocess.run(
        ["svn", "export", "--quiet", "--non-interactive",
         "--username", SVN_USER, "--password", SVN_PASS,
         "-r", info["build"]["revision"], info["build"]["svn_checkout"], SRC],
        check=True,
    )
    archive = os.path.join(WORK, f"chdk-{tag}-src.tar.gz")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(SRC, arcname=f"chdk-{tag}")
    return archive


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

def asset_url(tag, filename):
    return f"https://github.com/{REPO}/releases/download/{tag}/{filename}"


def build_cameras(info, tag):
    cameras = []
    for family in info["files"]:
        for model in family["models"]:
            aka = [a.strip() for a in model.get("aka", "").split(",") if a.strip()]
            cameras.append({
                "id": model["id"],
                "line": family["id"],
                "name": model["desc"],
                "aka": aka,
                "mid": model.get("mid"),
                "pid": model.get("pid"),
                "match": keywords(model["id"], model["desc"], aka),
                "firmware": [
                    {
                        "id": fw["id"],
                        **{
                            kind: {
                                "file": fw[kind]["file"],
                                "size": fw[kind]["size"],
                                "sha256": fw[kind]["sha256"],
                                "url": asset_url(tag, fw[kind]["file"]),
                            }
                            for kind in ("full", "small") if kind in fw
                        },
                    }
                    for fw in model["fw"]
                ],
            })

    return {
        "mirror": {"repo": REPO, "tag": tag},
        "build": {
            "version": info["build"]["version"],
            "revision": info["build"]["revision"],
            "utc": info["build"]["utc"],
            "source": info["build"]["svn_checkout"],
        },
        "cameras": drop_collisions(cameras),
    }


# --------------------------------------------------------------------------
# modes
# --------------------------------------------------------------------------

def mode_check():
    info = fetch_json(BUILD_INFO)
    tag = tag_for(info)
    needed = not release_exists(tag)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"tag={tag}\nneeded={'true' if needed else 'false'}\n")
    print(f"upstream {tag}: {'needs mirroring' if needed else 'already mirrored'}")


def mode_fetch():
    info = fetch_json(BUILD_INFO)
    tag = tag_for(info)
    os.makedirs(BIN, exist_ok=True)

    specs = []
    for _, _, _, spec in each_file(info):
        spec["_path"] = info["files_path"]
        specs.append(spec)

    total = sum(s["size"] for s in specs)
    print(f"{tag}: {len(specs)} archives, {total / 1024 / 1024:.0f} MB")

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for n, _ in enumerate(pool.map(download, specs), 1):
            if n % 100 == 0 or n == len(specs):
                print(f"  verified {n}/{len(specs)}")

    print("exporting source...")
    archive = export_source(info, tag)
    print(f"  {archive} ({os.path.getsize(archive) / 1024 / 1024:.0f} MB)")

    data = build_cameras(info, tag)
    with open("cameras.json", "w") as f:
        json.dump(data, f, indent=1, sort_keys=False)
    with open(os.path.join(WORK, "build_info.json"), "w") as f:
        json.dump(info, f, indent=1)

    total_kw = sum(len(c["match"]) for c in data["cameras"])
    print(f"cameras.json: {len(data['cameras'])} models, {total_kw} keywords")


def mode_selftest():
    kw = keywords("sx220hs", "SX220 HS", [])
    assert {"sx220hs", "SX220 HS", "SX220HS"} <= set(kw)

    kw = keywords("g7", "G7", [])
    assert "G7" in kw, "short names ship too -- wrong links get fixed in the forum"

    kw = keywords("ixus115_elph100hs", "IXUS 115 HS", ["ELPH 100 HS", "IXY 210F"])
    assert {"IXUS 115 HS", "ELPH 100 HS", "IXY 210F"} <= set(kw)

    # longest first, so a consumer matching in order can never let 'G7' eat 'G7 X'
    assert keywords("g7x", "G7 X", []).index("G7 X") < keywords("g7x", "G7 X", []).index("G7X")
    assert all(len(a) >= len(b) for a, b in zip(kw, kw[1:]))

    twins = [{"id": "a", "match": ["S100"]}, {"id": "b", "match": ["s100"]}]
    drop_collisions(twins)
    assert twins[0]["match"] == [] and twins[1]["match"] == [], "collisions must drop"

    assert tag_for({"build": {"version": "1.6.1", "revision": "6355"}}) == "1.6.1-6355"
    for bad in ["1.6.1\nfoo", "1.6.1; rm -rf /", "../../etc"]:
        try:
            tag_for({"build": {"version": bad, "revision": "1"}})
        except SystemExit:
            pass
        else:
            raise AssertionError(f"tag_for accepted {bad!r}")

    print("selftest ok")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    {"check": mode_check, "fetch": mode_fetch, "selftest": mode_selftest}[mode]()
