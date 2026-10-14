"""Remove host-specific paths from public diagnostic metadata, not model code."""
import ntpath
from pathlib import Path
import re


_WINDOWS_PATH = re.compile(
    r"""(?i)[a-z]:[\\/](?:[^\\/\r\n"'<>:]+[\\/])*[^\\/\r\n"'<>:\s]+"""
)
_HOME_PATH = re.compile(r"""/(?:home|Users|workspace|workspaces|tmp|var/tmp)/[^\s:"'<>]+""")


def redact_text(text):
    text = _WINDOWS_PATH.sub(lambda m: "<host-path>/" + ntpath.basename(m.group()), text)
    text = _HOME_PATH.sub(lambda m: "<host-path>/" + m.group().rsplit("/", 1)[-1], text)
    root = str(Path(__file__).resolve().parents[1])
    return text.replace(root, "<checkout>").replace(root.replace("\\", "/"), "<checkout>")


def public_metadata(value):
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [public_metadata(item) for item in value]
    if isinstance(value, dict):
        return {key: public_metadata(item) for key, item in value.items()}
    return value
