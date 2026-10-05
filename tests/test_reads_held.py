"""What vivibox reads of a clone is held to a budget and to the clone itself, at the places a
review found a change of the code would pass every test: the read of a file that grew after
its size was taken, the copies an approval keeps for later diffs, and what the risky files' view
reads and copies of a file swapped for a link out of the clone while it was looked at."""

import os

import pytest
from test_clone_read import (
    MiB,
    approved_gradle,
    clone,  # noqa: F401 (the fixture)
    outside,  # noqa: F401 (the fixture)
    past_the_limit_to_show,
    read_per_file,
    task,  # noqa: F401 (the fixture)
)

from vivibox import clone_read, risky
from vivibox.risky import Approvals, Change


def test_a_file_that_grew_after_its_size_was_taken_is_read_one_byte_past_the_budget(clone, monkeypatch):  # noqa: F811
    """Its size, as fstat said it, fitted what the pass had left; what is read is counted all
    the same, and asked for no further than one byte past what was left: enough to know that
    it grew, and not a chunk of a megabyte past the budget."""
    (clone / "Makefile").write_bytes(b"x" * (3 * MiB))
    real_fstat = os.fstat

    def small(fd):
        fields = list(real_fstat(fd))
        fields[6] = 10  # st_size, as if it grew after the look
        return os.stat_result(fields)

    monkeypatch.setattr(os, "fstat", small)
    budget = clone_read.Budget(10 << 10)
    with pytest.raises(clone_read.Refused) as refused:
        clone_read.read_bytes(clone, clone / "Makefile", clone_read.CHECK_LIMIT, budget)
    assert refused.value.kind == clone_read.OVER_BUDGET and budget.spent
    assert budget.left == -1, "one byte past what was left, and no more"
    # With room for less than a chunk after a first one: the second read is the short one.
    budget = clone_read.Budget(MiB + 10)
    with pytest.raises(clone_read.Refused):
        clone_read.read_bytes(clone, clone / "Makefile", clone_read.CHECK_LIMIT, budget)
    assert budget.left == -1


def test_one_approval_reads_no_more_than_the_budget_with_the_copies_it_keeps(task, monkeypatch):  # noqa: F811
    """p hashes a risky file, reads it to show it, and keeps a copy to diff the next change
    against: all three from one budget. Here the first two leave half of what the copy needs:
    the approval is taken, no copy is kept, and nothing past the budget was read for one."""
    monkeypatch.setattr(clone_read, "SCAN_BUDGET", 5 * MiB // 2)
    gradle = task.repo / "build.gradle"
    gradle.write_bytes(b"x" * MiB)
    approvals = Approvals(task.meta, task.repo)
    shown = approvals.view().identity
    read = read_per_file(monkeypatch, task.repo)
    assert approvals.approve_changes(shown) == [Change("build.gradle", "added")]
    assert read == {"build.gradle": 2 * MiB}, "hashed and shown; the copy did not fit and was not begun"
    assert not (approvals.dir / "files" / "build.gradle").exists()
    assert approvals.changes() == [], "approved all the same"
    # With room for all three, the copy is kept, and the three together are the budget's.
    monkeypatch.setattr(clone_read, "SCAN_BUDGET", 3 * MiB)
    gradle.write_bytes(b"y" * MiB)
    shown = approvals.view().identity
    read.clear()
    approvals.approve_changes(shown)
    assert read == {"build.gradle": 3 * MiB}, "hashed, shown and copied"
    assert (approvals.dir / "files" / "build.gradle").read_bytes() == b"y" * MiB


@pytest.mark.parametrize("link", ["by its whole path", "by the way up out of the clone"])
@pytest.mark.parametrize("swapped_after", ["the hash", "the read that refused it for its size"])
def test_a_file_swapped_for_a_link_out_while_it_is_shown_is_neither_read_nor_copied(
    task,  # noqa: F811
    outside,  # noqa: F811
    monkeypatch,
    swapped_after,
    link,
):
    """The view hashes a changed risky file and reads it to show it; one past what it shows is
    refused and read no further. Between the two the agent may put a link to a file of yours
    in its place: the read walks from the clone's root as the hash did, so the file outside
    is never opened, and nothing of it is copied anywhere. A link by a whole path is refused
    whatever the walk starts from; one that climbs out
    (../../host/secret.txt) only by a walk that starts at the clone: read from the root of the
    machine instead, it is followed to your file."""
    gradle = past_the_limit_to_show(approved_gradle(task), monkeypatch, b"plugins { id 'evil' }\n")
    secret = outside / "secret.txt"
    approvals = Approvals(task.meta, task.repo)
    real_digest, real_read, real_open, opened = risky._digest, clone_read.read_bytes, os.open, []

    def swap() -> None:
        gradle.unlink()
        gradle.symlink_to(secret if link == "by its whole path" else os.path.relpath(secret, gradle.parent))
        assert gradle.resolve() == secret.resolve() and gradle.read_text() == secret.read_text()

    def hash_then_swap(*args, **kwargs):
        found = real_digest(*args, **kwargs)
        swap()
        return found

    def read_then_swap(*args, **kwargs):
        try:
            return real_read(*args, **kwargs)
        except clone_read.Refused:  # for its size: what is copied next is what stands there by then
            swap()
            raise

    def watched(path, flags, mode=0o777, *, dir_fd=None):
        if os.path.basename(os.fspath(path)) == secret.name:
            opened.append(path)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    if swapped_after == "the hash":
        monkeypatch.setattr(risky, "_digest", hash_then_swap)
    else:
        monkeypatch.setattr(clone_read, "read_bytes", read_then_swap)
    monkeypatch.setattr(os, "open", watched)
    said = approvals.diff(Change("build.gradle", "changed"))
    assert gradle.is_symlink(), "the swap happened"
    assert opened == [], "the file outside the clone is not opened"
    assert "the task's change to it cannot be approved" in said
    assert not (approvals.dir / "view").exists(), "and nothing is copied anywhere for you to read"
    if swapped_after == "the hash":
        assert "build.gradle is a link that leads out of the repository" in said
    else:  # refused for its size: nothing reads what stands there afterwards
        assert "shows no file that large" in said
