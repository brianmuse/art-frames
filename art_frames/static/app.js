// A sign-in that ended (signed out elsewhere, or 30 days without a visit) makes each page poll get 401
// with X-Sign-In. Reload once, so the page shows the sign-in form instead of stale data.
(() => {
  const plain = window.fetch.bind(window);
  let reloading = false;
  window.fetch = async (...args) => {
    const response = await plain(...args);
    if (response.status === 401 && response.headers.get('X-Sign-In') === 'required' && !reloading) {
      reloading = true;
      location.reload();
    }
    return response;
  };
})();

// Activity drawer: what runs now, your commands, and (behind a switch) the schedule. The server
// orders, filters and groups the lines (art_frames/activity.py); this draws them with textContent only.
(() => {
  const drawer = document.querySelector('[data-activity-drawer]');
  const toggle = document.querySelector('[data-activity-toggle]');
  const overlay = document.querySelector('[data-activity-overlay]');
  const main = document.getElementById('main');
  const waiters = new Map();
  const expanded = new Set();
  let latest = null, timer = null, opener = null, reloading = false, failed = false, loaded = false;
  const finished = (entry) => ['done', 'skipped', 'failed', 'cancelled'].includes(entry.state);
  const tone = (entry) => ['skipped', 'cancelled'].includes(entry.state) ? 'neutral' : entry.category;
  const remember = (key, value) => {
    try { if (value) sessionStorage.setItem(key, '1'); else sessionStorage.removeItem(key); } catch { /* private mode */ }
  };
  const recalled = (key) => {
    try { const value = sessionStorage.getItem(key); sessionStorage.removeItem(key); return value === '1'; } catch { return false; }
  };
  const text = (tag, value, className) => {
    const node = document.createElement(tag);
    node.textContent = value;
    if (className) node.className = className;
    return node;
  };
  const entries = () => latest ? [...latest.running, ...latest.queued, ...latest.recent] : [];
  // Strip ?job= once its result shows, so a later reload does not repeat it.
  const forget = () => {
    const url = new URL(location.href);
    if (!url.searchParams.has('job')) return;
    url.searchParams.delete('job');
    history.replaceState(null, '', url);
  };
  if (main && main.hasAttribute('data-job-done')) {
    forget();
    const result = document.querySelector('[data-display-result][tabindex]');
    if (recalled('art-frames-focus-result') && result) result.focus();  // announce the panel result after the reload
  }
  if (!drawer || !toggle) {
    window.artFramesJob = async () => null;
    return;
  }
  toggle.setAttribute('role', 'button');
  toggle.setAttribute('aria-expanded', 'false');
  const status = drawer.querySelector('[data-activity-status]');
  const tvFilter = drawer.querySelector('[data-activity-tv]');
  const scheduleSwitch = drawer.querySelector('[data-activity-schedule]');
  const outcomes = [...drawer.querySelectorAll('[data-activity-outcome]')];
  // Filters are a per-tab convenience; the page works the same without them.
  const filters = (() => {
    const blank = {filter_tv: '', outcome: 'all', schedule: false};
    try {
      const saved = JSON.parse(sessionStorage.getItem('art-frames-activity-filters') || '{}');
      if ('tv' in saved && !('filter_tv' in saved)) saved.filter_tv = saved.tv;  // the older name
      return {...blank, filter_tv: saved.filter_tv || '', outcome: saved.outcome || 'all', schedule: Boolean(saved.schedule)};
    } catch { return blank; }
  })();
  const showFilters = () => {
    scheduleSwitch.checked = Boolean(filters.schedule);
    outcomes.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.activityOutcome === filters.outcome)));
    tvFilter.value = filters.filter_tv;
  };
  const setFilter = (key, value) => {
    filters[key] = value;
    try { sessionStorage.setItem('art-frames-activity-filters', JSON.stringify(filters)); } catch { /* private mode */ }
    showFilters();
    poll();
  };
  showFilters();
  tvFilter.addEventListener('change', () => setFilter('filter_tv', tvFilter.value));
  scheduleSwitch.addEventListener('change', () => setFilter('schedule', scheduleSwitch.checked));
  outcomes.forEach(button => button.addEventListener('click', () => setFilter('outcome', button.dataset.activityOutcome)));
  const cancel = async (entry, button) => {
    button.disabled = true;
    try {
      const url = new URL(drawer.dataset.cancelUrl.replace('JOB', encodeURIComponent(entry.id)), location.href);
      url.searchParams.set('tv', entry.tv);
      const response = await fetch(url, {
        method: 'POST', headers: {Accept: 'application/json'}, body: new URLSearchParams({csrf: drawer.dataset.csrf}),
      });
      const data = await response.json();
      status.textContent = response.ok ? `Cancelled: ${entry.label}.` : data.message;
    } catch {
      status.textContent = 'The command could not be cancelled. Try again.';
      button.disabled = false;
    }
    poll();
  };
  const when = (entry) => {
    const line = text('p', entry.status, 'small');
    if (!finished(entry)) line.append(` since ${entry.time}`);
    else {
      const ago = text('span', ` ${entry.ago}`);
      ago.title = entry.time;
      line.append(ago);
      if (entry.took) line.append(` · took ${entry.took}`);
    }
    if (entry.source === 'schedule') line.append(' · From the schedule');
    return line;
  };
  // One line: status dot · time · TV · action · result. The line is a button that shows the details.
  const fill = (row, line) => {
    const entry = line.entries[0];
    const detailId = `activity-detail-${line.id}`;
    const open = expanded.has(line.id);
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'activity-line';
    button.setAttribute('aria-expanded', String(open));
    button.setAttribute('aria-controls', detailId);
    const dot = text('span', '', `status-dot tone-${line.tone}`);
    dot.setAttribute('aria-hidden', 'true');
    const action = text('span', '', 'activity-action');
    action.append(text('span', line.label, 'activity-label'));
    if (line.repeat) action.append(text('span', ` · ${line.repeat}`, 'activity-repeat'));
    if (line.badge) action.append(text('span', line.badge, 'badge secondary activity-badge'));
    button.append(dot, text('span', line.time, 'activity-time'), text('span', line.tv_name, 'activity-tv'),
      action, text('span', line.status, `status-word tone-${line.tone}`));
    const detail = Object.assign(document.createElement('div'), {className: 'activity-detail', id: detailId, hidden: !open});
    detail.append(when(entry));
    if (entry.message) detail.append(text('p', entry.message, `alert notice ${tone(entry)}`));
    if (line.count > 1) {
      const times = text('ul', '', 'activity-repeats small');
      times.setAttribute('aria-label', 'Times');
      line.entries.forEach(item => times.append(text('li', item.time)));
      detail.append(times);
    }
    if (entry.state === 'queued') {
      const stop = text('button', 'Cancel', 'button outline sm');
      stop.type = 'button';
      stop.setAttribute('aria-label', `Cancel ${entry.label} on ${entry.tv_name}`);
      stop.addEventListener('click', () => cancel(entry, stop));
      detail.append(stop);
    }
    button.addEventListener('click', () => {
      const show = button.getAttribute('aria-expanded') !== 'true';
      if (show) expanded.add(line.id); else expanded.delete(line.id);
      button.setAttribute('aria-expanded', String(show));
      detail.hidden = !show;
    });
    const parts = [button];
    if (entry.step) parts.push(text('p', entry.step, 'small activity-step'));
    row.replaceChildren(...parts, detail);
  };
  const visible = (line) => JSON.stringify([line.status, line.time, line.action, line.count,
    ...line.entries.map(entry => [entry.step, entry.message, entry.time, entry.ago, entry.took])]);
  // Rows are kept by line ID and redrawn only when what they show changes, so focus and clicks survive a poll.
  const draw = (key) => {
    const lines = latest.view[key];
    const list = drawer.querySelector(`[data-activity-list="${key}"]`);
    const old = new Map([...list.children].map(row => [row.dataset.id, row]));
    const focused = document.activeElement;
    const rows = lines.map((line) => {
      const row = old.get(line.id) || Object.assign(document.createElement('li'), {className: 'activity-row'});
      row.dataset.id = line.id;
      if (row.dataset.shown !== visible(line)) {
        const hadFocus = row.contains(focused);
        row.dataset.shown = visible(line);
        fill(row, line);
        if (hadFocus) row.querySelector('.activity-line').focus();
      }
      return row;
    });
    if (rows.length !== list.children.length || rows.some((row, index) => list.children[index] !== row)) {
      const active = document.activeElement;
      list.replaceChildren(...rows);
      if (active && active.isConnected && document.activeElement !== active) active.focus();
    }
    const empty = drawer.querySelector(`[data-activity-empty="${key}"]`);
    if (key === 'yours' && latest.view.empty) empty.textContent = latest.view.empty;
    empty.hidden = lines.length > 0;
  };
  const drawFilters = (view) => {
    const options = [['', 'All TVs'], ...view.tvs.map(tv => [tv.id, tv.name])];
    const shown = [...tvFilter.options].map(option => [option.value, option.textContent]);
    if (JSON.stringify(options) !== JSON.stringify(shown)) {
      tvFilter.replaceChildren(...options.map(([value, label]) => Object.assign(document.createElement('option'), {value, textContent: label})));
    }
    filters.filter_tv = view.filters.filter_tv;  // a removed TV falls back to all TVs
    showFilters();
    drawer.querySelector('[data-activity-hidden]').textContent = view.hidden ? ` (${view.hidden} hidden)` : '';
    drawer.querySelector('[data-activity-section="schedule"]').hidden = !view.filters.schedule;
  };
  const mine = () => latest ? [...latest.running, ...latest.queued].filter(entry => entry.source === 'you').length : 0;
  const render = () => {
    const count = mine();
    const badge = toggle.querySelector('[data-activity-count]');
    badge.textContent = String(count);
    badge.hidden = !count;
    toggle.querySelector('[data-activity-count-text]').textContent = count ? `: ${count} running or queued` : '';
    if (!drawer.hidden && latest.view) {
      drawFilters(latest.view);
      ['now', 'yours', 'schedule'].forEach(draw);
    }
  };
  // A job started from this page: show its result where the page shows results.
  const watched = main && main.dataset.job;
  const settle = () => {
    for (const [id, resolve] of waiters) {
      const entry = entries().find(job => job.id === id);
      if (entry && finished(entry)) { waiters.delete(id); resolve(entry); }
    }
    if (!watched || reloading || !main.dataset.job) return;
    const entry = entries().find(job => job.id === watched);
    if (!entry || !finished(entry)) return;
    const reload = ['display', 'options', 'power', 'standby', 'setup'].includes(main.dataset.jobPlace);
    // The open drawer already shows the result. Reloading now would close it under the reader,
    // so the page reloads once the drawer closes (close() calls settle again).
    if (reload && !drawer.hidden) return;
    delete main.dataset.job;
    if (reload) {
      reloading = true;
      remember('art-frames-focus-result', true);
      location.reload();  // the Display page then draws the result inside its panel
      return;
    }
    document.querySelectorAll('.notice.queued').forEach(note => note.remove());
    main.querySelector('[data-job-notes]').replaceChildren(text('div', entry.message, `alert notice ${tone(entry)}`));
    forget();
  };
  const schedule = () => {
    clearTimeout(timer);
    timer = null;
    if (document.hidden) return;
    const fast = mine() || !drawer.hidden || waiters.size || (watched && main.dataset.job);
    timer = setTimeout(poll, fast ? 2000 : 20000);
  };
  async function poll() {
    clearTimeout(timer);
    try {
      const url = new URL(drawer.dataset.url, location.href);
      url.searchParams.set('view', '1');
      if (filters.filter_tv) url.searchParams.set('filter_tv', filters.filter_tv);
      if (filters.outcome !== 'all') url.searchParams.set('outcome', filters.outcome);
      if (filters.schedule) url.searchParams.set('schedule', '1');
      const response = await fetch(url, {headers: {Accept: 'application/json'}, cache: 'no-store'});
      if (!response.ok) throw new Error('Activity failed');
      latest = await response.json();
      loaded = true;
      if (failed) { failed = false; status.textContent = ''; }
      render();
      settle();
    } catch {
      failed = true;
      // Keep the last rows; an empty message next to this error would contradict it.
      drawer.querySelectorAll('[data-activity-empty]').forEach(empty => { empty.hidden = true; });
      if (!drawer.hidden) status.textContent = loaded ? 'Activity could not be updated. Art Frames tries again.' : 'Activity could not be loaded. Art Frames tries again.';
    }
    schedule();
  }
  const open = () => {
    opener = document.activeElement;
    drawer.hidden = false;
    if (overlay) overlay.hidden = false;
    document.body.classList.add('sheet-open');
    toggle.setAttribute('aria-expanded', 'true');
    status.textContent = '';
    if (latest) render();
    drawer.focus();
    poll();
  };
  const close = () => {
    drawer.hidden = true;
    if (overlay) overlay.hidden = true;
    document.body.classList.remove('sheet-open');
    toggle.setAttribute('aria-expanded', 'false');
    (opener && opener.isConnected && opener !== document.body ? opener : toggle).focus();
    settle();  // a result that finished while the drawer was open
    schedule();
  };
  toggle.addEventListener('click', (event) => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;  // new tab or window
    event.preventDefault();
    if (drawer.hidden) open(); else close();
  });
  toggle.addEventListener('keydown', (event) => {
    if (event.key === ' ') { event.preventDefault(); toggle.click(); }
  });
  drawer.querySelector('[data-activity-close]').addEventListener('click', close);
  if (overlay) overlay.addEventListener('click', close);
  document.addEventListener('keydown', (event) => {
    if (drawer.hidden) return;
    if (event.key === 'Escape') { close(); return; }
    if (event.key !== 'Tab') return;
    // The drawer is modal: keep Tab inside it.
    const stops = [...drawer.querySelectorAll('button, a[href], select, input')].filter(node => !node.disabled && node.offsetParent !== null);
    if (!stops.length) return;
    const first = stops[0], last = stops[stops.length - 1];
    if (!drawer.contains(document.activeElement)) {  // focus slipped behind the overlay: bring it back
      event.preventDefault();
      (event.shiftKey ? last : first).focus();
      return;
    }
    if (event.shiftKey && (document.activeElement === first || document.activeElement === drawer)) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });
  document.addEventListener('visibilitychange', () => { if (document.hidden) clearTimeout(timer); else poll(); });
  window.artFramesJob = (id, deadline) => new Promise((resolve) => {
    waiters.set(id, resolve);
    setTimeout(() => { if (waiters.delete(id)) resolve(null); }, Math.max(0, deadline - Date.now()));
    poll();
  });
  // A result reload puts focus on the result, so the drawer stays closed instead of covering it.
  recalled('art-frames-drawer-open');  // forget the flag older pages set
  poll();
})();

// A filter form applies as soon as a field changes; the Apply button is only for pages without JS.
document.querySelectorAll('[data-auto-submit]').forEach((form) => {
  form.querySelectorAll('[data-auto-submit-go]').forEach(button => { button.hidden = true; });
  form.addEventListener('change', () => form.requestSubmit());
});

// A slider's live value: an <output data-range-value> in the same label.
document.querySelectorAll('[data-range-value]').forEach((output) => {
  const range = output.closest('label') && output.closest('label').querySelector('input[type="range"]');
  if (!range) return;
  const update = () => { output.textContent = range.value; };
  range.addEventListener('input', update);
  update();
});

document.querySelectorAll("[data-fit-form]").forEach((form) => {
  const image = form.closest(".art-card").querySelector("[data-art-preview]");
  const stage = form.closest(".art-card").querySelector("[data-art-stage]");
  const viewport = image.parentElement;
  const status = form.querySelector('[data-framing-status]');
  const retry = form.querySelector('button[type="submit"]');
  let version = 0, savedVersion = 0, saving = false, timer;
  retry.hidden = true;
  status.textContent = '';  // the page header says changes save automatically; a card speaks only while saving or after an error
  const save = async () => {
    clearTimeout(timer);
    timer = null;
    if (saving || savedVersion === version) return;
    const submitted = version;
    saving = true;
    retry.hidden = true;
    status.textContent = 'Saving…';
    try {
      const response = await fetch(form.action, {
        method: 'POST', body: new FormData(form), headers: {'Accept': 'application/json'}
      });
      const result = await response.json();
      if (!response.ok || result.status !== 'saved') throw new Error('Save failed');
      savedVersion = submitted;
      status.textContent = version === savedVersion ? 'Saved.' : 'Saving…';
    } catch {
      status.textContent = 'Couldn’t save your changes. Please retry.';
      retry.textContent = 'Retry saving →';
      retry.hidden = false;
    } finally {
      saving = false;
      if (version !== submitted && !timer) timer = setTimeout(save, 0);
    }
  };
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    event.stopImmediatePropagation();
    save();
  });
  window.addEventListener('beforeunload', (event) => {
    if (version !== savedVersion) { event.preventDefault(); event.returnValue = ''; }
  });
  let editing = false;
  const preview = () => {
    const zoom = Number(form.elements.zoom.value);
    const matWidth = Number(form.elements.mat_width.value);
    const border = Math.round(2160 * matWidth);
    form.querySelector("[data-zoom-value]").textContent = `${Math.round(zoom * 100)}%`;
    form.querySelector('[data-mat-width]').textContent = matWidth ? `${Number((matWidth * 100).toFixed(1))}%` : 'Off';
    stage.style.backgroundColor = border ? form.elements.mat_color.value : '';  // '' uses var(--art-ground)
    viewport.style.top = viewport.style.bottom = `${border / 2160 * 100}%`;
    viewport.style.left = viewport.style.right = `${border / 3840 * 100}%`;
    if (!image.naturalWidth || !image.naturalHeight) return;
    const width = viewport.clientWidth, height = viewport.clientHeight;
    const crop = form.elements.fit.value === "crop";
    const scale = (crop ? Math.max : Math.min)(width / image.naturalWidth, height / image.naturalHeight);
    const outputScale = stage.clientWidth / 3840;
    const depth = Math.min(6, border) * outputScale;
    const fittedWidth = Math.min(width, image.naturalWidth * scale * zoom);
    const fittedHeight = Math.min(height, image.naturalHeight * scale * zoom);
    const rgb = form.elements.mat_color.value.match(/[a-f\d]{2}/gi).map(value => parseInt(value, 16));
    stage.classList.toggle('has-mat-edge', border > 0);
    stage.style.setProperty('--mat-edge-left', `${border * outputScale + (width - fittedWidth) / 2 - depth}px`);
    stage.style.setProperty('--mat-edge-top', `${border * outputScale + (height - fittedHeight) / 2 - depth}px`);
    stage.style.setProperty('--mat-edge-width', `${fittedWidth + depth * 2}px`);
    stage.style.setProperty('--mat-edge-height', `${fittedHeight + depth * 2}px`);
    stage.style.setProperty('--mat-edge-depth', `${depth}px`);
    stage.style.setProperty('--mat-edge-shade', `rgb(${rgb.map(channel => Math.round(channel * .88)).join(',')})`);
    stage.style.setProperty('--mat-edge-light', `rgb(${rgb.map(channel => Math.round(channel + (255 - channel) * .38)).join(',')})`);
    stage.style.setProperty('--mat-edge-shadow', `${Math.min(10, border) * outputScale}px`);
    const x = image.naturalWidth * scale * zoom > width + 0.01 ? Number(form.elements.x.value) : 0.5;
    const y = image.naturalHeight * scale * zoom > height + 0.01 ? Number(form.elements.y.value) : 0.5;
    image.style.objectFit = crop ? "cover" : "contain";
    image.style.objectPosition = `${x * 100}% ${y * 100}%`;
    image.style.transformOrigin = `${x * 100}% ${y * 100}%`;
    image.style.transform = `scale(${zoom})`;
  };
  image.addEventListener("load", () => { if (editing) preview(); });
  new ResizeObserver(() => { if (editing) preview(); }).observe(stage);
  form.addEventListener("input", () => {
    version += 1;
    status.textContent = 'Saving…';
    retry.hidden = true;
    clearTimeout(timer);
    timer = setTimeout(save, 600);
    editing = true;
    if (image.getAttribute("src") !== image.dataset.original) image.src = image.dataset.original;
    preview();
  });
});
document.querySelectorAll('form[method="post"]').forEach((form) => {
  form.addEventListener("submit", (event) => {
    // Keep submit values intact while preventing accidental duplicate network operations. A disabled
    // button is left out of the form data, so a named submit button (Show now's layout) leaves a copy.
    const submitter = event.submitter;
    if (submitter && submitter.name && !event.defaultPrevented && submitter.form === form) {
      form.append(Object.assign(document.createElement('input'), {type: 'hidden', name: submitter.name, value: submitter.value}));
    }
    form
      .querySelectorAll('button[type="submit"], button:not([type])')
      .forEach((button) => {
        button.disabled = true;
        button.setAttribute("aria-busy", "true");
      });
  });
});

document.querySelectorAll('[data-tv-artwork-stage]').forEach((stage) => {
  const image = stage.querySelector('img');
  if (!image) return;
  const failed = () => {
    image.remove();
    stage.querySelector('[data-tv-artwork-error]').hidden = false;
  };
  image.addEventListener('error', failed);
  if (image.complete && !image.naturalWidth) failed();
});

document.querySelectorAll('[data-preview-form]').forEach((form) => {
  let running = false;
  const panel = form.closest('[data-preview-panel]');
  const sendFields = Array.from(panel.querySelectorAll('[data-preview-revision]'));
  const disableSend = () => sendFields.forEach(field => {field.closest('form').querySelector('button').disabled = true;});
  // A layout that needs setup cannot go to the TV: its send stays off while it is chosen.
  const choice = form.querySelector('select[name="app"]');
  // The status lines name a layout when one is chosen, else the custom website.
  const picked = form.querySelector('[name="app"]');
  const source = () => (picked && picked.value && picked.value !== 'artwork' ? 'layout' : 'website');
  const needsSetup = () => Boolean(choice && choice.selectedOptions[0] && 'setup' in choice.selectedOptions[0].dataset);
  const enableSend = () => sendFields.forEach(field => {
    field.closest('form').querySelector('button').disabled = needsSetup() || !field.value;
  });
  disableSend();
  if (choice) choice.addEventListener('change', () => {
    const image = panel.querySelector('[data-preview-stage] img');
    if (needsSetup() || !image || !image.naturalWidth) disableSend();
    else if (!running) enableSend();
  });
  const initialImage = panel.querySelector('[data-preview-stage] img');
  const imageFailed = () => {
    disableSend();
    form.querySelector('[data-preview-status]').textContent = 'The saved preview changed or could not load. Make a new preview, then show it.';
  };
  if (initialImage) {
    initialImage.addEventListener('error', imageFailed);
    initialImage.addEventListener('load', () => {if (!running && initialImage.naturalWidth) enableSend();});
    if (initialImage.complete) {
      if (initialImage.naturalWidth) enableSend();
      else imageFailed();
    }
  }
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (running) return;
    running = true;
    disableSend();
    const button = form.querySelector('button');
    const status = form.querySelector('[data-preview-status]');
    status.textContent = 'Preparing your preview…';
    const deadline = Date.now() + 300000;
    try {
      while (Date.now() < deadline) {
        const response = await fetch(form.action, {
          method:'POST', headers:{Accept:'application/json'},
          body:new URLSearchParams(new FormData(form)), signal:AbortSignal.timeout(60000),
        });
        let data = await response.json();
        if (response.status === 202 && data.status === 'queued') {
          // The render waits in this TV's queue; the activity record carries its result.
          status.textContent = 'Queued. Your preview starts when the TV is free.';
          const job = await window.artFramesJob(data.job, deadline);
          if (!job) {
            status.textContent = 'Your preview is still queued or running. Open Activity to follow it.';
            return;
          }
          if (job.pending) data = {status: 'pending'};
          else if (job.revision) data = {status: 'ready', revision: job.revision, image: `${form.dataset.imageUrl}&v=${job.revision}`};
          else { status.textContent = job.message; return; }
        }
        if (data.status === 'pending') {
          status.textContent = `Waiting for your ${source()}’s data… Your preview will update automatically.`;
          await new Promise(resolve => setTimeout(resolve, 2000));
          continue;
        }
        if (data.status === 'failed' && data.message) { status.textContent = data.message; return; }
        if (data.status !== 'ready') throw new Error('Preview failed');
        const frame = panel.querySelector('[data-preview-stage]');
        const image = new Image();
        image.alt = 'Latest preview';
        image.src = data.image;
        await image.decode();
        frame.replaceChildren(image);
        panel.querySelectorAll('[data-preview-revision]').forEach((field) => {field.value = data.revision;});
        enableSend();
        status.textContent = 'Preview updated.';
        return;
      }
      status.textContent = `Your ${source()} is still preparing data. Check its settings and try again.`;
    } catch {
      status.textContent = 'The preview could not be completed. Check your connection and the layout’s settings, then try again.';
    } finally {
      running = false; button.disabled = false; button.removeAttribute('aria-busy');
    }
  });
});

document.querySelectorAll("[data-tv-discovery]").forEach((panel) => {
  const form = panel.closest("form");
  const find = panel.querySelector("[data-tv-find]");
  const status = panel.querySelector("[data-tv-search-status]");
  const results = panel.querySelector("[data-tv-results]");
  form.elements.discovery_network.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.isComposing) {
      event.preventDefault();
      if (!find.disabled) find.click();
    }
  });
  find.addEventListener("click", async () => {
    find.disabled = true;
    find.setAttribute("aria-busy", "true");
    results.replaceChildren();
    status.textContent = "Looking for TVs… This can take about 25 seconds.";
    try {
      const response = await fetch(panel.dataset.url, {
        method: "POST",
        body: new URLSearchParams({
          csrf: form.elements.csrf.value,
          network: form.elements.discovery_network.value,
        }),
        signal: AbortSignal.timeout(35000),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "The TV search could not be completed.");
      for (const tv of data.devices) {
        const row = document.createElement("li");
        const choose = document.createElement("button");
        choose.type = "button";
        choose.className = "button outline";
        choose.textContent = `${tv.name} · ${tv.model || "Samsung TV"} · ${tv.ip}${tv.frame === true ? " · Frame TV" : ""}`;
        choose.addEventListener("click", () => {
          form.elements.tv_ip.value = tv.ip;
          form.elements.tv_mac.value = tv.mac || "";
          // The name the TV reports fills an empty name field (name on Add TV, tv_name on TV settings).
          const name = form.elements.namedItem("name") || form.elements.namedItem("tv_name");
          // On the setup page the default name ("Frame 1") also gives way (data-default-name).
          if (name && tv.name && (!name.value.trim() || name.value === name.dataset.defaultName)) name.value = tv.name;
          // Script changes fire no event; this one lets the card show Unsaved and Pair TV ask first.
          form.elements.tv_ip.dispatchEvent(new Event("change", { bubbles: true }));
          const next = form.hasAttribute('data-add-tv') ? 'Check the name, then select Add TV.'
            : form.hasAttribute('data-setup') ? 'Check the name, then select Next.' : 'Save, then select Pair TV.';
          status.textContent = `Selected ${tv.name}. ${next}${tv.mac ? "" : " Enter its MAC address to turn it on at a set time."}`;
        });
        row.append(choose);
        results.append(row);
      }
      status.textContent = data.devices.length
        ? "Choose your TV below. Its reported name may be outdated."
        : "No Samsung TVs found. Keep the TV on and try again, or open Search a different network. You can also enter its address yourself.";
    } catch (error) {
      status.textContent = error.name === "TimeoutError"
        ? "The search timed out. Check that your TV is awake and the network is reachable."
        : error instanceof SyntaxError
          ? "The search could not be completed. Refresh this page and try again."
          : error.message;
    } finally {
      find.disabled = false;
      find.removeAttribute("aria-busy");
    }
  });
  // The setup page starts the search on its own (_tv_discovery.html data-autostart).
  if (panel.hasAttribute("data-autostart")) find.click();
});

// Layout-defined repeatable fields; private values are never returned to this editor.
document.querySelectorAll('[data-app-list]').forEach((list) => {
  const rows = list.querySelector('[data-list-rows]');
  const add = list.querySelector('[data-list-add]');
  const status = list.querySelector('[data-list-status]');
  // The status counts only rows added or removed in this page view; saved rows are not news.
  let added = 0, removed = 0;
  const update = () => {
    add.disabled = rows.children.length >= Number(list.dataset.max);
    const changes = [added && `${added} added`, removed && `${removed} removed`].filter(Boolean);
    status.textContent = changes.length ? `${changes.join(', ')}. Save to apply your changes.`
      : rows.children.length ? '' : `No ${list.dataset.itemLabel.toLowerCase()}s added.`;
  };
  list.addEventListener('click', (event) => {
    const remove = event.target.closest('[data-list-remove]');
    if (remove) {
      remove.closest('[data-list-item]').remove();
      removed += 1;
      update();
      add.focus();
    }
  });
  add.addEventListener('click', () => {
    if (rows.children.length >= Number(list.dataset.max)) return;
    const fragment = list.querySelector('[data-list-template]').content.cloneNode(true);
    const id = crypto.randomUUID();
    fragment.querySelectorAll('[name]').forEach((input) => {
      input.name = input.name.replace('__ROW__', id);
      if (input.type === 'hidden') input.value = id;
    });
    const row = fragment.querySelector('[data-list-item]');
    rows.append(fragment);
    added += 1;
    update();
    row.querySelector('input:not([type="hidden"])').focus();
  });
  update();
});

// TV switcher and Show now: <details> menus, so they work without JavaScript. The target TV travels in
// the URL, so open tabs never change each other's TV. This only closes a menu on Escape or an outside click.
document.querySelectorAll('[data-tv-switch], [data-menu]').forEach((menu) => {
  document.addEventListener('click', (event) => { if (menu.open && !menu.contains(event.target)) menu.open = false; });
  menu.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && menu.open) { menu.open = false; menu.querySelector('summary').focus(); }
  });
});

// Sidebar: on narrow screens it is a Sheet. Without JavaScript the menu button's #sidebar link opens it.
(() => {
  const sidebar = document.querySelector('[data-sidebar]');
  const toggle = document.querySelector('[data-sidebar-toggle]');
  const overlay = document.querySelector('[data-sidebar-overlay]');
  if (!sidebar || !toggle) return;
  const close = sidebar.querySelector('[data-sidebar-close]');
  const show = (open) => {
    document.body.classList.toggle('sidebar-open', open);
    if (overlay) overlay.hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
    if (open) sidebar.querySelector('a, summary').focus(); else toggle.focus();
  };
  toggle.setAttribute('aria-expanded', 'false');
  toggle.addEventListener('click', (event) => { event.preventDefault(); show(!document.body.classList.contains('sidebar-open')); });
  if (close) close.addEventListener('click', (event) => { event.preventDefault(); show(false); });
  if (overlay) overlay.addEventListener('click', () => show(false));
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && document.body.classList.contains('sidebar-open')) show(false);
  });
})();
// Timezone field (timezone_field() in _ui.html). Without JS it is an input with a datalist. Browsers filter a
// datalist in different ways, so this replaces it with a list that finds a zone by alias (EST, pst), city,
// IANA name or UTC offset. The input still submits what it shows; the server turns an alias into its IANA name.
document.querySelectorAll('[data-timezone-field]').forEach((field) => {
  const input = field.querySelector('input');
  const datalist = field.querySelector('datalist');
  if (!input || !datalist) return;
  const plain = (value) => value.toLowerCase().replace(/−/g, '-').replace(/_/g, ' ');
  const zones = [...datalist.options].map((option) => ({
    value: option.value,
    label: option.label,
    aliases: (option.dataset.keywords || '').toLowerCase().split(' ').filter(Boolean),
    text: plain(`${option.value} ${option.label} ${option.dataset.keywords || ''}`),
  }));
  const list = document.createElement('ul');
  list.className = 'dropdown-content timezone-list';
  list.id = `${datalist.id}-list`;
  list.setAttribute('role', 'listbox');
  list.hidden = true;
  field.append(list);
  input.removeAttribute('list');
  input.setAttribute('role', 'combobox');
  input.setAttribute('aria-autocomplete', 'list');
  input.setAttribute('aria-controls', list.id);
  input.setAttribute('aria-expanded', 'false');
  let shown = [], active = -1;

  // Every word must match. The first word ranks the matches; the sort is stable, so common zones stay first in a tier.
  const EXACT_ALIAS = 0, WORD_START = 1, ANYWHERE = 2;
  const search = (query) => {
    const words = plain(query.trim()).split(/\s+/).filter(Boolean);
    if (!words.length) return zones;
    const matchesAll = (zone) => words.every(word => zone.text.includes(word));
    const rank = (zone) => zone.aliases.includes(words[0]) ? EXACT_ALIAS
      : ` ${zone.text}`.includes(` ${words[0]}`) ? WORD_START : ANYWHERE;
    return zones.filter(matchesAll).sort((a, b) => rank(a) - rank(b));
  };
  // The check mark from icon('check') in _ui.html.
  const check = () => {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    Object.entries({ class: 'icon', width: 16, height: 16, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor',
      'stroke-width': 2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true' })
      .forEach(([name, value]) => svg.setAttribute(name, value));
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', 'M20 6 9 17l-5-5');
    svg.append(path);
    return svg;
  };
  const known = (value) => zones.some(zone => zone.value === value);
  const highlight = (index) => {
    active = index;
    [...list.children].forEach((item, place) => item.setAttribute('aria-selected', String(place === index)));
    const item = list.children[index];
    if (item && item.id) { input.setAttribute('aria-activedescendant', item.id); item.scrollIntoView({ block: 'nearest' }); }
    else input.removeAttribute('aria-activedescendant');
  };
  // Only typing picks a first row. Focus and click never do, so Enter still submits a stored zone such as GMT.
  const open = (query, typed = false) => {
    shown = search(query);
    list.replaceChildren(...shown.map((zone, index) => {
      const item = document.createElement('li');
      item.className = 'dropdown-item';
      item.id = `${list.id}-${index}`;
      item.setAttribute('role', 'option');
      item.dataset.index = index;
      const text = document.createElement('span');
      text.className = 'dropdown-item-text';
      const name = document.createElement('span');
      name.textContent = zone.label;
      const value = document.createElement('span');
      value.className = 'status';
      value.textContent = zone.value;
      text.append(name, value);
      item.append(text);
      if (zone.value === input.value) { item.setAttribute('aria-current', 'true'); item.append(check()); }
      return item;
    }));
    if (!shown.length) {
      const empty = document.createElement('li');
      empty.className = 'dropdown-label';
      empty.textContent = 'No matching timezone. Try a city, an abbreviation such as EST, or an IANA name.';
      list.append(empty);
    }
    list.hidden = false;
    input.setAttribute('aria-expanded', 'true');
    highlight(typed && query.trim() && shown.length ? 0 : -1);
    const current = list.querySelector('[aria-current]');
    if (current && active < 0) current.scrollIntoView({ block: 'nearest' });
    // On a short screen, scroll the page so the whole list shows above the bottom edge and the frame band.
    const bottom = Math.min(innerHeight, window.visualViewport ? visualViewport.height : innerHeight);
    const inset = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--panel-inset')) || 0;
    const overflow = list.getBoundingClientRect().bottom - bottom + inset + 8;
    if (overflow > 0) window.scrollBy(0, overflow);
  };
  // A saved zone opens the whole list at that zone; typed text opens it filtered.
  const reopen = () => open(known(input.value) ? '' : input.value);
  const close = () => {
    list.hidden = true;
    input.setAttribute('aria-expanded', 'false');
    input.removeAttribute('aria-activedescendant');
    active = -1;
  };
  const choose = (index) => {
    input.value = shown[index].value;
    close();
    input.dispatchEvent(new Event('change', { bubbles: true }));
  };

  input.addEventListener('focus', reopen);
  input.addEventListener('click', () => { if (list.hidden) reopen(); });
  input.addEventListener('input', () => open(input.value, true));
  input.addEventListener('blur', close);
  input.addEventListener('keydown', (event) => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (list.hidden) open(input.value);
      const step = event.key === 'ArrowDown' ? 1 : -1;
      if (shown.length) highlight((active + step + shown.length) % shown.length);
    } else if (event.key === 'Enter' && !list.hidden && active >= 0) {
      event.preventDefault();  // pick the zone; do not submit the form yet
      choose(active);
    } else if (event.key === 'Escape' && !list.hidden) {
      event.preventDefault();
      close();
    }
  });
  // mousedown keeps the focus in the input, so the list stays open until the click picks a zone.
  list.addEventListener('mousedown', (event) => event.preventDefault());
  list.addEventListener('click', (event) => {
    const item = event.target.closest('[role="option"]');
    if (item) choose(Number(item.dataset.index));
  });
});

// Old links to TV settings sections that moved to other pages (configure.html [data-hash-moves]).
document.querySelectorAll('[data-hash-moves]').forEach((node) => {
  let moves = {};
  try { moves = JSON.parse(node.dataset.hashMoves); } catch { return; }
  const target = moves[location.hash.slice(1)];
  if (target) location.replace(target);
});

// A collapsed card (<details>) or a Schedule editor opens when a link or the address points at it.
// A card form posts to its own #anchor, so after an error the page opens at that card.
(() => {
  const reveal = (hash) => {
    let id = '';
    try { id = decodeURIComponent(hash.slice(1)); } catch { return; }
    const target = id && document.getElementById(id);
    if (!target) return;
    const details = target.closest('details');
    // Open every fold around it too, such as an Advanced row inside the Address fold.
    for (let fold = details; fold; fold = fold.parentElement && fold.parentElement.closest('details')) fold.open = true;
    // A Schedule editor (schedule.html .editor) opens by its #id, even after Cancel closed it.
    const editor = target.closest('.editor');
    if (editor) editor.classList.remove('closed');
    const field = target.querySelector('form input:not([type="hidden"]), form select');
    target.scrollIntoView({block: 'start'});
    if ((details || editor) && field) field.focus({preventScroll: true});
  };
  reveal(location.hash);
  window.addEventListener('hashchange', () => reveal(location.hash));
  document.querySelectorAll('[data-open-details]').forEach((link) => {
    link.addEventListener('click', (event) => {
      if (link.hash !== location.hash) return;  // a hashchange opens it
      event.preventDefault();
      reveal(link.hash);
    });
  });
})();

// Advanced folds (fold_card(remember=true)) keep each viewer's open or closed choice. Only a click on the
// summary counts: a fold the page opened for a result, an error or an #anchor stays open, and is not saved.
document.querySelectorAll('details[data-fold-remember]').forEach((fold) => {
  const key = `art-frames-fold:${location.pathname}#${fold.id}`;
  if (!fold.open) {
    try { if (localStorage.getItem(key) === 'open') fold.open = true; } catch (error) { /* no storage */ }
  }
  const summary = fold.querySelector(':scope > summary');
  if (!summary) return;
  summary.addEventListener('click', () => setTimeout(() => {
    try { localStorage.setItem(key, fold.open ? 'open' : 'closed'); } catch (error) { /* no storage */ }
  }, 0));
});

// Cancel in an editor (schedule.html [data-cancel]): drop what was typed and close the editor. A page
// drawn after a failed save shows the typed values, so there the link opens the saved page instead.
document.querySelectorAll('[data-cancel]').forEach((link) => {
  link.addEventListener('click', (event) => {
    const editor = link.closest('.editor');
    if (!editor) return;
    editor.querySelectorAll('form').forEach((form) => form.reset());
    editor.querySelectorAll('details').forEach((inner) => { inner.open = false; });
    if (link.pathname !== location.pathname || link.search !== location.search) return;
    event.preventDefault();
    editor.querySelectorAll('.card-note').forEach((note) => note.remove());
    // replaceState does not change :target, so the class closes it.
    editor.classList.remove('open');
    editor.classList.add('closed');
    history.replaceState(null, '', location.pathname + location.search);
    const timeline = document.getElementById('timeline');
    if (timeline) timeline.scrollIntoView({block: 'start'});
  });
});

// Remove asks first, inline (schedule.html details[data-confirm]): Cancel closes the question.
document.querySelectorAll('[data-confirm]').forEach((confirm) => {
  confirm.querySelector('[data-confirm-cancel]').addEventListener('click', () => { confirm.open = false; });
});

// Schedule timeline math, with no page access. tests/test_timeline_math.py runs the part between the markers under Node.
// BEGIN timeline-math
const timelineMath = (() => {
  const DAY = 1440, STEP = 15, NEW_BLOCK = 120;
  const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
  // The nearest 15 minutes.
  const snap = (minute) => Math.round(minute / STEP) * STEP;
  // Minutes of the day as "HH:MM".
  const hhmm = (minute) => `${String(Math.floor(minute / 60)).padStart(2, '0')}:${String(minute % 60).padStart(2, '0')}`;
  // Where a dropped layout's 2-hour block starts: at the pointer, snapped, and inside the day.
  const dropStart = (minute) => clamp(snap(minute), 0, DAY - NEW_BLOCK);
  // web.span_text: "7:00 to 9:00 AM", "10:00 PM to 12:00 AM", "all day".
  const twelve = (minute) => {
    const hour = Math.floor(minute / 60) % 24;
    return [`${hour % 12 || 12}:${String(minute % 60).padStart(2, '0')}`, hour < 12 ? 'AM' : 'PM'];
  };
  const spanText = (start, end) => {
    if (start === 0 && end === DAY) return 'all day';
    const [one, half] = twelve(start), [two, other] = twelve(end);
    return half === other ? `${one} to ${two} ${half}` : `${one} ${half} to ${two} ${other}`;
  };
  // web.tight_span, a block's label: "6–9 AM", "11:30 AM–1 PM", "All day".
  const tightSpan = (start, end) => {
    if (start === 0 && end === DAY) return 'All day';
    const [one, half] = twelve(start), [two, other] = twelve(end);
    const short = (value) => value.replace(/:00$/, '');
    return half === other ? `${short(one)}–${short(two)} ${half}` : `${short(one)} ${half}–${short(two)} ${other}`;
  };
  // Shorter labels for a narrow block, longest first: "7–9 AM", "7–9", "7". The last is the start only.
  const labelSteps = (label) => {
    const bare = label.replace(/ [AP]M/g, '');
    return [...new Set([label, bare, bare.split('–')[0]])];
  };
  return {DAY, STEP, NEW_BLOCK, clamp, snap, hhmm, dropStart, twelve, spanText, tightSpan, labelSteps};
})();
// END timeline-math

// A block's label gets shorter when the block is too narrow for it: "7–9 AM", then "7–9", then "7". When even
// the start does not fit, the label hides. `label` is a new full label, for a block that moved.
const fitLabel = (block, label) => {
  const text = block.querySelector('.block-text');
  if (!text) return;
  if (label !== undefined) text.dataset.label = label;
  else if (!text.dataset.label) text.dataset.label = text.textContent;
  block.classList.remove('tight');
  // On a phone, a block under 56 px shows no label: a cut-off time reads as a wrong time. Tap it to see it.
  if (window.matchMedia('(max-width: 479px)').matches && block.offsetWidth < 56) {
    text.textContent = text.dataset.label;
    block.classList.add('tight');
    return;
  }
  for (const step of timelineMath.labelSteps(text.dataset.label)) {
    text.textContent = step;
    if (text.scrollWidth <= text.clientWidth + 1) return;
  }
  block.classList.add('tight');
};

// Schedule timeline places (schedule.html data-left, data-width, data-now, data-a and data-b, in percent of the
// day). The Content-Security-Policy blocks style attributes, so the script sets them. Without the script, CSS
// hides the timeline; the list under it and the forms still work.
document.querySelectorAll('[data-timeline] .timeline').forEach((timeline) => {
  timeline.querySelectorAll('[data-left]').forEach((node) => {
    node.style.left = `${node.dataset.left}%`;
    if (node.dataset.width) node.style.width = `${node.dataset.width}%`;
  });
  if (timeline.dataset.now) timeline.style.setProperty('--now', timeline.dataset.now);
  timeline.classList.add('placed');
  const fitAll = () => timeline.querySelectorAll('.block').forEach((block) => fitLabel(block));
  fitAll();
  window.addEventListener('resize', fitAll);
});
document.querySelectorAll('[data-timeline] [data-a]').forEach((node) => {
  node.style.setProperty('--a', `var(--hue-${node.dataset.a})`);
  node.style.setProperty('--b', `var(--hue-${node.dataset.b})`);
});

// The Schedule timeline (schedule.html [data-timeline]). With a mouse or a pen:
// - Drag a layout chip onto the timeline: a 2-hour block that starts at the pointer.
// - Drag a block to move it, or its edges to change its times.
// - Drag the turn-on pin.
// Times snap to 15 minutes, and a block stays in the day. Each drop fills the matching editor's form and
// submits it, so the server checks and saves it as it does the form. A click without a move opens the
// editor. Touch uses the forms, so the page still scrolls.
(() => {
  const root = document.querySelector('[data-timeline]');
  const timeline = root && root.querySelector('.timeline');
  if (!timeline) return;
  const MOVED = 4;
  const {DAY, STEP, NEW_BLOCK, clamp, snap, hhmm, dropStart, twelve, tightSpan} = timelineMath;
  const toMinutes = (value) => { const [hour, minute] = value.split(':').map(Number); return hour * 60 + minute; };
  const percent = (minute) => `${(minute / DAY) * 100}%`;
  const box = () => timeline.querySelector('.lane-wake .track').getBoundingClientRect();  // every track has the same width
  const minuteAt = (x) => { const area = box(); return clamp(((x - area.left) / area.width) * DAY, 0, DAY); };
  const perMinute = () => box().width / DAY;
  // A form's end of 12:00 AM is midnight, the end of the day (web.block_values).
  const timeValue = (minute) => (minute >= DAY ? '00:00' : hhmm(minute));
  const submit = (form, values) => {
    if (!form) return;
    Object.entries(values).forEach(([name, value]) => { form.elements[name].value = value; });
    root.classList.add('saving');
    form.requestSubmit();
  };
  // After a drag, the click that follows must not open the editor.
  const swallowClick = () => {
    const stop = (event) => { event.preventDefault(); event.stopPropagation(); };
    window.addEventListener('click', stop, {capture: true, once: true});
    setTimeout(() => window.removeEventListener('click', stop, {capture: true}), 0);
  };
  const follow = (node, event, handlers) => {
    if (event.pointerType === 'touch' || event.button !== 0) return;
    event.preventDefault();
    const startX = event.clientX;
    let moved = false;
    node.setPointerCapture(event.pointerId);
    const move = (next) => {
      if (!moved && Math.abs(next.clientX - startX) < MOVED && Math.abs(next.clientY - event.clientY) < MOVED) return;
      if (!moved) { moved = true; handlers.begin(); }
      handlers.move(next, (next.clientX - startX) / perMinute());
    };
    const end = (last, cancelled) => {
      node.removeEventListener('pointermove', move);
      node.removeEventListener('pointerup', up);
      node.removeEventListener('pointercancel', cancel);
      if (!moved) return;
      swallowClick();
      handlers.end(last, cancelled);
    };
    const up = (last) => end(last, false);
    const cancel = (last) => end(last, true);
    node.addEventListener('pointermove', move);
    node.addEventListener('pointerup', up);
    node.addEventListener('pointercancel', cancel);
  };

  // A block: move it, or change one edge.
  timeline.querySelectorAll('a[data-block]').forEach((block) => {
    block.addEventListener('pointerdown', (event) => {
      const edge = event.target.closest('[data-edge]');
      const kind = edge ? edge.dataset.edge : 'move';
      const start = toMinutes(block.dataset.start), end = block.dataset.end === '24:00' ? DAY : toMinutes(block.dataset.end);
      let next = [start, end];
      const draw = ([one, two]) => {
        block.style.left = percent(one);
        block.style.width = percent(two - one);
        fitLabel(block, tightSpan(one, two));
      };
      follow(block, event, {
        begin: () => block.classList.add('dragging'),
        move: (_, shift) => {
          if (kind === 'move') {
            const one = clamp(snap(start + shift), 0, DAY - (end - start));
            next = [one, one + end - start];
          } else if (kind === 'start') {
            next = [clamp(snap(start + shift), 0, end - STEP), end];
          } else {
            next = [start, clamp(snap(end + shift), start + STEP, DAY)];
          }
          draw(next);
        },
        end: (_, cancelled) => {
          block.classList.remove('dragging');
          if (cancelled || (next[0] === start && next[1] === end)) { draw([start, end]); return; }
          const form = document.querySelector(`form[data-block-form="${block.dataset.block}"]`);
          submit(form, {start: hhmm(next[0]), end: timeValue(next[1])});
        },
      });
    });
  });

  // A layout chip: a new 2-hour block, in the layout's lane. A layout without a lane gets one while it is dragged.
  const wakeLane = timeline.querySelector('.lane-wake');
  document.querySelectorAll('.app-chip[data-app]').forEach((chip) => {
    chip.addEventListener('pointerdown', (event) => {
      const app = chip.dataset.app;
      const swatch = chip.querySelector('.swatch');
      const hue = swatch ? [...swatch.classList].find(name => name.startsWith('hue-')) : 'hue-0';
      let lane = timeline.querySelector(`[data-lane="${CSS.escape(app)}"]`), added = null, ghost = null, start = null;
      follow(chip, event, {
        begin: () => {
          if (!lane) {
            added = document.createElement('div');
            added.className = 'lane lane-new';
            added.innerHTML = '<span class="lane-name"></span><div class="track" data-track></div>';
            added.querySelector('.lane-name').textContent = chip.textContent.trim();
            timeline.insertBefore(added, timeline.querySelector('.lane-empty') || wakeLane);
            lane = added;
          }
          ghost = document.createElement('span');
          ghost.className = `block ghost ${hue}`;
          ghost.innerHTML = '<span class="block-text"></span>';
          ghost.hidden = true;
          lane.querySelector('[data-track]').append(ghost);
          timeline.classList.add('dropping');
          chip.classList.add('dragging');
        },
        move: (next) => {
          const area = timeline.getBoundingClientRect();
          const over = next.clientY >= area.top - 24 && next.clientY <= area.bottom + 24
            && next.clientX >= area.left && next.clientX <= area.right;
          ghost.hidden = !over;
          start = over ? dropStart(minuteAt(next.clientX)) : null;
          if (start === null) return;
          ghost.style.left = percent(start);
          ghost.style.width = percent(NEW_BLOCK);
          fitLabel(ghost, tightSpan(start, start + NEW_BLOCK));
        },
        end: (_, cancelled) => {
          chip.classList.remove('dragging');
          timeline.classList.remove('dropping');
          if (cancelled || start === null) {
            ghost.remove();
            if (added) added.remove();
            if (added) lane = null;
            return;
          }
          const form = document.querySelector(`form[data-block-form="add"][data-app="${CSS.escape(app)}"]`);
          submit(form, {start: hhmm(start), end: timeValue(start + NEW_BLOCK)});
        },
      });
    });
  });

  // The turn-on pin.
  const pin = timeline.querySelector('a[data-wake]');
  if (pin) {
    const label = pin.querySelector('.pin-label');
    pin.addEventListener('pointerdown', (event) => {
      const at = toMinutes(pin.dataset.wake);
      let next = at;
      const draw = (minute) => {
        pin.style.left = percent(minute);
        pin.classList.toggle('late', minute > DAY * 0.85);
        label.textContent = twelve(minute).join(' ');
      };
      follow(pin, event, {
        begin: () => pin.classList.add('dragging'),
        move: (_, shift) => { next = clamp(snap(at + shift), 0, DAY - STEP); draw(next); },
        end: (_, cancelled) => {
          pin.classList.remove('dragging');
          if (cancelled || next === at) { draw(at); return; }
          submit(document.querySelector('form[data-wake-form]'), {at: hhmm(next)});
        },
      });
    });
  }
})();

// Unsaved changes ([data-card-form]). A card shows "Unsaved" while its form differs from the form as the
// page drew it. Before another form submits, or the page closes, the person chooses to keep editing the
// changed card or to discard its changes.
(() => {
  const forms = [...document.querySelectorAll('[data-card-form]')];
  if (!forms.length) return;
  let leaving = false;
  const holder = (form) => form.closest('details.fold-card, .card');
  const snapshot = (form) => JSON.stringify([...new FormData(form)].filter(([name]) => name !== 'csrf'));
  const drawn = new Map(forms.map(form => [form, snapshot(form)]));
  const mark = (form, changed) => {
    form.dataset.changed = changed ? '1' : '';
    const tag = holder(form) && holder(form).querySelector('[data-unsaved]');
    if (tag) tag.hidden = !changed;
  };
  const update = (form) => mark(form, snapshot(form) !== drawn.get(form));
  forms.forEach((form) => {
    form.addEventListener('input', () => update(form));
    form.addEventListener('change', () => update(form));
  });
  const dialog = document.createElement('dialog');
  dialog.className = 'confirm-dialog';
  dialog.innerHTML = '<p class="confirm-dialog-text"></p><div class="confirm-dialog-actions">'
    + '<button class="button outline" type="button" data-keep-editing>Keep editing</button>'
    + '<button class="button destructive" type="button" data-discard>Discard changes</button></div>';
  document.body.append(dialog);
  let pending = null;  // {form, submitter, changed}
  dialog.querySelector('[data-keep-editing]').addEventListener('click', () => {
    dialog.close();
    const card = holder(pending.changed);
    if (card && card.tagName === 'DETAILS') card.open = true;
    (card || pending.changed).scrollIntoView({block: 'start'});
    pending = null;
  });
  dialog.querySelector('[data-discard]').addEventListener('click', () => {
    dialog.close();
    forms.forEach(form => mark(form, false));
    const {form, submitter} = pending;
    pending = null;
    if (submitter && submitter.form === form) form.requestSubmit(submitter); else form.requestSubmit();
  });
  // Capture on the document: this runs before each form's own submit handlers.
  document.addEventListener('submit', (event) => {
    const form = event.target;
    const changed = forms.find(item => item.dataset.changed && holder(item) !== (holder(form) || form) && item !== form);
    if (!changed) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    pending = {form, submitter: event.submitter, changed};
    dialog.querySelector('.confirm-dialog-text').textContent =
      `You have unsaved changes in "${changed.dataset.cardLabel}". Leave them unsaved?`;
    dialog.showModal();
  }, true);
  // A submit that no check stopped leaves the page on purpose.
  window.addEventListener('submit', (event) => { if (!event.defaultPrevented) leaving = true; });
  window.addEventListener('beforeunload', (event) => {
    if (!leaving && forms.some(form => form.dataset.changed)) { event.preventDefault(); event.returnValue = ''; }
  });
})();

// Sensor page and the Now page's Room block: they read live.json every 5 s while the page is in view (sensor_web.live_json).
document.querySelectorAll('[data-sensor-live]').forEach((card) => {
  const field = name => card.querySelector(`[data-sensor-field="${name}"]`);
  const update = async () => {
    if (document.hidden) return;
    try {
      const response = await fetch(card.dataset.url, {headers: {Accept: 'application/json'}, credentials: 'same-origin'});
      if (!response.ok) return;
      const data = await response.json();
      ['presence', 'light', 'control', 'word', 'brightness'].forEach((name) => {
        const node = field(name);
        if (!node) return;
        node.textContent = data[name] || '';
        // The Now page's Room block hides a row with nothing to say.
        const row = node.closest('[data-room-row]');
        if (row) row.hidden = !data[name];
      });
      // The live line shows in the Sensor control card.
      document.querySelectorAll('[data-sensor-field="curve"]').forEach((line) => { line.textContent = data.curve || ''; });
      if (data.check && field('status')) {
        const dot = field('status').querySelector('.status-dot');
        const text = field('status').querySelector('.status-text');
        if (dot) dot.className = `status-dot tone-${data.check.dot}`;  // sensor_control.DOTS
        if (text) text.textContent = data.check.title;
        if (field('when')) field('when').textContent = data.check.when || '';
        if (field('detail')) field('detail').textContent = data.check.detail || '';
      }
    } catch (error) { /* the next tick tries again */ }
  };
  setInterval(update, 5000);
});

// Preview first: a <dialog>. Without JavaScript, the link opens the page with ?preview=1 and the
// dialog shows open in the page. With JavaScript, the link opens it as a modal instead.
document.querySelectorAll('dialog[data-dialog]').forEach((dialog) => {
  if (dialog.open && typeof dialog.showModal === 'function') { dialog.close(); dialog.showModal(); }
  dialog.addEventListener('close', () => {
    const url = new URL(window.location.href);
    if (url.searchParams.has('preview')) { url.searchParams.delete('preview'); window.history.replaceState(null, '', url); }
  });
});
document.querySelectorAll('[data-open-dialog]').forEach((link) => {
  const dialog = document.getElementById(link.dataset.openDialog);
  if (!dialog || typeof dialog.showModal !== 'function') return;
  link.addEventListener('click', (event) => {
    event.preventDefault();
    const menu = link.closest('details');
    if (menu) menu.open = false;
    if (!dialog.open) dialog.showModal();
  });
});

// The preview caption names the layout picked in the form. Its words live in data-preview-label-text.
document.querySelectorAll('[data-preview-panel]').forEach((panel) => {
  const label = panel.querySelector('[data-preview-label]');
  const form = panel.querySelector('[data-preview-form]');
  const stage = panel.querySelector('[data-preview-stage]');
  if (!label || !form || !stage) return;
  form.addEventListener('submit', () => {
    const select = form.querySelector('select[name="app"]');
    label.dataset.pending = select ? select.selectedOptions[0].textContent : '';
  });
  new MutationObserver(() => {
    if (label.dataset.pending !== undefined && stage.querySelector('img')) {
      label.textContent = label.dataset.pending ? label.dataset.previewLabelText.replace('{app}', () => label.dataset.pending) : '';
    }
  }).observe(stage, {childList: true});
});

// Countdowns: "Switches to News in 4 min". The server writes the words; {left} is the time left.
(() => {
  const items = Array.from(document.querySelectorAll('[data-countdown]'));
  if (!items.length) return;
  const left = (ms) => {
    const minutes = Math.ceil(ms / 60000);
    if (minutes < 60) return `${minutes} min`;
    const hours = Math.floor(minutes / 60), rest = minutes % 60;
    return rest ? `${hours} h ${rest} min` : `${hours} h`;
  };
  const tick = () => items.forEach((item) => {
    const ms = new Date(item.dataset.countdown).getTime() - Date.now();
    if (Number.isNaN(ms)) return;
    item.textContent = ms > 0 ? item.dataset.countdownText.replace('{left}', () => left(ms)) : item.dataset.countdownDone;
    if (ms <= 0) reloadOnce(item.dataset.countdown);
  });
  // At 0, load the page once more after the swap has had time to run, so it shows the new layout or why
  // the turns paused. Once per page and switch minute, and never while a menu or dialog is open.
  let reloading = false;
  const reloadOnce = (target) => {
    if (reloading) return;
    const at = `${location.pathname} ${new Date(target).toISOString().slice(0, 16)}`;
    let seen = '';
    try { seen = sessionStorage.getItem('art-frames-countdown') || ''; } catch (error) { return; }
    if (seen === at) return;
    reloading = true;
    setTimeout(() => {
      // An open Activity drawer counts too: a reload would close it. The next tick tries again.
      if (document.hidden || document.querySelector('dialog[open], details[data-menu][open], [data-activity-drawer]:not([hidden])')) { reloading = false; return; }
      try { sessionStorage.setItem('art-frames-countdown', at); } catch (error) { return; }
      location.reload();
    }, 20000);
  };
  tick();
  setInterval(tick, 15000);
})();

// Copy a command (apps.html [data-copy]). The button shows only where the clipboard works.
document.querySelectorAll('[data-copy]').forEach((button) => {
  const source = button.parentElement.querySelector('[data-copy-source]');
  if (!source || !navigator.clipboard) return;
  button.hidden = false;
  button.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(source.textContent.trim());
      button.textContent = 'Copied';
    } catch {
      button.textContent = 'Select and copy it';
    }
  });
});

// Go back (error.html [data-go-back]): shown only when there is a page to go back to.
document.querySelectorAll('[data-go-back]').forEach((link) => {
  if (history.length < 2) return;
  link.hidden = false;
  link.addEventListener('click', (event) => { event.preventDefault(); history.back(); });
});

// Advanced rows (_ui.html advanced_row): remember in this browser which ones the viewer left open.
// A row the server opens (after an error) stays open. Storage can be missing; the row then starts closed.
document.querySelectorAll("details[data-advanced-row]").forEach((row) => {
  const key = `advanced:${location.pathname}#${row.id}`;
  if (location.hash === `#${row.id}`) row.open = true;  // a link to the row, such as a plan's turn-on note
  if (!row.open) {
    try { if (localStorage.getItem(key) === "open") row.open = true; } catch { /* no storage */ }
  }
  row.addEventListener("toggle", () => {
    try { if (row.open) localStorage.setItem(key, "open"); else localStorage.removeItem(key); } catch { /* no storage */ }
  });
});

// Presets (_ui.html preset_group): choosing an option writes its values into the fields under Custom, so the
// fields always show what Save stores, and Custom starts from the last preset.
document.querySelectorAll("[data-preset]").forEach((group) => {
  const form = group.closest("form");
  group.addEventListener("change", (event) => {
    const choice = event.target.tagName === "SELECT" ? event.target.selectedOptions[0] : event.target;
    if (!form || !choice || !choice.dataset.values) return;
    let values = {};
    try { values = JSON.parse(choice.dataset.values); } catch { return; }
    Object.entries(values).forEach(([key, value]) => {
      const field = form.querySelector(`[name="${CSS.escape(key)}"]`);
      if (!field) return;
      if (field.type === "checkbox") field.checked = Boolean(value);
      else field.value = String(value);
    });
  });
});

// The Home checklist offers this browser's timezone (home.html [data-browser-zone]). Without Intl it stays hidden.
document.querySelectorAll("[data-browser-zone]").forEach((button) => {
  let zone = "";
  try { zone = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch { /* no Intl */ }
  if (!zone || zone === "UTC") return;
  button.value = zone;
  // "America/New_York" reads "Use New York time".
  button.textContent = `Use ${zone.split("/").pop().replace(/_/g, " ")} time`;
  button.hidden = false;
});
