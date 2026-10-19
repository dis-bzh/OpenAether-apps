#!/usr/bin/env python3
"""Verify the CNPG Barman archive on each store: the latest base backup is complete and readable, the WAL is contiguous.

Runs in the PostgreSQL image (python3 and boto3 are there; so is barman-cloud, whose layout this reads:
<bucket>/cnpg/<cluster>/<cluster>/{base/<id>/{backup.info,data.tar*},wals/<16 hex>/<24 hex>[.ext]}).
Per cluster and store it checks:
  * the newest base backup whose backup.info says DONE exists (a failed or running newer one is skipped);
  * its data file(s) are non-empty and read to the end as a tar (the gzip CRC and the tar structure are verified);
  * every WAL segment from its begin_wal to the newest one of its timeline is present, none is empty, and a
    sample of segments reads to the end (CRC); a later timeline has its .history file;
  * on the replica store, no object older than the grace period is missing compared with the primary.
Output, one line per (cluster, store), tab separated: cnpg <store> <1|0|skip> <age of the newest base backup in s> <message>.
An archive younger than 30 h without a base backup is "skip": the first ScheduledBackup has not run yet.
"""
import bz2
import datetime
import gzip
import io
import lzma
import os
import re
import sys
import tarfile
import zlib

SEG = re.compile(r"^([0-9A-F]{8})([0-9A-F]{8})([0-9A-F]{8})(\.(gz|bz2|xz|snappy|lz4|zst))?$")
MAX_READ = int(os.environ.get("READ_DATA_MAX_BYTES", str(20 * 1024**3)))
SAMPLE_WALS = int(os.environ.get("WAL_SAMPLE", "5"))
YOUNG_S = 30 * 3600


def parse_info(text):
    kv = {}
    for line in text.splitlines():
        if "=" in line and not line.startswith(("backup_label", " ")):
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip()
    return kv


def parse_time(s):
    s = s.strip()
    if not s or s == "None":
        return None
    return datetime.datetime.fromisoformat(s).timestamp()


class Store:
    def __init__(self, client, bucket):
        self.c, self.bucket = client, bucket

    def list(self, prefix, delimiter=None):
        kw = {"Bucket": self.bucket, "Prefix": prefix}
        if delimiter:
            kw["Delimiter"] = delimiter
        objs, prefixes = [], []
        while True:
            r = self.c.list_objects_v2(**kw)
            objs += r.get("Contents", [])
            prefixes += [p["Prefix"] for p in r.get("CommonPrefixes", [])]
            if not r.get("IsTruncated"):
                return objs, prefixes
            kw["ContinuationToken"] = r["NextContinuationToken"]

    def get(self, key):
        return self.c.get_object(Bucket=self.bucket, Key=key)["Body"]


def decompressed(body, name):
    """The object as a file of its uncompressed bytes, or None when the codec is not here (size check only)."""
    if name.endswith(".gz"):
        return gzip.GzipFile(fileobj=body)
    if name.endswith(".bz2"):
        return bz2.BZ2File(body)
    if name.endswith(".xz"):
        return lzma.LZMAFile(body)
    if name.endswith((".snappy", ".lz4", ".zst")):
        return None
    return body


def drain(f):
    n = 0
    while True:
        chunk = f.read(1024 * 1024)
        if not chunk:
            return n
        n += len(chunk)


def read_tar(body, name):
    """A data file is a tar (possibly compressed). The decompressor is ours, so reading it to EOF verifies the gzip CRC (bz2 and
    xz check their own); the tar walk on top verifies the structure and reads every member."""
    raw = decompressed(body, name)
    if raw is None:
        return None
    tf = tarfile.open(fileobj=raw, mode="r|")
    for m in tf:
        f = tf.extractfile(m)
        if f is not None:
            drain(f)
    return drain(raw)   # the end-of-archive padding and the compressed stream's trailer


def check_cluster(st, base, server, now, sample=None):
    """Returns (status, age_seconds_of_newest_backup, message); status is 1, 0 or 'skip'."""
    root = f"{base}/{server}/{server}/"
    _, ids = st.list(root + "base/", "/")
    ids = sorted(p.rstrip("/").rsplit("/", 1)[-1] for p in ids)
    good = None
    notes = []
    for bid in reversed(ids):
        try:
            info = parse_info(st.get(f"{root}base/{bid}/backup.info").read().decode())
        except Exception as e:  # noqa: BLE001 - any read error is a finding
            notes.append(f"{bid}: backup.info unreadable ({type(e).__name__})")
            continue
        if info.get("status") == "DONE":
            good = (bid, info)
            break
        notes.append(f"{bid}: status {info.get('status')}")
    if good is None:
        objs, _ = st.list(root)
        oldest = min((o["LastModified"].timestamp() for o in objs), default=None)
        if oldest is None or now - oldest < YOUNG_S:
            return "skip", 0, "no base backup yet and the archive is younger than 30 h" if oldest else "empty archive"
        return 0, 0, "no completed base backup (" + "; ".join(notes[:3] or ["none listed"]) + ")"
    bid, info = good
    end = parse_time(info.get("end_time", "")) or 0
    age = max(0, int(now - end)) if end else 0
    # 1. data files
    files, _ = st.list(f"{root}base/{bid}/")
    data = [o for o in files if re.search(r"/(data|\d+)\.tar(\.[a-z0-9]+)?$", o["Key"])]
    if not any(re.search(r"/data\.tar(\.[a-z0-9]+)?$", o["Key"]) for o in data):
        return 0, age, f"backup {bid}: no data.tar* object"
    for o in data:
        if o["Size"] == 0:
            return 0, age, f"backup {bid}: {o['Key'].rsplit('/', 1)[-1]} is empty"
        if o["Size"] > MAX_READ:
            notes.append(f"{o['Key'].rsplit('/', 1)[-1]} not read (above {MAX_READ} bytes)")
            continue
        try:
            read_tar(st.get(o["Key"]), o["Key"])
        except (tarfile.TarError, zlib.error, EOFError, OSError, lzma.LZMAError) as e:
            return 0, age, f"backup {bid}: {o['Key'].rsplit('/', 1)[-1]} is corrupt ({type(e).__name__}: {e})"
    # 2. WAL continuity
    wobjs, _ = st.list(root + "wals/")
    segs, history = {}, set()
    for o in wobjs:
        name = o["Key"].rsplit("/", 1)[-1]
        m = SEG.match(name)
        if m:
            segs[(int(m.group(1), 16), int(m.group(2), 16), int(m.group(3), 16))] = o
        elif name.endswith(".history") or ".history." in name:
            history.add(int(name[:8], 16))
    tl = int(info["timeline"])
    size = int(info.get("xlog_segment_size") or 16 * 1024 * 1024)
    per = 0x100000000 // size

    def parse(w):
        return int(w[8:16], 16) * per + int(w[16:24], 16)

    first, last_end = parse(info["begin_wal"]), parse(info["end_wal"])
    mine = {(a * per + b): o for (t, a, b), o in segs.items() if t == tl}
    if not mine:
        return 0, age, f"backup {bid}: no WAL on timeline {tl}"
    top = max(mine)
    if top < last_end:
        return 0, age, f"backup {bid}: end_wal {info['end_wal']} is not archived (newest {top})"
    missing = [n for n in range(first, top + 1) if n not in mine]
    if missing:
        n = missing[0]
        return 0, age, f"WAL gap on timeline {tl}: {len(missing)} segment(s) missing from {tl:08X}{n // per:08X}{n % per:08X}"
    zero = [n for n in range(first, top + 1) if mine[n]["Size"] == 0]
    if zero:
        return 0, age, f"WAL segment {zero[0]} is empty"
    for t in range(tl + 1, max([t for (t, _, _) in segs] + [tl]) + 1):
        if t not in history:
            return 0, age, f"timeline {t} has WAL but no {t:08X}.history"
    pick = sorted({first, last_end, top} | set(sample if sample is not None else sorted(mine)[:: max(1, len(mine) // SAMPLE_WALS)][:SAMPLE_WALS]))
    for n in pick:
        o = mine.get(n)
        if o is None:
            continue
        try:
            raw = decompressed(st.get(o["Key"]), o["Key"])
            if raw is not None:
                drain(raw)
        except (zlib.error, EOFError, OSError, lzma.LZMAError) as e:
            return 0, age, f"WAL {o['Key'].rsplit('/', 1)[-1]} is corrupt ({type(e).__name__}: {e})"
    return 1, age, f"backup {bid} readable, WAL {first}..{top} contiguous ({len(mine)} segments)" + (f"; {'; '.join(notes)}" if notes else "")


def replica_gap(prim, repl, prefix, now, grace_s):
    """Keys the primary holds for longer than grace_s that the replica lacks (or holds with another size)."""
    po, _ = prim.list(prefix)
    ro, _ = repl.list(prefix)
    have = {o["Key"]: o["Size"] for o in ro}
    return [o["Key"] for o in po if now - o["LastModified"].timestamp() > grace_s and have.get(o["Key"]) != o["Size"]]


def client(endpoint, ak, sk, region):
    import boto3
    from botocore.config import Config
    return boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=ak, aws_secret_access_key=sk, region_name=region,
                        config=Config(retries={"max_attempts": 3}, request_checksum_calculation="when_required",
                                      response_checksum_validation="when_required"))


def main():
    e = os.environ
    now = float(e.get("NOW") or datetime.datetime.now(datetime.timezone.utc).timestamp())
    stores = {}
    for tag in ("PRIMARY", "REPLICA"):
        if e.get(f"{tag}_ENDPOINT") and e.get(f"{tag}_BUCKET"):
            stores[tag.lower()] = Store(client(e[f"{tag}_ENDPOINT"], e[f"{tag}_ACCESS_KEY"], e[f"{tag}_SECRET_KEY"],
                                               e.get(f"{tag}_REGION", "us-east-1")), e[f"{tag}_BUCKET"])
    grace = int(e.get("REPLICA_LAG_GRACE_S", str(2 * 3600)))
    for server in e.get("CLUSTERS", "zitadel-db grafana-db").split():
        for name, st in stores.items():
            try:
                status, age, msg = check_cluster(st, "cnpg", server, now)
                if name == "replica" and status == 1 and "primary" in stores:
                    gap = replica_gap(stores["primary"], st, f"cnpg/{server}/", now, grace)
                    if gap:
                        status, msg = 0, f"{len(gap)} object(s) older than {grace // 60} min on the primary are missing from the replica (first: {gap[0]})"
            except Exception as ex:  # noqa: BLE001 - an unreachable store is a failed verification, not a crash
                status, age, msg = 0, 0, f"store unreachable or unreadable ({type(ex).__name__}: {str(ex)[:150]})"
            print("\t".join(["cnpg", name, str(status), str(age), f"{server}: {msg}".replace("\t", " ").replace("\n", " ")]))
    return 0   # a failed check is a result, not an error of the stage


if __name__ == "__main__":
    sys.exit(main())
