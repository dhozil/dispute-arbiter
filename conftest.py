"""Windows-safe patch for genlayer-test direct mode.

The stock ``gltest.direct.loader._inject_message_to_fd0`` calls
``os.unlink(path)`` immediately after ``os.dup2(fd, 0)``. On POSIX unlink of an
open file succeeds; on Windows it raises ``PermissionError`` because the
process still holds an open handle through stdin. This breaks every
``direct_deploy`` on Windows.

Fix: defer the unlink to the *next* injection, by which point ``dup2`` has
closed the previous handle so the file can actually be removed.
"""

import os
import tempfile

import gltest.direct.loader as _loader

_pending_message_files: list = []


def _windows_safe_inject_message_to_fd0(vm) -> None:
    """Inject the message context into stdin (fd 0) without Windows unlink bug."""
    while _pending_message_files:
        try:
            os.unlink(_pending_message_files.pop(0))
        except OSError:
            break

    from genlayer.py import calldata
    from genlayer.py.types import Address

    sender_addr = vm.sender
    if isinstance(sender_addr, bytes):
        sender_addr = Address(sender_addr)

    contract_addr = vm._contract_address
    if isinstance(contract_addr, bytes):
        contract_addr = Address(contract_addr)

    origin_addr = vm.origin
    if isinstance(origin_addr, bytes):
        origin_addr = Address(origin_addr)

    message_data = {
        "contract_address": contract_addr,
        "sender_address": sender_addr,
        "origin_address": origin_addr,
        "stack": [],
        "value": vm._value,
        "datetime": vm._datetime,
        "is_init": False,
        "chain_id": vm._chain_id,
        "entry_kind": 0,
        "entry_data": b"",
        "entry_stage_data": None,
    }

    encoded = calldata.encode(message_data)

    fd, path = tempfile.mkstemp()
    _pending_message_files.append(path)
    try:
        os.write(fd, encoded)
        os.lseek(fd, 0, os.SEEK_SET)

        original_stdin = os.dup(0)
        vm._original_stdin_fd = original_stdin

        os.dup2(fd, 0)
    finally:
        os.close(fd)


if hasattr(_loader, "_inject_message_to_fd0") and _loader._inject_message_to_fd0 is not None:
    _loader._inject_message_to_fd0 = _windows_safe_inject_message_to_fd0
