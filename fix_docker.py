"""Windows: remove the stale socket files that stop Docker Desktop from starting.

Docker Desktop sometimes leaves empty Unix-socket placeholders behind when it is not shut down cleanly. At the next
start it cannot remove them ("The file cannot be accessed by the system") and quits with "An unexpected error
occurred", so `docker compose up` fails with "failed to connect to the docker API". `del` and PowerShell cannot
remove these files (error 1920), Python can.

Run it while Docker Desktop is NOT running (double-click fix_docker.bat, then start Docker Desktop). It only touches empty socket
placeholders in Docker's own folders under %LOCALAPPDATA%; images, containers and volumes (your index) are not touched.
"""
import os
import stat
import subprocess
import sys

FOLDERS = ("Docker\\run", "docker-secrets-engine")


def docker_running():
    out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True).stdout.lower()
    return "docker desktop.exe" in out or "com.docker.backend.exe" in out


def stale_sockets():
    base = os.environ.get("LOCALAPPDATA", "")
    for folder in FOLDERS:
        path = os.path.join(base, folder)
        if not os.path.isdir(path):
            continue
        for name in os.listdir(path):
            full = os.path.join(path, name)
            info = os.lstat(full)
            if info.st_size == 0 and getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                yield full


def main():
    if os.name != "nt":
        print("This is only needed on Windows.")
        return 0
    if docker_running():
        print("Docker Desktop is running. Quit it first (tray icon -> Quit), then run this again.")
        return 1
    removed = 0
    for path in stale_sockets():
        try:
            os.remove(path)
            removed += 1
            print("removed", path)
        except OSError as e:
            print(f"could not remove {path}: {e}")
    print(f"Done, {removed} file(s) removed. Now start Docker Desktop.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
