"""
状态管理 —— 记录上次同步后各目录的文件清单，用于增量判断。
"""
import json
import os
from typing import Optional


STATE_DIR = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")), "SyncTool", "states"
)


def _safe_path(name: str) -> str:
    """将任务名转为安全的文件名。"""
    return "".join(c if c.isalnum() or c in "._- " else "_" for c in name).strip()


def state_path(name: str) -> str:
    return os.path.join(STATE_DIR, _safe_path(name) + ".json")


def load_state(name: str) -> dict:
    """加载任务状态。返回 {dir_path: {rel_path: mtime}}。"""
    path = state_path(name)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(name: str, state: dict) -> None:
    """保存任务状态。"""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(state_path(name), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def snapshot(path: str) -> dict:
    """拍快照：扫描目录，返回 {rel_path: mtime}。"""
    result = {}
    if not os.path.isdir(path):
        return result
    for root, _, files in os.walk(path):
        for fn in files:
            fp = os.path.join(root, fn)
            rel = os.path.relpath(fp, path).replace("\\", "/")
            try:
                result[rel] = os.path.getmtime(fp)
            except OSError:
                pass
    return result


def build_state(dirs: list[str]) -> dict:
    """为一组目录构建状态字典。"""
    return {d: snapshot(d) for d in dirs if os.path.isdir(d)}


def delete_state(name: str) -> None:
    """删除任务的状态文件。"""
    path = state_path(name)
    try:
        os.remove(path)
    except OSError:
        pass
