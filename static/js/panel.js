/**
 * 炙彗JI器人 - 官方控制面板前端互動邏輯 (Apple Liquid Glass Control Panel)
 */

(function () {
  'use strict';

  // 全域變數
  let currentGuildId = null;
  let currentGuildName = '';
  let currentChannels = [];
  let currentRoles = [];
  let currentCommands = [];

  // DOM 元素快取
  const toastContainer = document.getElementById('toast-container');
  const serverChips = document.querySelectorAll('.server-chip');
  const navTabs = document.querySelectorAll('.nav-tab');
  const tabPanes = document.querySelectorAll('.tab-pane');
  const botInviteBanner = document.getElementById('bot-invite-banner');
  const activeServerDisplay = document.getElementById('active-server-display');
  const activeServerIdDisplay = document.getElementById('active-server-id-display');
  const btnSwitchOwner = document.getElementById('btn-switch-owner');
  const viewServerSelection = document.getElementById('view-server-selection');
  const viewServerSettings = document.getElementById('view-server-settings');
  const btnBackToServers = document.getElementById('btn-back-to-servers');
  const btnSidebarBack = document.getElementById('btn-sidebar-back');
  const btnDropdownOwner = document.getElementById('btn-dropdown-owner');

  const modalConfirm = document.getElementById('modal-confirm-clear');
  const modalConfirmTitle = document.getElementById('modal-confirm-title');
  const modalConfirmMessage = document.getElementById('modal-confirm-message');
  const modalConfirmOk = document.getElementById('modal-confirm-ok');
  const modalConfirmCancel = document.getElementById('modal-confirm-cancel');
  const modalConfirmClose = document.getElementById('modal-confirm-close');

  // ---------- 清除設定確認 Modal (Confirm Modal) ----------
  let currentConfirmCallback = null;

  function closeConfirmModal() {
    if (modalConfirm) modalConfirm.style.display = 'none';
    currentConfirmCallback = null;
  }

  function showConfirmModal({ title, message, confirmText, cancelText, onConfirm }) {
    if (!modalConfirm) {
      if (window.confirm(message)) {
        onConfirm();
      }
      return;
    }
    if (modalConfirmTitle && title) modalConfirmTitle.textContent = title;
    if (modalConfirmMessage && message) modalConfirmMessage.textContent = message;
    if (modalConfirmOk && confirmText) {
      const span = modalConfirmOk.querySelector('span');
      if (span) span.textContent = confirmText;
    }
    if (modalConfirmCancel && cancelText) {
      const span = modalConfirmCancel.querySelector('span');
      if (span) span.textContent = cancelText;
    }
    currentConfirmCallback = onConfirm;
    modalConfirm.style.display = 'flex';
  }

  if (modalConfirmOk) {
    modalConfirmOk.addEventListener('click', () => {
      const cb = currentConfirmCallback;
      closeConfirmModal();
      if (typeof cb === 'function') {
        cb();
      }
    });
  }
  if (modalConfirmCancel) modalConfirmCancel.addEventListener('click', closeConfirmModal);
  if (modalConfirmClose) modalConfirmClose.addEventListener('click', closeConfirmModal);
  if (modalConfirm) {
    modalConfirm.addEventListener('click', (e) => {
      if (e.target === modalConfirm) closeConfirmModal();
    });
  }

  // ---------- Toast 提示通知函式 ----------
  function showToast(message, type = 'success') {
    if (!toastContainer) return;
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    const icon = type === 'success' ? '✅' : '❌';
    toast.innerHTML = `<span>${icon}</span><span>${message}</span>`;
    toastContainer.appendChild(toast);

    setTimeout(() => {
      toast.style.opacity = '0';
      toast.style.transform = 'translateY(-10px)';
      toast.style.transition = 'all 0.3s ease';
      setTimeout(() => toast.remove(), 300);
    }, 4000);
  }

  // ---------- 視圖切換與返回伺服器清單 ----------
  function returnToServerSelection() {
    if (viewServerSettings) viewServerSettings.style.display = 'none';
    if (viewServerSelection) viewServerSelection.style.display = 'block';
    currentGuildId = null;
    currentGuildName = '';
    serverChips.forEach((c) => c.classList.remove('active'));
    if (window.location.hash) {
      history.replaceState(null, '', window.location.pathname);
    }
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  if (btnBackToServers) {
    btnBackToServers.addEventListener('click', returnToServerSelection);
  }
  if (btnSidebarBack) {
    btnSidebarBack.addEventListener('click', returnToServerSelection);
  }

  function openSettingsViewForOwner() {
    if (viewServerSelection) viewServerSelection.style.display = 'none';
    if (viewServerSettings) viewServerSettings.style.display = 'block';
    if (botInviteBanner) botInviteBanner.style.display = 'none';

    if (activeServerDisplay) activeServerDisplay.textContent = '👑 炙彗JI器人 (管理員設定)';
    if (activeServerIdDisplay) activeServerIdDisplay.textContent = '系統管理專區';

    switchTab('tab-owner');
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  if (btnDropdownOwner) {
    btnDropdownOwner.addEventListener('click', () => {
      const userDropdown = document.getElementById('topbar-user-dropdown');
      const userBtn = document.getElementById('topbar-user-btn');
      if (userDropdown) userDropdown.classList.remove('is-open');
      if (userBtn) {
        userBtn.classList.remove('is-active');
        userBtn.setAttribute('aria-expanded', 'false');
      }
      openSettingsViewForOwner();
    });
  }

  // ---------- Tab 切換邏輯 ----------
  function switchTab(tabId) {
    navTabs.forEach((tab) => {
      if (tab.getAttribute('data-tab') === tabId) {
        tab.classList.add('active');
      } else {
        tab.classList.remove('active');
      }
    });

    tabPanes.forEach((pane) => {
      if (pane.id === tabId) {
        pane.classList.add('active');
      } else {
        pane.classList.remove('active');
      }
    });

    // 若切換到擁有者專區，自動載入擁有者設定資料
    if (tabId === 'tab-owner') {
      loadOwnerEconomyConfig();
      loadOwnerCogsList();
      loadOwnerApiQuota();
    }
  }

  navTabs.forEach((tab) => {
    tab.addEventListener('click', () => {
      const targetTab = tab.getAttribute('data-tab');
      switchTab(targetTab);
    });
  });

  if (btnSwitchOwner) {
    btnSwitchOwner.addEventListener('click', () => {
      openSettingsViewForOwner();
    });
  }

  // ---------- 伺服器切換邏輯 ----------
  function selectGuild(chip) {
    serverChips.forEach((c) => c.classList.remove('active'));
    chip.classList.add('active');

    currentGuildId = chip.getAttribute('data-guild-id');
    currentGuildName = chip.getAttribute('data-guild-name');
    const botInGuild = chip.getAttribute('data-bot-in') === 'true';

    if (activeServerDisplay) activeServerDisplay.textContent = currentGuildName;
    if (activeServerIdDisplay) activeServerIdDisplay.textContent = `ID: ${currentGuildId}`;

    if (botInviteBanner) {
      botInviteBanner.style.display = botInGuild ? 'none' : 'flex';
    }

    // 切換視圖至伺服器設定畫面
    if (viewServerSelection) viewServerSelection.style.display = 'none';
    if (viewServerSettings) viewServerSettings.style.display = 'block';

    // 載入該伺服器的即時設定與頻道清單
    switchTab('tab-welcome');
    loadGuildData(currentGuildId);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  serverChips.forEach((chip) => {
    chip.addEventListener('click', () => selectGuild(chip));
  });

  // ---------- 載入伺服器資料與各模組設定 ----------
  async function loadGuildData(guildId) {
    if (!guildId) return;

    try {
      const res = await fetch(`/api/panel/guild/${guildId}/data`);
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        showToast(err.message || '載入伺服器設定失敗', 'error');
        return;
      }
      const data = await res.json();
      currentChannels = data.channels || [];
      currentRoles = data.roles || [];
      currentCommands = data.commands || [];

      // 1. 填入下拉選單 (歡迎頻道、身分組等)
      populateDropdowns(data.welcome, data.music);

      // 2. 渲染自動回覆規則清單
      renderAutoReplyRules(data.auto_reply_rules || []);

      // 3. 渲染表符身分組配置
      renderReactionRoles(data.reaction_roles_messages || []);

      // 4. 渲染指令清單 (確保 owner_guild_only 依身分嚴格隔離)
      renderCommandsTable(currentCommands);
    } catch (err) {
      console.error(err);
      showToast('網路連線失敗，無法讀取伺服器設定', 'error');
    }
  }

  function populateDropdowns(welcomeCfg = {}, musicCfg = {}) {
    const welcomeSelect = document.getElementById('select-welcome-channel');
    const rulesSelect = document.getElementById('select-rules-channel');
    const roleSelect = document.getElementById('select-role-channel');
    const autoRoleSelect = document.getElementById('select-auto-role');
    const musicSelect = document.getElementById('select-music-channel');
    const modalRuleChannelSelect = document.getElementById('modal-rule-channel');

    function fillChannels(selectEl, selectedVal, defaultLabel) {
      if (!selectEl) return;
      selectEl.innerHTML = `<option value="">-- ${defaultLabel} --</option>`;
      currentChannels.forEach((ch) => {
        const opt = document.createElement('option');
        opt.value = ch.id;
        opt.textContent = `# ${ch.name}`;
        if (String(ch.id) === String(selectedVal)) opt.selected = true;
        selectEl.appendChild(opt);
      });
    }

    fillChannels(welcomeSelect, welcomeCfg.welcome_channel_id, '未設定（停用發送）');
    fillChannels(rulesSelect, welcomeCfg.rules_channel_id, '未指定（略過提及）');
    fillChannels(roleSelect, welcomeCfg.role_channel_id, '未指定（略過提及）');
    fillChannels(musicSelect, musicCfg.music_channel_id, '未綁定（所有頻道皆可使用）');
    fillChannels(modalRuleChannelSelect, '', '全伺服器所有文字頻道生效');

    if (autoRoleSelect) {
      autoRoleSelect.innerHTML = '<option value="">-- 未設定（不自動賦予） --</option>';
      currentRoles.forEach((r) => {
        const opt = document.createElement('option');
        opt.value = r.id;
        opt.textContent = `@ ${r.name}`;
        if (String(r.id) === String(welcomeCfg.auto_role_id)) opt.selected = true;
        autoRoleSelect.appendChild(opt);
      });
    }
  }

  // ---------- 歡迎訊息設定儲存與清除 ----------
  const formWelcome = document.getElementById('form-welcome');
  if (formWelcome) {
    formWelcome.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (!currentGuildId) return;

      const payload = {
        action: 'save',
        welcome_channel_id: document.getElementById('select-welcome-channel').value || null,
        rules_channel_id: document.getElementById('select-rules-channel').value || null,
        role_channel_id: document.getElementById('select-role-channel').value || null,
        auto_role_id: document.getElementById('select-auto-role').value || null,
      };

      try {
        const res = await fetch(`/api/panel/guild/${currentGuildId}/welcome`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (res.ok) {
          showToast(data.message || '歡迎訊息設定已儲存！');
        } else {
          showToast(data.message || '儲存失敗', 'error');
        }
      } catch (err) {
        showToast('儲存請求發生網路錯誤', 'error');
      }
    });
  }

  const btnClearWelcome = document.getElementById('btn-clear-welcome');
  if (btnClearWelcome) {
    btnClearWelcome.addEventListener('click', () => {
      if (!currentGuildId) return;
      showConfirmModal({
        title: '⚠️ 確認清除歡迎設定',
        message: '確定要清除此伺服器的「歡迎訊息與身分組」設定嗎？清除後新進成員將不會收到歡迎訊息與自動身分組，此操作無法復原。',
        confirmText: '確認清除',
        cancelText: '取消',
        onConfirm: async () => {
          try {
            const res = await fetch(`/api/panel/guild/${currentGuildId}/welcome`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ action: 'clear' }),
            });
            const data = await res.json();
            if (res.ok) {
              showToast(data.message || '設定已清除');
              document.getElementById('select-welcome-channel').value = '';
              document.getElementById('select-rules-channel').value = '';
              document.getElementById('select-role-channel').value = '';
              document.getElementById('select-auto-role').value = '';
            } else {
              showToast(data.message || '清除失敗', 'error');
            }
          } catch (err) {
            showToast('請求發生網路錯誤', 'error');
          }
        },
      });
    });
  }

  // ---------- 音樂點歌頻道設定儲存與解除 ----------
  const formMusic = document.getElementById('form-music');
  if (formMusic) {
    formMusic.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (!currentGuildId) return;

      const musicChannelId = document.getElementById('select-music-channel').value || null;
      try {
        const res = await fetch(`/api/panel/guild/${currentGuildId}/music`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ music_channel_id: musicChannelId }),
        });
        const data = await res.json();
        if (res.ok) {
          showToast(data.message || '點歌頻道設定已儲存！');
        } else {
          showToast(data.message || '儲存失敗', 'error');
        }
      } catch (err) {
        showToast('儲存請求發生網路錯誤', 'error');
      }
    });
  }

  const btnClearMusic = document.getElementById('btn-clear-music');
  if (btnClearMusic) {
    btnClearMusic.addEventListener('click', () => {
      if (!currentGuildId) return;
      showConfirmModal({
        title: '⚠️ 確認清除音樂點歌設定',
        message: '確定要解除此伺服器的「專屬點歌頻道」綁定嗎？清除後所有文字頻道皆可使用點歌指令。',
        confirmText: '確認清除',
        cancelText: '取消',
        onConfirm: async () => {
          try {
            const res = await fetch(`/api/panel/guild/${currentGuildId}/music`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ music_channel_id: null }),
            });
            const data = await res.json();
            if (res.ok) {
              showToast(data.message || '已解除頻道綁定');
              document.getElementById('select-music-channel').value = '';
            } else {
              showToast(data.message || '操作失敗', 'error');
            }
          } catch (err) {
            showToast('請求發生網路錯誤', 'error');
          }
        },
      });
    });
  }

  // ---------- 關鍵字自動回覆 (Auto Reply) ----------
  const autoReplyListContainer = document.getElementById('auto-reply-list-container');
  const modalAutoReply = document.getElementById('modal-auto-reply');
  const btnAddAutoReply = document.getElementById('btn-add-auto-reply');
  const modalCloseBtn = document.getElementById('modal-auto-reply-close');
  const modalCancelBtn = document.getElementById('modal-auto-reply-cancel');
  const formModalAutoReply = document.getElementById('form-modal-auto-reply');
  let currentRulesList = [];

  function renderAutoReplyRules(rules) {
    currentRulesList = rules;
    if (!autoReplyListContainer) return;

    if (!rules || rules.length === 0) {
      autoReplyListContainer.innerHTML = '<div class="empty-rules-hint">目前尚未設定任何關鍵字自動回覆規則。點擊上方「＋ 新增回覆規則」開始建立！</div>';
      return;
    }

    autoReplyListContainer.innerHTML = '';
    rules.forEach((r) => {
      const card = document.createElement('div');
      card.className = `rule-card ${r.is_paused ? 'paused' : ''}`;

      const keywordsHtml = (r.keywords || [])
        .map((k) => `<span class="keyword-tag">${escapeHtml(k)}</span>`)
        .join('');

      const repliesText = (r.replies || []).join('\n---\n');

      let channelName = '全伺服器生效';
      if (r.channel_id) {
        const matched = currentChannels.find((c) => String(c.id) === String(r.channel_id));
        channelName = matched ? `# ${matched.name}` : `頻道 ID: ${r.channel_id}`;
      }

      card.innerHTML = `
        <div class="rule-card-main">
          <div class="rule-card-title-row">
            <span class="rule-title">${escapeHtml(r.name || '未命名規則')}</span>
            ${r.is_paused ? '<span class="tag" style="border-color: #f87171; color: #f87171;">已暫停</span>' : '<span class="tag" style="border-color: #34d399; color: #34d399;">啟用中</span>'}
          </div>
          <div class="rule-keywords-row">
            ${keywordsHtml}
          </div>
          <div class="rule-replies-box">${escapeHtml(repliesText)}</div>
          <div class="rule-meta-info">📍 生效範圍：${escapeHtml(channelName)}</div>
        </div>
        <div class="rule-actions-col">
          <button class="btn-icon-action btn-edit-rule" data-id="${r.id}" type="button">✏️ 編輯</button>
          <button class="btn-icon-action btn-toggle-rule" data-id="${r.id}" type="button">${r.is_paused ? '▶️ 啟用' : '⏸️ 暫停'}</button>
          <button class="btn-icon-action delete btn-del-rule" data-id="${r.id}" type="button">🗑️ 刪除</button>
        </div>
      `;
      autoReplyListContainer.appendChild(card);
    });

    // 綁定按鈕監聽
    autoReplyListContainer.querySelectorAll('.btn-edit-rule').forEach((b) => {
      b.addEventListener('click', () => openEditRuleModal(b.getAttribute('data-id')));
    });
    autoReplyListContainer.querySelectorAll('.btn-toggle-rule').forEach((b) => {
      b.addEventListener('click', () => toggleRule(b.getAttribute('data-id')));
    });
    autoReplyListContainer.querySelectorAll('.btn-del-rule').forEach((b) => {
      b.addEventListener('click', () => deleteRule(b.getAttribute('data-id')));
    });
  }

  function openEditRuleModal(ruleId = null) {
    if (!modalAutoReply) return;
    const titleEl = document.getElementById('modal-auto-reply-title');
    const idEl = document.getElementById('modal-rule-id');
    const nameEl = document.getElementById('modal-rule-name');
    const kwEl = document.getElementById('modal-rule-keywords');
    const repEl = document.getElementById('modal-rule-replies');
    const chanEl = document.getElementById('modal-rule-channel');
    const pausedEl = document.getElementById('modal-rule-paused');

    if (ruleId) {
      const rule = currentRulesList.find((r) => String(r.id) === String(ruleId));
      if (!rule) return;
      titleEl.textContent = '編輯自動回覆規則';
      idEl.value = rule.id;
      nameEl.value = rule.name || '';
      kwEl.value = (rule.keywords || []).join(', ');
      repEl.value = (rule.replies || []).join('\n');
      chanEl.value = rule.channel_id || '';
      pausedEl.checked = Boolean(rule.is_paused);
    } else {
      titleEl.textContent = '新增自動回覆規則';
      idEl.value = '';
      nameEl.value = '';
      kwEl.value = '';
      repEl.value = '';
      chanEl.value = '';
      pausedEl.checked = false;
    }

    modalAutoReply.style.display = 'flex';
  }

  function closeModal() {
    if (modalAutoReply) modalAutoReply.style.display = 'none';
  }

  if (btnAddAutoReply) btnAddAutoReply.addEventListener('click', () => openEditRuleModal(null));
  if (modalCloseBtn) modalCloseBtn.addEventListener('click', closeModal);
  if (modalCancelBtn) modalCancelBtn.addEventListener('click', closeModal);

  if (formModalAutoReply) {
    formModalAutoReply.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (!currentGuildId) return;

      const ruleId = document.getElementById('modal-rule-id').value;
      const payload = {
        id: ruleId ? parseInt(ruleId) : null,
        name: document.getElementById('modal-rule-name').value.trim(),
        keywords: document.getElementById('modal-rule-keywords').value.trim(),
        replies: document.getElementById('modal-rule-replies').value.trim(),
        channel_id: document.getElementById('modal-rule-channel').value || null,
        is_paused: document.getElementById('modal-rule-paused').checked,
      };

      try {
        const res = await fetch(`/api/panel/guild/${currentGuildId}/auto-reply/save`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (res.ok) {
          showToast(data.message || '規則已成功儲存！');
          closeModal();
          loadGuildData(currentGuildId);
        } else {
          showToast(data.message || '儲存失敗', 'error');
        }
      } catch (err) {
        showToast('網路請求失敗', 'error');
      }
    });
  }

  async function toggleRule(ruleId) {
    if (!currentGuildId || !ruleId) return;
    try {
      const res = await fetch(`/api/panel/guild/${currentGuildId}/auto-reply/toggle`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: parseInt(ruleId) }),
      });
      const data = await res.json();
      if (res.ok) {
        showToast(data.message);
        loadGuildData(currentGuildId);
      } else {
        showToast(data.message || '切換失敗', 'error');
      }
    } catch (err) {
      showToast('請求網路錯誤', 'error');
    }
  }

  async function deleteRule(ruleId) {
    if (!currentGuildId || !ruleId) return;
    if (!confirm('確定要刪除這條自動回覆規則嗎？')) return;

    try {
      const res = await fetch(`/api/panel/guild/${currentGuildId}/auto-reply/delete`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: parseInt(ruleId) }),
      });
      const data = await res.json();
      if (res.ok) {
        showToast(data.message || '規則已刪除');
        loadGuildData(currentGuildId);
      } else {
        showToast(data.message || '刪除失敗', 'error');
      }
    } catch (err) {
      showToast('請求網路錯誤', 'error');
    }
  }

  // ---------- 表符身分組配置清單渲染 ----------
  function renderReactionRoles(messages) {
    const container = document.getElementById('reaction-roles-list-container');
    if (!container) return;

    if (!messages || messages.length === 0) {
      container.innerHTML = '<div class="empty-rules-hint">此伺服器目前尚未設定任何表符身分組。您可在 Discord 中直接執行 <code>/reaction_roles_create</code> 快速建立領取身分組訊息！</div>';
      return;
    }

    container.innerHTML = '';
    messages.forEach((msg) => {
      const card = document.createElement('div');
      card.className = 'rr-card';

      const channelObj = currentChannels.find((c) => String(c.id) === String(msg.channel_id));
      const channelLabel = channelObj ? `# ${channelObj.name}` : `頻道 ID: ${msg.channel_id}`;

      const pairsHtml = (msg.pairs || [])
        .map((p) => {
          const roleObj = currentRoles.find((r) => String(r.id) === String(p.role_id));
          const roleName = roleObj ? roleObj.name : `身分組 ID: ${p.role_id}`;
          const roleColor = roleObj ? roleObj.color : '#94a3b8';
          return `
            <div class="rr-pair-item">
              <span class="rr-emoji">${escapeHtml(p.emoji)}</span>
              <span class="rr-role-name" style="color: ${roleColor};">@ ${escapeHtml(roleName)}</span>
            </div>
          `;
        })
        .join('');

      card.innerHTML = `
        <div class="rr-header-row">
          <span>📨 訊息 ID：<code>${msg.message_id}</code></span>
          <span>📍 頻道：${escapeHtml(channelLabel)}</span>
          <span>⚙️ 模式：${msg.strict ? '單選互斥 (Strict)' : '複選自由'}</span>
        </div>
        <div class="rr-pairs-grid">
          ${pairsHtml}
        </div>
      `;
      container.appendChild(card);
    });
  }

  // ---------- 指令清單表格渲染與即時搜尋 ----------
  function renderCommandsTable(commands) {
    const container = document.getElementById('commands-table-container');
    if (!container) return;

    if (!commands || commands.length === 0) {
      container.innerHTML = '<div class="empty-rules-hint">沒有可用指令。</div>';
      return;
    }

    let html = `
      <table class="commands-table">
        <thead>
          <tr>
            <th>指令名稱</th>
            <th>分類</th>
            <th>功能說明</th>
            <th>權限限制</th>
          </tr>
        </thead>
        <tbody>
    `;

    commands.forEach((c) => {
      const isOwnerOnly = Boolean(c.owner_guild_only);
      const permBadge = isOwnerOnly
        ? '<span class="tag">管理員專用</span>'
        : '<span class="tag">全體成員 / 管理員</span>';

      html += `
        <tr>
          <td><span class="cmd-badge">${escapeHtml(c.name)}</span></td>
          <td>${escapeHtml(c.category)}</td>
          <td>${escapeHtml(c.description)}</td>
          <td>${permBadge}</td>
        </tr>
      `;
    });

    html += '</tbody></table>';
    container.innerHTML = html;
  }

  const cmdSearchInput = document.getElementById('command-search-input');
  if (cmdSearchInput) {
    cmdSearchInput.addEventListener('input', (e) => {
      const query = e.target.value.toLowerCase().trim();
      if (!query) {
        renderCommandsTable(currentCommands);
        return;
      }
      const filtered = currentCommands.filter(
        (c) =>
          c.name.toLowerCase().includes(query) ||
          c.description.toLowerCase().includes(query) ||
          c.category.toLowerCase().includes(query)
      );
      renderCommandsTable(filtered);
    });
  }

  // ============================================================================
  // 👑 擁有者專區邏輯 (嚴格後台保護，只有 is_owner 為 True 時運作)
  // ============================================================================

  // 擁有者子分頁切換
  const ownerSubtabBtns = document.querySelectorAll('.owner-subtab-btn');
  const ownerSubpanes = document.querySelectorAll('.owner-subpane');

  ownerSubtabBtns.forEach((btn) => {
    btn.addEventListener('click', () => {
      ownerSubtabBtns.forEach((b) => b.classList.remove('active'));
      ownerSubpanes.forEach((p) => p.classList.remove('active'));

      btn.classList.add('active');
      const targetSub = btn.getAttribute('data-subtab');
      const targetPane = document.getElementById(targetSub);
      if (targetPane) targetPane.classList.add('active');
    });
  });

  // 1. 經濟系統設定 (讀取 & 儲存)
  async function loadOwnerEconomyConfig() {
    const form = document.getElementById('form-owner-economy');
    if (!form) return;

    try {
      const res = await fetch('/api/panel/owner/economy-config');
      if (res.status === 403) return; // 非擁有者，後端直接拒絕
      const data = await res.json();
      if (!res.ok || !data.config) return;

      const cfg = data.config;
      for (const [key, val] of Object.entries(cfg)) {
        const input = document.getElementById(`econ-${key}`);
        if (!input) continue;
        if (input.type === 'checkbox') {
          input.checked = Boolean(val);
        } else {
          input.value = val;
        }
      }
    } catch (err) {
      console.error('無法讀取經濟後台設定:', err);
    }
  }

  const formOwnerEconomy = document.getElementById('form-owner-economy');
  if (formOwnerEconomy) {
    formOwnerEconomy.addEventListener('submit', async (e) => {
      e.preventDefault();
      const payload = {};
      const inputs = formOwnerEconomy.querySelectorAll('input');
      inputs.forEach((inp) => {
        const key = inp.id.replace('econ-', '');
        if (inp.type === 'checkbox') {
          payload[key] = inp.checked;
        } else if (inp.type === 'number') {
          payload[key] = parseFloat(inp.value);
        } else {
          payload[key] = inp.value;
        }
      });

      try {
        const res = await fetch('/api/panel/owner/economy-config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (res.ok) {
          showToast(data.message || '經濟系統後台參數已成功套用！');
        } else {
          showToast(data.message || '更新失敗', 'error');
        }
      } catch (err) {
        showToast('網路請求發生錯誤', 'error');
      }
    });
  }

  // 2. 玩家金幣帳戶查詢與修改
  const btnFetchUserBalance = document.getElementById('btn-fetch-user-balance');
  const userDetailsCard = document.getElementById('user-details-card');
  let currentInspectedUserId = null;

  async function inspectUserBalance() {
    const idInput = document.getElementById('manage-user-id');
    const uid = idInput ? idInput.value.trim() : '';
    if (!uid) {
      showToast('請輸入 Discord 使用者 ID', 'error');
      return;
    }

    try {
      const res = await fetch(`/api/panel/owner/economy-user?user_id=${encodeURIComponent(uid)}`);
      const data = await res.json();
      if (!res.ok) {
        showToast(data.message || '查詢失敗', 'error');
        return;
      }

      currentInspectedUserId = uid;
      const u = data.user;
      document.getElementById('stat-balance').textContent = u.balance.toLocaleString();
      document.getElementById('stat-streak').textContent = `${u.daily_streak} 天`;
      document.getElementById('stat-earned').textContent = u.total_earned.toLocaleString();
      document.getElementById('stat-wagered').textContent = u.total_wagered.toLocaleString();

      if (userDetailsCard) userDetailsCard.style.display = 'block';
      showToast(`已取得使用者 ${uid} 的金幣帳戶資訊`);
    } catch (err) {
      showToast('查詢網路錯誤', 'error');
    }
  }

  if (btnFetchUserBalance) btnFetchUserBalance.addEventListener('click', inspectUserBalance);

  async function modifyUserBalance(action) {
    if (!currentInspectedUserId) return;
    const amtInput = document.getElementById('modify-amount-input');
    const amountVal = amtInput ? amtInput.value.trim() : '';
    if (!amountVal) {
      showToast('請輸入異動金額', 'error');
      return;
    }

    try {
      const res = await fetch('/api/panel/owner/economy-user', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          user_id: currentInspectedUserId,
          action: action,
          amount: parseInt(amountVal),
        }),
      });
      const data = await res.json();
      if (res.ok) {
        showToast(data.message);
        inspectUserBalance(); // 重新整理顯示
        if (amtInput) amtInput.value = '';
      } else {
        showToast(data.message || '修改失敗', 'error');
      }
    } catch (err) {
      showToast('請求網路錯誤', 'error');
    }
  }

  const btnSetBal = document.getElementById('btn-set-balance');
  const btnAddBal = document.getElementById('btn-add-balance');
  const btnDedBal = document.getElementById('btn-deduct-balance');

  if (btnSetBal) btnSetBal.addEventListener('click', () => modifyUserBalance('set'));
  if (btnAddBal) btnAddBal.addEventListener('click', () => modifyUserBalance('add'));
  if (btnDedBal) {
    btnDedBal.addEventListener('click', () => {
      const amtInput = document.getElementById('modify-amount-input');
      const val = parseInt(amtInput ? amtInput.value : 0);
      if (val > 0) amtInput.value = -val;
      modifyUserBalance('add');
    });
  }

  // 3. Cog 模組線上熱重載
  const cogsGridList = document.getElementById('cogs-grid-list');
  const reloadOutput = document.getElementById('reload-sync-output');
  const btnReloadAllCogs = document.getElementById('btn-reload-all-cogs');
  const btnSyncAllCommands = document.getElementById('btn-sync-all-commands');

  async function loadOwnerCogsList() {
    if (!cogsGridList) return;
    try {
      const res = await fetch('/api/panel/owner/cogs');
      if (res.status === 403) return;
      const data = await res.json();
      if (!res.ok || !data.cogs) return;

      cogsGridList.innerHTML = '';
      data.cogs.forEach((cog) => {
        const card = document.createElement('div');
        card.className = 'cog-card';
        card.innerHTML = `
          <div>
            <span class="cog-name">${escapeHtml(cog.name)}</span>
            <div style="font-size: 0.75rem; color: #64748b; font-family: monospace;">${escapeHtml(cog.module)}</div>
          </div>
          <button class="btn secondary btn-reload-single" data-mod="${cog.module}" style="padding: 6px 14px; font-size: 0.82rem;" type="button">
            <span>🔄 重載</span>
          </button>
        `;
        cogsGridList.appendChild(card);
      });

      cogsGridList.querySelectorAll('.btn-reload-single').forEach((b) => {
        b.addEventListener('click', () => reloadModule(b.getAttribute('data-mod')));
      });
    } catch (err) {
      console.error(err);
    }
  }

  async function reloadModule(modName) {
    if (reloadOutput) reloadOutput.textContent = `⏳ 正在重載模組 [${modName}] 並同步指令樹...`;
    showToast(`正在重載 ${modName}...`);

    try {
      const res = await fetch('/api/panel/owner/reload-cog', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ module: modName }),
      });
      const data = await res.json();
      if (res.ok) {
        showToast(data.message);
        if (reloadOutput) {
          reloadOutput.textContent = `${data.message}\n指令同步狀態：${data.sync_summary}`;
        }
      } else {
        showToast(data.message || '重載失敗', 'error');
        if (reloadOutput) reloadOutput.textContent = `❌ 重載失敗：${data.message}`;
      }
    } catch (err) {
      showToast('重載模組時發生連線錯誤', 'error');
      if (reloadOutput) reloadOutput.textContent = `❌ 例外錯誤：${err.message}`;
    }
  }

  if (btnReloadAllCogs) btnReloadAllCogs.addEventListener('click', () => reloadModule('all'));

  if (btnSyncAllCommands) {
    btnSyncAllCommands.addEventListener('click', async () => {
      if (reloadOutput) reloadOutput.textContent = '⏳ 正在強制向 Discord 官方重新同步指令樹...';
      try {
        const res = await fetch('/api/panel/owner/sync-commands', { method: 'POST' });
        const data = await res.json();
        if (res.ok) {
          showToast(data.message);
          if (reloadOutput) reloadOutput.textContent = data.message;
        } else {
          showToast(data.message || '同步失敗', 'error');
          if (reloadOutput) reloadOutput.textContent = `❌ 同步失敗：${data.message}`;
        }
      } catch (err) {
        showToast('指令同步請求失敗', 'error');
      }
    });
  }

  // 4. API 每日配額狀態
  async function loadOwnerApiQuota() {
    try {
      const res = await fetch('/api/panel/owner/api-quota');
      if (res.status === 403) return;
      const data = await res.json();
      if (!res.ok || !data.quotas) return;

      const q = data.quotas;
      if (q.weather_cwa) {
        document.getElementById('quota-weather-count').textContent = q.weather_cwa.count;
        document.getElementById('quota-weather-date').textContent = `統計日期: ${q.weather_cwa.date || '今日'}`;
      }
      if (q.bus_tdx) {
        document.getElementById('quota-bus-count').textContent = q.bus_tdx.count;
        document.getElementById('quota-bus-date').textContent = `統計日期: ${q.bus_tdx.date || '今日'}`;
      }
      if (q.shortener_reurl) {
        document.getElementById('quota-reurl-count').textContent = q.shortener_reurl.count;
        document.getElementById('quota-reurl-date').textContent = `統計日期: ${q.shortener_reurl.date || '今日'}`;
      }
    } catch (err) {
      console.error(err);
    }
  }

  // ---------- 輔助工具函式 ----------
  function escapeHtml(str) {
    if (!str) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  // ---------- 初始化動作 ----------
  document.addEventListener('DOMContentLoaded', () => {
    // 檢查網址 hash
    const hash = window.location.hash;
    if (hash === '#owner') {
      openSettingsViewForOwner();
    } else if (hash && hash.startsWith('#guild-')) {
      const targetGid = hash.replace('#guild-', '');
      const targetChip = document.querySelector(`.server-chip[data-guild-id="${targetGid}"]`);
      if (targetChip) {
        selectGuild(targetChip);
      }
    }
    // 預設留在伺服器選擇畫面，不自動跳轉
  });
})();

