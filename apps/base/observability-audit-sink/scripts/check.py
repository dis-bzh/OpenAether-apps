#!/usr/bin/env python3
"""audit-sink-check: does the audit sink still receive? (infra #318, docs/audit-sink.md)

Every CHECK_INTERVAL seconds, for each configured sink (/creds/primary, and /creds/secondary when its Secret exists), list the sink with the
shipper's key pair and read the LastModified of the newest snapshot object of each node's newest repository. No password is needed or present:
this proves DELIVERY (objects arrive, in time), not integrity (scripts/ops/audit-sink-check.sh of the infra repository proves the sequence).
Layout it reads: <prefix>/<node>/<YYYY-MM-DD>/snapshots/<object>.

Series on :LISTEN_PORT/metrics (Python standard library + the AWS CLI of the image; no Pushgateway):
  oa_audit_sink_ok{sink}                              1 when every list call of the last check succeeded, else 0
  oa_audit_sink_last_check_timestamp_seconds{sink}    when the last check finished (a hung check stops advancing it)
  oa_audit_sink_nodes{sink}                           nodes that have a repository in the sink
  oa_audit_sink_last_object_timestamp_seconds{sink,node}   newest snapshot object of the node
  oa_audit_sink_last_object_age_seconds{sink,node}         now minus that, computed at every scrape (it keeps growing while the sink is down)
"""
import json
import os
import subprocess
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

CREDS = os.environ.get("CREDS_DIR", "/creds")
INTERVAL = int(os.environ.get("CHECK_INTERVAL", "300"))
PORT = int(os.environ.get("LISTEN_PORT", "9101"))
AWS = os.environ.get("AWS_BIN", "aws")
LOCK = threading.Lock()
STATE = {}   # sink -> {"ok": 0|1, "checked": ts, "nodes": {node: ts}}


def read(sink, name):
    try:
        with open(os.path.join(CREDS, sink, name), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def sinks():
    return [s for s in ("primary", "secondary") if read(s, "ENDPOINT") and read(s, "BUCKET")]


def aws_list(sink, prefix, delimiter=True):
    """One list call (the CLI paginates); the parsed JSON, or None on any failure."""
    env = dict(os.environ, AWS_ACCESS_KEY_ID=read(sink, "AWS_ACCESS_KEY_ID"), AWS_SECRET_ACCESS_KEY=read(sink, "AWS_SECRET_ACCESS_KEY"),
               AWS_DEFAULT_REGION=read(sink, "AWS_DEFAULT_REGION") or "us-east-1", AWS_EC2_METADATA_DISABLED="true", AWS_MAX_ATTEMPTS="2",
               AWS_RETRY_MODE="standard")
    cmd = [AWS, "s3api", "list-objects-v2", "--bucket", read(sink, "BUCKET"), "--prefix", prefix, "--endpoint-url", read(sink, "ENDPOINT"),
           "--output", "json"] + (["--delimiter", "/"] if delimiter else [])
    try:
        out = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout) if out.stdout.strip() else {}
    except ValueError:
        return None


def children(doc):
    return sorted(p["Prefix"] for p in (doc or {}).get("CommonPrefixes", []) or [])


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def check(sink):
    base = (read(sink, "PREFIX") or "audit").strip("/") + "/"
    ok, nodes = 1, {}
    top = aws_list(sink, base)
    if top is None:
        return {"ok": 0, "checked": time.time(), "nodes": None}
    for node_prefix in children(top):
        node = node_prefix[len(base):].strip("/")
        days = aws_list(sink, node_prefix)
        if days is None:
            ok = 0
            continue
        newest = None
        for day_prefix in sorted(children(days))[-2:]:   # today and yesterday: a day just rolled has no snapshot yet
            snaps = aws_list(sink, day_prefix + "snapshots/", delimiter=False)
            if snaps is None:
                ok = 0
                continue
            for obj in snaps.get("Contents", []) or []:
                t = parse_time(obj["LastModified"])
                newest = t if newest is None or t > newest else newest
        nodes[node] = newest
    return {"ok": ok, "checked": time.time(), "nodes": nodes}


def metrics():
    now, out = time.time(), []
    with LOCK:
        snap = {k: dict(v) for k, v in STATE.items()}
    for name, help_ in (("oa_audit_sink_ok", "1 when the last check could list the sink"),
                        ("oa_audit_sink_last_check_timestamp_seconds", "when the last check finished"),
                        ("oa_audit_sink_nodes", "nodes with a repository in the sink"),
                        ("oa_audit_sink_last_object_timestamp_seconds", "newest snapshot object of a node"),
                        ("oa_audit_sink_last_object_age_seconds", "age of the newest snapshot object of a node, at scrape time")):
        out.append(f"# HELP {name} {help_}\n# TYPE {name} gauge")
    for sink, st in sorted(snap.items()):
        out.append(f'oa_audit_sink_ok{{sink="{sink}"}} {st["ok"]}')
        out.append(f'oa_audit_sink_last_check_timestamp_seconds{{sink="{sink}"}} {st["checked"]:.0f}')
        known = st.get("known", {})
        out.append(f'oa_audit_sink_nodes{{sink="{sink}"}} {len(known)}')
        for node, ts in sorted(known.items()):
            if ts is not None:
                out.append(f'oa_audit_sink_last_object_timestamp_seconds{{sink="{sink}",node="{node}"}} {ts:.0f}')
                out.append(f'oa_audit_sink_last_object_age_seconds{{sink="{sink}",node="{node}"}} {max(0, now - ts):.0f}')
    return "\n".join(out) + "\n"


def loop():
    while True:
        for sink in sinks():
            res = check(sink)
            with LOCK:
                old = STATE.get(sink, {}).get("known", {})
                # A failed list keeps the last known newest object, so its age keeps growing instead of vanishing.
                known = dict(old)
                for node, ts in (res["nodes"] or {}).items():
                    if ts is not None or node not in known:
                        known[node] = ts if ts is not None else known.get(node)
                STATE[sink] = {"ok": res["ok"], "checked": res["checked"], "known": known}
        time.sleep(INTERVAL)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        body = metrics().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):   # nothing about a scrape is worth a line
        pass


if __name__ == "__main__":
    threading.Thread(target=loop, daemon=True).start()
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
