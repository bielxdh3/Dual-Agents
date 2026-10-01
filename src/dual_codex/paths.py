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


def _windows_close_handle(handle) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle(handle)


def _windows_handle_identity(handle) -> tuple[int, int, int]:
    import ctypes
    from ctypes import wintypes

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

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetFileInformationByHandle.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ByHandleInfo))
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    info = _ByHandleInfo()
    if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
        error = ctypes.get_last_error()
        raise _windows_os_error(error) from None
    return int(info.attributes), int(info.volume), (int(info.index_high) << 32) | int(info.index_low)


def _windows_nt_create_file_at(
    parent_handle,
    name: str,
    *,
    access: int,
    share: int,
    disposition: int,
    options: int,
    attributes: int = 0x80,
):
    """Open one child by name relative to a verified Windows directory handle."""

    import ctypes
    from ctypes import wintypes

    if not name or name in {".", ".."} or any(character in name for character in ("\\", "/", "\0", ":")):
        raise ValueError("relative Windows path operations require one safe name component")

    class _UnicodeString(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.USHORT),
            ("maximum_length", wintypes.USHORT),
            ("buffer", wintypes.LPWSTR),
        ]

    class _ObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.ULONG),
            ("root_directory", wintypes.HANDLE),
            ("object_name", ctypes.POINTER(_UnicodeString)),
            ("attributes", wintypes.ULONG),
            ("security_descriptor", wintypes.LPVOID),
            ("security_quality_of_service", wintypes.LPVOID),
        ]

    class _IoStatusBlock(ctypes.Structure):
        _fields_ = [("status", wintypes.LONG), ("information", ctypes.c_void_p)]

    encoded_name = name.encode("utf-16-le")
    name_buffer = ctypes.create_unicode_buffer(name)
    object_name = _UnicodeString(len(encoded_name), len(encoded_name) + 2, ctypes.cast(name_buffer, wintypes.LPWSTR))
    object_attributes = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes),
        parent_handle,
        ctypes.pointer(object_name),
        0x00000040,  # OBJ_CASE_INSENSITIVE
        None,
        None,
    )
    io_status = _IoStatusBlock()
    handle = wintypes.HANDLE()
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtCreateFile.argtypes = (
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_IoStatusBlock),
        ctypes.c_void_p,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        ctypes.c_void_p,
        wintypes.ULONG,
    )
    ntdll.NtCreateFile.restype = wintypes.LONG
    status = int(
        ntdll.NtCreateFile(
            ctypes.byref(handle),
            access,
            ctypes.byref(object_attributes),
            ctypes.byref(io_status),
            None,
            attributes,
            share,
            disposition,
            options,
            None,
            0,
        )
    )
    if status < 0:
        ntdll.RtlNtStatusToDosError.argtypes = (wintypes.LONG,)
        ntdll.RtlNtStatusToDosError.restype = wintypes.ULONG
        error = int(ntdll.RtlNtStatusToDosError(status))
        raise _windows_os_error(error, name) from None
    return handle


def _windows_open_root(path: Path, *, access: int = 0x0080):
    import ctypes
    from ctypes import wintypes

    path_info = path.lstat()
    if not _is_regular_directory(path_info):
        raise OSError("directory root is not a regular non-reparse directory")
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
    handle = kernel32.CreateFileW(
        str(path),
        access,
        0x00000001 | 0x00000002,  # Share read/write; deny delete.
        None,
        3,  # OPEN_EXISTING
        0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.get_last_error()
        raise _windows_os_error(error, str(path)) from None
    try:
        attributes, volume, file_id = _windows_handle_identity(handle)
        if (
            attributes & (0x10 | 0x400) != 0x10
            or volume != (int(path_info.st_dev) & 0xFFFFFFFF)
            or file_id != int(path_info.st_ino)
        ):
            raise OSError("directory root changed or resolved through a reparse point")
    except BaseException:
        _windows_close_handle(handle)
        raise
    return handle


def _windows_open_directory_at(parent_handle, name: str, *, access: int = 0x0080):
    handle = _windows_nt_create_file_at(
        parent_handle,
        name,
        access=access,
        share=0x00000001 | 0x00000002,  # Share read/write; deny delete.
        disposition=1,  # FILE_OPEN
        options=0x00000001 | 0x00200000,  # FILE_DIRECTORY_FILE | FILE_OPEN_REPARSE_POINT
        attributes=0x10,
    )
    try:
        attributes, _, _ = _windows_handle_identity(handle)
        if attributes & (0x10 | 0x400) != 0x10:
            raise OSError("directory path is not a regular non-reparse directory")
    except BaseException:
        _windows_close_handle(handle)
        raise
    return handle


def _windows_verify_directory_path(path: Path, handle) -> None:
    """Require a path's no-follow identity to match its already-open directory handle."""

    path_info = path.lstat()
    _windows_verify_directory_info(path_info, handle)


def _windows_verify_directory_info(path_info, handle) -> None:
    """Compare a no-follow path snapshot with the identity of an open directory handle."""

    attributes, volume, file_id = _windows_handle_identity(handle)
    if (
        not _is_regular_directory(path_info)
        or attributes & (0x10 | 0x400) != 0x10
        or volume != (int(path_info.st_dev) & 0xFFFFFFFF)
        or file_id != int(path_info.st_ino)
    ):
        raise OSError("directory path changed or resolved through a reparse point")


def _attach_safe_path_context(error: OSError, *, stage: str, operation: str, component: str) -> None:
    """Attach bounded operation context without changing the original OSError."""

    try:
        error._dual_codex_path_context = {
            "stage": stage,
            "operation": operation,
            "path_component": component,
        }
    except (AttributeError, TypeError):
        pass


def _windows_open_verified_directory_at(parent_handle, name: str, path: Path, *, access: int = 0x0080):
    """Open a child relative to its parent and verify the named path is that handle."""

    path_info = path.lstat()
    if not _is_regular_directory(path_info):
        raise OSError("directory path is not a regular non-reparse directory")
    handle = _windows_open_directory_at(parent_handle, name, access=access)
    try:
        _windows_verify_directory_info(path_info, handle)
    except BaseException:
        _windows_close_handle(handle)
        raise
    return handle


def _windows_create_directory_at(parent_handle, name: str):
    handle = _windows_nt_create_file_at(
        parent_handle,
        name,
        access=0x0080,  # FILE_READ_ATTRIBUTES
        share=0x00000001 | 0x00000002,
        disposition=2,  # FILE_CREATE
        options=0x00000001 | 0x00200000,  # FILE_DIRECTORY_FILE | FILE_OPEN_REPARSE_POINT
        attributes=0x10,
    )
    try:
        attributes, _, _ = _windows_handle_identity(handle)
        if attributes & (0x10 | 0x400) != 0x10:
            raise OSError("created directory is not a regular non-reparse directory")
    except BaseException:
        _windows_close_handle(handle)
        raise
    return handle


@contextmanager
def _windows_parent_handle(target: Path, *, leaf_access: int = 0):
    """Walk parents relative to verified handles and yield the exact leaf parent."""

    parent_components = target.parent.parts[1:]
    root_access = 0x0080 | (leaf_access if not parent_components else 0)
    handles = [_windows_open_root(Path(target.anchor), access=root_access)]
    current_path = Path(target.anchor)
    try:
        for index, component in enumerate(parent_components):
            access = 0x0080  # FILE_READ_ATTRIBUTES
            if index == len(parent_components) - 1:
                access |= leaf_access
            current_path /= component
            handles.append(_windows_open_verified_directory_at(handles[-1], component, current_path, access=access))
        yield handles[-1]
    finally:
        for handle in reversed(handles):
            _windows_close_handle(handle)


def _path_operation_checkpoint(_operation: str, _target: Path) -> None:
    """Private deterministic race-test seam; production behavior is a no-op."""


def safe_ensure_directory_tree(path: PathLike, *, mode: int = 0o700) -> Path:
    """Create a directory chain without following links or reparse points."""

    target = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    if os.name == "nt":
        handles = []
        components = target.parts[1:]
        current_path = Path(target.anchor)
        current_component = target.anchor or "root"
        current_operation = "open_root"
        try:
            handles.append(_windows_open_root(Path(target.anchor)))
            for index, component in enumerate(components):
                current_path /= component
                current_component = component
                current_operation = "open_child_directory"
                try:
                    child = _windows_open_verified_directory_at(handles[-1], component, current_path)
                except FileNotFoundError:
                    current_operation = "open_parent_for_directory_creation"
                    if index == 0:
                        writable_parent = _windows_open_root(
                            Path(target.anchor),
                            access=0x0080 | 0x0004,  # FILE_READ_ATTRIBUTES | FILE_ADD_SUBDIRECTORY
                        )
                    else:
                        writable_parent = _windows_open_verified_directory_at(
                            handles[-2],
                            components[index - 1],
                            current_path.parent,
                            access=0x0080 | 0x0004,  # FILE_READ_ATTRIBUTES | FILE_ADD_SUBDIRECTORY
                        )
                    try:
                        if _windows_handle_identity(writable_parent) != _windows_handle_identity(handles[-1]):
                            raise OSError("directory parent changed while preparing creation")
                        current_operation = "create_child_directory"
                        try:
                            child = _windows_create_directory_at(writable_parent, component)
                            try:
                                current_operation = "verify_created_directory"
                                _windows_verify_directory_path(current_path, child)
                            except BaseException:
                                _windows_close_handle(child)
                                raise
                        except FileExistsError:
                            current_operation = "open_concurrently_created_directory"
                            child = _windows_open_verified_directory_at(handles[-1], component, current_path)
                    finally:
                        _windows_close_handle(writable_parent)
                handles.append(child)
        except OSError as exc:
            _attach_safe_path_context(
                exc,
                stage="safe_ensure_directory_tree",
                operation=current_operation,
                component=current_component,
            )
            raise
        finally:
            for handle in reversed(handles):
                _windows_close_handle(handle)
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


def _windows_open_file_at(
    parent_handle,
    name: str,
    *,
    access: int,
    share: int,
    creation: int,
    descriptor_flags: int = os.O_RDONLY,
):
    import msvcrt
    disposition = {1: 2, 2: 5, 3: 1, 4: 3, 5: 4}.get(creation)
    if disposition is None:
        raise ValueError("unsupported Windows file creation mode")
    handle = _windows_nt_create_file_at(
        parent_handle,
        name,
        access=access | 0x00100000,  # SYNCHRONIZE for synchronous file I/O.
        share=share,
        disposition=disposition,
        options=0x00000020 | 0x00000040 | 0x00200000,  # SYNCHRONOUS_IO_NONALERT | NON_DIRECTORY_FILE | OPEN_REPARSE_POINT
    )
    try:
        descriptor = msvcrt.open_osfhandle(int(handle.value), os.O_BINARY | descriptor_flags)
    except BaseException:
        _windows_close_handle(handle)
        raise
    return descriptor


def _windows_stat_file_at(parent_handle, name: str):
    import os

    descriptor = _windows_open_file_at(
        parent_handle,
        name,
        access=0x0080,  # FILE_READ_ATTRIBUTES
        share=0x00000001 | 0x00000002,
        creation=3,
        descriptor_flags=os.O_RDONLY,
    )
    with os.fdopen(descriptor, "rb") as stream:
        return os.fstat(stream.fileno())


def safe_open_regular_file(path: PathLike, *, flags: int, mode: int = 0o600) -> int:
    """Open a regular control file relative to a verified, no-follow parent chain."""

    target = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    if flags & (os.O_TRUNC | os.O_APPEND):
        raise ValueError("safe control-file opens do not permit implicit truncation or append")
    if os.name == "nt":
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
        descriptor_flags = access_mode | os.O_BINARY
        with _windows_parent_handle(target, leaf_access=0x0002 if exclusive else 0) as parent_handle:
            try:
                before = _windows_stat_file_at(parent_handle, target.name)
            except FileNotFoundError:
                before = None
            if before is not None and not _is_regular_non_reparse(before):
                raise OSError("control file is not a regular non-reparse file")
            _path_operation_checkpoint("open", target)
            if creating and not exclusive and before is None:
                try:
                    descriptor = _windows_open_file_at(
                        parent_handle,
                        target.name,
                        access=desired_access,
                        share=0x00000001 | 0x00000002,  # FILE_SHARE_READ | FILE_SHARE_WRITE; deny delete.
                        creation=3,  # OPEN_EXISTING
                        descriptor_flags=descriptor_flags,
                    )
                except FileNotFoundError:
                    with _windows_parent_handle(target, leaf_access=0x0002) as create_parent:
                        if _windows_handle_identity(create_parent) != _windows_handle_identity(parent_handle):
                            raise OSError("file parent changed while preparing creation")
                        try:
                            descriptor = _windows_open_file_at(
                                create_parent,
                                target.name,
                                access=desired_access,
                                share=0x00000001 | 0x00000002,
                                creation=1,  # CREATE_NEW; this branch observed the leaf absent.
                                descriptor_flags=descriptor_flags,
                            )
                        except FileExistsError:
                            # Preserve non-exclusive O_CREAT behavior if another writer won the race.
                            descriptor = _windows_open_file_at(
                                parent_handle,
                                target.name,
                                access=desired_access,
                                share=0x00000001 | 0x00000002,
                                creation=3,  # OPEN_EXISTING
                                descriptor_flags=descriptor_flags,
                            )
            else:
                creation = 1 if exclusive else 3  # CREATE_NEW / OPEN_EXISTING
                descriptor = _windows_open_file_at(
                    parent_handle,
                    target.name,
                    access=desired_access,
                    share=0x00000001 | 0x00000002,  # FILE_SHARE_READ | FILE_SHARE_WRITE; deny delete.
                    creation=creation,
                    descriptor_flags=descriptor_flags,
                )
            try:
                opened = os.fstat(descriptor)
                if (
                    not _is_regular_non_reparse(opened)
                    or (before is not None and _file_identity(before) != _file_identity(opened))
                ):
                    raise OSError("control file changed or resolved through a reparse point while opening")
                return descriptor
            except BaseException:
                os.close(descriptor)
                raise

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
        except OSError as exc:
            _attach_safe_path_context(
                exc,
                stage="safe_create_temp_file",
                operation="create_exclusive_temp_file",
                component=target.name,
            )
            raise
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

    class _RenameInfo(ctypes.Structure):
        _fields_ = [
            ("replace_if_exists", wintypes.BOOLEAN),
            ("root_directory", wintypes.HANDLE),
            ("file_name_length", wintypes.DWORD),
            ("file_name", ctypes.c_wchar * 1),
        ]

    encoded_name = name.encode("utf-16-le")
    name_offset = _RenameInfo.file_name.offset
    buffer_size = name_offset + len(encoded_name) + 2
    buffer = ctypes.create_string_buffer(buffer_size)
    rename_info = ctypes.cast(buffer, ctypes.POINTER(_RenameInfo)).contents
    rename_info.replace_if_exists = wintypes.BOOLEAN(bool(replace))
    rename_info.root_directory = parent_handle
    rename_info.file_name_length = len(encoded_name)
    ctypes.memmove(ctypes.addressof(buffer) + name_offset, encoded_name, len(encoded_name))
    class _IoStatusBlock(ctypes.Structure):
        _fields_ = [("status", wintypes.LONG), ("information", ctypes.c_void_p)]

    io_status = _IoStatusBlock()
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtSetInformationFile.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_IoStatusBlock),
        ctypes.c_void_p,
        wintypes.ULONG,
        wintypes.ULONG,
    )
    ntdll.NtSetInformationFile.restype = wintypes.LONG
    status = int(
        ntdll.NtSetInformationFile(
            wintypes.HANDLE(handle),
            ctypes.byref(io_status),
            ctypes.byref(buffer),
            buffer_size,
            10,  # FileRenameInformation
        )
    )
    if status < 0:
        ntdll.RtlNtStatusToDosError.argtypes = (wintypes.LONG,)
        ntdll.RtlNtStatusToDosError.restype = wintypes.ULONG
        error = int(ntdll.RtlNtStatusToDosError(status))
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
        with _windows_parent_handle(target) as parent_handle:
            before = _windows_stat_file_at(parent_handle, target.name)
            if not _is_regular_non_reparse(before) or before.st_size > max_bytes:
                raise OSError("file is not a bounded regular non-reparse file")
            _path_operation_checkpoint("read", target)
            descriptor = _windows_open_file_at(
                parent_handle,
                target.name,
                access=0x80000000 | 0x0080,  # GENERIC_READ | FILE_READ_ATTRIBUTES
                share=0x00000001,  # FILE_SHARE_READ; deny writes and renames while reading.
                creation=3,  # OPEN_EXISTING
                descriptor_flags=os.O_RDONLY,
            )
            with os.fdopen(descriptor, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (
                    not _is_regular_non_reparse(opened)
                    or opened.st_size > max_bytes
                    or _file_identity(opened) != _file_identity(before)
                ):
                    raise OSError("file is not a bounded regular non-reparse file")
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

        with _windows_parent_handle(target, leaf_access=0x0002) as parent_handle:
            try:
                initial_file = _windows_stat_file_at(parent_handle, target.name)
            except FileNotFoundError:
                initial_file = None
            if initial_file is not None and not _is_regular_non_reparse(initial_file):
                raise OSError("control file is not a regular non-reparse file")
            descriptor = _windows_open_file_at(
                parent_handle,
                temporary.name,
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
                        current_file = _windows_stat_file_at(parent_handle, target.name)
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
                        parent_handle,
                        target.name,
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

        with _windows_parent_handle(target) as parent_handle:
            try:
                descriptor = _windows_open_file_at(
                    parent_handle,
                    target.name,
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
