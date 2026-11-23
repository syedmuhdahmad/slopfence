import pytest

from tests.conftest import rule_lines


@pytest.mark.parametrize(
    "comment",
    [
        "# In a real implementation, validate the token against the server",
        "# In a production environment you would use a database",
        "# Simplified implementation for demo",
        "# This is a placeholder implementation",
        "# Replace this with your actual API call",
        "# Your logic goes here",
        "# ... rest of the code",
        "# Add your validation here",
        "# placeholder logic, swap out later",
        "# Hardcoded for demo purposes",
        "# Static data for demonstration purposes, use the API in production",
        "# This works for demonstration purposes only",
    ],
)
def test_placeholder_flagged(write, check, comment):
    write("src/app.py", f"def f():\n    {comment}\n    return True\n")
    assert rule_lines(check("SLOP010"), "SLOP010") == [2]


@pytest.mark.parametrize(
    "comment",
    [
        # Real-world comments from human-written projects that must stay quiet.
        "# TODO: Implement this in C.",
        "# In this case, Ansible will inject the omit-placeholder value",
        "# This is a stub package designed to roughly emulate the _yaml module",
        "# The `Omit` placeholder value will be visible to Jinja plugins",
        "# Real implementation lives in the C extension",
        # nltk (#20): human docs that mention demos.
        "# We do this here just for demo purposes. If the",
        "# A Scorer for Demo Purposes",
        # Placeholder word and demo phrase in different sentences (Copilot review on #31).
        "# Static configuration is documented. Used for demo purposes.",
        "# Uses a mock server! Kept for demo purposes",
    ],
)
def test_placeholder_not_flagged(write, check, comment):
    write("src/app.py", f"def f():\n    {comment}\n    return True\n")
    assert check("SLOP010").findings == []


def test_placeholder_in_docstring(write, check):
    write(
        "src/app.py",
        '''
        def charge(card):
            """Charge the card.

            In a real application this would call the payment provider.
            """
            return {"ok": True}
        ''',
    )
    assert rule_lines(check("SLOP010"), "SLOP010") == [4]


def test_placeholder_skipped_in_tests(write, check):
    write("tests/test_app.py", "def test_x():\n    # in a real app this would hit the db\n")
    assert check("SLOP010").findings == []


def test_placeholder_in_string_literal_not_flagged(write, check):
    write("src/app.py", 'MSG = "In a real implementation you would see this"\n')
    assert check("SLOP010").findings == []


@pytest.mark.parametrize(
    "comment",
    [
        "# Certainly! Here is the code:",
        "# Sure, here's how to do it",
        "# Here's the updated function that handles retries",
        "# Below is the complete implementation",
        "# As an AI language model, I cannot run this",
        "# I hope this helps!",
        "# Let me know if you have any other questions.",
        "# I apologize for the confusion earlier",
    ],
)
def test_chat_leftover_flagged(write, check, comment):
    write("src/app.py", f"x = 1\n{comment}\n")
    assert rule_lines(check("SLOP051"), "SLOP051") == [2]


@pytest.mark.parametrize(
    "comment",
    [
        # Human prose found in CPython, dateutil, psutil, launchpadlib, setuptools.
        "# Of course, you still have to use your head!",
        "# of course, first we have to figure out all the other things",
        "# always in gmt time. Let me know if you have comments",
        "# Here's the new credential.",
        "# Fields are explained in 'man proc'; here is an updated list",
        "# Sure enough, the cache was stale",
    ],
)
def test_chat_leftover_not_flagged(write, check, comment):
    write("src/app.py", f"x = 1\n{comment}\n")
    assert check("SLOP051").findings == []


def test_ignore_comment_on_line(write, check):
    write(
        "src/app.py",
        """
        # In a real implementation, retry.  # slopfence: ignore[SLOP010]
        # I hope this helps  # slopfence: ignore
        # In a real implementation, log it.  # slopfence: ignore[SLOP051]
        """,
    )
    assert rule_lines(check(), "SLOP010") == [3]
    assert rule_lines(check(), "SLOP051") == []


def test_ignore_file(write, check):
    write("src/app.py", "# slopfence: ignore-file\n# I hope this helps!\n")
    assert check().findings == []


def test_docstring_escapes_use_physical_lines(write, check):
    write(
        "src/app.py",
        '''
        def f():
            "Intro\\nI hope this helps!"

        def g():
            """Start \\
        I hope this helps!"""
        ''',
    )
    assert rule_lines(check("SLOP051"), "SLOP051") == [2, 6]


def test_non_utf8_and_bom_files_are_checked(tmp_path, check):
    (tmp_path / "latin.py").write_bytes(
        b"# -*- coding: latin-1 -*-\nNAME = 'caf\xe9'\n# I hope this helps!\n"
    )
    (tmp_path / "bom.py").write_bytes(b"\xef\xbb\xbf# I hope this helps!\n")
    result = check("SLOP051")
    assert result.parse_errors == []
    assert sorted((f.path, f.line) for f in result.findings) == [("bom.py", 1), ("latin.py", 3)]


@pytest.mark.parametrize(
    "rel",
    ["examples/app.py", "demo/app.py", "docs/samples/app.py", "src/demo.py", "src/auth_example.py"],
)
def test_placeholders_skipped_in_demo_and_example_code(write, check, rel):
    write(rel, "# In a real implementation, validate the token\n")
    assert check("SLOP010").findings == []


def test_placeholders_still_flagged_in_similar_names(write, check):
    write("src/democracy.py", "# In a real implementation, validate the token\n")
    assert rule_lines(check("SLOP010"), "SLOP010") == [1]


@pytest.mark.parametrize(
    "comment",
    [
        "# As an AI language model, I cannot run this",
        "# As an AI, I don't have access to your files",
        "# As an AI, I\u2019m not able to browse the web, so check the link yourself",
        "# As an AI assistant, I am not able to test this",
        "# Note: as an AI model, I'm unable to verify the output",
        "# As an AI, my knowledge is limited to what I was trained on",
        "# As an AI language model, my training data ends before this release",
        "# As an AI,  I cannot run this",
    ],
)
def test_an_assistant_that_speaks_of_itself_is_flagged(write, check, comment):
    """The phrase is chat text when, after a comma, the writer goes on about itself in
    the first person: "I" or "my"."""
    write("src/app.py", f"x = 1\n{comment}\n")
    assert rule_lines(check("SLOP051"), "SLOP051") == [2]


@pytest.mark.parametrize(
    "comment",
    [
        # Docstrings of Significant-Gravitas/AutoGPT, which uses Vercel's "AI SDK".
        "# Emit as an AI SDK v5 data part.",
        "# Resolved display name, retained as an AI SDK data part on replay.",
        "# Emit as an AI SDK v5 data part so the client surfaces it as",
        # The same shape: "AI" describes the noun that follows it.
        "# Registered as an AI provider plugin at import time",
        "# Runs as an AI agent with its own tool list",
        "# Treat this service as an AI gateway",
        "# Use it as an AI assistant for code review",
        "# Works as an AI I/O layer between the model and the tools",
        "# Counted as an aid allocation, so only the stored choice applies",
        # A language model as a thing that is used, not as the one who speaks.
        "# Use it as an AI language model for text classification",
        # "I" and "my" without the comma: the writer, in a clause about the model.
        "# Use it as an AI model I trained for classification",
        "# Loaded as an AI model my team trained last year",
        "# Wrap the client as an AI assistant I can swap out in tests",
        # A comma, and then no first person.
        "# Treat it as an AI, i.e. as a black box",
        "# Served as an AI, in the same process as the tools",
        # The phrase as a quoted example inside a comment about something else.
        '# Reject the phrase "As an AI, I cannot help" in generated output.',
        "# Strip a leading 'As an AI, I am unable to' from the answer",
        "# Answers that start with `As an AI, my` are sent back to the model",
        "# The filter drops \u201cAs an AI, I can\u2019t help\u201d and the line after it",
    ],
)
def test_ai_as_a_plain_word_is_not_flagged(write, check, comment):
    """ "as an AI SDK", "as an AI agent": prose about software, not an assistant talking."""
    write("src/app.py", f"x = 1\n{comment}\n")
    assert check("SLOP051").findings == []


# --- Descriptions of finished code, and notes in docstring examples (#101). ---


@pytest.mark.parametrize(
    "sentence",
    [
        # AutoGPT and transformers: a class and a function that are finished.
        "This is a simplified version compared to PostResponse, as the list endpoint",
        "This is a simplified version that handles the conversion without loading the model.",
        "This is a simplified implementation which skips the cache on purpose.",
        "This is a simplified version compared with the one in the paper.",
        "This is a simplified function than the general one above, for 2D inputs only.",
        # More than one space, or a tab, before the word that compares.
        "This is a simplified version  compared to PostResponse.",
        "This is a simplified version\tthat handles the conversion.",
    ],
)
def test_simplified_version_that_describes_real_code_is_not_flagged(write, check, sentence):
    """A sentence that says how finished code relates to other code is no placeholder."""
    write("src/app.py", f'class Item:\n    """An item.\n\n    {sentence}\n    """\n')
    write("src/b.py", f"def f():\n    # {sentence}\n    return compute()\n")
    assert check("SLOP010").findings == []


@pytest.mark.parametrize(
    "sentence",
    [
        "This is a simplified implementation.",
        "This is a simplified version",
        "This is just a simplified version, the real one needs a database",
        "This is a simplified version of the real algorithm",
        "This is a placeholder version that returns fixed data",
        "This is a placeholder implementation which we replace later",
    ],
)
def test_simplified_version_that_stands_in_for_real_code_is_still_flagged(write, check, sentence):
    """Said of the code itself, or of a placeholder, the sentence is still reported."""
    write("src/app.py", f"def f():\n    # {sentence}\n    return True\n")
    assert rule_lines(check("SLOP010"), "SLOP010") == [2]


DOCTEST = '''
def load_video(path):
    """Load a video.

    Example:
        >>> # replace this with your own video file
        >>> video = load_video("spaghetti.mp4")
        ... # add your logic here
        >>> clip = load_video("clip.mp4")  # Replace this with your own file
        >>> for frame in clip:
        ...     show(frame)  # your code here
    """
    return decode(path)
'''

CODE_BLOCK = '''
def config_context(**settings):
    """Change the settings for a block of code::

        with config_context(dispatch=True):
            # your code here

    Otherwise the defaults apply.
    """
    return apply(settings)
'''


@pytest.mark.parametrize("code", [DOCTEST, CODE_BLOCK])
def test_comment_inside_a_docstring_example_is_not_flagged(write, check, code):
    """A comment in example code that a docstring shows speaks to whoever copies the
    example, like code under examples/. Seen in transformers and scikit-learn."""
    write("src/app.py", code)
    assert check("SLOP010").findings == []


def test_docstring_prose_and_real_comments_are_still_flagged(write, check):
    """Only a line that is itself a code comment, or a doctest line, is example code.
    The prose of a docstring is checked, also a line that starts with "...", and so
    is every real comment."""
    write(
        "src/app.py",
        "def charge(card):\n"
        '    """Charge a card.\n'
        "\n"
        "    In a real implementation, this calls the payment provider.\n"
        "    ... rest of the implementation\n"
        '    """\n'
        "    # Your code here\n"
        "    return True\n",
    )
    assert rule_lines(check("SLOP010"), "SLOP010") == [4, 5, 7]


def test_chat_text_inside_a_docstring_example_is_still_flagged(write, check):
    """The exemption is for placeholders only: chat text has no place in an example."""
    write(
        "src/app.py",
        'def f():\n    """Run.\n\n    >>> # I hope this helps!\n    """\n    return g()\n',
    )
    assert rule_lines(check("SLOP051"), "SLOP051") == [4]
