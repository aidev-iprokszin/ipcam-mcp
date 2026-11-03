import errno

from ipcam_mcp.tooling import client_message


def test_file_error_names_the_path():
    e = PermissionError(errno.EACCES, "Permission denied", "/media/aaaaaaaa/camera.json")
    assert client_message(e) == "could not access /media/aaaaaaaa/camera.json: Permission denied"


def test_os_error_without_path_stays_generic():
    e = ConnectionRefusedError(errno.ECONNREFUSED, "Connect call failed ('192.0.2.1', 554)")
    assert client_message(e) == "internal error (ConnectionRefusedError); see server log"
