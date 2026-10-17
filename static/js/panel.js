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
  let currentMembers = [];
  let currentEmojis = [];
  let currentCommands = [];
  let currentReactionRoles = [];
  let currentGuildAiAuthorized = true;

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
  const btnDropdownOwner = document.getElementById('btn-dropdown-owner');

  const modalConfirm = document.getElementById('modal-confirm-clear');
  const modalConfirmTitle = document.getElementById('modal-confirm-title');
  const modalConfirmMessage = document.getElementById('modal-confirm-message');
  const modalConfirmOk = document.getElementById('modal-confirm-ok');
  const modalConfirmCancel = document.getElementById('modal-confirm-cancel');
  const modalConfirmClose = document.getElementById('modal-confirm-close');

  const modalBackdrops = [
    document.getElementById('modal-auto-reply'),
    document.getElementById('modal-reaction-role'),
    modalConfirm,
  ].filter(Boolean);

  function syncModalScrollLock() {
    const anyModalOpen = modalBackdrops.some((el) => el.style.display === 'flex');
    document.documentElement.classList.toggle('modal-open', anyModalOpen);
    document.body.classList.toggle('modal-open', anyModalOpen);
    modalBackdrops.forEach((el) => {
      el.setAttribute('aria-hidden', el.style.display === 'flex' ? 'false' : 'true');
    });
  }

  // 防止在彈出視窗開啟時，於背景遮罩或非捲動區塊滑動導致捲動穿透到底層頁面
  modalBackdrops.forEach((backdrop) => {
    const blockBackdropScroll = (e) => {
      const scrollable = e.target.closest(
        '.modal-body, .searchable-select-menu, .reply-blocks-list, .rr-editor-pairs-list, .rr-emoji-picker-list, textarea'
      );
      if (!scrollable) {
        e.preventDefault();
      }
    };
    backdrop.addEventListener('wheel', blockBackdropScroll, { passive: false });
    backdrop.addEventListener('touchmove', blockBackdropScroll, { passive: false });
  });

  // ---------- 清除設定確認 Modal (Confirm Modal) ----------
  let currentConfirmCallback = null;

  function closeConfirmModal() {
    if (modalConfirm) modalConfirm.style.display = 'none';
    currentConfirmCallback = null;
    syncModalScrollLock();
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
    syncModalScrollLock();
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

  // 移除訊息開頭既有的狀態 emoji（✅ / ❌ / ⚠️ 等），避免與 Toast 圖示重複
  function stripStatusEmoji(message) {
    return String(message ?? '').replace(/^\s*(?:[✅❌✔️✖️⚠️❎☑️]|\uFE0F)+\s*/u, '');
  }

  // ---------- Toast 提示通知函式 ----------
  function showToast(message, type = 'success') {
    if (!toastContainer) return;
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    const icon = type === 'success' ? '✅' : '❌';
    toast.innerHTML = `<span>${icon}</span><span>${stripStatusEmoji(message)}</span>`;
    toastContainer.appendChild(toast);

    setTimeout(() => {
      toast.style.opacity = '0';
      toast.style.transform = 'translateY(10px)';
      toast.style.transition = 'all 0.3s ease';
      setTimeout(() => toast.remove(), 300);
    }, 4000);
  }

  // ---------- 尚未儲存變更底部懸浮提示列 (Unsaved Changes Bar) ----------
  const unsavedBar = document.getElementById('unsaved-changes-bar');
  const btnUnsavedSave = document.getElementById('btn-unsaved-save');
  const btnUnsavedReset = document.getElementById('btn-unsaved-reset');
  const savedFormState = {
    welcome: null,
    music: null,
    ai: null,
    ownerEconomy: null,
  };

  function getFormState(formEl) {
    if (!formEl) return null;
    const state = {};
    formEl.querySelectorAll('input, select, textarea').forEach((el) => {
      if (!el.id || el.classList.contains('searchable-select-input')) return;
      if (el.type === 'checkbox') {
        state[el.id] = Boolean(el.checked);
      } else {
        state[el.id] = String(el.value ?? '');
      }
    });
    return state;
  }

  function isFormDirty(formEl, snapshot) {
    if (!formEl || !snapshot) return false;
    const current = getFormState(formEl);
    if (!current) return false;
    const keys = Object.keys(snapshot);
    for (let i = 0; i < keys.length; i += 1) {
      const k = keys[i];
      if (current[k] !== snapshot[k]) return true;
    }
    return false;
  }

  function applyFormState(formEl, snapshot) {
    if (!formEl || !snapshot) return;
    Object.entries(snapshot).forEach(([id, val]) => {
      const el = document.getElementById(id);
      if (!el) return;
      if (el.type === 'checkbox') {
        el.checked = Boolean(val);
      } else {
        el.value = String(val ?? '');
      }
    });
  }

  function setUnsavedBarVisible(visible) {
    if (!unsavedBar) return;
    unsavedBar.classList.toggle('is-visible', Boolean(visible));
    unsavedBar.setAttribute('aria-hidden', visible ? 'false' : 'true');
    document.body.classList.toggle('has-unsaved-changes', Boolean(visible));
  }

  function checkUnsavedChanges() {
    if (!unsavedBar || !viewServerSettings || viewServerSettings.style.display === 'none') {
      setUnsavedBarVisible(false);
      return;
    }
    const fWelcome = document.getElementById('form-welcome');
    const fMusic = document.getElementById('form-music');
    const fAi = document.getElementById('form-ai');
    const fOwnerEcon = document.getElementById('form-owner-economy');

    const isOwnerMode = viewServerSettings.classList.contains('owner-mode');
    let dirty = false;
    if (isOwnerMode) {
      dirty = isFormDirty(fOwnerEcon, savedFormState.ownerEconomy);
    } else {
      dirty =
        isFormDirty(fWelcome, savedFormState.welcome) ||
        isFormDirty(fMusic, savedFormState.music) ||
        isFormDirty(fAi, savedFormState.ai);
    }
    setUnsavedBarVisible(dirty);
  }

  // ---------- 視圖切換與返回伺服器清單 ----------
  function returnToServerSelection() {
    if (viewServerSettings) {
      viewServerSettings.style.display = 'none';
      viewServerSettings.classList.remove('owner-mode');
    }
    if (viewServerSelection) viewServerSelection.style.display = 'block';
    currentGuildId = null;
    currentGuildName = '';
    serverChips.forEach((c) => c.classList.remove('active'));
    setUnsavedBarVisible(false);
    if (window.location.hash) {
      history.replaceState(null, '', window.location.pathname);
    }
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  if (btnBackToServers) {
    btnBackToServers.addEventListener('click', returnToServerSelection);
  }

  function openSettingsViewForOwner() {
    if (viewServerSelection) viewServerSelection.style.display = 'none';
    if (viewServerSettings) {
      viewServerSettings.style.display = 'block';
      viewServerSettings.classList.add('owner-mode');
    }
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
      loadOwnerAiGuilds();
    }
    checkUnsavedChanges();
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
    if (viewServerSettings) {
      viewServerSettings.style.display = 'block';
      viewServerSettings.classList.remove('owner-mode');
    }

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

    let data;
    try {
      const res = await fetch(`/api/panel/guild/${guildId}/data`);
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        showToast(err.message || '載入伺服器設定失敗', 'error');
        return;
      }
      data = await res.json();
    } catch (netErr) {
      console.error('Fetch guild data failed:', netErr);
      showToast('網路連線失敗，無法讀取伺服器設定', 'error');
      return;
    }

    try {
      currentChannels = data.channels || [];
      currentRoles = data.roles || [];
      currentMembers = data.members || [];
      currentEmojis = data.emojis || [];
      currentCommands = data.commands || [];
      currentReactionRoles = data.reaction_roles_messages || [];

      // 1. 填入下拉選單 (歡迎頻道、身分組等)
      populateDropdowns(data.welcome, data.music, data.ai);

      // 2. 渲染自動回覆規則清單
      renderAutoReplyRules(data.auto_reply_rules || []);

      // 3. 渲染表符身分組配置
      renderReactionRoles(currentReactionRoles);

      // 4. 渲染指令清單 (確保 owner_guild_only 依身分嚴格隔離)
      renderCommandsTable(currentCommands);

      // 5. 處理 AI 智慧助手封測授權狀態 (未授權時全面鎖定控制項)
      const isAiAuthorized = data.ai_authorized !== false;
      currentGuildAiAuthorized = isAiAuthorized;
      const aiUnauthBanner = document.getElementById('ai-unauthorized-banner');
      if (aiUnauthBanner) {
        aiUnauthBanner.style.display = isAiAuthorized ? 'none' : 'block';
      }
      const formAi = document.getElementById('form-ai');
      if (formAi) {
        formAi.querySelectorAll('input, select, textarea, button').forEach((el) => {
          el.disabled = !isAiAuthorized;
        });
        formAi.style.opacity = isAiAuthorized ? '1' : '0.45';
        formAi.style.pointerEvents = isAiAuthorized ? 'auto' : 'none';
        const aiSubmit = formAi.querySelector('.btn-save-setting');
        const aiReset = document.getElementById('btn-reset-ai');
        if (aiSubmit) {
          aiSubmit.disabled = !isAiAuthorized;
          aiSubmit.title = isAiAuthorized ? '' : '本伺服器尚未獲得 AI 封測授權';
        }
        if (aiReset) {
          aiReset.disabled = !isAiAuthorized;
          aiReset.title = isAiAuthorized ? '' : '本伺服器尚未獲得 AI 封測授權';
        }
      }
    } catch (renderErr) {
      console.error('UI rendering failed:', renderErr);
    }
  }

  // ---------- 可輸入即時搜尋下拉選單 (Searchable Combobox) ----------
  function enhanceSearchableSelect(selectEl) {
    if (!selectEl) return;
    if (typeof selectEl._searchableSync === 'function') {
      selectEl._searchableSync();
      return;
    }

    const wrap = document.createElement('div');
    wrap.className = 'searchable-select-wrap';
    selectEl.parentNode.insertBefore(wrap, selectEl);
    wrap.appendChild(selectEl);
    selectEl.style.display = 'none';

    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'form-control searchable-select-input';
    input.autocomplete = 'off';
    input.spellcheck = false;

    const menu = document.createElement('div');
    menu.className = 'searchable-select-menu';

    wrap.appendChild(input);
    wrap.appendChild(menu);

    if (selectEl.id) {
      const assocLabel = document.querySelector(`label[for="${selectEl.id}"]`);
      if (assocLabel) {
        assocLabel.addEventListener('click', (e) => {
          e.preventDefault();
          input.focus();
        });
      }
    }

    let highlightedIndex = -1;

    function getSelectedLabel() {
      const opts = Array.from(selectEl.options);
      const found = opts.find((o) => String(o.value) === String(selectEl.value)) || opts[0];
      return found ? found.textContent : '';
    }

    function syncDisplayFromSelect() {
      input.disabled = Boolean(selectEl.disabled);
      const label = getSelectedLabel();
      if (!wrap.classList.contains('is-open')) {
        input.value = label;
        input.placeholder = label || '輸入關鍵字搜尋...';
      }
    }

    function renderMenu(query = '') {
      const q = String(query || '').trim().toLowerCase();
      const allOptions = Array.from(selectEl.options);
      const filtered = q
        ? allOptions.filter(
            (o) =>
              o.textContent.toLowerCase().includes(q) ||
              String(o.value).toLowerCase().includes(q)
          )
        : allOptions;

      menu.innerHTML = '';
      if (filtered.length === 0) {
        const emptyEl = document.createElement('div');
        emptyEl.className = 'searchable-select-empty';
        emptyEl.textContent = `找不到符合「${query.trim()}」的選項`;
        menu.appendChild(emptyEl);
        return;
      }

      if (highlightedIndex >= filtered.length) {
        highlightedIndex = filtered.length - 1;
      }

      filtered.forEach((opt, i) => {
        const item = document.createElement('div');
        item.className = 'searchable-select-option';
        if (String(opt.value) === String(selectEl.value)) {
          item.classList.add('is-selected');
        }
        if (i === highlightedIndex) {
          item.classList.add('is-highlighted');
        }
        item.dataset.value = opt.value;
        item.textContent = opt.textContent;

        item.addEventListener('mousedown', (e) => {
          e.preventDefault();
          selectOption(opt.value);
        });

        menu.appendChild(item);
      });
    }

    function openMenu() {
      if (selectEl.disabled || input.disabled) return;
      document.querySelectorAll('.searchable-select-wrap.is-open').forEach((otherWrap) => {
        if (otherWrap !== wrap) {
          otherWrap.classList.remove('is-open');
          const sel = otherWrap.querySelector('select');
          if (sel && typeof sel._searchableSync === 'function') {
            sel._searchableSync();
          }
        }
      });

      const rect = input.getBoundingClientRect();
      const modalBody = input.closest('.modal-body');
      const bottomBarReserve = !modalBody && unsavedBar && unsavedBar.classList.contains('is-visible') ? 96 : 24;
      const containerBottom = modalBody
        ? modalBody.getBoundingClientRect().bottom
        : window.innerHeight - bottomBarReserve;
      const containerTop = modalBody
        ? modalBody.getBoundingClientRect().top
        : 80;
      const spaceBelow = containerBottom - rect.bottom;
      const spaceAbove = rect.top - containerTop;
      const shouldFlipUp = spaceBelow < 236 && spaceAbove > spaceBelow;
      menu.classList.toggle('flip-up', shouldFlipUp);
      const availableSpace = shouldFlipUp ? spaceAbove : spaceBelow;
      menu.style.maxHeight = `${Math.max(120, Math.min(220, Math.floor(availableSpace - 12)))}px`;

      wrap.classList.add('is-open');
      const currentLabel = getSelectedLabel();
      input.placeholder = currentLabel || '輸入關鍵字搜尋...';
      input.value = '';
      highlightedIndex = -1;
      renderMenu('');

      const selectedItem = menu.querySelector('.searchable-select-option.is-selected');
      if (selectedItem && selectedItem.scrollIntoView) {
        selectedItem.scrollIntoView({ block: 'nearest' });
      }
    }

    function closeMenu() {
      if (!wrap.classList.contains('is-open')) return;
      wrap.classList.remove('is-open');
      syncDisplayFromSelect();
    }

    function selectOption(val) {
      selectEl.value = val;
      selectEl.dispatchEvent(new Event('change', { bubbles: true }));
      closeMenu();
      input.blur();
    }

    input.addEventListener('focus', () => {
      if (!wrap.classList.contains('is-open')) {
        openMenu();
      }
    });

    input.addEventListener('click', () => {
      if (!wrap.classList.contains('is-open')) {
        openMenu();
      }
    });

    input.addEventListener('input', () => {
      if (!wrap.classList.contains('is-open')) {
        wrap.classList.add('is-open');
      }
      highlightedIndex = 0;
      renderMenu(input.value);
    });

    input.addEventListener('keydown', (e) => {
      const items = menu.querySelectorAll('.searchable-select-option');
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        if (!wrap.classList.contains('is-open')) {
          openMenu();
          return;
        }
        if (items.length > 0) {
          highlightedIndex = (highlightedIndex + 1) % items.length;
          items.forEach((el, idx) => el.classList.toggle('is-highlighted', idx === highlightedIndex));
          if (items[highlightedIndex] && items[highlightedIndex].scrollIntoView) {
            items[highlightedIndex].scrollIntoView({ block: 'nearest' });
          }
        }
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        if (!wrap.classList.contains('is-open')) {
          openMenu();
          return;
        }
        if (items.length > 0) {
          highlightedIndex = (highlightedIndex - 1 + items.length) % items.length;
          items.forEach((el, idx) => el.classList.toggle('is-highlighted', idx === highlightedIndex));
          if (items[highlightedIndex] && items[highlightedIndex].scrollIntoView) {
            items[highlightedIndex].scrollIntoView({ block: 'nearest' });
          }
        }
      } else if (e.key === 'Enter') {
        if (wrap.classList.contains('is-open')) {
          e.preventDefault();
          const target = items[highlightedIndex >= 0 ? highlightedIndex : 0];
          if (target) {
            selectOption(target.dataset.value);
          } else {
            closeMenu();
            input.blur();
          }
        }
      } else if (e.key === 'Escape') {
        if (wrap.classList.contains('is-open')) {
          e.preventDefault();
          e.stopPropagation();
          closeMenu();
          input.blur();
        }
      }
    });

    input.addEventListener('blur', () => {
      setTimeout(() => {
        closeMenu();
      }, 120);
    });

    selectEl._searchableSync = syncDisplayFromSelect;
    syncDisplayFromSelect();
  }

  function refreshAllSearchableSelects() {
    const ids = [
      'select-welcome-channel',
      'select-rules-channel',
      'select-role-channel',
      'select-auto-role',
      'select-music-channel',
      'select-ai-channel',
      'select-ai-payer',
      'modal-rule-channel',
      'modal-rr-channel',
      'modal-rr-new-role',
    ];
    ids.forEach((id) => {
      const el = document.getElementById(id);
      if (el) enhanceSearchableSelect(el);
    });
    document.querySelectorAll('.rr-draft-role-select').forEach((el) => {
      enhanceSearchableSelect(el);
    });
  }

  function populateDropdowns(welcomeCfg = {}, musicCfg = {}, aiCfg = {}) {
    const welcomeSelect = document.getElementById('select-welcome-channel');
    const rulesSelect = document.getElementById('select-rules-channel');
    const roleSelect = document.getElementById('select-role-channel');
    const autoRoleSelect = document.getElementById('select-auto-role');
    const musicSelect = document.getElementById('select-music-channel');
    const aiChannelSelect = document.getElementById('select-ai-channel');
    const modalRuleChannelSelect = document.getElementById('modal-rule-channel');
    const modalRrChannelSelect = document.getElementById('modal-rr-channel');

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
    fillChannels(aiChannelSelect, aiCfg.channel_id, '未綁定（所有文字頻道皆可使用）');
    fillChannels(modalRuleChannelSelect, '', '全伺服器所有文字頻道生效');
    fillChannels(modalRrChannelSelect, welcomeCfg.role_channel_id || '', '請選擇要發送領取訊息的文字頻道');

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

    // 填入 AI 智慧助手表單
    const aiEnabled = document.getElementById('ai-enabled');
    const aiModelSelect = document.getElementById('select-ai-model');
    const aiCustomWrap = document.getElementById('ai-custom-model-wrap');
    const aiCustomInput = document.getElementById('ai-custom-model-input');
    const aiInstruction = document.getElementById('ai-instruction');
    const aiTemp = document.getElementById('ai-temperature');
    const aiTokens = document.getElementById('ai-max-tokens');

    if (aiEnabled) aiEnabled.checked = aiCfg.is_enabled !== false;
    if (aiModelSelect) {
      const presetModels = [
        'auto',
        'gemini:gemini-2.5-flash-lite',
        'gemini:gemini-flash-latest',
        'nvidia:meta/llama-3.2-11b-vision-instruct',
        'mistral:ministral-8b-latest',
        'kilo:kilo-auto/free',
      ];
      const modelVal = aiCfg.model || 'auto';
      if (presetModels.includes(modelVal)) {
        aiModelSelect.value = modelVal;
        if (aiCustomWrap) aiCustomWrap.style.display = 'none';
        if (aiCustomInput) aiCustomInput.value = '';
      } else {
        aiModelSelect.value = 'custom';
        if (aiCustomWrap) aiCustomWrap.style.display = 'block';
        if (aiCustomInput) aiCustomInput.value = modelVal;
      }
    }
    const aiPayerSelect = document.getElementById('select-ai-payer');
    if (aiPayerSelect) {
      aiPayerSelect.innerHTML = '<option value="">-- 由發問者自行支付各自的 AI 幣（預設） --</option>';
      (currentMembers || []).forEach((m) => {
        const opt = document.createElement('option');
        opt.value = m.id;
        opt.textContent = `${m.name} (@${m.username || m.name})`;
        if (String(m.id) === String(aiCfg.payer_user_id)) opt.selected = true;
        aiPayerSelect.appendChild(opt);
      });
      if (aiCfg.payer_user_id && !Array.from(aiPayerSelect.options).some((o) => o.value === String(aiCfg.payer_user_id))) {
        const opt = document.createElement('option');
        opt.value = aiCfg.payer_user_id;
        opt.textContent = `指定使用者 (ID: ${aiCfg.payer_user_id})`;
        opt.selected = true;
        aiPayerSelect.appendChild(opt);
      }
    }
    if (aiInstruction) aiInstruction.value = aiCfg.instruction || '';
    if (aiTemp) aiTemp.value = aiCfg.temperature !== undefined ? aiCfg.temperature : 0.7;
    if (aiTokens) aiTokens.value = aiCfg.max_tokens !== undefined ? aiCfg.max_tokens : 1000;

    refreshAllSearchableSelects();
    savedFormState.welcome = getFormState(document.getElementById('form-welcome'));
    savedFormState.music = getFormState(document.getElementById('form-music'));
    savedFormState.ai = getFormState(document.getElementById('form-ai'));
    checkUnsavedChanges();
  }

  // ---------- 歡迎訊息設定儲存與清除 ----------
  const formWelcome = document.getElementById('form-welcome');
  if (formWelcome) {
    formWelcome.addEventListener('change', (e) => {
      if (e.target && e.target.classList && e.target.classList.contains('searchable-select-input')) return;
      checkUnsavedChanges();
    });
    formWelcome.addEventListener('input', (e) => {
      if (e.target && e.target.classList && e.target.classList.contains('searchable-select-input')) return;
      checkUnsavedChanges();
    });
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
          savedFormState.welcome = getFormState(formWelcome);
          checkUnsavedChanges();
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
              refreshAllSearchableSelects();
              savedFormState.welcome = getFormState(formWelcome);
              checkUnsavedChanges();
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
    formMusic.addEventListener('change', (e) => {
      if (e.target && e.target.classList && e.target.classList.contains('searchable-select-input')) return;
      checkUnsavedChanges();
    });
    formMusic.addEventListener('input', (e) => {
      if (e.target && e.target.classList && e.target.classList.contains('searchable-select-input')) return;
      checkUnsavedChanges();
    });
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
          savedFormState.music = getFormState(formMusic);
          checkUnsavedChanges();
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
              refreshAllSearchableSelects();
              savedFormState.music = getFormState(formMusic);
              checkUnsavedChanges();
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

  // ---------- AI 智慧助手與人設自訂 (AI Module) ----------
  const formAi = document.getElementById('form-ai');
  const selectAiModel = document.getElementById('select-ai-model');
  const aiCustomWrap = document.getElementById('ai-custom-model-wrap');

  if (selectAiModel && aiCustomWrap) {
    selectAiModel.addEventListener('change', () => {
      if (selectAiModel.value === 'custom') {
        aiCustomWrap.style.display = 'block';
        const inp = document.getElementById('ai-custom-model-input');
        if (inp) inp.focus();
      } else {
        aiCustomWrap.style.display = 'none';
      }
      checkUnsavedChanges();
    });
  }

  if (formAi) {
    formAi.addEventListener('change', (e) => {
      if (e.target && e.target.classList && e.target.classList.contains('searchable-select-input')) return;
      checkUnsavedChanges();
    });
    formAi.addEventListener('input', (e) => {
      if (e.target && e.target.classList && e.target.classList.contains('searchable-select-input')) return;
      checkUnsavedChanges();
    });
    formAi.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (!currentGuildId) return;
      if (!currentGuildAiAuthorized) {
        showToast('本伺服器尚未獲得 AI 封測授權，無法儲存或修改設定', 'error');
        return;
      }

      const modelSelectVal = document.getElementById('select-ai-model').value;
      const customModelVal = (document.getElementById('ai-custom-model-input').value || '').trim();
      const finalModel = modelSelectVal === 'custom' ? (customModelVal || 'auto') : modelSelectVal;

      const payerSelect = document.getElementById('select-ai-payer');
      const payload = {
        action: 'save',
        is_enabled: document.getElementById('ai-enabled').checked,
        model: finalModel,
        channel_id: document.getElementById('select-ai-channel').value || null,
        payer_user_id: payerSelect && payerSelect.value ? payerSelect.value : null,
        instruction: document.getElementById('ai-instruction').value,
        temperature: parseFloat(document.getElementById('ai-temperature').value) || 0.7,
        max_tokens: parseInt(document.getElementById('ai-max-tokens').value) || 1000,
      };

      try {
        const res = await fetch(`/api/panel/guild/${currentGuildId}/ai`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (res.ok) {
          savedFormState.ai = getFormState(formAi);
          checkUnsavedChanges();
          showToast(data.message || 'AI 設定已成功儲存！');
        } else {
          showToast(data.message || '儲存失敗', 'error');
        }
      } catch (err) {
        showToast('儲存請求發生網路錯誤', 'error');
      }
    });
  }

  const btnResetAi = document.getElementById('btn-reset-ai');
  if (btnResetAi) {
    btnResetAi.addEventListener('click', () => {
      if (!currentGuildId) return;
      if (!currentGuildAiAuthorized) {
        showToast('本伺服器尚未獲得 AI 封測授權，無法重設設定', 'error');
        return;
      }
      showConfirmModal({
        title: '⚠️ 確認重設 AI 設定',
        message: '確定要將此伺服器的「AI 智慧助手與人設」還原為系統預設值嗎？自訂的人設指令將會被清除。',
        confirmText: '確認重設',
        cancelText: '取消',
        onConfirm: async () => {
          try {
            const res = await fetch(`/api/panel/guild/${currentGuildId}/ai`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ action: 'reset' }),
            });
            const data = await res.json();
            if (res.ok && data.ai) {
              const ai = data.ai;
              document.getElementById('ai-enabled').checked = ai.is_enabled;
              document.getElementById('select-ai-model').value = ai.model || 'auto';
              if (aiCustomWrap) aiCustomWrap.style.display = 'none';
              document.getElementById('select-ai-channel').value = ai.channel_id || '';
              const payerSel = document.getElementById('select-ai-payer');
              if (payerSel) payerSel.value = ai.payer_user_id || '';
              document.getElementById('ai-instruction').value = ai.instruction || '';
              document.getElementById('ai-temperature').value = ai.temperature !== undefined ? ai.temperature : 0.7;
              document.getElementById('ai-max-tokens').value = ai.max_tokens !== undefined ? ai.max_tokens : 1000;
              refreshAllSearchableSelects();
              savedFormState.ai = getFormState(formAi);
              checkUnsavedChanges();
              showToast(data.message || '已還原為預設設定');
            } else {
              showToast(data.message || '重設失敗', 'error');
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
  const modalRuleRepliesList = document.getElementById('modal-rule-replies-list');
  const btnAddReplyBlock = document.getElementById('btn-add-reply-block');
  let currentRulesList = [];
  let ruleDraftReplies = [''];

  function renderRuleReplyBlocks() {
    if (!modalRuleRepliesList) return;
    if (!ruleDraftReplies || ruleDraftReplies.length === 0) {
      ruleDraftReplies = [''];
    }

    modalRuleRepliesList.innerHTML = '';
    ruleDraftReplies.forEach((replyText, idx) => {
      const item = document.createElement('div');
      item.className = 'reply-block-item';
      const removeBtnHtml = ruleDraftReplies.length > 1
        ? `<button type="button" class="btn-remove-reply-block" data-idx="${idx}">✕ 移除</button>`
        : '';

      item.innerHTML = `
        <div class="reply-block-top">
          <span class="reply-block-badge">回覆區塊 #${idx + 1}</span>
          ${removeBtnHtml}
        </div>
        <textarea class="form-control reply-block-textarea" data-idx="${idx}" rows="2" placeholder="輸入此則回覆內容（可直接按 Enter 換行）...">${escapeHtml(replyText)}</textarea>
      `;
      modalRuleRepliesList.appendChild(item);
    });

    modalRuleRepliesList.querySelectorAll('.reply-block-textarea').forEach((ta) => {
      ta.addEventListener('input', (e) => {
        const idx = parseInt(ta.getAttribute('data-idx'), 10);
        if (!Number.isNaN(idx)) {
          ruleDraftReplies[idx] = e.target.value;
        }
      });
    });

    modalRuleRepliesList.querySelectorAll('.btn-remove-reply-block').forEach((btn) => {
      btn.addEventListener('click', () => {
        const idx = parseInt(btn.getAttribute('data-idx'), 10);
        if (!Number.isNaN(idx) && ruleDraftReplies.length > 1) {
          ruleDraftReplies.splice(idx, 1);
          renderRuleReplyBlocks();
        }
      });
    });
  }

  if (btnAddReplyBlock) {
    btnAddReplyBlock.addEventListener('click', () => {
      if (ruleDraftReplies.length >= 10) {
        showToast('單條規則最多只能設定 10 則隨機回覆區塊', 'error');
        return;
      }
      ruleDraftReplies.push('');
      renderRuleReplyBlocks();
      if (modalRuleRepliesList) {
        const textareas = modalRuleRepliesList.querySelectorAll('.reply-block-textarea');
        const lastTa = textareas[textareas.length - 1];
        if (lastTa) lastTa.focus();
      }
    });
  }

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

      const repliesList = r.replies || [];
      const repliesHtml = repliesList
        .map((rep, idx) => `
          <div class="rule-reply-item">
            ${repliesList.length > 1 ? `<span class="rule-reply-idx">#${idx + 1}</span>` : ''}
            <div class="rule-reply-text">${escapeHtml(rep)}</div>
          </div>
        `)
        .join('');

      let channelName = '全伺服器生效';
      if (r.channel_id) {
        const matched = currentChannels.find((c) => String(c.id) === String(r.channel_id));
        channelName = matched ? `# ${matched.name}` : `頻道 ID: ${r.channel_id}`;
      }

      const statusBadge = r.is_paused
        ? '<span class="rule-status-badge paused">⏸️ 已暫停</span>'
        : '<span class="rule-status-badge active">● 啟用中</span>';

      card.innerHTML = `
        <div class="rule-card-header">
          <div class="rule-card-title-group">
            <span class="rule-title">${escapeHtml(r.name || '未命名規則')}</span>
            ${statusBadge}
            <span class="rule-scope-badge">📍 ${escapeHtml(channelName)}</span>
          </div>
          <div class="rule-actions-row">
            <button class="btn-icon-action btn-edit-rule" data-id="${r.id}" type="button">✏️ 編輯</button>
            <button class="btn-icon-action btn-toggle-rule" data-id="${r.id}" type="button">${r.is_paused ? '▶️ 啟用' : '⏸️ 暫停'}</button>
            <button class="btn-icon-action delete btn-del-rule" data-id="${r.id}" type="button">🗑️ 刪除</button>
          </div>
        </div>
        <div class="rule-card-body">
          <div class="rule-section-block">
            <span class="rule-section-label">🔑 觸發關鍵字（符合任一即觸發）</span>
            <div class="rule-keywords-row">
              ${keywordsHtml}
            </div>
          </div>
          <div class="rule-section-block">
            <span class="rule-section-label">💬 自動回覆內容${repliesList.length > 1 ? `（共 ${repliesList.length} 則隨機回覆）` : ''}</span>
            <div class="rule-replies-list">${repliesHtml}</div>
          </div>
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
    const chanEl = document.getElementById('modal-rule-channel');
    const pausedEl = document.getElementById('modal-rule-paused');

    if (ruleId) {
      const rule = currentRulesList.find((r) => String(r.id) === String(ruleId));
      if (!rule) return;
      titleEl.textContent = '編輯自動回覆規則';
      idEl.value = rule.id;
      nameEl.value = rule.name || '';
      kwEl.value = (rule.keywords || []).join(', ');
      ruleDraftReplies = (rule.replies && rule.replies.length > 0) ? [...rule.replies] : [''];
      chanEl.value = rule.channel_id ? String(rule.channel_id) : '';
      pausedEl.checked = Boolean(rule.is_paused);
    } else {
      titleEl.textContent = '新增自動回覆規則';
      idEl.value = '';
      nameEl.value = '';
      kwEl.value = '';
      ruleDraftReplies = [''];
      chanEl.value = '';
      pausedEl.checked = false;
    }

    renderRuleReplyBlocks();
    refreshAllSearchableSelects();
    modalAutoReply.style.display = 'flex';
    if (formModalAutoReply) formModalAutoReply.scrollTop = 0;
    syncModalScrollLock();
  }

  function closeModal() {
    if (modalAutoReply) modalAutoReply.style.display = 'none';
    syncModalScrollLock();
  }

  if (btnAddAutoReply) btnAddAutoReply.addEventListener('click', () => openEditRuleModal(null));
  if (modalCloseBtn) modalCloseBtn.addEventListener('click', closeModal);
  if (modalCancelBtn) modalCancelBtn.addEventListener('click', closeModal);
  if (modalAutoReply) {
    modalAutoReply.addEventListener('click', (e) => {
      if (e.target === modalAutoReply) closeModal();
    });
  }

  if (formModalAutoReply) {
    formModalAutoReply.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (!currentGuildId) return;

      const cleanReplies = ruleDraftReplies
        .map((r) => String(r || '').trim())
        .filter(Boolean);

      if (cleanReplies.length === 0) {
        showToast('請至少填寫一則自動回覆內容', 'error');
        return;
      }

      const ruleId = document.getElementById('modal-rule-id').value;
      const payload = {
        id: ruleId ? parseInt(ruleId) : null,
        name: document.getElementById('modal-rule-name').value.trim(),
        keywords: document.getElementById('modal-rule-keywords').value.trim(),
        replies: cleanReplies,
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

  function deleteRule(ruleId) {
    if (!currentGuildId || !ruleId) return;
    const rule = currentRulesList.find((r) => String(r.id) === String(ruleId));
    const ruleTitle = rule && rule.name ? `「${rule.name}」` : '這條';

    showConfirmModal({
      title: '⚠️ 確認刪除自動回覆規則',
      message: `確定要刪除${ruleTitle}自動回覆規則嗎？刪除後將無法復原。`,
      confirmText: '確認刪除',
      cancelText: '取消',
      onConfirm: async () => {
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
      },
    });
  }

  // ---------- 表符解析與表符身分組配置 (Reaction Roles) ----------
  function renderEmojiHtml(emojiStr) {
    const raw = String(emojiStr || '').trim();
    const match = raw.match(/^<(a?):([^:>]+):(\d+)>$/);
    if (match) {
      const isAnimated = match[1] === 'a';
      const id = match[3];
      const ext = isAnimated ? 'gif' : 'png';
      const url = `https://cdn.discordapp.com/emojis/${id}.${ext}?size=48&quality=lossless`;
      return `
        <span class="rr-emoji-wrap">
          <img class="rr-custom-emoji" src="${url}" alt="emoji" loading="lazy" />
        </span>
      `;
    }
    return `
      <span class="rr-emoji-wrap">
        <span class="rr-unicode-emoji">${escapeHtml(raw)}</span>
      </span>
    `;
  }

  function renderReactionRoles(messages) {
    const container = document.getElementById('reaction-roles-list-container');
    if (!container) return;

    if (!messages || messages.length === 0) {
      container.innerHTML = '<div class="empty-rules-hint">此伺服器目前尚未設定任何表符身分組。點擊上方「＋ 新增身分組訊息」即可直接建立並發送至 Discord 頻道！</div>';
      return;
    }

    container.innerHTML = '';
    messages.forEach((msg) => {
      const card = document.createElement('div');
      card.className = 'rr-card';

      const msgId = String(msg.message_id);
      const chanId = msg.channel_id ? String(msg.channel_id) : '';
      const channelObj = currentChannels.find((c) => String(c.id) === chanId);
      const channelLabel = channelObj ? `# ${channelObj.name}` : (chanId ? `頻道 ID: ${chanId}` : '未知頻道');
      const jumpUrl = currentGuildId && chanId && msgId
        ? `https://discord.com/channels/${currentGuildId}/${chanId}/${msgId}`
        : null;

      const pairsHtml = (msg.pairs || [])
        .map((p) => {
          const roleId = String(p.role_id);
          const roleObj = currentRoles.find((r) => String(r.id) === roleId);
          const roleName = roleObj ? roleObj.name : `身分組 ID: ${roleId}`;
          const roleColor = roleObj && roleObj.color ? roleObj.color : '#94a3b8';
          return `
            <div class="rr-pair-item">
              ${renderEmojiHtml(p.emoji)}
              <span class="rr-pair-arrow">→</span>
              <span class="rr-role-badge" style="color: ${roleColor};">
                <span class="rr-role-dot" style="background: ${roleColor};"></span>
                <span class="rr-role-text">@ ${escapeHtml(roleName)}</span>
              </span>
            </div>
          `;
        })
        .join('');

      const strictBadge = msg.strict
        ? '<span class="rr-meta-pill strict-on">🛡️ 僅限有效表符：開啟</span>'
        : '<span class="rr-meta-pill strict-off">🔓 僅限有效表符：關閉</span>';

      const jumpLinkHtml = jumpUrl
        ? `<a href="${jumpUrl}" target="_blank" rel="noopener noreferrer" class="rr-meta-pill" style="text-decoration: none; color: #93c5fd;">🔗 前往訊息</a>`
        : '';

      card.innerHTML = `
        <div class="rr-card-header">
          <div class="rr-meta-group">
            <span class="rr-meta-pill">📢 <strong>${escapeHtml(channelLabel)}</strong></span>
            <span class="rr-meta-pill">📨 訊息 ID：<code>${escapeHtml(msgId)}</code></span>
            ${strictBadge}
            ${jumpLinkHtml}
          </div>
          <div class="rr-actions-row">
            <button class="btn-icon-action btn-edit-rr" data-msg-id="${escapeHtml(msgId)}" type="button">✏️ 編輯配置</button>
            <button class="btn-icon-action btn-toggle-rr-strict" data-msg-id="${escapeHtml(msgId)}" type="button">${msg.strict ? '🔓 關閉限制' : '🛡️ 開啟限制'}</button>
            <button class="btn-icon-action delete btn-del-rr" data-msg-id="${escapeHtml(msgId)}" type="button">🗑️ 刪除訊息</button>
          </div>
        </div>
        <div class="rr-pairs-grid">
          ${pairsHtml || '<div class="empty-rules-hint">尚未設定任何表符與身分組配對</div>'}
        </div>
      `;
      container.appendChild(card);
    });

    container.querySelectorAll('.btn-edit-rr').forEach((btn) => {
      btn.addEventListener('click', () => openEditReactionRoleModal(btn.getAttribute('data-msg-id')));
    });
    container.querySelectorAll('.btn-toggle-rr-strict').forEach((btn) => {
      btn.addEventListener('click', () => toggleReactionRoleStrict(btn.getAttribute('data-msg-id')));
    });
    container.querySelectorAll('.btn-del-rr').forEach((btn) => {
      btn.addEventListener('click', () => deleteReactionRoleMessage(btn.getAttribute('data-msg-id')));
    });
  }

  // ---------- 表符身分組 新增 / 編輯 / 刪除 Modal 邏輯 ----------
  const btnAddReactionRole = document.getElementById('btn-add-reaction-role');
  const modalReactionRole = document.getElementById('modal-reaction-role');
  const modalRrTitle = document.getElementById('modal-rr-title');
  const modalRrClose = document.getElementById('modal-rr-close');
  const modalRrCancel = document.getElementById('modal-rr-cancel');
  const formModalRr = document.getElementById('form-modal-reaction-role');
  const modalRrMessageId = document.getElementById('modal-rr-message-id');
  const modalRrMetaInfo = document.getElementById('modal-rr-meta-info');
  const modalRrChannelGroup = document.getElementById('modal-rr-channel-group');
  const modalRrChannel = document.getElementById('modal-rr-channel');
  const modalRrStrict = document.getElementById('modal-rr-strict');
  const modalRrPairsList = document.getElementById('modal-rr-pairs-list');
  const modalRrNewEmoji = document.getElementById('modal-rr-new-emoji');
  const modalRrNewRole = document.getElementById('modal-rr-new-role');
  const btnRrAddPair = document.getElementById('btn-rr-add-pair');
  const modalRrEmojiPickerWrap = document.getElementById('modal-rr-emoji-picker-wrap');
  const modalRrEmojiPicker = document.getElementById('modal-rr-emoji-picker');
  const modalRrContent = document.getElementById('modal-rr-content');
  const modalRrAutoSyncText = document.getElementById('modal-rr-auto-sync-text');
  const btnModalRrSubmit = document.getElementById('btn-modal-rr-submit');

  let rrDraftPairs = [];

  function closeRrModal() {
    if (modalReactionRole) modalReactionRole.style.display = 'none';
    rrDraftPairs = [];
    syncModalScrollLock();
  }

  if (modalRrClose) modalRrClose.addEventListener('click', closeRrModal);
  if (modalRrCancel) modalRrCancel.addEventListener('click', closeRrModal);
  if (modalReactionRole) {
    modalReactionRole.addEventListener('click', (e) => {
      if (e.target === modalReactionRole) closeRrModal();
    });
  }

  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (document.querySelector('.searchable-select-wrap.is-open')) return;
    if (modalConfirm && modalConfirm.style.display === 'flex') {
      closeConfirmModal();
    } else if (modalReactionRole && modalReactionRole.style.display === 'flex') {
      closeRrModal();
    } else if (modalAutoReply && modalAutoReply.style.display === 'flex') {
      closeModal();
    }
  });

  function renderRrDraftPairs() {
    if (!modalRrPairsList) return;
    if (!rrDraftPairs || rrDraftPairs.length === 0) {
      modalRrPairsList.innerHTML = '<div class="empty-rules-hint" style="padding: 16px;">目前沒有任何配對，請在下方新增至少一組表符與身分組！</div>';
      return;
    }

    modalRrPairsList.innerHTML = '';
    rrDraftPairs.forEach((pair, idx) => {
      const row = document.createElement('div');
      row.className = 'rr-editor-pair-row';

      const roleOptionsHtml = currentRoles
        .map((r) => {
          const selected = String(r.id) === String(pair.role_id) ? 'selected' : '';
          return `<option value="${escapeHtml(String(r.id))}" ${selected}>@ ${escapeHtml(r.name)}</option>`;
        })
        .join('');

      // 若原角色不在清單中，保留該 ID 選項
      const hasRoleInList = currentRoles.some((r) => String(r.id) === String(pair.role_id));
      const fallbackOption = !hasRoleInList && pair.role_id
        ? `<option value="${escapeHtml(String(pair.role_id))}" selected>身分組 ID: ${escapeHtml(String(pair.role_id))}</option>`
        : '';

      row.innerHTML = `
        <div class="rr-editor-emoji-cell">
          ${renderEmojiHtml(pair.emoji)}
        </div>
        <span class="rr-pair-arrow">→</span>
        <div class="rr-editor-role-select">
          <select class="form-control rr-draft-role-select" data-idx="${idx}">
            ${fallbackOption}
            ${roleOptionsHtml}
          </select>
        </div>
        <button type="button" class="btn-icon-action delete btn-rr-remove-pair" data-idx="${idx}">✕ 移除</button>
      `;
      modalRrPairsList.appendChild(row);
    });

    modalRrPairsList.querySelectorAll('.rr-draft-role-select').forEach((sel) => {
      sel.addEventListener('change', (e) => {
        const idx = parseInt(sel.getAttribute('data-idx'), 10);
        if (rrDraftPairs[idx]) {
          rrDraftPairs[idx].role_id = e.target.value;
        }
      });
    });

    modalRrPairsList.querySelectorAll('.btn-rr-remove-pair').forEach((btn) => {
      btn.addEventListener('click', () => {
        const idx = parseInt(btn.getAttribute('data-idx'), 10);
        rrDraftPairs.splice(idx, 1);
        renderRrDraftPairs();
      });
    });

    refreshAllSearchableSelects();
  }

  function populateRrModalRoleAndEmojis() {
    if (modalRrNewRole) {
      modalRrNewRole.innerHTML = '<option value="">-- 選擇對應身分組 --</option>';
      currentRoles.forEach((r) => {
        const opt = document.createElement('option');
        opt.value = String(r.id);
        opt.textContent = `@ ${r.name}`;
        modalRrNewRole.appendChild(opt);
      });
    }
    refreshAllSearchableSelects();

    if (modalRrEmojiPicker && modalRrEmojiPickerWrap) {
      // 收集伺服器自訂表符以及現有設定中的自訂表符
      const emojiMap = new Map();
      currentEmojis.forEach((em) => {
        emojiMap.set(em.raw, em);
      });
      currentReactionRoles.forEach((msg) => {
        (msg.pairs || []).forEach((p) => {
          const m = String(p.emoji || '').match(/^<(a?):([^:>]+):(\d+)>$/);
          if (m && !emojiMap.has(p.emoji)) {
            const isAnim = m[1] === 'a';
            const name = m[2];
            const id = m[3];
            emojiMap.set(p.emoji, {
              id,
              name,
              raw: p.emoji,
              url: `https://cdn.discordapp.com/emojis/${id}.${isAnim ? 'gif' : 'png'}?size=48&quality=lossless`,
            });
          }
        });
      });

      const emojiList = Array.from(emojiMap.values());
      if (emojiList.length > 0) {
        modalRrEmojiPickerWrap.style.display = 'block';
        modalRrEmojiPicker.innerHTML = '';
        emojiList.forEach((em) => {
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'rr-picker-emoji-btn rr-emoji-chip-btn';
          btn.innerHTML = `<img src="${em.url}" alt="emoji" loading="lazy" />`;
          btn.addEventListener('click', () => {
            if (modalRrNewEmoji) {
              modalRrNewEmoji.value = em.raw;
              modalRrNewEmoji.focus();
            }
          });
          modalRrEmojiPicker.appendChild(btn);
        });
      } else {
        modalRrEmojiPickerWrap.style.display = 'none';
      }
    }
  }

  if (btnRrAddPair) {
    btnRrAddPair.addEventListener('click', () => {
      const emojiVal = modalRrNewEmoji ? modalRrNewEmoji.value.trim() : '';
      const roleVal = modalRrNewRole ? modalRrNewRole.value.trim() : '';

      if (!emojiVal) {
        showToast('請輸入或選擇一個表符', 'error');
        return;
      }
      if (!roleVal) {
        showToast('請選擇要綁定的身分組', 'error');
        return;
      }
      if (rrDraftPairs.length >= 20) {
        showToast('單一訊息最多只能設定 20 個表符配對', 'error');
        return;
      }
      if (rrDraftPairs.some((p) => p.emoji === emojiVal)) {
        showToast('此表符已經在配對清單中，請直接修改該列的身分組', 'error');
        return;
      }

      rrDraftPairs.push({ emoji: emojiVal, role_id: roleVal });
      if (modalRrNewEmoji) modalRrNewEmoji.value = '';
      if (modalRrNewRole) modalRrNewRole.value = '';
      renderRrDraftPairs();
      refreshAllSearchableSelects();
    });
  }

  function openCreateReactionRoleModal() {
    if (!modalReactionRole || !currentGuildId) return;

    if (modalRrTitle) modalRrTitle.textContent = '➕ 新增表符身分組訊息';
    if (modalRrMessageId) modalRrMessageId.value = '';
    if (modalRrMetaInfo) {
      modalRrMetaInfo.style.display = 'none';
      modalRrMetaInfo.innerHTML = '';
    }
    if (modalRrChannelGroup) modalRrChannelGroup.style.display = 'flex';
    if (modalRrStrict) modalRrStrict.checked = true;
    if (modalRrAutoSyncText) modalRrAutoSyncText.checked = true;
    if (modalRrContent) {
      modalRrContent.value = '';
      modalRrContent.placeholder = '可選填訊息開頭文字（預設為「點擊表符領取身分組！」），下方將自動依配對列出表符與身分組';
    }
    if (btnModalRrSubmit) {
      const span = btnModalRrSubmit.querySelector('span');
      if (span) span.textContent = '儲存設定';
    }

    rrDraftPairs = [];
    populateRrModalRoleAndEmojis();
    renderRrDraftPairs();
    modalReactionRole.style.display = 'flex';
    if (formModalRr) formModalRr.scrollTop = 0;
    syncModalScrollLock();
  }

  if (btnAddReactionRole) {
    btnAddReactionRole.addEventListener('click', openCreateReactionRoleModal);
  }

  async function openEditReactionRoleModal(messageId) {
    if (!modalReactionRole || !currentGuildId || !messageId) return;
    const msg = currentReactionRoles.find((m) => String(m.message_id) === String(messageId));
    if (!msg) return;

    const chanId = msg.channel_id ? String(msg.channel_id) : '';
    const channelObj = currentChannels.find((c) => String(c.id) === chanId);
    const channelLabel = channelObj ? `# ${channelObj.name}` : (chanId ? `頻道 ID: ${chanId}` : '未知頻道');

    if (modalRrTitle) modalRrTitle.textContent = '✏️ 編輯表符身分組配置';
    if (modalRrMessageId) modalRrMessageId.value = String(msg.message_id);
    if (modalRrChannelGroup) modalRrChannelGroup.style.display = 'none';
    if (modalRrStrict) modalRrStrict.checked = Boolean(msg.strict);
    if (modalRrAutoSyncText) modalRrAutoSyncText.checked = true;
    if (modalRrContent) {
      modalRrContent.value = '';
      modalRrContent.placeholder = '正在從 Discord 讀取現有訊息內文...';
    }
    if (modalRrMetaInfo) {
      modalRrMetaInfo.style.display = 'flex';
      modalRrMetaInfo.innerHTML = `
        <span class="rr-meta-pill">📢 頻道：<strong>${escapeHtml(channelLabel)}</strong></span>
        <span class="rr-meta-pill">📨 訊息 ID：<code>${escapeHtml(String(msg.message_id))}</code></span>
      `;
    }
    if (btnModalRrSubmit) {
      const span = btnModalRrSubmit.querySelector('span');
      if (span) span.textContent = '儲存設定';
    }

    rrDraftPairs = (msg.pairs || []).map((p) => ({
      emoji: String(p.emoji),
      role_id: String(p.role_id),
    }));

    populateRrModalRoleAndEmojis();
    renderRrDraftPairs();
    modalReactionRole.style.display = 'flex';
    if (formModalRr) formModalRr.scrollTop = 0;
    syncModalScrollLock();

    // 非同步向後端拉取現有 Discord 訊息內文
    try {
      const res = await fetch(`/api/panel/guild/${currentGuildId}/reaction-roles/message/${encodeURIComponent(String(messageId))}`);
      if (res.ok) {
        const data = await res.json();
        if (modalRrContent) {
          modalRrContent.value = data.content || '';
          modalRrContent.placeholder = '可在此編輯 Discord 訊息文字內容（留空則維持原訊息文字不變）';
        }
      } else if (modalRrContent) {
        modalRrContent.placeholder = '可在此輸入新的 Discord 訊息文字內容（留空則維持原訊息文字不變）';
      }
    } catch (err) {
      if (modalRrContent) {
        modalRrContent.placeholder = '可在此輸入新的 Discord 訊息文字內容（留空則維持原訊息文字不變）';
      }
    }
  }

  if (formModalRr) {
    formModalRr.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (!currentGuildId) return;

      const messageId = modalRrMessageId ? modalRrMessageId.value.trim() : '';
      const isCreateMode = !messageId;

      if (rrDraftPairs.length === 0) {
        showToast('請至少新增一組表符與身分組配對！', 'error');
        return;
      }

      if (isCreateMode) {
        const channelId = modalRrChannel ? modalRrChannel.value.trim() : '';
        if (!channelId) {
          showToast('請選擇要發送領取訊息的文字頻道', 'error');
          return;
        }

        const createPayload = {
          channel_id: channelId,
          strict: modalRrStrict ? modalRrStrict.checked : true,
          pairs: rrDraftPairs,
          message_content: modalRrContent && modalRrContent.value.trim() ? modalRrContent.value : null,
          auto_sync_text: modalRrAutoSyncText ? modalRrAutoSyncText.checked : true,
        };

        try {
          const res = await fetch(`/api/panel/guild/${currentGuildId}/reaction-roles/create`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(createPayload),
          });
          const data = await res.json();
          if (res.ok) {
            showToast(data.message || '已成功建立表符身分組訊息！');
            closeRrModal();
            loadGuildData(currentGuildId);
          } else {
            showToast(data.message || '建立失敗', 'error');
          }
        } catch (err) {
          showToast('網路請求失敗', 'error');
        }
        return;
      }

      const payload = {
        message_id: messageId,
        strict: modalRrStrict ? modalRrStrict.checked : false,
        pairs: rrDraftPairs,
        message_content: modalRrContent && modalRrContent.value.trim() ? modalRrContent.value : null,
        auto_sync_text: modalRrAutoSyncText ? modalRrAutoSyncText.checked : false,
      };

      try {
        const res = await fetch(`/api/panel/guild/${currentGuildId}/reaction-roles/update`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (res.ok) {
          showToast(data.message || '表符身分組設定已儲存！');
          closeRrModal();
          loadGuildData(currentGuildId);
        } else {
          showToast(data.message || '更新失敗', 'error');
        }
      } catch (err) {
        showToast('網路請求失敗', 'error');
      }
    });
  }

  async function toggleReactionRoleStrict(messageId) {
    if (!currentGuildId || !messageId) return;
    const msg = currentReactionRoles.find((m) => String(m.message_id) === String(messageId));
    if (!msg) return;

    try {
      const res = await fetch(`/api/panel/guild/${currentGuildId}/reaction-roles/update`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message_id: String(messageId),
          strict: !msg.strict,
        }),
      });
      const data = await res.json();
      if (res.ok) {
        showToast(data.message || '已切換表符限制模式');
        loadGuildData(currentGuildId);
      } else {
        showToast(data.message || '切換失敗', 'error');
      }
    } catch (err) {
      showToast('網路請求錯誤', 'error');
    }
  }

  function deleteReactionRoleMessage(messageId) {
    if (!currentGuildId || !messageId) return;
    showConfirmModal({
      title: '⚠️ 確認刪除表符身分組訊息',
      message: `確定要刪除訊息 ID (${messageId}) 的表符身分組設定嗎？此操作將清除資料庫綁定並一併刪除 Discord 頻道中的該則領取訊息，無法復原。`,
      confirmText: '確認刪除',
      cancelText: '取消',
      onConfirm: async () => {
        try {
          const res = await fetch(`/api/panel/guild/${currentGuildId}/reaction-roles/delete`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message_id: String(messageId), delete_discord_message: true }),
          });
          const data = await res.json();
          if (res.ok) {
            showToast(data.message || '已刪除表符身分組訊息');
            loadGuildData(currentGuildId);
          } else {
            showToast(data.message || '刪除失敗', 'error');
          }
        } catch (err) {
          showToast('網路請求錯誤', 'error');
        }
      },
    });
  }

  // ---------- 指令清單表格渲染與即時搜尋 ----------
  function renderCommandsTable(commands) {
    const container = document.getElementById('commands-table-container');
    if (!container) return;

    // 過濾掉系統管理員專用指令 (owner_guild_only)，一般伺服器指令清單僅顯示「所有成員」與「管理員」指令
    const visibleCommands = (commands || []).filter((c) => !c.owner_guild_only);

    if (visibleCommands.length === 0) {
      container.innerHTML = '<div class="empty-rules-hint">沒有符合條件的可用指令。</div>';
      return;
    }

    let html = `
      <table class="commands-table">
        <thead>
          <tr>
            <th>指令名稱</th>
            <th>功能說明</th>
            <th>權限限制</th>
          </tr>
        </thead>
        <tbody>
    `;

    visibleCommands.forEach((c) => {
      const isAdminPerm = c.permission === '管理員';
      const permBadge = isAdminPerm
        ? '<span class="perm-badge admin">管理員</span>'
        : '<span class="perm-badge member">所有成員</span>';

      html += `
        <tr>
          <td><span class="cmd-badge">${escapeHtml(c.name)}</span></td>
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
          !c.owner_guild_only &&
          (c.name.toLowerCase().includes(query) ||
            c.description.toLowerCase().includes(query) ||
            (c.permission && c.permission.toLowerCase().includes(query)))
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
      if (targetSub === 'owner-sub-ai-guilds') {
        loadOwnerAiGuilds();
      }
      checkUnsavedChanges();
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
      savedFormState.ownerEconomy = getFormState(form);
      checkUnsavedChanges();
    } catch (err) {
      console.error('無法讀取經濟後台設定:', err);
    }
  }

  const formOwnerEconomy = document.getElementById('form-owner-economy');
  if (formOwnerEconomy) {
    formOwnerEconomy.addEventListener('change', checkUnsavedChanges);
    formOwnerEconomy.addEventListener('input', checkUnsavedChanges);
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
          savedFormState.ownerEconomy = getFormState(formOwnerEconomy);
          checkUnsavedChanges();
          showToast(data.message || '經濟系統後台參數已成功套用！');
        } else {
          showToast(data.message || '更新失敗', 'error');
        }
      } catch (err) {
        showToast('網路請求發生錯誤', 'error');
      }
    });
  }

  if (btnUnsavedSave) {
    btnUnsavedSave.addEventListener('click', () => {
      const isOwnerMode = viewServerSettings && viewServerSettings.classList.contains('owner-mode');
      if (isOwnerMode) {
        if (formOwnerEconomy && isFormDirty(formOwnerEconomy, savedFormState.ownerEconomy)) {
          if (typeof formOwnerEconomy.requestSubmit === 'function') {
            formOwnerEconomy.requestSubmit();
          } else {
            formOwnerEconomy.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
          }
        }
        return;
      }

      if (formWelcome && isFormDirty(formWelcome, savedFormState.welcome)) {
        if (typeof formWelcome.requestSubmit === 'function') {
          formWelcome.requestSubmit();
        } else {
          formWelcome.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
        }
      }
      if (formMusic && isFormDirty(formMusic, savedFormState.music)) {
        if (typeof formMusic.requestSubmit === 'function') {
          formMusic.requestSubmit();
        } else {
          formMusic.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
        }
      }
      if (formAi && isFormDirty(formAi, savedFormState.ai)) {
        if (typeof formAi.requestSubmit === 'function') {
          formAi.requestSubmit();
        } else {
          formAi.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
        }
      }
    });
  }

  if (btnUnsavedReset) {
    btnUnsavedReset.addEventListener('click', () => {
      const isOwnerMode = viewServerSettings && viewServerSettings.classList.contains('owner-mode');
      if (isOwnerMode) {
        if (formOwnerEconomy && savedFormState.ownerEconomy) {
          applyFormState(formOwnerEconomy, savedFormState.ownerEconomy);
        }
      } else {
        if (formWelcome && savedFormState.welcome) {
          applyFormState(formWelcome, savedFormState.welcome);
        }
        if (formMusic && savedFormState.music) {
          applyFormState(formMusic, savedFormState.music);
        }
        if (formAi && savedFormState.ai) {
          applyFormState(formAi, savedFormState.ai);
          const sel = document.getElementById('select-ai-model');
          const wrap = document.getElementById('ai-custom-model-wrap');
          if (sel && wrap) {
            wrap.style.display = sel.value === 'custom' ? 'block' : 'none';
          }
        }
        refreshAllSearchableSelects();
      }
      checkUnsavedChanges();
      showToast('已重設尚未儲存的變更');
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
      const statAi = document.getElementById('stat-ai-points');
      if (statAi) statAi.textContent = (u.ai_points !== undefined ? u.ai_points : 0).toLocaleString();
      document.getElementById('stat-streak').textContent = `${u.daily_streak} 天`;
      document.getElementById('stat-earned').textContent = u.total_earned.toLocaleString();
      document.getElementById('stat-wagered').textContent = u.total_wagered.toLocaleString();

      if (userDetailsCard) userDetailsCard.style.display = 'block';
      showToast(`已取得使用者 ${uid} 的帳戶資訊`);
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

  async function modifyUserAiPoints(action) {
    if (!currentInspectedUserId) return;
    const amtInput = document.getElementById('modify-ai-amount-input');
    const amountVal = amtInput ? amtInput.value.trim() : '';
    if (!amountVal) {
      showToast('請輸入異動數量', 'error');
      return;
    }

    try {
      const res = await fetch('/api/panel/owner/economy-user', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          user_id: currentInspectedUserId,
          action: action,
          currency: 'ai',
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

  const btnSetAi = document.getElementById('btn-set-ai-points');
  const btnAddAi = document.getElementById('btn-add-ai-points');
  const btnDedAi = document.getElementById('btn-deduct-ai-points');

  if (btnSetAi) btnSetAi.addEventListener('click', () => modifyUserAiPoints('set'));
  if (btnAddAi) btnAddAi.addEventListener('click', () => modifyUserAiPoints('add'));
  if (btnDedAi) {
    btnDedAi.addEventListener('click', () => {
      const amtInput = document.getElementById('modify-ai-amount-input');
      const val = parseInt(amtInput ? amtInput.value : 0);
      if (val > 0) amtInput.value = -val;
      modifyUserAiPoints('add');
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
          reloadOutput.textContent = `✅ ${stripStatusEmoji(data.message)}\n指令同步狀態：${data.sync_summary}`;
        }
      } else {
        showToast(data.message || '重載失敗', 'error');
        if (reloadOutput) reloadOutput.textContent = `❌ ${stripStatusEmoji(data.message || '重載失敗')}`;
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
          if (reloadOutput) reloadOutput.textContent = `✅ ${stripStatusEmoji(data.message)}`;
        } else {
          showToast(data.message || '同步失敗', 'error');
          if (reloadOutput) reloadOutput.textContent = `❌ ${stripStatusEmoji(data.message || '同步失敗')}`;
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

  // 5. 擁有者後台：AI 伺服器授權管理
  let cachedOwnerAiGuilds = [];
  const ownerAiGuildsList = document.getElementById('owner-ai-guilds-list');
  const aiGuildSearchInput = document.getElementById('ai-guild-search-input');
  const btnRefreshAiGuilds = document.getElementById('btn-refresh-ai-guilds');

  async function loadOwnerAiGuilds() {
    if (!ownerAiGuildsList) return;
    try {
      const res = await fetch('/api/panel/owner/ai-guilds');
      if (res.status === 403) return;
      const data = await res.json();
      if (!res.ok || !data.guilds) {
        ownerAiGuildsList.innerHTML = '<div class="empty-rules-hint">載入伺服器授權名單失敗</div>';
        return;
      }
      cachedOwnerAiGuilds = data.guilds;
      renderOwnerAiGuilds(cachedOwnerAiGuilds);
    } catch (err) {
      console.error(err);
      if (ownerAiGuildsList) {
        ownerAiGuildsList.innerHTML = '<div class="empty-rules-hint">載入失敗，網路連線異常</div>';
      }
    }
  }

  function renderOwnerAiGuilds(guilds) {
    if (!ownerAiGuildsList) return;

    if (!guilds || guilds.length === 0) {
      ownerAiGuildsList.innerHTML = '<div class="empty-rules-hint">沒有找到符合條件的伺服器。</div>';
      return;
    }

    ownerAiGuildsList.innerHTML = '';
    guilds.forEach((g) => {
      const card = document.createElement('div');
      card.className = `owner-ai-guild-card ${g.is_owner_guild ? 'is-owner-guild' : (g.is_authorized ? 'is-authorized' : '')}`;

      const iconHtml = g.icon
        ? `<img class="owner-ai-guild-icon" src="${escapeHtml(g.icon)}" alt="${escapeHtml(g.name)}" />`
        : `<div class="owner-ai-guild-placeholder">${escapeHtml(g.name ? g.name.slice(0, 2) : 'SV')}</div>`;

      let badgeHtml = '';
      if (g.is_owner_guild) {
        badgeHtml = '<span class="owner-ai-badge owner">👑 擁有者伺服器 (預設開通)</span>';
      } else if (g.is_authorized) {
        badgeHtml = '<span class="owner-ai-badge authorized">🟢 已開通封測授權</span>';
      } else {
        badgeHtml = '<span class="owner-ai-badge unauthorized">⚪ 未授權</span>';
      }

      let actionHtml = '';
      if (g.is_owner_guild) {
        actionHtml = '<span style="font-size: 0.8rem; color: #94a3b8;">永久開通</span>';
      } else if (g.is_authorized) {
        actionHtml = `
          <button type="button" class="btn-ai-auth btn-revoke" data-gid="${escapeHtml(g.id)}" data-gname="${escapeHtml(g.name)}">
            <span>🚫 撤銷授權</span>
          </button>
        `;
      } else {
        actionHtml = `
          <button type="button" class="btn-ai-auth btn-grant" data-gid="${escapeHtml(g.id)}" data-gname="${escapeHtml(g.name)}">
            <span>✨ 發放授權</span>
          </button>
        `;
      }

      const memberCountText = g.member_count ? `${g.member_count} 名成員` : '機器人所在伺服器';

      card.innerHTML = `
        <div class="owner-ai-guild-left">
          ${iconHtml}
          <div class="owner-ai-guild-info">
            <div class="owner-ai-guild-title-row">
              <span class="owner-ai-guild-name" title="${escapeHtml(g.name)}">${escapeHtml(g.name)}</span>
              ${badgeHtml}
            </div>
            <div class="owner-ai-guild-meta">
              <span>ID: <code>${escapeHtml(g.id)}</code></span>
              <span>•</span>
              <span>${escapeHtml(memberCountText)}</span>
              ${g.notes ? `<span>•</span><span>備註: ${escapeHtml(g.notes)}</span>` : ''}
            </div>
          </div>
        </div>
        <div class="owner-ai-guild-right">
          ${actionHtml}
        </div>
      `;
      ownerAiGuildsList.appendChild(card);
    });

    // 綁定發放授權按鈕
    ownerAiGuildsList.querySelectorAll('.btn-grant').forEach((btn) => {
      btn.addEventListener('click', () => {
        const gid = btn.getAttribute('data-gid');
        const gname = btn.getAttribute('data-gname');
        showConfirmModal({
          title: '✨ 確認發放 AI 封測授權',
          message: `確定要為伺服器「${gname}」(${gid}) 開通 AI 智慧助手封測使用權限嗎？開通後該伺服器成員將可使用 /ai 指令與 @機器人 對話。`,
          confirmText: '確認開通',
          cancelText: '取消',
          onConfirm: () => handleAiGuildAuth(gid, 'authorize'),
        });
      });
    });

    // 綁定撤銷授權按鈕
    ownerAiGuildsList.querySelectorAll('.btn-revoke').forEach((btn) => {
      btn.addEventListener('click', () => {
        const gid = btn.getAttribute('data-gid');
        const gname = btn.getAttribute('data-gname');
        showConfirmModal({
          title: '⚠️ 確認撤銷 AI 封測授權',
          message: `確定要撤銷伺服器「${gname}」(${gid}) 的 AI 封測使用權限嗎？撤銷後該伺服器將無法再使用 AI 相關功能。`,
          confirmText: '確認撤銷',
          cancelText: '取消',
          onConfirm: () => handleAiGuildAuth(gid, 'revoke'),
        });
      });
    });
  }

  async function handleAiGuildAuth(guildId, action) {
    try {
      const res = await fetch('/api/panel/owner/ai-guilds', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          guild_id: guildId,
          action: action,
        }),
      });
      const data = await res.json();
      if (res.ok) {
        showToast(data.message);
        loadOwnerAiGuilds(); // 重新整理清單
      } else {
        showToast(data.message || '操作失敗', 'error');
      }
    } catch (err) {
      showToast('請求網路連線失敗', 'error');
    }
  }

  if (aiGuildSearchInput) {
    aiGuildSearchInput.addEventListener('input', (e) => {
      const q = e.target.value.toLowerCase().trim();
      if (!q) {
        renderOwnerAiGuilds(cachedOwnerAiGuilds);
        return;
      }
      const filtered = (cachedOwnerAiGuilds || []).filter((g) =>
        (g.name && g.name.toLowerCase().includes(q)) ||
        (g.id && String(g.id).includes(q))
      );
      renderOwnerAiGuilds(filtered);
    });
  }

  if (btnRefreshAiGuilds) {
    btnRefreshAiGuilds.addEventListener('click', () => {
      loadOwnerAiGuilds();
      showToast('已重新整理伺服器授權名單');
    });
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

  // ---------- 回到最頂端按鈕 ----------
  const backToTopBtn = document.getElementById('back-to-top');
  if (backToTopBtn) {
    backToTopBtn.addEventListener('mousemove', (e) => {
      const rect = backToTopBtn.getBoundingClientRect();
      const x = ((e.clientX - rect.left) / rect.width) * 100;
      const y = ((e.clientY - rect.top) / rect.height) * 100;
      backToTopBtn.style.setProperty('--mouse-x', `${x.toFixed(1)}%`);
      backToTopBtn.style.setProperty('--mouse-y', `${y.toFixed(1)}%`);
    });

    backToTopBtn.addEventListener('click', () => {
      window.scrollTo({ top: 0, behavior: 'smooth' });
    });

    const syncBackToTopVisibility = () => {
      const isVisible = window.scrollY > 120;
      backToTopBtn.classList.toggle('is-visible', isVisible);
      document.body.classList.toggle('has-back-to-top', isVisible);
    };

    window.addEventListener('scroll', syncBackToTopVisibility, { passive: true });
    syncBackToTopVisibility();
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

