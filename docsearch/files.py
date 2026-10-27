"""Opening source files from the user interfaces."""
import os
import subprocess
import sys

# Only these file types are opened directly; everything else is only shown in the file manager, never executed.
OPENABLE = {"pdf", "docx", "doc", "xlsx", "xls", "odt", "ods", "pptx", "ppt", "rtf", "txt", "csv", "eml",
            "msg", "html", "htm", "jpg", "jpeg", "png", "tif", "tiff"}


def open_source(path):
    """Open a document with its default program, or reveal it in the file manager. Returns False if it is missing."""
    if not path or not os.path.exists(path):
        return False
    openable = os.path.splitext(path)[1].lower().lstrip(".") in OPENABLE
    if sys.platform == "win32":
        if openable:
            os.startfile(path)
        else:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path] if openable else ["open", "-R", path])
    else:
        subprocess.Popen(["xdg-open", path if openable else os.path.dirname(path)])
    return True
