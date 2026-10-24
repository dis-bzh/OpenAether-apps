#!/usr/bin/env python3
"""Serves /metrics: every metric file of the given directories merged, each metric's HELP and TYPE once, its samples together.

The backup verifier and the restore tests each publish Prometheus text into their own ConfigMap; one exporter pod serves
both. Files: every regular file in the directories (a ConfigMap key is a file; the ..data symlinks are skipped).
"""
import http.server
import os
import re
import sys


def merge(dirs):
    meta, samples, order = {}, {}, []
    for d in dirs:
        try:
            names = sorted(n for n in os.listdir(d) if not n.startswith("."))
        except OSError:
            continue
        for n in names:
            p = os.path.join(d, n)
            if not os.path.isfile(p):
                continue
            for line in open(p, errors="replace").read().splitlines():
                if not line.strip():
                    continue
                m = re.match(r"# (HELP|TYPE) (\S+)", line)
                if m:
                    if m.group(2) not in order:
                        order.append(m.group(2))
                    meta.setdefault(m.group(2), {}).setdefault(m.group(1), line)
                elif not line.startswith("#"):
                    name = re.split(r"[{ ]", line, 1)[0]
                    if name not in order:
                        order.append(name)
                    samples.setdefault(name, []).append(line)
    out = []
    for name in order:
        out += [meta.get(name, {}).get(k) for k in ("HELP", "TYPE") if meta.get(name, {}).get(k)]
        out += samples.get(name, [])
    return "\n".join(out) + "\n"


def main():
    port, dirs = int(sys.argv[1]), sys.argv[2:]

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] != "/metrics":
                self.send_error(404)
                return
            body = merge(dirs).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    http.server.ThreadingHTTPServer(("", port), H).serve_forever()


if __name__ == "__main__":
    main()
