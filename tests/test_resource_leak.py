"""
A regression test for a bug this suite found.

`do_GET("/")` re-reads cockpit.py off disk on every request, so that editing the
embedded UI shows up on a browser refresh without restarting the server. Handy.
But the original did it with a bare

    src = open(os.path.abspath(__file__), encoding="utf-8").read()

which never closes the handle. CPython's refcounting usually collects it soon
after, which is why nothing ever visibly broke — but "usually" is doing a lot of
work in a threaded server, and the failure mode is a long-running process
slowly running out of descriptors. It surfaced here as a ResourceWarning the
first time the HTTP tests ran.

Counting descriptors directly is not portable (no /proc on macOS, and psutil
would be a dependency this project does not have), so this walks the object
graph for unclosed file objects pointing at cockpit.py. Stdlib only, and it
fails loudly against the unfixed version.
"""

import gc
import io
import os
import unittest

import cockpit
from test_server import ServerTestCase


def _unclosed_cockpit_handles():
    """Live, unclosed file objects that have cockpit.py open."""
    gc.collect()
    out = []
    for obj in gc.get_objects():
        try:
            if not isinstance(obj, io.TextIOWrapper):
                continue
            name = getattr(obj, "name", "")
            if isinstance(name, str) and os.path.basename(name) == "cockpit.py" \
                    and not obj.closed:
                out.append(obj)
        except ReferenceError:  # object died mid-walk
            continue
    return out


class TestHotReloadDoesNotLeak(ServerTestCase):
    def test_serving_the_page_repeatedly_leaks_no_file_handles(self):
        # warm up: let any one-off reads settle
        self.get("/")
        baseline = len(_unclosed_cockpit_handles())

        for _ in range(8):
            status, _, _ = self.get("/")
            self.assertEqual(status, 200)

        after = len(_unclosed_cockpit_handles())
        self.assertLessEqual(
            after, baseline,
            "GET / leaked %d file handle(s) across 8 requests. The hot-reload "
            "read in do_GET must use `with open(...) as f:`." % (after - baseline),
        )

    def test_the_hot_reload_read_uses_a_context_manager(self):
        """Belt and braces: assert the shape of the fix, not just its effect.

        The counting test above can be defeated by garbage collection timing.
        This one cannot — it reads the source.
        """
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "cockpit.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        leaky = 'src = open(os.path.abspath(__file__), encoding="utf-8").read()'
        # assertTrue, not assertNotIn: assertNotIn prints the haystack on
        # failure, and the haystack here is the entire 90KB server.
        self.assertTrue(
            leaky not in src,
            "bare open() in the hot-reload path leaks a descriptor per page load; "
            "wrap it in `with open(...) as f:`",
        )


if __name__ == "__main__":
    unittest.main()
