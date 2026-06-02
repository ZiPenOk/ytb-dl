// ==UserScript==
// @name         YTB-DL Remote Push
// @namespace    https://github.com/thsrite/ytb-dl
// @version      0.1.0
// @description  Push the current YouTube video to your self-hosted ytb-dl instance.
// @match        https://www.youtube.com/*
// @match        https://youtube.com/*
// @match        https://m.youtube.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @connect      *
// @connect      *
// ==/UserScript==

(function () {
  'use strict';

  const DEFAULT_API_BASE = 'http://127.0.0.1:9832';
  const STORAGE_API_BASE = 'ytb_dl_api_base';
  const STORAGE_API_TOKEN = 'ytb_dl_api_token';

  const state = {
    apiBase: GM_getValue(STORAGE_API_BASE, DEFAULT_API_BASE),
    apiToken: GM_getValue(STORAGE_API_TOKEN, ''),
    currentUrl: null,
    videoInfo: null,
    panelOpen: false,
  };

  let actionSlot = null;
  let lastUrl = '';

  GM_registerMenuCommand('YTB-DL 设置', openSettings);

  const host = document.createElement('div');
  host.id = 'ytb-dl-remote-push-host';
  document.documentElement.appendChild(host);
  const root = host.attachShadow({ mode: 'open' });

  buildPanel(root);

  const els = {
    overlay: root.getElementById('overlay'),
    panel: root.getElementById('panel'),
    title: root.getElementById('video-title'),
    parseBtn: root.getElementById('parse-btn'),
    pushBtn: root.getElementById('push-btn'),
    settingsBtn: root.getElementById('settings-btn'),
    closeBtn: root.getElementById('close-btn'),
    settingsForm: root.getElementById('settings-form'),
    apiBaseInput: root.getElementById('api-base-input'),
    apiTokenInput: root.getElementById('api-token-input'),
    saveSettingsBtn: root.getElementById('save-settings-btn'),
    cancelSettingsBtn: root.getElementById('cancel-settings-btn'),
    select: root.getElementById('format-select'),
    status: root.getElementById('status'),
  };

  els.parseBtn.addEventListener('click', parseCurrentVideo);
  els.pushBtn.addEventListener('click', pushDownload);
  els.settingsBtn.addEventListener('click', showSettings);
  els.closeBtn.addEventListener('click', closePanel);
  els.saveSettingsBtn.addEventListener('click', saveSettings);
  els.cancelSettingsBtn.addEventListener('click', hideSettings);
  ['keydown', 'keypress', 'keyup'].forEach((eventName) => {
    root.addEventListener(eventName, blockPageShortcuts);
  });
  els.overlay.addEventListener('click', (event) => {
    if (event.target === els.overlay) closePanel();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && state.panelOpen) closePanel();
  });

  watchNavigation();
  handleRouteChange();

  function buildPanel(targetRoot) {
    const style = document.createElement('style');
    style.textContent = `
      :host { all: initial; }
      .overlay {
        position: fixed;
        inset: 0;
        z-index: 2147483647;
        display: none;
        align-items: center;
        justify-content: center;
        box-sizing: border-box;
        padding: 20px;
        background: rgba(2, 6, 23, .42);
        backdrop-filter: blur(2px);
      }
      .overlay.open { display: flex; }
      .panel {
        width: min(720px, calc(100vw - 32px));
        max-height: calc(100vh - 48px);
        box-sizing: border-box;
        padding: 16px;
        border: 1px solid rgba(148, 163, 184, .35);
        border-radius: 8px;
        background: rgba(15, 23, 42, .96);
        color: #e5e7eb;
        font: 13px/1.4 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        box-shadow: 0 16px 40px rgba(0, 0, 0, .34);
        overflow-x: hidden;
        overflow-y: auto;
      }
      .header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        margin-bottom: 10px;
      }
      .header-actions {
        display: flex;
        align-items: center;
        gap: 6px;
        flex: 0 0 auto;
      }
      .title-wrap {
        min-width: 0;
        flex: 1 1 auto;
      }
      .title { font-weight: 700; font-size: 14px; }
      .row { display: grid; gap: 10px; margin-top: 8px; }
      select, button, input {
        box-sizing: border-box;
        width: 100%;
        min-width: 0;
        min-height: 34px;
        border-radius: 6px;
        border: 1px solid #334155;
        background: #111827;
        color: #e5e7eb;
        padding: 0 9px;
        font: inherit;
      }
      select { padding: 7px 8px; }
      button {
        cursor: pointer;
        font-weight: 650;
        background: #2563eb;
        border-color: #2563eb;
      }
      button.secondary { background: #1f2937; border-color: #475569; }
      button.ghost {
        width: auto;
        min-height: 28px;
        padding: 0 8px;
        background: transparent;
        border-color: #475569;
      }
      button:disabled { cursor: not-allowed; opacity: .55; }
      .actions { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
      .settings-form {
        display: grid;
        gap: 10px;
        margin-top: 12px;
        padding-top: 12px;
        border-top: 1px solid rgba(148, 163, 184, .22);
      }
      .settings-form[hidden] { display: none; }
      .field {
        display: grid;
        gap: 6px;
        color: #cbd5e1;
      }
      .field span {
        color: #94a3b8;
        font-size: 12px;
      }
      .settings-actions {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 10px;
      }
      .status {
        min-height: 18px;
        color: #cbd5e1;
        word-break: break-word;
      }
      .status.error { color: #fca5a5; }
      .status.ok { color: #86efac; }
      .meta {
        color: #94a3b8;
        overflow: hidden;
        display: -webkit-box;
        -webkit-box-orient: vertical;
        -webkit-line-clamp: 2;
        line-height: 1.35;
        max-width: 100%;
        overflow-wrap: anywhere;
      }
      @media (max-width: 520px) {
        .overlay { align-items: flex-end; padding: 10px; }
        .panel {
          width: 100%;
          max-height: calc(100vh - 20px);
        }
      }
    `;

    const overlay = createElement('div', { id: 'overlay', className: 'overlay' });
    overlay.setAttribute('aria-hidden', 'true');
    const panel = createElement('div', { id: 'panel', className: 'panel' });
    const header = createElement('div', { className: 'header' });
    const titleWrap = createElement('div', { className: 'title-wrap' });
    titleWrap.append(
      createElement('div', { className: 'title', text: 'YTB-DL 远程推送' }),
      createElement('div', { id: 'video-title', className: 'meta', text: '当前视频' }),
    );
    const headerActions = createElement('div', { className: 'header-actions' });
    headerActions.append(
      createElement('button', { id: 'settings-btn', className: 'ghost', text: '设置' }),
      createElement('button', { id: 'close-btn', className: 'ghost', text: '关闭' }),
    );
    header.append(
      titleWrap,
      headerActions,
    );

    const row = createElement('div', { className: 'row' });
    const actions = createElement('div', { className: 'actions' });
    actions.append(
      createElement('button', { id: 'parse-btn', text: '解析清晰度' }),
      createElement('button', { id: 'push-btn', className: 'secondary', text: '推送下载', disabled: true }),
    );

    const select = createElement('select', { id: 'format-select', disabled: true });
    select.appendChild(new Option('先解析当前视频', ''));

    row.append(
      actions,
      select,
      createElement('div', { id: 'status', className: 'status', text: `API: ${state.apiBase}` }),
    );

    const settingsForm = createElement('div', { id: 'settings-form', className: 'settings-form', hidden: true });
    const apiBaseField = createElement('label', { className: 'field' });
    apiBaseField.append(
      createElement('span', { text: 'API 地址' }),
      createElement('input', { id: 'api-base-input', type: 'url', placeholder: DEFAULT_API_BASE }),
    );

    const apiTokenField = createElement('label', { className: 'field' });
    apiTokenField.append(
      createElement('span', { text: 'API Token' }),
      createElement('input', { id: 'api-token-input', type: 'password', placeholder: 'Bearer Token' }),
    );

    const settingsActions = createElement('div', { className: 'settings-actions' });
    settingsActions.append(
      createElement('button', { id: 'save-settings-btn', text: '保存设置' }),
      createElement('button', { id: 'cancel-settings-btn', className: 'secondary', text: '取消' }),
    );
    settingsForm.append(apiBaseField, apiTokenField, settingsActions);

    panel.append(header, row, settingsForm);
    overlay.appendChild(panel);
    targetRoot.append(style, overlay);
  }

  function createElement(tagName, options = {}) {
    const node = document.createElement(tagName);
    if (options.id) node.id = options.id;
    if (options.className) node.className = options.className;
    if (options.text !== undefined) node.textContent = options.text;
    if (options.disabled) node.disabled = true;
    if (options.hidden) node.hidden = true;
    if (options.type) node.type = options.type;
    if (options.placeholder) node.placeholder = options.placeholder;
    if (tagName === 'button') node.type = 'button';
    return node;
  }

  function ensureActionStyle() {
    if (document.getElementById('ytb-dl-remote-push-action-style')) return;

    const style = document.createElement('style');
    style.id = 'ytb-dl-remote-push-action-style';
    style.textContent = `
      .ytb-dl-action-slot {
        display: inline-flex;
        align-items: center;
        flex: 0 0 auto;
        margin-right: 8px;
      }
      .ytb-dl-action-button {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        gap: 7px;
        height: 36px;
        min-width: 0;
        padding: 0 13px;
        border: 0;
        border-radius: 18px;
        background: var(--yt-spec-badge-chip-background, rgba(0, 0, 0, .05));
        color: var(--yt-spec-text-primary, #0f0f0f);
        font: 500 14px/36px Roboto, Arial, sans-serif;
        white-space: nowrap;
        cursor: pointer;
      }
      .ytb-dl-action-button:hover {
        background: var(--yt-spec-mono-tonal-hover, rgba(0, 0, 0, .1));
      }
      .ytb-dl-action-dot {
        width: 7px;
        height: 7px;
        border-radius: 999px;
        background: #ef4444;
        box-shadow: 0 0 0 3px rgba(239, 68, 68, .15);
      }
    `;
    (document.head || document.documentElement).appendChild(style);
  }

  function createActionSlot() {
    ensureActionStyle();

    const slot = document.createElement('div');
    slot.id = 'ytb-dl-remote-push-action';
    slot.className = 'ytb-dl-action-slot style-scope ytd-watch-metadata';

    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'ytb-dl-action-button';
    button.title = 'YTB-DL 远程推送';
    button.setAttribute('aria-label', 'YTB-DL 远程推送');

    const dot = document.createElement('span');
    dot.className = 'ytb-dl-action-dot';
    dot.setAttribute('aria-hidden', 'true');

    const label = document.createElement('span');
    label.textContent = '远程下载';

    button.append(dot, label);
    button.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      openPanel();
    });

    slot.appendChild(button);
    return slot;
  }

  function getActionContainer() {
    return document.querySelector('ytd-watch-metadata #menu #top-level-buttons-computed')
      || document.querySelector('ytd-watch-metadata #menu');
  }

  function mountActionButton() {
    if (!state.currentUrl) {
      removeActionButton();
      return;
    }

    const container = getActionContainer();
    if (!container) return;

    if (!actionSlot) actionSlot = createActionSlot();

    if (actionSlot.parentElement !== container) {
      container.prepend(actionSlot);
      return;
    }

    if (container.firstElementChild !== actionSlot) {
      container.insertBefore(actionSlot, container.firstElementChild);
    }
  }

  function removeActionButton() {
    if (actionSlot?.parentElement) actionSlot.remove();
  }

  function openPanel(options = {}) {
    if (!state.currentUrl && !options.allowWithoutVideo) return;

    state.panelOpen = true;
    els.overlay.classList.add('open');
    els.overlay.setAttribute('aria-hidden', 'false');
  }

  function closePanel() {
    state.panelOpen = false;
    hideSettings();
    els.overlay.classList.remove('open');
    els.overlay.setAttribute('aria-hidden', 'true');
  }

  function blockPageShortcuts(event) {
    if (!state.panelOpen) return;

    if (event.key === 'Escape' && event.type === 'keydown') {
      event.preventDefault();
      closePanel();
    }

    event.stopPropagation();
  }

  function showSettings() {
    openPanel({ allowWithoutVideo: true });
    els.apiBaseInput.value = state.apiBase || DEFAULT_API_BASE;
    els.apiTokenInput.value = state.apiToken || '';
    els.settingsForm.hidden = false;
    setTimeout(() => els.apiBaseInput.focus(), 0);
  }

  function hideSettings() {
    els.settingsForm.hidden = true;
  }

  function saveSettings() {
    const apiBase = els.apiBaseInput.value.trim().replace(/\/$/, '');
    const token = els.apiTokenInput.value.trim();

    if (!apiBase || !token) {
      setStatus('请填写 API 地址和 Token', true);
      return;
    }

    state.apiBase = apiBase;
    state.apiToken = token;
    GM_setValue(STORAGE_API_BASE, state.apiBase);
    GM_setValue(STORAGE_API_TOKEN, state.apiToken);
    hideSettings();
    setStatus(`API: ${state.apiBase}`, false, true);
  }

  async function parseCurrentVideo() {
    if (!ensureConfigured()) return;

    if (!state.currentUrl) {
      setStatus('没有识别到 YouTube 视频链接', true);
      return;
    }

    setBusy(true, '解析中...');
    try {
      const info = await apiRequest('/api/video-info', {
        method: 'POST',
        body: { url: state.currentUrl },
      });

      state.videoInfo = info;
      els.title.textContent = info.title || info.id || '当前视频';
      fillFormats(info.formats || []);
      setStatus(`已解析：${info.formats?.length || 0} 个格式`, false, true);
    } catch (error) {
      setStatus(`解析失败：${error.message}`, true);
    } finally {
      setBusy(false);
    }
  }

  async function pushDownload() {
    if (!ensureConfigured()) return;

    const selected = els.select.value;
    if (!state.currentUrl || !selected) {
      setStatus('请先解析并选择清晰度', true);
      return;
    }

    const formatId = selected === '__best__' ? null : selected;
    els.pushBtn.disabled = true;
    setStatus('正在推送下载...');

    try {
      const result = await apiRequest('/api/remote/download', {
        method: 'POST',
        body: {
          url: state.currentUrl,
          format_id: formatId,
        },
      });
      setStatus(`已推送：${result.task_id || '任务已创建'}`, false, true);
    } catch (error) {
      setStatus(`推送失败：${error.message}`, true);
    } finally {
      els.pushBtn.disabled = false;
    }
  }

  function fillFormats(formats) {
    const videoFormats = formats
      .filter((item) => item.vcodec && item.vcodec !== 'none')
      .sort(compareVideoQuality);
    const audioFormats = formats
      .filter((item) => item.acodec && item.acodec !== 'none' && (!item.vcodec || item.vcodec === 'none'))
      .sort(compareAudioQuality);

    els.select.replaceChildren(new Option('最佳质量（自动合并音频）', '__best__'));

    if (videoFormats.length) {
      const group = document.createElement('optgroup');
      group.label = '视频';
      for (const format of videoFormats) {
        const hasAudio = format.acodec && format.acodec !== 'none';
        const value = hasAudio
          ? format.format_id
          : `${format.format_id}+bestaudio[ext=m4a]/${format.format_id}+bestaudio/${format.format_id}`;
        group.appendChild(new Option(labelVideo(format, hasAudio), value));
      }
      els.select.appendChild(group);
    }

    if (audioFormats.length) {
      const group = document.createElement('optgroup');
      group.label = '纯音频';
      for (const format of audioFormats) {
        group.appendChild(new Option(labelAudio(format), format.format_id));
      }
      els.select.appendChild(group);
    }

    els.select.disabled = false;
    els.pushBtn.disabled = false;
  }

  function labelVideo(format, hasAudio) {
    return [
      clean(format.resolution),
      clean(format.format_note),
      clean((format.ext || '').toUpperCase()),
      format.fps ? `${format.fps}fps` : '',
      hasAudio ? '含音频' : '无音频，自动合并',
      sizeOrBitrate(format),
    ].filter(Boolean).join(' - ');
  }

  function labelAudio(format) {
    return [
      '纯音频',
      clean(format.format_note),
      clean(format.acodec ? format.acodec.split('.')[0] : ''),
      clean((format.ext || '').toUpperCase()),
      sizeOrBitrate(format),
    ].filter(Boolean).join(' - ');
  }

  function sizeOrBitrate(format) {
    if (format.filesize) return formatBytes(format.filesize);
    if (format.tbr) return `~${Math.round(format.tbr)}kbps`;
    if (format.vbr) return `video ${Math.round(format.vbr)}kbps`;
    if (format.abr) return `${Math.round(format.abr)}kbps`;
    return '';
  }

  function compareVideoQuality(a, b) {
    const ah = parseHeight(a.resolution);
    const bh = parseHeight(b.resolution);
    if (ah !== bh) return bh - ah;
    if ((a.fps || 0) !== (b.fps || 0)) return (b.fps || 0) - (a.fps || 0);
    return (b.filesize || b.tbr || 0) - (a.filesize || a.tbr || 0);
  }

  function compareAudioQuality(a, b) {
    return (b.abr || b.tbr || b.filesize || 0) - (a.abr || a.tbr || a.filesize || 0);
  }

  function parseHeight(resolution) {
    const match = String(resolution || '').match(/x(\d+)/);
    return match ? Number(match[1]) : 0;
  }

  function getCanonicalVideoUrl() {
    const url = new URL(location.href);
    if (url.pathname.startsWith('/shorts/')) {
      const id = url.pathname.split('/').filter(Boolean)[1];
      return id ? `https://www.youtube.com/watch?v=${id}` : null;
    }
    const id = url.searchParams.get('v');
    return id ? `https://www.youtube.com/watch?v=${id}` : null;
  }

  function handleRouteChange() {
    if (location.href === lastUrl) return;
    lastUrl = location.href;

    state.currentUrl = getCanonicalVideoUrl();
    state.videoInfo = null;
    els.title.textContent = state.currentUrl ? '当前视频' : '未打开视频页';
    els.select.replaceChildren(new Option('先解析当前视频', ''));
    els.select.disabled = true;
    els.pushBtn.disabled = true;

    if (!state.currentUrl) {
      removeActionButton();
      closePanel();
      return;
    }

    mountActionButton();
    setStatus('页面已切换，请解析当前视频');
  }

  function watchNavigation() {
    const syncPage = () => {
      handleRouteChange();
      mountActionButton();
    };

    setInterval(syncPage, 800);
    window.addEventListener('popstate', syncPage);
    window.addEventListener('yt-navigate-finish', syncPage);

    if (window.navigation && typeof window.navigation.addEventListener === 'function') {
      window.navigation.addEventListener('navigate', () => setTimeout(syncPage, 0));
    }

    new MutationObserver(syncPage).observe(document.documentElement, {
      childList: true,
      subtree: true,
    });
  }

  function apiRequest(path, options = {}) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: options.method || 'GET',
        url: state.apiBase.replace(/\/$/, '') + path,
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${state.apiToken}`,
        },
        data: options.body ? JSON.stringify(options.body) : undefined,
        timeout: 120000,
        onload: (response) => {
          const text = response.responseText || '';
          let data = {};
          try {
            data = text ? JSON.parse(text) : {};
          } catch {
            data = { raw: text };
          }

          if (response.status >= 200 && response.status < 300) {
            resolve(data);
            return;
          }

          const detail = data.detail || data.error || data.message || `HTTP ${response.status}`;
          reject(new Error(detail));
        },
        ontimeout: () => reject(new Error('请求超时')),
        onerror: () => reject(new Error('请求失败，检查 API 地址和 Token')),
      });
    });
  }

  function ensureConfigured() {
    if (!state.apiBase || !state.apiToken) {
      openSettings();
      return false;
    }
    return true;
  }

  function openSettings() {
    showSettings();
  }

  function setBusy(busy, text) {
    els.parseBtn.disabled = busy;
    if (text) setStatus(text);
  }

  function setStatus(message, isError = false, isOk = false) {
    els.status.textContent = message;
    els.status.classList.toggle('error', Boolean(isError));
    els.status.classList.toggle('ok', Boolean(isOk));
  }

  function clean(value) {
    const text = String(value || '').trim();
    if (!text || text.includes('N/A')) return '';
    return text;
  }

  function formatBytes(bytes) {
    const units = ['B', 'KB', 'MB', 'GB'];
    let size = Number(bytes) || 0;
    let unit = 0;
    while (size >= 1024 && unit < units.length - 1) {
      size /= 1024;
      unit += 1;
    }
    return `${size.toFixed(unit ? 1 : 0)}${units[unit]}`;
  }

})();
