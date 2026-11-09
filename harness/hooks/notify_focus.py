"""Помощник notify_popup.ps1: клик по алерту -> вкладка Windows Terminal этой сессии.

python notify_focus.py --pid <pid claude.exe> --born <FILETIME создания>

PID и время создания claude.exe берёт notification.py через find_host до запуска окна;
при клике время создания сверяется заново: процесс исчез или PID занят другим - выход 1.
Находит консоль процесса claude.exe (AttachConsole), её окно-хозяин Windows
Terminal и выбирает вкладку через UI Automation (PowerShell, Select()). Заголовки
вкладок совпадают с заголовками консолей и начинаются со спиннером, поэтому
сравнение идёт без ведущих не-букв. Заголовок не уникален (две вкладки
"Claude Code") - консоли на время выбора ставится уникальный, затем прежний
возвращается. На stdout - десятичный HWND окна, которое надо поднять (вкладка уже
выбрана); пусто и код 1 - сфокусировать нельзя, окно-алерт просто закрывается.
Поднимает окно сам notify_popup.ps1: право на передний план есть у процесса,
получившего клик. Только Windows; любая ошибка глотается.
"""
from __future__ import annotations

import base64
import os
import subprocess
import sys
import time

_WT_CLASS = 'CASCADIA_HOSTING_WINDOW_CLASS'
_CONHOST_CLASS = 'ConsoleWindowClass'
_GA_ROOTOWNER = 3
# Бюджет: две попытки выбора - (5 + 0) + (5 + 2) = 12 с в худшем случае, у вызывающего
# notify_popup.ps1 терпения 25 с; возврат заголовка в finally успевает с запасом
_PS_TIMEOUT = 5
_TOKEN_WAIT_MS = 2000
_HOST_IMAGE = 'claude.exe'
_MAX_DEPTH = 32
_STILL_ACTIVE = 259

# @HWND@ и @WAIT@ - целые, @KEY@ - base64 UTF-8: в код PowerShell строки не попадают
# (заголовок консоли задаёт любая программа сессии, кавычки в нём бывают любые)
_SELECT_PS = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
function N([string]$s) { [regex]::Replace($s, '^[^\p{L}\p{N}]+', '') }
$key = N ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('@KEY@')))
if (-not $key) { 'many'; exit 0 }
$A = [System.Windows.Automation.AutomationElement]
$win = $A::FromHandle([IntPtr]@HWND@)
$cond = New-Object System.Windows.Automation.PropertyCondition($A::ControlTypeProperty, [System.Windows.Automation.ControlType]::TabItem)
$until = [DateTime]::UtcNow.AddMilliseconds(@WAIT@)
$state = 'none'
do {
    $hit = @($win.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond) | Where-Object { (N $_.Current.Name) -ceq $key })
    if ($hit.Count -eq 1) {
        $hit[0].GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select()
        $state = 'selected'
        break
    }
    if ($hit.Count -gt 1) { $state = 'many'; break }
    Start-Sleep -Milliseconds 150
} while ([DateTime]::UtcNow -lt $until)
$state
"""


class _Console:
    """Консоль чужого процесса: attach - AttachConsole, detach - FreeConsole."""

    def __init__(self, pid: int) -> None:
        import ctypes
        from ctypes import wintypes
        self.pid = pid
        self.k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        self.u32 = ctypes.WinDLL('user32', use_last_error=True)
        self.k32.GetConsoleWindow.restype = wintypes.HWND
        self.u32.GetAncestor.restype = wintypes.HWND
        self.u32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.u32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.ctypes = ctypes

    def attach(self) -> None:
        self.k32.FreeConsole()
        if not self.k32.AttachConsole(self.pid):
            raise OSError(f'AttachConsole({self.pid}) failed')

    def detach(self) -> None:
        self.k32.FreeConsole()

    def title(self) -> str:
        buf = self.ctypes.create_unicode_buffer(1024)
        self.k32.GetConsoleTitleW(buf, 1024)
        return buf.value

    def set_title(self, text: str) -> None:
        self.k32.SetConsoleTitleW(text)

    def root_window(self) -> tuple[int, str]:
        """Окно-хозяин консоли (для conpty - окно Windows Terminal) и его класс."""
        hwnd = self.k32.GetConsoleWindow()
        root = self.u32.GetAncestor(hwnd, _GA_ROOTOWNER) if hwnd else None
        if not root:
            return 0, ''
        buf = self.ctypes.create_unicode_buffer(256)
        self.u32.GetClassNameW(root, buf, 256)
        return int(root), buf.value


def _select_tab(hwnd: int, title: str, wait_ms: int) -> str:
    """'selected' - вкладка выбрана; 'many' - заголовок не уникален; 'none' - нет такой.

    Заводит один PowerShell не дольше _PS_TIMEOUT + wait_ms: хватает на запуск и обход вкладок.
    """
    script = (_SELECT_PS.replace('@HWND@', str(int(hwnd))).replace('@WAIT@', str(int(wait_ms)))
              .replace('@KEY@', base64.b64encode(title.encode('utf-8')).decode('ascii')))
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    try:
        proc = subprocess.run(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
             '-EncodedCommand', encoded],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=_PS_TIMEOUT + wait_ms / 1000, stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000), check=False)
    except subprocess.TimeoutExpired:
        return 'none'
    out = proc.stdout.strip().splitlines()
    return out[-1].strip() if proc.returncode == 0 and out else 'none'


def focus(pid: int) -> int:
    """HWND окна терминала с уже выбранной вкладкой процесса pid; 0 - нельзя."""
    con = _Console(pid)
    con.attach()
    try:
        title = con.title()
        root, cls = con.root_window()
        if not root:
            return 0
        if cls == _CONHOST_CLASS:  # классическая консоль: вкладок нет, окно одно
            return root
        if cls != _WT_CLASS:
            return 0
        if _select_tab(root, title, 0) == 'selected':
            return root
        # заголовок общий (или успел смениться): уникальный на время выбора
        token = f'* claude-alert {os.getpid()}-{time.monotonic_ns() % 10**8}'
        con.set_title(token)
        try:
            return root if _select_tab(root, token, _TOKEN_WAIT_MS) == 'selected' else 0
        finally:
            con.set_title(title)
    finally:
        con.detach()


def _snapshot() -> dict[int, tuple[int, str]]:
    """PID -> (PID родителя, имя образа) всех процессов: Toolhelp32Snapshot."""
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                    ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
                    ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
                    ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', wintypes.LONG),
                    ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260)]

    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == wintypes.HANDLE(-1).value:
        return {}
    procs, entry = {}, Entry(dwSize=ctypes.sizeof(Entry))
    try:
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            procs[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile)
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return procs


def _birth(pid: int) -> int | None:
    """FILETIME создания живого процесса pid; нет процесса или он завершён - None."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        code, times = wintypes.DWORD(), [wintypes.FILETIME() for _ in range(4)]
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != _STILL_ACTIVE:
            return None
        if not k32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        return (times[0].dwHighDateTime << 32 | times[0].dwLowDateTime) or None
    finally:
        k32.CloseHandle(handle)


def _ancestor(procs: dict[int, tuple[int, str]], birth, pid: int) -> tuple[int, int] | None:
    """Ближайший живой claude.exe среди предков pid: (PID, FILETIME создания) или None.

    Не дальше _MAX_DEPTH предков; цикл, отсутствующий или мёртвый предок и PID,
    занятый процессом новее потомка (родитель умер, номер достался другому), - None.
    """
    seen, child = {pid}, birth(pid)
    for _ in range(_MAX_DEPTH):
        pid = procs.get(pid, (0, ''))[0]
        if not pid or pid in seen or pid not in procs:
            return None
        born = birth(pid)
        if not born or (child and born > child):
            return None
        if procs[pid][1].lower() == _HOST_IMAGE:
            return pid, born
        seen.add(pid)
        child = born
    return None


def find_host() -> tuple[int, int] | None:
    """(PID, FILETIME создания) claude.exe, запустившего этот процесс; не Windows или не найден - None."""
    if sys.platform != 'win32':
        return None
    try:
        return _ancestor(_snapshot(), _birth, os.getpid())
    except Exception:  # noqa: BLE001 - без цели окно покажется, клик просто закроет его
        return None


def main(argv: list[str]) -> int:
    try:
        if sys.platform != 'win32' or len(argv) != 4 or argv[0] != '--pid' or argv[2] != '--born':
            return 1
        pid, born = int(argv[1]), int(argv[3])
        # процесс исчез или его PID занят другим: чужое окно не поднимать
        if pid <= 0 or born <= 0 or _birth(pid) != born:
            return 1
        hwnd = focus(pid)
        if hwnd:
            print(hwnd)
            return 0
    except Exception:  # noqa: BLE001, S110 - при любой ошибке окно просто закроется
        pass
    return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
