"""Create and verify Mochi's per-user Windows Start menu identity."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
from tempfile import TemporaryDirectory


APP_ID = "Mochi.DeskPet"
ARGUMENTS = "-m deskpet"
ROOT = Path(__file__).resolve().parents[2]


def _shortcut_path() -> Path:
    from win32com.shell import shell, shellcon

    programs = shell.SHGetSpecialFolderPath(0, shellcon.CSIDL_PROGRAMS, False)
    return Path(programs) / "Mochi.lnk"


def _shell_link(path: Path | None = None):
    import pythoncom
    from win32com.shell import shell

    link = pythoncom.CoCreateInstance(
        shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink,
    )
    file = link.QueryInterface(pythoncom.IID_IPersistFile)
    if path is not None:
        file.Load(str(path))
    return link, file


def _identity(link):
    from win32com.propsys import propsys, pscon

    store = link.QueryInterface(propsys.IID_IPropertyStore)
    return store.GetValue(pscon.PKEY_AppUserModel_ID).GetValue()


def _belongs_to_checkout(path: Path) -> bool:
    from win32com.shell import shell

    if not path.is_file():
        return False
    link, _ = _shell_link(path)
    executable, _ = link.GetPath(shell.SLGP_RAWPATH)
    target = Path(executable)
    return (
        _identity(link) == APP_ID
        and target.is_file()
        and target.parent == Path(sys.executable).parent
        and link.GetArguments() == ARGUMENTS
        and Path(link.GetWorkingDirectory()) == ROOT
    )


def activate_notification_identity() -> str:
    """Use an existing installed identity. This performs no installation."""
    import ctypes
    import pythoncom

    try:
        installed = _belongs_to_checkout(_shortcut_path())
    except pythoncom.com_error as error:
        raise RuntimeError("无法读取 Mochi Windows 开始菜单快捷方式") from error
    if not installed:
        raise RuntimeError("系统通知需要先安装 Mochi Windows 开始菜单快捷方式")
    set_identity = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
    set_identity.argtypes = [ctypes.c_wchar_p]
    set_identity.restype = ctypes.c_long
    if set_identity(APP_ID) != 0:
        raise RuntimeError("无法设置 Mochi Windows 应用标识")
    return APP_ID


def _install_shortcut(path: Path) -> None:
    import pythoncom
    from win32com.propsys import propsys, pscon

    executable = Path(sys.executable).with_name("pythonw.exe")
    if not executable.is_file():
        executable = Path(sys.executable)
    link, file = _shell_link()
    link.SetPath(str(executable))
    link.SetArguments(ARGUMENTS)
    link.SetWorkingDirectory(str(ROOT))
    link.SetDescription("Mochi desktop pet")

    store = link.QueryInterface(propsys.IID_IPropertyStore)
    store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(APP_ID, pythoncom.VT_LPWSTR))
    store.Commit()
    with TemporaryDirectory(dir=path.parent) as temporary:
        new_link = Path(temporary) / path.name
        file.Save(str(new_link), False)
        new_link.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "remove"))
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("This shortcut is installed on Windows.")
    if sys.prefix == sys.base_prefix:
        parser.error("Run this command with the environment created by uv sync.")

    path = _shortcut_path()
    if path.exists():
        link, _ = _shell_link(path)
        if _identity(link) != APP_ID:
            parser.error(f"{path} already exists with a different application identity.")
    if args.action == "remove":
        path.unlink(missing_ok=True)
        print(f"Removed {path}.")
    else:
        _install_shortcut(path)
        if not _belongs_to_checkout(path):
            parser.error(f"Windows could not verify the shortcut at {path}.")
        print(f"Installed {path}. Open Mochi from the Start menu.")


if __name__ == "__main__":
    main()
