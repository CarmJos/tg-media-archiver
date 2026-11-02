"""Publish complete media with the destination share's inherited ACL."""

import os
import shutil
import stat
import uuid
from pathlib import Path


def publish_download(source, destination, share_root):
    root = Path(os.path.abspath(share_root))
    target = Path(os.path.abspath(destination))
    source = Path(source)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Download share must be an existing real directory")
    relative = target.relative_to(root)
    if not relative.parts or target == root:
        raise ValueError("Invalid download destination")
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        try:
            current.mkdir(mode=0o775)
        except FileExistsError:
            pass
        if current.is_symlink() or not current.is_dir():
            raise ValueError("Download parent must be a real directory")
    if os.path.lexists(target):
        raise FileExistsError(str(target))

    staging = target.parent / (".tdl-" + uuid.uuid4().hex + ".part")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(source, os.O_RDONLY | nofollow)
    try:
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Download source must be a regular file")
        output_fd = os.open(
            staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o664
        )
        try:
            with (
                os.fdopen(os.dup(source_fd), "rb") as incoming,
                os.fdopen(output_fd, "wb") as output,
            ):
                shutil.copyfileobj(incoming, output, 1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
                after = os.fstat(source_fd)
                if (before.st_size, before.st_mtime_ns) != (
                    after.st_size,
                    after.st_mtime_ns,
                ):
                    raise OSError("Download source changed while publishing")
                if os.fstat(output.fileno()).st_size != before.st_size:
                    raise OSError("Published download has the wrong size")
            # The destination inode inherits the share ACL; link prevents overwrite.
            os.link(staging, target, follow_symlinks=False)
            if os.name == "posix":
                directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            os.close(source_fd)
            source_fd = None
            source.unlink()
        finally:
            if staging.exists():
                staging.unlink()
    finally:
        if source_fd is not None:
            os.close(source_fd)
