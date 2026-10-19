#!/usr/bin/env python3
"""Stage 3 of the backup verifier: inspect what stage 1 restored, ask Longhorn, and publish the metrics.

Inputs (files): restic.tsv and cnpg.tsv from the earlier stages (dataset, store, 1|0|skip, ref, message), and the restored
snapshots under WORK/<dataset>/<store>/. It adds the content checks only a restore allows:
  openbao  the raft snapshot (*.snap) is a readable tar.gz holding meta.json, state.bin and SHA256SUMS, and the sums hold;
  etcd     the file is a bolt database that a restore would accept: both meta pages valid (magic, version, FNV-1a checksum),
           the file at least as long as the high-water mark says, and, when it ends with the 32-byte sha256 an etcd
           snapshot stream carries, that hash equals the content's.
It also reads the Longhorn Backup and BackupVolume objects (state Completed, no error, a size, for the newest backup of
each volume). Results are written as Prometheus text, to OUT and (with --publish) into the ConfigMap the exporter serves.

Series (all gauges): oa_backup_verify_ok{dataset} and oa_backup_verify_last_success_timestamp_seconds{dataset} (the contract
of the M1 alerts: ok is 1 only if EVERY store passed, the timestamp is the last run where every store passed and survives
failures), plus oa_backup_verify_store_ok{dataset,store}, oa_backup_verify_newest_backup_age_seconds{dataset,store} and
oa_backup_verify_last_run_timestamp_seconds. A dataset with no result (skip everywhere) has no series.
"""
import argparse
import datetime
import hashlib
import io
import json
import os
import re
import struct
import subprocess
import sys
import tarfile
import time

CM = os.environ.get("VERIFY_CONFIGMAP", "oa-backup-verify-metrics")
KUBECTL = os.environ.get("KUBECTL", "kubectl")
LH_NS = os.environ.get("LONGHORN_NAMESPACE", "longhorn-system")


def fnv1a64(b):
    h = 0xcbf29ce484222325
    for x in b:
        h = ((h ^ x) * 0x100000001b3) & 0xFFFFFFFFFFFFFFFF
    return h


def check_bolt(path):
    """(ok, message) for an etcd snapshot file. Meta page layout (bbolt): page header 16 bytes, then
    magic u32, version u32, pageSize u32, flags u32, root{pgid u64, sequence u64}, freelist u64, pgid u64, txid u64,
    checksum u64 (FNV-1a 64 of the 56 bytes before it)."""
    size = os.path.getsize(path)
    if size < 8192:
        return False, f"{size} bytes: too short for a bolt database"
    with open(path, "rb") as f:
        psize0 = struct.unpack_from("<I", f.read(32), 24)[0]
        if psize0 < 4096 or psize0 & (psize0 - 1) or 2 * psize0 > size:
            return False, f"implausible page size {psize0}"
        best = None
        for off in (0, psize0):
            f.seek(off)
            page = f.read(16 + 64)
            magic, version, psize = struct.unpack_from("<III", page, 16)
            if magic != 0xED0CDAED:
                return False, f"meta page at {off}: bad magic {magic:#x}"
            if version != 2:
                return False, f"meta page at {off}: version {version}"
            if fnv1a64(page[16:16 + 56]) != struct.unpack_from("<Q", page, 16 + 56)[0]:
                return False, f"meta page at {off}: checksum mismatch"
            pgid, txid = struct.unpack_from("<QQ", page, 16 + 40)
            if best is None or txid > best[1]:
                best = (pgid, txid, psize)
    pgid, _, psize = best
    body = size - (size % psize) if size % psize in (0, 32) else None
    if body is None:
        return False, f"size {size} is neither a multiple of the page size {psize} nor that plus a 32-byte hash"
    if body < pgid * psize:
        return False, f"truncated: {body} bytes but the high-water mark is page {pgid} ({pgid * psize} bytes)"
    if size % psize == 32:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            left = size - 32
            while left:
                chunk = f.read(min(left, 1 << 20))
                h.update(chunk)
                left -= len(chunk)
            if f.read(32) != h.digest():
                return False, "the sha256 trailer does not match the content"
        return True, f"bolt database, {pgid} pages, sha256 trailer verified"
    return True, f"bolt database, {pgid} pages, no hash trailer (meta pages and length verified)"


def check_openbao(path):
    snaps = [os.path.join(d, f) for d, _, fs in os.walk(path) for f in fs if f.endswith(".snap")]
    if not snaps:
        return False, "no *.snap in the restored snapshot"
    try:
        with tarfile.open(snaps[0], "r:gz") as tf:
            names = {m.name.lstrip("./"): m for m in tf.getmembers()}
            for part in ("meta.json", "state.bin", "SHA256SUMS"):
                if part not in names or names[part].size == 0:
                    return False, f"{part} missing or empty in the snapshot"
            sums = tf.extractfile(names["SHA256SUMS"]).read().decode()
            for line in sums.splitlines():
                digest, _, name = line.partition("  ")
                name = name.strip().lstrip("*./")
                if name in names and names[name].isfile():
                    h = hashlib.sha256()
                    f = tf.extractfile(names[name])
                    while True:
                        c = f.read(1 << 20)
                        if not c:
                            break
                        h.update(c)
                    if h.hexdigest() != digest:
                        return False, f"SHA256SUMS: {name} does not match"
        return True, "raft snapshot archive and its SHA256SUMS intact"
    except (tarfile.TarError, OSError, EOFError) as e:
        return False, f"snapshot unreadable ({type(e).__name__})"


def check_etcd(path):
    files = [os.path.join(d, f) for d, _, fs in os.walk(path) for f in fs]
    if len(files) != 1:
        return False, f"want exactly one file in the snapshot, found {len(files)}"
    return check_bolt(files[0])


def read_tsv(p):
    rows = []
    if os.path.exists(p):
        for line in open(p):
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 5:
                rows.append(parts[:4] + ["\t".join(parts[4:])])
    return rows


def age_of(ref, now):
    if not ref:
        return None
    try:
        if re.fullmatch(r"\d+", ref):
            return max(0, int(ref))
        t = datetime.datetime.fromisoformat(re.sub(r"(\.\d+)", "", ref).replace("Z", "+00:00"))
        return max(0, int(now - t.timestamp()))
    except ValueError:
        return None


def longhorn(now):
    """[(store, ok, age, msg)] or None when Longhorn or its backup objects are absent."""
    def get(kind):
        r = subprocess.run([KUBECTL, "get", kind, "-n", LH_NS, "-o", "json"], capture_output=True, text=True)
        if r.returncode != 0:
            if re.search(r"doesn't have a resource type|NotFound|the server could not find|not found", r.stderr, re.I):
                return None
            raise RuntimeError(r.stderr.strip()[:200] or "kubectl failed")
        return json.loads(r.stdout)["items"]
    try:
        vols, backs = get("backupvolumes.longhorn.io"), get("backups.longhorn.io")
    except RuntimeError as e:
        return [("primary", False, None, f"cannot read the Longhorn objects: {e}")]
    if vols is None or backs is None or not vols:
        return None
    by = {b["metadata"]["name"]: b for b in backs}
    bad, newest = [], None
    for v in vols:
        name = v["status"].get("lastBackupName", "")
        b = by.get(name)
        vn = v["metadata"]["name"]
        if not name or b is None:
            bad.append(f"{vn}: no last backup object")
            continue
        st = b.get("status", {})
        if st.get("state") != "Completed" or st.get("error") or not st.get("size") or st.get("size") == "0":
            bad.append(f"{vn}: {name} state={st.get('state')} error={st.get('error', '')[:60]} size={st.get('size')}")
        a = age_of(st.get("backupCreatedAt") or v["status"].get("lastBackupAt"), now)
        if a is not None:
            newest = a if newest is None else min(newest, a)
    if bad:
        return [("primary", False, newest, "; ".join(bad[:3]))]
    return [("primary", True, newest, f"{len(vols)} volume(s), the newest backup of each is Completed")]


def prom_escape(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def parse_previous(text):
    prev = {}
    for m in re.finditer(r'^oa_backup_verify_last_success_timestamp_seconds\{dataset="([^"]+)"\} ([0-9.e+]+)$', text or "", re.M):
        prev[m.group(1)] = float(m.group(2))
    return prev


def build(rows, now, previous):
    """rows: (dataset, store, ok, age|None, msg) with ok in True/False. Returns (text, summary lines)."""
    ds = {}
    for d, s, ok, age, msg in rows:
        ds.setdefault(d, {})[s] = (ok, age, msg)
    out = []
    def metric(name, help_, lines):
        out.append(f"# HELP {name} {help_}\n# TYPE {name} gauge")
        out.extend(lines)
    ok_l, ts_l, so_l, ag_l, log = [], [], [], [], []
    for d in sorted(ds):
        all_ok = all(v[0] for v in ds[d].values())
        ok_l.append(f'oa_backup_verify_ok{{dataset="{d}"}} {int(all_ok)}')
        last = now if all_ok else previous.get(d)
        if last is not None:
            ts_l.append(f'oa_backup_verify_last_success_timestamp_seconds{{dataset="{d}"}} {last:.0f}')
        for s in sorted(ds[d]):
            ok, age, msg = ds[d][s]
            so_l.append(f'oa_backup_verify_store_ok{{dataset="{d}",store="{s}"}} {int(ok)}')
            if age is not None:
                ag_l.append(f'oa_backup_verify_newest_backup_age_seconds{{dataset="{d}",store="{s}"}} {age}')
            log.append(f"{'PASS' if ok else 'FAIL'} {d}/{s}: {msg}")
    # datasets that earlier runs knew and this run has no result for keep their last success, no ok series
    for d, t in previous.items():
        if d not in ds:
            ts_l.append(f'oa_backup_verify_last_success_timestamp_seconds{{dataset="{d}"}} {t:.0f}')
    metric("oa_backup_verify_ok", "1 when the last verification passed on every store of the dataset, 0 when one failed", ok_l)
    metric("oa_backup_verify_last_success_timestamp_seconds", "Unix time of the last verification that passed on every store", ts_l)
    metric("oa_backup_verify_store_ok", "Result of the last verification per store", so_l)
    metric("oa_backup_verify_newest_backup_age_seconds", "Age of the newest backup that the verification found", ag_l)
    metric("oa_backup_verify_last_run_timestamp_seconds", "Unix time the verifier last finished a run", [f"oa_backup_verify_last_run_timestamp_seconds {now:.0f}"])
    return "\n".join(out) + "\n", log


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=os.environ.get("RESULTS", "/results"))
    ap.add_argument("--work", default=os.environ.get("WORK", "/work"))
    ap.add_argument("--out", default="")
    ap.add_argument("--publish", action="store_true")
    a = ap.parse_args()
    now = float(os.environ.get("NOW") or time.time())
    rows = []
    for f in ("restic.tsv", "cnpg.tsv"):
        for ds, store, st, ref, msg in read_tsv(os.path.join(a.results, f)):
            if st == "skip":
                print(f"SKIP {ds}/{store}: {msg}")
                continue
            ok = st == "1"
            if ok and ds in ("openbao", "etcd"):
                ok, msg = (check_openbao if ds == "openbao" else check_etcd)(os.path.join(a.work, ds, store))
            rows.append((ds, store, ok, age_of(ref, now), msg))
    lh = longhorn(now)
    if lh:
        rows += [("longhorn", s, ok, age, msg) for s, ok, age, msg in lh]
    else:
        print("SKIP longhorn: no Longhorn backup objects")
    prev = {}
    if a.publish:
        r = subprocess.run([KUBECTL, "get", "configmap", CM, "-o", "jsonpath={.data.metrics}"], capture_output=True, text=True)
        prev = parse_previous(r.stdout)
    elif a.out and os.path.exists(a.out):
        prev = parse_previous(open(a.out).read())
    text, log = build(rows, now, prev)
    print("\n".join(log))
    if a.out:
        open(a.out, "w").write(text)
    if a.publish:
        patch = json.dumps({"data": {"metrics": text}})
        r = subprocess.run([KUBECTL, "patch", "configmap", CM, "--type", "merge", "-p", patch], capture_output=True, text=True)
        if r.returncode != 0:
            print("ERROR: cannot publish the metrics:", r.stderr.strip()[:300])
            return 1
        print(f"published {len(text)} bytes to configmap/{CM}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
