"""Disposable parser/OCR process. Parent kills it on timeout or cancellation."""
import json
import sys
import os
import threading
import time
from pathlib import Path
from zipfile import BadZipFile

from .checkpoints import atomic_json, save_extraction
from .ingest import extract
from .multimodal import collect


def watchdog(parent_pid, timeout):
    """An orphan parser must also terminate when its supervising server dies."""
    deadline = time.monotonic()+timeout
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32",use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE,wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100000,False,parent_pid)
        if not handle:
            os._exit(124)
        try:
            while time.monotonic()<deadline:
                # WAIT_TIMEOUT == 258; all other outcomes terminate fail-closed.
                if kernel.WaitForSingleObject(handle,250) != 258:
                    os._exit(124)
        finally:
            kernel.CloseHandle(handle)
    else:
        while time.monotonic()<deadline:
            if os.getppid()!=parent_pid:
                os._exit(124)
            time.sleep(.25)
    os._exit(124)


def main():
    folder = Path(sys.argv[1])
    request = json.loads((folder/"request.json").read_text(encoding="utf8"))
    threading.Thread(target=watchdog,args=(request.get("parent_pid",os.getppid()),request.get("timeout",300)),daemon=True).start()
    try:
        path = Path(request["path"])
        result = collect(path,request["suffix"],request["max_images"]) if request["multimodal"] else extract(path,request["suffix"])
        save_extraction(folder,result,request["fingerprint"])
    except Exception as exc:
        message = str(exc)[:600] if isinstance(exc,(ValueError,BadZipFile)) else "文件解析失败，请核对格式或查看原文件"
        atomic_json(folder/"parse-error.json",{"message":message})
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
