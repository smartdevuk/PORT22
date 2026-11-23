"""Bounded, ordered LSTAT batches on one worker-owned Paramiko channel."""

from paramiko import SFTPAttributes, SFTPError
from paramiko.sftp import CMD_ATTRS, CMD_LSTAT, CMD_STATUS


class StatRequests:
    # Paramiko has no public API for pipelining LSTAT. Its async receiver
    # interface routes replies by request ID, including replies consumed
    # while an intervening synchronous operation (e.g. CLOSE) is waiting.
    limit = 16

    def __init__(self, sftp, paths):
        if len(paths) > self.limit:
            raise ValueError("Too many metadata requests in one batch")
        self.sftp = sftp
        self.replies = {}
        self.ids = [
            sftp._async_request(self, CMD_LSTAT, sftp._adjust_cwd(path))
            for path in paths
        ]

    def _async_response(self, opcode, message, request_id):
        self.replies[request_id] = (opcode, message)

    def results(self):
        while len(self.replies) < len(self.ids):
            self.sftp._read_response()
        for request_id in self.ids:
            opcode, message = self.replies[request_id]
            if opcode == CMD_ATTRS:
                yield SFTPAttributes._from_msg(message)
            elif opcode == CMD_STATUS:
                try:
                    self.sftp._convert_status(message)
                except FileNotFoundError:
                    yield None
                else:
                    raise SFTPError("LSTAT returned success without attributes")
            else:
                raise SFTPError("Unexpected response to LSTAT")
