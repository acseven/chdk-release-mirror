#!/usr/bin/env python3
"""Mirror a CHDK stable release: fetch upstream builds, verify them, emit cameras.json.

Stdlib only. Four modes:

    sync.py check      print the upstream tag and whether it needs mirroring
    sync.py fetch      download + verify archives, export source, write cameras.json
    sync.py upload     create the release and upload whatever is not on it yet
    sync.py selftest   run the keyword-matching asserts

`upload` exists because `gh release create <780 files>` does not survive
contact with GitHub: it fans out uploads with no pacing, no backoff and no
resume, and a stable CHDK build is ~780 assets. The first secondary-rate-limit
403 killed the run and discarded the whole release. This uploads serially,
one second apart per GitHub's own guidance for mutative requests, backs off on
403/429, and skips assets already present -- so a throttled run resumes on the
next attempt instead of starting from zero.
"""

import concurrent.futures
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import time
import urllib.request

BUILD_INFO = "https://build.chdk.photos/builds/release/meta/build_info.json"
# Trunk feed is used only to mark cameras that have no stable release yet.
TRUNK_BUILD_INFO = "https://build.chdk.photos/builds/trunk/meta/build_info.json"
BASE = "https://build.chdk.photos"
REPO = os.environ.get("MIRROR_REPO", "acseven/chdk-release-mirror")

WORK = "work"
BIN = os.path.join(WORK, "bin")
SRC = os.path.join(WORK, "src")

# GitHub asks for at least one second between mutative REST requests. 780
# assets therefore cost ~13 minutes of pure pacing, which is fine for a job
# that runs about once a year and has a six hour ceiling.
UPLOAD_DELAY = 1.0
UPLOAD_TRIES = 6

# Upstream is Subversion and needs a login. These are the well-known read-only
# credentials the CHDK community has used for years; there is no signup.
SVN_USER, SVN_PASS = "guest", "guest"


# --------------------------------------------------------------------------

# keyword matching
# --------------------------------------------------------------------------

# Ids like ixus115_elph100hs name one camera per sales region; each part is a
# platform directory name people actually type. Parts that are directory
# names but never camera names in prose do not ship.
PART_STOPLIST = {"facebook"}


def compact(name):
    """'SX220 HS' -> 'SX220HS'."""
    return re.sub(r"\s+", "", name)


def semicompact(name):
    """'G1 X Mark II' -> 'G1X Mark II': fuse ONLY the leading letter-digit
    token with the short token after it. People half-de-space model stems
    ('G1X mark ii'); the full compact ('G1XMarkII') misses it. Fuses nothing
    new on names whose stem is already one word ('A1000 IS' -> 'A1000IS',
    already shipped) and never touches 'EOS M3' or single-token names."""
    return re.sub(r"^([A-Za-z]{1,2}\d+)\s+([A-Za-z]{1,2})\b",
                  r"\1\2", name, count=1)


def spaced(part):
    """'elph100hs' -> 'elph 100 hs'.

    Only for alias parts of multi-word ids (never for whole ids like 'a1000',
    where 'a 1000' would be a prose hazard), and only when the digit run is at
    least two digits, so nothing ever turns 'g1x' into 'g 1 x'.
    """
    if re.fullmatch(r"[a-z]{2,}\d{2,}[a-z]*", part):
        return re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", part)
    return None


def keywords(platform_id, desc, aka):
    """Every spelling a person might plausibly type for one camera.

    Every name ships, including the short ones -- 'G7', 'M3', plain 'N'. Some
    will match ordinary prose. That is a deliberate trade: a false link is
    visible and correctable in the forum, a missing link is invisible.
    Longest-match-first is the consumer's job, so that 'G7' inside 'G7 X' loses.
    """
    names = [desc, *aka]
    if "_" in platform_id:
        for part in platform_id.split("_"):
            if len(part) < 3 or part in PART_STOPLIST:
                continue
            names.append(part)
            if spaced(part):
                names.append(spaced(part))
    out = {platform_id, platform_id.upper()}
    for name in names:
        name = name.strip()
        if not name:
            continue
        out |= {name, compact(name), compact(name).lower()}
        semi = semicompact(name)
        if semi != name:
            out |= {semi, semi.lower()}
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


def release_assets(tag):
    """Asset filenames already on the release. Empty set if there is no release.

    The unit of progress is the asset, not the release: uploads now accumulate
    across runs, so "the tag exists" says nothing about whether the mirror is
    complete.
    """
    p = subprocess.run(
        ["gh", "release", "view", tag, "--repo", REPO,
         "--json", "assets", "--jq", ".assets[].name"],
        capture_output=True, text=True,
    )
    return set(p.stdout.split()) if p.returncode == 0 else set()


def expected_assets(info, tag):
    """Every filename a complete mirror of this build carries."""
    names = {spec["file"] for _, _, _, spec in each_file(info)}
    return names | {f"chdk-{tag}-src.tar.gz", "cameras.json"}


def upload_one(tag, path):
    """Upload one asset, retrying through throttling.

    gh exits non-zero for both "GitHub said slow down" and "this file is
    broken". Only the first is worth retrying, but gh reports it as prose on
    stderr rather than a distinct exit code, so match the message.
    """
    delay = 5
    for attempt in range(1, UPLOAD_TRIES + 1):
        p = subprocess.run(
            ["gh", "release", "upload", tag, path, "--repo", REPO],
            capture_output=True, text=True,
        )
        if p.returncode == 0:
            return
        err = (p.stderr or "").strip()
        # an asset that is already there is success, not a failure to retry
        if "already exists" in err:
            return
        if not retryable(err) or attempt == UPLOAD_TRIES:
            raise SystemExit(f"upload failed for {path} after {attempt} try(s):\n{err}")
        print(f"  throttled on {os.path.basename(path)}, retry {attempt}"
              f"/{UPLOAD_TRIES - 1} in {delay}s", file=sys.stderr)
        time.sleep(delay)
        delay *= 2


def retryable(err):
    """Is this gh failure the kind that goes away if we wait?"""
    e = err.lower()
    return any(s in e for s in (
        "rate limit", "secondary rate", "abuse", "403", "429",
        "502", "503", "504", "timeout", "connection reset", "eof",
    ))


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


def build_cameras(info, tag, trunk=None):
    """Build the cameras.json document.

    `trunk` is the trunk (development) build_info, when available. Cameras only
    in trunk ship with state "alpha" and no firmware entries -- the mirror only
    carries stable releases. The changelog is the whole svnlog of the release;
    consumers match entries to cameras by platform id parts.
    """
    cameras = []
    seen = set()
    for family in info["files"]:
        for model in family["models"]:
            seen.add(model["id"])
            aka = [a.strip() for a in model.get("aka", "").split(",") if a.strip()]
            cameras.append({
                "id": model["id"],
                "line": family["id"],
                "name": model["desc"],
                "aka": aka,
                "mid": model.get("mid"),
                "pid": model.get("pid"),
                "state": "stable",
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

    if trunk:
        for family in trunk["files"]:
            for model in family["models"]:
                if model["id"] in seen:
                    continue
                cameras.append({
                    "id": model["id"],
                    "line": family["id"],
                    "name": model["desc"],
                    "aka": [],
                    "mid": model.get("mid"),
                    "pid": model.get("pid"),
                    "state": "alpha",
                    "match": keywords(model["id"], model["desc"], []),
                    "firmware": [],
                })

    changelog = [
        {
            "revision": e.get("revision", ""),
            "utc": e.get("utc", ""),
            "author": e.get("author", ""),
            "msg": " ".join(e.get("msg", [])),
        }
        for e in (info.get("svnlog") or info["build"].get("svnlog") or [])
    ]

    return {
        "mirror": {"repo": REPO, "tag": tag},
        "build": {
            "version": info["build"]["version"],
            "revision": info["build"]["revision"],
            "utc": info["build"]["utc"],
            "source": info["build"]["svn_checkout"],
        },
        "changelog": changelog,
        "cameras": drop_collisions(cameras),
    }


# --------------------------------------------------------------------------
# modes
# --------------------------------------------------------------------------

def mode_check():
    info = fetch_json(BUILD_INFO)
    tag = tag_for(info)
    want = expected_assets(info, tag)
    have = release_assets(tag)
    missing = want - have
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"tag={tag}\nneeded={'true' if missing else 'false'}\n")
    print(f"upstream {tag}: {len(have & want)}/{len(want)} assets mirrored"
          f"{f', {len(missing)} missing' if missing else ' -- complete'}")


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

    data = build_cameras(info, tag, fetch_json(TRUNK_BUILD_INFO))
    with open("cameras.json", "w") as f:
        json.dump(data, f, indent=1, sort_keys=False)
    with open(os.path.join(WORK, "build_info.json"), "w") as f:
        json.dump(info, f, indent=1)

    total_kw = sum(len(c["match"]) for c in data["cameras"])
    print(f"cameras.json: {len(data['cameras'])} models, {total_kw} keywords")


def mode_upload():
    """Create the release if absent, then upload only what is missing.

    Tag is re-derived from upstream rather than taken from argv: it ends up in
    a git tag and it comes from a third party, so it goes through tag_for's
    validation on every path that uses it.
    """
    info = fetch_json(BUILD_INFO)
    tag = tag_for(info)

    if not release_exists(tag):
        subprocess.run(
            ["gh", "release", "create", tag, "--repo", REPO, "--title", f"CHDK {tag}",
             "--notes", NOTES.format(tag=tag)],
            check=True,
        )
        print(f"created release {tag}")

    have = release_assets(tag)
    paths = sorted(glob.glob(os.path.join(BIN, "*")))
    paths += [os.path.join(WORK, f"chdk-{tag}-src.tar.gz"), "cameras.json"]
    todo = [p for p in paths if os.path.basename(p) not in have]

    print(f"{len(have)} already uploaded, {len(todo)} to go")
    for n, path in enumerate(todo, 1):
        upload_one(tag, path)
        if n % 50 == 0 or n == len(todo):
            print(f"  uploaded {n}/{len(todo)}")
        time.sleep(UPLOAD_DELAY)

    missing = expected_assets(info, tag) - release_assets(tag)
    if missing:
        raise SystemExit(f"{len(missing)} asset(s) still missing, e.g. "
                         f"{', '.join(sorted(missing)[:5])}")
    print(f"release {tag} complete")


NOTES = ("Mirror of upstream CHDK {tag} from build.chdk.photos, with the matching "
         "SVN source export. Archives are byte-for-byte upstream and were verified "
         "against the published SHA-256 hashes before upload.")


def mode_selftest():
    kw = keywords("sx220hs", "SX220 HS", [])
    assert {"sx220hs", "SX220 HS", "SX220HS"} <= set(kw)

    # partially de-spaced stems link too (live report: 'G1X mark ii' missed)
    kw = keywords("g1x2", "G1 X Mark II", [])
    assert {"G1X Mark II", "g1x mark ii", "G1X2"} <= set(kw)
    kw = keywords("g7x2", "G7 X Mark II", [])
    assert "G7X Mark II" in kw
    assert semicompact("A1000 IS") == "A1000IS" and semicompact("EOS M3") == "EOS M3"

    kw = keywords("g7", "G7", [])
    assert "G7" in kw, "short names ship too -- wrong links get fixed in the forum"

    kw = keywords("ixus115_elph100hs", "IXUS 115 HS", ["ELPH 100 HS", "IXY 210F"])
    assert {"IXUS 115 HS", "ELPH 100 HS", "IXY 210F", "elph100hs", "elph 100 hs"} <= set(kw)

    # id parts ship; stoplisted and 1-2 char parts never do
    assert "facebook" not in keywords("n_facebook", "N Facebook", [])
    assert "n" not in keywords("n_facebook", "N Facebook", [])
    assert "a 1000" not in keywords("a1000", "A1000 IS", []), "whole ids never get spaced forms"

    # no 'g 1 x' from single-digit runs
    assert spaced("g1x") is None and spaced("elph100hs") == "elph 100 hs"

    # trunk-only cameras land as alpha with no firmware
    info = {"build": {"version": "1.6.1", "revision": "6355", "utc": "", "svn_checkout": ""},
            "files": [{"id": "A", "models": [{"id": "a1000", "desc": "A1000 IS", "mid": 1, "pid": 2,
                                              "fw": [{"id": "100a", "full": {"file": "f.zip", "size": 1, "sha256": "x"}}]}]}],
            "svnlog": []}
    trunk = {"files": [{"id": "A", "models": [{"id": "a1000", "desc": "A1000 IS", "fw": []},
                                           {"id": "a5900", "desc": "A5900 IS", "fw": []}]}]}
    doc = build_cameras(info, "1.6.1-6355", trunk)
    by_id = {c["id"]: c for c in doc["cameras"]}
    assert by_id["a1000"]["state"] == "stable" and by_id["a1000"]["firmware"]
    assert by_id["a5900"]["state"] == "alpha" and not by_id["a5900"]["firmware"]

    # longest first, so a consumer matching in order can never let 'G7' eat 'G7 X'
    assert keywords("g7x", "G7 X", []).index("G7 X") < keywords("g7x", "G7 X", []).index("G7X")
    assert all(len(a) >= len(b) for a, b in zip(kw, kw[1:]))

    twins = [{"id": "a", "match": ["S100"]}, {"id": "b", "match": ["s100"]}]
    drop_collisions(twins)
    assert twins[0]["match"] == [] and twins[1]["match"] == [], "collisions must drop"

    # a complete mirror is archives + source snapshot + cameras.json
    want = expected_assets(info, "1.6.1-6355")
    assert want == {"f.zip", "chdk-1.6.1-6355-src.tar.gz", "cameras.json"}, want

    # only throttling and transport faults are worth waiting out; a corrupt
    # upload must fail loudly rather than be retried six times
    assert retryable("HTTP 403: You have exceeded a secondary rate limit")
    assert retryable("HTTP 429: too many requests")
    assert retryable("Post ...: EOF")
    assert not retryable("HTTP 422: Validation Failed")
    assert not retryable("open work/bin/x.zip: no such file or directory")

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
    {"check": mode_check, "fetch": mode_fetch,
     "upload": mode_upload, "selftest": mode_selftest}[mode]()
