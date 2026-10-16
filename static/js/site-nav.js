/**
 * 炙彗JI器人 - 全站頂部導航列與指令文檔側邊欄互動腳本
 * 包含：
 * 1. 頂部 Bar 最左側選單符號點擊動畫 (☰ <-> ✕) 與全站導航展開/關閉
 * 2. 指令文檔頁面 (docs.html) 專屬左側邊欄：點擊標籤 UI 或鼠標抵達最左側邊緣時自動展開
 */

(function () {
  'use strict';

  // ==========================================================================
  // 1. 全站頂端 Bar 選單動畫與開關邏輯
  // ==========================================================================
  const menuBtn = document.getElementById('site-menu-btn');
  const navDrawer = document.getElementById('site-nav-drawer');
  const navBackdrop = document.getElementById('site-nav-backdrop');

  function toggleSiteNav(force) {
    if (!menuBtn || !navDrawer) return;
    const isOpen = typeof force === 'boolean' ? force : !menuBtn.classList.contains('is-active');

    menuBtn.classList.toggle('is-active', isOpen);
    menuBtn.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
    navDrawer.classList.toggle('is-open', isOpen);
    if (navBackdrop) {
      navBackdrop.classList.toggle('is-open', isOpen);
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

  // 按 ESC 鍵關閉全站選單
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      toggleSiteNav(false);
    }
  });

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
      if (closeTimer) clearTimeout(closeTimer);
      if (pinned) isPinned = true;
      docsSidebar.classList.add('is-open');
      if (sidebarBackdrop && (isPinned || window.innerWidth <= 768)) {
        sidebarBackdrop.classList.add('is-open');
      }
      if (sidebarTrigger) sidebarTrigger.classList.add('is-active');
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
      if (sidebarTrigger) sidebarTrigger.classList.remove('is-active');
    }

    // 觸發方式 A：點擊展開 UI 標籤
    if (sidebarTrigger) {
      sidebarTrigger.addEventListener('click', (e) => {
        e.stopPropagation();
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
        if (window.innerWidth > 768) openSidebar(false);
      });
    }

    // 全域左邊緣感知：當滑鼠游標 X 座標 <= 18px 時自動滑出側邊欄
    document.addEventListener('mousemove', (e) => {
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
})();
