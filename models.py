"""
数据模型层 —— 任务配置的 CRUD 与 JSON 持久化。
每个任务一个独立 JSON，保存在 %APPDATA%/SyncTool/tasks/ 下。
"""
import json
import os
from dataclasses import dataclass, field, asdict
from typing import Optional


APP_NAME = "SyncTool"
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
TASKS_DIR = os.path.join(CONFIG_DIR, "tasks")
ORDER_FILE = os.path.join(TASKS_DIR, "_order.json")
LEGACY_FILE = os.path.join(CONFIG_DIR, "tasks.json")


def _safe_name(name: str) -> str:
    """将任务名转为安全的文件名（不含 .json 后缀）。"""
    return "".join(c if c.isalnum() or c in "._- " else "_" for c in name).strip()


@dataclass
class TaskConfig:
    name: str
    source: str = ""                   # 源路径（仅单向镜像使用）
    source_type: str = "folder"        # "file" | "folder"
    targets: list = field(default_factory=list)  # 同步池路径列表
    direction: str = "mirror"          # "mirror" | "bidirectional"
    interval: int = 0                  # 整数秒，0 表示仅手动
    enabled: bool = True

    @property
    def interval_ms(self) -> int:
        return self.interval * 1000


class TaskStore:
    """任务配置的持久化存储 —— 每任务一个独立 JSON"""

    def __init__(self, directory: Optional[str] = None):
        self._dir = directory or TASKS_DIR
        self._tasks: list[TaskConfig] = []
        os.makedirs(self._dir, exist_ok=True)

    # ── 路径 ──────────────────────────────────────────────

    @staticmethod
    def task_path(name: str, directory: Optional[str] = None) -> str:
        d = directory or TASKS_DIR
        return os.path.join(d, _safe_name(name) + ".json")

    def _path(self, name: str) -> str:
        return self.task_path(name, self._dir)

    # ── 加载 ──────────────────────────────────────────────

    def load(self) -> list[TaskConfig]:
        """加载所有任务配置。优先读 tasks/ 目录，空则尝试迁移旧 tasks.json。"""
        try:
            files = sorted(f for f in os.listdir(self._dir)
                          if f.endswith(".json") and f != "_order.json")
        except OSError:
            files = []

        if files:
            raw = []
            for fn in files:
                task = self._load_one(os.path.join(self._dir, fn))
                if task:
                    raw.append(task)
            # 按 _order.json 排序
            self._tasks = self._sort_by_order(raw)
            return self._tasks

        # 迁移：旧 tasks.json → 独立文件
        if os.path.exists(LEGACY_FILE):
            tasks = self._migrate()
            self._tasks = self._sort_by_order(tasks)
            return self._tasks

        return []

    def _load_one(self, path: str) -> Optional[TaskConfig]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return TaskConfig(**data)
        except (json.JSONDecodeError, TypeError, OSError):
            return None

    def _migrate(self) -> list[TaskConfig]:
        """从旧 tasks.json 迁移到 tasks/ 目录。"""
        try:
            with open(LEGACY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            tasks = [TaskConfig(**item) for item in data]
            self.save(tasks)
            bak = LEGACY_FILE + ".bak"
            try:
                os.rename(LEGACY_FILE, bak)
            except OSError:
                pass
            return tasks
        except (json.JSONDecodeError, TypeError):
            return []

    # ── 排序持久化 ─────────────────────────────────────

    @staticmethod
    def _sort_by_order(tasks: list[TaskConfig]) -> list[TaskConfig]:
        """按 _order.json 排序，不在列表中的放末尾。"""
        try:
            with open(ORDER_FILE, "r", encoding="utf-8") as f:
                order = json.load(f)
        except (OSError, json.JSONDecodeError):
            return list(tasks)
        index = {name: i for i, name in enumerate(order)}
        return sorted(tasks, key=lambda t: index.get(t.name, 9999))

    def _write_order(self, tasks: list[TaskConfig]) -> None:
        """将任务名顺序写入 _order.json。"""
        names = [t.name for t in tasks]
        with open(ORDER_FILE, "w", encoding="utf-8") as f:
            json.dump(names, f, ensure_ascii=False, indent=2)

    # ── 保存 ──────────────────────────────────────────────

    def save(self, tasks: list[TaskConfig]) -> None:
        """批量保存所有任务。"""
        for t in tasks:
            self._write(t)
        self._write_order(tasks)
        self._tasks = list(tasks)

    def save_one(self, task: TaskConfig) -> None:
        """保存单个任务。"""
        self._write(task)
        # 同步内存列表
        for i, t in enumerate(self._tasks):
            if t.name == task.name:
                self._tasks[i] = task
                return
        self._tasks.append(task)

    def _write(self, task: TaskConfig) -> None:
        data = asdict(task)
        with open(self._path(task.name), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ── 删除 ──────────────────────────────────────────────

    def delete(self, name: str) -> None:
        """删除指定任务的配置文件。"""
        try:
            os.remove(self._path(name))
        except OSError:
            pass
        self._tasks = [t for t in self._tasks if t.name != name]

    @property
    def tasks(self) -> list[TaskConfig]:
        return self._tasks
