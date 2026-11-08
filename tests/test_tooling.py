import errno

from ipcam_mcp.tooling import client_message, os_error_text


def test_file_error_names_the_path():
    e = PermissionError(errno.EACCES, "Permission denied", "/media/aaaaaaaa/camera.json")
    assert client_message(e) == "could not access /media/aaaaaaaa/camera.json: Permission denied"


def test_os_error_without_path_stays_generic():
    e = ConnectionRefusedError(errno.ECONNREFUSED, "Connect call failed ('192.0.2.1', 554)")
    assert client_message(e) == "internal error (ConnectionRefusedError); see server log"


def test_os_error_text_names_both_paths_of_a_rename():
    e = OSError(errno.EXDEV, "Invalid cross-device link", "/m/a", None, "/n/b")
    assert os_error_text(e) == "/m/a -> /n/b: Invalid cross-device link"


def test_os_error_text_one_path():
    e = PermissionError(errno.EACCES, "Permission denied", "/m/a")
    assert os_error_text(e) == "/m/a: Permission denied"
