from __future__ import annotations

import os
from pathlib import Path
import stat
from contextlib import contextmanager
from typing import TypeAlias
from uuid import uuid4


PathLike: TypeAlias = str | os.PathLike[str]


_REPARSE_FLAG = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _windows_os_error(error: int, filename: str | None = None) -> OSError:
    """Preserve a Win32 error code instead of treating it as a Python errno."""

    import ctypes

    return OSError(None, ctypes.FormatError(error).strip(), filename, error)


def _file_identity(info) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_mode,
        getattr(info, "st_file_attributes", 0),
    )


def _file_object_identity(info) -> tuple[int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        getattr(info, "st_file_attributes", 0),
    )


def _is_regular_non_reparse(info) -> bool:
    return (
        stat.S_ISREG(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and not getattr(info, "st_file_attributes", 0) & _REPARSE_FLAG
    )


def _is_regular_directory(info) -> bool:
    return (
        stat.S_ISDIR(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and not getattr(info, "st_file_attributes", 0) & _REPARSE_FLAG
    )


@contextmanager
def _posix_parent_fd(target: Path):
    """Open the parent chain without following symlinks and pin the leaf parent."""

    needed = (os.open, os.stat, os.unlink, os.rename)
    if any(operation not in os.supports_dir_fd for operation in needed):
        raise OSError("directory-relative safe path operations are unavailable")
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    nofollow_flag = getattr(os, "O_NOFOLLOW", 0)
    if not directory_flag or not nofollow_flag:
        raise OSError("no-follow directory opens are unavailable")

    flags = os.O_RDONLY | directory_flag | nofollow_flag | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(target.anchor or os.sep, flags)
    try:
        if not _is_regular_directory(os.fstat(descriptor)):
            raise OSError("file parent is not a regular directory")
        for component in target.parent.parts[1:]:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            try:
                if not _is_regular_directory(os.fstat(next_descriptor)):
                    raise OSError("file parent is not a regular directory")
            except BaseException:
                os.close(next_descriptor)
                raise
            os.close(descriptor)
            descriptor = next_descriptor
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _windows_parent_handle(target: Path):
    """Pin every Windows parent directory, rejecting reparse components.

    Each parent is opened with OPEN_REPARSE_POINT and FILE_READ_ATTRIBUTES only,
    without FILE_SHARE_DELETE. Holding the chain prevents its names from being
    renamed or replaced while the path-based operation runs. The operation
    itself performs the required access check when it opens or creates its leaf.
    """

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    class _FileTime(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

    class _ByHandleInfo(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD),
            ("creation", _FileTime),
            ("access", _FileTime),
            ("write", _FileTime),
            ("volume", wintypes.DWORD),
            ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD),
            ("links", wintypes.DWORD),
            ("index_high", wintypes.DWORD),
            ("index_low", wintypes.DWORD),
        ]

    kernel32.GetFileInformationByHandle.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ByHandleInfo))
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL

    file_read_attributes = 0x0080
    share_read = 0x00000001
    share_write = 0x00000002
    open_existing = 3
    backup_semantics = 0x02000000
    open_reparse_point = 0x00200000
    directory_attribute = 0x00000010
    reparse_attribute = 0x00000400
    handles = []
    deepest = None
    try:
        for parent in reversed(target.parents):
            path_info = parent.lstat()
            if not _is_regular_directory(path_info):
                raise OSError("file parent is not a regular directory")
            desired_access = file_read_attributes
            handle = kernel32.CreateFileW(
                str(parent),
                desired_access,
                share_read | share_write,  # Deliberately omit FILE_SHARE_DELETE.
                None,
                open_existing,
                backup_semantics | open_reparse_point,
                None,
            )
            invalid_handle = ctypes.c_void_p(-1).value
            if handle == invalid_handle:
                error = ctypes.get_last_error()
                raise _windows_os_error(error, str(parent)) from None
            info = _ByHandleInfo()
            if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
                error = ctypes.get_last_error()
                kernel32.CloseHandle(handle)
                raise _windows_os_error(error, str(parent)) from None
            file_id = (int(info.index_high) << 32) | int(info.index_low)
            if (
                info.attributes & (directory_attribute | reparse_attribute) != directory_attribute
                or int(info.volume) != (int(path_info.st_dev) & 0xFFFFFFFF)
                or file_id != int(path_info.st_ino)
            ):
                kernel32.CloseHandle(handle)
                raise OSError("file parent changed or resolved through a reparse point")
            handles.append(handle)
            deepest = handle
        if deepest is None:
            raise OSError("file parent could not be anchored")
        yield deepest
    finally:
        for handle in reversed(handles):
            kernel32.CloseHandle(handle)


def _path_operation_checkpoint(_operation: str, _target: Path) -> None:
    """Private deterministic race-test seam; production behavior is a no-op."""


def safe_ensure_directory_tree(path: PathLike, *, mode: int = 0o700) -> Path:
    """Create a directory chain without following links or reparse points."""

    target = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    if os.name == "nt":
        root = Path(target.anchor)
        root_info = root.lstat()
        if not _is_regular_directory(root_info):
            raise OSError("directory root is not a regular non-reparse directory")
        directories = [*reversed(target.parents), target]
        for directory in directories:
            if directory == root:
                continue
            try:
                existing = directory.lstat()
            except FileNotFoundError:
                existing = None
            if existing is not None:
                if not _is_regular_directory(existing):
                    raise OSError("directory path is not a regular non-reparse directory")
                with _windows_parent_handle(directory):
                    current = directory.lstat()
                    if not _is_regular_directory(current) or _file_identity(current) != _file_identity(existing):
                        raise OSError("directory path changed during validation")
                continue
            with _windows_parent_handle(directory):
                try:
                    directory.mkdir(mode=mode)
                except FileExistsError:
                    pass
                info = directory.lstat()
                if not _is_regular_directory(info):
                    raise OSError("directory path is not a regular non-reparse directory")
        return target

    if not all(operation in os.supports_dir_fd for operation in (os.open, os.mkdir)):
        raise OSError("directory-relative safe creation is unavailable")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    if not getattr(os, "O_DIRECTORY", 0) or not getattr(os, "O_NOFOLLOW", 0):
        raise OSError("no-follow directory opens are unavailable")
    descriptor = os.open(target.anchor or os.sep, directory_flags)
    try:
        for component in target.parts[1:]:
            try:
                os.mkdir(component, mode=mode, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(component, directory_flags, dir_fd=descriptor)
            try:
                if not _is_regular_directory(os.fstat(child)):
                    raise OSError("directory path is not a regular non-reparse directory")
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
    finally:
        os.close(descriptor)
    return target


def _windows_open_file(
    path: Path,
    *,
    access: int,
    share: int,
    creation: int,
    descriptor_flags: int = os.O_RDONLY,
):
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateFileW(
        str(path),
        access,
        share,
        None,
        creation,
        0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        error = ctypes.get_last_error()
        raise _windows_os_error(error, str(path)) from None
    try:
        descriptor = msvcrt.open_osfhandle(int(handle), os.O_BINARY | descriptor_flags)
    except BaseException:
        kernel32.CloseHandle(handle)
        raise
    return descriptor


def safe_open_regular_file(path: PathLike, *, flags: int, mode: int = 0o600) -> int:
    """Open a regular control file relative to a verified, no-follow parent chain."""

    target = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    if os.name == "nt":
        import msvcrt

        creating = bool(flags & os.O_CREAT)
        exclusive = bool(flags & os.O_EXCL)
        access_mode = flags & (os.O_WRONLY | os.O_RDWR)
        desired_access = 0x0080  # FILE_READ_ATTRIBUTES
        if access_mode == os.O_WRONLY:
            desired_access |= 0x40000000  # GENERIC_WRITE
        elif access_mode == os.O_RDWR:
            desired_access |= 0x80000000 | 0x40000000  # GENERIC_READ | GENERIC_WRITE
        else:
            desired_access |= 0x80000000  # GENERIC_READ
        creation = 1 if exclusive else (4 if creating else 3)  # CREATE_NEW / OPEN_ALWAYS / OPEN_EXISTING
        descriptor_flags = access_mode | (flags & getattr(os, "O_APPEND", 0)) | os.O_BINARY
        with _windows_parent_handle(target):
            try:
                before = target.lstat()
            except FileNotFoundError:
                before = None
            if before is not None and not _is_regular_non_reparse(before):
                raise OSError("control file is not a regular non-reparse file")
            _path_operation_checkpoint("open", target)
            descriptor = _windows_open_file(
                target,
                access=desired_access,
                share=0x00000001 | 0x00000002,  # FILE_SHARE_READ | FILE_SHARE_WRITE; deny delete.
                creation=creation,
                descriptor_flags=descriptor_flags,
            )
            try:
                opened = os.fstat(descriptor)
                current = target.lstat()
                if (
                    not _is_regular_non_reparse(opened)
                    or _file_identity(opened) != _file_identity(current)
                    or (before is not None and _file_identity(before) != _file_identity(opened))
                ):
                    raise OSError("control file changed or resolved through a reparse point while opening")
                return descriptor
            except BaseException:
                os.close(descriptor)
                raise

    if flags & (os.O_TRUNC | os.O_APPEND):
        raise ValueError("safe control-file opens do not permit implicit truncation or append")
    with _posix_parent_fd(target) as parent_fd:
        try:
            before = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            before = None
        if before is not None and not _is_regular_non_reparse(before):
            raise OSError("control file is not a regular non-reparse file")
        _path_operation_checkpoint("open", target)
        open_flags = flags | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(target.name, open_flags, mode, dir_fd=parent_fd)
        try:
            opened = os.fstat(descriptor)
            current = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not _is_regular_non_reparse(opened)
                or _file_identity(opened) != _file_identity(current)
                or (before is not None and _file_identity(before) != _file_identity(opened))
            ):
                raise OSError("control file changed or resolved through a reparse point while opening")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise


def safe_create_temp_file(
    directory: PathLike,
    *,
    prefix: str,
    suffix: str = "",
    mode: int = 0o600,
    token_length: int = 32,
    attempts: int = 128,
) -> tuple[int, Path]:
    """Create an exclusive file under an anchored, verified directory."""

    if not 1 <= token_length <= 32:
        raise ValueError("temporary filename token length must be between 1 and 32")
    parent = Path(os.path.abspath(os.path.expanduser(os.fspath(directory))))
    for _ in range(attempts):
        target = parent / f"{prefix}{uuid4().hex[:token_length]}{suffix}"
        try:
            descriptor = safe_open_regular_file(
                target,
                flags=os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                mode=mode,
            )
            return descriptor, target
        except FileExistsError:
            continue
    raise FileExistsError("could not allocate a unique safe temporary file")


def _windows_mark_delete(handle) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetFileInformationByHandle.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    kernel32.SetFileInformationByHandle.restype = wintypes.BOOL

    class _DispositionInfo(ctypes.Structure):
        _fields_ = [("delete_file", wintypes.BOOLEAN)]

    disposition = _DispositionInfo(1)
    if not kernel32.SetFileInformationByHandle(handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)):
        error = ctypes.get_last_error()
        raise _windows_os_error(error) from None


def _windows_rename_by_handle(handle, parent_handle, name: str, *, replace: bool) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetFileInformationByHandle.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    kernel32.SetFileInformationByHandle.restype = wintypes.BOOL

    class _RenameInfo(ctypes.Structure):
        _fields_ = [
            ("replace_if_exists", wintypes.BOOLEAN),
            ("root_directory", wintypes.HANDLE),
            ("file_name_length", wintypes.DWORD),
            ("file_name", ctypes.c_wchar * 1),
        ]

    encoded_name = name.encode("utf-16-le")
    name_offset = _RenameInfo.file_name.offset
    buffer_size = ctypes.sizeof(_RenameInfo) + len(encoded_name)
    buffer = ctypes.create_string_buffer(buffer_size)
    rename_info = ctypes.cast(buffer, ctypes.POINTER(_RenameInfo)).contents
    rename_info.replace_if_exists = wintypes.BOOLEAN(bool(replace))
    rename_info.root_directory = parent_handle
    rename_info.file_name_length = len(encoded_name)
    ctypes.memmove(ctypes.addressof(buffer) + name_offset, encoded_name, len(encoded_name))
    if not kernel32.SetFileInformationByHandle(
        handle,
        3,  # FileRenameInfo
        ctypes.byref(buffer),
        buffer_size,
    ):
        error = ctypes.get_last_error()
        raise _windows_os_error(error, name) from None


def path_identity_key(value: PathLike) -> str:
    """Return a stable, platform-native key for path comparisons and hashes."""

    path = Path(value).expanduser()
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        resolved = Path(os.path.abspath(os.fspath(path)))
    return os.path.normcase(os.path.normpath(os.fspath(resolved)))


def same_path(left: PathLike, right: PathLike) -> bool:
    """Compare path identity, including Windows short/long names when possible."""

    left_raw = os.fspath(left)
    right_raw = os.fspath(right)
    if not left_raw or not right_raw:
        return False
    try:
        if os.path.exists(left_raw) and os.path.exists(right_raw):
            return os.path.samefile(left_raw, right_raw)
    except (OSError, NotImplementedError, TypeError, ValueError):
        pass
    return path_identity_key(left_raw) == path_identity_key(right_raw)


def safe_read_bytes(path: PathLike, *, max_bytes: int = 16 * 1024 * 1024) -> bytes:
    """Read one stable regular file without following any path reparse point."""

    target = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    if os.name == "nt":
        with _windows_parent_handle(target) as _parent_handle:
            before = target.lstat()
            if not _is_regular_non_reparse(before) or before.st_size > max_bytes:
                raise OSError("file is not a bounded regular non-reparse file")
            descriptor = _windows_open_file(
                target,
                access=0x80000000 | 0x0080,  # GENERIC_READ | FILE_READ_ATTRIBUTES
                share=0x00000001,  # FILE_SHARE_READ; deny writes and renames while reading.
                creation=3,  # OPEN_EXISTING
                descriptor_flags=os.O_RDONLY,
            )
            with os.fdopen(descriptor, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if not _is_regular_non_reparse(opened) or _file_identity(opened) != _file_identity(before):
                    raise OSError("file changed or resolved through a reparse point while opening")
                _path_operation_checkpoint("read", target)
                content = stream.read(max_bytes + 1)
                after_open = os.fstat(stream.fileno())
                if len(content) > max_bytes or _file_identity(after_open) != _file_identity(opened):
                    raise OSError("file changed while being read")
                return content

    with _posix_parent_fd(target) as parent_fd:
        before = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        if not _is_regular_non_reparse(before) or before.st_size > max_bytes:
            raise OSError("file is not a bounded regular non-reparse file")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | os.O_NOFOLLOW
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        descriptor = os.open(target.name, flags, dir_fd=parent_fd)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not _is_regular_non_reparse(opened) or _file_identity(opened) != _file_identity(before):
                raise OSError("file changed or resolved through a reparse point while opening")
            _path_operation_checkpoint("read", target)
            content = stream.read(max_bytes + 1)
            after_open = os.fstat(stream.fileno())
            try:
                after_path = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError as exc:
                raise OSError("file changed while being read") from exc
            if (
                len(content) > max_bytes
                or _file_identity(after_open) != _file_identity(opened)
                or _file_identity(after_path) != _file_identity(opened)
            ):
                raise OSError("file changed while being read")
            return content


def safe_atomic_write_bytes(path: PathLike, content: bytes) -> None:
    """Atomically replace a control file without resolving links or junctions.

    Parent traversal and publication remain anchored to verified directories.
    The OS rename APIs do not offer a portable compare-by-file-ID replacement:
    a concurrently replaced existing leaf can still be overwritten after the
    final identity check. Callers that share a writable control directory must
    serialize writers if they require protection from that same-name race.
    POSIX also exposes staged files by name rather than rename-by-open-handle,
    so replacing the random temporary entry in its directory during the final
    check/use window remains outside the portable guarantee.
    """

    target = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}-{uuid4().hex}")
    if os.name == "nt":
        import msvcrt

        with _windows_parent_handle(target) as parent_handle:
            try:
                initial_file = target.lstat()
            except FileNotFoundError:
                initial_file = None
            if initial_file is not None and not _is_regular_non_reparse(initial_file):
                raise OSError("control file is not a regular non-reparse file")
            descriptor = _windows_open_file(
                temporary,
                access=0x40000000 | 0x00010000 | 0x0080,  # GENERIC_WRITE | DELETE | FILE_READ_ATTRIBUTES
                share=0x00000001,
                creation=1,  # CREATE_NEW
                descriptor_flags=os.O_RDWR,
            )
            renamed = False
            with os.fdopen(descriptor, "w+b") as stream:
                try:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                    try:
                        current_file = target.lstat()
                    except FileNotFoundError:
                        current_file = None
                    if (initial_file is None) != (current_file is None):
                        raise OSError("control file appeared or disappeared during write")
                    if (
                        initial_file is not None
                        and current_file is not None
                        and _file_identity(current_file) != _file_identity(initial_file)
                    ):
                        raise OSError("control file changed during write")
                    _path_operation_checkpoint("write", target)
                    _windows_rename_by_handle(
                        msvcrt.get_osfhandle(stream.fileno()),
                        None,
                        str(target),
                        replace=initial_file is not None,
                    )
                    renamed = True
                finally:
                    if not renamed:
                        try:
                            _windows_mark_delete(msvcrt.get_osfhandle(stream.fileno()))
                        except OSError:
                            pass
        return

    with _posix_parent_fd(target) as parent_fd:
        try:
            initial_file = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            initial_file = None
        if initial_file is not None and not _is_regular_non_reparse(initial_file):
            raise OSError("control file is not a regular non-reparse file")
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0) | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(temporary.name, flags, 0o600, dir_fd=parent_fd)
        renamed = False
        with os.fdopen(descriptor, "wb") as stream:
            temporary_identity = _file_object_identity(os.fstat(stream.fileno()))
            try:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
                try:
                    current_file = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    current_file = None
                if (initial_file is None) != (current_file is None):
                    raise OSError("control file appeared or disappeared during write")
                if initial_file is not None and current_file is not None and _file_identity(current_file) != _file_identity(initial_file):
                    raise OSError("control file changed during write")
                current_temporary = os.stat(temporary.name, dir_fd=parent_fd, follow_symlinks=False)
                if _file_object_identity(current_temporary) != temporary_identity:
                    raise OSError("temporary control file changed during write")
                _path_operation_checkpoint("write", target)
                os.rename(temporary.name, target.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                renamed = True
            finally:
                if not renamed:
                    try:
                        candidate = os.stat(temporary.name, dir_fd=parent_fd, follow_symlinks=False)
                        if _file_object_identity(candidate) == temporary_identity:
                            os.unlink(temporary.name, dir_fd=parent_fd)
                    except OSError:
                        # Preserve the original write error; a failed safe cleanup may leave a temp file.
                        pass


def safe_atomic_write_text(path: PathLike, content: str, *, encoding: str = "utf-8") -> None:
    safe_atomic_write_bytes(path, content.encode(encoding))


def safe_unlink_if_identity(path: PathLike, expected) -> bool:
    """Unlink a regular file only if its no-follow identity still matches."""

    target = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    if os.name == "nt":
        import msvcrt

        with _windows_parent_handle(target) as _parent_handle:
            try:
                current = target.lstat()
            except FileNotFoundError:
                return False
            if _file_identity(current) != _file_identity(expected) or not _is_regular_non_reparse(current):
                return False
            try:
                descriptor = _windows_open_file(
                    target,
                    access=0x00010000 | 0x0080,  # DELETE | FILE_READ_ATTRIBUTES
                    share=0x00000001 | 0x00000002,  # Share readers/writers, but deny rename/delete.
                    creation=3,
                    descriptor_flags=os.O_RDONLY,
                )
            except FileNotFoundError:
                return False
            with os.fdopen(descriptor, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if _file_identity(opened) != _file_identity(expected) or not _is_regular_non_reparse(opened):
                    return False
                _path_operation_checkpoint("unlink", target)
                _windows_mark_delete(msvcrt.get_osfhandle(stream.fileno()))
            return True

    with _posix_parent_fd(target) as parent_fd:
        try:
            current = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if _file_identity(current) != _file_identity(expected) or not _is_regular_non_reparse(current):
            return False
        _path_operation_checkpoint("unlink", target)
        try:
            os.unlink(target.name, dir_fd=parent_fd)
            return True
        except FileNotFoundError:
            return False
