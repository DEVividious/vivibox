"""The planner's index tools: the six read-only navigations the architecture chose, each with a
schema the wrapper checks every call against before the index sees it."""

from __future__ import annotations

import pytest
from index_harness import VALID

from vivibox.index import Limits, tools

LIMITS = Limits()
# What opencode and Claude Code call their own tools: a tool of the index never takes one's name.
BUILT_IN = {
    "bash",
    "read",
    "write",
    "edit",
    "multiedit",
    "patch",
    "glob",
    "grep",
    "list",
    "ls",
    "task",
    "todoread",
    "todowrite",
    "webfetch",
    "websearch",
    "codesearch",
    "lsp",
    "skill",
    "question",
}


def test_the_six_tools_are_read_only_navigation():
    assert [t.name for t in tools.TOOLS] == list(VALID)
    assert not {t.name for t in tools.TOOLS} & BUILT_IN
    excluded = {
        "replace_in_files",
        "rename_symbol",
        "safe_delete_symbol",
        "get_diagnostics_for_file",
        "initial_instructions",
    }
    assert not {t.name for t in tools.TOOLS} & excluded


def test_every_schema_is_a_closed_object_of_plain_bounded_values():
    for tool in tools.TOOLS:
        schema = tool.schema
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert set(schema["required"]) <= set(schema["properties"])
        for name, prop in schema["properties"].items():
            kind = prop["type"]
            keys = {
                "string": {"minLength", "maxLength"},
                "integer": {"minimum", "maximum"},
                "boolean": set(),
            }[kind]
            assert set(prop) == keys | {"type", "description"}, (tool.name, name)
        # The wrapper sets how long the index's answer is; the model is never offered it.
        assert "max_answer_chars" not in schema["properties"]


def test_the_list_is_the_same_at_every_request_and_not_the_one_the_wrapper_holds():
    first = tools.listed()
    first[0]["description"] = "changed"
    first[0]["inputSchema"]["properties"].clear()
    assert tools.listed()[0]["description"] != "changed"
    assert tools.listed()[0]["inputSchema"]["properties"]


@pytest.mark.parametrize("name", list(VALID))
def test_a_valid_call_is_forwarded_with_the_answer_length_the_wrapper_sets(name):
    tool, sent = tools.forwarded(name, VALID[name], LIMITS)
    expected = {**VALID[name], "max_answer_chars": LIMITS.text_chars} if tool.answer_chars else VALID[name]
    assert sent == expected


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("find_symbol", {"name_path_pattern": "A", "relative_path": "src", "depth": 3}),
        ("find_symbol", {"name_path_pattern": "A", "relative_path": "./src/./a", "include_body": False}),
        ("search_for_pattern", {"substring_pattern": "x", "relative_path": "a", "context_lines_after": 10}),
        (
            "search_for_pattern",
            {"substring_pattern": "x", "relative_path": "a", "paths_include_glob": "**/*.ts"},
        ),
        ("get_symbols_overview", {"relative_path": "a/b.py", "depth": 0}),
    ],
)
def test_calls_at_the_bounds_are_taken(name, arguments):
    tools.forwarded(name, arguments, LIMITS)


@pytest.mark.parametrize(
    ("name", "arguments", "why"),
    [
        ("find_symbol", {"name_path_pattern": "A", "relative_path": "src", "depth": 4}, "depth is not"),
        ("find_symbol", {"name_path_pattern": "A", "relative_path": "src", "depth": 1.0}, "depth is not"),
        ("find_symbol", {"name_path_pattern": "", "relative_path": "src"}, "name_path_pattern is not"),
        ("find_symbol", {"name_path_pattern": "A", "relative_path": "./"}, "relative_path is a path"),
        ("find_symbol", {"name_path_pattern": "A", "relative_path": "a\x00b"}, "relative_path is a path"),
        ("search_for_pattern", {"substring_pattern": "x"}, "requires relative_path"),
        (
            "search_for_pattern",
            {"substring_pattern": "x", "relative_path": "a", "context_lines_before": 11},
            "is not",
        ),
        (
            "search_for_pattern",
            {"substring_pattern": "x", "relative_path": "a", "multiline": True},
            "does not have",
        ),
        ("find_declaration", {"relative_path": "a.py"}, "requires regex"),
        ("find_implementations", {"name_path": "A", "relative_path": "../a.java"}, "relative_path is a path"),
        ("get_symbols_overview", None, "requires relative_path"),
    ],
)
def test_calls_outside_the_schema_are_refused_in_the_wrappers_words(name, arguments, why):
    with pytest.raises(tools.NotACall) as e:
        tools.forwarded(name, arguments, LIMITS)
    assert str(e.value).startswith(f"Not a call {name} takes: ") and why in str(e.value)


def test_arguments_too_long_for_one_request_are_refused():
    many = {
        "substring_pattern": "\U0001f600" * 2048,
        "relative_path": "a",
        "paths_include_glob": "\U0001f600" * 1024,
    }
    with pytest.raises(tools.NotACall, match="too long"):
        tools.forwarded("search_for_pattern", many, LIMITS)


def test_a_cut_never_leaves_half_an_escape():
    text, cut = tools.cleaned("ab\x00\x00", 5)
    assert (text, cut) == ("ab", True)
    assert tools.cleaned("ab\x00", 10) == ("ab‹U+0000›", False)
    assert tools.cleaned("x" * 11, 10) == ("x" * 10, True)


def test_the_escapes_own_brackets_in_the_data_are_shown_as_code_points():
    """The repository's own ‹ or › never reads as the wrapper's escape, and a cut never takes
    the data from a lone one to the end."""
    assert tools.cleaned("a‹U+202E›b", 100) == ("a‹U+2039›U+202E‹U+203A›b", False)
    text, cut = tools.cleaned("‹" + "x" * 200, 100)
    assert cut and len(text) >= 100 - len("‹U+2039›")
    assert text.startswith("‹U+2039›x")


@pytest.mark.parametrize(
    "c",
    ["️", "︀", "\U000e0100", "͏", "ᅟ", "ᅠ", "ㅤ", "ﾠ", "⠀", " ", " ", "　", "᠎", "\U000e0041"],
)
def test_a_character_with_nothing_to_see_reaches_the_model_as_its_code_point(c):
    assert tools.cleaned(f"a{c}b", 100) == (f"a‹U+{ord(c):04X}›b", False)


def test_an_error_result_that_was_cut_says_so_in_its_first_line():
    limits = Limits(text_chars=10)
    head, data = tools.result(tools.BY_NAME["find_symbol"], ["e" * 50], True, limits).split("\n", 1)
    assert "error" in head and "cut at 10 characters" in head and head.endswith(f"{tools.DATA}:")
    assert data == "e" * 10
