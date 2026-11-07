#!/usr/bin/env python3
"""
Offline tests for Dropdrive's transfer engine.

Runs a local mock of Dropbox's shared-link download and Google Drive's resumable upload,
injects dropped connections, partial commits, server errors and lost sessions, and checks
that the uploaded bytes and the Dropbox content hash come out right.

    python3 tests/test_engine.py
"""
import hashlib
import json
import os
import random
import re
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="dropdrive-test-")
os.environ["DROPDRIVE_HOME"] = TMP
os.environ["DROPDRIVE_LOG"] = os.path.join(TMP, "test.log")
os.environ["DROPDRIVE_TEST_TOKEN"] = "test-token"

MiB = 1024 * 1024
Q = 256 * 1024  # Google commits in 256 KiB units


def dropbox_hash(data):
    blocks = b"".join(hashlib.sha256(data[i:i + 4 * MiB]).digest() for i in range(0, len(data), 4 * MiB))
    return hashlib.sha256(blocks).hexdigest()


class Mock:
    """Shared state for the fake servers."""

    def __init__(self, data):
        self.data = data
        self.sessions = {}            # id -> bytearray committed
        self.done = {}                # id -> bytes
        self.rng = random.Random(7)
        self.fail_rate = 0.0          # chance a request gets a failure injected
        self.down = False             # every upload request returns 503
        self.ignore_range = False     # Dropbox ignores Range headers
        self.lose_session_once = False
        self.counter = 0
        self.lock = threading.Lock()


def make_handler(m, port_ref):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, body=b"", headers=None):
            self.send_response(code)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def do_POST(self):
            if self.path.startswith("/content.dropboxapi.com/2/sharing/get_shared_link_file"):
                self._body()
                assert json.loads(self.headers["Dropbox-API-Arg"])["url"]
                start = 0
                r = self.headers.get("Range")
                if r and not m.ignore_range:
                    start = int(re.match(r"bytes=(\d+)-", r).group(1))
                payload = m.data[start:]
                self.send_response(206 if start else 200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                cut = None
                if m.rng.random() < m.fail_rate:
                    cut = m.rng.randint(0, max(1, len(payload) - 1))
                pos = 0
                try:
                    while pos < len(payload):
                        step = 256 * 1024
                        if cut is not None and pos + step > cut:
                            self.wfile.write(payload[pos:cut])
                            self.wfile.flush()
                            self.connection.shutdown(2)   # drop the connection mid-download
                            return
                        self.wfile.write(payload[pos:pos + step])
                        pos += step
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if self.path.startswith("/www.googleapis.com/upload/drive/v3/files?uploadType=resumable"):
                meta = json.loads(self._body())
                assert meta["parents"] and meta["name"]
                assert int(self.headers["X-Upload-Content-Length"]) == len(m.data)
                with m.lock:
                    m.counter += 1
                    sid = "s%d" % m.counter
                    m.sessions[sid] = bytearray()
                self._send(200, b"{}", {"Location": "http://127.0.0.1:%d/session/%s" % (port_ref[0], sid)})
                return
            self._send(404, b"{}")

        def do_PUT(self):
            sid = self.path.rsplit("/", 1)[-1]
            total = len(m.data)
            cr = self.headers["Content-Range"]
            if m.lose_session_once and sid in m.sessions and len(m.sessions[sid]) > 6 * MiB:
                m.lose_session_once = False
                del m.sessions[sid]
            if sid in m.done:
                self._body()
                return self._send(200, json.dumps({"id": sid, "size": str(total)}).encode())
            if sid not in m.sessions:
                self._body()
                return self._send(404, b'{"error":{"message":"gone"}}')
            committed = m.sessions[sid]
            if cr.startswith("bytes */"):
                self._body()
                return self._status(sid)
            if m.down:
                self._body()
                return self._send(503, b'{"error":{"errors":[{"reason":"backendError"}],"message":"down"}}')
            a, b, t = map(int, re.match(r"bytes (\d+)-(\d+)/(\d+)", cr).groups())
            assert t == total
            assert a == len(committed), "client sent from %d but %d is committed" % (a, len(committed))
            n = int(self.headers["Content-Length"])
            assert n == b - a + 1
            roll = m.rng.random()
            if roll < m.fail_rate / 3:
                # Read part of the body, commit whole 256 KiB units of it, then drop the connection.
                part = self.rfile.read(m.rng.randint(0, n))
                keep = len(part) // Q * Q
                committed += part[:keep]
                self.connection.shutdown(2)
                return
            body = self.rfile.read(n)
            if roll < 2 * m.fail_rate / 3:
                return self._send(503, b'{"error":{"errors":[{"reason":"backendError"}],"message":"busy"}}')
            if roll < m.fail_rate and b + 1 < total:
                keep = m.rng.randint(0, n // Q) * Q   # Google keeps only part of the chunk
                committed += body[:keep]
                return self._status(sid)
            committed += body
            if len(committed) == total:
                m.done[sid] = bytes(committed)
                return self._send(200, json.dumps({"id": sid, "size": str(total)}).encode())
            return self._status(sid)

        def _status(self, sid):
            n = len(m.sessions[sid])
            if sid in m.done:
                return self._send(200, json.dumps({"id": sid, "size": str(len(m.data))}).encode())
            h = {"Range": "bytes=0-%d" % (n - 1)} if n else {}
            return self._send(308, b"", h)
    return H


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass  # dropped connections are injected on purpose


class EngineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rnd = random.Random(1)
        cls.data = bytes(rnd.getrandbits(8) for _ in range(9 * MiB + 12345))
        cls.mock = Mock(cls.data)
        port = [0]
        cls.server = QuietServer(("127.0.0.1", 0), make_handler(cls.mock, port))
        port[0] = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        os.environ["DROPDRIVE_TEST_SERVER"] = "127.0.0.1:%d" % port[0]
        sys.path.insert(0, os.path.dirname(HERE))
        import dropdrive
        dropdrive.CHUNK = 1 * MiB
        dropdrive.TIMEOUT = 5
        dropdrive.time.sleep = lambda s: None    # no real waiting in tests
        cls.dd = dropdrive

    def setUp(self):
        m = self.mock
        m.sessions.clear()
        m.done.clear()
        m.fail_rate, m.down, m.ignore_range, m.lose_session_once = 0.0, False, False, False

    def make_job(self, name, content_hash="auto"):
        dd = self.dd
        ch = dropbox_hash(self.data) if content_hash == "auto" else content_hash
        job = dd.Job(id=dd.Job.make_id("https://www.dropbox.com/scl/fo/x", "/" + name, "folder1", name),
                     link="https://www.dropbox.com/scl/fo/x", dbx_path="/" + name, name=name,
                     display=name, size=len(self.data), content_hash=ch, parent_id="folder1",
                     dest_name="Dest", session=None, created=time.time())
        job.save()
        return job

    def uploaded(self):
        self.assertEqual(len(self.mock.done), 1)
        return list(self.mock.done.values())[0]

    def run_job(self, job):
        return self.dd.run_job(job, 1, self.dd.Progress(1, job.size))

    def test_clean_transfer(self):
        job = self.make_job("clean.mp4")
        self.assertEqual(self.run_job(job), "ok")
        self.assertEqual(self.uploaded(), self.data)
        self.assertFalse(os.path.exists(job.path))

    def test_flaky_network(self):
        self.mock.fail_rate = 0.35
        job = self.make_job("flaky.mp4")
        self.assertEqual(self.run_job(job), "ok")
        self.assertEqual(self.uploaded(), self.data)

    def test_dropbox_ignores_range(self):
        self.mock.fail_rate = 0.3
        self.mock.ignore_range = True
        job = self.make_job("norange.mp4")
        self.assertEqual(self.run_job(job), "ok")
        self.assertEqual(self.uploaded(), self.data)

    def test_session_lost(self):
        self.mock.lose_session_once = True
        job = self.make_job("lost.mp4")
        self.assertEqual(self.run_job(job), "ok")
        self.assertEqual(self.uploaded(), self.data)

    def test_resume_after_giving_up(self):
        dd = self.dd
        job = self.make_job("resume.mp4")
        old_max = dd.MAX_FAILS
        dd.MAX_FAILS = 2
        orig_put = dd.gd_put
        calls = {"n": 0}

        def put_then_outage(uri, rng, data=b""):
            if data:
                calls["n"] += 1
                if calls["n"] == 5:
                    self.mock.down = True
            return orig_put(uri, rng, data)
        dd.gd_put = put_then_outage
        try:
            with self.assertRaises(dd.UserError):
                self.run_job(job)
        finally:
            dd.gd_put = orig_put
            dd.MAX_FAILS = old_max
        self.assertTrue(os.path.exists(job.path), "state should be kept for resume")
        sid = list(self.mock.sessions)[0]
        self.assertGreater(len(self.mock.sessions[sid]), 0)
        # Outage over; a new run picks the job up from disk and continues the same session.
        self.mock.down = False
        job2 = dd.Job.load(job.id)
        self.assertEqual(job2.session, job.session)
        self.assertEqual(self.run_job(job2), "ok")
        self.assertEqual(self.uploaded(), self.data)
        self.assertEqual(len(self.mock.sessions), 1, "should not have started a second upload")

    def test_hash_mismatch_detected(self):
        job = self.make_job("bad.mp4", content_hash="0" * 64)
        with self.assertRaises(self.dd.UserError) as cm:
            self.run_job(job)
        self.assertIn("fingerprint", str(cm.exception))

    def test_no_hash_available(self):
        job = self.make_job("filelink.mp4", content_hash=None)
        self.mock.fail_rate = 0.3
        self.assertEqual(self.run_job(job), "unverified")
        self.assertEqual(self.uploaded(), self.data)


class HelpersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.dirname(HERE))
        import dropdrive
        cls.dd = dropdrive

    def test_parse_drive_folder(self):
        p = self.dd.parse_drive_folder
        fid = "1yoPAJ2WhLIyZN80N_JL0pjgMFdw5yb-e"
        self.assertEqual(p("https://drive.google.com/drive/folders/%s?usp=sharing" % fid), fid)
        self.assertEqual(p("https://drive.google.com/drive/u/1/folders/%s" % fid), fid)
        self.assertEqual(p("https://drive.google.com/open?id=%s" % fid), fid)
        self.assertEqual(p(fid), fid)
        self.assertEqual(p(""), "root")
        with self.assertRaises(self.dd.UserError):
            p("https://example.com/nope")

    def test_parse_selection(self):
        s = self.dd.parse_selection
        self.assertEqual(s("", 5), [0, 1, 2, 3, 4])
        self.assertEqual(s("all", 3), [0, 1, 2])
        self.assertEqual(s("2", 5), [1])
        self.assertEqual(s("1, 3 5-4", 5), [0, 2, 3, 4])
        with self.assertRaises(ValueError):
            s("9", 5)
        with self.assertRaises(ValueError):
            s("x", 5)

    def test_expiry(self):
        e = self.dd._expiry_ts
        self.assertIsNone(e("0001-01-01T00:00:00Z"))
        self.assertAlmostEqual(e("2026-11-06T18:41:56.031489-05:00"), 1794008516, delta=1)

    def test_sizes(self):
        self.assertEqual(self.dd.size_str(194015989488), "194.0 GB")
        self.assertEqual(self.dd.duration_str(5400), "1h 30m")


if __name__ == "__main__":
    unittest.main(verbosity=2)
