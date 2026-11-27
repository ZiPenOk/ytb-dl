// ==UserScript==
// @name         YTB-DL Remote Push
// @namespace    https://github.com/thsrite/ytb-dl
// @version      0.3.2
// @description  Push YouTube videos from watch pages and list menus to your self-hosted ytb-dl instance.
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
  const VIDEO_CARD = 'yt-lockup-view-model, ytd-rich-grid-media, ytd-rich-item-renderer, ytd-video-renderer, ytd-grid-video-renderer, ytd-compact-video-renderer, ytd-playlist-video-renderer';
  const VIDEO_MENU_TRIGGER = '.ytLockupMetadataViewModelMenuButton button, ytd-menu-renderer #button-shape button, ytd-menu-renderer yt-icon-button#button, ytd-menu-renderer button[aria-haspopup="true"]';
  const VIDEO_MENU = '#contentWrapper yt-sheet-view-model yt-list-view-model[role="menu"], ytd-menu-popup-renderer tp-yt-paper-listbox';

  const state = {
    apiBase: GM_getValue(STORAGE_API_BASE, DEFAULT_API_BASE),
    apiToken: GM_getValue(STORAGE_API_TOKEN, ''),
    currentUrl: null,
    panelOpen: false,
  };

  let actionSlot = null;
  let lastUrl = '';
  let panelVideo = null;

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
    rotationSection: root.getElementById('rotation-section'),
    rotationToggle: root.getElementById('rotation-enabled'),
    rotationControls: root.getElementById('rotation-controls'),
    rotationAngle: root.getElementById('rotation-angle'),
    rotationPreview: root.getElementById('rotation-preview'),
    previewUnavailable: root.getElementById('preview-unavailable'),
    originalCover: root.getElementById('rotation-preview-original'),
    rotatedCover: root.getElementById('rotation-preview-rotated'),
    status: root.getElementById('status'),
  };

  els.parseBtn.addEventListener('click', parseCurrentVideo);
  els.pushBtn.addEventListener('click', pushDownload);
  els.settingsBtn.addEventListener('click', showSettings);
  els.closeBtn.addEventListener('click', closePanel);
  els.saveSettingsBtn.addEventListener('click', saveSettings);
  els.cancelSettingsBtn.addEventListener('click', hideSettings);
  els.select.addEventListener('change', syncRotationAvailability);
  els.rotationToggle.addEventListener('change', () => {
    els.rotationControls.hidden = !els.rotationToggle.checked;
  });
  els.rotationAngle.addEventListener('change', updateRotatedCover);
  els.originalCover.addEventListener('error', () => setRotationThumbnail(null));
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
  watchContextMenus();

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
      .rotation-toggle {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        min-height: 28px;
        cursor: pointer;
      }
      .rotation-toggle input {
        width: 16px;
        height: 16px;
        min-height: 0;
        margin: 0;
        accent-color: #60a5fa;
        cursor: pointer;
      }
      .rotation-controls {
        display: grid;
        gap: 8px;
        margin-top: 6px;
      }
      .rotation-controls[hidden], .rotation-preview[hidden] { display: none; }
      .rotation-angle { width: min(100%, 180px); }
      .rotation-preview {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 10px;
        width: min(100%, 380px);
        margin-inline: auto;
      }
      .rotation-preview figure { margin: 0; }
      .rotation-frame {
        position: relative;
        aspect-ratio: 1;
        overflow: hidden;
        border: 1px solid #334155;
        border-radius: 6px;
        background: #020617;
      }
      .rotation-frame img {
        position: absolute;
        inset: 0;
        width: 100%;
        height: 100%;
        object-fit: contain;
      }
      .rotation-preview figcaption {
        margin-top: 4px;
        color: #cbd5e1;
        font-size: 12px;
        text-align: center;
      }
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
      buildRotationSection(),
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

  function buildRotationSection() {
    const section = createElement('section', { id: 'rotation-section', hidden: true });
    const toggle = createElement('label', { className: 'rotation-toggle' });
    toggle.append(createElement('input', { id: 'rotation-enabled', type: 'checkbox' }), '旋转视频');

    const controls = createElement('div', { id: 'rotation-controls', className: 'rotation-controls', hidden: true });
    const angle = createElement('select', { id: 'rotation-angle', className: 'rotation-angle' });
    angle.setAttribute('aria-label', '旋转角度');
    angle.append(
      new Option('顺时针 90°', '90'),
      new Option('180°', '180'),
      new Option('逆时针 90°', '270'),
    );

    const preview = createElement('div', { id: 'rotation-preview', className: 'rotation-preview', hidden: true });
    for (const [id, caption] of [['original', '原始封面'], ['rotated', '旋转后']]) {
      const figure = createElement('figure');
      const frame = createElement('div', { className: 'rotation-frame' });
      const image = createElement('img', { id: `rotation-preview-${id}` });
      image.alt = '';
      frame.append(image);
      figure.append(frame, createElement('figcaption', { text: caption }));
      preview.append(figure);
    }
    controls.append(angle, preview, createElement('div', {
      id: 'preview-unavailable', hidden: true, text: '封面暂不可用',
    }));
    section.append(toggle, controls);
    return section;
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
      .ytb-dl-menu-item {
        display: flex;
        align-items: center;
        gap: 12px;
        width: 100%;
        min-height: 36px;
        box-sizing: border-box;
        padding: 0 12px 0 16px;
        border: 0;
        background: transparent;
        color: var(--yt-spec-text-primary, inherit);
        font: 400 14px/20px Roboto, Arial, sans-serif;
        text-align: left;
        white-space: nowrap;
        cursor: pointer;
      }
      .ytb-dl-menu-item:hover, .ytb-dl-menu-item:focus-visible {
        background: var(--yt-spec-touch-response, rgba(128, 128, 128, .16));
      }
      .ytb-dl-menu-icon { width: 24px; height: 24px; flex: 0 0 24px; }
      .ytb-dl-expanded-menu { max-height: calc(100dvh - 16px) !important; }
      .ytb-dl-expanded-menu > yt-contextual-sheet-layout { max-height: inherit !important; }
      tp-yt-iron-dropdown.ytb-dl-menu-positioned {
        translate: 0 var(--ytb-dl-menu-offset, 0px);
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

  function openPanel(video = state.currentUrl ? { url: state.currentUrl, title: '当前视频' } : null) {
    if (video?.url !== panelVideo?.url) setPanelVideo(video);

    state.panelOpen = true;
    els.overlay.classList.add('open');
    els.overlay.setAttribute('aria-hidden', 'false');
  }

  function setPanelVideo(video) {
    panelVideo = video;
    els.title.textContent = video?.title || '未打开视频页';
    els.parseBtn.disabled = !video;
    els.select.replaceChildren(new Option('先解析当前视频', ''));
    els.select.disabled = true;
    els.pushBtn.disabled = true;
    els.rotationToggle.checked = false;
    els.rotationControls.hidden = true;
    els.rotationAngle.value = '90';
    updateRotatedCover();
    setRotationThumbnail(null);
    syncRotationAvailability();
    setStatus(video ? '请点击解析清晰度' : `API: ${state.apiBase}`);
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
    if (!state.panelOpen) openPanel();
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
    const video = panelVideo;
    if (!video) return;

    els.parseBtn.disabled = true;
    els.pushBtn.disabled = true;
    setStatus('解析中...');
    try {
      const info = await apiRequest('/api/video-info', {
        method: 'POST',
        body: { url: video.url },
      });

      if (video !== panelVideo) return;
      els.title.textContent = info.title || info.id || '当前视频';
      setRotationThumbnail(info.thumbnail);
      fillFormats(info.formats || []);
      setStatus(`已解析：${info.formats?.length || 0} 个格式`, false, true);
    } catch (error) {
      if (video === panelVideo) setStatus(`解析失败：${error.message}`, true);
    } finally {
      if (video === panelVideo) {
        els.parseBtn.disabled = false;
        els.pushBtn.disabled = !els.select.value;
      }
    }
  }

  async function pushDownload() {
    if (!ensureConfigured()) return;
    const video = panelVideo;
    const selected = els.select.value;
    if (!video || !selected) {
      setStatus('请先解析并选择清晰度', true);
      return;
    }

    const formatId = selected === '__best__' ? null : selected;
    const rotation = els.rotationToggle.checked && !els.rotationSection.hidden ? Number(els.rotationAngle.value) : 0;
    els.parseBtn.disabled = true;
    els.pushBtn.disabled = true;
    setStatus('正在推送下载...');

    try {
      const result = await apiRequest('/api/remote/download', {
        method: 'POST',
        body: {
          url: video.url,
          format_id: formatId,
          rotation,
        },
      });
      if (video === panelVideo) setStatus(`已推送：${result.task_id || '任务已创建'}`, false, true);
    } catch (error) {
      if (video === panelVideo) setStatus(`推送失败：${error.message}`, true);
    } finally {
      if (video === panelVideo) {
        els.parseBtn.disabled = false;
        els.pushBtn.disabled = false;
      }
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
      group.dataset.audioOnly = 'true';
      for (const format of audioFormats) {
        group.appendChild(new Option(labelAudio(format), format.format_id));
      }
      els.select.appendChild(group);
    }

    els.select.disabled = false;
    syncRotationAvailability();
  }

  function syncRotationAvailability() {
    const selected = els.select.selectedOptions[0];
    els.rotationSection.hidden = !selected?.value || selected.parentElement.dataset.audioOnly === 'true';
  }

  function setRotationThumbnail(thumbnail) {
    const available = typeof thumbnail === 'string' && thumbnail.startsWith('https://');
    els.rotationPreview.hidden = !available;
    els.previewUnavailable.hidden = available;
    for (const image of [els.originalCover, els.rotatedCover]) {
      if (available) {
        image.src = thumbnail;
      } else {
        image.removeAttribute('src');
      }
    }
  }

  function updateRotatedCover() {
    els.rotatedCover.style.transform = `rotate(${els.rotationAngle.value}deg)`;
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

  function getCanonicalVideoUrl(href) {
    const url = new URL(href, location.href);
    if (url.protocol !== 'https:' || !['www.youtube.com', 'youtube.com', 'm.youtube.com'].includes(url.hostname)) return null;

    const id = url.pathname === '/watch'
      ? url.searchParams.get('v')
      : url.pathname.match(/^\/shorts\/([\w-]+)\/?$/)?.[1];
    return id && /^[\w-]{11}$/.test(id) ? `https://www.youtube.com/watch?v=${id}` : null;
  }

  function handleRouteChange() {
    if (location.href === lastUrl) return;
    lastUrl = location.href;

    state.currentUrl = getCanonicalVideoUrl(location.href);
    setPanelVideo(state.currentUrl ? { url: state.currentUrl, title: '当前视频' } : null);

    if (!state.currentUrl) {
      removeActionButton();
      closePanel();
      return;
    }

    mountActionButton();
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

  function watchContextMenus() {
    let item = null;
    let restoreLayout;
    let observer = null;
    let frame;
    let timer;

    const stopWatching = () => {
      observer?.disconnect();
      observer = null;
      cancelAnimationFrame(frame);
      frame = undefined;
      clearTimeout(timer);
    };

    const reset = () => {
      stopWatching();
      restoreLayout?.();
      restoreLayout = undefined;
      item?.remove();
      item = null;
    };

    // YouTube can reuse a popup outside the card, so capture the video before it opens.
    window.addEventListener('click', (event) => {
      const path = event.composedPath();
      if (item && path.includes(item)) return;
      reset();

      const trigger = path.find((node) => node instanceof Element && node.matches(VIDEO_MENU_TRIGGER));
      const card = trigger?.closest(VIDEO_CARD);
      const video = card && getCardVideo(card);
      if (!video) return;

      const pageUrl = location.href;
      const scheduleMount = () => {
        if (frame !== undefined) return;
        frame = requestAnimationFrame(() => {
          frame = undefined;
          if (location.href !== pageUrl) {
            reset();
            return;
          }

          const menu = [...document.querySelectorAll(VIDEO_MENU)].find(isOpenVideoMenu);
          if (!menu || item?.parentElement === menu) return;
          restoreLayout?.();
          item?.remove();
          item = createMenuItem(() => {
            closeVideoMenu(menu);
            reset();
            openPanel(video);
          });
          menu.append(item);
          restoreLayout = expandVideoMenu(menu);
        });
      };

      observer = new MutationObserver(scheduleMount);
      observer.observe(document.documentElement, {
        childList: true,
        subtree: true,
        attributes: true,
        attributeFilter: ['aria-hidden', 'class', 'hidden', 'open', 'style'],
      });
      timer = setTimeout(stopWatching, 2000);
      scheduleMount();
    }, true);

    window.addEventListener('yt-navigate-start', reset);
    window.addEventListener('popstate', reset);
    window.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') reset();
    }, true);
  }

  function getCardVideo(card) {
    const link = card.querySelector('a#video-title[href], a#video-title-link[href], a.ytLockupMetadataViewModelTitle[href], .ytLockupMetadataViewModelTitle a[href]')
      || card.querySelector('a[href*="/watch?"], a[href*="/shorts/"]');
    if (!link || link.closest(VIDEO_CARD) !== card) return null;

    const url = getCanonicalVideoUrl(link.href);
    if (!url) return null;
    const title = (link.getAttribute('title') || link.textContent || link.getAttribute('aria-label') || '').trim();
    return { url, title: title || '列表中的视频' };
  }

  function isOpenVideoMenu(menu) {
    return menu.firstElementChild
      && !menu.closest('[hidden], [aria-hidden="true"]')
      && menu.getClientRects().length > 0
      && getComputedStyle(menu).visibility === 'visible';
  }

  function createMenuItem(onSelect) {
    ensureActionStyle();

    const button = createElement('button', { className: 'ytb-dl-menu-item' });
    button.setAttribute('role', 'menuitem');
    const svgNamespace = 'http://www.w3.org/2000/svg';
    const icon = document.createElementNS(svgNamespace, 'svg');
    icon.classList.add('ytb-dl-menu-icon');
    icon.setAttribute('viewBox', '0 0 24 24');
    icon.setAttribute('aria-hidden', 'true');
    icon.setAttribute('focusable', 'false');
    const path = document.createElementNS(svgNamespace, 'path');
    path.setAttribute('fill', 'none');
    path.setAttribute('stroke', 'currentColor');
    path.setAttribute('stroke-linecap', 'round');
    path.setAttribute('stroke-linejoin', 'round');
    path.setAttribute('stroke-width', '2');
    path.setAttribute('d', 'M7 19A5 5 0 0 1 7 9c2-7 11-6 11 2a4 4 0 1 1 0 8Zm2-6l3 3l3-3m-3 3v-6');
    icon.append(path);
    button.append(icon, createElement('span', { text: 'YTB-DL 下载' }));
    button.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      onSelect();
    });
    return button;
  }

  function expandVideoMenu(menu) {
    const sheet = menu.closest('yt-sheet-view-model, ytd-menu-popup-renderer');
    const popup = sheet?.closest('tp-yt-iron-dropdown');
    if (!popup) return;

    // The native height can be calculated before the download row is added.
    sheet.classList.add('ytb-dl-expanded-menu');
    popup.classList.add('ytb-dl-menu-positioned');
    const margin = 8;
    let appliedOffset = 0;

    const fitVertically = () => {
      const { top, height } = popup.getBoundingClientRect();
      if (!height) return;
      const baseTop = top - appliedOffset;
      const maxTop = document.documentElement.clientHeight - margin - height;
      const fittedTop = Math.max(margin, Math.min(baseTop, maxTop));
      const offset = Math.round(fittedTop - baseTop);
      if (offset === appliedOffset) return;
      appliedOffset = offset;
      popup.style.setProperty('--ytb-dl-menu-offset', `${offset}px`);
    };

    const resizeObserver = new ResizeObserver(fitVertically);
    resizeObserver.observe(popup);
    const positionObserver = new MutationObserver(fitVertically);
    positionObserver.observe(popup, { attributes: true, attributeFilter: ['style'] });
    window.addEventListener('resize', fitVertically);
    fitVertically();

    return () => {
      resizeObserver.disconnect();
      positionObserver.disconnect();
      window.removeEventListener('resize', fitVertically);
      popup.style.removeProperty('--ytb-dl-menu-offset');
      popup.classList.remove('ytb-dl-menu-positioned');
      sheet.classList.remove('ytb-dl-expanded-menu');
    };
  }

  function closeVideoMenu(menu) {
    const popup = menu.closest('tp-yt-iron-dropdown, dialog');
    if (typeof popup?.close === 'function') {
      popup.close();
      return;
    }

    // The userscript's isolated world may not expose the site's custom element methods.
    menu.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'Escape',
      code: 'Escape',
      keyCode: 27,
      bubbles: true,
      composed: true,
      cancelable: true,
    }));
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
