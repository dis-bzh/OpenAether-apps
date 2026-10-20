#!/usr/bin/env python3
"""The canary file set of a Longhorn volume: written hourly on the live volume, checked on a volume restored from its backup.

write DIR    next generation: DIR/gen-<n>.bin (64 KiB, a function of n alone) and DIR/MANIFEST (counter, time, sha256 of each
             file), keeping the last 24 generations. The content depends on n only, so a checker needs neither the live
             volume nor the manifest to know what a file must hold: it recomputes it.
verify DIR   --backup-created EPOCH [--now EPOCH]: every file holds what its number says, the manifest agrees, the counters are
             contiguous, and the newest generation is not newer than the backup nor more than 3 h older. Prints one JSON line;
             exit 0 only when ok.
"""
import hashlib
import json
import os
import sys
import time

KEEP = 24
SIZE = 65536
MAX_GAP = 3 * 3600.0   # hourly writer, snapshot taken at some point after it


def content(n):
    return hashlib.shake_256(f"oa-canary|{n}".encode()).digest(SIZE)


def sha(b):
    return hashlib.sha256(b).hexdigest()


def fname(n):
    return f"gen-{n}.bin"


def write(d, now=None):
    os.makedirs(d, exist_ok=True)
    m = os.path.join(d, "MANIFEST")
    prev = json.load(open(m)) if os.path.exists(m) else {"counter": 0, "files": {}}
    n = prev["counter"] + 1
    p = os.path.join(d, fname(n))
    with open(p + ".tmp", "wb") as f:
        f.write(content(n))
        f.flush()
        os.fsync(f.fileno())
    os.rename(p + ".tmp", p)
    files = dict(prev["files"])
    files[fname(n)] = sha(content(n))
    for k in sorted(files, key=lambda x: int(x[4:-4]))[:-KEEP]:
        os.remove(os.path.join(d, k))
        del files[k]
    with open(m + ".tmp", "w") as f:
        json.dump({"counter": n, "written_at": now or time.time(), "files": files}, f)
        f.flush()
        os.fsync(f.fileno())
    os.rename(m + ".tmp", m)
    fd = os.open(d, os.O_RDONLY)
    os.fsync(fd)
    os.close(fd)
    return n


def verify(d, backup_created, now=None):
    now = now or time.time()
    why = []
    m = os.path.join(d, "MANIFEST")
    if not os.path.exists(m):
        return False, ["no MANIFEST on the restored volume"], {}
    try:
        man = json.load(open(m))
        n, files, w = int(man["counter"]), man["files"], float(man["written_at"])
    except (ValueError, KeyError, TypeError) as e:
        return False, [f"MANIFEST unreadable: {e}"], {}
    nums = sorted(int(k[4:-4]) for k in files)
    if not nums or nums[-1] != n:
        why.append("the manifest does not end at its counter")
    if nums and nums != list(range(nums[0], nums[0] + len(nums))):
        why.append("the generations in the manifest are not contiguous")
    for k, want in sorted(files.items()):
        p = os.path.join(d, k)
        if not os.path.exists(p):
            why.append(f"{k} is missing")
            continue
        got = sha(open(p, "rb").read())
        if got != sha(content(int(k[4:-4]))):
            why.append(f"{k} does not hold what generation {k[4:-4]} must hold")
        elif got != want:
            why.append(f"{k} differs from the manifest")
    on_disk = sorted(int(f[4:-4]) for f in os.listdir(d) if f.startswith("gen-") and f.endswith(".bin"))
    extra = [x for x in on_disk if x not in nums]
    if extra and extra != [n + 1]:  # one generation ahead of the manifest is a snapshot taken between the two writes
        why.append(f"files the manifest does not know: {extra[:3]}")
    if w > backup_created + 60:
        why.append("the newest generation is newer than the backup that holds it")
    if backup_created - w > MAX_GAP:
        why.append(f"the newest generation is {(backup_created - w) / 3600:.1f} h older than the backup: writes stopped before it")
    return not why, why, {"counter": n, "files": len(nums), "newest_age": now - w}


def main(argv):
    if len(argv) >= 3 and argv[1] == "write":
        print(f"generation {write(argv[2])}")
        return 0
    if len(argv) >= 5 and argv[1] == "verify" and argv[3] == "--backup-created":
        now = float(argv[argv.index("--now") + 1]) if "--now" in argv else None
        ok, why, info = verify(argv[2], float(argv[4]), now)
        print(json.dumps({"ok": ok, "reasons": why, **info}))
        return 0 if ok else 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
