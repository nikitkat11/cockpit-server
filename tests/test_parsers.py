"""
Parser tests.

The cockpit's job is to turn a folder of hand-written markdown into JSON. Hand-
written is the operative word: the input is typed by a human at 1am and is
routinely malformed. So most of what follows is not "does it parse a good note"
— it is "what does it do with a bad one", because that is the case that
actually occurs.

Three tests below assert behaviour that is arguably wrong (uppercase `- [X]`
is not recognised; a duplicate note name is silently dropped from the graph).
They are marked LIMITATION and they pin the current behaviour deliberately: an
undocumented quirk becomes a bug the moment someone depends on it, and a test
is the cheapest place to write it down.
"""

import unittest

import cockpit
from vaultfixture import VaultTestCase


class TestFrontmatter(unittest.TestCase):
    def test_parses_key_values(self):
        fm = cockpit.frontmatter("---\ntype: project\nstatus: active\n---\n# Title\n")
        self.assertEqual(fm, {"type": "project", "status": "active"})

    def test_value_may_contain_colons(self):
        """Split on the FIRST colon. Timestamps and URLs in frontmatter are normal."""
        fm = cockpit.frontmatter("---\ncreated: 2026-09-02T10:30:00\n---\n")
        self.assertEqual(fm["created"], "2026-09-02T10:30:00")

    def test_unterminated_block_yields_nothing(self):
        """A note where the author forgot the closing --- must not swallow the body."""
        fm = cockpit.frontmatter("---\ntype: project\n\n# Actually the body\n")
        self.assertEqual(fm, {})

    def test_lines_without_a_colon_are_skipped(self):
        fm = cockpit.frontmatter("---\ntype: project\njust a stray line\n---\n")
        self.assertEqual(fm, {"type": "project"})

    def test_no_frontmatter(self):
        self.assertEqual(cockpit.frontmatter("# Just a heading\n"), {})

    def test_empty_and_none_are_safe(self):
        self.assertEqual(cockpit.frontmatter(""), {})
        self.assertEqual(cockpit.frontmatter(None), {})


class TestStripFrontmatter(unittest.TestCase):
    def test_removes_the_block(self):
        self.assertEqual(
            cockpit.strip_fm("---\ntype: project\n---\n# Title\nbody\n"),
            "# Title\nbody\n",
        )

    def test_leaves_a_note_without_frontmatter_alone(self):
        text = "# Title\nbody\n"
        self.assertEqual(cockpit.strip_fm(text), text)

    def test_only_strips_at_the_start(self):
        """A horizontal rule mid-note is content, not frontmatter."""
        text = "# Title\n\n---\n\nstill body\n"
        self.assertEqual(cockpit.strip_fm(text), text)

    def test_none_becomes_empty_string(self):
        self.assertEqual(cockpit.strip_fm(None), "")


class TestSection(unittest.TestCase):
    NOTE = (
        "# Title\n"
        "intro line\n"
        "## Log\n"
        "- first\n"
        "- second\n"
        "### Detail\n"
        "nested line\n"
        "## Next Steps\n"
        "- not in the log\n"
    )

    def test_extracts_the_matching_section(self):
        lines = cockpit.section(self.NOTE, r"^Log$")
        self.assertIn("- first", lines)
        self.assertIn("- second", lines)

    def test_stops_at_the_next_heading_of_the_same_level(self):
        lines = cockpit.section(self.NOTE, r"^Log$")
        self.assertNotIn("- not in the log", lines)

    def test_includes_deeper_subheadings(self):
        """A ### inside a ## section is part of that section, heading included."""
        lines = cockpit.section(self.NOTE, r"^Log$")
        self.assertIn("### Detail", lines)
        self.assertIn("nested line", lines)

    def test_stops_at_a_higher_level_heading(self):
        note = "## Log\n- entry\n# Top Level\n- after\n"
        self.assertEqual(cockpit.section(note, r"^Log$"), ["- entry"])

    def test_missing_heading_returns_empty(self):
        self.assertEqual(cockpit.section(self.NOTE, r"^Nonexistent$"), [])

    def test_content_before_any_heading_is_excluded(self):
        self.assertNotIn("intro line", cockpit.section(self.NOTE, r"^Log$"))


class TestChecklist(unittest.TestCase):
    def test_reads_done_and_undone(self):
        items = cockpit.checklist(["- [ ] open item", "- [x] closed item"])
        self.assertEqual(
            items,
            [{"done": False, "text": "open item"}, {"done": True, "text": "closed item"}],
        )

    def test_indented_items_are_read(self):
        self.assertEqual(cockpit.checklist(["    - [x] nested"]),
                         [{"done": True, "text": "nested"}])

    def test_non_checklist_lines_are_ignored(self):
        noise = ["plain text", "- a bullet, not a box", "## heading", ""]
        self.assertEqual(cockpit.checklist(noise), [])

    def test_LIMITATION_uppercase_X_is_not_recognised(self):
        """`- [X] done` is valid Markdown and is NOT matched by the current regex.

        Obsidian writes lowercase, so this has never bitten in practice — but a
        note typed by hand can easily carry `[X]`, and it would silently read as
        no checklist item at all rather than as an unfinished one. Pinned here
        so the behaviour is a decision rather than a surprise. One-character fix
        in the regex if it ever matters.
        """
        self.assertEqual(cockpit.checklist(["- [X] done"]), [])


class TestChecklistLastLog(unittest.TestCase):
    def test_returns_the_last_entry(self):
        body = "## Log\n- older entry\n- newest entry\n## Other\n"
        self.assertEqual(cockpit.checklist_last_log(body), "newest entry")

    def test_no_log_section_returns_empty_string(self):
        self.assertEqual(cockpit.checklist_last_log("# Title\nno log here\n"), "")


class TestReadNoteAndMdFiles(VaultTestCase):
    def test_reads_a_note(self):
        self.write("note.md", "hello")
        self.assertEqual(cockpit.read_note("note.md"), "hello")

    def test_missing_note_returns_none_rather_than_raising(self):
        self.assertIsNone(cockpit.read_note("nope.md"))

    def test_lists_only_markdown_sorted(self):
        for name in ["b.md", "a.md", "notes.txt", "image.png"]:
            self.write("folder/" + name, "x")
        self.assertEqual(cockpit.md_files("folder"), ["a.md", "b.md"])

    def test_missing_folder_returns_empty_list(self):
        self.assertEqual(cockpit.md_files("does/not/exist"), [])


class TestParseProjects(VaultTestCase):
    def test_only_notes_typed_as_project_are_included(self):
        self.write("05 Projects/Real.md", "---\ntype: project\nstatus: active\n---\n")
        self.write("05 Projects/Other.md", "---\ntype: reference\n---\n")
        self.write("05 Projects/Bare.md", "no frontmatter at all\n")
        names = [p["name"] for p in cockpit.parse_projects()]
        self.assertEqual(names, ["Real"])

    def test_extracts_goal_and_deadline_from_the_status_line(self):
        self.write(
            "05 Projects/P.md",
            "---\ntype: project\nstatus: active\n---\n"
            "Status: Goal: ship the thing | Deadline: 2026-10-01\n",
        )
        p = cockpit.parse_projects()[0]
        self.assertEqual(p["goal"], "ship the thing")
        self.assertEqual(p["deadline"], "2026-10-01")

    def test_priority_projects_sort_first(self):
        self.write("05 Projects/AAA Ordinary.md", "---\ntype: project\n---\nnothing\n")
        self.write("05 Projects/ZZZ Urgent.md", "---\ntype: project\n---\n#1 PRIORITY\n")
        names = [p["name"] for p in cockpit.parse_projects()]
        self.assertEqual(names[0], "ZZZ Urgent",
                         "a #1 PRIORITY project must outrank alphabetical order")

    def test_missing_status_defaults_rather_than_raising(self):
        self.write("05 Projects/P.md", "---\ntype: project\n---\n")
        self.assertEqual(cockpit.parse_projects()[0]["status"], "?")


class TestParseGraph(VaultTestCase):
    def test_wikilink_becomes_an_edge(self):
        self.write("A.md", "see [[B]] for more\n")
        self.write("B.md", "the target\n")
        g = cockpit.parse_graph()
        self.assertEqual(len(g["nodes"]), 2)
        self.assertEqual(len(g["edges"]), 1)

    def test_aliased_and_anchored_links_resolve_to_the_note(self):
        """[[B|call it something else]] and [[B#a heading]] both point at B."""
        self.write("A.md", "[[B|an alias]] and [[B#some heading]]\n")
        self.write("B.md", "target\n")
        self.assertEqual(len(cockpit.parse_graph()["edges"]), 1)

    def test_link_to_a_nonexistent_note_is_dropped(self):
        self.write("A.md", "[[Ghost]] does not exist\n")
        g = cockpit.parse_graph()
        self.assertEqual(g["nodes"][0]["name"], "A")
        self.assertEqual(g["edges"], [])

    def test_self_link_does_not_create_an_edge(self):
        self.write("A.md", "I am [[A]]\n")
        self.assertEqual(cockpit.parse_graph()["edges"], [])

    def test_edges_are_deduplicated_and_undirected(self):
        """A->B twice, plus B->A, is still one edge."""
        self.write("A.md", "[[B]] and again [[B]]\n")
        self.write("B.md", "back to [[A]]\n")
        self.assertEqual(len(cockpit.parse_graph()["edges"]), 1)

    def test_dot_directories_are_skipped(self):
        self.write("A.md", "real note\n")
        self.write(".obsidian/plugin.md", "config noise\n")
        names = [n["name"] for n in cockpit.parse_graph()["nodes"]]
        self.assertEqual(names, ["A"])

    def test_folder_is_recorded_for_each_node(self):
        self.write("05 Projects/P.md", "x\n")
        self.write("Root.md", "x\n")
        folders = {n["name"]: n["folder"] for n in cockpit.parse_graph()["nodes"]}
        self.assertEqual(folders["P"], "05 Projects")
        self.assertEqual(folders["Root"], "root")

    def test_internal_rel_path_is_not_leaked_to_the_client(self):
        """parse_graph pops 'rel' before returning — the UI gets names, not paths."""
        self.write("A.md", "x\n")
        self.assertNotIn("rel", cockpit.parse_graph()["nodes"][0])

    def test_LIMITATION_duplicate_note_names_collapse(self):
        """Two notes with the same filename in different folders become ONE node.

        The graph is keyed by bare note name, matching Obsidian's own default
        wikilink resolution — `[[Handoff]]` is ambiguous if two Handoff.md exist.
        Consequence: the second file is invisible in the graph. Acceptable for a
        vault that keeps names unique, and a real data-loss bug for one that
        does not. Pinned so the assumption is explicit.
        """
        self.write("one/Dup.md", "first\n")
        self.write("two/Dup.md", "second\n")
        nodes = cockpit.parse_graph()["nodes"]
        self.assertEqual(len(nodes), 1)


if __name__ == "__main__":
    unittest.main()
