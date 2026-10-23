/**
 * 炙彗JI機器人 - Bento Grid 個人 Profile 編輯器前端邏輯
 * 嚴格遵守規範：全純文字按鈕、無 Emoji、卡片文字直接點擊編輯、Apple Liquid Glass 風格
 */

(function () {
  'use strict';

  // ==================== 1. 全域變數與狀態管理 ====================
  const profileJsonEl = document.getElementById('profile-data-json');
  const economyJsonEl = document.getElementById('economy-data-json');

  let parsedProfile = {};
  let parsedEconomy = {};

  try {
    if (profileJsonEl && profileJsonEl.textContent) {
      parsedProfile = JSON.parse(profileJsonEl.textContent.trim());
    }
  } catch (e) {
    parsedProfile = window.PROFILE_DATA || {};
  }

  try {
    if (economyJsonEl && economyJsonEl.textContent) {
      parsedEconomy = JSON.parse(economyJsonEl.textContent.trim());
    }
  } catch (e) {
    parsedEconomy = window.BOT_ECONOMY_DATA || {};
  }

  const profileData = Object.assign({}, window.PROFILE_DATA || {}, parsedProfile);
  const economyData = Object.assign({}, window.BOT_ECONOMY_DATA || {}, parsedEconomy);

  let widgets = JSON.parse(JSON.stringify(profileData.layout_widgets || []));
  let themeConfig = Object.assign(
    {
      theme_id: 'sky',
      glow_color: '#7dd3fc',
      card_radius: '24px',
      bg_style: 'default',
    },
    profileData.theme_config || {}
  );

  const isOwner = Boolean(profileData.is_owner);
  const devMode = Boolean(profileData.dev_mode);
  const currentRole = profileData.current_role || 'owner';

  let isEditing = isOwner; // 擁有者預設為編輯模式
  let gridInstance = null;
  let editingCardId = null;
  let editingSocialLinkId = null;
  let pendingConfirmCallback = null;
  let isGridResizing = false;
  let isGridDragging = false;
  let draggedSocialBtn = null;
  let glassUpdateTimer = null;

  function debouncedUpdateGlassSections() {
    if (glassUpdateTimer) clearTimeout(glassUpdateTimer);
    glassUpdateTimer = setTimeout(() => {
      glassUpdateTimer = null;
      updateGlassSections();
    }, 40);
  }

  // ==================== 1.5 各卡片類型數量上限與顯示名稱 ====================
  const CARD_LIMITS = {
    profile_header: 1,
    bot_economy: 1,
    text_card: 5,
    social_link: 5,
    media_embed: 5,
  };

  const CARD_TYPE_NAMES = {
    profile_header: '名片區塊',
    bot_economy: '經濟數據',
    text_card: '文字區塊',
    social_link: '社群連結區塊',
    media_embed: '圖片或YouTube嵌入',
  };

  // ==================== 2. 各卡片最小與最大長寬限制 ====================
  function getWidgetSizeLimits(type) {
    switch (type) {
      case 'profile_header':
        return { minW: 4, maxW: 12, minH: 2, maxH: 6 };
      case 'bot_economy':
        return { minW: 3, maxW: 12, minH: 2, maxH: 4 };
      case 'text_card':
        return { minW: 3, maxW: 12, minH: 1, maxH: 20 };
      case 'social_link':
        return { minW: 3, maxW: 12, minH: 1, maxH: 20 };
      case 'media_embed':
        return { minW: 2, maxW: 12, minH: 2, maxH: 12 };
      case 'divider':
        return { minW: 1, maxW: 12, minH: 1, maxH: 6 };
      default:
        return { minW: 1, maxW: 12, minH: 1, maxH: 20 };
    }
  }

  // 檢查卡片是否支援內容自適應高度 (文字區塊與社群連結區塊)
  function isAutoHeightCard(widget) {
    if (!widget) return false;
    return widget.type === 'text_card' || widget.type === 'social_link';
  }

  function isMarkdownCard(widget) {
    return isAutoHeightCard(widget);
  }

  // ==================== 2.5 Markdown 解析與 HackMD 格式套用 ====================
  function renderMarkdown(md) {
    if (!md) return '';
    if (typeof marked !== 'undefined') {
      try {
        return marked.parse(md, { gfm: true, breaks: true });
      } catch (err) {
        console.warn('Markdown parse error:', err);
      }
    }
    return escapeHtml(md).replace(/\n/g, '<br />');
  }

  function applyMarkdownFormatting(textarea, format) {
    if (!textarea) return;
    const start = textarea.selectionStart;
    const end = textarea.selectionEnd;
    const val = textarea.value;
    const sel = val.substring(start, end);

    let replacement = '';
    let newCursorPos = 0;

    switch (format) {
      case 'bold':
        replacement = sel ? `**${sel}**` : `**粗體文字**`;
        newCursorPos = sel ? start + replacement.length : start + 2;
        break;
      case 'italic':
        replacement = sel ? `*${sel}*` : `*斜體文字*`;
        newCursorPos = sel ? start + replacement.length : start + 1;
        break;
      case 'strike':
        replacement = sel ? `~~${sel}~~` : `~~刪除線~~`;
        newCursorPos = sel ? start + replacement.length : start + 2;
        break;
      case 'heading':
        replacement = sel ? `\n## ${sel}\n` : `\n## 標題文字\n`;
        newCursorPos = start + replacement.length;
        break;
      case 'quote':
        replacement = sel ? `\n> ${sel}\n` : `\n> 引用佳句或註解\n`;
        newCursorPos = start + replacement.length;
        break;
      case 'code':
        if (sel.includes('\n') || sel.length > 25) {
          replacement = `\n\`\`\`\n${sel || '代碼區塊'}\n\`\`\`\n`;
        } else {
          replacement = sel ? `\`${sel}\`` : `\`代碼\``;
        }
        newCursorPos = start + replacement.length;
        break;
      case 'list':
        replacement = sel ? `\n- ${sel}` : `\n- 清單項目`;
        newCursorPos = start + replacement.length;
        break;
      case 'numlist':
        replacement = sel ? `\n1. ${sel}\n2. ` : `\n1. 項目一\n2. 項目二`;
        newCursorPos = start + replacement.length;
        break;
      case 'task':
        replacement = sel ? `\n- [ ] ${sel}` : `\n- [ ] 待辦任務`;
        newCursorPos = start + replacement.length;
        break;
      case 'link':
        replacement = sel ? `[${sel}](https://)` : `[連結文字](https://)`;
        newCursorPos = start + replacement.length - 1;
        break;
      case 'image':
        replacement = sel ? `![${sel}](https://)` : `![圖片說明](https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?auto=format&fit=crop&w=800&q=80)`;
        newCursorPos = start + replacement.length;
        break;
      case 'table':
        replacement = `\n\n| 欄位 1 | 欄位 2 |\n| :--- | :--- |\n| 項目 A | 項目 B |\n\n`;
        newCursorPos = start + replacement.length;
        break;
      case 'divider':
        replacement = `\n\n---\n\n`;
        newCursorPos = start + replacement.length;
        break;
      case 'comment':
        replacement = sel ? `\n> [!NOTE]\n> ${sel}\n` : `\n> [!NOTE]\n> 筆記內容或重要備註\n`;
        newCursorPos = start + replacement.length;
        break;
      default:
        return;
    }

    textarea.focus();
    if (typeof textarea.setRangeText === 'function') {
      textarea.setRangeText(replacement, start, end, 'end');
    } else {
      textarea.value = val.substring(0, start) + replacement + val.substring(end);
    }

    if (newCursorPos) {
      textarea.setSelectionRange(newCursorPos, newCursorPos);
    }

    const event = new Event('input', { bubbles: true });
    textarea.dispatchEvent(event);
  }

  // ==================== 2.6 HackMD 快捷工具列通用 HTML ====================
  function renderMarkdownToolbarHtml(widgetId) {
    return `
      <div class="markdown-quick-toolbar-wrap">
        <div class="markdown-quick-toolbar" data-widget-id="${escapeHtml(widgetId)}">
          <div class="md-tool-group">
            <button type="button" class="md-tool-btn" data-format="bold" title="粗體 (**文字**)"><span class="icon-txt icon-bold">B</span></button>
            <button type="button" class="md-tool-btn" data-format="italic" title="斜體 (*文字*)"><span class="icon-txt icon-italic">I</span></button>
            <button type="button" class="md-tool-btn" data-format="strike" title="刪除線 (~~文字~~)"><span class="icon-txt icon-strike">S</span></button>
            <button type="button" class="md-tool-btn" data-format="heading" title="標題 (## 標題)"><span class="icon-txt icon-heading">H</span></button>
          </div>
          <span class="md-toolbar-sep"></span>
          <div class="md-tool-group">
            <button type="button" class="md-tool-btn" data-format="code" title="代碼 (\`\`\`代碼\`\`\`)"><span class="icon-txt icon-code">&lt;/&gt;</span></button>
            <button type="button" class="md-tool-btn" data-format="quote" title="引用 (> 引用)"><span class="icon-txt icon-quote">99</span></button>
            <button type="button" class="md-tool-btn" data-format="list" title="無序清單 (- 項目)"><svg viewBox="0 0 24 24"><line x1="9" y1="6" x2="20" y2="6"></line><line x1="9" y1="12" x2="20" y2="12"></line><line x1="9" y1="18" x2="20" y2="18"></line><circle cx="4" cy="6" r="1.5" fill="currentColor"></circle><circle cx="4" cy="12" r="1.5" fill="currentColor"></circle><circle cx="4" cy="18" r="1.5" fill="currentColor"></circle></svg></button>
            <button type="button" class="md-tool-btn" data-format="numlist" title="有序清單 (1. 項目)"><svg viewBox="0 0 24 24"><line x1="10" y1="6" x2="21" y2="6"></line><line x1="10" y1="12" x2="21" y2="12"></line><line x1="10" y1="18" x2="21" y2="18"></line><path d="M4 6h1v4" stroke-width="1.8"></path><path d="M4 14h2a1 1 0 0 1 1 1v1a1 1 0 0 1-1 1H4v1h3" stroke-width="1.8"></path></svg></button>
            <button type="button" class="md-tool-btn" data-format="task" title="待辦任務 (- [ ] 事項)"><svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="3"></rect><polyline points="8 12 11 15 16 9"></polyline></svg></button>
          </div>
          <span class="md-toolbar-sep"></span>
          <div class="md-tool-group">
            <button type="button" class="md-tool-btn" data-format="link" title="超連結 ([文字](網址))"><svg viewBox="0 0 24 24"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"></path><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"></path></svg></button>
            <button type="button" class="md-tool-btn" data-format="image" title="插入圖片 (![說明](網址))"><svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect><circle cx="8.5" cy="8.5" r="1.5" fill="currentColor"></circle><polyline points="21 15 16 10 5 21"></polyline></svg></button>
            <button type="button" class="md-tool-btn" data-format="table" title="插入表格 (| 欄位 |)"><svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2"></rect><line x1="3" y1="9" x2="21" y2="9"></line><line x1="3" y1="15" x2="21" y2="15"></line><line x1="12" y1="3" x2="12" y2="21"></line></svg></button>
            <button type="button" class="md-tool-btn" data-format="divider" title="插入分隔線 (---)"><svg viewBox="0 0 24 24"><line x1="3" y1="12" x2="21" y2="12"></line></svg></button>
            <button type="button" class="md-tool-btn" data-format="comment" title="提示與備註 (> [!NOTE])"><svg viewBox="0 0 24 24"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path></svg></button>
          </div>
        </div>
      </div>
    `;
  }

  // ==================== 2.7 社群平台圖示庫 (支援 Threads, Spotify, GitHub, YouTube, Discord 等) ====================
  function getPlatformSvg(platform) {
    const p = String(platform || '').toLowerCase();
    switch (p) {
      case 'threads':
        return `<svg viewBox="0 0 192 192" width="18" height="18" fill="currentColor"><path d="M141.537 88.9883C140.71 88.5919 139.87 88.2104 139.019 87.8451C137.537 60.5382 122.616 44.905 97.5619 44.745C97.4484 44.7443 97.3355 44.7443 97.222 44.7443C82.2364 44.7443 69.7731 51.1409 62.102 62.7807L75.881 72.2328C81.6116 63.5383 90.6052 61.6848 97.2286 61.6848C97.3051 61.6848 97.3819 61.6848 97.4576 61.6855C105.707 61.7381 111.932 64.1366 115.961 68.814C118.893 72.2193 120.854 76.925 121.825 82.8638C114.511 81.6207 106.601 81.2385 98.145 81.7233C74.3247 83.0954 59.0111 96.9879 60.0396 116.292C60.5615 126.084 65.4397 134.508 73.775 140.011C80.8224 144.663 89.899 146.938 99.3323 146.423C111.79 145.74 121.563 140.987 128.381 132.296C133.559 125.696 136.834 117.143 138.28 106.366C144.217 109.949 148.617 114.664 151.047 120.332C155.179 129.967 155.42 145.8 142.501 158.708C131.182 170.016 117.576 174.908 97.0135 175.059C74.2042 174.89 56.9538 167.575 45.7381 153.317C35.2355 139.966 29.8077 120.682 29.6052 96C29.8077 71.3178 35.2355 52.0336 45.7381 38.6827C56.9538 24.4249 74.2039 17.11 97.0132 16.9405C119.988 17.1113 137.539 24.4614 149.184 38.788C154.894 45.8136 159.199 54.6488 162.037 64.9503L178.184 60.6422C174.744 47.9622 169.331 37.0357 161.965 27.974C147.036 9.60668 125.202 0.195148 97.0695 0H96.9569C68.8816 0.19447 47.2921 9.6418 32.7883 28.0793C19.8819 44.4864 13.2244 67.3157 13.0007 95.9325L13 96L13.0007 96.0675C13.2244 124.684 19.8819 147.514 32.7883 163.921C47.2921 182.358 68.8816 191.806 96.9569 192H97.0695C122.03 191.827 139.624 185.292 154.118 170.811C173.081 151.866 172.51 128.119 166.26 113.541C161.776 103.087 153.227 94.5962 141.537 88.9883ZM98.4405 129.507C88.0005 130.095 77.1544 125.409 76.6196 115.372C76.2232 107.93 81.9158 99.626 99.0812 98.6368C101.047 98.5234 102.976 98.468 104.871 98.468C111.106 98.468 116.939 99.0737 122.242 100.233C120.264 124.935 108.662 128.946 98.4405 129.507Z"/></svg>`;
      case 'steam':
        return `<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M11.979 0C5.678 0 .511 4.86.022 11.037l6.432 2.658c.545-.371 1.203-.59 1.912-.59.063 0 .125.004.188.006l2.861-4.142V8.91c0-2.495 2.028-4.524 4.524-4.524 2.494 0 4.524 2.031 4.524 4.527s-2.03 4.525-4.524 4.525h-.105l-4.076 2.911c0 .052.004.105.004.159 0 1.875-1.515 3.396-3.39 3.396-1.635 0-3.016-1.173-3.331-2.727L.632 15.938C2.52 20.59 7.042 24 11.979 24c6.627 0 12-5.373 12-12s-5.373-12-12-12zm4.524 8.91c-1.047 0-1.898.851-1.898 1.898 0 1.047.851 1.898 1.898 1.898 1.047 0 1.898-.851 1.898-1.898 0-1.047-.851-1.898-1.898-1.898z"/></svg>`;
      case 'spotify':
        return `<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M12 2C6.477 2 2 6.477 2 12s4.477 10 10 10 10-4.477 10-10S17.523 2 12 2zm4.586 14.424c-.18.295-.563.387-.857.207-2.35-1.434-5.308-1.758-8.793-.963-.335.077-.67-.133-.746-.469-.077-.334.132-.67.467-.747 3.808-.871 7.076-.496 9.721 1.117.295.18.388.563.208.855zm1.226-2.724c-.227.367-.71.482-1.077.254-2.69-1.652-6.79-2.131-9.97-1.165-.413.125-.849-.107-.974-.52-.125-.413.108-.849.52-.974 3.632-1.102 8.147-.568 11.248 1.328.366.226.481.71.253 1.077zm.106-2.835C14.692 8.85 9.375 8.673 6.297 9.608c-.494.15-1.022-.129-1.171-.623-.15-.494.129-1.022.623-1.171 3.536-1.073 9.404-.863 13.115 1.34.444.263.59.84.327 1.284-.264.444-.841.59-1.284.327z"/></svg>`;
      case 'github':
        return `<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path fill-rule="evenodd" clip-rule="evenodd" d="M12 2C6.477 2 2 6.484 2 12.017c0 4.425 2.865 8.18 6.839 9.504.5.092.682-.217.682-.483 0-.237-.008-.868-.013-1.703-2.782.605-3.369-1.343-3.369-1.343-.454-1.158-1.11-1.466-1.11-1.466-.908-.62.069-.608.069-.608 1.003.07 1.53 1.032 1.53 1.032.892 1.53 2.341 1.088 2.91.832.092-.647.35-1.088.636-1.338-2.22-.253-4.555-1.113-4.555-4.951 0-1.093.39-1.988 1.029-2.688-.103-.253-.446-1.272.098-2.65 0 0 .84-.27 2.75 1.026A9.564 9.564 0 0112 6.844c.85.004 1.705.115 2.504.337 1.909-1.296 2.747-1.027 2.747-1.027.546 1.379.202 2.398.1 2.651.64.7 1.028 1.595 1.028 2.688 0 3.848-2.339 4.695-4.566 4.943.359.309.678.92.678 1.855 0 1.338-.012 2.419-.012 2.747 0 .268.18.58.688.482A10.019 10.019 0 0022 12.017C22 6.484 17.522 2 12 2z"/></svg>`;
      case 'youtube':
        return `<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M23.498 6.186a3.016 3.016 0 0 0-2.122-2.136C19.505 3.545 12 3.545 12 3.545s-7.505 0-9.377.505A3.017 3.017 0 0 0 .502 6.186C0 8.07 0 12 0 12s0 3.93.502 5.814a3.016 3.016 0 0 0 2.122 2.136c1.871.505 9.376.505 9.376.505s7.505 0 9.377-.505a3.015 3.015 0 0 0 2.122-2.136C24 15.93 24 12 24 12s0-3.93-.502-5.814zM9.545 15.568V8.432L15.818 12l-6.273 3.568z"/></svg>`;
      case 'twitter':
      case 'x':
        return `<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z"/></svg>`;
      case 'discord':
        return `<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M20.317 4.37a19.791 19.791 0 0 0-4.885-1.515.074.074 0 0 0-.079.037c-.21.375-.444.864-.608 1.25a18.27 18.27 0 0 0-5.487 0 12.64 12.64 0 0 0-.617-1.25.077.077 0 0 0-.079-.037A19.736 19.736 0 0 0 3.677 4.37a.07.07 0 0 0-.032.027C.533 9.046-.32 13.58.099 18.057a.082.082 0 0 0 .031.057 19.9 19.9 0 0 0 5.993 3.03.078.078 0 0 0 .084-.028c.462-.63.874-1.295 1.226-1.994.021-.041.001-.09-.041-.106a13.107 13.107 0 0 1-1.872-.892.077.077 0 0 1-.008-.128 10.2 10.2 0 0 0 .372-.292.074.074 0 0 1 .077-.01c3.929 1.793 8.18 1.793 12.061 0a.074.074 0 0 1 .078.01c.12.098.246.198.373.292a.077.077 0 0 1-.006.127 12.299 12.299 0 0 1-1.873.894.077.077 0 0 0-.041.107c.36.698.772 1.362 1.225 1.993a.076.076 0 0 0 .084.028 19.839 19.839 0 0 0 6.002-3.03.077.077 0 0 0 .032-.054c.5-5.177-.838-9.674-3.549-13.66a.061.061 0 0 0-.031-.028zM8.02 15.33c-1.183 0-2.157-1.085-2.157-2.419 0-1.333.956-2.419 2.157-2.419 1.21 0 2.176 1.096 2.157 2.42 0 1.333-.956 2.418-2.157 2.418zm7.975 0c-1.183 0-2.157-1.085-2.157-2.419 0-1.333.955-2.419 2.157-2.419 1.21 0 2.176 1.096 2.157 2.42 0 1.333-.946 2.418-2.157 2.418z"/></svg>`;
      case 'instagram':
        return `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="20" height="20" rx="5" ry="5"></rect><path d="M16 11.37A4 4 0 1 1 12.63 8 4 4 0 0 1 16 11.37z"></path><line x1="17.5" y1="6.5" x2="17.51" y2="6.5"></line></svg>`;
      case 'twitch':
        return `<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M11.571 4.714h1.715v5.143H11.57zm4.715 0H18v5.143h-1.714zM6 0L1.714 4.286v15.428h5.143V24l4.286-4.286h3.428L22.286 12V0zm14.571 11.143l-3.428 3.428h-3.429l-3 3v-3H6.857V1.714h13.714z"/></svg>`;
      default:
        return `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><line x1="2" y1="12" x2="22" y2="12"></line><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"></path></svg>`;
    }
  }

  // ==================== 2.8 液態玻璃分區與剪刀分離運算 (24px 官網同款間距) ====================

  function syncWidgetCoordinatesFromGrid() {
    if (!gridInstance || !gridInstance.engine || !gridInstance.engine.nodes) return;
    gridInstance.engine.nodes.forEach((node) => {
      const target = widgets.find((w) => w.id === node.id);
      if (target) {
        target.x = node.x;
        target.y = node.y;
        target.w = node.w;
        target.h = node.h;
      }
    });
  }

  // 自動將 legacy divider 轉移至 themeConfig.section_splits
  function migrateLegacyDividers() {
    if (!Array.isArray(themeConfig.section_splits)) {
      themeConfig.section_splits = [];
    }
    const legacyDividers = widgets.filter((w) => w.type === 'divider');
    if (legacyDividers.length > 0) {
      legacyDividers.forEach((d) => {
        const cutY = (d.y !== undefined ? d.y : 0);
        if (cutY > 0 && !themeConfig.section_splits.includes(cutY)) {
          themeConfig.section_splits.push(cutY);
        }
      });
      widgets = widgets.filter((w) => w.type !== 'divider');
      themeConfig.section_splits.sort((a, b) => a - b);
    }
  }

  // 檢驗指定 Y 切點是否合法（切點絕不可切過任何卡片內部，且上下兩側均需有卡片）
  function isValidHorizontalCut(cutY, widgetsList) {
    if (cutY <= 0) return false;
    const slicesThrough = widgetsList.some((w) => {
      const wy = w.y !== undefined ? Number(w.y) : 0;
      const wh = Number(w.h) || 2;
      return wy < cutY && (wy + wh) > cutY;
    });
    if (slicesThrough) return false;

    const hasAbove = widgetsList.some((w) => {
      const wy = w.y !== undefined ? Number(w.y) : 0;
      const wh = Number(w.h) || 2;
      return (wy + wh) <= cutY;
    });
    const hasBelow = widgetsList.some((w) => {
      const wy = w.y !== undefined ? Number(w.y) : 0;
      return wy >= cutY;
    });

    return hasAbove && hasBelow;
  }

  // 檢驗指定 X 切點是否合法（切點絕不可切過該範圍內任何卡片內部，且左右兩側均需有卡片）
  function isValidVerticalCut(cutX, bandWidgets, minY = 0, maxY = 9999) {
    if (cutX <= 0 || cutX >= 12) return false;
    const relevantWidgets = bandWidgets.filter((w) => {
      const wy = w.y !== undefined ? Number(w.y) : 0;
      const wh = Number(w.h) || 2;
      return wy < maxY && (wy + wh) > minY;
    });
    if (relevantWidgets.length < 2) return false;

    const slicesThrough = relevantWidgets.some((w) => {
      const wx = w.x !== undefined ? Number(w.x) : 0;
      const ww = Number(w.w) || 1;
      return wx < cutX && (wx + ww) > cutX;
    });
    if (slicesThrough) return false;

    const hasLeft = relevantWidgets.some((w) => {
      const wx = w.x !== undefined ? Number(w.x) : 0;
      const ww = Number(w.w) || 1;
      return (wx + ww) <= cutX;
    });
    const hasRight = relevantWidgets.some((w) => {
      const wx = w.x !== undefined ? Number(w.x) : 0;
      return wx >= cutX;
    });

    return hasLeft && hasRight;
  }

  // 預覽/瀏覽模式：計算區塊內每張卡片的實際高度與因上方卡片留白而需上移的距離，消除卡片間與底部的無效空白
  let viewBottomById = {};

  function computeBandLayout(inBand, minY, cellHeight, onlySocial) {
    const px = {};
    const slack = {};
    inBand.forEach((w) => {
      const wh = Number(w.h) || 2;
      let hPx = wh * cellHeight;
      if (isAutoHeightCard(w) && (!onlySocial || w.type === 'social_link')) {
        const nodeEl = document.querySelector('.grid-stack-item[gs-id="' + w.id + '"]');
        if (nodeEl) {
          const actualPx = getWidgetActualContentHeight(w, nodeEl);
          const buffer = w.type === 'social_link' ? 28 : 14;
          const need = Math.ceil(actualPx + buffer + 8);
          if (need > 0 && need < hPx) hPx = need;
        }
      }
      px[w.id] = hPx;
      slack[w.id] = Math.max(0, wh * cellHeight - hPx);
    });

    const sorted = inBand.slice().sort((m, n) => (Number(m.y) || 0) - (Number(n.y) || 0));
    const shift = {};
    const newTop = {};
    sorted.forEach((w) => {
      const wx = Number(w.x) || 0;
      const ww = Number(w.w) || 1;
      const origTop = (Number(w.y) || 0) * cellHeight;
      let top = origTop;
      let constrained = false;
      sorted.forEach((u) => {
        if (u === w || newTop[u.id] === undefined) return;
        const ux = Number(u.x) || 0;
        const uw = Number(u.w) || 1;
        const uOrigBottom = ((Number(u.y) || 0) + (Number(u.h) || 2)) * cellHeight;
        const overlapX = ux < wx + ww && wx < ux + uw;
        if (!overlapX || uOrigBottom > origTop) return;
        // 保留原有間隙，但以上方卡片「實際」底部為準：所有重疊的上方卡片都必須避開，取最下方者
        const candidate = newTop[u.id] + px[u.id] + (origTop - uOrigBottom);
        if (!constrained || candidate > top) {
          top = candidate;
          constrained = true;
        }
      });
      newTop[w.id] = top;
      shift[w.id] = Math.max(0, origTop - top);
    });

    let bandPx = 0;
    inBand.forEach((w) => {
      const bottom = ((Number(w.y) || 0) - minY) * cellHeight - shift[w.id] + px[w.id];
      if (bottom > bandPx) bandPx = bottom;
    });
    return { px, shift, bandPx };
  }

  function updateGlassSections() {
    const backdropContainer = document.getElementById('grid-glass-backdrops');
    if (!backdropContainer) return;
    backdropContainer.innerHTML = '';

    syncWidgetCoordinatesFromGrid();
    migrateLegacyDividers();

    const activeWidgets = widgets.filter((w) => w.type !== 'divider');
    if (activeWidgets.length === 0) return;

    document.querySelectorAll('.grid-stack-item[gs-id]').forEach((el) => {
      el.style.height = '';
    });

    // 編輯模式：社群連結卡片外框高度隨連結編輯區域縮放 (格位不變，僅外框貼合內容，不影響拖曳座標)
    if (isEditing) {
      activeWidgets.forEach((w) => {
        if (w.type !== 'social_link') return;
        const el = document.querySelector('.grid-stack-item[gs-id="' + w.id + '"]');
        if (!el) return;
        const need = Math.ceil(getWidgetActualContentHeight(w, el) + 28 + 8);
        if (need > 0 && need < (Number(w.h) || 2) * 95) {
          el.style.height = need + 'px';
        }
      });
    }

    viewBottomById = {};
    const cellHeight = 95; // 緊湊型網格單元高度 (縮小空白區域)
    const pad = 12; // 增加各區塊左右預留空間 (12px 呼吸感)
    const sectionGap = 24; // 官網其他分頁標準區塊間距 (24px)

    // 自動過濾任何穿透卡片內部的非法切點
    if (!Array.isArray(themeConfig.section_splits)) {
      themeConfig.section_splits = [];
    }
    themeConfig.section_splits = themeConfig.section_splits
      .map(Number)
      .filter((y) => !isNaN(y) && isValidHorizontalCut(y, activeWidgets));
    themeConfig.section_splits.sort((a, b) => a - b);

    const splits = themeConfig.section_splits;

    if (!Array.isArray(themeConfig.section_splits_x)) {
      themeConfig.section_splits_x = [];
    }
    const splitsX = themeConfig.section_splits_x;

    // 依 splits 切分垂直區段 (Y Bands)
    const yBands = [];
    let curY = 0;

    splits.forEach((splitY) => {
      if (splitY > curY) {
        yBands.push({ startY: curY, endY: splitY });
      }
      curY = splitY;
    });

    const maxAllY = Math.max(...activeWidgets.map((w) => (w.y || 0) + (w.h || 2)), curY);
    if (maxAllY > curY) {
      yBands.push({ startY: curY, endY: maxAllY + 1 });
    }

    // 預覽與瀏覽模式：裁掉區塊底部因網格單位化而產生的無效空白 (自適應高度卡片依實際內容高度計算)
    let cumulativeTrimPx = 0;
    const bandTrimTotals = [];

    // 依 Band 渲染獨立之液態玻璃背板 (支援垂直與水平雙向分離)
    yBands.forEach((band, bandIdx) => {
      const inBand = activeWidgets.filter((w) => {
        const wy = w.y !== undefined ? w.y : 0;
        return wy >= band.startY && wy < band.endY;
      });

      if (inBand.length === 0) return;

      const minY = Math.min(...inBand.map((w) => (w.y !== undefined ? w.y : 0)));
      const maxY = Math.max(...inBand.map((w) => (w.y !== undefined ? w.y : 0) + (w.h || 2)));

      // 背板上下高度精準貼合該區塊所有卡片，內部卡片透過 inset: 5px 自然形成 5px 內縮邊距，絕不超出背板
      const bandShiftBefore = cumulativeTrimPx;
      const topPx = minY * cellHeight + (bandIdx * sectionGap) - bandShiftBefore;
      let heightPx = Math.max(70, (maxY - minY) * cellHeight);
      let bandTrim = 0;
      let bandLayout = null;
      {
        // 編輯模式僅社群連結卡片貼合內容；預覽/瀏覽模式所有自適應卡片貼合內容；卡片間空隙一律向上吸附
        bandLayout = computeBandLayout(inBand, minY, cellHeight, isEditing);
        const actualBandPx = bandLayout.bandPx;
        if (actualBandPx > 0 && actualBandPx < heightPx) {
          bandTrim = Math.max(0, heightPx - Math.max(70, actualBandPx));
          heightPx -= bandTrim;
        }
      }
      cumulativeTrimPx += bandTrim;
      bandTrimTotals[bandIdx] = cumulativeTrimPx;

      // 檢查此 Band 是否有啟用的左右向垂直分離 (X Splits)，並嚴格過濾不可切過卡片內部
      const bandActiveSplitsX = splitsX.filter((key) => {
        const parts = key.split('_');
        const cutY = parseInt(parts[1], 10);
        return !isNaN(cutY) && (cutY === band.startY || cutY === minY || (cutY >= band.startY && cutY < band.endY));
      });
      const splitCols = bandActiveSplitsX
        .map((k) => parseInt(k.split('_')[0], 10))
        .filter((x) => !isNaN(x) && x > 0 && x < 12 && isValidVerticalCut(x, inBand, minY, maxY));

      if (splitCols.length > 0) {
        // 存在左右分離：切分為多個獨立玻璃背板，間隔 24px (官網規範)
        const colCuts = [0, ...splitCols.sort((a, b) => a - b), 12];
        for (let i = 0; i < colCuts.length - 1; i++) {
          const startCol = colCuts[i];
          const endCol = colCuts[i + 1];
          const widgetsInColBand = inBand.filter((w) => {
            const wx = w.x !== undefined ? Number(w.x) : 0;
            return wx >= startCol && wx < endCol;
          });
          if (widgetsInColBand.length === 0) continue;

          // 背板寬度與位置精確對齊區塊內所有卡片實際涵蓋的欄位
          const minCol = Math.min(...widgetsInColBand.map((w) => (w.x !== undefined ? Number(w.x) : 0)));
          const maxCol = Math.max(...widgetsInColBand.map((w) => (w.x !== undefined ? Number(w.x) : 0) + (Number(w.w) || 1)));

          const leftPercent = (minCol / 12) * 100;
          const widthPercent = ((maxCol - minCol) / 12) * 100;

          const leftOffset = (minCol > 0 && splitCols.includes(minCol)) ? 12 : 0;
          const rightOffset = (maxCol < 12 && splitCols.includes(maxCol)) ? 12 : 0;

          const backdrop = document.createElement('div');
          backdrop.className = 'grid-section-glass-backdrop';
          backdrop.style.top = `${topPx}px`;
          backdrop.style.height = `${heightPx}px`;
          backdrop.style.left = `calc(${leftPercent}% + ${leftOffset}px)`;
          backdrop.style.width = `calc(${widthPercent}% - ${leftOffset + rightOffset}px)`;
          backdropContainer.appendChild(backdrop);

          widgetsInColBand.forEach((w) => {
            const itemEl = document.querySelector(`.grid-stack-item[gs-id="${w.id}"]`);
            if (itemEl) {
              const ty = (bandIdx > 0 ? bandIdx * sectionGap : 0) - bandShiftBefore - (bandLayout ? (bandLayout.shift[w.id] || 0) : 0);
              if (bandLayout && bandLayout.px[w.id] < (Number(w.h) || 2) * cellHeight) itemEl.style.height = bandLayout.px[w.id] + 'px';
              viewBottomById[w.id] = (Number(w.y) || 0) * cellHeight + ty + (bandLayout ? bandLayout.px[w.id] : (Number(w.h) || 2) * cellHeight);
              const tx = (minCol > 0 && splitCols.includes(minCol)) ? 12 : ((maxCol < 12 && splitCols.includes(maxCol)) ? -12 : 0);
              const transforms = [];
              if (ty) transforms.push(`translateY(${ty}px)`);
              if (tx) transforms.push(`translateX(${tx}px)`);
              itemEl.style.transform = transforms.join(' ');
            }
          });
        }
      } else {
        // 未分離：連成單一液態玻璃無縫大背板，背板寬度精準對齊區塊內所有卡片實際涵蓋的欄位寬度
        const minCol = Math.min(...inBand.map((w) => (w.x !== undefined ? Number(w.x) : 0)));
        const maxCol = Math.max(...inBand.map((w) => (w.x !== undefined ? Number(w.x) : 0) + (Number(w.w) || 1)));

        const leftPercent = (minCol / 12) * 100;
        const widthPercent = ((maxCol - minCol) / 12) * 100;

        const leftOffset = 0;
        const rightOffset = 0;

        const backdrop = document.createElement('div');
        backdrop.className = 'grid-section-glass-backdrop';
        backdrop.style.top = `${topPx}px`;
        backdrop.style.height = `${heightPx}px`;
        backdrop.style.left = `calc(${leftPercent}% + ${leftOffset}px)`;
        backdrop.style.width = `calc(${widthPercent}% - ${leftOffset + rightOffset}px)`;
        backdropContainer.appendChild(backdrop);

        inBand.forEach((w) => {
          const itemEl = document.querySelector(`.grid-stack-item[gs-id="${w.id}"]`);
          if (itemEl) {
            const tyAll = (bandIdx > 0 ? bandIdx * sectionGap : 0) - bandShiftBefore - (bandLayout ? (bandLayout.shift[w.id] || 0) : 0);
            if (bandLayout && bandLayout.px[w.id] < (Number(w.h) || 2) * cellHeight) itemEl.style.height = bandLayout.px[w.id] + 'px';
            viewBottomById[w.id] = (Number(w.y) || 0) * cellHeight + tyAll + (bandLayout ? bandLayout.px[w.id] : (Number(w.h) || 2) * cellHeight);
            itemEl.style.transform = tyAll ? `translateY(${tyAll}px)` : '';
          }
        });
      }
    });

    // 動態確保網格與編輯容器高度完整包覆所有平移卡片，杜絕下方界線超出或裁切問題
    let maxContentBottomPx = 0;
    activeWidgets.forEach((w) => {
      const b = viewBottomById[w.id] !== undefined ? viewBottomById[w.id] : (((Number(w.y) || 0) + (Number(w.h) || 2)) * cellHeight);
      if (b > maxContentBottomPx) {
        maxContentBottomPx = b;
      }
    });
    document.querySelectorAll('.grid-section-glass-backdrop').forEach((bd) => {
      const bTop = parseFloat(bd.style.top) || 0;
      const bH = parseFloat(bd.style.height) || 0;
      if (bTop + bH > maxContentBottomPx) {
        maxContentBottomPx = bTop + bH;
      }
    });

    const gridEl = document.getElementById('profile-grid');
    if (gridEl && maxContentBottomPx > 0) {
      gridEl.style.minHeight = `${maxContentBottomPx + pad * 2 + 32}px`;
    }

    // 渲染區塊間界剪刀圖案按鈕 (包含上下向與左右向)
    renderScissorCutBars(yBands, activeWidgets, cellHeight, sectionGap);
  }

  function renderScissorCutBars(yBands, activeWidgets, cellHeight, sectionGap) {
    const gridEl = document.getElementById('profile-grid');
    if (!gridEl) return;

    gridEl.querySelectorAll('.section-scissor-bar').forEach((el) => el.remove());

    if (!isEditing) return;

    const currentSplits = themeConfig.section_splits || [];
    const currentSplitsX = Array.isArray(themeConfig.section_splits_x) ? themeConfig.section_splits_x : [];

    // 1. 渲染上下向（水平）剪刀按鈕 (僅在合法且不穿透卡片的邊界處渲染)
    const possibleY = new Set();
    activeWidgets.forEach((w) => {
      const bottomY = (w.y !== undefined ? Number(w.y) : 0) + (Number(w.h) || 2);
      if (isValidHorizontalCut(bottomY, activeWidgets)) {
        possibleY.add(bottomY);
      }
    });
    currentSplits.forEach((s) => {
      if (isValidHorizontalCut(s, activeWidgets)) {
        possibleY.add(s);
      }
    });

    possibleY.forEach((cutY) => {
      const isSplit = currentSplits.includes(cutY);
      const priorSplits = currentSplits.filter((s) => s < cutY).length;
      let topPx = cutY * cellHeight + (priorSplits * sectionGap) - 12;
      const touching = activeWidgets.filter((w) => (Number(w.y) || 0) + (Number(w.h) || 2) === cutY && viewBottomById[w.id] !== undefined);
      if (touching.length > 0) {
        topPx = Math.max(...touching.map((w) => viewBottomById[w.id])) - 12;
      }

      const bar = document.createElement('div');
      bar.className = 'section-scissor-bar' + (isSplit ? ' is-split' : '');
      bar.style.top = `${topPx}px`;

      bar.innerHTML = `
        <button type="button" class="btn-scissor-split ${isSplit ? 'is-split' : ''}" data-cut-y="${cutY}" data-cut-dir="h" title="${isSplit ? '點擊合併上下區塊' : '點擊在此分離上下區塊 (官網 24px 間距)'}">
          <svg viewBox="0 0 24 24" class="scissor-svg"><circle cx="6" cy="6" r="3"></circle><circle cx="6" cy="18" r="3"></circle><line x1="20" y1="4" x2="8.12" y2="15.88"></line><line x1="14.47" y1="14.48" x2="20" y2="20"></line><line x1="8.12" y1="8.12" x2="12" y2="12"></line></svg>
          <span>${isSplit ? '合併區塊' : '分離區塊'}</span>
        </button>
      `;

      gridEl.appendChild(bar);
    });

    // 2. 渲染左右向（垂直）剪刀按鈕 (文字直式排列)
    yBands.forEach((band, bandIdx) => {
      const inBand = activeWidgets.filter((w) => {
        const wy = w.y !== undefined ? Number(w.y) : 0;
        return wy >= band.startY && wy < band.endY;
      });
      if (inBand.length < 2) return;

      // 按 Y 座標分組為列叢集 (Row Clusters)，讓不同列的卡片獨立判定左右切點，解決滿版名片卡阻擋下層垂直切點的問題
      const rowClusters = [];
      const sortedWidgets = [...inBand].sort((a, b) => (Number(a.y) || 0) - (Number(b.y) || 0));

      sortedWidgets.forEach((w) => {
        const wy = Number(w.y) || 0;
        const wh = Number(w.h) || 2;
        let cluster = rowClusters.find((c) => wy < c.maxY && (wy + wh) > c.minY);
        if (cluster) {
          cluster.widgets.push(w);
          cluster.minY = Math.min(cluster.minY, wy);
          cluster.maxY = Math.max(cluster.maxY, wy + wh);
        } else {
          rowClusters.push({
            minY: wy,
            maxY: wy + wh,
            widgets: [w],
          });
        }
      });

      rowClusters.forEach((cluster) => {
        if (cluster.widgets.length < 2) return;

        const clusterMinY = cluster.minY;
        const clusterMaxY = cluster.maxY;

        // 核心約束：垂直分離僅在上下皆無直接連接區塊時出現，避免分離邏輯衝突！
        // 1. 上方條件：為網格頂端 (y=0)、或上方存在水平分隔線、或上方無任何實體卡片直接貼合
        const hasNoTopConnection =
          clusterMinY === 0 ||
          currentSplits.includes(clusterMinY) ||
          !activeWidgets.some((w) => (Number(w.y) || 0) + (Number(w.h) || 2) === clusterMinY);

        // 2. 下方條件：下方存在水平分隔線、或下方無任何實體卡片直接貼合
        const hasNoBottomConnection =
          currentSplits.includes(clusterMaxY) ||
          !activeWidgets.some((w) => (Number(w.y) || 0) === clusterMaxY);

        if (!hasNoTopConnection || !hasNoBottomConnection) {
          // 上下有直接相連之區塊，必須先進行水平分離後方可左右分離，避免幾何衝突！
          return;
        }

        const topPx = clusterMinY * cellHeight + (bandIdx * sectionGap);
        const heightPx = (clusterMaxY - clusterMinY) * cellHeight;

        // 找出左右相鄰欄位合法交界處
        const possibleX = new Set();
        cluster.widgets.forEach((wA) => {
          const rightEdge = (Number(wA.x) || 0) + (Number(wA.w) || 1);
          if (isValidVerticalCut(rightEdge, inBand, clusterMinY, clusterMaxY)) {
            possibleX.add(rightEdge);
          }
        });

        currentSplitsX.forEach((k) => {
          const parts = k.split('_');
          const cutX = parseInt(parts[0], 10);
          const cutY = parseInt(parts[1], 10);
          if (cutY === clusterMinY || (cutY >= clusterMinY && cutY < clusterMaxY)) {
            if (isValidVerticalCut(cutX, inBand, clusterMinY, clusterMaxY)) {
              possibleX.add(cutX);
            }
          }
        });

        possibleX.forEach((cutX) => {
          const cutKey = `${cutX}_${clusterMinY}`;
          const isSplit = currentSplitsX.includes(cutKey);

          const vBar = document.createElement('div');
          vBar.className = 'section-scissor-bar is-vertical' + (isSplit ? ' is-split' : '');
          vBar.style.left = `calc(${(cutX / 12) * 100}% - 12px)`;
          vBar.style.top = `${topPx}px`;
          vBar.style.height = `${heightPx}px`;

          vBar.innerHTML = `
            <button type="button" class="btn-scissor-split is-vertical ${isSplit ? 'is-split' : ''}" data-cut-key="${cutKey}" data-cut-dir="v" title="${isSplit ? '點擊合併左右兩區塊' : '點擊在此分離左右兩區塊 (官網 24px 間距)'}">
              <svg viewBox="0 0 24 24" class="scissor-svg"><circle cx="6" cy="6" r="3"></circle><circle cx="6" cy="18" r="3"></circle><line x1="20" y1="4" x2="8.12" y2="15.88"></line><line x1="14.47" y1="14.48" x2="20" y2="20"></line><line x1="8.12" y1="8.12" x2="12" y2="12"></line></svg>
              <span class="scissor-v-txt">
                ${isSplit ? '<span>合</span><span>併</span>' : '<span>分</span><span>離</span>'}
              </span>
            </button>
          `;

          gridEl.appendChild(vBar);
        });
      });
    });
  }

  // ==================== 2.9 上下區塊自動磁吸壓實 (分隔線除外) ====================
  function compactWidgetsWithDividerBounds() {
    if (!gridInstance) return;
    const wasStatic = !!(gridInstance.opts && gridInstance.opts.staticGrid);
    if (wasStatic) {
      gridInstance.setStatic(false);
    }
    try {
      syncWidgetCoordinatesFromGrid();

      // 取得所有水平分隔界線 (包含 0 與排序後的 section_splits)
      const splits = Array.isArray(themeConfig.section_splits)
        ? [...themeConfig.section_splits].map(Number).filter((y) => !isNaN(y) && y > 0).sort((a, b) => a - b)
        : [];

      const bounds = [0, ...splits, 9999];
      let changed = false;

      // 依分隔線切分區間 (各區間內部自動向上吸附，但絕不越界穿透分隔線)
      for (let b = 0; b < bounds.length - 1; b++) {
        const minYBound = bounds[b];
        const maxYBound = bounds[b + 1];

        // 取得屬於該區間內的所有非分隔線卡片，按 y 座標由小至大排序
        const bandWidgets = widgets
          .filter((w) => {
            const wy = w.y !== undefined ? Number(w.y) : 0;
            return wy >= minYBound && wy < maxYBound && w.type !== 'divider';
          })
          .sort((a, b) => (Number(a.y) || 0) - (Number(b.y) || 0));

        // 記錄每欄目前已被卡片佔用的最高 y 座標
        const colMaxY = new Array(12).fill(minYBound);

        // 若區間內有實體分隔線卡片，納入邊界
        widgets.filter((w) => w.type === 'divider').forEach((d) => {
          const dy = Number(d.y) || 0;
          const dh = Number(d.h) || 1;
          const dx = Number(d.x) || 0;
          const dw = Number(d.w) || 12;
          if (dy >= minYBound && dy < maxYBound) {
            for (let c = dx; c < Math.min(12, dx + dw); c++) {
              colMaxY[c] = Math.max(colMaxY[c], dy + dh);
            }
          }
        });

        bandWidgets.forEach((w) => {
          const wx = Math.max(0, Math.min(11, Number(w.x) || 0));
          const ww = Math.max(1, Math.min(12 - wx, Number(w.w) || 1));
          const curY = Number(w.y) || 0;

          // 核心約束：名片卡 (profile_header) 永久置頂第一列 (y=0)，只能縮放禁止移位
          if (w.type === 'profile_header') {
            if (w.y !== 0) {
              w.y = 0;
              changed = true;
            }
            const nodeEl = document.querySelector(`.grid-stack-item[gs-id="${w.id}"]`);
            if (nodeEl) {
              if (nodeEl.getAttribute('gs-y') !== '0') {
                nodeEl.setAttribute('gs-y', '0');
                gridInstance.update(nodeEl, { y: 0, noMove: true });
              }
            }
            const cardH = Number(w.h) || 2;
            for (let c = wx; c < wx + ww; c++) {
              colMaxY[c] = Math.max(colMaxY[c], cardH);
            }
            return;
          }

          // 計算此卡片涵蓋欄位中，允許向上吸附的最高 Y 座標
          let targetY = minYBound;
          for (let c = wx; c < wx + ww; c++) {
            if (colMaxY[c] > targetY) {
              targetY = colMaxY[c];
            }
          }

          // 若 targetY 小於目前 y，表示上方有空行，執行向上吸附
          if (targetY < curY) {
            w.y = targetY;
            changed = true;
            const nodeEl = document.querySelector(`.grid-stack-item[gs-id="${w.id}"]`);
            if (nodeEl) {
              nodeEl.setAttribute('gs-y', String(targetY));
              gridInstance.update(nodeEl, { y: targetY });
            }
          }

          // 更新此卡片佔用的欄位高度
          const cardH = Number(w.h) || 2;
          for (let c = wx; c < wx + ww; c++) {
            colMaxY[c] = Math.max(colMaxY[c], (w.y || 0) + cardH);
          }
        });
      }

      if (changed) {
        updateGlassSections();
      }
    } finally {
      if (wasStatic) {
        gridInstance.setStatic(true);
      }
    }
  }

  // ==================== 3. 工具函式 (純文字 Toast 與安全剪貼簿) ====================

  function showToast(message, type = 'info', duration = 3000) {
    const container = document.getElementById('toast-container');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = `toast-item toast-${type}`;
    toast.textContent = message;

    container.appendChild(toast);

    setTimeout(() => {
      toast.style.opacity = '0';
      toast.style.transform = 'translateX(20px)';
      setTimeout(() => {
        if (toast.parentNode) {
          toast.parentNode.removeChild(toast);
        }
      }, 300);
    }, duration);
  }

  function copyToClipboard(text, triggerBtn, successText = '已複製連結', toastMsg = '已複製個人主頁連結至剪貼簿') {
    if (!text) return;

    function onCopySuccess() {
      if (triggerBtn) {
        if (triggerBtn.classList.contains('btn-icon-sq')) {
          triggerBtn.classList.add('copied');
          setTimeout(() => {
            triggerBtn.classList.remove('copied');
          }, 2000);
        } else {
          const originalText = triggerBtn.textContent;
          triggerBtn.textContent = successText;
          triggerBtn.classList.add('copied');

          setTimeout(() => {
            triggerBtn.textContent = originalText;
            triggerBtn.classList.remove('copied');
          }, 2000);
        }
      }
      if (toastMsg) {
        showToast(toastMsg, 'success');
      }
    }

    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard
        .writeText(text)
        .then(onCopySuccess)
        .catch(() => fallbackCopy(text, onCopySuccess));
    } else {
      fallbackCopy(text, onCopySuccess);
    }
  }

  function fallbackCopy(text, callback) {
    const textarea = document.createElement('textarea');
    textarea.value = text;
    textarea.style.position = 'fixed';
    textarea.style.top = '-9999px';
    textarea.style.left = '-9999px';
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    try {
      document.execCommand('copy');
      if (typeof callback === 'function') callback();
    } catch (err) {
      showToast('複製失敗，請手動複製', 'error');
    }
    document.body.removeChild(textarea);
  }

  function escapeHtml(str) {
    if (!str) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  function sanitizeUrl(url) {
    if (!url) return '#';
    const clean = String(url).trim();
    if (clean.startsWith('/') && !clean.startsWith('//')) {
      return clean;
    }
    const lower = clean.toLowerCase();
    if (lower.startsWith('http://') || lower.startsWith('https://')) {
      return clean;
    }
    return '#';
  }

  // ==================== 4. Panel 同款確認刪除 Modal ====================

  function showConfirmModal({ title, message, confirmText = '確認刪除', cancelText = '取消', onConfirm }) {
    const modalEl = document.getElementById('modal-confirm');
    const titleEl = document.getElementById('modal-confirm-title');
    const msgEl = document.getElementById('modal-confirm-message');
    const okBtn = document.getElementById('modal-confirm-ok');

    if (!modalEl) {
      if (window.confirm(message)) {
        if (typeof onConfirm === 'function') onConfirm();
      }
      return;
    }

    if (titleEl && title) titleEl.textContent = title;
    if (msgEl && message) msgEl.textContent = message;
    if (okBtn && confirmText) {
      const span = okBtn.querySelector('span');
      if (span) span.textContent = confirmText;
      else okBtn.textContent = confirmText;
    }

    pendingConfirmCallback = onConfirm;
    openModal('modal-confirm');
  }

  // ==================== 5. 網格卡片 HTML 生成器 ====================

  function renderCardInnerHtml(widget) {
    const type = widget.type;
    const content = widget.content || {};
    let html = '';

    // 統一拖曳握把：僅圖示、不重疊
    const dragHandleHtml = (isOwner && type !== 'profile_header') ? `
      <div class="card-drag-handle" data-widget-id="${escapeHtml(widget.id)}" title="按住拖曳調整順序 (手機版亦可點選箭頭移動)">
        <svg viewBox="0 0 24 24" class="drag-handle-svg" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="9" cy="6" r="1.5" fill="currentColor"></circle>
          <circle cx="9" cy="12" r="1.5" fill="currentColor"></circle>
          <circle cx="9" cy="18" r="1.5" fill="currentColor"></circle>
          <circle cx="15" cy="6" r="1.5" fill="currentColor"></circle>
          <circle cx="15" cy="12" r="1.5" fill="currentColor"></circle>
          <circle cx="15" cy="18" r="1.5" fill="currentColor"></circle>
        </svg>
      </div>
    ` : '';

    // 手機版快速上下移動卡片控制項
    const moveBtnsHtml = (isOwner && type !== 'profile_header') ? `
      <button type="button" class="card-control-btn btn-move-card btn-move-up" data-id="${escapeHtml(widget.id)}" title="向上移動卡片">▲</button>
      <button type="button" class="card-control-btn btn-move-card btn-move-down" data-id="${escapeHtml(widget.id)}" title="向下移動卡片">▼</button>
    ` : '';

    // 右上角按鈕：個人基礎資訊卡片禁用刪除，分隔線提供 90° 旋轉，外部連結保留設定
    let controlsHtml = '';
    if (isOwner) {
      if (type === 'profile_header') {
        controlsHtml = '';
      } else if (type === 'text_card') {
        controlsHtml = `
          <div class="card-controls">
            ${moveBtnsHtml}
            <button type="button" class="card-control-btn btn-delete card-control-btn-del" data-id="${escapeHtml(widget.id)}" title="刪除卡片">刪除</button>
          </div>
        `;
      } else if (type === 'divider') {
        controlsHtml = `
          <div class="card-controls">
            ${moveBtnsHtml}
            <button type="button" class="card-control-btn btn-delete card-control-btn-del" data-id="${escapeHtml(widget.id)}" title="刪除分隔線">刪除</button>
          </div>
        `;
      } else if (type === 'social_link') {
        controlsHtml = `
          <div class="card-controls">
            ${moveBtnsHtml}
            <button type="button" class="card-control-btn btn-add-social-btn" data-widget-id="${escapeHtml(widget.id)}" title="新增社群連結">+ 新增連結</button>
            <button type="button" class="card-control-btn btn-delete card-control-btn-del" data-id="${escapeHtml(widget.id)}">刪除</button>
          </div>
        `;
      } else if (type === 'media_embed') {
        const isImage = (content.media_type || 'image') === 'image';
        controlsHtml = `
          <div class="card-controls">
            ${moveBtnsHtml}
            <button type="button" class="card-control-btn btn-edit-card" data-id="${escapeHtml(widget.id)}">${isImage ? '圖片裁切與設定' : '設定媒體'}</button>
            <button type="button" class="card-control-btn btn-delete card-control-btn-del" data-id="${escapeHtml(widget.id)}">刪除</button>
          </div>
        `;
      } else {
        controlsHtml = `
          <div class="card-controls">
            ${moveBtnsHtml}
            <button type="button" class="card-control-btn btn-delete card-control-btn-del" data-id="${escapeHtml(widget.id)}">刪除</button>
          </div>
        `;
      }
    }

    if (type === 'profile_header') {
      const displayName = escapeHtml(content.display_name || profileData.display_name || profileData.username);
      const username = escapeHtml(content.username || profileData.username);
      const bio = escapeHtml(content.bio || profileData.bio || '熱愛開源開發與社群建造。正在打造次世代 Discord 機器人。');
      const avatarUrl = sanitizeUrl(content.avatar_url || profileData.avatar_url || '/static/logo.png');

      let tags = [];
      if (Array.isArray(content.hashtags)) {
        tags = content.hashtags;
      } else if (content.badge) {
        tags = [content.badge];
      } else {
        tags = ['炙彗社群', '次世代機器人'];
      }
      tags = tags.slice(0, 5);

      let tagsHtml = '';
      if (isEditing) {
        tagsHtml = `
          <div class="header-hashtags-list" data-widget-id="${escapeHtml(widget.id)}">
            ${tags.map((tag, idx) => `
              <span class="header-hashtag-pill is-editing">
                <span class="hashtag-hash">#</span>
                <span class="hashtag-text" contenteditable="true" data-inline-edit="hashtag" data-widget-id="${escapeHtml(widget.id)}" data-tag-index="${idx}" title="點擊自訂標籤文字">${escapeHtml(tag)}</span>
                <button type="button" class="btn-remove-tag" data-widget-id="${escapeHtml(widget.id)}" data-tag-index="${idx}" title="刪除此標籤">&times;</button>
              </span>
            `).join('')}
            ${tags.length < 5 ? `
              <button type="button" class="btn-add-hashtag" data-widget-id="${escapeHtml(widget.id)}" title="新增自訂標籤 (上限 5 個)">
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"></line><line x1="5" y1="12" x2="19" y2="12"></line></svg>
                新增標籤
              </button>
            ` : `
              <span class="hashtag-limit-hint">(已達上限 5 個)</span>
            `}
          </div>
        `;
      } else {
        tagsHtml = `
          <div class="header-hashtags-list">
            ${tags.map((tag) => `
              <span class="header-hashtag-pill">
                <span class="hashtag-hash">#</span>
                <span class="hashtag-text">${escapeHtml(tag)}</span>
              </span>
            `).join('')}
          </div>
        `;
      }

      html = `
        ${controlsHtml}
        <div class="widget-profile-header">
          <div class="header-main-row">
            <div class="header-avatar-wrap">
              <img class="header-avatar-img" src="${avatarUrl}" alt="${displayName}" onerror="this.src='/static/logo.png'" />
            </div>
            <div class="header-info">
              <div class="header-title-row">
                <h1 class="header-name" data-inline-edit="display_name" data-widget-id="${escapeHtml(widget.id)}" title="點擊直接修改名稱">${displayName}</h1>
                ${tagsHtml}
              </div>
              <div class="header-username">@${username}</div>
              <p class="header-bio" data-inline-edit="bio" data-widget-id="${escapeHtml(widget.id)}" title="點擊直接修改簡介">${bio}</p>
            </div>
          </div>
        </div>
      `;
    } else if (type === 'bot_economy') {
      const title = escapeHtml(content.title || '經濟數據');
      const balance = Number(economyData.balance || 0).toLocaleString();
      const streak = Number(economyData.daily_streak || 0);
      const earned = Number(economyData.total_earned || 0).toLocaleString();
      const goldRank = escapeHtml(String(economyData.gold_rank || '第 3 名'));

      html = `
        ${dragHandleHtml}
        ${controlsHtml}
        <div class="widget-bot-economy">
          <div class="economy-header">
            <h3 class="economy-title">${title}</h3>
            <span class="economy-tag">即時連動</span>
          </div>
          <div class="economy-stats-grid">
            <div class="economy-stat-item">
              <span class="stat-label">金幣餘額</span>
              <span class="stat-val val-gold">${balance}</span>
            </div>
            <div class="economy-stat-item">
              <span class="stat-label">連續簽到</span>
              <span class="stat-val val-streak">${streak}<span class="stat-val-unit">天</span></span>
            </div>
            <div class="economy-stat-item">
              <span class="stat-label">累計收益</span>
              <span class="stat-val val-earn">${earned}</span>
            </div>
            <div class="economy-stat-item">
              <span class="stat-label">金幣排行</span>
              <span class="stat-val val-rank">${goldRank}</span>
            </div>
          </div>
        </div>
      `;
    } else if (type === 'text_card') {
      const rawBody = content.body || '### 歡迎來到我的個人空間\n點此直接編輯內容，支援 **粗體**、*斜體*、清單與 Markdown 語法。';

      if (isEditing) {
        html = `
          ${dragHandleHtml}
          ${controlsHtml}
          <div class="widget-text-card is-editing-markdown">
            <div class="markdown-card-topbar" style="padding-left: 36px; padding-right: 120px;">
              <span class="markdown-card-tag">MARKDOWN 文字區塊</span>
            </div>
            ${renderMarkdownToolbarHtml(widget.id)}
            <div class="markdown-pane-container">
              <textarea class="markdown-textarea-input" data-widget-id="${escapeHtml(widget.id)}" placeholder="在此輸入 Markdown 內容...">${escapeHtml(rawBody)}</textarea>
            </div>
          </div>
        `;
      } else {
        html = `
          <div class="widget-text-card is-rendered">
            <div class="markdown-body markdown-rendered-content">${renderMarkdown(rawBody)}</div>
          </div>
        `;
      }
    } else if (type === 'divider') {
      const orientation = content.orientation || 'horizontal';
      if (isEditing) {
        html = `
          ${dragHandleHtml}
          ${controlsHtml}
          <div class="widget-divider-container is-editing orientation-${orientation}" data-widget-id="${escapeHtml(widget.id)}" title="短按旋轉 90° (切換水平 / 垂直)">
            <div class="divider-line-bar ${orientation}"></div>
            <span class="divider-orientation-label">短按旋轉 90° (${orientation === 'vertical' ? '垂直' : '水平'})</span>
          </div>
        `;
      } else {
        html = `
          <div class="widget-divider-container is-preview orientation-${orientation}" data-widget-id="${escapeHtml(widget.id)}">
            <div class="divider-line-bar ${orientation}"></div>
          </div>
        `;
      }
    } else if (type === 'social_link') {
      const title = escapeHtml(content.title || '社群連結');

      // 支援舊格式向下相容與多按鈕陣列解析
      let links = [];
      if (Array.isArray(content.links) && content.links.length > 0) {
        links = content.links;
      } else if (content.url || content.platform) {
        links = [
          {
            id: 'legacy-1',
            platform: content.platform || 'github',
            username: content.title || '前往專屬社群',
            url: content.url || '#',
            style: 'inline',
          }
        ];
      } else {
        links = [
          {
            id: 'l-threads',
            platform: 'threads',
            username: profileData.username || 'patricksu',
            url: 'https://threads.net',
            style: 'inline',
          },
          {
            id: 'l-spotify',
            platform: 'spotify',
            username: profileData.display_name || 'Spotify',
            url: 'https://spotify.com',
            style: 'inline',
          },
          {
            id: 'l-github',
            platform: 'github',
            username: profileData.username || 'patricksu',
            url: 'https://github.com',
            style: 'avatar',
          }
        ];
      }

      const buttonsHtml = links.map((link) => {
        const platform = link.platform || 'custom';
        const rawUName = link.username || '用戶名';
        const displayUName = rawUName.startsWith('@') ? rawUName : '@' + rawUName;
        const linkUrl = sanitizeUrl(link.url || '#');
        const style = link.style || 'inline';
        const usePlatformIcon = Boolean(link.use_platform_icon);
        const avatarUrl = usePlatformIcon ? '' : (link.avatar_url || '');

        if (style === 'avatar') {
          return `
            <div class="social-btn-wrap" data-link-id="${escapeHtml(link.id)}" data-widget-id="${escapeHtml(widget.id)}" draggable="${isEditing ? 'true' : 'false'}" style="position: relative; display: inline-flex;">
              <a href="${linkUrl}" target="_blank" rel="noopener noreferrer" draggable="false" class="social-btn-item social-btn-avatar platform-${escapeHtml(platform)}" title="${isEditing ? '點擊編輯此連結 (可拖曳排序)' : escapeHtml(platform) + ': ' + escapeHtml(displayUName)}">
                <div class="social-avatar-circle">
                  ${avatarUrl ? `<img src="${avatarUrl}" alt="${escapeHtml(displayUName)}" onerror="this.style.display='none'; if(this.nextElementSibling) this.nextElementSibling.style.display='inline-flex';" /><span class="social-avatar-svg" style="display:none;">${getPlatformSvg(platform)}</span>` : `<span class="social-avatar-svg">${getPlatformSvg(platform)}</span>`}
                </div>
                <span class="social-avatar-name">${escapeHtml(displayUName)}</span>
              </a>
              ${isEditing ? `<button type="button" class="btn-remove-social-btn" data-widget-id="${escapeHtml(widget.id)}" data-link-id="${escapeHtml(link.id)}" title="刪除此按鈕">&times;</button>` : ''}
            </div>
          `;
        } else {
          return `
            <div class="social-btn-wrap" data-link-id="${escapeHtml(link.id)}" data-widget-id="${escapeHtml(widget.id)}" draggable="${isEditing ? 'true' : 'false'}" style="position: relative; display: inline-flex;">
              <a href="${linkUrl}" target="_blank" rel="noopener noreferrer" draggable="false" class="social-btn-item social-btn-inline platform-${escapeHtml(platform)}" title="${isEditing ? '點擊編輯此連結 (可拖曳排序)' : escapeHtml(platform) + ': ' + escapeHtml(displayUName)}">
                <span class="social-btn-icon">${getPlatformSvg(platform)}</span>
                <span class="social-btn-username">${escapeHtml(displayUName)}</span>
              </a>
              ${isEditing ? `<button type="button" class="btn-remove-social-btn" data-widget-id="${escapeHtml(widget.id)}" data-link-id="${escapeHtml(link.id)}" title="刪除此按鈕">&times;</button>` : ''}
            </div>
          `;
        }
      }).join('');

      html = `
        ${dragHandleHtml}
        ${controlsHtml}
        <div class="widget-social-link">
          <div class="social-block-header">
            <h3 class="social-block-title" data-inline-edit="title" data-widget-id="${escapeHtml(widget.id)}" title="點擊直接修改區塊標題">${title}</h3>
          </div>
          <div class="social-buttons-container">
            ${buttonsHtml}
          </div>
        </div>
      `;
    } else if (type === 'media_embed') {
      const mediaType = content.media_type || 'image';
      const title = escapeHtml(content.title || '精選視覺展示');
      const caption = escapeHtml(content.caption || '點擊編輯說明文字');
      const mediaUrl = sanitizeUrl(content.media_url || 'https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?auto=format&fit=crop&w=800&q=80');
      const fitMode = content.fit_mode || 'cover';
      const posX = content.pos_x !== undefined ? Number(content.pos_x) : 50;
      const posY = content.pos_y !== undefined ? Number(content.pos_y) : 50;
      const zoom = content.zoom !== undefined ? Number(content.zoom) : 1;
      const showCaption = content.show_caption !== false;

      if (mediaType === 'youtube' && (mediaUrl.includes('youtube.com') || mediaUrl.includes('youtu.be'))) {
        let videoId = '';
        if (mediaUrl.includes('youtu.be/')) {
          videoId = mediaUrl.split('youtu.be/')[1].split('?')[0];
        } else if (mediaUrl.includes('v=')) {
          videoId = mediaUrl.split('v=')[1].split('&')[0];
        }
        html = `
          ${dragHandleHtml}
          ${controlsHtml}
          <div class="widget-media-embed">
            <iframe width="100%" height="100%" src="https://www.youtube.com/embed/${videoId}" frameborder="0" allowfullscreen style="border:0; border-radius:18px;"></iframe>
          </div>
        `;
      } else {
        html = `
          ${dragHandleHtml}
          ${controlsHtml}
          <div class="widget-media-embed mode-${fitMode}" style="--img-fit: ${fitMode}; --img-pos: ${posX}% ${posY}%; --img-zoom: ${zoom};">
            <div class="media-image-holder">
              ${fitMode === 'contain' ? `<div class="media-ambient-blur" style="background-image: url('${mediaUrl}');"></div>` : ''}
              <img src="${mediaUrl}" alt="${title}" class="media-main-img" onerror="this.src='/static/logo.png'" />
            </div>
            ${showCaption ? `
              <div class="media-caption-overlay">
                <h4 class="media-title" data-inline-edit="title" data-widget-id="${escapeHtml(widget.id)}" title="點擊直接修改標題">${title}</h4>
                <p class="media-caption" data-inline-edit="caption" data-widget-id="${escapeHtml(widget.id)}" title="點擊直接修改說明">${caption}</p>
              </div>
            ` : ''}
          </div>
        `;
      }
    }

    return html;
  }

  // ==================== 6. GridStack 12 欄網格初始化與卡片尺寸限制 ====================

  // 監聽社群區塊寬度變更（僅在寬度改變時觸發窄版樣式與防抖自適應，嚴格阻斷因高度改變而引發之無限重繪震動）
  const lastObservedWidths = new WeakMap();
  const socialObserver = new ResizeObserver((entries) => {
    if (isGridResizing || isGridDragging) return;
    for (const entry of entries) {
      const el = entry.target;
      if (el && el.classList.contains('widget-social-link')) {
        const curW = Math.round(entry.contentRect.width);
        const prevW = lastObservedWidths.get(el);
        if (prevW === curW) {
          // 僅高度變動，寬度未變：嚴格忽略，阻斷無限重算與畫面震動死循環
          continue;
        }
        lastObservedWidths.set(el, curW);

        if (curW < 290) {
          el.classList.add('is-narrow-social');
        } else {
          el.classList.remove('is-narrow-social');
        }

        const wNode = el.closest('.grid-stack-item');
        const wId = wNode?.getAttribute('gs-id');
        if (wId) {
          debouncedAutoAdjustWidgetHeight(wId);
        }
      }
    }
  });

  function observeSocialBlocks() {
    document.querySelectorAll('.widget-social-link').forEach((el) => {
      socialObserver.observe(el);
    });
  }

  // 精確測量卡片內部真實內容高度（徹底排除 textarea 拉伸後的無效空白區域）
  // 精確測量卡片內部真實內容高度（徹底排除 textarea 拉伸後的無效空白區域）
  function getWidgetActualContentHeight(widget, nodeEl) {
    const contentEl = nodeEl.querySelector('.grid-stack-item-content');
    if (!contentEl) return 0;

    if (widget.type === 'text_card') {
      const isEditingMode = nodeEl.querySelector('.is-editing-markdown');
      if (isEditingMode) {
        const topbar = nodeEl.querySelector('.markdown-card-topbar');
        const toolbarWrap = nodeEl.querySelector('.markdown-quick-toolbar-wrap');

        const topbarH = topbar ? topbar.offsetHeight : 34;
        const toolbarH = toolbarWrap ? toolbarWrap.offsetHeight : 40;
        const textarea = nodeEl.querySelector('.markdown-textarea-input');

        let textH = 60;
        if (textarea) {
          // 精準測量文字本身的真實未拉伸高度，重設 flex、height 與 minHeight
          const prevFlex = textarea.style.flex;
          const prevH = textarea.style.height;
          const prevMinH = textarea.style.minHeight;
          textarea.style.flex = 'none';
          textarea.style.height = '0px';
          textarea.style.minHeight = '0px';
          textH = textarea.scrollHeight;

          const val = textarea.value || '';
          const trailingNewlines = (val.match(/\n+$/) || [''])[0].length;
          if (trailingNewlines > 0) {
            textH += trailingNewlines * 22;
          }

          textarea.style.flex = prevFlex;
          textarea.style.height = prevH;
          textarea.style.minHeight = prevMinH;
        }
        return topbarH + toolbarH + textH + 24;
      } else {
        // 預覽效果或訪客實際瀏覽模式：精確測量渲染後 Markdown 的文字總高度 (切除頂部編輯工具列佔據的空間)
        const rendered = nodeEl.querySelector('.markdown-rendered-content');
        if (rendered) {
          let measuredH = 0;
          if (rendered.children && rendered.children.length > 0) {
            const firstChild = rendered.firstElementChild;
            const lastChild = rendered.lastElementChild;
            const rFirst = firstChild.getBoundingClientRect();
            const rLast = lastChild.getBoundingClientRect();
            measuredH = Math.max(0, rLast.bottom - rFirst.top);
          }
          if (!measuredH) {
            const rect = rendered.getBoundingClientRect();
            measuredH = Math.max(rect.height || 0, rendered.scrollHeight || 0, rendered.offsetHeight || 0);
          }
          return Math.max(20, Math.ceil(measuredH));
        }
        return 40;
      }
    }

    if (widget.type === 'social_link') {
      const card = nodeEl.querySelector('.widget-social-link');
      if (!card) return 60;

      const header = card.querySelector('.social-block-header');
      const headerH = header ? header.offsetHeight : 26;

      const btns = card.querySelector('.social-buttons-container');
      let btnsH = 32;
      if (btns) {
        const btnWraps = Array.from(btns.querySelectorAll('.social-btn-wrap, .social-btn-link'));
        if (btnWraps.length > 0) {
          const btnsRect = btns.getBoundingClientRect();
          let maxBottom = 0;
          btnWraps.forEach((bw) => {
            const r = bw.getBoundingClientRect();
            const b = r.bottom - btnsRect.top;
            if (b > maxBottom) maxBottom = b;
          });
          btnsH = Math.max(32, Math.ceil(maxBottom));
        } else {
          btnsH = Math.max(32, btns.scrollHeight);
        }
      }

      const gap = 4;
      return headerH + gap + btnsH;
    }

    if (widget.type === 'profile_header') {
      const headerEl = nodeEl.querySelector('.widget-profile-header');
      return headerEl ? headerEl.scrollHeight + 16 : 140;
    }

    if (widget.type === 'bot_economy') {
      const ecoEl = nodeEl.querySelector('.widget-bot-economy');
      return ecoEl ? ecoEl.scrollHeight + 16 : 140;
    }

    if (widget.type === 'media_embed') {
      const mediaEl = nodeEl.querySelector('.widget-media-embed');
      return mediaEl ? mediaEl.scrollHeight + 16 : 140;
    }

    return contentEl.scrollHeight;
  }

  const adjustingWidgets = new Set();
  const adjustHeightTimers = {};

  function debouncedAutoAdjustWidgetHeight(widgetId, isManualResize = false) {
    if (adjustHeightTimers[widgetId]) {
      clearTimeout(adjustHeightTimers[widgetId]);
    }
    adjustHeightTimers[widgetId] = setTimeout(() => {
      delete adjustHeightTimers[widgetId];
      const changed = autoAdjustWidgetHeight(widgetId, isManualResize);
      if (changed) {
        compactWidgetsWithDividerBounds();
        syncWidgetCoordinatesFromGrid();
        updateGlassSections();
      }
    }, 40);
  }

  function autoAdjustWidgetHeight(widgetId, isManualResize = false) {
    if (!gridInstance || adjustingWidgets.has(widgetId)) return false;
    const widget = widgets.find((w) => w.id === widgetId);
    if (!widget || !isAutoHeightCard(widget)) {
      return false; // 核心約束：僅支援自適應高度之卡片 (text_card 與 social_link) 改變高度
    }

    const nodeEl = document.querySelector(`.grid-stack-item[gs-id="${widgetId}"]`);
    if (!nodeEl) return false;
    const contentEl = nodeEl.querySelector('.grid-stack-item-content');
    if (!contentEl) return false;

    const cellHeight = 95;
    const limits = getWidgetSizeLimits(widget.type);
    const actualPx = getWidgetActualContentHeight(widget, nodeEl);

    // 核心計算：
    // 在編輯模式下：text_card 最小高度為 3 以容納頂部編輯工具列與輸入框，其餘保持 limits.minH
    // 在預覽或瀏覽模式下：編輯工具列完全移除，高度直接貼合實際內容，精準切除編輯工具列所佔高度，最小高度降為 limits.minH (1)
    const minHForEdit = widget.type === 'text_card' ? 3 : limits.minH;
    const effectiveMinH = isEditing ? minHForEdit : limits.minH;

    // 安全邊距緩衝：
    // 預覽與瀏覽模式下外層 padding 與 inset 緊湊 (約 22px-30px)，設定緩衝為 14px 即可完全杜絕跳列並精確切除多餘留白；
    // 編輯模式下設定為 28px 確保打字換行有充裕空間
    // 社群連結：外層 inset (10px) + padding (18px) = 28px，精準貼合單列 95px，不額外留白
    const safetyBuffer = widget.type === 'social_link' ? 28 : (isEditing ? 28 : 14);
    const requiredH = Math.min(limits.maxH, Math.max(effectiveMinH, Math.ceil((actualPx + safetyBuffer) / cellHeight)));
    const currentH = Number(nodeEl.getAttribute('gs-h')) || widget.h || effectiveMinH;

    nodeEl.setAttribute('gs-min-h', String(effectiveMinH));

    let targetH = currentH;
    if (isManualResize) {
      targetH = currentH < requiredH ? requiredH : currentH;
    } else {
      if (isEditing && widget.type !== 'social_link') {
        // 核心修復：在編輯打字時，只有在內容真正溢出當前卡片高度 (requiredH > currentH) 時才長高！
        targetH = requiredH > currentH ? requiredH : currentH;
      } else {
        // 預覽與實際瀏覽：精確收縮至實際內容高度 (requiredH)，切除編輯工具列所佔的空間，杜絕卡片下方無效留白
        targetH = Math.max(effectiveMinH, requiredH);
      }
    }

    if (currentH !== targetH) {
      adjustingWidgets.add(widgetId);
      try {
        const wasStatic = !!(gridInstance.opts && gridInstance.opts.staticGrid);
        if (wasStatic) {
          gridInstance.setStatic(false);
        }
        widget.h = targetH;
        nodeEl.setAttribute('gs-h', String(targetH));
        nodeEl.setAttribute('gs-min-h', String(effectiveMinH));
        gridInstance.update(nodeEl, { h: targetH, minH: effectiveMinH });
        if (wasStatic) {
          gridInstance.setStatic(true);
        }
        return true;
      } catch (err) {
        console.warn('GridStack update height warning:', err);
        return false;
      } finally {
        adjustingWidgets.delete(widgetId);
      }
    }
    return false;
  }

  function checkMarkdownCardsAutoHeight() {
    let anyChanged = false;
    widgets.forEach((w) => {
      if (isAutoHeightCard(w)) {
        const changed = autoAdjustWidgetHeight(w.id);
        if (changed) anyChanged = true;
      }
    });
    if (anyChanged) {
      compactWidgetsWithDividerBounds();
      syncWidgetCoordinatesFromGrid();
      updateGlassSections();
    }
    return anyChanged;
  }

  function initGrid() {
    const gridEl = document.getElementById('profile-grid');
    if (!gridEl) return;

    gridEl.innerHTML = '';

    gridInstance = GridStack.init(
      {
        column: 12,
        cellHeight: 95,
        margin: 0,
        animate: true,
        float: true,
        staticGrid: !isOwner,
        minRow: 2,
        draggable: {
          cancel: 'input, textarea, button, select, option, [contenteditable="true"], [data-inline-edit], .card-controls, .card-control-btn, .social-btn-item, .social-btn-wrap, .btn-remove-social-btn, .btn-sub-action, .btn-scissor-split, .markdown-quick-toolbar, .md-tool-btn, .markdown-textarea-input, .md-view-btn, .social-md-textarea, .social-md-preview-pane, .social-md-mode-btn, .btn-toggle-social-md',
        },
      },
      gridEl
    );

    widgets.forEach((w) => {
      const limits = getWidgetSizeLimits(w.type);
      const isHeader = w.type === 'profile_header';
      if (isHeader) {
        w.y = 0; // 永久置頂第一列
      }

      const nodeEl = document.createElement('div');
      nodeEl.className = 'grid-stack-item' + (w.type === 'divider' ? ' item-type-divider' : '');
      nodeEl.setAttribute('gs-id', w.id);
      nodeEl.setAttribute('data-widget-type', w.type);
      nodeEl.setAttribute('gs-x', String(w.x !== undefined ? w.x : 0));
      nodeEl.setAttribute('gs-y', String(isHeader ? 0 : (w.y !== undefined ? w.y : 0)));
      nodeEl.setAttribute('gs-w', String(w.w !== undefined ? w.w : 4));
      nodeEl.setAttribute('gs-h', String(w.h !== undefined ? w.h : 2));

      nodeEl.setAttribute('gs-min-w', String(limits.minW));
      nodeEl.setAttribute('gs-max-w', String(limits.maxW));
      nodeEl.setAttribute('gs-min-h', String(limits.minH));
      nodeEl.setAttribute('gs-max-h', String(limits.maxH));
      if (isHeader) {
        nodeEl.setAttribute('gs-no-move', 'true'); // 核心修復：永久置頂，只能縮放禁止移位
      }

      const contentEl = document.createElement('div');
      contentEl.className = 'grid-stack-item-content';
      contentEl.innerHTML = renderCardInnerHtml(w);
      nodeEl.appendChild(contentEl);

      gridInstance.addWidget(nodeEl, {
        id: w.id,
        x: w.x,
        y: isHeader ? 0 : w.y,
        w: w.w,
        h: w.h,
        minW: limits.minW,
        maxW: limits.maxW,
        minH: limits.minH,
        maxH: limits.maxH,
        noMove: isHeader, // 核心修復：只能縮放
        autoPosition: false,
      });
    });

    gridInstance.on('change', function (event, items) {
      if (!items) return;
      items.forEach((item) => {
        const id = item.id;
        const target = widgets.find((w) => w.id === id);
        if (target) {
          if (target.type === 'profile_header') {
            target.y = 0;
            item.y = 0;
          } else {
            target.y = item.y;
          }
          target.x = item.x;
          target.w = item.w;
          target.h = item.h;
        }
      });
      // 核心防護：拖曳進行中嚴禁執行耗時之全畫布 DOM 銷毀與背板重繪，徹底解決移動卡片時網頁卡頓無回應問題！
      if (!isGridDragging && !isGridResizing) {
        debouncedUpdateGlassSections();
      }
    });

    gridInstance.on('dragstart', function () {
      isGridDragging = true;
      document.querySelectorAll('.grid-stack-item').forEach((el) => {
        el.style.transform = '';
      });
    });

    gridInstance.on('resizestart', function () {
      isGridResizing = true;
      document.querySelectorAll('.grid-stack-item').forEach((el) => {
        el.style.transform = '';
      });
    });

    // 手動縮放結束：同步座標、補正最小高度防裁切，並執行上下區塊自動吸附 (分隔線除外)
    gridInstance.on('resizestop', function (event, el) {
      isGridResizing = false;
      syncWidgetCoordinatesFromGrid();
      if (el) {
        const id = el.getAttribute('gs-id');
        const target = widgets.find((w) => w.id === id);
        if (target && isMarkdownCard(target)) {
          debouncedAutoAdjustWidgetHeight(id, true);
        }
      }
      compactWidgetsWithDividerBounds();
      syncWidgetCoordinatesFromGrid();
      updateGlassSections();
    });

    gridInstance.on('dragstop', function () {
      isGridDragging = false;
      syncWidgetCoordinatesFromGrid();
      compactWidgetsWithDividerBounds();
      syncWidgetCoordinatesFromGrid();
      updateGlassSections();
      observeSocialBlocks();
    });

    ['added', 'removed'].forEach((evName) => {
      gridInstance.on(evName, function () {
        syncWidgetCoordinatesFromGrid();
        compactWidgetsWithDividerBounds();
        syncWidgetCoordinatesFromGrid();
        updateGlassSections();
        observeSocialBlocks();
      });
    });

    applyModeState(isEditing);
    setTimeout(() => {
      checkMarkdownCardsAutoHeight();
      compactWidgetsWithDividerBounds();
      syncWidgetCoordinatesFromGrid();
      updateGlassSections();
      observeSocialBlocks();
    }, 60);
  }

  function refreshCardContent(widgetId) {
    const widget = widgets.find((w) => w.id === widgetId);
    if (!widget) return;

    const nodeEl = document.querySelector(`.grid-stack-item[gs-id="${widgetId}"]`);
    if (nodeEl) {
      const contentEl = nodeEl.querySelector('.grid-stack-item-content');
      if (contentEl) {
        contentEl.innerHTML = renderCardInnerHtml(widget);
      }
    }
    updateInlineEditableState();
    observeSocialBlocks();
    if (isMarkdownCard(widget)) {
      debouncedAutoAdjustWidgetHeight(widgetId);
    }
  }

  // ==================== 7. 文字直接點擊編輯 (Inline Editable) ====================

  function updateInlineEditableState() {
    const editableEls = document.querySelectorAll('[data-inline-edit]');
    editableEls.forEach((el) => {
      if (isEditing) {
        el.setAttribute('contenteditable', 'true');
        el.setAttribute('spellcheck', 'false');
      } else {
        el.removeAttribute('contenteditable');
      }
    });
  }

  function setupInlineEditListeners() {
    const gridEl = document.getElementById('profile-grid');
    if (!gridEl) return;

    // 1. 攔截 pointerdown 與 mousedown 冒泡，防止 GridStack 拖曳機制搶奪文字編輯與工具列點擊
    gridEl.addEventListener(
      'pointerdown',
      function (e) {
        const isExcluded =
          e.target.closest('[data-inline-edit]') ||
          e.target.closest('.header-hashtags-list') ||
          e.target.closest('.btn-add-hashtag') ||
          e.target.closest('.btn-remove-tag') ||
          e.target.closest('.social-btn-item') ||
          e.target.closest('.btn-remove-social-btn') ||
          e.target.closest('.btn-sub-action') ||
          e.target.closest('.btn-scissor-split') ||
          e.target.closest('.markdown-card-topbar') ||
          e.target.closest('.markdown-quick-toolbar-wrap') ||
          e.target.closest('.markdown-quick-toolbar') ||
          e.target.closest('.md-tool-btn') ||
          e.target.closest('.md-tool-group') ||
          e.target.closest('.markdown-textarea-input') ||
          e.target.closest('.md-view-btn') ||
          e.target.closest('.social-markdown-wrapper') ||
          e.target.closest('.social-md-textarea') ||
          e.target.closest('.social-md-preview-pane') ||
          e.target.closest('.social-md-mode-btn') ||
          e.target.closest('.btn-toggle-social-md') ||
          e.target.closest('.card-controls');
        if (isExcluded && isEditing) {
          e.stopPropagation();
        }
      },
      true
    );

    gridEl.addEventListener(
      'mousedown',
      function (e) {
        const isExcluded =
          e.target.closest('[data-inline-edit]') ||
          e.target.closest('.header-hashtags-list') ||
          e.target.closest('.btn-add-hashtag') ||
          e.target.closest('.btn-remove-tag') ||
          e.target.closest('.social-btn-item') ||
          e.target.closest('.btn-remove-social-btn') ||
          e.target.closest('.btn-sub-action') ||
          e.target.closest('.btn-scissor-split') ||
          e.target.closest('.markdown-card-topbar') ||
          e.target.closest('.markdown-quick-toolbar-wrap') ||
          e.target.closest('.markdown-quick-toolbar') ||
          e.target.closest('.md-tool-btn') ||
          e.target.closest('.md-tool-group') ||
          e.target.closest('.markdown-textarea-input') ||
          e.target.closest('.md-view-btn') ||
          e.target.closest('.social-markdown-wrapper') ||
          e.target.closest('.social-md-textarea') ||
          e.target.closest('.social-md-preview-pane') ||
          e.target.closest('.social-md-mode-btn') ||
          e.target.closest('.btn-toggle-social-md') ||
          e.target.closest('.card-controls');
        if (isExcluded && isEditing) {
          e.stopPropagation();
        }
      },
      true
    );

    // 2. 點擊直接獲得焦點與輸入游標
    gridEl.addEventListener(
      'click',
      function (e) {
        const editable = e.target.closest('[data-inline-edit]');
        if (editable && isEditing) {
          e.stopPropagation();
          editable.focus();
        }
      },
      true
    );

    // 3. 預覽模式或訪客模式下，點擊社群卡片背景直達外部連結
    gridEl.addEventListener('click', function (e) {
      if (!isEditing) {
        const card = e.target.closest('.widget-social-link');
        if (card && !e.target.closest('.card-control-btn') && !e.target.closest('.social-arrow-indicator')) {
          const url = card.getAttribute('data-url');
          if (url && url !== '#' && url !== 'https://') {
            window.open(url, '_blank', 'noopener,noreferrer');
          }
        }
      }
    });

    // 4. 失焦即時更新資料模型 (widget.content)
    gridEl.addEventListener('blur', function (e) {
      const target = e.target;
      if (!target || !target.hasAttribute('data-inline-edit')) return;

      const field = target.getAttribute('data-inline-edit');
      const widgetId = target.getAttribute('data-widget-id');
      const newText = target.innerText.trim();

      const widget = widgets.find((w) => w.id === widgetId);
      if (!widget) return;

      if (!widget.content) widget.content = {};
      widget.content[field] = newText;

      if (widget.type === 'profile_header') {
        if (field === 'display_name') profileData.display_name = newText;
        if (field === 'bio') profileData.bio = newText;
        if (field === 'hashtag') {
          const tagIdx = parseInt(target.getAttribute('data-tag-index'), 10);
          if (!Array.isArray(widget.content.hashtags)) {
            widget.content.hashtags = ['炙彗社群', '次世代機器人'];
          }
          let cleanTag = newText.replace(/^#+/, '').trim();
          if (!cleanTag) cleanTag = '標籤';
          if (cleanTag.length > 40) cleanTag = cleanTag.slice(0, 40);
          widget.content.hashtags[tagIdx] = cleanTag;
          target.innerText = cleanTag;
        }
      }
    }, true);

    // 5. 單行標題與標籤按 Enter 自動完成編輯 (失焦)
    gridEl.addEventListener('keydown', function (e) {
      const target = e.target;
      if (!target || !target.hasAttribute('data-inline-edit')) return;

      const field = target.getAttribute('data-inline-edit');
      if (field === 'display_name' || field === 'badge' || field === 'title' || field === 'platform' || field === 'hashtag') {
        if (e.key === 'Enter') {
          e.preventDefault();
          target.blur();
        }
      }
    });
  }

  // ==================== 8. 「編輯模式 / 預覽效果」分段切換器邏輯 ====================

  function setViewMode(mode) {
    applyModeState(mode === 'edit');
  }

  function applyModeState(editState) {
    try {
      isEditing = !!editState;
      const gridEl = document.getElementById('profile-grid');
      const editorBar = document.getElementById('editor-bar');
      const btnEdit = document.getElementById('btn-mode-edit');
      const btnPreview = document.getElementById('btn-mode-preview');
      const statusPill = document.getElementById('status-pill-text');
      const statusLabel = document.getElementById('status-label');

      // 注意：切換模式期間保持 gridInstance 可寫 (staticGrid = false)，
      // 確保卡片內容重繪、高度自適應與緊湊吸附 (compaction) 順暢完成，最後再設定 static
      if (gridInstance) {
        try {
          gridInstance.setStatic(false);
        } catch (gsErr) {
          console.warn('GridStack setStatic warning:', gsErr);
        }
      }

      if (document.body) {
        document.body.classList.toggle('is-editing', isEditing);
      }
      const actionBtnTour = document.getElementById('action-btn-tour');
      if (actionBtnTour) {
        actionBtnTour.style.display = isEditing ? 'inline-flex' : 'none';
      }

      if (isEditing) {
        // 1. 切換至「編輯模式」
        if (gridEl) gridEl.classList.add('is-editing');
        if (editorBar) editorBar.classList.remove('editor-bar-preview');

        if (btnEdit) btnEdit.classList.add('active');
        if (btnPreview) btnPreview.classList.remove('active');

        // 狀態欄呈現「編輯中...」
        if (statusPill) {
          statusPill.className = 'dev-status-pill status-editing';
        }
        if (statusLabel) {
          statusLabel.textContent = '編輯中...';
        }
      } else {
        // 2. 切換至「預覽效果」
        if (gridEl) {
          gridEl.classList.remove('is-editing');
          gridEl.classList.remove('drag-target-active');
        }
        document.querySelectorAll('.drag-target-active').forEach((el) => {
          el.classList.remove('drag-target-active');
        });
        if (editorBar) editorBar.classList.add('editor-bar-preview');

        if (btnEdit) btnEdit.classList.remove('active');
        if (btnPreview) btnPreview.classList.add('active');

        // 狀態欄呈現「正在預覽...」
        if (statusPill) {
          statusPill.className = 'dev-status-pill status-previewing';
        }
        if (statusLabel) {
          statusLabel.textContent = '正在預覽...';
        }
      }

      // 切換模式前，先將當前正在編輯之 Textarea 內容即時同步至資料模型
      document.querySelectorAll('.markdown-textarea-input').forEach((ta) => {
        const wId = ta.getAttribute('data-widget-id');
        const target = widgets.find((w) => w.id === wId);
        if (target) {
          if (!target.content) target.content = {};
          target.content.body = ta.value;
        }
      });

      // 重新渲染卡片內部內容，使 Markdown 與分隔線在預覽與編輯間流暢切換
      widgets.forEach((w) => {
        try {
          refreshCardContent(w.id);
        } catch (cardErr) {
          console.error('Error refreshing card:', w.id, cardErr);
        }
      });

      // 模式切換收尾：高度自適應計算、格網向上緊湊吸附、玻璃背板更新與 staticGrid 鎖定
      const finalizeModeSwitch = () => {
        try {
          checkMarkdownCardsAutoHeight();
        } catch (err) {
          console.error('checkMarkdownCardsAutoHeight error:', err);
        }
        try {
          compactWidgetsWithDividerBounds();
        } catch (err) {
          console.error('compactWidgetsWithDividerBounds error:', err);
        }
        try {
          syncWidgetCoordinatesFromGrid();
        } catch (err) {
          console.error('syncWidgetCoordinatesFromGrid error:', err);
        }
        try {
          updateInlineEditableState();
        } catch (err) {
          console.error('updateInlineEditableState error:', err);
        }
        try {
          applyThemeStyles();
        } catch (err) {
          console.error('applyThemeStyles error:', err);
        }
        try {
          updateGlassSections();
        } catch (err) {
          console.error('updateGlassSections error:', err);
        }
        if (gridInstance) {
          try {
            gridInstance.setStatic(!isEditing);
          } catch (gsErr) {
            console.warn('GridStack setStatic warning:', gsErr);
          }
        }
      };

      finalizeModeSwitch();
      requestAnimationFrame(() => {
        finalizeModeSwitch();
      });
      setTimeout(() => {
        finalizeModeSwitch();
      }, 60);

    } catch (e) {
      console.error('Fatal error in applyModeState:', e);
    }
  }

  // ==================== 9. 彈窗編輯 (社群按鈕 / 媒體設定) ====================

  function detectSocialLinkInfo(rawUrl) {
    if (!rawUrl || typeof rawUrl !== 'string') {
      return { platform: 'custom', username: '', avatar_url: '' };
    }

    const trimmed = rawUrl.trim();
    let urlObj = null;
    try {
      urlObj = new URL(trimmed.startsWith('http://') || trimmed.startsWith('https://') ? trimmed : 'https://' + trimmed);
    } catch (e) {
      return { platform: 'custom', username: '', avatar_url: '' };
    }

    const host = urlObj.hostname.toLowerCase().replace(/^www\./, '');
    const pathname = urlObj.pathname;
    const pathParts = pathname.split('/').filter(Boolean);

    // 系統保留字過濾（避免將 login, settings 等當作用戶名）
    const SYSTEM_WORDS = new Set(['login', 'signin', 'register', 'signup', 'explore', 'settings', 'about', 'help', 'features', 'home', 'watch', 'search', 'terms', 'privacy', 'blog', 'support', 'status']);

    // 1. GitHub
    if (host === 'github.com') {
      let u = pathParts[0] || '';
      if (u.startsWith('@')) u = u.slice(1);
      if (SYSTEM_WORDS.has(u.toLowerCase())) u = '';
      return {
        platform: 'github',
        username: u,
        avatar_url: u ? `https://github.com/${u}.png` : '',
      };
    }

    // 2. Threads (threads.net or threads.com)
    if (host === 'threads.net' || host === 'threads.com') {
      let u = pathParts[0] || '';
      if (u.startsWith('@')) u = u.slice(1);
      if (SYSTEM_WORDS.has(u.toLowerCase())) u = '';
      return {
        platform: 'threads',
        username: u,
        avatar_url: u ? `https://unavatar.io/threads/${u}` : '',
      };
    }

    // 3. X / Twitter
    if (host === 'x.com' || host === 'twitter.com') {
      let u = pathParts[0] || '';
      if (u.startsWith('@')) u = u.slice(1);
      if (SYSTEM_WORDS.has(u.toLowerCase())) u = '';
      return {
        platform: 'twitter',
        username: u,
        avatar_url: u ? `https://unavatar.io/x/${u}` : '',
      };
    }

    // 4. Instagram
    if (host === 'instagram.com') {
      let u = pathParts[0] || '';
      if (u.startsWith('@')) u = u.slice(1);
      if (SYSTEM_WORDS.has(u.toLowerCase())) u = '';
      return {
        platform: 'instagram',
        username: u,
        avatar_url: u ? `https://unavatar.io/instagram/${u}` : '',
      };
    }

    // 5. YouTube
    if (host === 'youtube.com' || host === 'youtu.be') {
      let u = '';
      if (pathParts[0]?.startsWith('@')) {
        u = pathParts[0].slice(1);
      } else if (pathParts[0] === 'c' || pathParts[0] === 'channel' || pathParts[0] === 'user') {
        u = pathParts[1] || '';
      }
      if (u.startsWith('@')) u = u.slice(1);
      if (SYSTEM_WORDS.has(u.toLowerCase())) u = '';
      return {
        platform: 'youtube',
        username: u,
        avatar_url: u ? `https://unavatar.io/youtube/${u}` : '',
      };
    }

    // 6. Twitch
    if (host === 'twitch.tv') {
      let u = pathParts[0] || '';
      if (u.startsWith('@')) u = u.slice(1);
      if (SYSTEM_WORDS.has(u.toLowerCase())) u = '';
      return {
        platform: 'twitch',
        username: u,
        avatar_url: u ? `https://unavatar.io/twitch/${u}` : '',
      };
    }

    // 7. Spotify
    if (host === 'spotify.com' || host === 'open.spotify.com') {
      let u = '';
      if (pathParts[0] === 'user' || pathParts[0] === 'artist') {
        u = pathParts[1] || '';
      }
      return {
        platform: 'spotify',
        username: u,
        avatar_url: '',
      };
    }

    // 8. Discord
    if (host === 'discord.gg' || host === 'discord.com') {
      let u = '';
      if (pathParts[0] === 'invite' && pathParts[1]) {
        u = pathParts[1];
      } else if (pathParts[0] === 'users' && pathParts[1]) {
        u = pathParts[1];
      } else if (host === 'discord.gg' && pathParts[0]) {
        u = pathParts[0];
      }
      return {
        platform: 'discord',
        username: u,
        avatar_url: '',
      };
    }

    // 9. Steam
    if (host === 'steamcommunity.com') {
      let u = '';
      if ((pathParts[0] === 'id' || pathParts[0] === 'profiles') && pathParts[1]) {
        u = pathParts[1];
      }
      return {
        platform: 'steam',
        username: u,
        avatar_url: '',
      };
    }

    // 10. General Website / Custom
    return {
      platform: 'custom',
      username: '',
      avatar_url: '',
    };
  }

  function openSocialButtonModal(widgetId, linkId = null) {
    const widget = widgets.find((w) => w.id === widgetId);
    if (!widget) return;

    editingCardId = widgetId;
    editingSocialLinkId = linkId;

    const container = document.getElementById('edit-card-form-container');
    const titleEl = document.getElementById('edit-card-modal-title');
    if (!container) return;

    if (!widget.content) widget.content = {};
    if (!Array.isArray(widget.content.links)) widget.content.links = [];

    let targetLink = null;
    if (linkId) {
      targetLink = widget.content.links.find((l) => l.id === linkId);
    }

    const isEditMode = Boolean(targetLink);
    if (titleEl) {
      titleEl.textContent = isEditMode ? '編輯社群連結' : '新增社群連結';
    }

    const curUrl = targetLink ? (targetLink.url || '') : '';
    const curStyle = targetLink ? (targetLink.style || 'inline') : 'inline';
    const curPlatform = targetLink ? (targetLink.platform || 'auto') : 'auto';
    const curUsername = targetLink ? (targetLink.username || '') : '';
    const curAvatar = targetLink ? (targetLink.avatar_url || '') : '';
    const curUsePlatformIcon = Boolean(targetLink ? targetLink.use_platform_icon : false);

    container.innerHTML = `
      <div class="form-group">
        <label class="form-label" for="edit-link-url">連結網址 (URL) <span style="color: var(--danger, #f87171);">*</span></label>
        <input type="url" class="form-input" id="edit-link-url" value="${escapeHtml(curUrl)}" placeholder="貼上網址，例如 https://github.com/yourname" autofocus />
        <span class="modal-description" style="font-size: 11px; margin-top: 4px; display: block; color: var(--muted, #94a3b8);">
          支援自動偵測平台、用戶名稱與頭像
        </span>
      </div>

      <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 14px;">
        <div class="form-group">
          <label class="form-label" for="edit-link-style">外觀樣式</label>
          <select class="form-select" id="edit-link-style">
            <option value="inline" ${curStyle !== 'avatar' ? 'selected' : ''}>精簡</option>
            <option value="avatar" ${curStyle === 'avatar' ? 'selected' : ''}>頭像</option>
          </select>
        </div>
        <div class="form-group">
          <label class="form-label" for="edit-link-platform">社群平台 <span style="color: var(--muted, #94a3b8); font-weight: normal;">(非必填)</span></label>
          <select class="form-select" id="edit-link-platform">
            <option value="auto" ${(!curPlatform || curPlatform === 'auto') ? 'selected' : ''}>自動偵測</option>
            <option value="threads" ${curPlatform === 'threads' ? 'selected' : ''}>Threads</option>
            <option value="spotify" ${curPlatform === 'spotify' ? 'selected' : ''}>Spotify</option>
            <option value="github" ${curPlatform === 'github' ? 'selected' : ''}>GitHub</option>
            <option value="youtube" ${curPlatform === 'youtube' ? 'selected' : ''}>YouTube</option>
            <option value="twitter" ${curPlatform === 'twitter' ? 'selected' : ''}>X / Twitter</option>
            <option value="instagram" ${curPlatform === 'instagram' ? 'selected' : ''}>Instagram</option>
            <option value="discord" ${curPlatform === 'discord' ? 'selected' : ''}>Discord</option>
            <option value="twitch" ${curPlatform === 'twitch' ? 'selected' : ''}>Twitch</option>
            <option value="steam" ${curPlatform === 'steam' ? 'selected' : ''}>Steam</option>
            <option value="custom" ${curPlatform === 'custom' ? 'selected' : ''}>官方網站 / 自訂</option>
          </select>
        </div>
      </div>

      <div class="form-group" style="margin-top: 14px;">
        <label class="form-label" for="edit-link-username">用戶名 / 顯示名稱 <span style="color: var(--muted, #94a3b8); font-weight: normal;">(非必填)</span></label>
        <input type="text" class="form-input" id="edit-link-username" value="${escapeHtml(curUsername)}" placeholder="留空將自動偵測並帶入用戶名" />
      </div>

      <div class="form-group" id="edit-link-avatar-wrap" style="margin-top: 14px; ${curStyle === 'avatar' ? '' : 'display: none;'}">
        <label class="form-label" for="edit-link-avatar-url">頭像網址 <span style="color: var(--muted, #94a3b8); font-weight: normal;">(若未設定頭像預設使用該平台圖標)</span></label>
        <div style="display: flex; gap: 8px; align-items: center;">
          <input type="url" class="form-input" id="edit-link-avatar-url" value="${escapeHtml(curAvatar)}" placeholder="留空預設使用社群平台官方圖標" style="flex: 1;" ${curUsePlatformIcon ? 'disabled' : ''} />
          <div id="edit-link-avatar-preview" style="width: 36px; height: 36px; border-radius: 50%; overflow: hidden; background: rgba(15,23,42,0.75); display: flex; align-items: center; justify-content: center; flex-shrink: 0; border: 1px solid rgba(255,255,255,0.18);">
            <img id="edit-link-avatar-img" src="${escapeHtml(curAvatar)}" alt="" style="width: 100%; height: 100%; object-fit: cover; ${curUsePlatformIcon || !curAvatar ? 'display: none;' : ''}" onerror="this.style.display='none'; const fb = document.getElementById('edit-link-avatar-fallback-icon'); if (fb) fb.style.display='inline-flex';" />
            <span id="edit-link-avatar-fallback-icon" class="social-avatar-svg" style="width: 20px; height: 20px; display: ${curUsePlatformIcon || !curAvatar ? 'inline-flex' : 'none'}; align-items: center; justify-content: center; color: var(--theme-glow, #7dd3fc);">
              ${getPlatformSvg(curPlatform || 'custom')}
            </span>
          </div>
        </div>
        <div class="form-group checkbox-group" style="margin-top: 10px;">
          <label class="form-checkbox-label" style="display: inline-flex; align-items: center; gap: 8px; cursor: pointer; font-size: 13px; color: #f1f5f9; user-select: none;">
            <input type="checkbox" id="edit-link-use-platform-icon" ${curUsePlatformIcon ? 'checked' : ''} style="accent-color: var(--theme-glow, #7dd3fc); width: 16px; height: 16px; cursor: pointer;" />
            <span>使用社群軟體圖標 (替代個人頭像)</span>
          </label>
        </div>
        <span id="edit-link-avatar-hint" style="font-size: 11px; margin-top: 4px; display: block; color: var(--theme-glow, #7dd3fc);">
          ✓ 若未設定頭像或勾選上方選項，頭像處將直接呈現社群官方圖標
        </span>
      </div>
    `;

    const urlInput = document.getElementById('edit-link-url');
    const styleSelect = document.getElementById('edit-link-style');
    const platSelect = document.getElementById('edit-link-platform');
    const usernameInput = document.getElementById('edit-link-username');
    const avatarWrap = document.getElementById('edit-link-avatar-wrap');
    const avatarInput = document.getElementById('edit-link-avatar-url');
    const avatarImg = document.getElementById('edit-link-avatar-img');

    if (styleSelect && avatarWrap) {
      styleSelect.addEventListener('change', () => {
        avatarWrap.style.display = styleSelect.value === 'avatar' ? 'block' : 'none';
      });
    }

    let userEditedName = false;
    let userEditedAvatar = false;
    let userEditedPlatform = false;

    const usePlatformIconCheckbox = document.getElementById('edit-link-use-platform-icon');
    const fallbackIcon = document.getElementById('edit-link-avatar-fallback-icon');

    function getEffectivePlatform() {
      const platVal = platSelect ? platSelect.value : 'auto';
      if (platVal && platVal !== 'auto') return platVal;
      const detected = detectSocialLinkInfo(urlInput ? urlInput.value : '');
      return detected.platform || 'custom';
    }

    function updateAvatarPreview() {
      const isChecked = usePlatformIconCheckbox ? usePlatformIconCheckbox.checked : false;
      const effectivePlat = getEffectivePlatform();
      if (fallbackIcon) {
        fallbackIcon.innerHTML = getPlatformSvg(effectivePlat);
      }
      if (avatarInput) {
        avatarInput.disabled = isChecked;
      }
      const val = avatarInput ? avatarInput.value.trim() : '';
      if (isChecked || !val) {
        if (avatarImg) avatarImg.style.display = 'none';
        if (fallbackIcon) fallbackIcon.style.display = 'inline-flex';
      } else {
        if (avatarImg) {
          avatarImg.style.display = 'block';
          avatarImg.src = val;
        }
        if (fallbackIcon) fallbackIcon.style.display = 'none';
      }
    }

    if (usePlatformIconCheckbox) {
      usePlatformIconCheckbox.addEventListener('change', updateAvatarPreview);
    }

    if (usernameInput) {
      usernameInput.addEventListener('input', () => { userEditedName = true; });
    }
    if (avatarInput) {
      avatarInput.addEventListener('input', () => {
        userEditedAvatar = true;
        updateAvatarPreview();
      });
    }
    if (platSelect) {
      platSelect.addEventListener('change', () => {
        userEditedPlatform = true;
        updateAvatarPreview();
      });
    }

    function handleUrlDetection() {
      if (!urlInput) return;
      const detected = detectSocialLinkInfo(urlInput.value);
      if (detected.username) {
        usernameInput.placeholder = `自動偵測: ${detected.username}`;
        if (!userEditedName) {
          usernameInput.value = detected.username;
        }
      }
      if (detected.platform && detected.platform !== 'custom') {
        if (!userEditedPlatform || platSelect.value === 'auto') {
          platSelect.value = detected.platform;
        }
      }
      if (detected.avatar_url) {
        avatarInput.placeholder = `自動抓取: ${detected.avatar_url}`;
        if (!userEditedAvatar && !usePlatformIconCheckbox?.checked) {
          avatarInput.value = detected.avatar_url;
        }
      } else {
        avatarInput.placeholder = '留空預設使用社群平台官方圖標';
      }
      updateAvatarPreview();
    }

    if (urlInput) {
      urlInput.addEventListener('input', handleUrlDetection);
      urlInput.addEventListener('change', handleUrlDetection);
      if (curUrl) {
        handleUrlDetection();
      }
      setTimeout(() => urlInput.focus(), 150);
    }

    openModal('modal-edit-card');
  }

  function openEditModal(widgetId) {
    const widget = widgets.find((w) => w.id === widgetId);
    if (!widget) return;

    if (widget.type === 'social_link') {
      openSocialButtonModal(widgetId, null);
      return;
    }

    editingCardId = widgetId;
    editingSocialLinkId = null;
    const container = document.getElementById('edit-card-form-container');
    const titleEl = document.getElementById('edit-card-modal-title');
    if (!container) return;

    container.innerHTML = '';
    const content = widget.content || {};

    if (widget.type === 'media_embed') {
      const dialogEl = document.querySelector('#modal-edit-card .modal-dialog');
      if (dialogEl) dialogEl.classList.add('modal-dialog-media');

      const mType = content.media_type || 'image';
      if (titleEl) titleEl.textContent = mType === 'image' ? '設定圖片裁切與呈現' : '設定 YouTube 影片嵌入';

      const mediaUrl = content.media_url || 'https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?auto=format&fit=crop&w=800&q=80';
      const fitMode = content.fit_mode || 'cover';
      const posX = content.pos_x !== undefined ? Number(content.pos_x) : 50;
      const posY = content.pos_y !== undefined ? Number(content.pos_y) : 50;
      const zoom = content.zoom !== undefined ? Number(content.zoom) : 1;
      const showCaption = content.show_caption !== false;
      const cardTitle = content.title || '精選視覺展示';
      const cardCaption = content.caption || '點擊編輯說明文字';

      container.innerHTML = `
        <div class="form-group">
          <label class="form-label" for="edit-media-type">媒體格式</label>
          <select class="form-select" id="edit-media-type">
            <option value="image" ${mType === 'image' ? 'selected' : ''}>精美圖片展示 (支援裁切與自動縮放填滿)</option>
            <option value="youtube" ${mType === 'youtube' ? 'selected' : ''}>YouTube 影片嵌入</option>
          </select>
        </div>
        <div class="form-group">
          <label class="form-label" for="edit-media-url">媒體網址 (必須以 https:// 開頭)</label>
          <input type="url" class="form-input" id="edit-media-url" value="${escapeHtml(mediaUrl)}" placeholder="https://" />
        </div>

        <div id="image-crop-options-panel" style="${mType === 'image' ? '' : 'display: none;'}">
          <!-- 1. 即時裁切預覽視窗 -->
          <div class="crop-preview-section">
            <div class="crop-preview-header">
              <span class="crop-section-label">即時效果預覽</span>
              <div class="crop-ratio-toggles" id="crop-ratio-toggles">
                <button type="button" class="crop-ratio-btn active" data-ratio="2 / 1">寬卡片 2:1</button>
                <button type="button" class="crop-ratio-btn" data-ratio="16 / 9">標準 16:9</button>
                <button type="button" class="crop-ratio-btn" data-ratio="1 / 1">正方 1:1</button>
                <button type="button" class="crop-ratio-btn" data-ratio="3 / 4">縱向 3:4</button>
              </div>
            </div>

            <div class="crop-preview-box" id="crop-preview-box" style="aspect-ratio: 2 / 1;">
              <div class="crop-preview-ambient" id="crop-preview-ambient" style="background-image: url('${mediaUrl}'); display: ${fitMode === 'contain' ? 'block' : 'none'};"></div>
              <img src="${mediaUrl}" id="crop-preview-img" class="crop-preview-img" style="object-fit: ${fitMode}; object-position: ${posX}% ${posY}%; transform: scale(${zoom}); transform-origin: ${posX}% ${posY}%;" onerror="this.src='/static/logo.png'" />
              <div class="crop-preview-overlay" id="crop-preview-overlay" style="${showCaption ? '' : 'display: none;'}">
                <h5 id="crop-preview-title" class="crop-overlay-title">${escapeHtml(cardTitle)}</h5>
                <p id="crop-preview-caption" class="crop-overlay-caption">${escapeHtml(cardCaption)}</p>
              </div>
            </div>
          </div>

          <!-- 2. 縮放適配模式 -->
          <div class="form-group">
            <label class="form-label">縮放適配模式 (Fit Mode)</label>
            <div class="crop-mode-group" id="crop-mode-group">
              <button type="button" class="crop-mode-btn ${fitMode === 'cover' ? 'active' : ''}" data-mode="cover">
                <div class="crop-mode-name">裁切填滿</div>
                <div class="crop-mode-desc">維持比例滿版填滿</div>
              </button>
              <button type="button" class="crop-mode-btn ${fitMode === 'contain' ? 'active' : ''}" data-mode="contain">
                <div class="crop-mode-name">完整縮放</div>
                <div class="crop-mode-desc">原圖完整呈現＋氛圍光暈</div>
              </button>
              <button type="button" class="crop-mode-btn ${fitMode === 'fill' ? 'active' : ''}" data-mode="fill">
                <div class="crop-mode-name">拉伸填滿</div>
                <div class="crop-mode-desc">強制縮放填滿邊界</div>
              </button>
            </div>
            <input type="hidden" id="edit-media-fit-mode" value="${fitMode}" />
          </div>

          <!-- 3. 裁切焦點對齊 -->
          <div class="form-group">
            <div class="crop-focal-header">
              <label class="form-label" style="margin-bottom: 0;">裁切焦點對齊 (Focal Point)</label>
              <span class="crop-coord-badge" id="crop-coord-display">X: ${posX}% , Y: ${posY}%</span>
            </div>
            <div class="crop-focal-layout">
              <!-- 九宮格按鈕 -->
              <div class="crop-grid-3x3" id="crop-grid-3x3">
                <button type="button" class="crop-grid-cell ${posX === 0 && posY === 0 ? 'active' : ''}" data-x="0" data-y="0" title="左上 (0%, 0%)">↖</button>
                <button type="button" class="crop-grid-cell ${posX === 50 && posY === 0 ? 'active' : ''}" data-x="50" data-y="0" title="置頂 (50%, 0%)">↑</button>
                <button type="button" class="crop-grid-cell ${posX === 100 && posY === 0 ? 'active' : ''}" data-x="100" data-y="0" title="右上 (100%, 0%)">↗</button>
                <button type="button" class="crop-grid-cell ${posX === 0 && posY === 50 ? 'active' : ''}" data-x="0" data-y="50" title="靠左 (0%, 50%)">←</button>
                <button type="button" class="crop-grid-cell ${posX === 50 && posY === 50 ? 'active' : ''}" data-x="50" data-y="50" title="置中 (50%, 50%)">•</button>
                <button type="button" class="crop-grid-cell ${posX === 100 && posY === 50 ? 'active' : ''}" data-x="100" data-y="50" title="靠右 (100%, 50%)">→</button>
                <button type="button" class="crop-grid-cell ${posX === 0 && posY === 100 ? 'active' : ''}" data-x="0" data-y="100" title="左下 (0%, 100%)">↙</button>
                <button type="button" class="crop-grid-cell ${posX === 50 && posY === 100 ? 'active' : ''}" data-x="50" data-y="100" title="置底 (50%, 100%)">↓</button>
                <button type="button" class="crop-grid-cell ${posX === 100 && posY === 100 ? 'active' : ''}" data-x="100" data-y="100" title="右下 (100%, 100%)">↘</button>
              </div>

              <!-- 微調滑桿 -->
              <div class="crop-sliders-wrap">
                <div class="crop-slider-row">
                  <div class="crop-slider-label-row">
                    <span class="crop-slider-label">水平焦點 (X)</span>
                    <span class="crop-slider-val" id="val-pos-x">${posX}%</span>
                  </div>
                  <input type="range" class="form-range" id="edit-crop-x" min="0" max="100" value="${posX}" />
                </div>
                <div class="crop-slider-row">
                  <div class="crop-slider-label-row">
                    <span class="crop-slider-label">垂直焦點 (Y)</span>
                    <span class="crop-slider-val" id="val-pos-y">${posY}%</span>
                  </div>
                  <input type="range" class="form-range" id="edit-crop-y" min="0" max="100" value="${posY}" />
                </div>
              </div>
            </div>
          </div>

          <!-- 4. 裁切縮放倍率 -->
          <div class="form-group">
            <div class="crop-slider-label-row" style="margin-bottom: 6px;">
              <label class="form-label" for="edit-crop-zoom" style="margin-bottom: 0;">縮放放大倍率 (Zoom Scale)</label>
              <span class="crop-slider-val" id="val-crop-zoom">${Math.round(zoom * 100)}%</span>
            </div>
            <input type="range" class="form-range" id="edit-crop-zoom" min="1.0" max="2.5" step="0.05" value="${zoom}" />
            <span class="form-hint" style="display:block; margin-top: 4px; font-size: 11.5px; color: var(--muted);">放大圖片可針對畫面特定局部進行特寫裁切。</span>
          </div>

          <!-- 5. 標題與說明文字設定 -->
          <div class="form-group" style="margin-top: 14px;">
            <label class="form-check-label" style="display: flex; align-items: center; gap: 8px; cursor: pointer; user-select: none;">
              <input type="checkbox" id="edit-media-show-caption" ${showCaption ? 'checked' : ''} style="accent-color: var(--theme-glow, #7dd3fc); width: 16px; height: 16px;" />
              <span style="font-size: 13.5px; font-weight: 600; color: #fff;">顯示底部文字覆蓋層 (標題與說明)</span>
            </label>
          </div>

          <div id="media-text-inputs-wrap" style="${showCaption ? '' : 'display: none;'}">
            <div class="form-group">
              <label class="form-label" for="edit-media-title">標題文字</label>
              <input type="text" class="form-input" id="edit-media-title" value="${escapeHtml(cardTitle)}" placeholder="輸入標題..." />
            </div>
            <div class="form-group">
              <label class="form-label" for="edit-media-caption">說明文字</label>
              <input type="text" class="form-input" id="edit-media-caption" value="${escapeHtml(cardCaption)}" placeholder="輸入說明文字..." />
            </div>
          </div>
        </div>
      `;

      // 綁定即時互動事件
      const mediaTypeSelect = document.getElementById('edit-media-type');
      const cropPanel = document.getElementById('image-crop-options-panel');
      const mediaUrlInput = document.getElementById('edit-media-url');
      const previewBox = document.getElementById('crop-preview-box');
      const previewImg = document.getElementById('crop-preview-img');
      const previewAmbient = document.getElementById('crop-preview-ambient');
      const previewOverlay = document.getElementById('crop-preview-overlay');
      const previewTitle = document.getElementById('crop-preview-title');
      const previewCaption = document.getElementById('crop-preview-caption');
      const fitModeInput = document.getElementById('edit-media-fit-mode');
      const sliderX = document.getElementById('edit-crop-x');
      const sliderY = document.getElementById('edit-crop-y');
      const sliderZoom = document.getElementById('edit-crop-zoom');
      const showCaptionCheck = document.getElementById('edit-media-show-caption');
      const textInputsWrap = document.getElementById('media-text-inputs-wrap');
      const titleInput = document.getElementById('edit-media-title');
      const captionInput = document.getElementById('edit-media-caption');
      const coordDisplay = document.getElementById('crop-coord-display');
      const valPosX = document.getElementById('val-pos-x');
      const valPosY = document.getElementById('val-pos-y');
      const valZoom = document.getElementById('val-crop-zoom');

      function updatePreviewStyles() {
        const curMode = fitModeInput ? fitModeInput.value || 'cover' : 'cover';
        const curX = sliderX ? sliderX.value : 50;
        const curY = sliderY ? sliderY.value : 50;
        const curZ = sliderZoom ? sliderZoom.value : 1;

        if (previewImg) {
          previewImg.style.objectFit = curMode;
          previewImg.style.objectPosition = `${curX}% ${curY}%`;
          previewImg.style.transform = `scale(${curZ})`;
          previewImg.style.transformOrigin = `${curX}% ${curY}%`;
        }

        if (previewAmbient) {
          previewAmbient.style.display = curMode === 'contain' ? 'block' : 'none';
        }

        if (coordDisplay) coordDisplay.textContent = `X: ${curX}% , Y: ${curY}%`;
        if (valPosX) valPosX.textContent = `${curX}%`;
        if (valPosY) valPosY.textContent = `${curY}%`;
        if (valZoom) valZoom.textContent = `${Math.round(curZ * 100)}%`;

        // 更新 3x3 九宮格 active 狀態
        document.querySelectorAll('#crop-grid-3x3 .crop-grid-cell').forEach((cell) => {
          const cx = cell.getAttribute('data-x');
          const cy = cell.getAttribute('data-y');
          if (cx === curX && cy === curY) {
            cell.classList.add('active');
          } else {
            cell.classList.remove('active');
          }
        });
      }

      mediaTypeSelect?.addEventListener('change', () => {
        const isImg = mediaTypeSelect.value === 'image';
        if (cropPanel) cropPanel.style.display = isImg ? '' : 'none';
        if (titleEl) titleEl.textContent = isImg ? '設定圖片裁切與呈現' : '設定 YouTube 影片嵌入';
      });

      mediaUrlInput?.addEventListener('input', () => {
        const val = mediaUrlInput.value.trim();
        if (val && previewImg) previewImg.src = val;
        if (val && previewAmbient) previewAmbient.style.backgroundImage = `url('${val}')`;
      });

      // 比例切換按鈕
      document.querySelectorAll('#crop-ratio-toggles .crop-ratio-btn').forEach((btn) => {
        btn.addEventListener('click', () => {
          document.querySelectorAll('#crop-ratio-toggles .crop-ratio-btn').forEach((b) => b.classList.remove('active'));
          btn.classList.add('active');
          const ratio = btn.getAttribute('data-ratio');
          if (previewBox && ratio) previewBox.style.aspectRatio = ratio;
        });
      });

      // 適配模式按鈕
      document.querySelectorAll('#crop-mode-group .crop-mode-btn').forEach((btn) => {
        btn.addEventListener('click', () => {
          document.querySelectorAll('#crop-mode-group .crop-mode-btn').forEach((b) => b.classList.remove('active'));
          btn.classList.add('active');
          const mode = btn.getAttribute('data-mode') || 'cover';
          if (fitModeInput) fitModeInput.value = mode;
          updatePreviewStyles();
        });
      });

      // 九宮格點擊
      document.querySelectorAll('#crop-grid-3x3 .crop-grid-cell').forEach((cell) => {
        cell.addEventListener('click', () => {
          const cx = cell.getAttribute('data-x');
          const cy = cell.getAttribute('data-y');
          if (sliderX) sliderX.value = cx;
          if (sliderY) sliderY.value = cy;
          updatePreviewStyles();
        });
      });

      sliderX?.addEventListener('input', updatePreviewStyles);
      sliderY?.addEventListener('input', updatePreviewStyles);
      sliderZoom?.addEventListener('input', updatePreviewStyles);

      showCaptionCheck?.addEventListener('change', () => {
        const checked = showCaptionCheck.checked;
        if (textInputsWrap) textInputsWrap.style.display = checked ? '' : 'none';
        if (previewOverlay) previewOverlay.style.display = checked ? '' : 'none';
      });

      titleInput?.addEventListener('input', () => {
        if (previewTitle) previewTitle.textContent = titleInput.value || '精選視覺展示';
      });

      captionInput?.addEventListener('input', () => {
        if (previewCaption) previewCaption.textContent = captionInput.value || '點擊編輯說明文字';
      });
    } else {
      const dialogEl = document.querySelector('#modal-edit-card .modal-dialog');
      if (dialogEl) dialogEl.classList.remove('modal-dialog-media');
    }

    openModal('modal-edit-card');
  }

  function saveCardFromModal() {
    if (!editingCardId) return;
    const widget = widgets.find((w) => w.id === editingCardId);
    if (!widget) return;

    if (!widget.content) widget.content = {};

    if (widget.type === 'social_link') {
      const urlInput = document.getElementById('edit-link-url');
      let rawUrl = urlInput ? urlInput.value.trim() : '';
      if (!rawUrl) {
        showToast('請輸入連結網址', 'warning');
        urlInput?.focus();
        return;
      }

      if (!rawUrl.startsWith('http://') && !rawUrl.startsWith('https://')) {
        rawUrl = 'https://' + rawUrl;
      }

      const detected = detectSocialLinkInfo(rawUrl);
      const style = document.getElementById('edit-link-style')?.value || 'inline';
      const platVal = document.getElementById('edit-link-platform')?.value;
      const platform = (platVal && platVal !== 'auto') ? platVal : (detected.platform || 'custom');

      const userInputName = document.getElementById('edit-link-username')?.value.trim();
      const username = userInputName || detected.username || '專屬連結';

      const usePlatformIcon = document.getElementById('edit-link-use-platform-icon')?.checked || false;
      const userInputAvatar = document.getElementById('edit-link-avatar-url')?.value.trim();
      // 若勾選使用社群圖標或未自訂頭像，avatar_url 留空，自動於外層呈現官方圖標
      const avatar_url = usePlatformIcon ? '' : (userInputAvatar || detected.avatar_url || '');

      if (!Array.isArray(widget.content.links)) widget.content.links = [];

      if (editingSocialLinkId) {
        const link = widget.content.links.find((l) => l.id === editingSocialLinkId);
        if (link) {
          link.url = rawUrl;
          link.style = style;
          link.platform = platform;
          link.username = username;
          link.avatar_url = avatar_url;
          link.use_platform_icon = usePlatformIcon;
        }
      } else {
        const newLinkId = 'l-' + Date.now() + '-' + Math.random().toString(36).substr(2, 4);
        widget.content.links.push({
          id: newLinkId,
          url: rawUrl,
          style: style,
          platform: platform,
          username: username,
          avatar_url: avatar_url,
          use_platform_icon: usePlatformIcon,
        });
      }

      refreshCardContent(editingCardId);
      updateGlassSections();
      closeModal('modal-edit-card');
      showToast('社群連結已更新 (請記得點擊「儲存變更」寫入資料庫)', 'info');
      return;
    } else if (widget.type === 'media_embed') {
      const mType = document.getElementById('edit-media-type')?.value || 'image';
      const url = document.getElementById('edit-media-url')?.value.trim();

      widget.content.media_type = mType;
      widget.content.media_url = url || '';

      if (mType === 'image') {
        const fitMode = document.getElementById('edit-media-fit-mode')?.value || 'cover';
        const posX = Number(document.getElementById('edit-crop-x')?.value || 50);
        const posY = Number(document.getElementById('edit-crop-y')?.value || 50);
        const zoom = Number(document.getElementById('edit-crop-zoom')?.value || 1);
        const showCaption = document.getElementById('edit-media-show-caption')?.checked !== false;
        const title = document.getElementById('edit-media-title')?.value.trim();
        const caption = document.getElementById('edit-media-caption')?.value.trim();

        widget.content.fit_mode = fitMode;
        widget.content.pos_x = posX;
        widget.content.pos_y = posY;
        widget.content.zoom = zoom;
        widget.content.show_caption = showCaption;
        if (title !== undefined && title !== '') widget.content.title = title;
        if (caption !== undefined && caption !== '') widget.content.caption = caption;
      }
    }

    refreshCardContent(editingCardId);
    updateGlassSections();
    closeModal('modal-edit-card');
    showToast('卡片設定已更新 (請記得點擊「儲存變更」寫入資料庫)', 'info');
  }

  function deleteCard(widgetId) {
    const target = widgets.find((w) => w.id === widgetId);
    if (!target) return;
    if (target.type === 'profile_header') {
      showToast('個人基礎資訊卡片為必備項目，無法刪除', 'warning');
      return;
    }

    const index = widgets.findIndex((w) => w.id === widgetId);
    if (index === -1) return;

    const nodeEl = document.querySelector(`.grid-stack-item[gs-id="${widgetId}"]`);
    if (nodeEl && gridInstance) {
      gridInstance.removeWidget(nodeEl);
    }
    widgets.splice(index, 1);
    compactWidgetsWithDividerBounds();
    updateAddCardPaletteState();
    updateGlassSections();
    showToast('已自畫布移除卡片', 'info');
  }

  function rotateDivider(widgetId) {
    const widget = widgets.find((w) => w.id === widgetId);
    if (!widget || widget.type !== 'divider') return;

    if (!widget.content) widget.content = {};
    const curOrientation = widget.content.orientation || 'horizontal';
    const newOrientation = curOrientation === 'horizontal' ? 'vertical' : 'horizontal';
    widget.content.orientation = newOrientation;

    const nodeEl = document.querySelector(`.grid-stack-item[gs-id="${widgetId}"]`);
    if (gridInstance && nodeEl) {
      if (newOrientation === 'vertical') {
        widget.w = 1;
        widget.h = Math.max(3, Number(widget.h) || 3);
        gridInstance.update(nodeEl, { w: 1, h: widget.h, minW: 1, maxW: 1 });
      } else {
        widget.w = 12;
        widget.h = 1;
        gridInstance.update(nodeEl, { w: 12, h: 1, minW: 4, maxW: 12 });
      }
    }

    refreshCardContent(widgetId);
    updateGlassSections();
    showToast(`分隔線已切換為${newOrientation === 'vertical' ? '垂直' : '水平'}方向`, 'info');
  }

  function addNewWidget(type, dropX, dropY) {
    if (widgets.length >= 20) {
      showToast('卡片總數上限為 20 張，無法再新增', 'error');
      return;
    }

    // 嚴格卡片類型上限限制 (文字區塊、社群連結、圖片或YouTube嵌入上限 5 個；經濟面板與名片上限 1 個)
    const maxLimit = CARD_LIMITS[type] || 20;
    const currentCount = widgets.filter((w) => w.type === type).length;
    if (currentCount >= maxLimit) {
      const typeName = CARD_TYPE_NAMES[type] || '卡片';
      showToast(`${typeName}數量上限為 ${maxLimit} 個，無法再新增`, 'warning');
      return;
    }

    const newId = 'card-' + Date.now().toString(36);
    let newWidget = null;
    const limits = getWidgetSizeLimits(type);

    const xPos = dropX !== undefined ? dropX : 0;
    const yPos = dropY !== undefined ? dropY : 12;

    if (type === 'text_card') {
      newWidget = {
        id: newId,
        type: 'text_card',
        x: xPos,
        y: yPos,
        w: 6,
        h: 3,
        content: {
          body: '### 歡迎來到我的個人空間\n點此直接編輯內容，支援 **粗體**、*斜體*、清單與 Markdown 語法。',
        },
      };
    } else if (type === 'social_link') {
      newWidget = {
        id: newId,
        type: 'social_link',
        x: xPos,
        y: yPos,
        w: 6,
        h: 2,
        content: {
          title: '社群連結',
          links: [
            {
              id: 'btn-' + Date.now() + '-1',
              platform: 'threads',
              username: profileData.username || 'threads_user',
              url: 'https://threads.net',
              style: 'inline',
            },
            {
              id: 'btn-' + Date.now() + '-2',
              platform: 'spotify',
              username: profileData.display_name || 'Spotify',
              url: 'https://spotify.com',
              style: 'inline',
            },
            {
              id: 'btn-' + Date.now() + '-3',
              platform: 'github',
              username: profileData.username || 'github_user',
              url: 'https://github.com',
              style: 'avatar',
              avatar_url: profileData.avatar_url || '/static/logo.png',
            }
          ],
        },
      };
    } else if (type === 'media_embed') {
      newWidget = {
        id: newId,
        type: 'media_embed',
        x: xPos,
        y: yPos,
        w: 6,
        h: 2,
        content: {
          media_type: 'image',
          title: '精選視覺',
          media_url: 'https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?auto=format&fit=crop&w=800&q=80',
          caption: '點擊編輯說明',
          fit_mode: 'cover',
          pos_x: 50,
          pos_y: 50,
          zoom: 1,
          show_caption: true,
        },
      };
    } else if (type === 'divider') {
      newWidget = {
        id: newId,
        type: 'divider',
        x: 0,
        y: yPos,
        w: 12,
        h: 1,
        content: {
          orientation: 'horizontal',
        },
      };
    } else if (type === 'bot_economy') {
      newWidget = {
        id: newId,
        type: 'bot_economy',
        x: xPos,
        y: yPos,
        w: 4,
        h: 2,
        content: {
          title: '經濟數據',
        },
      };
    }

    if (!newWidget) return;

    if (dropX !== undefined) {
      if (newWidget.type === 'divider') {
        newWidget.x = 0;
      } else {
        newWidget.x = Math.max(0, Math.min(xPos, 12 - newWidget.w));
      }
    }

    widgets.push(newWidget);

    const nodeEl = document.createElement('div');
    nodeEl.className = 'grid-stack-item' + (newWidget.type === 'divider' ? ' item-type-divider' : '');
    nodeEl.setAttribute('gs-id', newWidget.id);
    nodeEl.setAttribute('data-widget-type', newWidget.type);
    nodeEl.setAttribute('gs-x', String(newWidget.x));
    nodeEl.setAttribute('gs-y', String(newWidget.y));
    nodeEl.setAttribute('gs-w', String(newWidget.w));
    nodeEl.setAttribute('gs-h', String(newWidget.h));
    nodeEl.setAttribute('gs-min-w', String(limits.minW));
    nodeEl.setAttribute('gs-max-w', String(limits.maxW));
    nodeEl.setAttribute('gs-min-h', String(limits.minH));
    nodeEl.setAttribute('gs-max-h', String(limits.maxH));

    const contentEl = document.createElement('div');
    contentEl.className = 'grid-stack-item-content';
    contentEl.innerHTML = renderCardInnerHtml(newWidget);
    nodeEl.appendChild(contentEl);

    gridInstance.addWidget(nodeEl, {
      id: newWidget.id,
      x: newWidget.x,
      y: newWidget.y,
      w: newWidget.w,
      h: newWidget.h,
      minW: limits.minW,
      maxW: limits.maxW,
      minH: limits.minH,
      maxH: limits.maxH,
      autoPosition: dropX === undefined && dropY === undefined,
    });

    syncWidgetCoordinatesFromGrid();
    // 拖曳放置至指定位置時，保留使用者拖曳落點，不執行自動吸附至頂端；僅在點擊新增 (未給座標) 時自動吸附
    if (dropX === undefined && dropY === undefined) {
      compactWidgetsWithDividerBounds();
    }
    closeModal('modal-add-card');
    updateAddCardPaletteState();
    updateGlassSections();
    showToast('已新增卡片至畫布', 'success');
    updateInlineEditableState();

    if (type === 'media_embed') {
      setTimeout(() => {
        openEditModal(newId);
      }, 300);
    }
  }

  // ==================== 9.5 新增卡片 Palette 視覺預覽與拖曳互動 ====================

  function updateAddCardPaletteState() {
    const cardItems = document.querySelectorAll('.palette-card-item');
    cardItems.forEach((item) => {
      const type = item.getAttribute('data-type');
      if (!type) return;
      const maxLimit = CARD_LIMITS[type] || 20;
      const currentCount = widgets.filter((w) => w.type === type).length;
      const typeName = CARD_TYPE_NAMES[type] || '卡片';
      const actionEl = item.querySelector('.palette-card-action');

      if (currentCount >= maxLimit) {
        item.classList.add('card-disabled');
        item.setAttribute('draggable', 'false');
        item.setAttribute('title', `${typeName}已達數量上限 (最多 ${maxLimit} 個)`);
        if (actionEl) {
          actionEl.textContent = `已達上限 (${currentCount}/${maxLimit})`;
        }
      } else {
        item.classList.remove('card-disabled');
        item.setAttribute('draggable', 'true');
        item.setAttribute('title', '長按拖曳至畫布，或點擊直接新增');
        if (actionEl) {
          actionEl.textContent = maxLimit > 1 ? `點擊新增或拖曳 (${currentCount}/${maxLimit})` : '點擊新增或拖曳';
        }
      }
    });
  }

  function setupPaletteDragAndDrop() {
    const paletteModal = document.getElementById('modal-add-card');
    const gridEl = document.getElementById('profile-grid');
    const cardItems = document.querySelectorAll('.palette-card-item');

    let draggedType = null;

    cardItems.forEach((item) => {
      // 點擊卡片直接新增至版面
      item.addEventListener('click', function () {
        const type = this.getAttribute('data-type');
        if (this.classList.contains('card-disabled')) {
          const typeName = CARD_TYPE_NAMES[type] || '卡片';
          const maxLimit = CARD_LIMITS[type] || 1;
          showToast(`${typeName}數量已達上限 (${maxLimit} 個)`, 'warning');
          return;
        }
        if (type) {
          closeModal('modal-add-card');
          addNewWidget(type);
        }
      });

      // 長按拖曳至畫布
      item.addEventListener('dragstart', function (e) {
        const type = this.getAttribute('data-type');
        if (this.classList.contains('card-disabled')) {
          e.preventDefault();
          const typeName = CARD_TYPE_NAMES[type] || '卡片';
          const maxLimit = CARD_LIMITS[type] || 1;
          showToast(`${typeName}數量已達上限 (${maxLimit} 個)`, 'warning');
          return;
        }
        draggedType = type;
        if (e.dataTransfer) {
          e.dataTransfer.setData('text/plain', draggedType);
          e.dataTransfer.effectAllowed = 'copy';
        }
        setTimeout(() => {
          if (paletteModal) {
            paletteModal.classList.add('is-dragging-palette');
          }
        }, 20);
      });

      item.addEventListener('dragend', function () {
        draggedType = null;
        if (paletteModal) {
          paletteModal.classList.remove('is-dragging-palette');
        }
        if (gridEl) {
          gridEl.classList.remove('drag-target-active');
        }
      });
    });

    const handleDragOver = function (e) {
      if (draggedSocialBtn || !isEditing) return; // 絕不干擾社群按鈕拖曳與預覽模式
      if (draggedType) {
        e.preventDefault();
        if (e.dataTransfer) {
          e.dataTransfer.dropEffect = 'copy';
        }
        if (gridEl) {
          gridEl.classList.add('drag-target-active');
        }
      }
    };

    const handleDrop = function (e) {
      if (draggedSocialBtn || !isEditing) return;
      const typeToAdd = draggedType;
      if (!typeToAdd) return;
      e.preventDefault();
      e.stopPropagation();

      draggedType = null;

      if (paletteModal) {
        paletteModal.classList.remove('is-dragging-palette');
        closeModal('modal-add-card');
      }
      if (gridEl) {
        gridEl.classList.remove('drag-target-active');
      }

      let dropX = undefined;
      let dropY = undefined;

      if (gridEl) {
        const rect = gridEl.getBoundingClientRect();
        const cellWidth = rect.width / 12;
        const cellHeight = 95;
        const mouseX = e.clientX;
        const mouseY = e.clientY;

        if (mouseX >= rect.left && mouseX <= rect.right && mouseY >= rect.top) {
          const col = Math.floor((mouseX - rect.left) / cellWidth);
          const row = Math.floor((mouseY - rect.top) / cellHeight);
          dropX = Math.max(0, Math.min(11, col));
          dropY = Math.max(0, row);
        }
      }

      addNewWidget(typeToAdd, dropX, dropY);
    };

    // 監聽拖曳懸停在整個視窗、彈窗與畫布上
    document.addEventListener('dragover', handleDragOver);
    document.addEventListener('drop', handleDrop);
    if (paletteModal) {
      paletteModal.addEventListener('dragover', handleDragOver);
      paletteModal.addEventListener('drop', handleDrop);
    }
    if (gridEl) {
      gridEl.addEventListener('dragover', handleDragOver);
      gridEl.addEventListener('drop', handleDrop);
    }
  }

  // ==================== 10. 外觀主題樣式套用 (修復預覽畫面生效) ====================

  function applyThemeStyles() {
    const glow = themeConfig.glow_color || '#7dd3fc';
    const radius = themeConfig.card_radius || '24px';

    document.documentElement.style.setProperty('--theme-glow', glow);
    document.body.style.setProperty('--theme-glow', glow);
    document.body.style.setProperty('--card-radius', radius);

    // 同步動態更新背景鮮活流體光球
    const orb = document.querySelector('.ambient-orb-1');
    if (orb) {
      orb.style.background = `radial-gradient(circle, ${glow} 0%, transparent 70%)`;
    }
  }

  // ==================== 11. Modal 彈窗控制 (防選取文字拉到視窗外放開時閃退) ====================

  function openModal(id) {
    const el = document.getElementById(id);
    if (el) el.classList.add('active');
  }

  function closeModal(id) {
    const el = document.getElementById(id);
    if (el) el.classList.remove('active');
    if (id === 'modal-confirm') {
      pendingConfirmCallback = null;
    }
    if (id === 'modal-edit-card') {
      editingCardId = null;
      editingSocialLinkId = null;
      const dialogEl = document.querySelector('#modal-edit-card .modal-dialog');
      if (dialogEl) dialogEl.classList.remove('modal-dialog-media');
    }
  }

  // ==================== 12. 分享彈窗與 QR Code ====================

  function initShareModal() {
    const qrContainer = document.getElementById('qr-code-container');
    if (!qrContainer) return;

    qrContainer.innerHTML = '';
    const targetUrl = profileData.canonical_url || window.location.href.split('?')[0];

    if (typeof QRCode !== 'undefined') {
      try {
        new QRCode(qrContainer, {
          text: targetUrl,
          width: 132,
          height: 132,
          colorDark: '#090d1a',
          colorLight: '#ffffff',
          correctLevel: QRCode.CorrectLevel.M,
        });
      } catch (e) {
        qrContainer.textContent = targetUrl;
      }
    } else {
      qrContainer.textContent = targetUrl;
    }
  }

  // ==================== 13. 後端儲存 API 整合 ====================

  function saveProfileToBackend() {
    // 確保任何正在編輯的輸入框在送出前完成失焦並同步資料
    if (document.activeElement && typeof document.activeElement.blur === 'function') {
      document.activeElement.blur();
    }

    // 同步所有 Markdown Textarea 當前輸入至資料結構
    document.querySelectorAll('.markdown-textarea-input').forEach((ta) => {
      const widgetId = ta.getAttribute('data-widget-id');
      const target = widgets.find((w) => w.id === widgetId);
      if (target) {
        if (!target.content) target.content = {};
        target.content.body = ta.value;
      }
    });

    const saveBtn = document.getElementById('btn-save-profile');
    if (saveBtn) {
      saveBtn.textContent = '儲存中...';
      saveBtn.disabled = true;
    }

    if (window.innerWidth <= 640) {
      normalizeMobileLayoutToDesktop();
    } else if (gridInstance && gridInstance.engine && gridInstance.engine.nodes) {
      gridInstance.engine.nodes.forEach((node) => {
        const id = node.id;
        const target = widgets.find((w) => w.id === id);
        if (target) {
          target.x = node.x;
          target.y = node.y;
          target.w = node.w;
          target.h = node.h;
        }
      });
    }

    const payload = {
      username: profileData.username,
      layout_widgets: widgets,
      theme_config: themeConfig,
      display_name: profileData.display_name,
      bio: profileData.bio,
      avatar_url: profileData.avatar_url,
    };

    let apiUrl = '/api/profile/save';
    if (devMode && currentRole === 'visitor') {
      apiUrl += '?role=visitor';
    }

    fetch(apiUrl, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(payload),
    })
      .then(async (res) => {
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          throw new Error(data.error || '儲存失敗 (HTTP ' + res.status + ')');
        }
        return data;
      })
      .then((data) => {
        showToast(data.message || '個人檔案排版已成功儲存！', 'success');
      })
      .catch((err) => {
        showToast(err.message || '儲存失敗，請稍後再試', 'error');
      })
      .finally(() => {
        if (saveBtn) {
          saveBtn.textContent = '儲存變更';
          saveBtn.disabled = false;
        }
      });
  }

  // ==================== 14. 事件綁定 (Event Listeners) ====================


  // ==================== 2.9 手機版排版正規化至電腦端 (單一橫排一區塊) ====================
  function normalizeMobileLayoutToDesktop() {
    let curY = 0;
    const headerWidget = widgets.find((w) => w.type === 'profile_header');
    if (headerWidget) {
      headerWidget.x = 0;
      headerWidget.y = 0;
      headerWidget.w = 12;
      curY = headerWidget.h || 2;
    }

    const nonHeader = widgets.filter((w) => w.type !== 'profile_header');
    nonHeader.forEach((w) => {
      w.x = 0;
      w.w = 12;
      w.y = curY;
      curY += (w.h || 2);
    });

    // 依 y 排序 widgets
    widgets.sort((a, b) => (a.y || 0) - (b.y || 0));
  }

  function moveWidgetOrder(widgetId, delta) {
    const gridEl = document.getElementById('profile-grid');
    if (!gridEl) return;

    const cardEl = gridEl.querySelector(`.grid-stack-item[gs-id="${widgetId}"]`);
    if (!cardEl || cardEl.getAttribute('gs-no-move') === 'true') {
      showToast('名片資訊固定置頂，無法移動', 'warning');
      return;
    }

    if (delta < 0) {
      // 向上移動
      const prev = cardEl.previousElementSibling;
      if (!prev || prev.getAttribute('gs-no-move') === 'true') {
        showToast('已在最頂端', 'info');
        return;
      }
      gridEl.insertBefore(cardEl, prev);
    } else {
      // 向下移動
      const next = cardEl.nextElementSibling;
      if (!next) {
        showToast('已在最底端', 'info');
        return;
      }
      gridEl.insertBefore(next, cardEl);
    }

    // 依目前 DOM 節點順序更新 widgets 陣列
    const domItems = Array.from(gridEl.querySelectorAll('.grid-stack-item'));
    const orderedIds = domItems.map((el) => el.getAttribute('gs-id'));
    widgets.sort((a, b) => {
      const idxA = orderedIds.indexOf(a.id);
      const idxB = orderedIds.indexOf(b.id);
      return (idxA === -1 ? 999 : idxA) - (idxB === -1 ? 999 : idxB);
    });

    normalizeMobileLayoutToDesktop();
    updateGlassSections();
    showToast('已調整卡片順序', 'info');
  }

  // ==================== Apple Liquid Glass 游標動態高光追蹤 ====================
  function setupGlobalSpecularTracking() {
    const container = document.getElementById('profile-container');
    if (!container) return;

    container.addEventListener('mousemove', (e) => {
      const backdrops = document.querySelectorAll('.grid-section-glass-backdrop');
      backdrops.forEach((el) => {
        const rect = el.getBoundingClientRect();
        if (e.clientX >= rect.left - 50 && e.clientX <= rect.right + 50 &&
            e.clientY >= rect.top - 50 && e.clientY <= rect.bottom + 50) {
          const x = ((e.clientX - rect.left) / rect.width) * 100;
          const y = ((e.clientY - rect.top) / rect.height) * 100;
          el.style.setProperty('--mouse-x', `${x.toFixed(1)}%`);
          el.style.setProperty('--mouse-y', `${y.toFixed(1)}%`);
        }
      });

      const cards = document.querySelectorAll('.grid-stack-item-content');
      cards.forEach((el) => {
        const rect = el.getBoundingClientRect();
        if (e.clientX >= rect.left - 30 && e.clientX <= rect.right + 30 &&
            e.clientY >= rect.top - 30 && e.clientY <= rect.bottom + 30) {
          const x = ((e.clientX - rect.left) / rect.width) * 100;
          const y = ((e.clientY - rect.top) / rect.height) * 100;
          el.style.setProperty('--mouse-x', `${x.toFixed(1)}%`);
          el.style.setProperty('--mouse-y', `${y.toFixed(1)}%`);
        }
      });
    }, { passive: true });
  }

  // ==================== 手機觸控拖曳排序支援 ====================
  function setupMobileTouchDrag() {
    const gridEl = document.getElementById('profile-grid');
    if (!gridEl) return;

    let touchDraggedItem = null;
    let initialY = 0;
    let currentY = 0;

    gridEl.addEventListener('touchstart', (e) => {
      const handle = e.target.closest('.card-drag-handle');
      if (!handle || !isEditing) return;

      const item = handle.closest('.grid-stack-item');
      if (!item || item.getAttribute('gs-no-move') === 'true') return;

      e.preventDefault();

      const touch = e.touches[0];
      touchDraggedItem = item;
      initialY = touch.clientY;
      currentY = touch.clientY;

      item.classList.add('is-touch-dragging');
      item.style.zIndex = '999';
      item.style.opacity = '0.85';
      item.style.boxShadow = '0 12px 30px rgba(0,0,0,0.6)';
    }, { passive: false });

    gridEl.addEventListener('touchmove', (e) => {
      if (!touchDraggedItem) return;
      e.preventDefault();

      const touch = e.touches[0];
      currentY = touch.clientY;
      const diffY = currentY - initialY;
      touchDraggedItem.style.transform = `translateY(${diffY}px) scale(1.02)`;

      touchDraggedItem.style.pointerEvents = 'none';
      const elemBelow = document.elementFromPoint(touch.clientX, touch.clientY);
      touchDraggedItem.style.pointerEvents = '';

      const targetItem = elemBelow?.closest('.grid-stack-item');
      if (targetItem && targetItem !== touchDraggedItem && targetItem.getAttribute('gs-no-move') !== 'true') {
        const rect = targetItem.getBoundingClientRect();
        const midY = rect.top + rect.height / 2;
        if (currentY < midY) {
          targetItem.parentNode.insertBefore(touchDraggedItem, targetItem);
        } else {
          targetItem.parentNode.insertBefore(touchDraggedItem, targetItem.nextSibling);
        }
      }
    }, { passive: false });

    const finishTouchDrag = () => {
      if (!touchDraggedItem) return;
      touchDraggedItem.classList.remove('is-touch-dragging');
      touchDraggedItem.style.zIndex = '';
      touchDraggedItem.style.opacity = '';
      touchDraggedItem.style.boxShadow = '';
      touchDraggedItem.style.transform = '';
      touchDraggedItem = null;

      // 依目前 DOM 順序重整 widgets
      const domItems = Array.from(gridEl.querySelectorAll('.grid-stack-item'));
      const orderedIds = domItems.map((el) => el.getAttribute('gs-id'));
      widgets.sort((a, b) => {
        const idxA = orderedIds.indexOf(a.id);
        const idxB = orderedIds.indexOf(b.id);
        return (idxA === -1 ? 999 : idxA) - (idxB === -1 ? 999 : idxB);
      });

      normalizeMobileLayoutToDesktop();
      updateGlassSections();
      showToast('已更新卡片順序', 'info');
    };

    gridEl.addEventListener('touchend', finishTouchDrag);
    gridEl.addEventListener('touchcancel', finishTouchDrag);
  }

  function setupEvents() {
    // 1. 模式分段切換器 [ 編輯模式 | 預覽效果 ] (含容器事件委派與按鈕綁定，杜絕點擊未觸發鎖定)
    const modeToggle = document.getElementById('mode-toggle');
    if (modeToggle) {
      modeToggle.addEventListener('click', function (e) {
        const optionBtn = e.target.closest('.toggle-option');
        if (!optionBtn) return;
        const mode = optionBtn.getAttribute('data-mode');
        if (mode === 'edit') {
          applyModeState(true);
        } else if (mode === 'preview') {
          applyModeState(false);
        }
      });
    }

    const btnEdit = document.getElementById('btn-mode-edit');
    const btnPreview = document.getElementById('btn-mode-preview');

    if (btnEdit) {
      btnEdit.addEventListener('click', function (e) {
        e.stopPropagation();
        applyModeState(true);
      });
    }
    if (btnPreview) {
      btnPreview.addEventListener('click', function (e) {
        e.stopPropagation();
        applyModeState(false);
      });
    }

    // 2. 儲存變更按鈕
    const btnSave = document.getElementById('btn-save-profile');
    if (btnSave) {
      btnSave.addEventListener('click', saveProfileToBackend);
    }

    // 3. 底部「新增卡片」按鈕 (展開縮小版互動 Palette)
    const btnAddCard = document.getElementById('btn-add-card');
    if (btnAddCard) {
      btnAddCard.addEventListener('click', function () {
        updateAddCardPaletteState();
        openModal('modal-add-card');
      });
    }

    // 4. 卡片內部專屬操作 (設定連結、刪除卡片、旋轉分隔線、HackMD 格式套用、編輯/預覽切換)
    const gridEl = document.getElementById('profile-grid');
    if (gridEl) {
      gridEl.addEventListener('click', function (e) {
        // 4.0 自訂標籤 (Hashtag) 新增與刪除
        const addTagBtn = e.target.closest('.btn-add-hashtag');
        if (addTagBtn) {
          e.preventDefault();
          e.stopPropagation();
          const widgetId = addTagBtn.getAttribute('data-widget-id');
          const widget = widgets.find((w) => w.id === widgetId);
          if (widget) {
            if (!widget.content) widget.content = {};
            if (!Array.isArray(widget.content.hashtags)) {
              widget.content.hashtags = widget.content.badge ? [widget.content.badge] : ['炙彗社群', '次世代機器人'];
            }
            if (widget.content.hashtags.length >= 5) {
              showToast('自訂標籤已達上限 (最多 5 個)', 'warning');
              return;
            }
            widget.content.hashtags.push('新標籤');
            refreshCardContent(widgetId);
            setTimeout(() => {
              const cardEl = document.querySelector(`.grid-stack-item[gs-id="${widgetId}"]`);
              const tagTexts = cardEl?.querySelectorAll('.header-hashtags-list .hashtag-text');
              if (tagTexts && tagTexts.length > 0) {
                const lastTag = tagTexts[tagTexts.length - 1];
                lastTag.focus();
                const range = document.createRange();
                range.selectNodeContents(lastTag);
                const sel = window.getSelection();
                sel.removeAllRanges();
                sel.addRange(range);
              }
            }, 60);
          }
          return;
        }

        const removeTagBtn = e.target.closest('.btn-remove-tag');
        if (removeTagBtn) {
          e.preventDefault();
          e.stopPropagation();
          const widgetId = removeTagBtn.getAttribute('data-widget-id');
          const tagIdx = parseInt(removeTagBtn.getAttribute('data-tag-index'), 10);
          const widget = widgets.find((w) => w.id === widgetId);
          if (widget && Array.isArray(widget.content?.hashtags)) {
            widget.content.hashtags.splice(tagIdx, 1);
            refreshCardContent(widgetId);
          }
          return;
        }

        // 4.01 剪刀分離/合併兩區塊 (支援上下水平與左右垂直雙向分離，官網 24px 間距)
        const scissorBtn = e.target.closest('.btn-scissor-split');
        if (scissorBtn) {
          e.preventDefault();
          e.stopPropagation();
          const cutDir = scissorBtn.getAttribute('data-cut-dir');
          if (cutDir === 'v') {
            const cutKey = scissorBtn.getAttribute('data-cut-key');
            if (!Array.isArray(themeConfig.section_splits_x)) {
              themeConfig.section_splits_x = [];
            }
            const idx = themeConfig.section_splits_x.indexOf(cutKey);
            if (idx !== -1) {
              themeConfig.section_splits_x.splice(idx, 1);
              showToast('已合併左右區塊', 'info');
            } else {
              themeConfig.section_splits_x.push(cutKey);
              showToast('已分離左右區塊 (官網標準 24px 間距)', 'success');
            }
            updateGlassSections();
          } else {
            const cutY = parseInt(scissorBtn.getAttribute('data-cut-y'), 10);
            if (!isNaN(cutY)) {
              if (!Array.isArray(themeConfig.section_splits)) {
                themeConfig.section_splits = [];
              }
              const idx = themeConfig.section_splits.indexOf(cutY);
              if (idx !== -1) {
                themeConfig.section_splits.splice(idx, 1);
                showToast('已合併上下區塊', 'info');
              } else {
                themeConfig.section_splits.push(cutY);
                themeConfig.section_splits.sort((a, b) => a - b);
                showToast('已分離上下區塊 (官網標準 24px 間距)', 'success');
              }
              updateGlassSections();
            }
          }
          return;
        }

        // 4.01 手機端卡片上下移動排序
        const moveUpBtn = e.target.closest('.btn-move-up');
        if (moveUpBtn) {
          e.preventDefault();
          e.stopPropagation();
          const id = moveUpBtn.getAttribute('data-id');
          moveWidgetOrder(id, -1);
          return;
        }

        const moveDownBtn = e.target.closest('.btn-move-down');
        if (moveDownBtn) {
          e.preventDefault();
          e.stopPropagation();
          const id = moveDownBtn.getAttribute('data-id');
          moveWidgetOrder(id, 1);
          return;
        }

        // 4.02 社群區塊快速新增連結
        const addSocialBtn = e.target.closest('.btn-add-social-btn');
        if (addSocialBtn) {
          e.preventDefault();
          e.stopPropagation();
          const wId = addSocialBtn.getAttribute('data-widget-id');
          if (wId) {
            openSocialButtonModal(wId, null);
          }
          return;
        }

        // 4.03 社群按鈕獨立刪除
        const removeSocialBtn = e.target.closest('.btn-remove-social-btn');
        if (removeSocialBtn) {
          e.preventDefault();
          e.stopPropagation();
          const wId = removeSocialBtn.getAttribute('data-widget-id');
          const lId = removeSocialBtn.getAttribute('data-link-id');
          const targetWidget = widgets.find((w) => w.id === wId);
          if (targetWidget && targetWidget.type === 'social_link' && Array.isArray(targetWidget.content?.links)) {
            const lIdx = targetWidget.content.links.findIndex((l) => l.id === lId);
            if (lIdx !== -1) {
              targetWidget.content.links.splice(lIdx, 1);
              refreshCardContent(wId);
              debouncedAutoAdjustWidgetHeight(wId);
              showToast('已移除社群連結', 'info');
            }
          }
          return;
        }

        // 4.04 編輯模式下點擊單一社群按鈕開啟按鈕編輯
        if (isEditing) {
          const socialWrap = e.target.closest('.social-btn-wrap');
          if (socialWrap && !wasDraggingSocialBtn) {
            e.preventDefault();
            e.stopPropagation();
            const wId = socialWrap.getAttribute('data-widget-id');
            const lId = socialWrap.getAttribute('data-link-id');
            if (wId && lId) {
              openSocialButtonModal(wId, lId);
            }
            return;
          }
        }

        // 4.1 設定連結
        const editBtn = e.target.closest('.btn-edit-card');
        if (editBtn) {
          const id = editBtn.getAttribute('data-id');
          openEditModal(id);
          return;
        }

        // 4.1.1 編輯模式下點擊圖片區快速開啟裁切與縮放設定
        const mediaImageEl = e.target.closest('.media-image-holder');
        if (mediaImageEl && isEditing && !isGridDragging && !e.target.closest('[data-inline-edit]')) {
          const cardItem = mediaImageEl.closest('.grid-stack-item');
          const id = cardItem ? cardItem.getAttribute('gs-id') : null;
          if (id) {
            openEditModal(id);
            return;
          }
        }

        // 4.2 刪除卡片 (個人基礎資訊卡片禁用刪除)
        const delBtn = e.target.closest('.card-control-btn-del');
        if (delBtn) {
          const id = delBtn.getAttribute('data-id');
          const target = widgets.find((w) => w.id === id);
          if (target && target.type === 'profile_header') {
            showToast('個人基礎資訊卡片為必備項目，無法刪除', 'warning');
            return;
          }
          showConfirmModal({
            title: '確認刪除卡片',
            message: '確定要刪除這張卡片嗎？此操作將自畫布移除該卡片，儲存後生效。',
            confirmText: '確認刪除',
            cancelText: '取消',
            onConfirm: () => {
              deleteCard(id);
            },
          });
          return;
        }

        // 4.3 旋轉分隔線 90° (短按分隔線直接旋轉)
        const dividerEl = e.target.closest('.widget-divider-container');
        if (dividerEl && isEditing) {
          if (e.target.closest('.card-control-btn-del')) return;
          const id = dividerEl.getAttribute('data-widget-id');
          if (id) {
            rotateDivider(id);
            return;
          }
        }

        const rotateBtn = e.target.closest('.btn-rotate-divider');
        if (rotateBtn) {
          const id = rotateBtn.getAttribute('data-id');
          if (id) rotateDivider(id);
          return;
        }

        // 4.4 HackMD 快捷格式工具列點按 (支援獨立 Markdown 卡片與社群嵌入 Markdown 編輯器)
        const toolBtn = e.target.closest('.md-tool-btn');
        if (toolBtn) {
          e.preventDefault();
          e.stopPropagation();
          const format = toolBtn.getAttribute('data-format');
          const cardEl = toolBtn.closest('.widget-text-card, .widget-social-link');
          const textarea = cardEl?.querySelector('.markdown-textarea-input');
          if (textarea && format) {
            applyMarkdownFormatting(textarea, format);
            const wItem = cardEl.closest('.grid-stack-item');
            const wId = wItem?.getAttribute('gs-id');
            if (wId) {
              debouncedAutoAdjustWidgetHeight(wId);
            }
          }
          return;
        }


      });

      // 監聽 Markdown Textarea 即時輸入，即時更新資料模型並自動根據文字長度調整高度
      gridEl.addEventListener('input', function (e) {
        const target = e.target;
        if (target && target.classList.contains('markdown-textarea-input')) {
          const widgetId = target.getAttribute('data-widget-id');
          const widget = widgets.find((w) => w.id === widgetId);
          if (widget) {
            if (!widget.content) widget.content = {};
            widget.content.body = target.value;
            debouncedAutoAdjustWidgetHeight(widgetId);
          }
        }
      });

      // 4.6 社群矩陣按鈕在區域內自由拖動排序 (支援跨行與同列精準插入，絕無晃動與異常增高)
      let draggedWidgetId = null;
      let wasDraggingSocialBtn = false;

      gridEl.addEventListener('dragstart', function (e) {
        const btnWrap = e.target.closest('.social-btn-wrap');
        if (btnWrap && isEditing) {
          wasDraggingSocialBtn = true;
          draggedSocialBtn = btnWrap;
          draggedWidgetId = btnWrap.getAttribute('data-widget-id');
          btnWrap.classList.add('is-dragging-social-btn');
          if (e.dataTransfer) {
            e.dataTransfer.setData('text/plain', 'social-btn');
            e.dataTransfer.effectAllowed = 'move';
          }
        }
      });

      gridEl.addEventListener('dragover', function (e) {
        if (!draggedSocialBtn) return;
        const container = e.target.closest('.social-buttons-container');
        if (!container) return;

        e.preventDefault();
        e.stopPropagation();
        if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';

        const btnWraps = Array.from(container.querySelectorAll('.social-btn-wrap:not(.is-dragging-social-btn)'));
        if (btnWraps.length === 0) {
          if (container.lastElementChild !== draggedSocialBtn) {
            container.appendChild(draggedSocialBtn);
          }
          return;
        }

        // 依滑鼠座標找到最佳插入點 (支援拖曳到下一行或行首行尾)
        let afterElement = null;
        for (const child of btnWraps) {
          const box = child.getBoundingClientRect();
          const isAbove = e.clientY < box.top;
          const isSameLine = e.clientY >= box.top && e.clientY <= box.bottom;
          const isLeft = e.clientX < box.left + box.width / 2;

          if (isAbove || (isSameLine && isLeft)) {
            afterElement = child;
            break;
          }
        }

        if (afterElement) {
          if (draggedSocialBtn.nextSibling !== afterElement) {
            container.insertBefore(draggedSocialBtn, afterElement);
          }
        } else {
          if (container.lastElementChild !== draggedSocialBtn) {
            container.appendChild(draggedSocialBtn);
          }
        }
      });

      gridEl.addEventListener('dragend', function (e) {
        if (draggedSocialBtn) {
          draggedSocialBtn.classList.remove('is-dragging-social-btn');
          if (draggedWidgetId) {
            const widget = widgets.find((w) => w.id === draggedWidgetId);
            const container = document.querySelector(`.grid-stack-item[gs-id="${draggedWidgetId}"] .social-buttons-container`);
            if (widget && container && Array.isArray(widget.content?.links)) {
              const reorderedIds = Array.from(container.querySelectorAll('.social-btn-wrap')).map((el) => el.getAttribute('data-link-id'));
              widget.content.links.sort((a, b) => {
                const idxA = reorderedIds.indexOf(a.id);
                const idxB = reorderedIds.indexOf(b.id);
                return (idxA === -1 ? 999 : idxA) - (idxB === -1 ? 999 : idxB);
              });
            }
          }
          draggedSocialBtn = null;
          draggedWidgetId = null;
          setTimeout(() => {
            wasDraggingSocialBtn = false;
          }, 150);
        }
      });
    }

    // 5. 新增卡片視覺互動 Palette 拖曳與點擊初始化
    setupPaletteDragAndDrop();

    // 6. Panel 同款確認視窗按鈕綁定
    const modalConfirmOk = document.getElementById('modal-confirm-ok');
    if (modalConfirmOk) {
      modalConfirmOk.addEventListener('click', function () {
        const cb = pendingConfirmCallback;
        closeModal('modal-confirm');
        if (typeof cb === 'function') {
          cb();
        }
      });
    }

    // 7. 確定儲存卡片設定 Modal
    const btnConfirmEdit = document.getElementById('btn-confirm-edit-card');
    if (btnConfirmEdit) {
      btnConfirmEdit.addEventListener('click', saveCardFromModal);
    }

    // 8. 外觀主題 Modal 與色盤切換
    const btnThemeModal = document.getElementById('btn-theme-modal');
    if (btnThemeModal) {
      btnThemeModal.addEventListener('click', function () {
        document.querySelectorAll('.color-swatch').forEach((swatch) => {
          if (swatch.getAttribute('data-color') === themeConfig.glow_color) {
            swatch.classList.add('active');
          } else {
            swatch.classList.remove('active');
          }
        });
        const radiusSelect = document.getElementById('theme-radius-select');
        if (radiusSelect) radiusSelect.value = themeConfig.card_radius || '24px';
        openModal('modal-theme');
      });
    }

    document.querySelectorAll('.color-swatch').forEach((swatch) => {
      swatch.addEventListener('click', function () {
        document.querySelectorAll('.color-swatch').forEach((s) => s.classList.remove('active'));
        this.classList.add('active');
      });
    });

    const btnApplyTheme = document.getElementById('btn-apply-theme');
    if (btnApplyTheme) {
      btnApplyTheme.addEventListener('click', function () {
        const activeSwatch = document.querySelector('.color-swatch.active');
        if (activeSwatch) {
          themeConfig.glow_color = activeSwatch.getAttribute('data-color');
        }
        const radiusSelect = document.getElementById('theme-radius-select');
        if (radiusSelect) {
          themeConfig.card_radius = radiusSelect.value;
        }
        applyThemeStyles();
        closeModal('modal-theme');
        showToast('已套用外觀主題 (請記得儲存變更)', 'success');
      });
    }

    // 9. 頂部操作列「分享主頁」按鈕
    const actionBtnShare = document.getElementById('action-btn-share');
    if (actionBtnShare) {
      actionBtnShare.addEventListener('click', function () {
        initShareModal();
        openModal('modal-share');
      });
    }

    // 9.9 獨立操作列「?」導覽教學按鈕
    const actionBtnTour = document.getElementById('action-btn-tour');
    if (actionBtnTour) {
      actionBtnTour.addEventListener('click', function (e) {
        e.preventDefault();
        if (!isEditing && isOwner) {
          setViewMode('edit');
        }
        startSpotlightTour(true);
      });
    }

    // 10. 獨立操作列「複製連結」按鈕
    const actionBtnCopy = document.getElementById('action-btn-copy');
    if (actionBtnCopy) {
      actionBtnCopy.addEventListener('click', function () {
        const url = this.getAttribute('data-url') || profileData.canonical_url;
        copyToClipboard(url, this, '已複製連結', '已複製個人主頁連結至剪貼簿');
      });
    }

    // 11. 分享 Modal 內的複製網址與宣傳文案按鈕
    const btnShareCopyUrl = document.getElementById('btn-share-copy-url');
    if (btnShareCopyUrl) {
      btnShareCopyUrl.addEventListener('click', function () {
        const input = document.getElementById('share-link-input');
        if (input) {
          copyToClipboard(input.value, this, '已複製網址', '已複製個人主頁網址');
        }
      });
    }

    const btnShareCopyPromo = document.getElementById('btn-share-copy-promo');
    if (btnShareCopyPromo) {
      btnShareCopyPromo.addEventListener('click', function () {
        const input = document.getElementById('share-promo-input');
        if (input) {
          copyToClipboard(input.value, this, '已複製文案', '已複製 Discord 宣傳推廣文案');
        }
      });
    }

    // 12. Modal 關閉按鈕綁定
    document.querySelectorAll('[data-close]').forEach((btn) => {
      btn.addEventListener('click', function () {
        const targetId = this.getAttribute('data-close');
        closeModal(targetId);
      });
    });

    // 13. 修復滑鼠在編輯視窗外放開時異常關閉 (嚴格分離 mousedown 與 mouseup 來源)
    document.querySelectorAll('.modal-overlay').forEach((overlay) => {
      let isMouseDownOnBackdrop = false;

      overlay.addEventListener('mousedown', function (e) {
        isMouseDownOnBackdrop = (e.target === this);
      });

      overlay.addEventListener('mouseup', function (e) {
        if (isMouseDownOnBackdrop && e.target === this) {
          closeModal(this.id);
        }
        isMouseDownOnBackdrop = false;
      });
    });
  }

  // ==================== 14. 首次登入編輯介面聚光燈導覽 (Spotlight Tour) ====================
  let currentTourStep = 0;
  let isTourActive = false;

  const TOUR_STEPS = [
    {
      target: () => document.querySelector('.widget-profile-header')?.closest('.grid-stack-item') || document.querySelector('.widget-profile-header'),
      title: '個人名片區塊',
      desc: '這是你的核心個人名片（永久置頂）。點擊暱稱、簡介或標籤可直接輸入修改文字，拖曳右下角把手可自由調整寬度與高度。',
    },
    {
      target: () => {
        // 核心例外處理：優先聚焦文字區塊 (Markdown)；若已被刪除，智慧退回第一個非置頂的可移動卡片；若畫布全空，退回整張畫布 #profile-grid
        const textCard = document.querySelector('.widget-text-card')?.closest('.grid-stack-item');
        if (textCard) return textCard;
        const anyMovable = document.querySelector('.grid-stack-item:not([gs-no-move="true"])');
        if (anyMovable) return anyMovable;
        const anyCard = document.querySelector('.grid-stack-item');
        if (anyCard) return anyCard;
        return document.getElementById('profile-grid');
      },
      title: '自由排版與內容區塊',
      desc: '文字區塊支援 Markdown 格式與即時編輯；卡片可自由拖曳調整排列順序，亦可拖曳邊框縮放大小。',
    },
    {
      target: () => document.getElementById('btn-add-card'),
      title: '新增豐富卡片',
      desc: '點擊可開啟卡片面板，包含文字區塊、社群連結、圖片或YouTube嵌入及經濟數據，支援直接點擊新增或拖曳至畫布。',
    },
    {
      target: () => document.getElementById('mode-toggle'),
      title: '編輯與預覽切換',
      desc: '隨時切換「編輯模式」與「預覽效果」，即時預覽訪客看到的真實玻璃卡片與無框排版效果。',
    },
    {
      target: () => document.getElementById('btn-save-profile'),
      title: '儲存個人檔案',
      desc: '完成排版與文字設定後，點擊「儲存變更」即可立即將你的專屬主頁同步至伺服器並公開展示！',
    },
  ];

  function startSpotlightTour(force = false) {
    if (!isOwner) return;
    if (!isEditing) {
      setViewMode('edit');
    }
    const tourSeen = localStorage.getItem('jihui_profile_tour_seen');
    if (tourSeen && !force) return;

    const overlay = document.getElementById('spotlight-tour-overlay');
    if (!overlay) return;

    isTourActive = true;
    currentTourStep = 0;
    overlay.style.display = 'block';
    overlay.setAttribute('aria-hidden', 'false');

    renderTourStep(currentTourStep);
    bindTourKeyboard();
  }

  function renderTourStep(stepIndex) {
    const overlay = document.getElementById('spotlight-tour-overlay');
    const spotlightBox = document.getElementById('tour-spotlight-box');
    const tooltipBox = document.getElementById('tour-tooltip-box');
    const titleEl = document.getElementById('tour-step-title');
    const descEl = document.getElementById('tour-step-desc');
    const indicatorEl = document.getElementById('tour-step-indicator');
    const nextBtn = document.getElementById('tour-btn-next');

    if (!overlay || !spotlightBox || !tooltipBox) return;

    if (stepIndex < 0 || stepIndex >= TOUR_STEPS.length) {
      endSpotlightTour();
      return;
    }

    const step = TOUR_STEPS[stepIndex];
    let targetEl = typeof step.target === 'function' ? step.target() : step.target;

    if (indicatorEl) indicatorEl.textContent = `${stepIndex + 1} / ${TOUR_STEPS.length}`;
    if (titleEl) titleEl.textContent = step.title;
    if (descEl) descEl.textContent = step.desc;
    if (nextBtn) {
      nextBtn.textContent = (stepIndex === TOUR_STEPS.length - 1) ? '完成 (Enter)' : '下一步 (Enter)';
    }

    // 例外處理：若目標不存在或已被使用者刪除，智慧降級至畫布或自動進入下一步
    if (!targetEl || !document.body.contains(targetEl)) {
      const fallback = document.querySelector('.grid-stack-item:not([gs-no-move="true"])') || document.getElementById('profile-grid');
      if (fallback) {
        targetEl = fallback;
      } else if (stepIndex + 1 < TOUR_STEPS.length) {
        currentTourStep++;
        renderTourStep(currentTourStep);
        return;
      } else {
        endSpotlightTour();
        return;
      }
    }

    function updateSpotlightPos() {
      if (!isTourActive || !targetEl || !document.body.contains(targetEl)) return;
      const rect = targetEl.getBoundingClientRect();
      const pad = 6;
      const top = Math.max(0, rect.top - pad);
      const left = Math.max(0, rect.left - pad);
      const width = rect.width + pad * 2;
      const height = rect.height + pad * 2;

      spotlightBox.style.top = `${top}px`;
      spotlightBox.style.left = `${left}px`;
      spotlightBox.style.width = `${width}px`;
      spotlightBox.style.height = `${height}px`;

      const tipWidth = Math.min(340, window.innerWidth - 32);
      const tipHeight = 200;
      let tipTop = rect.bottom + 16;
      let tipLeft = Math.max(16, Math.min(rect.left + (rect.width - tipWidth) / 2, window.innerWidth - tipWidth - 16));

      if (tipTop + tipHeight > window.innerHeight - 20) {
        tipTop = Math.max(20, rect.top - tipHeight - 16);
      }

      tooltipBox.style.top = `${tipTop}px`;
      tooltipBox.style.left = `${tipLeft}px`;
    }

    // 核心修復：立即對齊並在平滑捲動過程中持續透過 requestAnimationFrame 即時更新座標，徹底杜絕位置偏低或偏移
    updateSpotlightPos();
    targetEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
    const scrollStartTime = performance.now();
    function trackScrollLoop() {
      if (!isTourActive) return;
      updateSpotlightPos();
      if (performance.now() - scrollStartTime < 500) {
        requestAnimationFrame(trackScrollLoop);
      }
    }
    requestAnimationFrame(trackScrollLoop);
  }

  function nextTourStep() {
    if (!isTourActive) return;
    if (currentTourStep + 1 < TOUR_STEPS.length) {
      currentTourStep++;
      renderTourStep(currentTourStep);
    } else {
      endSpotlightTour();
      showToast('導覽結束！隨時開始打造專屬個人檔案吧', 'success');
    }
  }

  function endSpotlightTour() {
    isTourActive = false;
    const overlay = document.getElementById('spotlight-tour-overlay');
    if (overlay) {
      overlay.style.display = 'none';
      overlay.setAttribute('aria-hidden', 'true');
    }
    localStorage.setItem('jihui_profile_tour_seen', 'true');
    unbindTourKeyboard();
  }

  function handleTourKeyDown(e) {
    if (!isTourActive) return;
    if (e.key === 'Enter') {
      e.preventDefault();
      e.stopPropagation();
      nextTourStep();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      endSpotlightTour();
    }
  }

  function bindTourKeyboard() {
    document.addEventListener('keydown', handleTourKeyDown, true);
  }

  function unbindTourKeyboard() {
    document.removeEventListener('keydown', handleTourKeyDown, true);
  }

  function setupSpotlightTourEvents() {
    const nextBtn = document.getElementById('tour-btn-next');
    const skipBtn = document.getElementById('tour-btn-skip');
    const closeBtn = document.getElementById('tour-btn-close');

    if (nextBtn) nextBtn.addEventListener('click', nextTourStep);
    if (skipBtn) skipBtn.addEventListener('click', endSpotlightTour);
    if (closeBtn) closeBtn.addEventListener('click', endSpotlightTour);
  }

  window.jihuiStartTour = () => startSpotlightTour(true);

  // ==================== 15. 啟動入口 ====================
  document.addEventListener('DOMContentLoaded', function () {
    applyThemeStyles();
    initGrid();
    setupInlineEditListeners();
    setupEvents();
    setupSpotlightTourEvents();
    setupGlobalSpecularTracking();
    setupMobileTouchDrag();
    window.addEventListener('resize', updateGlassSections);
    setTimeout(() => {
      startSpotlightTour();
    }, 600);
  });
})();
