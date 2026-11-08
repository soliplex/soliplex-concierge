"""Check skill manifests against trees built with real git objects."""

import subprocess
import sys
import time

import pytest

from soliplex_concierge.skills_deployer.manifest import _records
from soliplex_concierge.skills_deployer.manifest import _TreeLimitError
from soliplex_concierge.skills_deployer.manifest import read_tree
from soliplex_concierge.skills_deployer.model import Check
from soliplex_concierge.skills_deployer.model import Entry
from soliplex_concierge.skills_deployer.model import Limits


def _git(repo, *args, input=None):
    """Run git in a test repository and return its output bytes."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        input=input,
        capture_output=True,
        check=True,
    ).stdout


def _head(repo):
    """Return the full committed object id."""
    return _git(repo, "rev-parse", "HEAD").decode().strip()


def _tree(repo, records):
    """Write a tree from raw git records."""
    return _git(repo, "mktree", "-z", input=b"\0".join(records)).strip()


def _commit_tree(repo, tree):
    """Commit a plumbing-built tree and return its object id."""
    return _git(repo, "commit-tree", tree, input=b"Tree\n").decode().strip()


@pytest.fixture
def repository(tmp_path):
    """Build committed trees with files, modes, symlinks and gitlinks."""

    def build(files, *, modes=None, symlinks=None, gitlinks=()):
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "--quiet")
        _git(repo, "config", "user.name", "Manifest tests")
        _git(repo, "config", "user.email", "manifest@example.invalid")
        _git(repo, "config", "commit.gpgsign", "false")
        _git(repo, "config", "core.ignorecase", "false")
        _git(repo, "config", "core.precomposeunicode", "false")
        _git(repo, "commit", "--quiet", "--allow-empty", "-m", "Base")
        base = _git(repo, "rev-parse", "HEAD").decode().strip()
        for path, content in files.items():
            target = repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            mode = (modes or {}).get(path, "100644")
            target.chmod(int(mode[-3:], 8))
            oid = (
                _git(
                    repo,
                    "hash-object",
                    "-w",
                    "--stdin",
                    input=content.encode(),
                )
                .decode()
                .strip()
            )
            _git(
                repo,
                "update-index",
                "--add",
                "--cacheinfo",
                f"{mode},{oid},{path}",
            )
        for path, link_target in (symlinks or {}).items():
            target = repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(link_target)
            oid = (
                _git(
                    repo,
                    "hash-object",
                    "-w",
                    "--stdin",
                    input=link_target.encode(),
                )
                .decode()
                .strip()
            )
            _git(
                repo,
                "update-index",
                "--add",
                "--cacheinfo",
                f"120000,{oid},{path}",
            )
        for path in gitlinks:
            _git(
                repo,
                "update-index",
                "--add",
                "--cacheinfo",
                f"160000,{base},{path}",
            )
        _git(repo, "commit", "--quiet", "--allow-empty", "-m", "Skills")
        return repo

    return build


def test_read_tree_uses_revision_objects_and_full_paths(repository):
    repo = repository({"acme-a/SKILL.md": "committed"})
    revision = _git(repo, "rev-parse", "HEAD").decode().strip()
    (repo / "acme-a/SKILL.md").write_text("dirty", encoding="utf-8")
    (repo / "untracked.txt").write_text("untracked", encoding="utf-8")
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "-m", "Different tree")
    (repo / "acme-a/SKILL.md").unlink()

    entries, violations = read_tree(repo / "acme-a", revision)

    assert violations == []
    assert [(entry.path, entry.size) for entry in entries] == [
        ("acme-a/SKILL.md", len("committed"))
    ]


def test_non_utf8_paths_continue_reading(repository):
    repo = repository({"acme-a/SKILL.md": "ok"})
    raw_paths = [b"acme-a/resources/invalid-\xff.txt", b"acme-a/\xfe.txt"]
    oid = _git(repo, "hash-object", "-w", "--stdin", input=b"x").strip()
    for raw_path in raw_paths:
        _git(
            repo,
            "update-index",
            "--add",
            "--cacheinfo",
            b"100644," + oid + b"," + raw_path,
        )
    _git(repo, "commit", "--quiet", "-m", "Non-UTF-8 paths")

    entries, violations = read_tree(repo, _head(repo))

    assert [entry.path for entry in entries] == [
        "acme-a/SKILL.md",
        "acme-a/resources",
    ]
    assert len(violations) == 2
    assert {v.path for v in violations} == {repr(p) for p in raw_paths}
    for violation in violations:
        assert violation.check == Check.LAYOUT
        assert violation.path in violation.message
        assert "UTF-8" in violation.message


def test_unknown_revision(repository):
    repo = repository({})
    revision = "0" * 40
    result = subprocess.run(
        [
            "git",
            "--no-replace-objects",
            "-C",
            str(repo),
            "ls-tree",
            "-z",
            "--full-tree",
            "--end-of-options",
            revision,
        ],
        capture_output=True,
        check=False,
    )

    with pytest.raises(ValueError, match="Unknown revision or object"):
        read_tree(repo, revision)

    with pytest.raises(ValueError, match="not a tree object") as caught:
        list(_records(result.args, 10000, time.monotonic() + 5))
    assert str(caught.value) == result.stderr.decode()


@pytest.mark.parametrize(
    "revision",
    [
        "HEAD",
        "--help",
        "-r",
        "a" * 39,
        "a" * 41,
        "A" * 40,
        "g" * 40,
        "a" * 63,
        "a" * 65,
        "a" * 40 + "\n",
    ],
)
def test_revision_requires_full_lowercase_object_id(repository, revision):
    repo = repository({})

    with pytest.raises(ValueError, match="full 40 or 64 character"):
        read_tree(repo, revision)


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_revision_object_formats_and_end_of_options(tmp_path, object_format):
    repo = tmp_path / "objects"
    repo.mkdir()
    _git(repo, "init", "--quiet", f"--object-format={object_format}")
    _git(repo, "config", "user.name", "Manifest tests")
    _git(repo, "config", "user.email", "manifest@example.invalid")
    blob = _git(repo, "hash-object", "-w", "--stdin", input=b"x").strip()
    child = _tree(repo, [b"100644 blob " + blob + b"\tx"])
    tree = _tree(repo, [b"040000 tree " + child + b"\tnested"])
    entries, violations = read_tree(repo, _commit_tree(repo, tree))
    assert violations == []
    assert entries == [Entry("100644", blob.decode(), 1, "nested/x")]
    assert len(entries[0].oid) == (40 if object_format == "sha1" else 64)


def test_shared_subtree_bomb_is_bounded(repository):
    repo = repository({})
    blob = _git(repo, "hash-object", "-w", "--stdin", input=b"x").strip()
    tree = _tree(repo, [b"100644 blob " + blob + b"\tx"])
    for _ in range(30):
        tree = _tree(
            repo,
            [
                b"040000 tree " + tree + b"\ta",
                b"040000 tree " + tree + b"\tb",
            ],
        )
    revision = _commit_tree(repo, tree)
    started = time.monotonic()

    entries, violations = read_tree(repo, revision, max_records=10)

    assert time.monotonic() - started < 5
    assert entries == []
    assert len(violations) == 1
    assert violations[0].check == Check.LIMITS
    assert violations[0].path == ""
    assert "record count exceeds 10" in violations[0].message


@pytest.mark.parametrize("nested", [False, True])
def test_oversized_tree_is_rejected_without_listing(
    repository, monkeypatch, nested
):
    repo = repository({"acme-a/SKILL.md": "x"})
    blob = _git(repo, "rev-parse", "HEAD:acme-a/SKILL.md").strip()
    oversized = _tree(
        repo,
        [b"100644 blob " + blob + f"\tentry-{i}".encode() for i in range(32)],
    )
    root = (
        _tree(repo, [b"040000 tree " + oversized + b"\tacme-a"])
        if nested
        else oversized
    )
    revision = _commit_tree(repo, root)
    listed = []
    popen = subprocess.Popen

    def record_listing(command, **kwargs):
        """Observe tree listings while running the real git executable."""
        if "ls-tree" in command:
            listed.append(command[-1])
        return popen(command, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", record_listing)

    entries, violations = read_tree(repo, revision, max_tree_bytes=256)

    assert entries == []
    assert len(violations) == 1
    assert violations[0].check == Check.LIMITS
    assert violations[0].path == ""
    assert "exceeds 256 bytes" in violations[0].message
    assert listed == ([root.decode()] if nested else [])


def test_oversized_commit_is_rejected_without_reading(repository, monkeypatch):
    repo = repository({})
    tree = _git(repo, "rev-parse", "HEAD^{tree}").strip()
    revision = (
        _git(repo, "commit-tree", tree, input=b"x" * (64 * 1024) + b"\n")
        .decode()
        .strip()
    )
    size = int(_git(repo, "cat-file", "-s", revision))
    assert read_tree(repo, revision, max_tree_bytes=size) == ([], [])
    commands = []
    popen = subprocess.Popen

    def record_command(command, **kwargs):
        """Observe commands while running the real git executable."""
        commands.append(command)
        return popen(command, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", record_command)

    entries, violations = read_tree(repo, revision, max_tree_bytes=256)

    assert entries == []
    assert [(v.check, v.path, v.message) for v in violations] == [
        (Check.LIMITS, "", f"Git commit size {size} exceeds 256 bytes")
    ]
    assert commands == [
        [
            "git",
            "--no-replace-objects",
            "-C",
            str(repo),
            "cat-file",
            "-Z",
            "--batch-check=%(objecttype) %(objectsize)",
        ]
    ]


@pytest.mark.parametrize(
    "kind, reference",
    [
        ("tag", "release"),
        ("tree", "HEAD^{tree}"),
        ("blob", "HEAD:acme-a/SKILL.md"),
    ],
)
def test_revision_requires_commit_object(repository, kind, reference):
    repo = repository({"acme-a/SKILL.md": "x"})
    _git(repo, "tag", "--annotate", "--message", "Release", "release")
    revision = _git(repo, "rev-parse", reference).decode().strip()

    with pytest.raises(
        ValueError, match=f"Expected commit object, got {kind}"
    ):
        read_tree(repo, revision)


def test_record_budget_covers_the_whole_walk(repository):
    repo = repository({"acme-a/SKILL.md": "x", "acme-b/SKILL.md": "x"})

    entries, violations = read_tree(repo, _head(repo), max_records=3)

    assert entries == []
    assert [(v.check, v.path, v.message) for v in violations] == [
        (Check.LIMITS, "", "Git tree record count exceeds 3")
    ]
    entries, violations = read_tree(repo, _head(repo), max_records=4)
    assert violations == []
    assert {entry.path for entry in entries} == {
        "acme-a/SKILL.md",
        "acme-b/SKILL.md",
    }


@pytest.mark.parametrize("length", [8500, 13000])
def test_oversized_tree_record_is_bounded(repository, length):
    repo = repository({})
    blob = _git(repo, "hash-object", "-w", "--stdin", input=b"x").strip()
    tree = _tree(repo, [b"100644 blob " + blob + b"\t" + b"x" * length])

    entries, violations = read_tree(repo, _commit_tree(repo, tree))

    assert entries == []
    assert [(v.check, v.path, v.message) for v in violations] == [
        (Check.LIMITS, "", "Git tree record exceeds 8192 bytes")
    ]


def test_full_path_record_is_bounded_across_trees(repository):
    repo = repository({})
    blob = _git(repo, "hash-object", "-w", "--stdin", input=b"x").strip()
    child = _tree(repo, [b"100644 blob " + blob + b"\t" + b"x" * 5000])
    root = _tree(repo, [b"040000 tree " + child + b"\t" + b"y" * 5000])

    entries, violations = read_tree(repo, _commit_tree(repo, root))

    assert entries == []
    assert [(v.check, v.path, v.message) for v in violations] == [
        (Check.LIMITS, "", "Git tree record exceeds 8192 bytes")
    ]


def test_tree_read_timeout_is_bounded(repository):
    repo = repository({"acme-a/SKILL.md": "x"})
    started = time.monotonic()

    entries, violations = read_tree(repo, _head(repo), timeout=0)

    assert time.monotonic() - started < 5
    assert entries == []
    assert [(v.check, v.path, v.message) for v in violations] == [
        (Check.LIMITS, "", "Git tree read timeout exceeded")
    ]


def test_stderr_is_bounded(repository):
    repo = repository({})
    command = [
        "git",
        "-C",
        str(repo),
        "-c",
        "alias.noisy=!head -c 10000 /dev/zero >&2",
        "noisy",
    ]

    with pytest.raises(_TreeLimitError, match="stderr exceeds 8192 bytes"):
        list(_records(command, 10000, time.monotonic() + 5))


def test_commands_terminate_options_before_revision(repository, monkeypatch):
    repo = repository({"acme-a/SKILL.md": "x"})
    revision = _head(repo)
    root = _git(repo, "rev-parse", "HEAD^{tree}").decode().strip()
    skill = _git(repo, "rev-parse", "HEAD:acme-a").decode().strip()
    commands = []
    popen = subprocess.Popen

    def record_command(command, **kwargs):
        """Observe commands while running the real git executable."""
        commands.append(command)
        return popen(command, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", record_command)

    entries, violations = read_tree(repo, revision)

    assert violations == []
    assert len(entries) == 1
    assert commands == [
        [
            "git",
            "--no-replace-objects",
            "-C",
            str(repo),
            "cat-file",
            "-Z",
            "--batch-check=%(objecttype) %(objectsize)",
        ],
        [
            "git",
            "--no-replace-objects",
            "-C",
            str(repo),
            "cat-file",
            "commit",
            revision,
        ],
        [
            "git",
            "--no-replace-objects",
            "-C",
            str(repo),
            "cat-file",
            "-Z",
            "--batch-check=%(objecttype) %(objectsize)",
        ],
        [
            "git",
            "--no-replace-objects",
            "-C",
            str(repo),
            "ls-tree",
            "-z",
            "-l",
            "--full-tree",
            "--end-of-options",
            root,
        ],
        [
            "git",
            "--no-replace-objects",
            "-C",
            str(repo),
            "cat-file",
            "-Z",
            "--batch-check=%(objecttype) %(objectsize)",
        ],
        [
            "git",
            "--no-replace-objects",
            "-C",
            str(repo),
            "ls-tree",
            "-z",
            "-l",
            "--full-tree",
            "--end-of-options",
            skill,
        ],
    ]


def test_timeout_reaps_process_after_git_output_closes(
    repository, monkeypatch
):
    repo = repository({"acme-a/SKILL.md": "x"})
    command = [
        "sh",
        "-c",
        'git -C "$1" ls-tree -r -z --full-tree -l --end-of-options "$2"; '
        "exec 1>&- 2>&-; exec sleep 5",
        "git-tree",
        str(repo),
        _head(repo),
    ]
    processes = []
    popen = subprocess.Popen

    def record_process(command, **kwargs):
        """Observe the real child process through its cleanup."""
        process = popen(command, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", record_process)

    with pytest.raises(_TreeLimitError, match="timeout exceeded"):
        list(_records(command, 10000, time.monotonic() + 1))

    assert len(processes) == 1
    assert processes[0].returncode == -9


def test_record_and_count_exact_boundaries(repository):
    repo = repository({"acme-a/SKILL.md": "x"})
    blob = _git(repo, "rev-parse", "HEAD:acme-a/SKILL.md").strip()
    header = _git(repo, "ls-tree", "-l", "HEAD:acme-a").split(b"\t")[0]
    path = b"x" * (8192 - len(header) - 1)
    tree = _tree(repo, [b"100644 blob " + blob + b"\t" + path])

    entries, violations = read_tree(
        repo,
        _commit_tree(repo, tree),
        max_records=1,
        max_tree_bytes=int(_git(repo, "cat-file", "-s", tree)),
    )

    assert violations == []
    assert len(entries) == 1
    assert entries[0].path == path.decode()
    assert entries[0].size == 1


def test_non_utf8_empty_directory_continues_reading(repository):
    repo = repository({"acme-a/SKILL.md": "x"})
    skill = _git(repo, "rev-parse", "HEAD:acme-a").strip()
    empty = _tree(repo, [])
    tree = _tree(
        repo,
        [
            b"040000 tree " + empty + b"\t\xff",
            b"040000 tree " + skill + b"\tacme-a",
        ],
    )

    entries, violations = read_tree(repo, _commit_tree(repo, tree))

    assert [entry.path for entry in entries] == ["acme-a/SKILL.md"]
    assert len(violations) == 1
    assert violations[0].check == Check.LAYOUT
    assert violations[0].path == repr(b"\xff")


def test_empty_tree(repository):
    repo = repository({})
    assert read_tree(repo, _head(repo)) == ([], [])


def test_invalid_directory_is_not_descended(repository):
    repo = repository({})
    blob = _git(repo, "hash-object", "-w", "--stdin", input=b"x").strip()
    child = _tree(repo, [b"100644 blob " + blob + b"\tx"])
    invalid = _tree(repo, [b"040000 tree " + child + b"\t\xff"])
    root = _tree(repo, [b"040000 tree " + invalid + b"\tBadName"])
    entries, violations = read_tree(repo, _commit_tree(repo, root))
    assert [e.path for e in entries] == ["BadName"]
    assert len(violations) == 1
    assert violations[0].path == repr(b"BadName/\xff")


@pytest.mark.parametrize(
    "mode", ["100644", "100755", "040000", "120000", "160000"]
)
def test_dimensions_include_empty_and_invalid_entries(mode):
    from soliplex_concierge.skills_deployer.manifest import _check_limits

    path = "acme-a/SKILL.md"
    entries = [Entry(mode, "a" * 40, 2, path)]
    limits = Limits(
        max_skills=1,
        max_entries=1,
        max_depth=2,
        max_path_chars=len(path),
        max_file_bytes=2,
        max_version_bytes=2,
        max_skill_md_bytes=2,
    )
    assert _check_limits(entries, 1, limits) == []
    limits = Limits(
        max_skills=0,
        max_entries=0,
        max_depth=1,
        max_path_chars=len(path) - 1,
        max_file_bytes=1,
        max_version_bytes=1,
        max_skill_md_bytes=1,
    )
    violations = _check_limits(entries, 1, limits)
    assert [v.message for v in violations] == [
        "Skill count 1 exceeds 0",
        "Entry count 1 exceeds 0",
        "Total size in bytes 2 exceeds 1",
        "Path depth 2 exceeds 1",
        f"Path length in characters {len(path)} exceeds {len(path) - 1}",
        "File size in bytes 2 exceeds 1",
    ] + (
        ["SKILL.md size in bytes 2 exceeds 1"]
        if mode in {"100644", "100755"}
        else []
    )
    assert all(v.check == Check.LIMITS for v in violations)


def test_reader_keeps_forbidden_modes_and_blob_sizes(repository):
    repo = repository(
        {"acme-a/SKILL.md": "x", "acme-a/resources/executable": "xx"},
        modes={"acme-a/resources/executable": "100755"},
        symlinks={"acme-a/resources/link": "missing"},
        gitlinks=["acme-a/resources/module"],
    )
    entries, violations = read_tree(repo, _head(repo))
    assert violations == []
    assert {e.path: (e.mode, e.size) for e in entries} == {
        "acme-a/SKILL.md": ("100644", 1),
        "acme-a/resources/executable": ("100755", 2),
        "acme-a/resources/link": ("120000", 7),
        "acme-a/resources/module": ("160000", 0),
    }


@pytest.mark.parametrize("output", [b"partial", b"complete\0partial"])
def test_unterminated_subprocess_records(output):
    command = [sys.executable, "-c", f"import os; os.write(1, {output!r})"]
    with pytest.raises(ValueError, match="Unterminated Git output record"):
        list(_records(command, 10, time.monotonic() + 5))


def test_batch_input_is_bounded_before_starting_process():
    with pytest.raises(ValueError, match="Git batch input exceeds 65 bytes"):
        list(
            _records(
                ["unused-command"], 1, time.monotonic() + 5, input=b"x" * 65536
            )
        )


@pytest.mark.parametrize("metadata", [b"", b"commit", b"commit 10 extra"])
def test_missing_object_metadata_is_value_error(
    repository, monkeypatch, metadata
):
    from soliplex_concierge.skills_deployer import manifest

    repo = repository({})
    monkeypatch.setattr(
        manifest,
        "_records",
        lambda *args, **kwargs: (
            item for item in ([metadata] if metadata else [])
        ),
    )
    with pytest.raises(
        ValueError, match="Missing or malformed Git object metadata"
    ):
        read_tree(repo, _head(repo))


@pytest.mark.parametrize("header", [b"", b"tree", b"blob abc"])
def test_missing_commit_header_is_value_error(repository, monkeypatch, header):
    from soliplex_concierge.skills_deployer import manifest

    repo = repository({})

    def records(command, *args, **kwargs):
        return (
            item
            for item in (
                [b"commit 10"]
                if "-Z" in command
                else ([header] if header else [])
            )
        )

    monkeypatch.setattr(manifest, "_records", records)
    with pytest.raises(
        ValueError, match="Missing or malformed Git commit tree header"
    ):
        read_tree(repo, _head(repo))


@pytest.mark.parametrize("terminal_limit", [False, True])
def test_reader_sorts_and_retains_utf8_diagnostics(
    repository, monkeypatch, terminal_limit
):
    from soliplex_concierge.skills_deployer import manifest

    repo = repository({})

    def walk(*args):
        for path in [b"\xff", b"\xfe"]:
            yield [b"100644", b"blob", b"a" * 40, b"1"], path
        if terminal_limit:
            message = "record budget exhausted"
            raise _TreeLimitError(message)

    monkeypatch.setattr(manifest, "_walk_tree", walk)
    entries, violations = read_tree(repo, _head(repo))
    assert entries == []
    assert [v.path for v in violations] == [repr(b"\xfe"), repr(b"\xff")] + (
        [""] if terminal_limit else []
    )
    assert [v.check for v in violations] == [Check.LAYOUT, Check.LAYOUT] + (
        [Check.LIMITS] if terminal_limit else []
    )


def test_preserves_empty_directories_at_multiple_depths(repository):
    repo = repository({"acme-a/SKILL.md": "x"})
    blob = _git(repo, "rev-parse", "HEAD:acme-a/SKILL.md").strip()
    empty = _tree(repo, [])
    nested = _tree(repo, [b"040000 tree " + empty + b"\tdeep"])
    resources = _tree(
        repo,
        [
            b"040000 tree " + empty + b"\tempty",
            b"040000 tree " + nested + b"\tnested",
            b"100644 blob " + blob + b"\tpopulated.txt",
        ],
    )
    skill = _tree(
        repo,
        [
            b"100644 blob " + blob + b"\tSKILL.md",
            b"040000 tree " + resources + b"\tresources",
        ],
    )
    root = _tree(
        repo,
        [
            b"040000 tree " + skill + b"\tacme-a",
            b"040000 tree " + empty + b"\tempty",
        ],
    )

    entries, violations = read_tree(repo, _commit_tree(repo, root))

    assert violations == []
    assert sorted(entries, key=lambda entry: entry.path) == [
        Entry("100644", blob.decode(), 1, "acme-a/SKILL.md"),
        Entry("040000", empty.decode(), 0, "acme-a/resources/empty"),
        Entry("040000", empty.decode(), 0, "acme-a/resources/nested/deep"),
        Entry("100644", blob.decode(), 1, "acme-a/resources/populated.txt"),
        Entry("040000", empty.decode(), 0, "empty"),
    ]


@pytest.mark.parametrize("kind", ["commit", "tree", "blob"])
def test_read_tree_ignores_replacement_objects(repository, kind):
    repo = repository({"acme-a/SKILL.md": "original"})
    revision = _head(repo)
    original_tree = _git(repo, "rev-parse", "HEAD^{tree}").strip()
    original_blob = _git(repo, "rev-parse", "HEAD:acme-a/SKILL.md").strip()
    expected_entries, expected_violations = read_tree(repo, revision)
    assert expected_violations == []
    assert [(entry.path, entry.size) for entry in expected_entries] == [
        ("acme-a/SKILL.md", len("original"))
    ]
    replacement_blob = _git(
        repo, "hash-object", "-w", "--stdin", input=b"replacement content"
    ).strip()
    replacement_skill = _tree(
        repo, [b"100644 blob " + replacement_blob + b"\tSKILL.md"]
    )
    replacement_tree = _tree(
        repo, [b"040000 tree " + replacement_skill + b"\tacme-b"]
    )
    replacement_commit = _commit_tree(repo, replacement_tree)
    originals = {
        "commit": revision,
        "tree": original_tree,
        "blob": original_blob,
    }
    replacements = {
        "commit": replacement_commit,
        "tree": replacement_tree,
        "blob": replacement_blob,
    }
    _git(repo, "replace", originals[kind], replacements[kind])

    assert read_tree(repo, revision) == (expected_entries, [])
    entries, violations = read_tree(repo, revision, max_tree_bytes=1)
    assert entries == []
    assert len(violations) == 1
    assert violations[0].check == Check.LIMITS
    assert "exceeds 1 bytes" in violations[0].message
