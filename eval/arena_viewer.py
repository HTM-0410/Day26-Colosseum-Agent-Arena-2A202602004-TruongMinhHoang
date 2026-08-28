"""Local match browser and spar runner for COLOSSEUM bot replays.

This viewer lives in ``eval/`` (student-owned) and serves ``kit/arena_ui/spar.html``
byte-for-byte. It adds an interactive match picker, filters, round history,
instant replay re-triggering, and a local match runner to test bots on-demand.
Standard library only; no file under ``kit/`` is modified.

Run from repository root::

    python -m eval.arena_viewer --port 8766
"""

from __future__ import annotations

import argparse
import contextlib
import html
import io
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs"
ARENA_HTML = ROOT / "kit" / "arena_ui" / "spar.html"
RUN_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


INDEX_HTML = """<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>COLOSSEUM — Match Viewer & Arena</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #070d17;
      --panel: #0d1626;
      --panel-sub: #132238;
      --line: #1e3555;
      --line-active: #38bdf8;
      --text: #e2edff;
      --muted: #8198bb;
      --you: #34d399;
      --bot: #fbbf24;
      --danger: #f87171;
      --win: #10b981;
      --loss: #ef4444;
      --draw: #eab308;
      --accent: #0ea5e9;
      --accent-hover: #38bdf8;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: radial-gradient(circle at top, #152542 0, #070d17 55%);
      color: var(--text);
      font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Consolas, monospace;
    }
    header {
      min-height: 64px;
      padding: 10px 20px;
      display: flex;
      gap: 14px;
      align-items: center;
      border-bottom: 1px solid var(--line);
      background: rgba(9, 17, 30, 0.95);
      backdrop-filter: blur(8px);
      position: sticky;
      top: 0;
      z-index: 20;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 17px;
      font-weight: 700;
      letter-spacing: .06em;
      white-space: nowrap;
    }
    .brand span { color: var(--accent-hover); }
    .controls {
      display: flex;
      flex: 1;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
    }
    select, button, input {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-sub);
      color: var(--text);
      padding: 8px 12px;
      font: inherit;
      transition: all .15s ease;
    }
    select {
      min-width: 320px;
      flex: 1;
      max-width: 520px;
      cursor: pointer;
    }
    select:focus, button:focus, input:focus {
      outline: none;
      border-color: var(--accent-hover);
      box-shadow: 0 0 0 2px rgba(56, 189, 248, 0.25);
    }
    button {
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-weight: 500;
    }
    button:hover {
      border-color: var(--accent-hover);
      background: #192d4a;
    }
    button.btn-primary {
      background: #0284c7;
      border-color: #38bdf8;
      color: #fff;
    }
    button.btn-primary:hover {
      background: #0369a1;
    }
    .status-pill {
      margin-left: auto;
      color: var(--muted);
      font-size: 13px;
      background: #0b1424;
      padding: 5px 10px;
      border-radius: 6px;
      border: 1px solid var(--line);
      white-space: nowrap;
    }
    main {
      display: grid;
      grid-template-columns: minmax(640px, 1fr) 380px;
      gap: 16px;
      padding: 16px;
    }
    .arena-wrap, .side {
      border: 1px solid var(--line);
      border-radius: 12px;
      overflow: hidden;
      background: var(--panel);
      box-shadow: 0 14px 45px rgba(0, 0, 0, 0.45);
    }
    .arena-wrap {
      display: flex;
      flex-direction: column;
    }
    iframe {
      display: block;
      width: 100%;
      aspect-ratio: 16/9;
      border: 0;
      background: #070d17;
      flex: 1;
    }
    .hint {
      padding: 10px 16px;
      color: var(--muted);
      border-top: 1px solid var(--line);
      font-size: 13px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      background: #091220;
    }
    .side {
      display: flex;
      flex-direction: column;
      height: calc(100vh - 96px);
      max-height: 850px;
    }
    .side-tabs {
      display: flex;
      background: #091322;
      border-bottom: 1px solid var(--line);
    }
    .tab-btn {
      flex: 1;
      padding: 12px 10px;
      background: transparent;
      border: 0;
      border-bottom: 2px solid transparent;
      border-radius: 0;
      color: var(--muted);
      font-weight: 600;
      font-size: 13px;
      text-align: center;
      cursor: pointer;
    }
    .tab-btn:hover { color: var(--text); background: rgba(255,255,255,0.03); }
    .tab-btn.active {
      color: var(--accent-hover);
      border-bottom-color: var(--accent-hover);
      background: rgba(14, 165, 233, 0.08);
    }
    .tab-pane {
      display: none;
      flex: 1;
      overflow-y: auto;
      padding: 14px;
    }
    .tab-pane.active { display: flex; flex-direction: column; gap: 12px; }
    .score {
      display: grid;
      grid-template-columns: 1fr auto 1fr;
      align-items: center;
      text-align: center;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: #0b1424;
      margin-bottom: 4px;
    }
    .score strong {
      display: block;
      font-size: 32px;
      font-weight: 700;
      line-height: 1.1;
      margin-top: 4px;
    }
    .score-label { font-size: 11px; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); }
    .score-you { color: var(--you); }
    .score-bot { color: var(--bot); }
    .versus-box {
      padding: 0 10px;
      text-align: center;
    }
    .winner-tag {
      font-size: 12px;
      font-weight: 700;
      padding: 3px 8px;
      border-radius: 4px;
      text-transform: uppercase;
      letter-spacing: .05em;
    }
    .winner-tag.win { background: rgba(16, 185, 129, 0.2); color: var(--win); border: 1px solid rgba(16, 185, 129, 0.4); }
    .winner-tag.loss { background: rgba(239, 68, 68, 0.2); color: var(--loss); border: 1px solid rgba(239, 68, 68, 0.4); }
    .winner-tag.draw { background: rgba(234, 179, 8, 0.2); color: var(--draw); border: 1px solid rgba(234, 179, 8, 0.4); }
    
    /* Filters */
    .filter-bar {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      margin-bottom: 4px;
    }
    .filter-chip {
      font-size: 11px;
      padding: 4px 9px;
      border-radius: 20px;
      background: #111d30;
      border: 1px solid var(--line);
      color: var(--muted);
      cursor: pointer;
      user-select: none;
    }
    .filter-chip:hover { color: var(--text); border-color: var(--muted); }
    .filter-chip.active {
      background: rgba(14, 165, 233, 0.2);
      border-color: var(--accent-hover);
      color: #fff;
      font-weight: 600;
    }

    /* Cards */
    .cards { display: grid; gap: 8px; }
    .run-card {
      padding: 10px 12px;
      border: 1px solid #1a2c47;
      border-radius: 8px;
      cursor: pointer;
      background: #0e1828;
      transition: all .15s ease;
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .run-card:hover {
      border-color: var(--accent-hover);
      background: #132238;
      transform: translateY(-1px);
    }
    .run-card.active {
      border-color: var(--accent-hover);
      background: #142540;
      box-shadow: 0 0 0 1px var(--accent-hover), 0 4px 14px rgba(0, 0, 0, 0.3);
    }
    .card-top {
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .card-title {
      font-weight: 600;
      font-size: 13px;
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .card-badge {
      font-size: 10px;
      padding: 2px 6px;
      border-radius: 4px;
      font-weight: 700;
    }
    .card-bottom {
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 12px;
      color: var(--muted);
    }
    .card-score {
      font-family: ui-monospace, monospace;
      font-weight: 700;
    }
    .card-score .you-num { color: var(--you); }
    .card-score .bot-num { color: var(--bot); }
    .card-meta { font-size: 11px; }

    /* Rounds table */
    .round-table {
      display: grid;
      gap: 4px;
      font-size: 13px;
    }
    .round-row {
      display: grid;
      grid-template-columns: 46px 1fr 1fr 1fr;
      gap: 6px;
      padding: 8px 10px;
      background: #0e1828;
      border-radius: 6px;
      align-items: center;
      border: 1px solid #17273d;
    }
    .round-row.header {
      background: #091322;
      color: var(--muted);
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
    }
    .round-row .damage { color: var(--danger); font-weight: 600; }
    .round-row .dealt { color: var(--you); font-weight: 600; }

    /* Modal */
    .modal-backdrop {
      position: fixed;
      inset: 0;
      background: rgba(0, 0, 0, 0.7);
      backdrop-filter: blur(4px);
      z-index: 50;
      display: none;
      align-items: center;
      justify-content: center;
    }
    .modal-backdrop.open { display: flex; }
    .modal {
      width: 440px;
      background: #0f1a2e;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 20px;
      box-shadow: 0 20px 50px rgba(0,0,0,0.6);
      display: flex;
      flex-direction: column;
      gap: 16px;
    }
    .modal h3 { margin: 0; font-size: 17px; display: flex; align-items: center; gap: 8px; }
    .form-group { display: flex; flex-direction: column; gap: 6px; }
    .form-group label { font-size: 12px; color: var(--muted); font-weight: 600; text-transform: uppercase; }
    .modal-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 8px; }
    .empty { color: var(--muted); text-align: center; padding: 28px 0; font-size: 13px; }

    @media (max-width: 1020px) {
      main { grid-template-columns: 1fr; }
      .side { height: auto; max-height: none; }
      header { flex-direction: column; align-items: stretch; }
      .status-pill { margin-left: 0; text-align: center; }
    }
  </style>
</head>
<body>
  <header>
    <div class="brand">⚔ <span>COLOSSEUM</span> MATCH ARENA</div>
    <div class="controls">
      <select id="runSelect" aria-label="Chọn trận đấu"></select>
      <button id="btnNewMatch" class="btn-primary" title="Tạo trận mới">⚔ Đấu trận mới</button>
      <button id="restart" title="Xem lại từ đầu">↻ Phát lại từ đầu</button>
      <button id="refresh" title="Làm mới danh sách">⟳ Làm mới</button>
    </div>
    <div class="status-pill" id="status">Đang tải…</div>
  </header>

  <main>
    <section class="arena-wrap">
      <iframe id="arena" title="COLOSSEUM Arena Replay"></iframe>
      <div class="hint">
        <span>🎮 <strong>Điều khiển:</strong> Trong khung đấu, dùng thanh timeline để Play/Pause, tua trận và chọn tốc độ 1× / 2× / 8×.</span>
        <span id="currentDuelLabel" style="color:var(--accent-hover);font-weight:600;">—</span>
      </div>
    </section>

    <aside class="side">
      <div class="side-tabs">
        <button class="tab-btn active" data-tab="runs">🎮 Danh sách trận (<span id="tabRunCount">0</span>)</button>
        <button class="tab-btn" data-tab="rounds">📋 Chi tiết lượt đấu</button>
      </div>

      <!-- Tab 1: Danh sách trận đấu -->
      <div class="tab-pane active" id="paneRuns">
        <div class="filter-bar" id="filterBar">
          <div class="filter-chip active" data-filter="all">Tất cả</div>
          <div class="filter-chip" data-filter="rookie">Rookie</div>
          <div class="filter-chip" data-filter="operator">Operator</div>
          <div class="filter-chip" data-filter="adversary">Adversary</div>
          <div class="filter-chip" data-filter="mirror">Mirror</div>
          <div class="filter-chip" data-filter="win">Thắng</div>
        </div>
        <div class="cards" id="cards"></div>
      </div>

      <!-- Tab 2: Chi tiết hiệp đấu của trận đang chọn -->
      <div class="tab-pane" id="paneRounds">
        <div class="score">
          <div>
            <span class="score-label">YOU</span>
            <strong class="score-you" id="youHp">—</strong>
          </div>
          <div class="versus-box">
            <span class="score-label" id="roundsTotal">VS</span>
            <div style="margin-top:4px;"><span class="winner-tag win" id="winnerTag">YOU WIN</span></div>
          </div>
          <div>
            <span class="score-label" id="botName">BOT</span>
            <strong class="score-bot" id="botHp">—</strong>
          </div>
        </div>
        <div class="round-table" id="roundsTable"></div>
      </div>
    </aside>
  </main>

  <!-- Modal Đấu trận mới -->
  <div class="modal-backdrop" id="modalBackdrop">
    <div class="modal">
      <h3>⚔ Bắt đầu trận đấu mới</h3>
      <div class="form-group">
        <label for="botSelect">Chọn đối thủ</label>
        <select id="botSelect">
          <option value="rookie">🤖 ROOKIE (Tập sự — Dễ)</option>
          <option value="operator" selected>⚡ OPERATOR (Tiêu chuẩn — Trung bình)</option>
          <option value="adversary">🔥 ADVERSARY (Khắc nghiệt — Khó)</option>
          <option value="mirror">🪞 MIRROR CHALLENGER (Tự đấu bản sao)</option>
        </select>
      </div>
      <div class="form-group">
        <label for="seedInput">Hạt giống ngẫu nhiên (Seed)</label>
        <input type="number" id="seedInput" value="1" min="1" max="9999">
      </div>
      <div class="form-group">
        <label for="roundsInput">Số hiệp đấu (Rounds)</label>
        <input type="number" id="roundsInput" value="10" min="1" max="30">
      </div>
      <div class="modal-actions">
        <button id="btnCancelModal">Hủy</button>
        <button id="btnStartMatch" class="btn-primary">⚔ Đấu ngay</button>
      </div>
    </div>
  </div>

  <script>
    const select = document.querySelector('#runSelect');
    const frame = document.querySelector('#arena');
    const status = document.querySelector('#status');
    const cardsBox = document.querySelector('#cards');
    const currentDuelLabel = document.querySelector('#currentDuelLabel');
    const tabRunCount = document.querySelector('#tabRunCount');
    let runs = [], current = null, currentFilter = 'all';

    // Switch Tabs
    document.querySelectorAll('.tab-btn').forEach(btn => {
      btn.onclick = () => {
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
        btn.classList.add('active');
        const tab = btn.dataset.tab;
        document.querySelector(tab === 'runs' ? '#paneRuns' : '#paneRounds').classList.add('active');
      };
    });

    // Filter Chips
    document.querySelectorAll('.filter-chip').forEach(chip => {
      chip.onclick = () => {
        document.querySelectorAll('.filter-chip').forEach(c => c.classList.remove('active'));
        chip.classList.add('active');
        currentFilter = chip.dataset.filter;
        renderCards();
      };
    });

    function getFilteredRuns() {
      if (currentFilter === 'all') return runs;
      if (currentFilter === 'win') return runs.filter(r => r.winner === 'YOU');
      if (currentFilter === 'mirror') return runs.filter(r => r.kind === 'mirror' || r.name.includes('mirror'));
      return runs.filter(r => r.bot.toLowerCase() === currentFilter);
    }

    function renderDropdown() {
      select.replaceChildren();
      const botGroup = document.createElement('optgroup');
      botGroup.label = '🤖 Đấu với Bot (Sparring)';
      const mirrorGroup = document.createElement('optgroup');
      mirrorGroup.label = '🪞 Thử thách Tự đấu (Mirror Challenge)';

      for (const run of runs) {
        const o = document.createElement('option');
        o.value = run.name;
        const icon = run.winner === 'YOU' ? '🟢' : (run.winner === 'DRAW' ? '🟡' : '🔴');
        const winWord = run.winner === 'YOU' ? 'THẮNG' : (run.winner === 'DRAW' ? 'HÒA' : 'THUA');
        o.textContent = `${icon} [${winWord}] ${run.opponent} · Seed ${run.seed} · HP ${run.hp_you}-${run.hp_bot}`;
        if (run.kind === 'mirror' || run.name.includes('mirror')) {
          mirrorGroup.append(o);
        } else {
          botGroup.append(o);
        }
      }
      if (botGroup.children.length) select.append(botGroup);
      if (mirrorGroup.children.length) select.append(mirrorGroup);
      if (current) select.value = current.name;
    }

    function renderCards() {
      cardsBox.replaceChildren();
      const filtered = getFilteredRuns();
      tabRunCount.textContent = runs.length;
      if (!filtered.length) {
        cardsBox.innerHTML = '<div class="empty">Không có trận nào phù hợp bộ lọc.</div>';
        return;
      }
      for (const run of filtered) {
        const el = document.createElement('div');
        el.className = 'run-card' + (run.name === current?.name ? ' active' : '');
        
        const isWin = run.winner === 'YOU';
        const isDraw = run.winner === 'DRAW';
        const badgeClass = isWin ? 'win' : (isDraw ? 'draw' : 'loss');
        const badgeText = isWin ? 'THẮNG' : (isDraw ? 'HÒA' : 'THUA');

        el.innerHTML = `
          <div class="card-top">
            <div class="card-title">
              <span>${run.kind === 'mirror' ? '🪞' : '🤖'}</span>
              <span>${run.opponent}</span>
              <span style="color:var(--muted);font-weight:normal;">#${run.seed}</span>
            </div>
            <span class="winner-tag ${badgeClass}">${badgeText}</span>
          </div>
          <div class="card-bottom">
            <div class="card-score">
              HP: <span class="you-num">${run.hp_you}</span> – <span class="bot-num">${run.hp_bot}</span>
            </div>
            <div class="card-meta">
              ${run.rounds}R · ${run.events} evts
            </div>
          </div>
        `;
        el.onclick = () => {
          loadRun(run.name);
        };
        cardsBox.append(el);
      }
    }

    function renderRounds(run) {
      const table = document.querySelector('#roundsTable');
      table.replaceChildren();
      const header = document.createElement('div');
      header.className = 'round-row header';
      header.innerHTML = '<span>Hiệp</span><span>Tỉ số HP</span><span>Nhận đòn</span><span>Gây đòn</span>';
      table.append(header);

      for (const row of run.summary) {
        const el = document.createElement('div');
        el.className = 'round-row';
        const tookCls = row.took > 0 ? 'damage' : '';
        const dealtCls = row.dealt > 0 ? 'dealt' : '';
        el.innerHTML = `
          <span style="font-weight:700;">R${row.round}</span>
          <span>HP ${row.hp_you}–${row.hp_bot}</span>
          <span class="${tookCls}">-${row.took || 0} HP</span>
          <span class="${dealtCls}">+${row.dealt || 0} HP</span>
        `;
        table.append(el);
      }
    }

    function loadRun(name) {
      const run = runs.find(x => x.name === name);
      if (!run) return;
      current = run;
      select.value = name;

      document.querySelector('#youHp').textContent = run.hp_you;
      document.querySelector('#botHp').textContent = run.hp_bot;
      document.querySelector('#botName').textContent = run.opponent;
      
      const tag = document.querySelector('#winnerTag');
      tag.className = 'winner-tag ' + (run.winner === 'YOU' ? 'win' : (run.winner === 'DRAW' ? 'draw' : 'loss'));
      tag.textContent = run.winner === 'YOU' ? 'YOU WIN' : (run.winner === 'DRAW' ? 'HÒA' : `${run.opponent} WIN`);
      document.querySelector('#roundsTotal').textContent = `${run.rounds} HIỆP`;

      currentDuelLabel.textContent = `Đang xem: ${run.opponent} (Seed ${run.seed}) — ${run.events} events`;

      renderRounds(run);
      renderCards();

      // Ensure cache-busting timestamp so iframe fully reloads even when same match is selected
      const replay = `/runs/${encodeURIComponent(run.name)}/events.jsonl`;
      const targetUrl = `/arena?replay=${encodeURIComponent(replay)}&you=A&_t=${Date.now()}`;
      
      try {
        if (frame.contentWindow && frame.src) {
          frame.contentWindow.location.replace(targetUrl);
        } else {
          frame.src = targetUrl;
        }
      } catch (e) {
        frame.src = targetUrl;
      }

      status.textContent = `${run.events} events · ${run.rounds} rounds`;
      history.replaceState(null, '', `/?run=${encodeURIComponent(run.name)}`);
    }

    async function loadRuns(preferred) {
      status.textContent = 'Đang tải danh sách…';
      try {
        const res = await fetch('/api/runs', { cache: 'no-store' });
        runs = await res.json();
        renderDropdown();
        renderCards();

        if (!runs.length) {
          status.textContent = 'Chưa có trận nào';
          return;
        }
        const wanted = preferred || new URLSearchParams(location.search).get('run');
        const pick = runs.some(x => x.name === wanted) ? wanted : runs[0].name;
        loadRun(pick);
      } catch (err) {
        status.textContent = `Lỗi: ${err.message}`;
      }
    }

    // Dropdown change
    select.onchange = () => loadRun(select.value);

    // Restart button
    document.querySelector('#restart').onclick = () => {
      if (current) loadRun(current.name);
    };

    // Refresh button
    document.querySelector('#refresh').onclick = () => {
      loadRuns(current?.name);
    };

    // Modal Create Match
    const modal = document.querySelector('#modalBackdrop');
    document.querySelector('#btnNewMatch').onclick = () => {
      // Suggest next seed
      const currentSeed = current?.seed || 1;
      document.querySelector('#seedInput').value = currentSeed + 1;
      modal.classList.add('open');
    };
    document.querySelector('#btnCancelModal').onclick = () => {
      modal.classList.remove('open');
    };
    modal.onclick = (e) => {
      if (e.target === modal) modal.classList.remove('open');
    };

    document.querySelector('#btnStartMatch').onclick = async () => {
      const bot = document.querySelector('#botSelect').value;
      const seed = parseInt(document.querySelector('#seedInput').value, 10) || 1;
      const rounds = parseInt(document.querySelector('#roundsInput').value, 10) || 10;
      
      const btn = document.querySelector('#btnStartMatch');
      btn.disabled = true;
      btn.textContent = 'Đang đấu…';
      status.textContent = `Đang mô phỏng trận đấu với ${bot.toUpperCase()} (seed ${seed})…`;

      try {
        const res = await fetch('/api/run_match', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ bot, seed, rounds })
        });
        const data = await res.json();
        if (!data.ok) throw new Error(data.error || 'Lỗi chạy trận đấu');
        modal.classList.remove('open');
        await loadRuns(data.run_name);
      } catch (err) {
        alert('Lỗi: ' + err.message);
        status.textContent = 'Lỗi tạo trận';
      } finally {
        btn.disabled = false;
        btn.textContent = '⚔ Đấu ngay';
      }
    };

    loadRuns();
  </script>
</body>
</html>
"""


def _safe_run(name: str) -> Path | None:
    if not RUN_RE.fullmatch(name):
        return None
    path = (RUNS / name).resolve()
    try:
        path.relative_to(RUNS.resolve())
    except ValueError:
        return None
    return path if path.is_dir() else None


def _run_info(path: Path) -> dict:
    summary_path = path / "summary.json"
    events_path = path / "events.jsonl"
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        summary = []
    final = summary[-1] if summary else {}
    match = re.fullmatch(r"spar-(rookie|operator|adversary)-(\d+)", path.name)
    metadata: dict = {}
    try:
        metadata = json.loads((path / "match.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    bot = match.group(1) if match else str(metadata.get("kind") or "challenge")
    seed = int(match.group(2)) if match else int(metadata.get("seed", 0) or 0)
    opponent = str(metadata.get("opponent") or bot.upper())
    hp_you = int(final.get("hp_you", 100))
    hp_bot = int(final.get("hp_bot", 100))
    try:
        events = sum(1 for line in events_path.open(encoding="utf-8") if line.strip())
    except OSError:
        events = 0
    return {
        "name": path.name,
        "bot": bot,
        "opponent": opponent,
        "kind": str(metadata.get("kind") or "bot"),
        "seed": seed,
        "rounds": len(summary),
        "events": events,
        "hp_you": hp_you,
        "hp_bot": hp_bot,
        "winner": "YOU" if hp_you > hp_bot else (opponent if hp_bot > hp_you else "DRAW"),
        "summary": summary,
        "updated": path.stat().st_mtime,
    }


def list_runs() -> list[dict]:
    if not RUNS.is_dir():
        return []
    rows = [_run_info(path) for path in RUNS.iterdir() if path.is_dir()]
    return sorted(rows, key=lambda row: (0 if row["kind"] == "mirror" else 1, row["opponent"], row["seed"], -row["updated"]))


def execute_match(bot: str, seed: int = 1, rounds: int = 10) -> str:
    """Run a spar match or mirror challenge match synchronously."""
    if bot == "mirror":
        from eval import challenge_match
        run_name = f"challenge-mirror-{seed}"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            challenge_match.generate(seed=seed, rounds=rounds, run_name=run_name)
        return run_name

    import spar
    run_name = f"spar-{bot}-{seed}"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        spar.main(["--bot", bot, "--seed", str(seed), "--rounds", str(rounds), "--ui", "--quiet"])
    return run_name


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: bytes, content_type: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: object, code: int = 200) -> None:
        self._send(json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", code)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            self._send(INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path in {"/arena", "/arena.html"}:
            if not ARENA_HTML.is_file():
                self._json({"error": "arena UI is not built"}, 404)
                return
            self._send(ARENA_HTML.read_bytes(), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/runs":
            self._json(list_runs())
            return
        if parsed.path == "/favicon.ico":
            self._send(b"", "image/x-icon", 204)
            return
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if len(parts) == 3 and parts[0] == "runs" and parts[2] == "events.jsonl":
            run = _safe_run(parts[1])
            event_file = run / "events.jsonl" if run else None
            if event_file is None or not event_file.is_file():
                self._json({"error": "run not found"}, 404)
                return
            self._send(event_file.read_bytes(), "application/x-ndjson; charset=utf-8")
            return
        self._json({"error": "not found", "path": html.escape(parsed.path)}, 404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/run_match":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length > 0 else b"{}"
                data = json.loads(body.decode("utf-8"))
                bot = str(data.get("bot", "rookie")).lower()
                seed = int(data.get("seed", 1))
                rounds = int(data.get("rounds", 10))
                if bot not in {"rookie", "operator", "adversary", "mirror"}:
                    self._json({"ok": False, "error": f"Invalid bot: {bot}"}, 400)
                    return
                run_name = execute_match(bot, seed, rounds)
                self._json({"ok": True, "run_name": run_name})
                return
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 500)
                return

        self._json({"error": "not found"}, 404)

    def log_message(self, *_args: object) -> None:
        return


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        try:
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            if hasattr(sys.stderr, "reconfigure"):
                sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"viewer: http://localhost:{args.port}/")
    print(f"runs: {RUNS}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
