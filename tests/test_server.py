"""
HTTP tests.

These boot the real `ThreadingHTTPServer` on an ephemeral port against a temp
vault and talk to it over a socket. No mocking of the request layer: the point
is to catch the things that only appear once a real client is on the other end
— status codes, headers, and what a malformed body does.

The last test in this file is the one that matters most. The server binds
127.0.0.1 by design, and "by design" is worth exactly nothing unless something
checks it.
"""

import json
import os
import re
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import cockpit
from vaultfixture import VaultTestCase


class ServerTestCase(VaultTestCase):
    """A temp vault plus a live server bound to a free port on localhost."""

    def setUp(self):
        super().setUp()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), cockpit.Handler)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def get(self, path):
        with urllib.request.urlopen(self.url(path), timeout=10) as r:
            return r.status, r.headers, r.read()

    def post(self, path, raw):
        body = raw if isinstance(raw, bytes) else raw.encode()
        req = urllib.request.Request(self.url(path), data=body, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()


class TestRoutes(ServerTestCase):
    def test_root_serves_html(self):
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertIn(b"<", body)

    def test_vault_endpoint_returns_the_expected_shape(self):
        self.write("05 Projects/P.md", "---\ntype: project\nstatus: active\n---\n")
        status, _, body = self.get("/api/vault")
        self.assertEqual(status, 200)
        data = json.loads(body)
        for key in ("vault", "projects", "agents", "skills", "workflows",
                    "handoff", "memory", "decisions", "inbox"):
            self.assertIn(key, data)
        self.assertEqual(data["projects"][0]["name"], "P")

    def test_graph_endpoint_returns_nodes_and_edges(self):
        self.write("A.md", "[[B]]\n")
        self.write("B.md", "x\n")
        _, _, body = self.get("/api/graph")
        data = json.loads(body)
        self.assertEqual(len(data["nodes"]), 2)
        self.assertEqual(len(data["edges"]), 1)

    def test_agents_and_queue_endpoints_answer_on_an_empty_vault(self):
        """An empty vault is a legitimate state — a fresh clone has no notes."""
        for path in ("/api/agents", "/api/queue"):
            status, _, body = self.get(path)
            self.assertEqual(status, 200, path)
            json.loads(body)  # must be valid JSON, not a stack trace

    def test_content_length_matches_the_body(self):
        _, headers, body = self.get("/api/vault")
        self.assertEqual(int(headers["Content-Length"]), len(body))

    def test_unknown_get_path_is_404_json(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("/api/does-not-exist")
        self.assertEqual(cm.exception.code, 404)
        self.assertIn("error", json.loads(cm.exception.read()))

    def test_unknown_post_path_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.post("/api/nope", "{}")
        self.assertEqual(cm.exception.code, 404)


class TestMalformedInput(ServerTestCase):
    def test_invalid_json_body_is_400_not_500(self):
        """A broken body is the client's fault. It must not read as a server crash."""
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.post("/api/chat", "{not json at all")
        self.assertEqual(cm.exception.code, 400)
        self.assertFalse(json.loads(cm.exception.read())["ok"])

    def test_empty_body_is_handled(self):
        """Content-Length 0 falls back to {} rather than raising."""
        status, body = self.post("/api/answer", b"")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["ok"])

    def test_answer_with_missing_fields_is_refused_politely(self):
        status, body = self.post("/api/answer", json.dumps({"q": "", "answer": ""}))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": False, "error": "empty answer"})


class TestQueueWriteBack(ServerTestCase):
    """/api/answer is the only endpoint that writes to disk. Cover it properly."""

    QUEUE = (
        "---\ntype: system\n---\n"
        "# Operator Queue\n\n"
        "## Queue\n"
        "- [ ] (Research Agent) 2026-09-02 — Which lane should I rank first?\n"
    )

    def test_answering_ticks_the_box_and_records_the_answer(self):
        self.write(cockpit.QUEUE_PATH, self.QUEUE)
        line = "- [ ] (Research Agent) 2026-09-02 — Which lane should I rank first?"

        status, body = self.post("/api/answer", json.dumps({
            "q": "Which lane should I rank first?",
            "answer": "Software engineering, then solutions.",
            "line": line,
        }))
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

        text = self.read(cockpit.QUEUE_PATH)
        self.assertIn("- [x] (Research Agent)", text)
        self.assertIn("Software engineering, then solutions.", text)
        self.assertNotIn("- [ ] (Research Agent)", text)

    def test_answer_without_a_matching_line_is_appended_under_the_queue_heading(self):
        """Derived questions have no source line, so they append as a record."""
        self.write(cockpit.QUEUE_PATH, self.QUEUE)
        self.post("/api/answer", json.dumps({
            "q": "An ad-hoc question",
            "answer": "An ad-hoc answer",
            "agent": "Operator",
        }))
        text = self.read(cockpit.QUEUE_PATH)
        self.assertIn("An ad-hoc question", text)
        self.assertIn("An ad-hoc answer", text)
        # the original item must survive untouched
        self.assertIn("- [ ] (Research Agent)", text)

    def test_newlines_in_an_answer_cannot_break_the_list_structure(self):
        """A multi-line answer would otherwise inject raw lines into the markdown."""
        self.write(cockpit.QUEUE_PATH, self.QUEUE)
        self.post("/api/answer", json.dumps({
            "q": "Question",
            "answer": "line one\nline two\nline three",
            "agent": "Operator",
        }))
        text = self.read(cockpit.QUEUE_PATH)
        answer_lines = [l for l in text.splitlines() if "line one" in l]
        self.assertEqual(len(answer_lines), 1, "the answer must stay on one line")
        self.assertIn("line one line two line three", text)


class TestBindsLocalhostOnly(unittest.TestCase):
    """A control panel that reads a private vault must never listen on 0.0.0.0.

    This is asserted against the source because the bind address is a literal in
    __main__, which tests do not execute. It is a cheap guard against a future
    edit that makes the server reachable from the network — the kind of one-word
    change that is easy to make and expensive to notice.
    """

    def setUp(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "cockpit.py")
        with open(path, encoding="utf-8") as f:
            self.src = f.read()

    def test_serve_forever_is_bound_to_loopback(self):
        self.assertRegex(self.src, r'ThreadingHTTPServer\(\s*\(\s*["\']127\.0\.0\.1["\']')

    def test_no_wildcard_bind_anywhere_in_the_source(self):
        self.assertIsNone(re.search(r'["\']0\.0\.0\.0["\']', self.src),
                          "0.0.0.0 must never appear: this server reads a private vault")


if __name__ == "__main__":
    unittest.main()
