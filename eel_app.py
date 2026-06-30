"""
SyncTool Eel 后端 —— HTML 界面 + Python 功能。
启动：python eel_app.py
"""
import json
import mimetypes
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog

import eel
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from engine import SyncEngine
from models import TaskConfig, TaskStore
from state import load_state, save_state, build_state, delete_state

# ═══════════ 全局状态 ═══════════
store = TaskStore()
_tasks: list[TaskConfig] = []
_current_name: str = ""
_sync_thread: threading.Thread | None = None
_sync_queue: list[TaskConfig] = []
_queue_lock = threading.Lock()
_tk_root: tk.Tk | None = None
_dialog_lock = threading.Lock()
_auto_thread: threading.Thread | None = None
_auto_observer: Observer | None = None
_auto_stop = threading.Event()
_auto_wake = threading.Event()
_auto_lock = threading.Lock()
_auto_next_run: dict[str, float] = {}
_auto_intervals: dict[str, int] = {}
_auto_tasks: dict[str, TaskConfig] = {}
_auto_suppress_until: dict[str, float] = {}


def _safe_log(msg: str) -> None:
    try:
        eel.append_log(msg)
    except Exception:
        pass


def _safe_sync_start(name: str) -> None:
    try:
        eel.sync_start(name)
    except Exception:
        pass


def _safe_sync_done() -> None:
    try:
        eel.sync_done()
    except Exception:
        pass


def _task_by_name(name: str) -> TaskConfig | None:
    return next((t for t in _tasks if t.name == name), None)


def _watch_paths(task: TaskConfig) -> list[str]:
    if task.direction == "mirror":
        candidates = [task.source]
    else:
        candidates = list(task.targets)
    paths = []
    for path in candidates:
        if not path:
            continue
        if os.path.isdir(path):
            watch_path = path
        elif os.path.isfile(path):
            watch_path = os.path.dirname(path)
        else:
            continue
        watch_path = os.path.normcase(os.path.abspath(watch_path))
        if watch_path not in paths:
            paths.append(watch_path)
    return paths


def _refresh_auto_schedule() -> None:
    """Keep the change-triggered auto-sync watcher in step with saved tasks."""
    active_tasks: dict[str, TaskConfig] = {}
    active_intervals: dict[str, int] = {}
    for task in _tasks:
        try:
            interval = int(task.interval or 0)
        except (TypeError, ValueError):
            interval = 0
        if task.enabled and interval > 0:
            active_tasks[task.name] = task
            active_intervals[task.name] = interval

    with _auto_lock:
        _auto_tasks.clear()
        _auto_tasks.update(active_tasks)
        _auto_intervals.clear()
        _auto_intervals.update(active_intervals)
        for name in list(_auto_next_run):
            if name not in active_tasks:
                _auto_next_run.pop(name, None)
        for name in list(_auto_suppress_until):
            if name not in active_tasks:
                _auto_suppress_until.pop(name, None)

    _rebuild_auto_observer()
    _auto_wake.set()


class _AutoEventHandler(FileSystemEventHandler):
    def on_any_event(self, event):
        if event.is_directory and event.event_type == "modified":
            return
        _mark_auto_changed(event.src_path)
        dest_path = getattr(event, "dest_path", "")
        if dest_path:
            _mark_auto_changed(dest_path)


def _path_touches_watch(candidate: str, watch_path: str) -> bool:
    if not candidate:
        return False
    try:
        path = os.path.normcase(os.path.abspath(candidate))
        return path == watch_path or path.startswith(watch_path + os.sep)
    except OSError:
        return False


def _mark_auto_changed(changed_path: str) -> None:
    now = time.monotonic()
    due: list[tuple[str, int]] = []
    with _auto_lock:
        for name, task in _auto_tasks.items():
            if _auto_suppress_until.get(name, 0) > now:
                continue
            if any(_path_touches_watch(changed_path, watch_path) for watch_path in _watch_paths(task)):
                interval = _auto_intervals.get(name, 1)
                _auto_next_run[name] = now + interval
                due.append((name, interval))
    if due:
        _auto_wake.set()


def _rebuild_auto_observer() -> None:
    global _auto_observer
    if _auto_observer is not None:
        try:
            _auto_observer.stop()
            _auto_observer.join(timeout=2)
        except Exception:
            pass
        _auto_observer = None

    watch_paths: list[str] = []
    with _auto_lock:
        tasks = list(_auto_tasks.values())
    for task in tasks:
        for path in _watch_paths(task):
            if path not in watch_paths:
                watch_paths.append(path)

    if not watch_paths or _auto_stop.is_set():
        return

    observer = Observer()
    handler = _AutoEventHandler()
    for path in watch_paths:
        try:
            observer.schedule(handler, path, recursive=True)
        except Exception as e:
            _safe_log(f"[自动] 监听失败: {path} ({e})")
    if observer.emitters:
        observer.daemon = True
        observer.start()
        _auto_observer = observer


def _start_auto_scheduler() -> None:
    global _auto_thread
    if _auto_thread and _auto_thread.is_alive():
        return
    _auto_stop.clear()
    _auto_thread = threading.Thread(target=_auto_scheduler_loop, daemon=True)
    _auto_thread.start()


def _stop_auto_scheduler() -> None:
    global _auto_observer
    _auto_stop.set()
    _auto_wake.set()
    if _auto_observer is not None:
        try:
            _auto_observer.stop()
            _auto_observer.join(timeout=2)
        except Exception:
            pass
        _auto_observer = None


def _auto_scheduler_loop() -> None:
    while not _auto_stop.is_set():
        due_names: list[str] = []
        wait_seconds = 1.0
        now = time.monotonic()

        with _auto_lock:
            if _auto_next_run:
                next_at = min(_auto_next_run.values())
                if next_at <= now:
                    for name, due_at in list(_auto_next_run.items()):
                        if due_at <= now:
                            _auto_next_run.pop(name, None)
                            due_names.append(name)
                    wait_seconds = 0.2
                else:
                    wait_seconds = max(0.2, min(1.0, next_at - now))

        for name in due_names:
            with _auto_lock:
                task = _auto_tasks.get(name)
            if not task or not task.enabled:
                continue
            try:
                if _validate(task):
                    with _auto_lock:
                        _auto_suppress_until[task.name] = time.monotonic() + max(2, _auto_intervals.get(task.name, 1))
                    _safe_log(f"[自动] 检测到变更，同步: {task.name}")
                    _run_sync(task)
            except Exception as e:
                _safe_log(f"[自动] 同步失败: {e}")

        _auto_wake.wait(wait_seconds)
        _auto_wake.clear()


# ═══════════ 回调钩子 ═══════════
def _on_log_hook(msg: str):
    _safe_log(msg)


def _on_locked_hook(path: str) -> bool:
    """文件占用提示：默认不重试。"""
    _safe_log(f"[占用] {path}")
    return False


engine = SyncEngine(log=_on_log_hook, on_locked=_on_locked_hook)


# ═══════════ 暴露给 JS 的 API ═══════════

@eel.expose
def load_task_list() -> list[dict]:
    """返回任务列表（名称 + 图标信息）。"""
    global _tasks
    _tasks = store.load()
    _refresh_auto_schedule()
    result = []
    for t in _tasks:
        result.append({
            "name": t.name,
            "direction": t.direction,
            "source_type": t.source_type,
            "enabled": t.enabled,
            "interval": t.interval,
        })
    return result


@eel.expose
def get_task(name: str) -> dict | None:
    """获取单个任务完整配置。"""
    task = next((t for t in _tasks if t.name == name), None)
    if not task:
        return None
    return {
        "name": task.name,
        "source": task.source,
        "source_type": task.source_type,
        "direction": task.direction,
        "enabled": task.enabled,
        "interval": task.interval,
        "targets": task.targets,
    }


@eel.expose
def save_task(data: dict) -> dict:
    """保存当前表单数据为任务配置。data 来自 JS。"""
    global _tasks, _current_name
    name = data.get("name", "").strip()
    if not name:
        return {"ok": False, "error": "任务名称不能为空"}
    old_name = data.get("_old_name", name)

    task = TaskConfig(
        name=name,
        source=data.get("source", "").strip(),
        source_type=data.get("source_type", "folder"),
        targets=data.get("targets", []),
        direction=data.get("direction", "mirror"),
        interval=max(1, int(data.get("interval", 2) or 2)),
        enabled=data.get("enabled", False),
    )

    # 重名检查
    names = [t.name for t in _tasks if t.name != old_name]
    if name in names:
        return {"ok": False, "error": f"任务名称 {name} 已存在"}

    store.save_one(task)
    if old_name != name:
        store.delete(old_name)
        delete_state(old_name)

    _current_name = name
    _tasks = store.load()
    _refresh_auto_schedule()
    return {"ok": True, "name": name}


@eel.expose
def new_task() -> dict:
    """创建新任务。"""
    global _tasks, _current_name
    names = {t.name for t in _tasks}
    base = "新任务"
    name = base
    i = 2
    while name in names:
        name = f"{base} ({i})"
        i += 1
    task = TaskConfig(name=name)
    _tasks.append(task)
    store.save_one(task)
    save_state(name, {})
    _current_name = name
    _refresh_auto_schedule()
    return {"ok": True, "name": name}


@eel.expose
def delete_task(name: str) -> dict:
    """删除任务。"""
    global _tasks
    store.delete(name)
    delete_state(name)
    _tasks = [t for t in _tasks if t.name != name]
    _refresh_auto_schedule()
    return {"ok": True}


@eel.expose
def move_task(name: str, delta: int) -> dict:
    """上下移动任务顺序。delta: -1 上移, 1 下移。"""
    global _tasks
    idx = next((i for i, t in enumerate(_tasks) if t.name == name), -1)
    if idx < 0:
        return {"ok": False}
    new_idx = idx + delta
    if new_idx < 0 or new_idx >= len(_tasks):
        return {"ok": False}
    _tasks[idx], _tasks[new_idx] = _tasks[new_idx], _tasks[idx]
    store.save(_tasks)
    return {"ok": True, "order": [t.name for t in _tasks]}


@eel.expose
def browse_folder() -> str:
    """打开文件夹选择对话框。"""
    return _tk_dialog(lambda root: filedialog.askdirectory(
        parent=root,
        title="选择文件夹",
        mustexist=True,
    ))


@eel.expose
def browse_file() -> str:
    """打开文件选择对话框。"""
    return _tk_dialog(lambda root: filedialog.askopenfilename(
        parent=root,
        title="选择文件",
        filetypes=[("所有文件", "*.*")],
    ))


def _tk_dialog(fn):
    """同步打开原生文件选择窗口并返回结果。"""
    with _dialog_lock:
        root = None
        try:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            root.update()
            path = fn(root) or ""
            return os.path.normpath(path) if path else ""
        except Exception as e:
            try:
                eel.append_log(f"[错误] 打开选择窗口失败: {e}")
            except Exception:
                pass
            return ""
        finally:
            if root is not None:
                try:
                    root.attributes("-topmost", False)
                    root.destroy()
                except Exception:
                    pass


def _ensure_tk_root():
    global _tk_root
    if _tk_root is None:
        _tk_root = tk.Tk()
        _tk_root.withdraw()
        _tk_root.attributes("-topmost", True)


# ═══════════ 同步 ═══════════

@eel.expose
def run_sync(data: dict) -> dict:
    """从表单数据构造任务并加入同步队列。"""
    task = TaskConfig(
        name=data.get("name", "_temp_").strip() or "_temp_",
        source=data.get("source", "").strip(),
        source_type=data.get("source_type", "folder"),
        targets=data.get("targets", []),
        direction=data.get("direction", "mirror"),
        interval=max(1, int(data.get("interval", 2) or 2)),
        enabled=data.get("enabled", False),
    )

    if _validate(task):
        _run_sync(task)
        return {"ok": True}
    return {"ok": False, "error": "验证失败"}


@eel.expose
def preview(data: dict):
    """预览变更。"""
    task = TaskConfig(
        name=data.get("name", "_temp_").strip() or "_temp_",
        source=data.get("source", "").strip(),
        source_type=data.get("source_type", "folder"),
        targets=data.get("targets", []),
        direction=data.get("direction", "mirror"),
        interval=max(1, int(data.get("interval", 2) or 2)),
        enabled=data.get("enabled", False),
    )
    if not _validate(task):
        return
    eel.append_log(f"── 预览 {task.name} ──")
    state = load_state(task.name) if task.direction == "bidirectional" else None
    ops = engine.preview(task.source, task.targets, task.direction, state=state)
    if not ops:
        eel.append_log("（无变更）")
    else:
        for op in ops:
            eel.append_log(op)
    eel.append_log("── 预览结束 ──\n")


def _validate(task: TaskConfig) -> bool:
    if not task.name.strip():
        eel.append_log("[错误] 请输入任务名称")
        return False
    if task.direction == "mirror":
        if not task.source:
            eel.append_log("[错误] 请选择同步源")
            return False
        if not os.path.exists(task.source):
            eel.append_log(f"[错误] 同步源不存在: {task.source}")
            return False
        for t in task.targets:
            if t == task.source:
                eel.append_log(f"[错误] 同步池不能包含源: {t}")
                return False
    if not task.targets:
        eel.append_log("[错误] 同步池不能为空")
        return False
    if task.direction == "bidirectional" and len(task.targets) < 2:
        eel.append_log("[错误] 双向至少需要 2 个目标")
        return False
    return True


def _run_sync(task: TaskConfig):
    global _sync_thread
    with _queue_lock:
        if task.name not in [queued.name for queued in _sync_queue]:
            _sync_queue.append(task)
        else:
            for i, queued in enumerate(_sync_queue):
                if queued.name == task.name:
                    _sync_queue[i] = task
                    break
        order = {t.name: i for i, t in enumerate(_tasks)}
        _sync_queue.sort(key=lambda queued: order.get(queued.name, 999))
        already_running = _sync_thread and _sync_thread.is_alive()
    if not already_running:
        _start_next_sync()


def _start_next_sync():
    global _sync_thread
    with _queue_lock:
        if not _sync_queue:
            _safe_sync_done()
            return
        task = _sync_queue.pop(0)
    _safe_sync_start(task.name)
    _safe_log(f"── 开始同步 {task.name} ──")
    _sync_thread = threading.Thread(target=_sync_worker, args=(task,), daemon=True)
    _sync_thread.start()
    threading.Thread(target=_wait_sync_done, args=(task,), daemon=True).start()


def _sync_worker(task: TaskConfig):
    try:
        state = load_state(task.name) if task.direction == "bidirectional" else None
        engine.run(task.source, task.targets, task.direction, state=state)
    except Exception as e:
        _safe_log(f"[严重异常] {e}")


def _wait_sync_done(task: TaskConfig):
    global _sync_thread
    try:
        thread = _sync_thread
        if thread:
            thread.join()
        _after_sync_done(task)
    finally:
        _start_next_sync()


def _after_sync_done(task: TaskConfig):
    with _auto_lock:
        if task.name in _auto_tasks:
            _auto_suppress_until[task.name] = time.monotonic() + max(2, _auto_intervals.get(task.name, 1))
    if task.direction == "bidirectional":
        dirs = list(task.targets)
        if task.source_type == "folder":
            try:
                save_state(task.name, build_state(dirs))
            except Exception as e:
                _safe_log(f"[警告] 更新状态失败: {e}")
        else:
            st = {}
            for fp in dirs:
                if os.path.exists(fp):
                    st[fp] = {"": os.path.getmtime(fp)}
                else:
                    st[fp] = {}
            save_state(task.name, st)
    stats = engine.stats
    _safe_log(
        f"── 完成: 复制 {stats['copied']}, 跳过 {stats['skipped']}, "
        f"删除 {stats['deleted']}, 错误 {stats['errors']} ──\n"
    )


# ═══════════ 启动 ═══════════

def main():
    global _tasks
    mimetypes.add_type('font/woff2', '.woff2')
    mimetypes.add_type('text/css', '.css')

    # PyInstaller 打包后 web 文件在 _MEIPASS/gui 下
    if getattr(sys, 'frozen', False):
        web_dir = os.path.join(sys._MEIPASS, 'gui')
    else:
        web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gui')

    eel.init(web_dir)
    _tasks = store.load()
    _refresh_auto_schedule()
    _start_auto_scheduler()
    try:
        eel.start("index.html", mode="chrome", size=(1200, 800),
                  port=0, block=True, cmdline_args=['--disable-extensions', '--disable-plugins'])
    except EnvironmentError:
        # Chrome 不可用时回退到默认浏览器
        eel.start("index.html", mode="default", size=(1200, 800),
                  port=0, block=True)
    finally:
        _stop_auto_scheduler()


if __name__ == "__main__":
    main()
