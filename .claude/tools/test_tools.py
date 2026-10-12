"""Tests for the Claude Code hooks and tool scripts in this folder.

They are not part of the package's test suite. Run them with:
    .venv/bin/python -m pytest .claude/tools -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent
posix_only = pytest.mark.skipif(os.name == "nt", reason="uses a shell shim for the virtualenv")


def git(cwd: Path, *args: str) -> None:
    """Run a git command that must succeed."""
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def make_repo(path: Path, branch: str = "main") -> Path:
    """Create a git repository with one commit on ``branch``."""
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", branch)
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "test")
    (path / "README.md").write_text("# demo\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "base")
    return path


def add_venv(repo: Path) -> None:
    """Give ``repo`` a ``.venv`` whose python is the interpreter running these tests."""
    shim = repo / ".venv" / "bin" / "python"
    shim.parent.mkdir(parents=True)
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    (repo / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-qm", "ignore venv")


def run_hook(script: str, event: dict, project: Path) -> dict | str | None:
    """Run a hook script with ``event`` on stdin; return its JSON output, text, or None."""
    done = subprocess.run(
        [sys.executable, str(TOOLS / script)],
        input=json.dumps(event),
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(project)},
        check=False,
    )
    assert done.returncode == 0, done.stderr
    out = done.stdout.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return out


def bash_decision(command: str, project: Path, cwd: Path | None = None) -> str:
    """What the git guard decides for a Bash command: ``deny`` or ``allow``."""
    event = {"tool_name": "Bash", "cwd": str(cwd or project), "tool_input": {"command": command}}
    result = run_hook("hook_guard_git.py", event, project)
    if result is None:
        return "allow"
    assert isinstance(result, dict)
    return result["hookSpecificOutput"]["permissionDecision"]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A repository on ``main`` that plays the role of this project."""
    return make_repo(tmp_path / "project")


@pytest.fixture
def worktree(project: Path, tmp_path: Path) -> Path:
    """A second checkout of the project, on the feature branch ``feat/y``."""
    path = tmp_path / "wt"
    git(project, "worktree", "add", "-q", "-b", "feat/y", str(path))
    return path


def link_folder(link: Path, target: Path) -> None:
    """Make ``link`` a symbolic link to the folder ``target``; skip the test where that fails."""
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links can't be created here")


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m x",
        "git commit --amend --no-edit",
        "GIT_EDITOR=true git commit",
        "git add -A && git commit -m x",
        "git add -A\ngit commit -m x",
        "git commit -F - <<EOF\ndon't lose this\nEOF",
        "git push",
        "git push origin main",
        "git push origin HEAD:main",
        "git push --force origin +refs/heads/main",
        "git push origin HEAD",
        "sudo git commit -m x",
        "env GIT_EDITOR=true git commit",
        "git push -o ci.skip origin",
        "git push -o ci.skip origin main",
    ],
)
def test_guard_denies_on_protected_branch(project: Path, command: str) -> None:
    """Commits on main, and pushes that update main, are denied."""
    assert bash_decision(command, project) == "deny"


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m x",
        "git push",
        "git push -u origin feat/x",
        "git push origin HEAD",
        "git push origin v1.0.0",
        "git push origin -o main feat/x",
        "git push --push-option main -u origin feat/x",
    ],
)
def test_guard_allows_on_feature_branch(project: Path, command: str) -> None:
    """The same commands are fine once the checkout is on a feature branch."""
    git(project, "switch", "-qc", "feat/x")
    assert bash_decision(command, project) == "allow"


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "git status",
        "echo git commit",
        "git log main",
        "# git commit -m x",
        "echo done # ; git commit -m x",
    ],
)
def test_guard_ignores_other_commands(project: Path, command: str) -> None:
    """Commands that neither commit nor push are never touched."""
    assert bash_decision(command, project) == "allow"


def test_guard_follows_the_folder_the_command_runs_in(project: Path, worktree: Path) -> None:
    """``git -C`` and ``cd`` decide which checkout's branch counts, not the session folder."""
    assert bash_decision(f"git -C {worktree} commit -m x", project) == "allow"
    assert bash_decision(f"cd {worktree} && git commit -m x", project) == "allow"
    assert bash_decision(f"git -C {project} commit -m x", project, cwd=worktree) == "deny"


def test_guard_follows_variables_set_on_the_command_line(project: Path, worktree: Path) -> None:
    """A folder reached through ``W=...; cd $W`` counts, in both directions."""
    assert bash_decision(f"W={worktree}; cd $W && git commit -m x", project) == "allow"
    assert bash_decision(f'export W={worktree} && git -C "${{W}}" commit -m x', project) == "allow"
    assert bash_decision(f"W={project}; cd $W && git commit -m x", project, cwd=worktree) == "deny"
    assert bash_decision(f"W={project}\ngit -C $W push", project, cwd=worktree) == "deny"


@pytest.mark.parametrize("reference", ["'$W'", "\\$W", "'${W}'"])
def test_guard_does_not_expand_a_variable_the_shell_takes_as_text(
    project: Path, worktree: Path, reference: str
) -> None:
    """``cd '$W'`` goes to a folder named ``$W``, not to the value of ``W``.

    That folder does not exist, so the shell stays in the main checkout and the
    commit is on main, although ``W`` names a feature-branch worktree.
    """
    assert bash_decision(f"W={worktree}; cd {reference}; git commit -m x", project) == "deny"
    assert bash_decision(f'W={worktree}; cd "$W"; git commit -m x', project) == "allow"


@pytest.mark.parametrize(
    "second_assignment",
    ["W=$(pwd)", "W+=/sub", "read W", "for W in a b", "unset W", "declare W"],
)
def test_guard_forgets_a_variable_that_gets_an_unknown_value(
    project: Path, worktree: Path, second_assignment: str
) -> None:
    """A variable set again to something the guard can't follow no longer has its old value.

    Started in a feature-branch worktree, ``W`` first names the main checkout. If the
    guard kept that value it would refuse a commit that happens somewhere else.
    """
    command = f'W={project}; {second_assignment}; cd "$W" && git commit -m x'
    assert bash_decision(command, project, cwd=worktree) == "allow"


@pytest.mark.parametrize(
    "command",
    [
        "cd $NO_SUCH_VARIABLE_HERE && git commit -m x",
        "git -C $NO_SUCH_VARIABLE_HERE commit -m x",
        "cd $(mktemp -d) && git commit -m x",
        "W=$(pwd)/elsewhere; cd $W && git push",
        "git -C /no/such/folder/anywhere commit -m x",
        "mkdir -p /no/such/folder/demo && cd /no/such/folder/demo && git commit -m x",
        "mkdir demo\ncd demo\ngit init -q\ngit commit -m x",
        "git clone https://example.com/r.git ../fresh && cd ../fresh && git commit -m x",
        "mkdir -p out/a/b && cd out && git commit -m x",
        "sudo mkdir -p /no/such/folder/made && cd /no/such/folder/made && git commit -m x",
        "git worktree add ../wt-new -b feat/new && cd ../wt-new && git commit -m x",
    ],
)
def test_guard_lets_through_what_it_cannot_place(project: Path, command: str) -> None:
    """When the folder a git command runs in can't be known, the guard does not guess.

    That includes a folder that is missing now but that an earlier command on the
    same line names, and so may create.
    """
    assert bash_decision(command, project) == "allow"


@pytest.mark.parametrize(
    "command",
    [
        "cd /no/such/folder/anywhere; git commit -m x",
        "cd /no/such/folder/anywhere\ngit commit -m x",
        "cd /no/such/folder/anywhere || git commit -m x",
        "mkdir other && cd /no/such/folder/anywhere; git commit -m x",
        "echo /no/such/folder/anywhere; cd /no/such/folder/anywhere; git commit -m x",
        "ls /no/such/folder/anywhere || true; cd /no/such/folder/anywhere; git commit -m x",
    ],
)
def test_guard_stays_put_after_a_cd_that_fails(project: Path, command: str) -> None:
    """A ``cd`` to a folder nothing creates fails, so the commit happens where the shell was."""
    assert bash_decision(command, project) == "deny"


def test_guard_stays_put_after_a_cd_that_names_two_folders(project: Path, worktree: Path) -> None:
    """The shell refuses ``cd a b`` ("too many arguments"), so it goes nowhere.

    It is a ``cd`` that failed, so a later ``cd -`` does not go back to it either.
    """
    assert bash_decision(f"cd {worktree} extra; git commit -m x", project) == "deny"
    assert bash_decision(f"cd {project} extra; git commit -m x", project, cwd=worktree) == "allow"
    command = f"cd {worktree}; cd {project} extra; cd -; git commit -m x"
    assert bash_decision(command, project) == "deny"


@pytest.mark.parametrize(
    ("moves", "decision"),
    [
        ("cd {worktree}; cd -", "deny"),
        ("cd {worktree} && cd - && cd -", "allow"),
        ("cd {worktree}; cd /no/such/folder/anywhere; cd -", "deny"),
        ("cd {worktree}; cd .; cd -", "allow"),
        ("cd {worktree}; cd $NO_SUCH_VARIABLE_HERE; cd -", "allow"),
        ("cd -", "allow"),
    ],
)
def test_guard_goes_back_with_cd_dash(
    project: Path, worktree: Path, moves: str, decision: str
) -> None:
    """``cd -`` returns to the folder the shell was in before the last ``cd`` that worked.

    The session starts in the main checkout. Where the shell was before that is not
    known, so a ``cd -`` with no ``cd`` before it leaves the folder unknown.
    """
    command = moves.format(worktree=worktree) + "; git commit -m x"
    assert bash_decision(command, project) == decision


@pytest.mark.parametrize("options", ["--", "-L", "-P", "-L --", "-LP"])
def test_guard_reads_past_the_options_of_cd(project: Path, worktree: Path, options: str) -> None:
    """An option before the folder is not taken for the folder."""
    assert bash_decision(f"cd {options} {worktree} && git commit -m x", project) == "allow"
    command = f"cd {options} {project} && git commit -m x"
    assert bash_decision(command, project, cwd=worktree) == "deny"


@pytest.mark.parametrize(
    ("command", "decision"),
    [
        ("git -C link/.. commit -m x", "deny"),
        ("cd link && git -C .. commit -m x", "deny"),
        ("cd -P link/.. && git commit -m x", "deny"),
        ("cd -PL link/.. && git commit -m x", "allow"),
        ("cd link/.. && git commit -m x", "allow"),
        ("cd link && cd .. && git commit -m x", "allow"),
    ],
)
def test_guard_follows_a_link_the_way_the_command_does(
    project: Path, worktree: Path, command: str, decision: str
) -> None:
    """``link/..`` is the folder above the link's target for git, and above the link for ``cd``.

    ``link`` is in the feature-branch worktree and points into the main checkout, so
    the two readings land on different branches.
    """
    (project / "docs").mkdir()
    link_folder(worktree / "link", project / "docs")
    assert bash_decision(command, project, cwd=worktree) == decision


@pytest.mark.parametrize(
    "command",
    [
        "python3 - <<'EOF'\nprint('then run: git commit -m x')\nEOF",
        "cat > notes.txt <<EOF\ngit push origin main\nEOF",
        "cat <<-EOF > notes.txt\n\tgit commit --amend\n\tEOF\necho done",
        "python3 - <<'PY' && echo ok\ntext = 'git push'\nPY",
        "cat <<'EOF'\n$(git commit -m x)\nEOF",
        "cat <<\\EOF\n`git commit -m x`\nEOF",
        'cat <<"E"OF\n$(git commit -m x)\nEOF',
        "cat <<EOF\n$(printf ok) git commit -m x\nEOF",
        "cat <<EOF\ncost: \\$(git commit -m x)\nEOF",
        "printf bash <<'EOF'\ngit commit -m x\nEOF",
        "echo bash; cat <<'EOF'\ngit commit -m x\nEOF",
        "echo \"$(cat <<'EOF'\nhow to: git commit, then git push\nEOF\n)\"",
    ],
)
def test_guard_ignores_here_document_bodies(project: Path, command: str) -> None:
    """Text fed to another program is data: mentioning a commit there is not a commit."""
    assert bash_decision(command, project) == "allow"


@pytest.mark.parametrize(
    "command",
    [
        "cat > notes.txt <<EOF\nsome notes\nEOF\ngit commit -m x",
        "git commit -F - <<'EOF'\nfix: it's done\nEOF",
        "git commit -m \"$(cat <<'EOF'\nfix: it's done\nEOF\n)\"",
        "python3 -c 'print(1 << 4)' && git commit -m x",
        "echo $((1 << n))\ngit commit -m x\nn",
        "echo hi <<< text && git push",
    ],
)
def test_guard_still_sees_commands_around_a_here_document(project: Path, command: str) -> None:
    """A real commit before, after or fed by a here-document is still denied on main."""
    assert bash_decision(command, project) == "deny"


@pytest.mark.parametrize(
    "command",
    [
        "cat <<EOF\n$(git commit -m x)\nEOF",
        "cat <<EOF\nresult: `git commit -m x`\nEOF",
        "cat <<EOF\n$(echo $(git commit -m x))\nEOF",
        "cat <<EOF\n$(printf ')' ; git commit -m x)\nEOF",
        'cat <<EOF\n$(printf "a)b" && git commit -m x)\nEOF',
        "bash <<'EOF'\ngit commit -m x\nEOF",
        "/bin/sh -s <<EOF\ncd . && git push\nEOF",
        "echo ok; sudo bash <<'EOF'\ngit commit -m x\nEOF",
        "FOO=1 /bin/bash -s <<EOF\ngit commit -m x\nEOF",
        "cat script | sh <<'EOF'\ngit push\nEOF",
    ],
)
def test_guard_reads_here_document_text_the_shell_runs(project: Path, command: str) -> None:
    """A command substitution in an unquoted body runs, and so does a body given to a shell."""
    assert bash_decision(command, project) == "deny"


@pytest.mark.parametrize(
    "command",
    [
        "echo '<<EOF'\ngit commit -m x",
        'echo "<<EOF"\ngit commit -m x',
        "echo hi # <<EOF\ngit commit -m x",
        "echo \\<<EOF\ngit commit -m x\nEOF",
        "cat <<E\\OF\ntext\nEOF\ngit commit -m x",
        "cat <<'E'\"OF\"\ntext\nEOF\ngit commit -m x",
    ],
)
def test_guard_takes_only_real_here_document_openers(project: Path, command: str) -> None:
    """``<<`` in quotes or in a comment opens nothing, so the next line is still a command."""
    assert bash_decision(command, project) == "deny"


def test_guard_leaves_other_repositories_alone(project: Path, tmp_path: Path) -> None:
    """A throwaway repository on main (a test fixture) can be committed to."""
    other = make_repo(tmp_path / "other")
    assert bash_decision(f"cd {other} && git commit -m x", project) == "allow"
    assert bash_decision("git commit -m x", project, cwd=other) == "allow"


def edit_event(path: Path) -> dict:
    """The event Claude Code sends for an Edit or Write of ``path``."""
    return {"tool_name": "Edit", "cwd": str(path.parent), "tool_input": {"file_path": str(path)}}


def test_protected_file_asks_first(project: Path) -> None:
    """Editing the sloppy CI fixture becomes a question; ordinary files pass through."""
    fixture = project / ".github" / "action-fixture" / "app.py"
    result = run_hook("hook_protect_files.py", edit_event(fixture), project)
    assert isinstance(result, dict)
    assert result["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert "sloppy on purpose" in result["hookSpecificOutput"]["permissionDecisionReason"]
    assert run_hook("hook_protect_files.py", edit_event(project / "README.md"), project) is None


def test_protected_names_in_other_repositories_are_free(project: Path, tmp_path: Path) -> None:
    """A LICENSE in some other repository is none of this hook's business."""
    other = make_repo(tmp_path / "other")
    assert run_hook("hook_protect_files.py", edit_event(other / "LICENSE"), project) is None


@posix_only
def test_format_hook_formats_python_only(project: Path) -> None:
    """An edited Python file is reformatted; other files and other repositories are not."""
    add_venv(project)
    messy = "x   =  [1,2,\n3]\n"
    code, text = project / "app.py", project / "notes.txt"
    code.write_text(messy, encoding="utf-8")
    text.write_text(messy, encoding="utf-8")
    assert run_hook("hook_format_python.py", edit_event(code), project) is None
    assert run_hook("hook_format_python.py", edit_event(text), project) is None
    assert code.read_text(encoding="utf-8") == "x = [1, 2, 3]\n"
    assert text.read_text(encoding="utf-8") == messy


def stop_event(project: Path, active: bool = False) -> dict:
    """The event Claude Code sends when a turn is about to end."""
    return {"hook_event_name": "Stop", "cwd": str(project), "stop_hook_active": active}


@posix_only
def test_stop_gate_blocks_failing_changes_once(project: Path) -> None:
    """Changed Python that fails the quick checks blocks the stop, but only the first time."""
    add_venv(project)
    (project / "src").mkdir()
    (project / "src" / "app.py").write_text("import os\n", encoding="utf-8")  # unused import
    result = run_hook("hook_stop_gate.py", stop_event(project), project)
    assert isinstance(result, dict)
    assert result["decision"] == "block"
    assert "FAIL - ruff check" in result["reason"]
    assert run_hook("hook_stop_gate.py", stop_event(project, active=True), project) is None


def test_stop_gate_is_silent_without_python_changes(project: Path) -> None:
    """No changed Python under src/ or tests/ means nothing runs."""
    (project / "notes.md").write_text("hello\n", encoding="utf-8")
    assert run_hook("hook_stop_gate.py", stop_event(project), project) is None


def test_stop_gate_never_blocks_when_checks_cannot_run(project: Path) -> None:
    """Without a virtualenv the checks can't run, and the session must not be trapped."""
    (project / "src").mkdir()
    (project / "src" / "app.py").write_text("import os\n", encoding="utf-8")
    assert run_hook("hook_stop_gate.py", stop_event(project), project) is None


def test_stop_gate_says_so_when_the_checks_do_not_finish(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A timeout lets the turn end, with a message that nothing was checked."""
    import hook_stop_gate

    def never_finishes(root: Path) -> None:
        """Stand in for a preflight run that hangs."""
        raise subprocess.TimeoutExpired(cmd="preflight", timeout=1)

    (project / "src").mkdir()
    (project / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    monkeypatch.setattr(hook_stop_gate, "read_event", lambda: stop_event(project))
    monkeypatch.setattr(hook_stop_gate, "run_quick_checks", never_finishes)

    assert hook_stop_gate.main() == 0
    message = json.loads(capsys.readouterr().out)
    assert "did not finish" in message["systemMessage"]
    assert "decision" not in message


def test_preflight_fails_a_command_that_hangs(tmp_path: Path) -> None:
    """A command that outlives the timeout fails its check instead of hanging preflight."""
    import preflight

    sleeper = preflight.Check("sleeps", ((sys.executable, "-c", "import time; time.sleep(30)"),))
    passed, output = preflight.run_check(sleeper, tmp_path, timeout=0.5)
    assert not passed
    assert "did not finish" in output


def test_session_context_states_the_branch(project: Path) -> None:
    """On main the context says the branch is protected; elsewhere it names the branch."""
    on_main = run_hook("hook_session_context.py", {"cwd": str(project)}, project)
    assert isinstance(on_main, str)
    assert "'main', which is protected" in on_main
    assert "no .venv" in on_main
    git(project, "switch", "-qc", "fix/z")
    on_branch = run_hook("hook_session_context.py", {"cwd": str(project)}, project)
    assert isinstance(on_branch, str)
    assert "branch 'fix/z'" in on_branch


def docstring_share(repo: Path) -> subprocess.CompletedProcess[str]:
    """Run the docstring coverage script against ``main`` in ``repo``."""
    return subprocess.run(
        [sys.executable, str(TOOLS / "docstring_coverage.py"), "--base", "main", "--min", "80"],
        cwd=repo,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )


def test_docstring_coverage_counts_touched_functions_only(project: Path) -> None:
    """Only functions changed since the base count, and a missing docstring is listed."""
    module = project / "mod.py"
    module.write_text("def old():\n    return 1\n", encoding="utf-8")
    git(project, "add", "-A")
    git(project, "commit", "-qm", "old code")
    git(project, "switch", "-qc", "feat/doc")

    module.write_text("def old():\n    return 1\n\n\ndef new():\n    return 2\n", encoding="utf-8")
    failing = docstring_share(project)
    assert failing.returncode == 1
    assert "0 of 1 touched functions" in failing.stdout
    assert "mod.py:5  new" in failing.stdout

    module.write_text(
        'def old():\n    return 1\n\n\ndef new():\n    """Return two."""\n    return 2\n',
        encoding="utf-8",
    )
    passing = docstring_share(project)
    assert passing.returncode == 0
    assert "1 of 1 touched functions" in passing.stdout


def test_corpus_run_reports_counts_and_a_sample(tmp_path: Path) -> None:
    """The corpus script counts findings and prints the flagged line with context."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "app.py").write_text(
        "def load():\n    try:\n        return 1\n    except Exception:\n        pass\n",
        encoding="utf-8",
    )
    done = subprocess.run(
        [sys.executable, str(TOOLS / "corpus_run.py"), str(corpus), "--rule", "slop040"],
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "findings:  1 in 1 files" in done.stdout
    assert ">     4      except Exception:" in done.stdout
