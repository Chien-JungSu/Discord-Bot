/**
 * 炙彗JI器人 - 全站頂部導航列與指令文檔側邊欄互動腳本
 * 包含：
 * 1. 頂部 Bar 最左側選單符號點擊動畫 (☰ <-> ✕) 與全站導航展開/關閉
 * 2. 指令文檔頁面 (docs.html) 專屬左側邊欄：點擊標籤 UI 或鼠標抵達最左側邊緣時自動展開
 */

(function () {
  'use strict';

  let closeSidebarFn = null;

  // ==========================================================================
  // 1. 全站頂端 Bar 選單動畫與開關邏輯
  // ==========================================================================
  const menuBtn = document.getElementById('site-menu-btn');
  const navDrawer = document.getElementById('site-nav-drawer');
  const navBackdrop = document.getElementById('site-nav-backdrop');
  const userBtn = document.getElementById('topbar-user-btn');
  const userDropdown = document.getElementById('topbar-user-dropdown');

  function isAnyGlobalMenuOpen() {
    return !!(
      (navDrawer && navDrawer.classList.contains('is-open')) ||
      (userDropdown && userDropdown.classList.contains('is-open'))
    );
  }

  function updateNavDrawerPosition() {
    if (!navDrawer) return;
    const topbar = document.getElementById('site-topbar');
    if (topbar) {
      const rect = topbar.getBoundingClientRect();
      const gap = 8;
      navDrawer.style.top = `${Math.round(rect.bottom + gap)}px`;

      if (window.innerWidth > 640) {
        navDrawer.style.left = `${Math.round(rect.left)}px`;
        navDrawer.style.width = '280px';
      } else {
        navDrawer.style.left = '12px';
        navDrawer.style.width = 'calc(100vw - 24px)';
      }
    }
  }

  function toggleSiteNav(force) {
    if (!menuBtn || !navDrawer) return;
    const isOpen = typeof force === 'boolean' ? force : !menuBtn.classList.contains('is-active');

    if (isOpen) {
      updateNavDrawerPosition();
    }

    menuBtn.classList.toggle('is-active', isOpen);
    menuBtn.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
    navDrawer.classList.toggle('is-open', isOpen);
    document.body.classList.toggle('site-nav-open', isOpen);
    if (navBackdrop) {
      navBackdrop.classList.toggle('is-open', isOpen);
    }

    if (isOpen) {
      toggleUserDropdown(false);
      if (typeof closeSidebarFn === 'function') {
        closeSidebarFn();
      }
    }
  }

  if (menuBtn) {
    menuBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleSiteNav();
    });
  }

  if (navBackdrop) {
    navBackdrop.addEventListener('click', () => toggleSiteNav(false));
  }

  // 點擊選單內部連結後自動關閉選單
  if (navDrawer) {
    navDrawer.querySelectorAll('a').forEach((link) => {
      link.addEventListener('click', () => toggleSiteNav(false));
    });
  }

  // 視窗縮放或滾動時，若選單已展開則即時更新對齊位置
  window.addEventListener('resize', () => {
    if (navDrawer && navDrawer.classList.contains('is-open')) {
      updateNavDrawerPosition();
    }
  });

  window.addEventListener('scroll', () => {
    if (navDrawer && navDrawer.classList.contains('is-open')) {
      updateNavDrawerPosition();
    }
  }, { passive: true });

  // 按 ESC 鍵關閉全站選單與使用者下拉
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      toggleSiteNav(false);
      toggleUserDropdown(false);
    }
  });

  // ==========================================================================
  // 1-2. 頂部 Bar 使用者頭像點擊彈出下拉選單
  // ==========================================================================
  function toggleUserDropdown(force) {
    if (!userBtn || !userDropdown) return;
    const isOpen = typeof force === 'boolean' ? force : !userDropdown.classList.contains('is-open');
    userDropdown.classList.toggle('is-open', isOpen);
    userBtn.classList.toggle('is-active', isOpen);
    userBtn.setAttribute('aria-expanded', isOpen ? 'true' : 'false');

    if (isOpen) {
      toggleSiteNav(false);
      if (typeof closeSidebarFn === 'function') {
        closeSidebarFn();
      }
    }
  }

  if (userBtn && userDropdown) {
    userBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleUserDropdown();
    });

    document.addEventListener('click', (e) => {
      if (!userDropdown.contains(e.target) && !userBtn.contains(e.target)) {
        toggleUserDropdown(false);
      }
    });
  }

  // ==========================================================================
  // 2. docs.html 專屬指令目錄側邊欄：鼠標抵達最左側或點擊 UI 標籤時展開
  // ==========================================================================
  const docsSidebar = document.getElementById('docs-sidebar');
  const sidebarTrigger = document.getElementById('docs-sidebar-trigger');
  const sidebarClose = document.getElementById('docs-sidebar-close');
  const sidebarBackdrop = document.getElementById('docs-sidebar-backdrop');
  const edgeHotspot = document.getElementById('docs-edge-hotspot');
  const sidebarList = document.getElementById('docs-sidebar-list');

  if (docsSidebar) {
    let closeTimer = null;
    let isPinned = false;

    function openSidebar(pinned = false) {
      // 若全站導航選單或頭像選單為開啟狀態，禁止打開目錄
      if (isAnyGlobalMenuOpen()) return;

      if (closeTimer) clearTimeout(closeTimer);
      if (pinned) isPinned = true;
      docsSidebar.classList.add('is-open');
      if (sidebarBackdrop && (isPinned || window.innerWidth <= 768)) {
        sidebarBackdrop.classList.add('is-open');
      }
      if (sidebarTrigger) {
        sidebarTrigger.classList.add('is-active');
        sidebarTrigger.setAttribute('aria-label', '收合指令目錄');
        sidebarTrigger.setAttribute('title', '收合指令目錄');
      }
    }

    function scheduleCloseSidebar(delay = 280) {
      if (isPinned) return; // 若被點擊釘住，移出滑鼠不自動關閉
      if (closeTimer) clearTimeout(closeTimer);
      closeTimer = setTimeout(() => {
        closeSidebar();
      }, delay);
    }

    function closeSidebar() {
      if (closeTimer) clearTimeout(closeTimer);
      isPinned = false;
      docsSidebar.classList.remove('is-open');
      if (sidebarBackdrop) sidebarBackdrop.classList.remove('is-open');
      if (sidebarTrigger) {
        sidebarTrigger.classList.remove('is-active');
        sidebarTrigger.setAttribute('aria-label', '展開指令目錄');
        sidebarTrigger.setAttribute('title', '展開指令目錄');
      }
    }

    closeSidebarFn = closeSidebar;

    // 觸發方式 A：點擊展開 / 收合 UI 標籤
    if (sidebarTrigger) {
      sidebarTrigger.addEventListener('mouseenter', () => {
        if (isAnyGlobalMenuOpen()) return;
        if (closeTimer) clearTimeout(closeTimer);
      });
      sidebarTrigger.addEventListener('mouseleave', () => {
        if (window.innerWidth > 768 && !isPinned) {
          scheduleCloseSidebar(280);
        }
      });
      sidebarTrigger.addEventListener('click', (e) => {
        e.stopPropagation();
        if (isAnyGlobalMenuOpen()) return;
        if (docsSidebar.classList.contains('is-open')) {
          closeSidebar();
        } else {
          openSidebar(true);
        }
      });
    }

    // 觸發方式 B：滑鼠抵達螢幕最左側邊緣 (Edge Hotspot 或 window mousemove)
    if (edgeHotspot) {
      edgeHotspot.addEventListener('mouseenter', () => {
        if (isAnyGlobalMenuOpen()) return;
        if (window.innerWidth > 768) openSidebar(false);
      });
    }

    // 全域左邊緣感知：當滑鼠游標 X 座標 <= 18px 時自動滑出側邊欄
    document.addEventListener('mousemove', (e) => {
      if (isAnyGlobalMenuOpen()) return;
      if (window.innerWidth > 768 && !docsSidebar.classList.contains('is-open')) {
        if (e.clientX <= 18) {
          openSidebar(false);
        }
      }
    });

    // 鼠標移入側邊欄本體時保持展開
    docsSidebar.addEventListener('mouseenter', () => {
      if (closeTimer) clearTimeout(closeTimer);
    });

    // 鼠標離開側邊欄時延遲收合
    docsSidebar.addEventListener('mouseleave', () => {
      if (window.innerWidth > 768 && !isPinned) {
        scheduleCloseSidebar(280);
      }
    });

    // 關閉按鈕與遮罩點擊
    if (sidebarClose) {
      sidebarClose.addEventListener('click', () => closeSidebar());
    }
    if (sidebarBackdrop) {
      sidebarBackdrop.addEventListener('click', () => closeSidebar());
    }

    // ESC 鍵關閉側邊欄
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        closeSidebar();
      }
    });

    // 動態讀取文檔卡片生成側邊欄清單
    const cards = Array.from(document.querySelectorAll('.card[id]'));
    if (sidebarList && cards.length) {
      sidebarList.innerHTML = '';
      const linkMap = {};

      cards.forEach((card) => {
        const titleEl = card.querySelector('h2');
        const tagEl = card.querySelector('.tag');
        const name = titleEl ? titleEl.textContent.trim() : card.id;
        const tag = tagEl ? tagEl.textContent.trim() : '';

        const li = document.createElement('li');
        li.className = 'sidebar-nav-item';

        const a = document.createElement('a');
        a.className = 'sidebar-nav-link';
        a.href = '#' + card.id;
        a.dataset.target = card.id;

        a.innerHTML = `
          <span class="sidebar-item-title">${name}</span>
          ${tag ? `<span class="sidebar-item-tag">${tag}</span>` : ''}
        `;

        a.addEventListener('click', (e) => {
          e.preventDefault();
          closeSidebar();
          const target = document.getElementById(card.id);
          if (target) {
            target.scrollIntoView({ behavior: 'smooth', block: 'start' });
            history.replaceState(null, '', '#' + card.id);
          }
        });

        li.appendChild(a);
        sidebarList.appendChild(li);
        linkMap[card.id] = a;
      });

      // 監聽滾動以高亮當前分類項目
      function updateActiveDocSection() {
        const marker = 140;
        let activeId = cards.length ? cards[0].id : null;
        cards.forEach((card) => {
          if (card.getBoundingClientRect().top <= marker) {
            activeId = card.id;
          }
        });

        Object.keys(linkMap).forEach((id) => {
          linkMap[id].classList.toggle('is-active', id === activeId);
        });
      }

      window.addEventListener('scroll', updateActiveDocSection, { passive: true });
      updateActiveDocSection();
    }
  }

  // 全域：點擊任何返回首頁的按鈕/連結時，設定置頂標籤
  document.addEventListener('click', (e) => {
    const homeLink = e.target.closest('a[href="/"], .topbar-brand, #back-link, .back-link, .unauth-back-link');
    if (homeLink) {
      sessionStorage.setItem('scroll_to_top', 'true');
    }
  });
})();

