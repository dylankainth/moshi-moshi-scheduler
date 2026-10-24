import ctypes
from ctypes import wintypes

from .source import RawWindow

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
UWP_HOST = "applicationframehost.exe"


def idle_ms(tick: int, last_input: int) -> int:
    # GetTickCount wraps every ~49.7 days; both are DWORDs.
    return (tick - last_input) & 0xFFFFFFFF


def resolve_uwp(host_pid: int, exe: str | None, child_pids: list[int], exe_of) -> str | None:
    # UWP apps are hosted by ApplicationFrameHost; the real app is a child window owned by another process.
    if (exe or "").lower() != UWP_HOST:
        return exe
    for pid in child_pids:
        if pid != host_pid:
            return exe_of(pid) or exe
    return exe


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class Win32Source:
    def __init__(self):
        u = ctypes.WinDLL("user32", use_last_error=True)
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        self._enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        u.GetForegroundWindow.argtypes, u.GetForegroundWindow.restype = [], wintypes.HWND
        u.GetWindowTextLengthW.argtypes, u.GetWindowTextLengthW.restype = [wintypes.HWND], ctypes.c_int
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetWindowTextW.restype = ctypes.c_int
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        u.EnumChildWindows.argtypes = [wintypes.HWND, self._enum_proc, wintypes.LPARAM]
        u.EnumChildWindows.restype = wintypes.BOOL
        u.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
        u.GetLastInputInfo.restype = wintypes.BOOL
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD)]
        k.QueryFullProcessImageNameW.restype = wintypes.BOOL
        k.CloseHandle.argtypes, k.CloseHandle.restype = [wintypes.HANDLE], wintypes.BOOL
        k.GetTickCount.argtypes, k.GetTickCount.restype = [], wintypes.DWORD
        self.u, self.k = u, k

    def _pid(self, hwnd) -> int:
        pid = wintypes.DWORD()
        self.u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value

    def _exe(self, pid: int) -> str | None:
        h = self.k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return None
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if not self.k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return None
            return buf.value.rsplit("\\", 1)[-1].lower()
        finally:
            self.k.CloseHandle(h)

    def _child_pids(self, hwnd) -> list[int]:
        pids = []
        cb = self._enum_proc(lambda child, _: pids.append(self._pid(child)) or True)
        self.u.EnumChildWindows(hwnd, cb, 0)
        return pids

    def foreground(self) -> RawWindow | None:
        hwnd = self.u.GetForegroundWindow()
        if not hwnd:
            return None
        n = self.u.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        self.u.GetWindowTextW(hwnd, buf, n + 1)
        pid = self._pid(hwnd)
        exe = self._exe(pid)
        if (exe or "") == UWP_HOST:
            exe = resolve_uwp(pid, exe, self._child_pids(hwnd), self._exe)
        return RawWindow(exe=exe, title=buf.value)

    def idle_seconds(self) -> float:
        lii = LASTINPUTINFO(cbSize=ctypes.sizeof(LASTINPUTINFO))
        if not self.u.GetLastInputInfo(ctypes.byref(lii)):
            return 0.0
        return idle_ms(self.k.GetTickCount(), lii.dwTime) / 1000
