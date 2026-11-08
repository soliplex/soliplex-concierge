"""Check skill manifests against trees built with real git objects."""

import dataclasses
import subprocess
import sys
import time

import pytest

from soliplex_concierge.skills_deployer.manifest import _records
from soliplex_concierge.skills_deployer.manifest import _TreeLimitError
from soliplex_concierge.skills_deployer.manifest import check_structure
from soliplex_concierge.skills_deployer.manifest import read_tree
from soliplex_concierge.skills_deployer.model import Check
from soliplex_concierge.skills_deployer.model import Entry
from soliplex_concierge.skills_deployer.model import Limits
from soliplex_concierge.skills_deployer.model import Violation


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


def _check(repo, *, team="acme", other_names=None, limits=None):
    """Check the structure of a committed test tree."""
    entries, violations = read_tree(repo, _head(repo))
    assert violations == []
    return check_structure(
        team,
        entries,
        other_names=other_names or {},
        limits=limits or Limits(),
    )


def test_valid_two_skill_tree(repository):
    files = {
        "acme-one/SKILL.md": "one",
        "acme-one/resources/tool.txt": "hello",
        "acme-two/SKILL.md": "two",
        "acme-two/resources/nested/é.txt": "é",
    }
    repo = repository(files, modes={"acme-one/resources/tool.txt": "100755"})

    entries, violations = read_tree(repo, _head(repo))

    assert violations == []
    assert {entry.path: entry.size for entry in entries} == {
        path: len(content.encode()) for path, content in files.items()
    }
    assert {entry.path: entry.mode for entry in entries} == {
        path: "100755" if path.endswith("tool.txt") else "100644"
        for path in files
    }
    for entry in entries:
        assert (
            entry.oid
            == _git(repo, "rev-parse", f"HEAD:{entry.path}").decode().strip()
        )
    assert (
        check_structure(
            "acme",
            entries,
            other_names={"trusted-skill": "trusted"},
            limits=Limits(),
        )
        == []
    )


def test_read_tree_preserves_delimiters_and_blob_sizes(repository):
    files = {
        "acme-a/SKILL.md": "",
        "acme-a/resources/with\ttab\nand newline.txt": "123456789012",
    }
    repo = repository(files)

    entries, violations = read_tree(repo, _head(repo))

    assert violations == []
    assert {entry.path: entry.size for entry in entries} == {
        path: len(content) for path, content in files.items()
    }
    assert _check(repo) == []


def test_empty_top_level_directory_counts_as_skill(repository):
    repo = repository({})
    empty = _tree(repo, [])
    tree = _tree(repo, [b"040000 tree " + empty + b"\tacme-empty"])

    entries, violations = read_tree(repo, _commit_tree(repo, tree))

    assert violations == []
    assert [(entry.path, entry.mode) for entry in entries] == [
        ("acme-empty", "040000")
    ]
    violations = check_structure(
        "acme", entries, other_names={}, limits=Limits(max_skills=0)
    )
    assert [(v.check, v.path, v.message) for v in violations] == [
        (
            Check.LAYOUT,
            "acme-empty",
            "Skill needs exactly one SKILL.md, found 0",
        ),
        (Check.LIMITS, "", "Skill count 1 exceeds 0"),
    ]


@pytest.mark.parametrize(
    "kind", ["empty-tree", "nonempty-tree", "gitlink", "symlink"]
)
def test_only_blobs_satisfy_skill_md(repository, kind):
    repo = repository({"acme-a/SKILL.md": "x"})
    blob = _git(repo, "rev-parse", "HEAD:acme-a/SKILL.md").strip()
    if kind == "gitlink":
        record = b"160000 commit " + _head(repo).encode()
    elif kind == "symlink":
        record = b"120000 blob " + blob
    else:
        records = (
            [b"100644 blob " + blob + b"\tfile"]
            if kind == "nonempty-tree"
            else []
        )
        record = b"040000 tree " + _tree(repo, records)
    skill = _tree(repo, [record + b"\tSKILL.md"])
    tree = _tree(repo, [b"040000 tree " + skill + b"\tacme-a"])
    entries, violations = read_tree(repo, _commit_tree(repo, tree))
    assert violations == []

    violations = check_structure(
        "acme",
        entries,
        other_names={},
        limits=Limits(max_skill_md_bytes=0),
    )

    path = "acme-a/SKILL.md"
    if kind == "nonempty-tree":
        path += "/file"
    assert [entry.path for entry in entries] == [path]
    expected = [
        Violation(
            Check.LAYOUT, "acme-a", "Skill needs exactly one SKILL.md, found 0"
        )
    ]
    if kind in {"empty-tree", "nonempty-tree"}:
        expected.extend(
            [
                Violation(
                    Check.LAYOUT,
                    path,
                    (
                        "Place files at <skill>/SKILL.md "
                        "or <skill>/resources/<path>"
                    ),
                ),
                Violation(
                    Check.LAYOUT,
                    path,
                    (
                        "Use SKILL.md only at <skill>/SKILL.md "
                        "with that exact casing"
                    ),
                ),
            ]
        )
    if kind in {"gitlink", "symlink"}:
        mode = "160000" if kind == "gitlink" else "120000"
        expected.append(
            Violation(
                Check.MODE,
                path,
                f"Mode {mode} is forbidden, use 100644 or 100755",
            )
        )
    assert violations == expected


@pytest.mark.parametrize(
    "path, message",
    [
        ("README.md", "Place files"),
        ("SKILL.md", "Use SKILL.md only"),
        (".gitattributes", "starting with '.'"),
        (".gitmodules", "starting with '.'"),
        (".gitconfig/SKILL.md", "starting with '.'"),
        ("acme-a/resources/.hidden/file.txt", "starting with '.'"),
        ("acme-a/resources/.gitignore", "starting with '.'"),
        ("acme-a/extra.txt", "Place files"),
        ("acme-a/resources", "Place files"),
        ("acme-a/skill.md", "Use SKILL.md only"),
        ("acme-a/SKILL.MD", "Use SKILL.md only"),
        ("acme-a/Skill.md", "Use SKILL.md only"),
        ("acme-a/resources/SKILL.md", "Use SKILL.md only"),
        ("acme-a/resources/skill.md/file.txt", "Use SKILL.md only"),
        ("SKILL.md/resources/file.txt", "Use SKILL.md only"),
        ("acme-a/SKILL.md/file.txt", "Use SKILL.md only"),
        ("acme-a/Resources/file.txt", "Spell every resources"),
        ("acme-a/RESOURCES/file.txt", "Spell every resources"),
        (
            "acme-a/resources/nested/Resources/file.txt",
            "Spell every resources",
        ),
    ],
)
def test_layout_rejects_paths(repository, path, message):
    repo = repository({path: "x"})

    violations = _check(repo)

    assert any(
        violation.check == Check.LAYOUT
        and violation.path == path
        and message in violation.message
        for violation in violations
    )


@pytest.mark.parametrize(
    "path",
    [
        "/acme-a/SKILL.md",
        "acme-a//resources/file.txt",
        "acme-a/resources/file.txt/",
        "acme-a/resources/../file.txt",
        "../SKILL.md",
        "",
    ],
)
def test_layout_rejects_unsafe_components(repository, path):
    repo = repository({"acme-a/SKILL.md": "x"})
    entries, _ = read_tree(repo, _head(repo))
    entries = [dataclasses.replace(entries[0], path=path)]

    violations = check_structure(
        "acme", entries, other_names={}, limits=Limits()
    )

    assert any(
        violation.check == Check.LAYOUT
        and violation.path == path
        and "relative paths" in violation.message
        for violation in violations
    )


@pytest.mark.parametrize("copies", [0, 2])
def test_each_skill_needs_exactly_one_skill_md(repository, copies):
    repo = repository(
        {"acme-a/SKILL.md": "x", "acme-a/resources/file.txt": "x"}
    )
    entries, _ = read_tree(repo, _head(repo))
    entries = [entries[0]] * copies + entries[1:]

    violations = check_structure(
        "acme", entries, other_names={}, limits=Limits()
    )

    assert [
        (violation.path, violation.message)
        for violation in violations
        if violation.check == Check.LAYOUT
    ] == [("acme-a", f"Skill needs exactly one SKILL.md, found {copies}")]


@pytest.mark.parametrize(
    "name, team, message",
    [
        ("acme-Bad", "acme", "must match"),
        ("acme-bad_name", "acme", "must match"),
        ("acme-bad--name", "acme", "must match"),
        ("acme-" + "x" * 60, "acme", "at most 64 characters"),
        ("soliplex-skill", "soliplex", "must not start with"),
        ("acme-a\n", "acme", "must match"),
    ],
)
def test_skill_names(repository, name, team, message):
    repo = repository({f"{name}/SKILL.md": "x"})

    violations = _check(repo, team=team)

    assert len(violations) == 1
    assert violations[0].check == Check.NAME
    assert violations[0].path == name
    assert message in violations[0].message


def test_longest_skill_name(repository):
    repo = repository({f"acme-{'x' * 59}/SKILL.md": "x"})

    violations = _check(repo)

    assert violations == []


@pytest.mark.parametrize("kind", ["symlink", "gitlink"])
def test_forbidden_modes(repository, kind):
    path = "acme-a/resources/link"
    options = (
        {"symlinks": {path: "missing-target"}}
        if kind == "symlink"
        else {"gitlinks": [path]}
    )
    repo = repository({"acme-a/SKILL.md": "x"}, **options)

    entries, violations = read_tree(repo, _head(repo))

    assert violations == []
    link = next(entry for entry in entries if entry.path == path)
    assert link.mode == ("120000" if kind == "symlink" else "160000")
    assert link.size == (len("missing-target") if kind == "symlink" else 0)
    assert link.oid == _git(repo, "rev-parse", f"HEAD:{path}").decode().strip()
    violations = check_structure(
        "acme", entries, other_names={}, limits=Limits()
    )
    assert len(violations) == 1
    assert violations[0].check == Check.MODE
    assert violations[0].path == path
    assert link.mode in violations[0].message


def test_other_modes_are_forbidden(repository):
    repo = repository({"acme-a/SKILL.md": "x"})
    entries, _ = read_tree(repo, _head(repo))
    entries = [dataclasses.replace(entries[0], mode="100600")]

    violations = check_structure(
        "acme", entries, other_names={}, limits=Limits()
    )

    assert [v.check for v in violations] == [Check.LAYOUT, Check.MODE]
    assert "found 0" in violations[0].message
    assert "100600" in violations[1].message


@pytest.mark.parametrize(
    "path",
    [
        "acme-a/scripts/tool.py",
        "acme-a/resources/scripts/tool.py",
        "acme-a/resources/nested/scripts/tool.py",
        "acme-a/resources/Scripts/tool.py",
        "acme-a/resources/SCRIPTS",
        "scripts/SKILL.md",
    ],
)
def test_scripts_at_any_depth(repository, path):
    repo = repository({path: "x"})

    violations = _check(repo)

    assert [v.path for v in violations if v.check == Check.SCRIPTS] == [path]


def test_script_substrings_are_allowed(repository):
    repo = repository(
        {
            "acme-scripts/SKILL.md": "x",
            "acme-scripts/resources/scripts.txt": "x",
        }
    )

    violations = _check(repo)

    assert violations == []


@pytest.mark.parametrize(
    "field, maximum, path, label",
    [
        ("max_skills", 0, "", "Skill count"),
        ("max_entries", 1, "", "Entry count"),
        ("max_depth", 2, "acme-a/resources/é.txt", "Path depth"),
        (
            "max_path_chars",
            len("acme-a/resources/é.txt") - 1,
            "acme-a/resources/é.txt",
            "Path length in characters",
        ),
        ("max_skill_md_bytes", 3, "acme-a/SKILL.md", "SKILL.md size in bytes"),
        ("max_file_bytes", 4, "acme-a/resources/é.txt", "File size in bytes"),
        ("max_version_bytes", 8, "", "Total size in bytes"),
    ],
)
def test_limits(repository, field, maximum, path, label):
    repo = repository(
        {"acme-a/SKILL.md": "éé", "acme-a/resources/é.txt": "12345"}
    )
    limits = dataclasses.replace(Limits(), **{field: maximum})

    violations = _check(repo, limits=limits)

    assert len(violations) == 1
    assert violations[0].check == Check.LIMITS
    assert violations[0].path == path
    assert violations[0].message == f"{label} {maximum + 1} exceeds {maximum}"


def test_limits_accept_exact_boundaries(repository):
    repo = repository(
        {"acme-a/SKILL.md": "éé", "acme-a/resources/é.txt": "12345"}
    )
    limits = Limits(
        max_skills=1,
        max_entries=2,
        max_depth=3,
        max_path_chars=len("acme-a/resources/é.txt"),
        max_skill_md_bytes=4,
        max_file_bytes=5,
        max_version_bytes=9,
    )

    violations = _check(repo, limits=limits)

    assert violations == []


def test_limits_count_invalid_entries_and_distinct_skill_directories(
    repository,
):
    repo = repository(
        {"Bad/resources/file": "xx", "acme-a/SKILL.md": "x"},
        symlinks={"acme-a/resources/link": "missing"},
        gitlinks=["acme-a/resources/submodule"],
    )

    violations = _check(
        repo,
        limits=Limits(max_skills=1, max_entries=3, max_version_bytes=9),
    )

    assert [v.message for v in violations if v.check == Check.LIMITS] == [
        "Skill count 2 exceeds 1",
        "Entry count 4 exceeds 3",
        "Total size in bytes 10 exceeds 9",
    ]


@pytest.mark.parametrize(
    "team", ["", "-bad", "bad-", "bad--team", "Bad", "bad_team", "é", "acme\n"]
)
def test_bad_team_slug(repository, team):
    repo = repository({})
    entries, _ = read_tree(repo, _head(repo))

    with pytest.raises(ValueError, match="Team slug must match"):
        check_structure(team, entries, other_names={}, limits=Limits())


def test_numeric_team_slug(repository):
    repo = repository({"123-skill/SKILL.md": "x"})

    violations = _check(repo, team="123")

    assert violations == []


@pytest.mark.parametrize("path", ["SKILL.md", "acme-a/resources/SKILL.md"])
def test_skill_md_size_only_applies_to_direct_skill_files(repository, path):
    repo = repository({"acme-a/SKILL.md": "x", path: "xx"})

    violations = _check(repo, limits=Limits(max_skill_md_bytes=1))

    assert any(v.check == Check.LAYOUT for v in violations)
    assert not any(v.check == Check.LIMITS for v in violations)


def test_skill_count_excludes_hidden_unsafe_and_root_paths(repository):
    repo = repository(
        {
            ".hidden/SKILL.md": "x",
            "root-file": "x",
            "acme-a/SKILL.md": "x",
        }
    )
    entries, _ = read_tree(repo, _head(repo))
    entries.append(dataclasses.replace(entries[0], path="../SKILL.md"))

    violations = check_structure(
        "acme", entries, other_names={}, limits=Limits(max_skills=1)
    )

    assert any(v.check == Check.LAYOUT for v in violations)
    assert not any(v.check == Check.LIMITS for v in violations)
    assert not any(v.check == Check.NAME for v in violations)


@pytest.mark.parametrize(
    "field, maximum, label",
    [
        ("max_depth", 3, "Path depth"),
        ("max_path_chars", 19, "Path length in characters"),
        ("max_entries", 1, "Entry count"),
    ],
)
def test_empty_directory_limits(repository, field, maximum, label):
    repo = repository({"acme-a/SKILL.md": "x"})
    blob = _git(repo, "rev-parse", "HEAD:acme-a/SKILL.md").strip()
    nested = _tree(repo, [])
    for name in [b"bb", b"a"]:
        nested = _tree(repo, [b"040000 tree " + nested + b"\t" + name])
    skill = _tree(
        repo,
        [
            b"100644 blob " + blob + b"\tSKILL.md",
            b"040000 tree " + nested + b"\tresources",
        ],
    )
    tree = _tree(repo, [b"040000 tree " + skill + b"\tacme-a"])
    entries, errors = read_tree(repo, _commit_tree(repo, tree))
    assert errors == []
    path = "acme-a/resources/a/bb"
    actual = {"max_depth": 4, "max_path_chars": len(path), "max_entries": 2}
    for bound in [actual[field], maximum]:
        limits = dataclasses.replace(Limits(), **{field: bound})
        violations = check_structure(
            "acme", entries, other_names={}, limits=limits
        )
        if bound == actual[field]:
            assert violations == []
        else:
            assert len(violations) == 1
            assert violations[0].check == Check.LIMITS
            assert violations[0].path == (
                "" if field == "max_entries" else path
            )
            assert violations[0].message == (
                f"{label} {actual[field]} exceeds {maximum}"
            )


@pytest.mark.parametrize("name", [b"BadName", b"acme-a"])
def test_invalid_only_child_preserves_skill(repository, name):
    repo = repository({"acme-a/SKILL.md": "x"})
    blob = _git(repo, "rev-parse", "HEAD:acme-a/SKILL.md").strip()
    child = _tree(repo, [b"100644 blob " + blob + b"\t\xff"])
    tree = _tree(repo, [b"040000 tree " + child + b"\t" + name])
    entries, errors = read_tree(repo, _commit_tree(repo, tree))
    assert len(errors) == 1
    assert [entry.path for entry in entries] == [name.decode()]
    violations = check_structure(
        "acme", entries, other_names={}, limits=Limits(max_skills=0)
    )
    assert (
        Violation(
            Check.LAYOUT,
            name.decode(),
            "Skill needs exactly one SKILL.md, found 0",
        )
        in violations
    )
    assert Violation(Check.LIMITS, "", "Skill count 1 exceeds 0") in violations
    assert [v.check for v in violations].count(Check.NAME) == (
        1 if name == b"BadName" else 0
    )


def test_empty_resources_is_allowed(repository):
    repo = repository({"acme-a/SKILL.md": "x"})
    blob = _git(repo, "rev-parse", "HEAD:acme-a/SKILL.md").strip()
    empty = _tree(repo, [])
    skill = _tree(
        repo,
        [
            b"100644 blob " + blob + b"\tSKILL.md",
            b"040000 tree " + empty + b"\tresources",
        ],
    )
    root = _tree(repo, [b"040000 tree " + skill + b"\tacme-a"])
    entries, errors = read_tree(repo, _commit_tree(repo, root))
    assert errors == []
    assert {e.path for e in entries} == {
        "acme-a/SKILL.md",
        "acme-a/resources",
    }
    assert (
        check_structure("acme", entries, other_names={}, limits=Limits()) == []
    )


@pytest.mark.parametrize(
    "empty_name, skill_name",
    [("ACME-A", "acme-a"), ("acme-e\u0301", "acme-é")],
)
@pytest.mark.parametrize("reverse", [False, True])
def test_empty_top_level_directory_collides_with_skill(
    repository, empty_name, skill_name, reverse
):
    repo = repository({"acme-a/SKILL.md": "x"})
    skill = _git(repo, "rev-parse", "HEAD:acme-a").strip()
    empty = _tree(repo, [])
    tree = _tree(
        repo,
        [
            b"040000 tree " + empty + b"\t" + empty_name.encode(),
            b"040000 tree " + skill + b"\t" + skill_name.encode(),
        ],
    )
    entries, violations = read_tree(repo, _commit_tree(repo, tree))
    assert violations == []
    entries.sort(key=lambda entry: entry.path, reverse=reverse)

    violations = check_structure(
        "acme", entries, other_names={}, limits=Limits()
    )

    collisions = [v for v in violations if v.check == Check.COLLISION]
    assert len(collisions) == 1
    assert collisions[0].path in {empty_name, skill_name}
    assert "canonical caseless matching" in collisions[0].message


@pytest.mark.parametrize(
    "first, second",
    [
        ("a.txt", "A.txt"),
        ("é.txt", "e\u0301.txt"),
        ("Straße", "STRASSE"),
        ("α\u0345\u0301", "άι"),
    ],
)
def test_path_collisions(repository, first, second):
    paths = [f"acme-a/resources/{name}" for name in (first, second)]
    repo = repository({"acme-a/SKILL.md": "x", **dict.fromkeys(paths, "x")})

    violations = _check(repo)

    assert len(violations) == 1
    assert violations[0].check == Check.COLLISION
    assert violations[0].path in paths
    assert "canonical caseless matching" in violations[0].message
    assert (
        repr(next(path for path in paths if path != violations[0].path))
        in violations[0].message
    )


def test_collisions_cover_complete_paths(repository):
    repo = repository({"acme-a/SKILL.md": "x", "ACME-A/SKILL.md": "x"})

    violations = _check(repo)

    assert any(
        violation.check == Check.COLLISION
        and "Path collides" in violation.message
        for violation in violations
    )


@pytest.mark.parametrize(
    "name, other_name",
    [("acme-a", "ACME-A"), ("acme-é", "ACME-E\u0301")],
)
def test_collisions_with_other_teams_and_baseline(
    repository, name, other_name
):
    repo = repository({f"{name}/SKILL.md": "x"})

    violations = _check(
        repo, other_names={other_name: "acme", "unrelated-skill": "docs"}
    )

    collisions = [v for v in violations if v.check == Check.COLLISION]
    assert len(collisions) == 1
    assert collisions[0].path == name
    assert collisions[0].message == (
        f"Skill name {collisions[0].path!r} is already published by team acme"
    )


def test_returns_all_violations_sorted_by_check_and_path(repository):
    repo = repository(
        {"acme-z/resources/z": "x", "acme-a/resources/a": "x"},
        symlinks={"Wrong/Scripts/skill.md": "missing"},
    )

    violations = _check(
        repo,
        other_names={"wrong": "acme"},
        limits=Limits(max_skills=0, max_entries=0),
    )

    assert [(v.check, v.path) for v in violations] == [
        (Check.LAYOUT, "Wrong"),
        (Check.LAYOUT, "Wrong/Scripts/skill.md"),
        (Check.LAYOUT, "Wrong/Scripts/skill.md"),
        (Check.LAYOUT, "acme-a"),
        (Check.LAYOUT, "acme-z"),
        (Check.NAME, "Wrong"),
        (Check.COLLISION, "Wrong"),
        (Check.MODE, "Wrong/Scripts/skill.md"),
        (Check.SCRIPTS, "Wrong/Scripts/skill.md"),
        (Check.LIMITS, ""),
        (Check.LIMITS, ""),
    ]


@pytest.mark.parametrize("first, second", [("a", "A"), ("é", "e\u0301")])
@pytest.mark.parametrize("file_first", [True, False])
def test_file_directory_aliases(repository, first, second, file_first):
    file_path = f"acme-a/resources/{first}"
    nested_path = f"acme-a/resources/{second}/x"
    paths = (
        [file_path, nested_path] if file_first else [nested_path, file_path]
    )
    repo = repository({"acme-a/SKILL.md": "x"})
    blob = _git(repo, "rev-parse", "HEAD:acme-a/SKILL.md").strip()
    nested = _tree(repo, [b"100644 blob " + blob + b"\tx"])
    resources = _tree(
        repo,
        [
            b"100644 blob " + blob + b"\t" + first.encode(),
            b"040000 tree " + nested + b"\t" + second.encode(),
        ],
    )
    skill = _tree(
        repo,
        [
            b"100644 blob " + blob + b"\tSKILL.md",
            b"040000 tree " + resources + b"\tresources",
        ],
    )
    tree = _tree(repo, [b"040000 tree " + skill + b"\tacme-a"])
    entries, _ = read_tree(repo, _commit_tree(repo, tree))
    entries.sort(
        key=lambda entry: (
            paths.index(entry.path) if entry.path in paths else -1
        )
    )

    violations = check_structure(
        "acme", entries, other_names={}, limits=Limits()
    )

    assert len(violations) == 1
    assert violations[0].check == Check.COLLISION
    assert violations[0].path == file_path
    assert f"directory 'acme-a/resources/{second}'" in violations[0].message


def test_other_names_use_canonical_caseless_matching(repository):
    repo = repository({"acme-α\u0308ι/SKILL.md": "x"})

    violations = _check(repo, other_names={"ACME-Α\u0345\u0308": "acme"})

    collisions = [v for v in violations if v.check == Check.COLLISION]
    assert len(collisions) == 1
    assert collisions[0].path == "acme-α\u0308ι"
    assert collisions[0].message == (
        f"Skill name {collisions[0].path!r} is already published by team acme"
    )


@pytest.mark.parametrize(
    "path, check",
    [
        ("extra", Check.LAYOUT),
        ("skill.md", Check.LAYOUT),
        ("resources/.hidden", Check.LAYOUT),
        ("resources/scripts", Check.SCRIPTS),
        ("resources/SKILL.md", Check.LAYOUT),
        ("resources/FOO", Check.COLLISION),
        ("resources/allowed", None),
    ],
)
def test_empty_nested_trees_are_validated(repository, path, check):
    repo = repository({"acme-a/SKILL.md": "x"})
    blob = _git(repo, "rev-parse", "HEAD:acme-a/SKILL.md").strip()
    empty = _tree(repo, [])
    components = path.split("/")
    record = b"040000 tree " + empty + b"\t" + components[-1].encode()
    if len(components) == 2:
        resources = _tree(repo, [record, b"100644 blob " + blob + b"\tfoo"])
        record = b"040000 tree " + resources + b"\tresources"
    skill = _tree(repo, [record, b"100644 blob " + blob + b"\tSKILL.md"])
    tree = _tree(repo, [b"040000 tree " + skill + b"\tacme-a"])
    entries, violations = read_tree(repo, _commit_tree(repo, tree))
    assert violations == []
    assert any(
        entry.path == f"acme-a/{path}" and entry.mode == "040000"
        for entry in entries
    )
    violations = check_structure(
        "acme", entries, other_names={}, limits=Limits()
    )
    if check is None:
        assert violations == []
    else:
        messages = {
            "extra": (
                "Place files at <skill>/SKILL.md or <skill>/resources/<path>"
            ),
            "skill.md": (
                "Place files at <skill>/SKILL.md or <skill>/resources/<path>"
            ),
            "resources/.hidden": "Remove path components starting with '.'",
            "resources/scripts": "Remove scripts components",
            "resources/SKILL.md": (
                "Use SKILL.md only at <skill>/SKILL.md with that exact casing"
            ),
            "resources/FOO": (
                "File collides with directory 'acme-a/resources/FOO' "
                "after canonical caseless matching"
            ),
        }
        expected = [Violation(check, "acme-a/" + path, messages[path])]
        if path == "resources/FOO":
            expected[0] = dataclasses.replace(
                expected[0], path="acme-a/resources/foo"
            )
        if path == "skill.md":
            expected.append(
                Violation(
                    Check.LAYOUT,
                    "acme-a/skill.md",
                    (
                        "Use SKILL.md only at <skill>/SKILL.md "
                        "with that exact casing"
                    ),
                )
            )
        if path == "skill.md":
            expected.append(
                Violation(
                    Check.COLLISION,
                    "acme-a/SKILL.md",
                    "File collides with directory 'acme-a/skill.md' "
                    "after canonical caseless matching",
                )
            )
        assert violations == sorted(expected, key=lambda v: (v.check, v.path))


@pytest.mark.parametrize("first, second", [("foo", "FOO"), ("é", "e\u0301")])
@pytest.mark.parametrize("empty", [True, False])
@pytest.mark.parametrize("reverse", [True, False])
def test_directory_directory_aliases(
    repository, first, second, empty, reverse
):
    repo = repository({"acme-a/SKILL.md": "x"})
    blob = _git(repo, "rev-parse", "HEAD:acme-a/SKILL.md").strip()
    populated = _tree(repo, [b"100644 blob " + blob + b"\tx"])
    sibling = _tree(
        repo,
        []
        if empty
        else [
            b"100644 blob " + blob + b"\ty",
            b"100644 blob " + blob + b"\tz",
        ],
    )
    resources = _tree(
        repo,
        [
            b"040000 tree " + populated + b"\t" + first.encode(),
            b"040000 tree " + sibling + b"\t" + second.encode(),
        ],
    )
    skill = _tree(
        repo,
        [
            b"100644 blob " + blob + b"\tSKILL.md",
            b"040000 tree " + resources + b"\tresources",
        ],
    )
    tree = _tree(repo, [b"040000 tree " + skill + b"\tacme-a"])
    entries, errors = read_tree(repo, _commit_tree(repo, tree))
    assert errors == []
    entries.sort(key=lambda entry: entry.path, reverse=reverse)
    violations = check_structure(
        "acme", entries, other_names={}, limits=Limits()
    )
    assert len(violations) == 1
    assert violations[0].check == Check.COLLISION
    paths = {f"acme-a/resources/{first}", f"acme-a/resources/{second}"}
    assert violations[0].path in paths
    other = (paths - {violations[0].path}).pop()
    assert repr(other) in violations[0].message


@pytest.mark.parametrize("top_level", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_directory_alias_does_not_repeat_for_descendants(top_level, reverse):
    first = "acme-a" if top_level else "acme-a/resources/foo"
    second = "ACME-A" if top_level else "acme-a/resources/FOO"
    entries = [
        Entry("100644", "a" * 40, 1, f"{prefix}/sub/{filename}")
        for prefix, filename in [
            (first, "1.txt"),
            (second, "2.txt"),
            (second, "3.txt"),
        ]
    ]
    if reverse:
        entries.reverse()
    violations = check_structure(
        "acme", entries, other_names={}, limits=Limits()
    )
    collisions = [v for v in violations if v.check == Check.COLLISION]
    reported, original = (first, second) if reverse else (second, first)
    label = "Skill name" if top_level else "Directory"
    assert collisions == [
        Violation(
            Check.COLLISION,
            reported,
            f"{label} collides with {original!r} "
            "after canonical caseless matching",
        )
    ]


@pytest.mark.parametrize(
    "first, second",
    [
        ("foo", "FOO"),
        ("é", "e\u0301"),
        ("Straße", "STRASSE"),
        ("α\u0345\u0301", "άι"),
    ],
)
@pytest.mark.parametrize(
    "occupancy", [(False, False), (False, True), (True, False), (True, True)]
)
@pytest.mark.parametrize("top", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_directory_occupancy_matrix(first, second, occupancy, top, reverse):
    base = "" if top else "acme-a/resources/"
    prefixes = [base + name for name in (first, second)]
    entries = []
    for prefix, populated in zip(prefixes, occupancy, strict=True):
        if populated:
            entries.extend(
                Entry("100644", "a" * 40, 1, prefix + "/" + name)
                for name in ["x", "y"]
            )
        else:
            entries.append(Entry("040000", "a" * 40, 0, prefix))
    if reverse:
        entries.reverse()
    violations = check_structure(
        "acme", entries, other_names={}, limits=Limits()
    )
    collisions = [v for v in violations if v.check == Check.COLLISION]
    # Populated aliases also share file names: these are full-path collisions.
    path_collisions = 2 if all(occupancy) else 0
    assert len(collisions) == 1 + path_collisions
    directory_collisions = [
        v for v in collisions if not v.message.startswith("Path")
    ]
    assert len(directory_collisions) == 1
    reported, original = prefixes if reverse else prefixes[::-1]
    label = "Skill name" if top else "Directory"
    assert directory_collisions == [
        Violation(
            Check.COLLISION,
            reported,
            f"{label} collides with {original!r} "
            "after canonical caseless matching",
        )
    ]


@pytest.mark.parametrize("ancestor", ["ACME-A", "acme-a/resources/DIR"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    "alias, canonical", [("SUB", "sub"), ("e\u0301", "é")]
)
def test_independent_collisions_beneath_ancestor_aliases(
    ancestor, reverse, alias, canonical
):
    original = "acme-a" if ancestor == "ACME-A" else "acme-a/resources/dir"
    entries = [
        Entry("100644", "a" * 40, 1, original + "/first/x"),
        Entry("100644", "a" * 40, 1, ancestor + f"/{canonical}/x"),
        Entry("100644", "a" * 40, 1, ancestor + f"/{alias}/y"),
        Entry("100644", "a" * 40, 1, ancestor + "/file"),
        Entry("100644", "a" * 40, 1, ancestor + "/FILE/z"),
    ]
    if reverse:
        entries.reverse()
    collisions = [
        v
        for v in check_structure(
            "acme", entries, other_names={}, limits=Limits()
        )
        if v.check == Check.COLLISION
    ]
    assert len(collisions) == 3
    assert sum(v.message.startswith("File collides") for v in collisions) == 1
    assert (
        next(
            v for v in collisions if v.message.startswith("File collides")
        ).path
        == ancestor + "/file"
    )
    child = ancestor + "/" + (canonical if reverse else alias)
    assert any(
        v.path == child and v.message.startswith("Directory collides")
        for v in collisions
    )


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("first, second", [("foo", "FOO"), ("é", "e\u0301")])
def test_file_directory_collision_exactly_once(empty, reverse, first, second):
    file = "acme-a/resources/" + first
    directory = "acme-a/resources/" + second
    entries = [
        Entry("100644", "a" * 40, 1, file),
        Entry(
            "040000" if empty else "100644",
            "a" * 40,
            0,
            directory if empty else directory + "/x",
        ),
    ]
    if reverse:
        entries.reverse()
    collisions = [
        v
        for v in check_structure(
            "acme", entries, other_names={}, limits=Limits()
        )
        if v.check == Check.COLLISION
    ]
    assert collisions == [
        Violation(
            Check.COLLISION,
            file,
            f"File collides with directory {directory!r} "
            "after canonical caseless matching",
        )
    ]


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


@pytest.mark.parametrize(
    "path",
    [
        "Resources/empty",
        "extra/empty",
        "SKILL.md/empty",
        "resources/SKILL.md/empty",
    ],
)
def test_invalid_directories_with_empty_descendants(repository, path):
    repo = repository({"acme-a/SKILL.md": "x"})
    blob = _git(repo, "rev-parse", "HEAD:acme-a/SKILL.md").strip()
    tree = _tree(repo, [])
    parts = path.split("/")
    for part in reversed(parts[1:]):
        tree = _tree(repo, [b"040000 tree " + tree + b"\t" + part.encode()])
    records = [b"040000 tree " + tree + b"\t" + parts[0].encode()]
    if parts[0] != "SKILL.md":
        records.append(b"100644 blob " + blob + b"\tSKILL.md")
    skill = _tree(repo, records)
    root = _tree(repo, [b"040000 tree " + skill + b"\tacme-a"])
    entries, errors = read_tree(repo, _commit_tree(repo, root))
    assert errors == []
    assert sum(e.path == "acme-a/" + path for e in entries) == 1
    violations = check_structure(
        "acme", entries, other_names={}, limits=Limits()
    )
    expected = []
    if parts[0] != "resources":
        expected.append(
            Violation(
                Check.LAYOUT,
                "acme-a/" + path,
                "Place files at <skill>/SKILL.md or <skill>/resources/<path>",
            )
        )
    if "SKILL.md" in parts:
        expected.append(
            Violation(
                Check.LAYOUT,
                "acme-a/" + path,
                "Use SKILL.md only at <skill>/SKILL.md with that exact casing",
            )
        )
    if parts[0] == "Resources":
        expected.append(
            Violation(
                Check.LAYOUT,
                "acme-a/" + path,
                "Spell every resources component as 'resources'",
            )
        )
    if parts[0] == "SKILL.md":
        expected.append(
            Violation(
                Check.LAYOUT,
                "acme-a",
                "Skill needs exactly one SKILL.md, found 0",
            )
        )
    assert violations == sorted(expected, key=lambda v: (v.check, v.path))


@pytest.mark.parametrize("team", ["acme", "team-a", "123"])
def test_standard_skill_name(repository, team):
    repo = repository({"triage/SKILL.md": "x"})

    assert _check(repo, team=team) == []


@pytest.mark.parametrize("published", ["triage", "TRIAGE"])
def test_published_skill_names_identify_owner(repository, published):
    repo = repository({"triage/SKILL.md": "x"})

    assert _check(repo, team="docs", other_names={published: "acme"}) == [
        Violation(
            Check.COLLISION,
            "triage",
            "Skill name 'triage' is already published by team acme",
        )
    ]
