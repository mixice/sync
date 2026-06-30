"""
同步引擎 —— 基于状态文件判断增删改，单向镜像 + 双向同步。
"""
import errno
import os
import shutil
import time
from typing import Callable, Optional


LogFn = Callable[[str], None]
LockedFn = Callable[[str], bool]


def _is_locked_error(e: OSError) -> bool:
    if isinstance(e, PermissionError):
        return True
    if hasattr(e, "winerror") and e.winerror in (32, 33):
        return True
    if getattr(e, "errno", None) in (errno.EACCES, errno.EAGAIN):
        return True
    return False


def _snapshot(path: str) -> dict:
    """扫描目录，返回 {rel_path: mtime}，跳过 . 开头的文件。"""
    result = {}
    if not os.path.isdir(path):
        return result
    for root, dirs, files in os.walk(path):
        # 跳过 . 开头的目录
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fn in files:
            if fn.startswith("."):
                continue
            fp = os.path.join(root, fn)
            rel = os.path.relpath(fp, path).replace("\\", "/")
            try:
                result[rel] = os.path.getmtime(fp)
            except OSError:
                pass
    return result


def _counterpart(rel: str, from_dir: str, to_dir: str) -> str:
    return os.path.join(to_dir, rel.replace("/", os.sep))


class SyncEngine:
    def __init__(self, log: Optional[LogFn] = None,
                 on_locked: Optional[LockedFn] = None):
        self.log = log or (lambda _: None)
        self.on_locked = on_locked
        self._stats = {"copied": 0, "skipped": 0, "deleted": 0, "errors": 0}

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    def _reset_stats(self):
        self._stats = {"copied": 0, "skipped": 0, "deleted": 0, "errors": 0}

    # ──────────────────────────── public API ────────────────────────────

    def run(self, source: str, pool: list[str], direction: str,
            state: dict | None = None) -> bool:
        """
        执行一次同步。
        state: 上次同步后的文件清单 {dir_path: {rel: mtime}}，双向同步时用于判断增删。
        """
        self._reset_stats()
        ok = True

        if direction == "mirror":
            if not os.path.exists(source):
                self.log(f"[错误] 源路径不存在: {source}")
                self._stats["errors"] += 1
                return False
            for target in pool:
                try:
                    self._mirror(source, target)
                except Exception as e:
                    self.log(f"[异常] {e}")
                    self._stats["errors"] += 1
                    ok = False
        else:
            state = state or {}
            for i in range(len(pool)):
                for j in range(i + 1, len(pool)):
                    try:
                        self._sync_pair(pool[i], pool[j], state)
                    except Exception as e:
                        self.log(f"[异常] {e}")
                        self._stats["errors"] += 1
                        ok = False
        return ok

    def preview(self, source: str, pool: list[str], direction: str,
                state: dict | None = None) -> list[str]:
        ops = []
        if direction == "mirror":
            if not os.path.exists(source):
                ops.append(f"[错误] 源不存在: {source}")
                return ops
            for target in pool:
                self._collect_mirror(source, target, ops)
        else:
            state = state or {}
            for i in range(len(pool)):
                for j in range(i + 1, len(pool)):
                    self._collect_pair(pool[i], pool[j], state, ops)
        return ops

    # ───────────────────────── 单向镜像 ─────────────────────────

    def _mirror(self, src: str, dst: str):
        self.log(f"[镜像] {src} → {dst}")
        self._propagate(src, dst)
        self._prune_extra(src, dst)

    # ───────────────────────── 文件双向 ─────────────────────────

    def _sync_file_pair(self, a: str, b: str, state: dict):
        """基于状态锚点同步两个文件。"""
        snap_a = state.get(a, {}).get("", 0)
        snap_b = state.get(b, {}).get("", 0)
        exists_a = os.path.exists(a)
        exists_b = os.path.exists(b)
        mtime_a = os.path.getmtime(a) if exists_a else 0
        mtime_b = os.path.getmtime(b) if exists_b else 0

        changed_a = exists_a and mtime_a != snap_a
        changed_b = exists_b and mtime_b != snap_b
        deleted_a = not exists_a and snap_a > 0
        deleted_b = not exists_b and snap_b > 0

        if deleted_a:
            if exists_b and not changed_b:
                self._do_delete(b, source=a)
            elif changed_b:
                self._file_copy(b, a, show_flow=True)
        elif deleted_b:
            if exists_a and not changed_a:
                self._do_delete(a, source=b)
            elif changed_a:
                self._file_copy(a, b, show_flow=True)
        elif changed_a and changed_b:
            # 两边都改了 → 保留较新
            if mtime_a > mtime_b:
                self._file_copy(a, b, show_flow=True)
            elif mtime_b > mtime_a:
                self._file_copy(b, a, show_flow=True)
            else:
                self._stats["skipped"] += 1
        elif changed_a:
            self._file_copy(a, b, show_flow=True)
        elif changed_b:
            self._file_copy(b, a, show_flow=True)
        elif not exists_a and not exists_b:
            pass  # 两边都没了
        elif not exists_a:
            self._file_copy(b, a, show_flow=True)
        elif not exists_b:
            self._file_copy(a, b, show_flow=True)
        else:
            self._stats["skipped"] += 1

    # ───────────────────────── 双向同步 ─────────────────────────

    def _sync_pair(self, a: str, b: str, state: dict):
        """基于状态判断增删，同步一对路径（目录或文件）。"""
        self.log(f"[双向] {a} ↔ {b}")

        # 文件模式：也用状态锚点判断
        if not os.path.isdir(a) or not os.path.isdir(b):
            self._sync_file_pair(a, b, state)
            return

        snap_a = state.get(a, {})
        snap_b = state.get(b, {})
        cur_a = _snapshot(a)
        cur_b = _snapshot(b)

        # ── A 侧变化 ──
        for rel, a_mtime in cur_a.items():
            a_path = os.path.join(a, rel.replace("/", os.sep))
            b_path = _counterpart(rel, a, b)
            if rel not in snap_a:
                # A 新增 → 推到 B
                if rel in cur_b:
                    if a_mtime > cur_b[rel]:
                        self._file_copy(a_path, b_path, show_flow=True)
                    elif a_mtime < cur_b[rel]:
                        self.log(f"[跳过] B 较新: {b_path} > {a_path}")
                        self._stats["skipped"] += 1
                    else:
                        self._stats["skipped"] += 1
                else:
                    self._file_copy(a_path, b_path, show_flow=True)
            elif snap_a[rel] != a_mtime:
                # A 修改 → 推到 B（若 A 更新）
                b_mtime = cur_b.get(rel, 0)
                if a_mtime > b_mtime:
                    self._file_copy(a_path, b_path, show_flow=True)
                else:
                    self._stats["skipped"] += 1
            # else: 未变化，跳过

        for rel in snap_a:
            if rel not in cur_a:
                # A 删除 → B 也删（如果 B 没被修改）
                b_path = _counterpart(rel, a, b)
                if rel in cur_b:
                    b_snap_mtime = snap_b.get(rel, 0)
                    b_cur_mtime = cur_b[rel]
                    if b_cur_mtime == b_snap_mtime:
                        # B 没被改过 → 删
                        a_path = os.path.join(a, rel.replace("/", os.sep))
                        self._do_delete(b_path, source=a_path)
                    else:
                        # B 被改过了 → 保留 B 的版本，推到 A
                        self.log(f"[保留] B 修改版: {rel}")
                        a_path = os.path.join(a, rel.replace("/", os.sep))
                        self._file_copy(b_path, a_path, show_flow=True)

        # ── B 侧变化 ──
        for rel, b_mtime in cur_b.items():
            b_path = os.path.join(b, rel.replace("/", os.sep))
            a_path = _counterpart(rel, b, a)
            if rel not in snap_b:
                # B 新增
                if rel in cur_a:
                    if b_mtime > cur_a[rel]:
                        self._file_copy(b_path, a_path, show_flow=True)
                    elif b_mtime < cur_a[rel]:
                        self.log(f"[跳过] A 较新: {a_path} > {b_path}")
                        self._stats["skipped"] += 1
                    else:
                        self._stats["skipped"] += 1
                else:
                    self._file_copy(b_path, a_path, show_flow=True)
            elif snap_b[rel] != b_mtime:
                # B 修改
                a_mtime = cur_a.get(rel, 0)
                if b_mtime > a_mtime:
                    self._file_copy(b_path, a_path, show_flow=True)
                else:
                    self._stats["skipped"] += 1

        for rel in snap_b:
            if rel not in cur_b:
                # B 删除 → A 也删（如果 A 没被修改）
                a_path = _counterpart(rel, b, a)
                if rel in cur_a:
                    a_snap_mtime = snap_a.get(rel, 0)
                    a_cur_mtime = cur_a[rel]
                    if a_cur_mtime == a_snap_mtime:
                        b_path = os.path.join(b, rel.replace("/", os.sep))
                        self._do_delete(a_path, source=b_path)
                    else:
                        self.log(f"[保留] A 修改版: {rel}")
                        b_path = os.path.join(b, rel.replace("/", os.sep))
                        self._file_copy(a_path, b_path, show_flow=True)

    # ───────────────────────── 核心操作 ─────────────────────────

    def _propagate(self, src: str, dst: str):
        if os.path.isfile(src):
            self._file_copy(src, _target_path(src, dst))
        elif os.path.isdir(src):
            self._propagate_dir(src, dst)

    def _propagate_dir(self, src_dir: str, dst_dir: str):
        os.makedirs(dst_dir, exist_ok=True)
        try:
            entries = os.listdir(src_dir)
        except OSError as e:
            self.log(f"[错误] 无法读取 {src_dir}: {e}")
            self._stats["errors"] += 1
            return
        for name in entries:
            if name.startswith("."):
                continue
            s = os.path.join(src_dir, name)
            d = os.path.join(dst_dir, name)
            try:
                if os.path.isdir(s):
                    self._propagate_dir(s, d)
                else:
                    self._file_copy(s, d)
            except Exception as e:
                self.log(f"[异常] {s}: {e}")
                self._stats["errors"] += 1

    def _file_copy(self, src: str, dst: str, show_flow: bool = False):
        """复制文件，冲突时按 mtime 保留较新。"""
        if not os.path.exists(dst):
            self._do_copy(src, dst, show_flow=show_flow)
            return
        try:
            if os.path.getmtime(src) > os.path.getmtime(dst):
                self._do_copy(src, dst, show_flow=show_flow)
            elif os.path.getmtime(src) < os.path.getmtime(dst):
                if show_flow:
                    self.log(f"[跳过] 目标较新: {dst} > {src}")
                else:
                    self.log(f"[跳过] 目标较新: {os.path.basename(dst)}")
                self._stats["skipped"] += 1
            else:
                self._stats["skipped"] += 1
        except OSError as e:
            self.log(f"[异常] {os.path.basename(dst)}: {e}")
            self._stats["errors"] += 1

    def _do_copy(self, src: str, dst: str, show_flow: bool = False):
        if self._retry_op(
            lambda: (os.makedirs(os.path.dirname(dst), exist_ok=True), shutil.copy2(src, dst)),
            dst,
        ):
            if show_flow:
                self.log(f"[复制] {src} > {dst}")
            else:
                self.log(f"[复制] {os.path.basename(src)}")
            self._stats["copied"] += 1
        else:
            if show_flow:
                self.log(f"[跳过] 占用: {src} > {dst}")
            else:
                self.log(f"[跳过] 占用: {os.path.basename(dst)}")
            self._stats["skipped"] += 1

    def _prune_extra(self, src: str, dst: str):
        if not os.path.isdir(src) or not os.path.isdir(dst):
            return
        try:
            entries = os.listdir(dst)
        except OSError:
            return
        for name in entries:
            if name.startswith("."):
                continue
            s = os.path.join(src, name)
            d = os.path.join(dst, name)
            if os.path.isfile(d) and not os.path.exists(s):
                self._do_delete(d)
            elif os.path.isdir(d):
                if not os.path.exists(s):
                    self._do_delete(d)
                elif os.path.isdir(s):
                    self._prune_extra(s, d)

    def _do_delete(self, path: str, source: str | None = None):
        try:
            if self._retry_op(
                lambda: (shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)),
                path,
            ):
                if source:
                    self.log(f"[删除] {source} > {path}")
                else:
                    self.log(f"[删除] {os.path.basename(path)}")
                self._stats["deleted"] += 1
            else:
                if source:
                    self.log(f"[跳过] 占用: {source} > {path}")
                else:
                    self.log(f"[跳过] 占用: {os.path.basename(path)}")
                self._stats["skipped"] += 1
        except OSError as e:
            self.log(f"[删除失败] {path}: {e}")
            self._stats["errors"] += 1

    def _retry_op(self, op, path: str, max_retries: int = 10) -> bool:
        for attempt in range(max_retries):
            try:
                op()
                return True
            except OSError as e:
                if not _is_locked_error(e):
                    raise
                if attempt == max_retries - 1:
                    raise
                self.log(f"[占用] {os.path.basename(path)}")
                if self.on_locked:
                    if self.on_locked(path):
                        time.sleep(0.5)
                        continue
                    else:
                        return False
                else:
                    time.sleep(1)
                    continue
        return False

    # ──────────────────── dry-run ────────────────────

    def _collect_mirror(self, src: str, dst: str, ops: list):
        try:
            self._collect_propagate(src, dst, ops)
            self._collect_prune(src, dst, ops)
        except Exception as e:
            ops.append(f"[异常] {e}")

    def _collect_pair(self, a: str, b: str, state: dict, ops: list):
        try:
            if not os.path.isdir(a) or not os.path.isdir(b):
                # 文件模式预览
                snap_a = state.get(a, {}).get("", 0)
                snap_b = state.get(b, {}).get("", 0)
                exists_a = os.path.exists(a)
                exists_b = os.path.exists(b)
                mtime_a = os.path.getmtime(a) if exists_a else 0
                mtime_b = os.path.getmtime(b) if exists_b else 0
                changed_a = exists_a and mtime_a != snap_a
                changed_b = exists_b and mtime_b != snap_b
                deleted_a = not exists_a and snap_a > 0
                deleted_b = not exists_b and snap_b > 0
                if deleted_a and exists_b:
                    ops.append(f"[将删除] {a} > {b}")
                elif deleted_b and exists_a:
                    ops.append(f"[将删除] {b} > {a}")
                elif changed_a:
                    ops.append(f"[将复制] {a} > {b}")
                elif changed_b:
                    ops.append(f"[将复制] {b} > {a}")
                elif not exists_a and exists_b:
                    ops.append(f"[将复制] {b} > {a}")
                elif not exists_b and exists_a:
                    ops.append(f"[将复制] {a} > {b}")
                return
            snap_a = state.get(a, {})
            snap_b = state.get(b, {})
            cur_a = _snapshot(a)
            cur_b = _snapshot(b)

            for rel in cur_a:
                a_path = os.path.join(a, rel.replace("/", os.sep))
                b_path = _counterpart(rel, a, b)
                if rel not in snap_a and rel not in cur_b:
                    ops.append(f"[将复制] {a_path} > {b_path}")
                elif rel not in snap_a:
                    ops.append(f"[将覆盖] {a_path} > {b_path}")
                elif snap_a.get(rel) != cur_a[rel] and cur_a[rel] > cur_b.get(rel, 0):
                    ops.append(f"[将覆盖] {a_path} > {b_path}")

            for rel in snap_a:
                if rel not in cur_a:
                    b_snap = snap_b.get(rel, 0)
                    b_cur = cur_b.get(rel, 0)
                    a_path = os.path.join(a, rel.replace("/", os.sep))
                    b_path = _counterpart(rel, a, b)
                    if rel in cur_b and b_cur == b_snap:
                        ops.append(f"[将删除] {a_path} > {b_path}")
                    elif rel in cur_b:
                        ops.append(f"[将复制] {b_path} > {a_path}")

            for rel in cur_b:
                b_path = os.path.join(b, rel.replace("/", os.sep))
                a_path = _counterpart(rel, b, a)
                if rel not in snap_b and rel not in cur_a:
                    ops.append(f"[将复制] {b_path} > {a_path}")
                elif rel not in snap_b:
                    ops.append(f"[将覆盖] {b_path} > {a_path}")
                elif snap_b.get(rel) != cur_b[rel] and cur_b[rel] > cur_a.get(rel, 0):
                    ops.append(f"[将覆盖] {b_path} > {a_path}")

            for rel in snap_b:
                if rel not in cur_b:
                    a_snap = snap_a.get(rel, 0)
                    a_cur = cur_a.get(rel, 0)
                    b_path = os.path.join(b, rel.replace("/", os.sep))
                    a_path = _counterpart(rel, b, a)
                    if rel in cur_a and a_cur == a_snap:
                        ops.append(f"[将删除] {b_path} > {a_path}")
                    elif rel in cur_a:
                        ops.append(f"[将复制] {a_path} > {b_path}")
        except Exception as e:
            ops.append(f"[异常] {e}")

    def _collect_propagate(self, src: str, dst: str, ops: list):
        if os.path.isfile(src):
            tgt = _target_path(src, dst)
            if not os.path.exists(tgt):
                ops.append(f"[将复制] {os.path.basename(src)} → {tgt}")
            elif os.path.getmtime(src) > os.path.getmtime(tgt):
                ops.append(f"[将覆盖] {os.path.basename(tgt)} (源较新)")
            else:
                ops.append(f"[将跳过] {os.path.basename(tgt)}")
        elif os.path.isdir(src):
            for root, dirs, files in os.walk(src):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                rel = os.path.relpath(root, src)
                dst_root = os.path.join(dst, rel) if rel != "." else dst
                for f in files:
                    if f.startswith("."):
                        continue
                    s = os.path.join(root, f)
                    d = os.path.join(dst_root, f)
                    if not os.path.exists(d):
                        ops.append(f"[将复制] {os.path.relpath(s, src)} → {dst}")
                    elif os.path.getmtime(s) > os.path.getmtime(d):
                        ops.append(f"[将覆盖] {os.path.relpath(d, dst)} (源较新)")

    def _collect_prune(self, src: str, dst: str, ops: list):
        if not os.path.isdir(src) or not os.path.isdir(dst):
            return
        for root, dirs, files in os.walk(dst):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            rel = os.path.relpath(root, dst)
            src_root = os.path.join(src, rel) if rel != "." else src
            for f in files:
                if f.startswith("."):
                    continue
                if not os.path.exists(os.path.join(src_root, f)):
                    ops.append(f"[将删除] {os.path.join(rel, f)}")


def _target_path(src: str, dst: str) -> str:
    if os.path.isdir(dst):
        return os.path.join(dst, os.path.basename(src))
    return dst
