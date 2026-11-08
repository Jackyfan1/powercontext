// ScholarLens Frontend Application

document.addEventListener('DOMContentLoaded', () => {
    initTabs();
    initDragDrop();
    initFileInput();
    initKeyBindings();
    loadDocuments();
    checkMemoryStatus();
    loadRecall();
});

function initTabs() {
    document.querySelectorAll('.tab').forEach(tab => {
        tab.addEventListener('click', () => {
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            tab.classList.add('active');
            document.getElementById(`tab-${tab.dataset.tab}`).classList.add('active');
            if (tab.dataset.tab === 'memory') loadMemory();
        });
    });
}

function initDragDrop() {
    const zone = document.getElementById('dropZone');
    zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('dragover'); });
    zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
    zone.addEventListener('drop', e => {
        e.preventDefault();
        zone.classList.remove('dragover');
        handleFiles(e.dataTransfer.files);
    });
}

function initFileInput() {
    document.getElementById('fileInput').addEventListener('change', e => {
        handleFiles(e.target.files);
    });
}

function initKeyBindings() {
    document.getElementById('searchInput').addEventListener('keydown', e => {
        if (e.key === 'Enter') doSearch();
    });
    document.getElementById('qaInput').addEventListener('keydown', e => {
        if (e.key === 'Enter') doQA();
    });
}

async function handleFiles(files) {
    if (!files.length) return;
    const progress = document.getElementById('uploadProgress');
    const fill = document.getElementById('progressFill');
    const text = document.getElementById('progressText');
    progress.style.display = 'block';

    const total = files.length;
    let done = 0;
    const results = [];

    for (const file of files) {
        text.textContent = `正在处理: ${file.name} (${done + 1}/${total})`;
        fill.style.width = `${(done / total) * 100}%`;

        const formData = new FormData();
        formData.append('file', file);

        try {
            const res = await fetch('/api/import', { method: 'POST', body: formData });
            const data = await res.json();
            results.push(data);
        } catch (err) {
            results.push({ filename: file.name, status: 'error', error: err.message });
        }
        done++;
        fill.style.width = `${(done / total) * 100}%`;
    }

    text.textContent = `完成！成功导入 ${results.filter(r => r.status === 'success').length}/${total} 个文件`;
    fill.style.width = '100%';

    setTimeout(() => { progress.style.display = 'none'; fill.style.width = '0%'; }, 3000);
    loadDocuments();
}

async function loadDocuments() {
    const list = document.getElementById('documentList');
    try {
        const res = await fetch('/api/documents');
        const data = await res.json();
        if (!data.documents.length) {
            list.innerHTML = '<p class="hint">暂无已导入文献。请上传 PDF/Markdown/TXT 文件开始使用。</p>';
            return;
        }
        list.innerHTML = data.documents.map(doc => `
            <div class="doc-item">
                <span class="doc-name">${doc.filename}</span>
                <span class="doc-info">${doc.file_type.toUpperCase()} · ${doc.chunk_count} 段落 · ${new Date(doc.imported_at).toLocaleDateString('zh-CN')}</span>
            </div>
        `).join('');
    } catch (err) {
        list.innerHTML = `<p class="error">加载失败: ${err.message}</p>`;
    }
}

async function doSearch() {
    const input = document.getElementById('searchInput');
    const query = input.value.trim();
    if (!query) return;

    const btn = document.getElementById('searchBtn');
    const container = document.getElementById('searchResults');
    btn.disabled = true;
    btn.textContent = '检索中...';
    container.innerHTML = '<div class="loading">正在检索...</div>';

    try {
        const topK = parseInt(document.getElementById('topK').value);
        const res = await fetch('/api/search', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ query, top_k: topK }),
        });
        const data = await res.json();

        if (!data.results.length) {
            container.innerHTML = '<p class="hint">未找到相关段落。请先导入文献资料。</p>';
        } else {
            container.innerHTML = data.results.map((r, i) => `
                <div class="result-item">
                    <div class="result-header">
                        <span class="result-source">${r.source}</span>
                        <span class="result-score">相关度: ${(r.score * 100).toFixed(1)}%</span>
                    </div>
                    <div class="result-text">${escapeHtml(r.text)}</div>
                    <div class="result-meta">向量得分: ${r.vector_score.toFixed(3)} · 关键词得分: ${r.keyword_score.toFixed(3)}</div>
                </div>
            `).join('');
        }
    } catch (err) {
        container.innerHTML = `<p class="error">检索失败: ${err.message}</p>`;
    }

    btn.disabled = false;
    btn.textContent = '检索';
}

async function doQA() {
    const input = document.getElementById('qaInput');
    const query = input.value.trim();
    if (!query) return;

    const btn = document.getElementById('qaBtn');
    const container = document.getElementById('qaResult');
    btn.disabled = true;
    btn.textContent = '思考中...';
    container.innerHTML = '<div class="loading">正在生成回答...</div>';

    try {
        const res = await fetch('/api/qa', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ query }),
        });
        const data = await res.json();

        const modeLabel = data.mode === 'llm' ? '<span class="qa-mode llm">LLM 生成</span>' :
                          data.mode === 'excerpt' ? '<span class="qa-mode excerpt">摘录模式</span>' : '';

        container.innerHTML = `
            <div class="qa-result">
                ${modeLabel}
                <div class="qa-answer">${escapeHtml(data.answer)}</div>
            </div>
        `;
    } catch (err) {
        container.innerHTML = `<p class="error">问答失败: ${err.message}</p>`;
    }

    btn.disabled = false;
    btn.textContent = '提问';
}

async function checkMemoryStatus() {
    const badge = document.getElementById('memory-status');
    try {
        const res = await fetch('/api/memory/status');
        const data = await res.json();
        if (data.available) {
            badge.textContent = '记忆: 已连接';
            badge.classList.remove('offline');
        } else {
            badge.textContent = '记忆: 未连接';
            badge.classList.add('offline');
        }
    } catch {
        badge.textContent = '记忆: 未连接';
        badge.classList.add('offline');
    }
}

async function loadRecall() {
    try {
        const res = await fetch('/api/memory/recall');
        const data = await res.json();
        if (data.summary && data.summary !== '暂无研究记忆') {
            const banner = document.getElementById('recall-banner');
            document.getElementById('recall-text').textContent = data.summary;
            banner.style.display = 'flex';
        }
    } catch {}
}

function dismissRecall() {
    document.getElementById('recall-banner').style.display = 'none';
}

async function loadMemory() {
    const container = document.getElementById('memoryContent');
    container.innerHTML = '<div class="loading">加载中...</div>';

    try {
        const res = await fetch('/api/memory/recall');
        const data = await res.json();

        let html = '';

        if (data.topics && data.topics.length) {
            html += '<div class="memory-section"><h3>研究主题</h3>';
            data.topics.forEach(t => {
                html += `<div class="memory-item">${escapeHtml(t.topic)}${t.description ? ' — ' + escapeHtml(t.description) : ''}</div>`;
            });
            html += '</div>';
        }

        if (data.recent_searches && data.recent_searches.length) {
            html += '<div class="memory-section"><h3>最近检索</h3>';
            data.recent_searches.forEach(s => {
                html += `<div class="memory-item">"${escapeHtml(s.query)}" — ${s.results_count} 条结果</div>`;
            });
            html += '</div>';
        }

        if (data.conclusions && data.conclusions.length) {
            html += '<div class="memory-section"><h3>已确认结论</h3>';
            data.conclusions.forEach(c => {
                html += `<div class="memory-item"><strong>${escapeHtml(c.topic)}</strong>: ${escapeHtml(c.conclusion)}</div>`;
            });
            html += '</div>';
        }

        if (!html) {
            html = '<p class="hint">暂无研究记忆。开始检索和问答后，系统将自动记录。</p>';
        }

        container.innerHTML = html;
    } catch (err) {
        container.innerHTML = `<p class="error">加载失败: ${err.message}</p>`;
    }
}

async function saveConclusion() {
    const topic = document.getElementById('conclusionTopic').value.trim();
    const conclusion = document.getElementById('conclusionText').value.trim();
    if (!topic || !conclusion) { alert('请填写主题和结论内容'); return; }

    try {
        await fetch('/api/memory/conclusion', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ topic, conclusion }),
        });
        document.getElementById('conclusionTopic').value = '';
        document.getElementById('conclusionText').value = '';
        alert('结论已保存');
        loadMemory();
    } catch (err) {
        alert('保存失败: ' + err.message);
    }
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}
