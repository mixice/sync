# SyncTool

简单好用的文件同步工具，支持单向镜像与双向同步，带自动监听与 GUI 界面。

## 功能

- **单向镜像** — 源目录/文件单向推送到一个或多个目标，目标多余文件自动清理
- **双向同步** — 多目录/文件之间互相同步，基于 mtime 状态锚点判断增删改，冲突时保留较新版本
- **自动监听** — 启用后监听文件变更，延迟指定秒数自动触发同步
- **预览模式** — 同步前查看变更清单，心中有数再动手
- **任务管理** — 任务独立存储、排序、重命名，支持文件/文件夹混合同步池

## 运行

```bash
# 安装依赖
pip install -r requirements.txt

# 启动（Python 开发模式）
python eel_app.py
```

启动后会自动打开 Chrome 窗口（不可用时回退到系统默认浏览器）。

## 打包

```bash
pyinstaller SyncTool.spec
# 产物在 dist/SyncTool.exe
```

## 技术栈

| 层 | 方案 |
|---|---|
| 后端 | Python 3, Eel (WebSocket 桥接) |
| 前端 | UIGG 框架 |
| 文件监听 | watchdog |
| 原生对话框 | tkinter |
| 打包 | PyInstaller |

## 数据存储

- 任务配置：`%APPDATA%/SyncTool/tasks/` 下每任务一个 JSON
- 双向状态：`%APPDATA%/SyncTool/states/` 下记录每次同步后的文件快照

## 结构

```
sync/
├── eel_app.py          # 入口，Eel 后端 + 暴露 API
├── engine.py           # 同步引擎（镜像/双向 + 预览）
├── models.py           # 任务配置持久化层
├── state.py            # 双向同步状态管理
├── gui/                # 前端界面
│   ├── index.html
│   ├── js/
│   │   ├── app.js      # 业务逻辑胶水层
│   │   └── uigg.js     # UIGG 组件库
│   └── styles/
│       ├── uigg.css    # UIGG 样式
│       └── styles.css  # 页面样式
├── requirements.txt
└── SyncTool.spec       # PyInstaller 打包配置
```
