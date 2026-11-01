#!/usr/bin/env python3
"""A small S3 store for the offline tests of the backup scripts (the aws CLI and restic talk to it over plain HTTP).

Buckets are directories under --root, objects are files (the key is the path, the modification time is LastModified).
It speaks path-style S3: HeadBucket, ListObjectsV2, Get/Head/Put/DeleteObject, DeleteObjects, multipart upload.
Signatures are not verified; the access key is read from the Authorization header and written, with the method,
the path and the server-side-encryption header, to --log (one line per request). An optional file <root>/.acl, JSON
{"<bucket>": ["<access key>", ...]}, makes any other key answer 403, which is how the tests show that the primary key
does not open the replica store. Not a model of any provider; it models the calls these scripts make.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit
from xml.sax.saxutils import escape

NS = "http://s3.amazonaws.com/doc/2006-03-01/"
LOCK = threading.Lock()


def iso(ts):
    import datetime
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def dechunk(raw):
    """aws-chunked body: <hex>[;chunk-signature=..]\r\n<data>\r\n ... 0\r\n<trailers>."""
    out, i = b"", 0
    while True:
        j = raw.index(b"\r\n", i)
        size = int(raw[i:j].split(b";")[0], 16)
        if size == 0:
            return out
        out += raw[j + 2:j + 2 + size]
        i = j + 2 + size + 2


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    root = "."
    logf = None

    def log_message(self, *a):
        pass

    def _ak(self):
        m = re.search(r"Credential=([^/]+)/", self.headers.get("Authorization", ""))
        return m.group(1) if m else "-"

    def _reply(self, code, body=b"", ctype="application/xml", headers=None):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        if body:
            self.send_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _err(self, code, name, msg=""):
        self._reply(code, f"<Error><Code>{name}</Code><Message>{escape(msg)}</Message></Error>".encode())

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            raw = b""
            while True:
                size = int(self.rfile.readline().split(b";")[0], 16)
                if size == 0:
                    self.rfile.readline()
                    break
                raw += self.rfile.read(size)
                self.rfile.readline()
        else:
            raw = self.rfile.read(n)
        if self.headers.get("x-amz-content-sha256", "").startswith("STREAMING") or "aws-chunked" in self.headers.get("Content-Encoding", ""):
            raw = dechunk(raw)
        return raw

    def _route(self):
        u = urlsplit(self.path)
        parts = unquote(u.path).lstrip("/").split("/", 1)
        bucket, key = parts[0], (parts[1] if len(parts) > 1 else "")
        q = parse_qs(u.query, keep_blank_values=True)
        ak = self._ak()
        with LOCK:
            with open(os.environ.get("FAKE_S3_LOG", self.logf or os.devnull), "a") as f:
                f.write(f"{self.command} /{bucket}/{key} ak={ak} sse={self.headers.get('x-amz-server-side-encryption', '-')} q={u.query}\n")
        acl_path = os.path.join(self.root, ".acl")
        if os.path.exists(acl_path):
            acl = json.load(open(acl_path))
            if bucket in acl and ak not in acl[bucket]:
                return self._err(403, "AccessDenied", "key not allowed on this bucket")
        bdir = os.path.join(self.root, bucket)
        if not os.path.isdir(bdir):
            return self._err(404, "NoSuchBucket")
        path = os.path.normpath(os.path.join(bdir, key))
        if not (path + os.sep).startswith(bdir + os.sep) and path != bdir:
            return self._err(400, "InvalidRequest")
        m = self.command
        if not key:
            if m == "HEAD":
                return self._reply(200)
            if m == "GET":
                return self._list(bdir, q)
            if m == "POST" and "delete" in q:
                body = self._body().decode()
                for k in re.findall(r"<Key>(.*?)</Key>", body):
                    p = os.path.join(bdir, k.replace("&amp;", "&"))
                    if os.path.isfile(p):
                        os.remove(p)
                return self._reply(200, b"<DeleteResult xmlns='%s'/>" % NS.encode())
            return self._err(501, "NotImplemented")
        mp = os.path.join(self.root, ".mp")
        if m == "POST" and "uploads" in q:
            uid = hashlib.md5(os.urandom(8)).hexdigest()
            os.makedirs(os.path.join(mp, uid), exist_ok=True)
            return self._reply(200, f"<InitiateMultipartUploadResult xmlns='{NS}'><Bucket>{bucket}</Bucket><Key>{escape(key)}</Key><UploadId>{uid}</UploadId></InitiateMultipartUploadResult>".encode())
        if m == "PUT" and "uploadId" in q and "partNumber" in q:
            data = self._body()
            with open(os.path.join(mp, q["uploadId"][0], "%05d" % int(q["partNumber"][0])), "wb") as f:
                f.write(data)
            return self._reply(200, headers={"ETag": '"%s"' % hashlib.md5(data).hexdigest()})
        if m == "POST" and "uploadId" in q:
            self._body()
            d = os.path.join(mp, q["uploadId"][0])
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as out:
                for p in sorted(os.listdir(d)):
                    out.write(open(os.path.join(d, p), "rb").read())
            return self._reply(200, f"<CompleteMultipartUploadResult xmlns='{NS}'><Bucket>{bucket}</Bucket><Key>{escape(key)}</Key><ETag>\"x\"</ETag></CompleteMultipartUploadResult>".encode())
        if m == "DELETE" and "uploadId" in q:
            return self._reply(204)
        if m == "PUT":
            if self.headers.get("x-amz-copy-source"):
                return self._err(501, "NotImplemented", "copy")
            data = self._body()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
            return self._reply(200, headers={"ETag": '"%s"' % hashlib.md5(data).hexdigest()})
        if m in ("GET", "HEAD"):
            if not os.path.isfile(path):
                return self._err(404, "NoSuchKey")
            data = open(path, "rb").read()
            rng = self.headers.get("Range")
            code, h = 200, {"Last-Modified": self.date_time_string(os.path.getmtime(path)), "ETag": '"%s"' % hashlib.md5(data).hexdigest(), "Accept-Ranges": "bytes"}
            if rng and (r := re.match(r"bytes=(\d*)-(\d*)", rng)):
                a = int(r.group(1) or 0)
                b = int(r.group(2)) if r.group(2) else len(data) - 1
                h["Content-Range"] = f"bytes {a}-{min(b, len(data) - 1)}/{len(data)}"
                data, code = data[a:b + 1], 206
            if m == "HEAD":
                h["Content-Length"] = str(len(data))
                self.send_response(code)
                for k, v in h.items():
                    self.send_header(k, v)
                self.end_headers()
                return
            return self._reply(code, data, "application/octet-stream", h)
        if m == "DELETE":
            if os.path.isfile(path):
                os.remove(path)
            return self._reply(204)
        return self._err(501, "NotImplemented")

    def _list(self, bdir, q):
        prefix = q.get("prefix", [""])[0]
        after = q.get("continuation-token", q.get("start-after", [""]))[0]
        maxk = int(q.get("max-keys", ["1000"])[0])
        keys = []
        for dp, _, fs in os.walk(bdir):
            for f in fs:
                k = os.path.relpath(os.path.join(dp, f), bdir)
                if k.startswith(prefix) and k > after:
                    keys.append(k)
        keys.sort()
        delim = q.get("delimiter", [""])[0]
        common = []
        if delim:
            rolled = []
            for k in keys:
                rest = k[len(prefix):]
                if delim in rest:
                    cp = prefix + rest.split(delim, 1)[0] + delim
                    if cp not in common:
                        common.append(cp)
                else:
                    rolled.append(k)
            keys = rolled
        page, more = keys[:maxk], len(keys) > maxk
        items = "".join(
            "<Contents><Key>%s</Key><LastModified>%s</LastModified><ETag>\"x\"</ETag><Size>%d</Size><StorageClass>STANDARD</StorageClass></Contents>"
            % (escape(k), iso(os.path.getmtime(os.path.join(bdir, k))), os.path.getsize(os.path.join(bdir, k))) for k in page)
        cps = "".join(f"<CommonPrefixes><Prefix>{escape(c)}</Prefix></CommonPrefixes>" for c in common)
        nxt = f"<NextContinuationToken>{escape(page[-1])}</NextContinuationToken>" if more else ""
        self._reply(200, f"<ListBucketResult xmlns='{NS}'><Name>b</Name><Prefix>{escape(prefix)}</Prefix><KeyCount>{len(page)}</KeyCount><MaxKeys>{maxk}</MaxKeys><IsTruncated>{'true' if more else 'false'}</IsTruncated>{items}{cps}{nxt}</ListBucketResult>".encode())

    do_GET = do_PUT = do_POST = do_DELETE = do_HEAD = _route


def serve(root, port=0, log=None):
    """Start in a thread; returns (server, url)."""
    handler = type("Handler", (H,), {"root": root, "logf": log})
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--log")
    a = ap.parse_args()
    srv, url = serve(a.root, a.port, a.log)
    print(url, flush=True)
    threading.Event().wait()
