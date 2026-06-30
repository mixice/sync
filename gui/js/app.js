/**
 * SyncTool JS 胶水层 — Eel 连接 Python。
 * module，通过 window 暴露 onclick 函数。
 */

// ── Eel 调用 ──
function _call(fn, ...args) {
    if (!window.eel || typeof eel[fn] !== 'function') {
        const err = new Error(`Eel API unavailable: ${fn}`);
        window.append_log?.(`[错误] ${err.message}`);
        return Promise.reject(err);
    }
    return new Promise((resolve, reject) => {
        try {
            eel[fn](...args)(resolve);
        } catch (err) {
            window.append_log?.(`[错误] 调用 ${fn} 失败: ${err.message || err}`);
            reject(err);
        }
    });
}

function _catch(action) {
    return err => {
        const message = err?.message || err || '未知错误';
        console.error(`${action} failed:`, err);
        uiNotify(`[错误] ${action}失败: ${message}`);
    };
}

function uiAlert(message) {
    if (window.Uigg?.alert) {
        window.Uigg.alert(message);
    } else {
        alert(message);
    }
}

function uiConfirm(message) {
    if (window.Uigg?.confirm) {
        return window.Uigg.confirm(message);
    }
    return Promise.resolve(confirm(message));
}

function uiNotify(message) {
    window.append_log?.(message);
    if (window.Uigg?.notify) {
        window.Uigg.notify(message, 'bottom', 3000);
    } else if (!window.append_log) {
        alert(message);
    }
}

// ── DOM 工具 ──
function $(s, c) { return (c || document).querySelector(s); }
function $$(s, c) { return [...(c || document).querySelectorAll(s)]; }
function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

function gv(el) { const a = el.querySelector(':scope > a'); return a ? a.textContent : ''; }
function sv(el, t) { const a = el.querySelector(':scope > a'); if (a) a.textContent = t; }

// ── 状态 ──
let _tasks = [], _selName = '', _syncing = false;
let _loadingTask = false, _saveTimer = 0, _saving = false, _pendingSave = false;

function goals() { return $$('li span', $('#target-list')).map(s => s.textContent); }
function renderGoals(arr) {
    $('#target-list').innerHTML = arr.map(p =>
        `<li><span>${esc(p)}</span><a class="ico ico-delete" onclick="removeGoal(this)"></a></li>`
    ).join('');
}
async function removeGoal(a) {
    const li = a.closest('li');
    const path = li?.querySelector('span')?.textContent || '';
    if (!li) return;
    if (await uiConfirm(`确定删除同步对象？<br>${esc(path)}`)) {
        li.remove();
        autoSave('删除同步对象');
        uiNotify('[删除] 已移除同步对象');
    }
}

// ── 任务列表 ──
function renderList() {
    const el = $('#task-list');
    el.innerHTML = '';
    _tasks.forEach(t => {
        const sel = t.name === _selName;
        const icon = (t.enabled && t.interval > 0) ? 'ico-circular-media-play' : 'ico-circular-media-pause';
        const dir = t.direction === 'mirror' ? 'ico-circular-direction-right' : 'ico-exchange';
        el.innerHTML +=
            `<li class="${sel ? 'active' : ''}" onclick="pickTask('${esc(t.name)}')">` +
            `<i class="ico ${dir}"></i><z class="ico ${icon}"></z><span>${esc(t.name)}</span>` +
            `<a class="ico ico-alone-top" onclick="event.stopPropagation();moveTask('${esc(t.name)}',-1)"></a>` +
            `<a class="ico ico-alone-bottom" onclick="event.stopPropagation();moveTask('${esc(t.name)}',1)"></a>` +
            `</li>`;
    });
}

function pickTask(name) {
    _selName = name;
    _loadingTask = true;
    _call('get_task', name).then(data => {
        if (!data) return;
        $('#task-name').value = data.name || '';
        sv($('#source-type'), data.source_type === 'file' ? '文件' : '文件夹');
        sv($('#sync-direction'), data.direction === 'mirror' ? '单向' : '双向');
        $('#source-path').value = data.source || '';
        document.getElementById('address').style.display = data.direction === 'mirror' ? '' : 'none';
        const cb = $('#auto-enabled'), ti = $('#auto-delay');
        if (data.enabled) { cb.classList.add('active'); ti.removeAttribute('disabled'); }
        else { cb.classList.remove('active'); ti.setAttribute('disabled', ''); }
        ti.value = data.interval || '2';
        renderGoals(data.targets || []);
        renderList();
    }).finally(() => { _loadingTask = false; });
}

function moveTask(name, delta) {
    _call('move_task', name, delta).then(r => {
        if (!r.ok) return;
        _call('load_task_list').then(tasks => { _tasks = tasks; renderList(); });
    }).catch(_catch('移动任务'));
}

// ── 按钮 ──
function onNewTask() {
    window.append_log('[点击] 新建任务');
    _call('new_task').then(r => {
        if (!r.ok) return;
        _call('load_task_list').then(tasks => {
            _tasks = tasks; _selName = r.name; renderList();
            $('#task-name').value = r.name;
            sv($('#source-type'), '文件夹');
            sv($('#sync-direction'), '单向');
            $('#source-path').value = '';
            document.getElementById('address').style.display = '';
            $('#auto-enabled').classList.remove('active');
            const ti = $('#auto-delay'); ti.setAttribute('disabled', ''); ti.value = '2';
            renderGoals([]);
        });
    }).catch(_catch('新建任务'));
}
async function onDeleteTask() {
    if (!_selName) return;
    if (!await uiConfirm(`确定删除任务？<br>${esc(_selName)}`)) return;
    _call('delete_task', _selName).then(() => {
        _call('load_task_list').then(tasks => {
            _tasks = tasks; _selName = _tasks[0]?.name || ''; renderList();
            if (_selName) pickTask(_selName);
            else renderGoals([]);
            uiNotify('[删除] 任务已删除');
        });
    }).catch(_catch('删除任务'));
}
function onBrowse() {
    const isFile = gv($('#source-type')) === '文件';
    window.append_log(`[点击] 浏览${isFile ? '文件' : '文件夹'}`);
    _call(isFile ? 'browse_file' : 'browse_folder')
        .then(p => {
            if (!p) return;
            $('#source-path').value = p;
            autoSave('选择同步源');
        })
        .catch(_catch('浏览路径'));
}
function onAddGoal() {
    const isFile = gv($('#source-type')) === '文件';
    window.append_log(`[点击] 添加${isFile ? '文件' : '文件夹'}`);
    _call(isFile ? 'browse_file' : 'browse_folder').then(p => {
        if (!p) return;
        const cur = goals();
        if (cur.includes(p)) return;
        cur.push(p); renderGoals(cur);
        autoSave('添加同步对象');
    }).catch(_catch('添加对象'));
}
function formData() {
    return {
        name: ($('#task-name').value || '').trim(),
        source_type: gv($('#source-type')) === '文件' ? 'file' : 'folder',
        direction: gv($('#sync-direction')) === '双向' ? 'bidirectional' : 'mirror',
        source: $('#source-path').value,
        targets: goals(),
        enabled: $('#auto-enabled').classList.contains('active'),
        interval: parseInt($('#auto-delay').value) || 2,
    };
}
function autoSave(reason = '配置变更', delay = 350) {
    if (_loadingTask || !_selName) return;
    clearTimeout(_saveTimer);
    _saveTimer = setTimeout(() => saveCurrentTask(reason), delay);
}

function saveCurrentTask(reason = '配置变更') {
    if (_saving) {
        _pendingSave = true;
        return;
    }
    const data = formData(); data._old_name = _selName;
    if (!data.name) return;
    _saving = true;
    _call('save_task', data).then(r => {
        if (r.ok) {
            _selName = r.name;
            _call('load_task_list').then(tasks => {
                _tasks = tasks;
                renderList();
            });
            window.append_log(`[保存] ${reason}`);
        }
        else uiAlert(r.error || '保存失败');
    }).catch(_catch('保存任务')).finally(() => {
        _saving = false;
        if (_pendingSave) {
            _pendingSave = false;
            autoSave('配置变更');
        }
    });
}
function onPreview() {
    window.append_log('[点击] 预览');
    _call('preview', formData()).catch(_catch('预览'));
}
function onSync() {
    if (_syncing) return;
    window.append_log('[点击] 同步');
    _call('run_sync', formData()).then(r => {
        if (r && r.ok === false) {
            window.sync_done();
            uiAlert(r.error || '同步失败');
        }
    }).catch(err => {
        window.sync_done();
        _catch('同步')(err);
    });
}

// ── 方向 & 自动 ──
$('#sync-direction').addEventListener('click', () => {
    setTimeout(() => {
        document.getElementById('address').style.display = gv($('#sync-direction')) === '单向' ? '' : 'none';
        autoSave('选择同步方向');
    }, 100);
});
$('#source-type').addEventListener('click', () => {
    setTimeout(() => autoSave('选择文件类型'), 100);
});
$('#auto-enabled').addEventListener('click', () => {
    const cb = $('#auto-enabled'), ti = $('#auto-delay');
    if (cb.classList.contains('active')) ti.removeAttribute('disabled');
    else ti.setAttribute('disabled', '');
    autoSave('切换自动同步');
});
$('#task-name').addEventListener('blur', () => autoSave('修改任务名称', 0));
$('#auto-delay').addEventListener('blur', () => autoSave('修改自动延迟', 0));
$('#auto-delay').addEventListener('keydown', e => { if (e.key === 'Enter') e.currentTarget.blur(); });
$('#task-name').addEventListener('keydown', e => { if (e.key === 'Enter') e.currentTarget.blur(); });

// ── Eel 回调 ──
window.append_log = msg => {
    const ta = $('#log-output'); ta.value += msg + '\n'; ta.scrollTop = ta.scrollHeight;
};
window.sync_start = () => {
    _syncing = true;
    const btn = $('#sync-button');
    btn.classList.add('disabled'); const s = btn.querySelector('span'); if (s) s.textContent = '同步中...';
    $('#preview-button').classList.add('disabled');
};
window.sync_done = () => {
    _syncing = false;
    const btn = $('#sync-button');
    btn.classList.remove('disabled'); const s = btn.querySelector('span'); if (s) s.textContent = '同步';
    $('#preview-button').classList.remove('disabled');
};

// ── 暴露给 onclick ──
if (window.eel && typeof eel.expose === 'function') {
    eel.expose(window.append_log, 'append_log');
    eel.expose(window.sync_start, 'sync_start');
    eel.expose(window.sync_done, 'sync_done');
}

Object.assign(window, { pickTask, moveTask, onNewTask, onDeleteTask, onBrowse, onAddGoal, onPreview, onSync, removeGoal });

// ── 启动 ──
_call('load_task_list').then(tasks => {
    _tasks = tasks; renderList();
    if (_tasks.length) { _selName = _tasks[0].name; pickTask(_selName); }
    else renderGoals([]);
}).catch(err => console.error('init error:', err));
