/* avctl -- the remote's behaviour.
 *
 * Five jobs, and deliberately nothing else:
 *   1. poll /api/state and paint the screen from it
 *   2. POST every button press to /api/cmd, verbatim, and say what came back
 *   3. route between panels on the hash
 *   4. carry the authenticated Ask panel to the server-side control agent
 *   5. carry transient trackpad/keyboard input to the Mac session helper
 *
 * No button knows what its command does. The id in data-cmd is handed to the
 * server as-is, so wiring a device up later touches Python only -- and until
 * then the server answers 501 and the phone honestly says "coming soon".
 *
 * textContent everywhere, never innerHTML: some of these strings are device
 * names and error text from hardware, and none of it is ours to trust.
 */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const led = $('#led');
const toastEl = $('#toast');
const appShell = $('.app');

// Native Settings owns these preferences in the app; Safari keeps the last
// selection in localStorage. Closed sets make the native bridge data-only --
// no arbitrary script or CSS value crosses it.
const MUSIC_LAYOUTS = new Set(['docked', 'stage', 'cover-flow', 'split-deck']);
const PANEL_THEMES = new Set([
  'glass', 'mcintosh', 'porcelain', 'midnight', 'warm', 'contrast',
]);
function savedAppearance(key, fallback, allowed) {
  try {
    const value = localStorage.getItem(key);
    return allowed.has(value) ? value : fallback;
  } catch (err) { return fallback; }
}
let activeMusicLayout = savedAppearance(
  'avctl-music-layout', 'docked', MUSIC_LAYOUTS);
let activePanelTheme = savedAppearance(
  'avctl-panel-theme', 'glass', PANEL_THEMES);

function setAppearance(layout, theme) {
  activeMusicLayout = MUSIC_LAYOUTS.has(layout) ? layout : 'docked';
  activePanelTheme = PANEL_THEMES.has(theme) ? theme : 'glass';
  document.documentElement.dataset.musicLayout = activeMusicLayout;
  document.documentElement.dataset.theme = activePanelTheme;
  try {
    localStorage.setItem('avctl-music-layout', activeMusicLayout);
    localStorage.setItem('avctl-panel-theme', activePanelTheme);
  } catch (err) { /* private browsing can refuse storage; the view still works */ }
  document.dispatchEvent(new CustomEvent('avctlappearancechange', {
    detail: { layout: activeMusicLayout, theme: activePanelTheme },
  }));
}
document.documentElement.dataset.musicLayout = activeMusicLayout;
document.documentElement.dataset.theme = activePanelTheme;
window.avctlSetAppearance = setAppearance;

function persistAppearanceFromSettings(layout, theme) {
  setAppearance(layout, theme);
  window.webkit?.messageHandlers?.avctl?.postMessage({
    event: 'appearance-changed',
    layout: activeMusicLayout,
    theme: activePanelTheme,
  });
}

let snapshot = null;
let implemented = new Set();
let inflight = 0;

// ---- chrome -------------------------------------------------------------

function busy(delta) {
  inflight += delta;
  led.classList.toggle('busy', inflight > 0);
}

let toastTimer = null;
function toast(title, body) {
  toastEl.replaceChildren();
  if (title) {
    const b = document.createElement('b');
    b.textContent = title;
    toastEl.append(b);
  }
  toastEl.append(document.createTextNode(body));
  toastEl.classList.add('on');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove('on'), 2600);
}

// ---- settings workspace ------------------------------------------------
// Settings is an alternate root mode, not another control panel in the rail.
// The trigger and return control occupy the same top-right position, so the
// path in and out stays spatially predictable on phone and iPad.
const settingsWorkspace = $('#settings-workspace');
const settingsToggle = $('#settings-toggle');
const brandTitle = $('#brand-title');
const providerProfiles = $('#provider-profiles');
const providerState = $('#provider-state');
const panelSettingList = $('#panel-setting-list');
const panelSettingsState = $('#panel-settings-state');
const musicBackends = $('#music-backends');
const musicBackendState = $('#music-backend-state');
let settingsOpen = false;
let providerSettingsLoaded = false;
let activeProviderProfile = '';
let selectedProviderProfile = '';
let providerManaged = false;
let panelSettingsLoaded = false;
let panelSettingsManaged = false;
let panelDraft = [];
let savedPanelFingerprint = '';
let musicBackendLoaded = false;
let musicBackendManaged = false;
let activeMusicBackend = '';
let selectedMusicBackend = '';
let setupLoaded = false;
let setupStep = 0;
let setupManifest = null;
let setupDiscovery = {};
let setupDraft = {
  server: {port: 8000},
  music: 'apple_music', panels: new Set(['home', 'music', 'agent']),
  roon: {}, tv: {mac_input: 'HDMI_2'}, amp: {mode: 'serial'}, dac: {mode: 'itach'},
  agent_profile: '',
  agent_credential: '',
  voice: {enabled: true},
  apple_services: {mode: 'local', url: '', invite: '', enrolled: false},
  max_volume: {amp: 70, music: 80},
  scene_volume: {amp: 60, music: 56},
};

function syncAppearanceChoices() {
  $$('.layout-choice').forEach((button) => {
    const selected = button.dataset.layout === activeMusicLayout;
    button.classList.toggle('on', selected);
    button.setAttribute('aria-pressed', String(selected));
  });
  $$('.theme-choice').forEach((button) => {
    const selected = button.dataset.theme === activePanelTheme;
    button.classList.toggle('on', selected);
    button.setAttribute('aria-pressed', String(selected));
  });
}

function setSettingsOpen(open) {
  settingsOpen = Boolean(open);
  appShell.classList.toggle('settings-open', settingsOpen);
  settingsToggle.setAttribute('aria-expanded', String(settingsOpen));
  settingsToggle.setAttribute('aria-label',
    settingsOpen ? 'Return to controls' : 'Open settings');
  settingsWorkspace.setAttribute('aria-hidden', String(!settingsOpen));
  settingsWorkspace.inert = !settingsOpen;
  rail.inert = settingsOpen;
  $('.tabs').inert = settingsOpen;
  $('.mbar').inert = settingsOpen;
  brandTitle.textContent = settingsOpen ? 'settings' : 'avctl';
  if (settingsOpen) {
    document.activeElement?.blur();
    syncAppearanceChoices();
    if (!providerSettingsLoaded) loadProviderSettings();
    if (!panelSettingsLoaded) loadPanelSettings();
    if (!musicBackendLoaded) loadMusicBackendSettings();
    if (!setupLoaded) loadSetup();
    requestNativeSettings();
    settingsWorkspace.querySelector('.settings-page.on')?.scrollTo(0, 0);
  }
}

settingsToggle?.addEventListener('click', () => {
  setSettingsOpen(!settingsOpen);
});

window.avctlOpenSetup = function avctlOpenSetup() {
  const button = $('[data-settings-page="setup"]');
  if (button) button.click();
  setSettingsOpen(true);
};

$$('[data-settings-page]').forEach((button) => {
  button.addEventListener('click', () => {
    const page = button.dataset.settingsPage;
    $$('[data-settings-page]').forEach((item) => {
      item.classList.toggle('on', item === button);
    });
    $$('[data-settings-detail]').forEach((item) => {
      item.classList.toggle('on', item.dataset.settingsDetail === page);
    });
    if (page === 'panels') {
      if (!panelSettingsLoaded) loadPanelSettings();
      if (!musicBackendLoaded) loadMusicBackendSettings();
    }
    if (page === 'setup' && !setupLoaded) loadSetup();
  });
});

$$('.layout-choice').forEach((button) => {
  button.addEventListener('click', () => {
    persistAppearanceFromSettings(button.dataset.layout, activePanelTheme);
    syncAppearanceChoices();
  });
});
$$('.theme-choice').forEach((button) => {
  button.addEventListener('click', () => {
    persistAppearanceFromSettings(activeMusicLayout, button.dataset.theme);
    syncAppearanceChoices();
  });
});
document.addEventListener('avctlappearancechange', syncAppearanceChoices);

// ---- guided setup -----------------------------------------------------
const setupRoot = $('#setup-root');
const setupStepNames = ['Check', 'Music', 'Panels', 'Devices', 'Access', 'Review'];

function setupButton(label, className, action) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = className;
  button.textContent = label;
  button.addEventListener('click', action);
  return button;
}

function setupField(label, id, value, placeholder = '', type = 'text') {
  const row = document.createElement('label');
  row.className = 'setting-field';
  const title = document.createElement('span');
  title.textContent = label;
  const input = document.createElement('input');
  input.id = id;
  input.type = type;
  // Zero is a deliberate safe volume, not a missing setup value.
  input.value = value ?? '';
  input.placeholder = placeholder;
  input.autocapitalize = 'off';
  input.autocomplete = 'off';
  input.spellcheck = false;
  row.append(title, input);
  return row;
}

function setupChoice(id, label, detail, selected, action) {
  const button = setupButton('', 'setup-option' + (selected ? ' on' : ''), action);
  button.dataset.value = id;
  const mark = document.createElement('span');
  mark.className = 'setup-option-mark';
  mark.setAttribute('aria-hidden', 'true');
  const copy = document.createElement('span');
  copy.className = 'setup-option-copy';
  const title = document.createElement('b');
  title.textContent = label;
  const small = document.createElement('small');
  small.textContent = detail;
  copy.append(title, small);
  button.append(mark, copy);
  return button;
}

function captureSetupFields() {
  const value = (id) => $('#' + id)?.value.trim() || '';
  const currentOr = (id, current, fallback = '') => {
    const field = $('#' + id);
    if (field) return field.value.trim();
    return current ?? fallback;
  };
  setupDraft.server = {
    port: currentOr('setup-core-port', setupDraft.server.port, '8000'),
  };
  setupDraft.roon = {
    host: currentOr('setup-roon-host', setupDraft.roon.host),
    port: currentOr('setup-roon-port', setupDraft.roon.port),
    core_id: currentOr('setup-roon-core', setupDraft.roon.core_id),
    zone_id: currentOr('setup-roon-zone', setupDraft.roon.zone_id),
    output_id: currentOr('setup-roon-output', setupDraft.roon.output_id),
  };
  setupDraft.tv = {
    host: currentOr('setup-tv-host', setupDraft.tv.host),
    mac: currentOr('setup-tv-mac', setupDraft.tv.mac),
    mac_input: currentOr('setup-tv-input', setupDraft.tv.mac_input, 'HDMI_2'),
  };
  setupDraft.amp = Object.assign({}, setupDraft.amp, {
    port: currentOr('setup-amp-port', setupDraft.amp.port),
  });
  setupDraft.dac = Object.assign({}, setupDraft.dac, {
    host: currentOr('setup-itach-host', setupDraft.dac.host),
    ir_port: currentOr('setup-itach-port', setupDraft.dac.ir_port, '1'),
  });
  setupDraft.max_volume = {
    music: currentOr('setup-music-max', setupDraft.max_volume.music, '80'),
    amp: currentOr('setup-amp-max', setupDraft.max_volume.amp, '70'),
  };
  setupDraft.scene_volume = {
    music: currentOr('setup-music-scene', setupDraft.scene_volume.music, '56'),
    amp: currentOr('setup-amp-scene', setupDraft.scene_volume.amp, '60'),
  };
  setupDraft.apple_services = Object.assign({}, setupDraft.apple_services, {
    url: currentOr('setup-broker-url', setupDraft.apple_services.url),
    invite: currentOr('setup-broker-invite', setupDraft.apple_services.invite),
  });
  setupDraft.agent_credential = value('setup-agent-credential') ||
    setupDraft.agent_credential || '';
}

function setupHeader(card, title, detail) {
  const heading = document.createElement('h2');
  heading.textContent = title;
  const copy = document.createElement('p');
  copy.textContent = detail;
  card.append(heading, copy);
}

function renderInstallSetup(card) {
  setupHeader(card, 'Check this Mac',
    'The installer reports what is present before you choose panels. Missing optional capabilities stay off instead of producing broken controls.');
  const install = setupManifest.installation || {};
  const summary = document.createElement('div'); summary.className = 'setup-summary';
  const host = document.createElement('div'); host.className = 'setup-summary-row';
  const hostName = document.createElement('span'); hostName.textContent = 'Host';
  const hostValue = document.createElement('b');
  hostValue.textContent = [install.system, install.os_version, install.architecture]
    .filter(Boolean).join(' · ') || 'Unknown';
  host.append(hostName, hostValue); summary.append(host);
  (install.components || []).forEach((component) => {
    const row = document.createElement('div'); row.className = 'setup-summary-row';
    const label = document.createElement('span'); label.textContent = component.label;
    const value = document.createElement('b');
    value.textContent = (component.available ? 'Ready · ' : 'Unavailable · ') + component.detail;
    value.classList.toggle('setup-unavailable', !component.available);
    row.append(label, value); summary.append(row);
  });
  card.append(summary);
  if (install.supported_host === false) {
    setupDiscoveryText(card,
      'This release supports Apple-silicon Macs running macOS 14 or newer.', 'setup-error');
  }
  setupNavigation(card);
}

function setupNavigation(card, {scan = null, finish = false} = {}) {
  const actions = document.createElement('div');
  actions.className = 'setup-actions';
  if (setupStep > 0) {
    actions.append(setupButton('Back', 'setting-secondary', () => {
      captureSetupFields(); setupStep -= 1; renderSetup();
    }));
  }
  const right = document.createElement('span');
  right.className = 'right';
  if (scan) right.append(setupButton(scan.label, 'setting-secondary', scan.run));
  right.append(setupButton(finish ? 'Save setup' : 'Continue', 'setting-primary',
    finish ? activateSetup : () => {
      captureSetupFields(); setupStep += 1; renderSetup();
    }));
  actions.append(right);
  card.append(actions);
}

function setupDiscoveryText(card, text, className = '') {
  const note = document.createElement('div');
  note.className = 'setup-discovery ' + className;
  note.textContent = text;
  card.append(note);
}

async function runSetupDiscovery(kind) {
  const card = $('.setup-card.on');
  setupDiscoveryText(card, 'Looking…');
  try {
    const response = await fetchT('/api/setup/discover', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({kind}),
    }, kind === 'network' || kind === 'music' ? 30000 : 12000);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || 'Discovery failed');
    setupDiscovery[kind] = data;
    if (kind === 'music') {
      const roon = (data.candidates || []).find((item) => item.id === 'roon');
      const core = roon?.cores?.length === 1 ? roon.cores[0] : null;
      if (core) {
        setupDraft.roon.host = core.host;
        setupDraft.roon.port = String(core.port);
      }
    } else if (kind === 'network') {
      const tvs = (data.candidates || []).filter((item) => item.kinds?.includes('lg_webos'));
      const itachs = (data.candidates || []).filter((item) => item.kinds?.includes('itach'));
      const tv = tvs.length === 1 ? tvs[0] : null;
      const itach = itachs.length === 1 ? itachs[0] : null;
      if (tv) setupDraft.tv.host = tv.host;
      if (itach) setupDraft.dac.host = itach.host;
    } else if (kind === 'serial') {
      const serial = data.candidates?.length === 1 ? data.candidates[0] : null;
      if (serial) setupDraft.amp.port = serial.path;
    }
    renderSetup();
  } catch (err) {
    setupDiscovery[kind] = {error: err.message || 'Discovery failed'};
    renderSetup();
  }
}

async function authorizeSetupRoon() {
  captureSetupFields();
  const request = async (body) => {
    const response = await fetchT('/api/setup/roon', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    }, 15000);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || 'Roon authorization failed');
    return data;
  };
  try {
    setupDiscovery.roon_auth = await request({action: 'start',
      host: setupDraft.roon.host, port: setupDraft.roon.port});
    renderSetup();
    for (let attempt = 0; attempt < 120; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      const status = await request({action: 'status'});
      setupDiscovery.roon_auth = status;
      if (status.authorized) {
        setupDraft.roon.core_id = status.core_id || setupDraft.roon.core_id || '';
        if (status.zones?.length === 1) setupDraft.roon.zone_id = status.zones[0].id;
        if (status.outputs?.length === 1) setupDraft.roon.output_id = status.outputs[0].id;
        renderSetup(); return;
      }
      if (status.state !== 'waiting') { renderSetup(); return; }
      if (attempt % 5 === 4) renderSetup();
    }
  } catch (err) {
    setupDiscovery.roon_auth = {state: 'error', authorized: false,
      detail: err.message || 'Roon authorization failed'};
    renderSetup();
  }
}

function renderMusicSetup(card) {
  setupHeader(card, 'Choose music',
    'Use Apple Music on this Mac, Roon and Qobuz, or switch between both later.');
  const options = document.createElement('div');
  options.className = 'setup-options';
  (setupManifest.music || []).forEach((provider) => {
    options.append(setupChoice(provider.id, provider.label, provider.detail,
      setupDraft.music === provider.id, () => {
        setupDraft.music = provider.id; renderSetup();
      }));
  });
  card.append(options);
  if (setupDraft.music === 'apple_music') {
    const consent = setupDiscovery.apple_music;
    setupDiscoveryText(card, consent?.detail ||
      'Authorize once so Core can read Music.app and the signed bridge can play Apple Music catalog results.',
      consent?.error ? 'setup-error' : (consent?.authorized ? 'setup-success' : ''));
    const consentActions = document.createElement('div');
    consentActions.className = 'setup-actions';
    consentActions.append(setupButton(
      consent?.authorized ? 'Apple Music ready' : 'Authorize Apple Music',
      'setting-secondary', authorizeSetupAppleMusic));
    consentActions.firstChild.disabled = Boolean(consent?.authorized);
    card.append(consentActions);
  }
  const result = setupDiscovery.music;
  if (result) {
    if (result.error) setupDiscoveryText(card, result.error, 'setup-error');
    else setupDiscoveryText(card, (result.candidates || []).map((item) => {
      if (item.id === 'roon' && item.cores?.length) {
        return 'Roon found at ' + item.cores.map((core) =>
          core.host + ':' + core.port).join(', ');
      }
      return item.label + ': ' + (item.available ? 'available' : 'not detected');
    }).join('\n'));
    const roon = (result.candidates || []).find((item) => item.id === 'roon');
    if (roon?.cores?.length > 1) {
      const choices = document.createElement('div'); choices.className = 'setup-options';
      roon.cores.forEach((core) => choices.append(setupChoice(
        core.host + ':' + core.port, 'Roon Core at ' + core.host,
        'Port ' + core.port,
        setupDraft.roon.host === core.host && String(setupDraft.roon.port) === String(core.port),
        () => { setupDraft.roon.host = core.host; setupDraft.roon.port = String(core.port); renderSetup(); })));
      card.append(choices);
    }
  }
  setupNavigation(card, {scan: {label: 'Discover', run: () => runSetupDiscovery('music')}});
}

async function authorizeSetupAppleMusic() {
  setupDiscovery.apple_music = {detail: 'Waiting for macOS permission…'};
  renderSetup();
  try {
    const response = await fetchT('/api/setup/apple-music', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: 'authorize'}),
    }, 75000);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || 'Apple Music authorization failed');
    setupDiscovery.apple_music = data;
  } catch (err) {
    setupDiscovery.apple_music = {error: true,
      detail: err.message || 'Apple Music authorization failed'};
  }
  renderSetup();
}

function renderPanelSetup(card) {
  setupHeader(card, 'Choose panels',
    'Home stays on. Every other control can be skipped now and enabled later.');
  const list = document.createElement('div');
  list.className = 'setup-panel-list';
  (setupManifest.panels || []).forEach((panel) => {
    const enabled = panel.required || setupDraft.panels.has(panel.id);
    const button = setupButton('', 'setup-panel' + (enabled ? ' on' : ''), () => {
      if (panel.required) return;
      if (setupDraft.panels.has(panel.id)) setupDraft.panels.delete(panel.id);
      else setupDraft.panels.add(panel.id);
      renderSetup();
    });
    button.disabled = Boolean(panel.required);
    const copy = document.createElement('span');
    const title = document.createElement('b'); title.textContent = panel.label;
    const detail = document.createElement('small');
    detail.textContent = panel.required ? 'Always available' : (enabled ? 'Included' : 'Skipped');
    copy.append(title, document.createElement('br'), detail);
    const state = document.createElement('span'); state.className = 'setup-panel-state';
    state.textContent = enabled ? '✓' : '—';
    button.append(copy, state); list.append(button);
  });
  card.append(list);
  setupNavigation(card);
}

function appendRoonFields(card) {
  const fields = document.createElement('div'); fields.className = 'setup-fields';
  fields.append(
    setupField('Roon host', 'setup-roon-host', setupDraft.roon.host, 'Discovered or manual'),
    setupField('Roon port', 'setup-roon-port', setupDraft.roon.port, '9330', 'number'),
    setupField('Core ID', 'setup-roon-core', setupDraft.roon.core_id, 'Optional when one Core exists'),
    setupField('Music zone', 'setup-roon-zone', setupDraft.roon.zone_id, 'Zone id for playback'),
    setupField('Output', 'setup-roon-output', setupDraft.roon.output_id, 'Output id for DAC / amp'));
  card.append(fields);
  const authorization = setupDiscovery.roon_auth;
  setupDiscoveryText(card, authorization?.detail ||
    'Authorize avctl once, then choose the Roon zone and output by name.');
  const actions = document.createElement('div'); actions.className = 'setup-actions';
  const authorize = setupButton(
    authorization?.state === 'waiting' ? 'Waiting for Roon…' :
      (authorization?.authorized ? 'Roon authorized' : 'Authorize Roon'),
    'setting-secondary', authorizeSetupRoon);
  authorize.disabled = authorization?.state === 'waiting' || Boolean(authorization?.authorized);
  actions.append(authorize);
  card.append(actions);
  if (authorization?.authorized) {
    const zones = document.createElement('div'); zones.className = 'setup-options';
    (authorization.zones || []).forEach((zone) => zones.append(setupChoice(
      zone.id, zone.name, 'Playback zone', setupDraft.roon.zone_id === zone.id,
      () => {
        setupDraft.roon.zone_id = zone.id;
        if (zone.outputs?.length === 1) setupDraft.roon.output_id = zone.outputs[0];
        renderSetup();
      })));
    card.append(zones);
    const outputs = document.createElement('div'); outputs.className = 'setup-options';
    (authorization.outputs || []).forEach((output) => outputs.append(setupChoice(
      output.id, output.name,
      [output.volume ? 'volume' : 'fixed level',
        output.source_control ? 'source control' : 'no source control'].join(' · '),
      setupDraft.roon.output_id === output.id,
      () => { setupDraft.roon.output_id = output.id; renderSetup(); })));
    card.append(outputs);
  }
}

function renderDeviceSetup(card) {
  setupHeader(card, 'Configure devices',
    'Only controls whose panels you kept are shown. Empty optional devices stay skipped.');
  if (setupDraft.music === 'roon' || (setupDraft.panels.has('amp') &&
      (setupDraft.amp.mode === 'roon' || setupDraft.dac.mode === 'roon'))) {
    appendRoonFields(card);
  }
  const musicTitle = document.createElement('h2'); musicTitle.textContent = 'Music levels';
  musicTitle.style.marginTop = '18px'; card.append(musicTitle);
  const musicFields = document.createElement('div'); musicFields.className = 'setup-fields';
  musicFields.append(
    setupField('Music scene level', 'setup-music-scene',
      setupDraft.scene_volume.music, '56', 'number'),
    setupField('Music safety cap', 'setup-music-max',
      setupDraft.max_volume.music, '80', 'number'));
  card.append(musicFields);
  if (setupDraft.panels.has('agent')) {
    const title = document.createElement('h2'); title.textContent = 'Ask provider';
    title.style.marginTop = '18px'; card.append(title);
    const options = document.createElement('div'); options.className = 'setup-options';
    (setupManifest.agent_profiles || []).forEach((profile) => {
      options.append(setupChoice(profile.id, profile.label || profile.id,
        [profile.driver, profile.model,
          profile.credential === 'missing' ? 'credential needed' : profile.credential]
          .filter(Boolean).join(' · '),
        setupDraft.agent_profile === profile.id, () => {
          setupDraft.agent_profile = profile.id;
          setupDraft.agent_credential = '';
          setupDiscovery.agent = null;
          renderSetup();
        }));
    });
    card.append(options);
    const selected = (setupManifest.agent_profiles || []).find(
      (profile) => profile.id === setupDraft.agent_profile);
    if (selected?.credential === 'missing') {
      const credentials = document.createElement('div');
      credentials.className = 'setup-fields';
      credentials.append(setupField('Provider API credential',
        'setup-agent-credential', setupDraft.agent_credential,
        'Stored only on this Mac', 'password'));
      card.append(credentials);
    }
    const agentActions = document.createElement('div');
    agentActions.className = 'setup-actions';
    agentActions.append(setupButton('Save & test Ask', 'setting-secondary',
      configureSetupAgent));
    card.append(agentActions);
    const agentStatus = setupDiscovery.agent;
    if (agentStatus) setupDiscoveryText(card, agentStatus.pending
      ? 'Testing provider and structured tool calls…'
      : (agentStatus.error || ('Tool calls verified · ' +
        Number(agentStatus.latency_ms || 0).toLocaleString() + ' ms')),
    agentStatus.error ? 'setup-error' : (agentStatus.pending ? '' : 'setup-success'));

    const voiceTitle = document.createElement('h2');
    voiceTitle.textContent = 'Ask voice';
    voiceTitle.style.marginTop = '18px'; card.append(voiceTitle);
    const voiceOptions = document.createElement('div');
    voiceOptions.className = 'setup-options';
    voiceOptions.append(
      setupChoice('voice', 'Ask + local voice',
        'Hold to talk on iPhone/iPad; this Mac transcribes before Ask',
        setupDraft.voice.enabled, () => {
          setupDraft.voice.enabled = true; renderSetup();
        }),
      setupChoice('text', 'Text-only Ask',
        'No speech model or microphone setup', !setupDraft.voice.enabled, () => {
          setupDraft.voice.enabled = false; renderSetup();
        }));
    card.append(voiceOptions);
    if (setupDraft.voice.enabled) {
      const voice = setupDiscovery.voice || setupManifest.voice || {};
      setupDiscoveryText(card, voice.pending
        ? 'Downloading/loading the transcription model…'
        : (voice.detail || 'Prepare the local transcription model.'),
      voice.error ? 'setup-error' : (voice.ready ? 'setup-success' : ''));
      const voiceActions = document.createElement('div');
      voiceActions.className = 'setup-actions';
      const prepare = setupButton(voice.ready ? 'Voice ready' : 'Prepare voice',
        'setting-secondary', prepareSetupVoice);
      prepare.disabled = Boolean(voice.pending || voice.ready);
      voiceActions.append(prepare); card.append(voiceActions);
    }
  }
  if (setupDraft.panels.has('mini')) {
    const title = document.createElement('h2'); title.textContent = 'Mac mini control';
    title.style.marginTop = '18px'; card.append(title);
    const helper = setupDiscovery.mini?.helper;
    setupDiscoveryText(card, helper ?
      (helper.available && helper.permission
        ? 'Input helper is running and Accessibility is allowed.'
        : (helper.message || 'Input helper needs Accessibility permission.')) :
      'Enable the bundled input helper, allow it in Privacy & Security → Accessibility, then check it here.');
    const miniActions = document.createElement('div');
    miniActions.className = 'setup-actions';
    miniActions.append(setupButton(helper?.available ? 'Check accessibility' :
      'Enable input helper', 'setting-secondary', configureSetupMini));
    card.append(miniActions);
  }
  if (setupDraft.panels.has('tv')) {
    const title = document.createElement('h2'); title.textContent = 'LG webOS TV';
    title.style.marginTop = '18px'; card.append(title);
    const fields = document.createElement('div'); fields.className = 'setup-fields';
    fields.append(setupField('TV host', 'setup-tv-host', setupDraft.tv.host, '192.168.1.20'),
      setupField('TV MAC', 'setup-tv-mac', setupDraft.tv.mac, 'aa:bb:cc:dd:ee:ff'),
      setupField('Mac HDMI input', 'setup-tv-input', setupDraft.tv.mac_input, 'HDMI_2'));
    card.append(fields);
  }
  if (setupDraft.panels.has('amp')) {
    const title = document.createElement('h2'); title.textContent = 'Amplifier';
    title.style.marginTop = '18px'; card.append(title);
    const options = document.createElement('div'); options.className = 'setup-options';
    options.append(
      setupChoice('serial', 'Serial amplifier', 'Direct USB / RS-232 control',
        setupDraft.amp.mode === 'serial', () => { setupDraft.amp.mode = 'serial'; renderSetup(); }),
      setupChoice('roon', 'Roon volume', 'Use the selected Roon output',
        setupDraft.amp.mode === 'roon', () => { setupDraft.amp.mode = 'roon'; renderSetup(); }));
    card.append(options);
    if (setupDraft.amp.mode === 'serial') {
      const fields = document.createElement('div'); fields.className = 'setup-fields';
      fields.append(setupField('Serial port', 'setup-amp-port', setupDraft.amp.port,
        '/dev/cu.usbserial…')); card.append(fields);
    }
    const levels = document.createElement('div'); levels.className = 'setup-fields';
    levels.append(
      setupField('Amp scene level', 'setup-amp-scene', setupDraft.scene_volume.amp,
        '60', 'number'),
      setupField('Amp safety cap', 'setup-amp-max', setupDraft.max_volume.amp,
        '70', 'number'));
    card.append(levels);
  }
  if (setupDraft.panels.has('amp')) {
    const title = document.createElement('h2'); title.textContent = 'DAC';
    title.style.marginTop = '18px'; card.append(title);
    const options = document.createElement('div'); options.className = 'setup-options';
    options.append(
      setupChoice('itach', 'IR through iTach', 'Network IR for a DAC without an API',
        setupDraft.dac.mode === 'itach', () => { setupDraft.dac.mode = 'itach'; renderSetup(); }),
      setupChoice('roon', 'Roon output', 'Use Roon output state and input selection',
        setupDraft.dac.mode === 'roon', () => { setupDraft.dac.mode = 'roon'; renderSetup(); }));
    card.append(options);
    if (setupDraft.dac.mode === 'itach') {
      const fields = document.createElement('div'); fields.className = 'setup-fields';
      fields.append(setupField('iTach host', 'setup-itach-host', setupDraft.dac.host,
        '192.168.1.30'), setupField('IR port', 'setup-itach-port',
        setupDraft.dac.ir_port || '1', '1', 'number')); card.append(fields);
    }
  }
  const networkCandidates = setupDiscovery.network?.candidates || [];
  if (setupDraft.panels.has('tv')) {
    const candidates = networkCandidates.filter((item) => item.kinds?.includes('lg_webos'));
    if (candidates.length > 1) {
      const choices = document.createElement('div'); choices.className = 'setup-options';
      candidates.forEach((candidate) => choices.append(setupChoice(
        'tv-' + candidate.host, 'TV at ' + candidate.host,
        'webOS ports ' + candidate.ports.join(', '), setupDraft.tv.host === candidate.host,
        () => { setupDraft.tv.host = candidate.host; renderSetup(); })));
      card.append(choices);
    }
  }
  if (setupDraft.panels.has('amp') && setupDraft.amp.mode === 'serial' &&
      (setupDiscovery.serial?.candidates || []).length > 1) {
    const choices = document.createElement('div'); choices.className = 'setup-options';
    setupDiscovery.serial.candidates.forEach((candidate) => choices.append(setupChoice(
      'serial-' + candidate.path, candidate.name || candidate.path, candidate.path,
      setupDraft.amp.port === candidate.path,
      () => { setupDraft.amp.port = candidate.path; renderSetup(); })));
    card.append(choices);
  }
  if (setupDraft.panels.has('amp') && setupDraft.dac.mode === 'itach') {
    const candidates = networkCandidates.filter((item) => item.kinds?.includes('itach'));
    if (candidates.length > 1) {
      const choices = document.createElement('div'); choices.className = 'setup-options';
      candidates.forEach((candidate) => choices.append(setupChoice(
        'itach-' + candidate.host, 'iTach at ' + candidate.host,
        'IR bridge on port 4998', setupDraft.dac.host === candidate.host,
        () => { setupDraft.dac.host = candidate.host; renderSetup(); })));
      card.append(choices);
    }
  }
  const results = [];
  if (setupDiscovery.network) results.push(setupDiscovery.network.error ||
    ((setupDiscovery.network.candidates || []).length + ' network device(s) found'));
  if (setupDiscovery.serial) results.push(setupDiscovery.serial.error ||
    ((setupDiscovery.serial.candidates || []).length + ' serial device(s) found'));
  if (results.length) setupDiscoveryText(card, results.join('\n'));
  setupNavigation(card, {scan: {label: 'Scan devices', run: async () => {
    captureSetupFields();
    if (setupDraft.panels.has('amp') && setupDraft.amp.mode === 'serial') {
      await runSetupDiscovery('serial');
    }
    if (setupDraft.panels.has('tv') || (setupDraft.panels.has('amp') &&
        setupDraft.dac.mode === 'itach')) await runSetupDiscovery('network');
    if (setupDraft.panels.has('amp') && (setupDraft.amp.mode === 'roon' ||
        setupDraft.dac.mode === 'roon')) await runSetupDiscovery('music');
    if (setupDraft.panels.has('mini')) await runSetupDiscovery('mini');
  }}});
}

async function configureSetupAgent() {
  captureSetupFields();
  const profile = setupDraft.agent_profile;
  setupDiscovery.agent = {pending: true};
  renderSetup();
  try {
    if (setupDraft.agent_credential) {
      const credentialResponse = await fetchT('/api/settings/agent/credential', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({profile, credential: setupDraft.agent_credential}),
      }, 15000);
      const credential = await credentialResponse.json().catch(() => ({}));
      if (!credentialResponse.ok) throw new Error(
        credential.detail || 'Could not save provider credential');
      setupDraft.agent_credential = '';
      const configured = (setupManifest.agent_profiles || []).find(
        (item) => item.id === profile);
      if (configured) configured.credential = credential.credential;
    }
    const response = await fetchT('/api/settings/agent/test', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({profile}),
    }, 300000);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || 'Ask connection test failed');
    setupDiscovery.agent = data;
  } catch (err) {
    setupDiscovery.agent = {error: err.message || 'Ask connection test failed'};
  }
  renderSetup();
}

async function prepareSetupVoice() {
  setupDiscovery.voice = {pending: true};
  renderSetup();
  try {
    const response = await fetchT('/api/setup/voice', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: 'prepare'}),
    }, 900000);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || 'Voice preparation failed');
    setupDiscovery.voice = data;
    setupManifest.voice = data;
  } catch (err) {
    setupDiscovery.voice = {error: true, ready: false,
      detail: err.message || 'Voice preparation failed'};
  }
  renderSetup();
}

async function configureSetupMini() {
  setupDiscovery.mini = {pending: true};
  renderSetup();
  try {
    const response = await fetchT('/api/setup/mini', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: 'enable'}),
    }, 20000);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || 'Input helper setup failed');
    setupDiscovery.mini = data;
  } catch (err) {
    setupDiscovery.mini = {helper: {available: false, permission: false,
      message: err.message || 'Input helper setup failed'}};
  }
  renderSetup();
}

function renderAccessSetup(card) {
  setupHeader(card, 'Connect privately',
    'Keep Core on loopback and let Tailscale Serve provide HTTPS and identity. The app never stores a Tailscale auth key.');
  const coreFields = document.createElement('div');
  coreFields.className = 'setup-fields';
  coreFields.append(setupField('Core listening port', 'setup-core-port',
    setupDraft.server.port, '8000', 'number'));
  card.append(coreFields);
  setupDiscoveryText(card,
    'Core listens only on 127.0.0.1. Choose an available port from 1024 to 65535; Tailscale publishes it securely.');
  const status = setupDiscovery.access?.status;
  setupDiscoveryText(card, status ? [
    status.installed ? 'Tailscale installed' : 'Tailscale not installed',
    status.online ? 'Core is on the tailnet' : 'Core is offline',
    status.serve ? 'Serve is enabled' : 'Serve still needs setup',
    status.url || status.detail || '',
  ].filter(Boolean).join('\n') :
    'Install or sign in to Tailscale on this Mac, enable Serve, then check again. Your iPhone/iPad must join the same tailnet.');
  const accessActions = document.createElement('div'); accessActions.className = 'setup-actions';
  if (!status?.installed) {
    const install = document.createElement('a'); install.className = 'setting-secondary';
    install.href = 'https://tailscale.com/download/mac'; install.target = '_blank';
    install.rel = 'noopener'; install.textContent = 'Install Tailscale';
    accessActions.append(install);
  } else if (status.online && !status.serve) {
    accessActions.append(setupButton('Enable Tailscale Serve', 'setting-secondary',
      enableTailscaleServe));
  }
  if (status?.url) {
    const address = document.createElement('div'); address.className = 'setup-phone-address';
    address.textContent = 'Enter in the phone app: ' + status.url;
    card.append(address);
  }
  if (accessActions.childElementCount) card.append(accessActions);

  const appleTitle = document.createElement('h3');
  appleTitle.className = 'setup-section-title';
  appleTitle.textContent = 'Apple services';
  card.append(appleTitle);
  const options = document.createElement('div');
  options.className = 'setup-options';
  (setupManifest.apple_services || []).forEach((option) => {
    options.append(setupChoice(option.id, option.label, option.detail,
      setupDraft.apple_services.mode === option.id, () => {
        captureSetupFields();
        setupDraft.apple_services.mode = option.id;
        renderSetup();
      }));
  });
  card.append(options);
  if (setupDraft.apple_services.mode === 'managed') {
    const fields = document.createElement('div');
    fields.className = 'setup-fields';
    fields.append(
      setupField('Broker HTTPS address', 'setup-broker-url',
        setupDraft.apple_services.url, 'https://broker.example.com'),
      setupField('One-time passphrase', 'setup-broker-invite',
        setupDraft.apple_services.invite, 'Provided by the app publisher', 'password'));
    card.append(fields);
    const pairActions = document.createElement('div');
    pairActions.className = 'setup-actions';
    const pair = setupButton(
      setupDraft.apple_services.enrolled ? 'Pair again' : 'Pair Apple services',
      'setting-secondary', pairSetupAppleServices);
    pairActions.append(pair);
    card.append(pairActions);
    const broker = setupDiscovery.apple_services;
    setupDiscoveryText(card,
      broker?.error || (setupDraft.apple_services.enrolled
        ? 'Paired. This Core can request MusicKit tokens and relay notifications; no publisher key was downloaded.'
        : 'Enter the address and one-time passphrase from the publisher. Pairing creates a private key on this Core.'),
      broker?.error ? 'setup-error' :
        (setupDraft.apple_services.enrolled ? 'setup-success' : ''));
  }
  setupNavigation(card, {scan: {label: 'Check Tailscale', run: () => runSetupDiscovery('access')}});
}

async function enableTailscaleServe() {
  captureSetupFields();
  try {
    const response = await fetchT('/api/setup/access', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: 'enable_tailscale_serve',
        port: setupManifest.installation?.port || 8000}),
    }, 20000);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || 'Could not enable Tailscale Serve');
    setupDiscovery.access = {status: data};
  } catch (err) {
    setupDiscovery.access = {status: {detail: err.message || 'Could not enable Serve'}};
  }
  renderSetup();
}

async function pairSetupAppleServices() {
  captureSetupFields();
  const apple = setupDraft.apple_services;
  setupDiscovery.apple_services = {error: ''};
  renderSetup();
  try {
    const response = await fetchT('/api/setup/apple-services', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: 'enroll', url: apple.url,
        invite: apple.invite, label: 'avctl Core'}),
    }, 20000);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || 'Could not pair Apple services');
    setupDraft.apple_services = Object.assign({}, apple, data, {invite: ''});
    setupDiscovery.apple_services = data;
  } catch (err) {
    setupDiscovery.apple_services = {error: err.message || 'Could not pair Apple services'};
  }
  renderSetup();
}

function setupPayload() {
  const order = (setupManifest.panels || []).map((panel) => panel.id);
  const panels = order.filter((id) => id === 'home' || setupDraft.panels.has(id));
  const payload = {server: setupDraft.server, music: setupDraft.music, panels,
    max_volume: setupDraft.max_volume, scene_volume: setupDraft.scene_volume,
    apple_services: {mode: setupDraft.apple_services.mode},
    voice: {enabled: setupDraft.panels.has('agent') && setupDraft.voice.enabled}};
  if (setupDraft.music === 'roon' || (setupDraft.panels.has('amp') &&
      (setupDraft.amp.mode === 'roon' || setupDraft.dac.mode === 'roon'))) {
    payload.roon = setupDraft.roon;
  }
  if (setupDraft.panels.has('tv')) payload.tv = setupDraft.tv;
  if (setupDraft.panels.has('amp')) {
    payload.amp = setupDraft.amp;
    payload.dac = setupDraft.dac;
  }
  return payload;
}

function renderReviewSetup(card) {
  setupHeader(card, 'Review and save',
    'Configuration is written atomically. Restart Core once so every driver opens with the new setup.');
  const summary = document.createElement('div'); summary.className = 'setup-summary';
  const rows = [
    ['Core port', setupDraft.server.port],
    ['Music', setupDraft.music === 'roon' ? 'Roon + Qobuz' : 'Apple Music'],
    ['Panels', (setupManifest.panels || []).filter((panel) =>
      panel.required || setupDraft.panels.has(panel.id)).map((panel) => panel.label).join(', ')],
    ['TV', setupDraft.panels.has('tv') ? (setupDraft.tv.host || 'Needs host') : 'Skipped'],
    ['Amplifier', setupDraft.panels.has('amp') ? setupDraft.amp.mode : 'Skipped'],
    ['Ask', setupDraft.panels.has('agent') ?
      (setupDraft.agent_profile || 'Current provider') : 'Skipped'],
    ['Ask voice', setupDraft.panels.has('agent') && setupDraft.voice.enabled
      ? ((setupDiscovery.voice || setupManifest.voice || {}).ready
        ? 'Local transcription ready' : 'Needs preparation') : 'Text only'],
    ['Apple services', setupDraft.apple_services.mode === 'managed'
      ? (setupDraft.apple_services.enrolled ? 'Broker paired' : 'Needs pairing')
      : setupDraft.apple_services.mode],
    ['Remote access', setupDiscovery.access?.status?.url || 'Can be completed later'],
  ];
  rows.forEach(([label, value]) => {
    const row = document.createElement('div'); row.className = 'setup-summary-row';
    const name = document.createElement('span'); name.textContent = label;
    const answer = document.createElement('b'); answer.textContent = value;
    row.append(name, answer); summary.append(row);
  });
  card.append(summary);
  setupNavigation(card, {finish: true});
}

async function activateSetup() {
  captureSetupFields();
  if (setupDraft.panels.has('agent')) {
    const selected = (setupManifest.agent_profiles || []).find(
      (profile) => profile.id === setupDraft.agent_profile);
    if (!selected) {
      setupDiscoveryText($('.setup-card.on'),
        'Choose an Ask provider before saving.', 'setup-error');
      return;
    }
    if (selected.credential === 'missing') {
      setupDiscoveryText($('.setup-card.on'),
        'Save the Ask provider credential before saving setup.', 'setup-error');
      return;
    }
    const voice = setupDiscovery.voice || setupManifest.voice || {};
    if (setupDraft.voice.enabled && !voice.ready) {
      setupDiscoveryText($('.setup-card.on'),
        'Prepare and verify local voice transcription, or choose text-only Ask.',
        'setup-error');
      return;
    }
  }
  const button = $('.setup-card.on .setting-primary');
  if (button) button.disabled = true;
  setupDiscoveryText($('.setup-card.on'), 'Saving and validating…');
  try {
    const configResponse = await fetchT('/api/setup/activate', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(setupPayload()),
    }, 15000);
    const config = await configResponse.json().catch(() => ({}));
    if (!configResponse.ok) throw new Error(config.detail || 'Could not save setup');
    const order = (setupManifest.panels || []).map((panel) => panel.id);
    const enabled = order.filter((id) => id === 'home' || setupDraft.panels.has(id));
    const panelResponse = await fetchT('/api/settings/panels', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({order, enabled}),
    }, 15000);
    const panels = await panelResponse.json().catch(() => ({}));
    if (!panelResponse.ok) throw new Error(panels.detail || 'Could not save panels');
    if (setupDraft.panels.has('agent') && setupDraft.agent_profile) {
      const agentResponse = await fetchT('/api/settings/agent', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({profile: setupDraft.agent_profile}),
      }, 15000);
      const agent = await agentResponse.json().catch(() => ({}));
      if (!agentResponse.ok) throw new Error(agent.detail || 'Could not save Ask provider');
    }
    window.webkit?.messageHandlers?.avctl?.postMessage({event: 'setup-complete'});
    if (setupManifest.installation?.install_kind === 'package') {
      setupDiscoveryText($('.setup-card.on'),
        'Saved. Restarting the packaged Core with the configured drivers…', 'setup-success');
      const restart = await fetchT('/api/setup/restart', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}',
      }, 5000);
      if (!restart.ok) throw new Error('Configuration saved, but Core could not restart');
      setTimeout(() => {
        if (['127.0.0.1', 'localhost'].includes(location.hostname)) {
          const target = new URL(location.href);
          target.port = String(setupDraft.server.port || 8000);
          target.pathname = '/';
          target.search = '?setup=1';
          location.assign(target);
        } else location.reload();
      }, 2500);
    } else {
      setupDiscoveryText($('.setup-card.on'),
        'Saved. Restart avctl Core, then return here to run device checks.', 'setup-success');
    }
  } catch (err) {
    setupDiscoveryText($('.setup-card.on'), err.message || 'Could not save setup', 'setup-error');
    if (button) button.disabled = false;
  }
}

function renderSetup() {
  if (!setupRoot || !setupManifest) return;
  setupRoot.replaceChildren();
  const progress = document.createElement('div'); progress.className = 'setup-progress';
  setupStepNames.forEach((name, index) => {
    const segment = document.createElement('span');
    segment.classList.toggle('on', index <= setupStep); segment.title = name;
    progress.append(segment);
  });
  const card = document.createElement('section'); card.className = 'setup-card on';
  setupRoot.append(progress, card);
  [renderInstallSetup, renderMusicSetup, renderPanelSetup, renderDeviceSetup,
    renderAccessSetup, renderReviewSetup][setupStep](card);
}

async function loadSetup(force = false) {
  if (!setupRoot || (setupLoaded && !force)) return;
  setupRoot.textContent = 'Reading this Core…';
  try {
    const [setupResponse, panelsResponse, agentResponse] = await Promise.all([
      fetchT('/api/setup', null, 15000),
      fetchT('/api/settings/panels', null, 15000),
      fetchT('/api/settings/agent', null, 15000),
    ]);
    const data = await setupResponse.json().catch(() => ({}));
    const panels = await panelsResponse.json().catch(() => ({}));
    const agent = await agentResponse.json().catch(() => ({}));
    if (!setupResponse.ok) throw new Error(data.detail || 'Could not load setup');
    setupManifest = data;
    setupDraft.server.port = data.installation?.port || setupDraft.server.port;
    setupDraft.apple_services = Object.assign({}, setupDraft.apple_services,
      data.apple_broker || {}, {invite: ''});
    if (data.current) {
      setupDraft.server = Object.assign({}, setupDraft.server, data.current.server || {});
      setupDraft.music = data.current.music || setupDraft.music;
      setupDraft.roon = Object.assign({}, setupDraft.roon, data.current.roon || {});
      setupDraft.tv = Object.assign({}, setupDraft.tv, data.current.tv || {});
      setupDraft.amp = Object.assign({}, setupDraft.amp, data.current.amp || {});
      setupDraft.dac = Object.assign({}, setupDraft.dac, data.current.dac || {});
      setupDraft.max_volume = Object.assign({}, setupDraft.max_volume,
        data.current.max_volume || {});
      setupDraft.scene_volume = Object.assign({}, setupDraft.scene_volume,
        data.current.scene_volume || {});
      setupDraft.apple_services = Object.assign({}, setupDraft.apple_services,
        data.current.apple_services || {});
      setupDraft.voice = Object.assign({}, setupDraft.voice,
        data.current.voice || {});
      setupDraft.panels = new Set(data.current.panels || ['home']);
    }
    setupManifest.agent_profiles = agentResponse.ok ? (agent.profiles || []) : [];
    setupDraft.agent_profile = agentResponse.ok ? String(agent.active_profile || '') : '';
    if (panelsResponse.ok) {
      setupDraft.panels = new Set((panels.panels || [])
        .filter((panel) => panel.enabled).map((panel) => panel.id));
      setupDraft.panels.add('home');
    }
    setupLoaded = true;
    renderSetup();
  } catch (err) {
    setupRoot.textContent = err.message || 'Could not load setup';
    setupRoot.classList.add('setup-error');
  }
}

function panelFingerprint() {
  return JSON.stringify(panelDraft.map((panel) => [panel.id, panel.enabled]));
}

function syncPanelApply() {
  const apply = $('#panel-settings-apply');
  if (!apply) return;
  apply.disabled = panelSettingsManaged || !panelSettingsLoaded ||
    panelFingerprint() === savedPanelFingerprint;
}

function renderPanelSettings() {
  panelSettingList.replaceChildren();
  panelDraft.forEach((panel, index) => {
    const row = document.createElement('div');
    row.className = 'panel-setting-row';
    row.classList.toggle('panel-disabled', !panel.enabled);

    const identity = document.createElement('span');
    identity.className = 'panel-setting-identity';
    const glyph = document.createElement('span');
    glyph.className = 'panel-setting-glyph';
    glyph.textContent = panel.glyph || '·';
    glyph.setAttribute('aria-hidden', 'true');
    const copy = document.createElement('span');
    copy.className = 'panel-setting-copy';
    const title = document.createElement('b');
    title.textContent = panel.label;
    const status = document.createElement('small');
    status.textContent = panel.locked ? 'Always available' :
      (panel.enabled ? 'Shown in navigation' : 'Hidden');
    copy.append(title, status);
    identity.append(glyph, copy);

    const toggleLabel = document.createElement('label');
    toggleLabel.className = 'panel-setting-toggle';
    toggleLabel.setAttribute('aria-label', 'Show ' + panel.label);
    const toggle = document.createElement('input');
    toggle.type = 'checkbox';
    toggle.checked = panel.enabled;
    toggle.disabled = panel.locked || panelSettingsManaged;
    const toggleFace = document.createElement('span');
    toggleFace.setAttribute('aria-hidden', 'true');
    toggle.addEventListener('change', () => {
      panel.enabled = toggle.checked;
      renderPanelSettings();
    });
    toggleLabel.append(toggle, toggleFace);

    const moves = document.createElement('span');
    moves.className = 'panel-setting-moves';
    [['↑', -1, 'Move up'], ['↓', 1, 'Move down']].forEach(
      ([symbol, delta, label]) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = symbol;
        button.title = label;
        button.setAttribute('aria-label', label + ' ' + panel.label);
        button.disabled = panelSettingsManaged ||
          (delta < 0 ? index === 0 : index === panelDraft.length - 1);
        button.addEventListener('click', () => {
          const target = index + delta;
          [panelDraft[index], panelDraft[target]] =
            [panelDraft[target], panelDraft[index]];
          renderPanelSettings();
        });
        moves.append(button);
      });

    row.append(identity, toggleLabel, moves);
    panelSettingList.append(row);
  });
  const visible = panelDraft.filter((panel) => panel.enabled).length;
  panelSettingsState.textContent = panelSettingsManaged
    ? 'Managed by environment' : visible + ' visible';
  syncPanelApply();
}

async function loadPanelSettings(force = false) {
  if (panelSettingsLoaded && !force) return;
  panelSettingsState.textContent = 'Loading…';
  try {
    const response = await fetchT('/api/settings/panels', null, 15000);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || 'Could not load panels');
    panelSettingsManaged = Boolean(data.managed);
    panelDraft = (Array.isArray(data.panels) ? data.panels : []).map(
      (panel) => ({
        id: String(panel.id), label: String(panel.label),
        glyph: String(panel.glyph || ''), enabled: Boolean(panel.enabled),
        locked: Boolean(panel.locked),
      }));
    panelSettingsLoaded = true;
    savedPanelFingerprint = panelFingerprint();
    renderPanelSettings();
  } catch (err) {
    panelSettingsState.textContent = err.message || 'Could not load panels';
  }
}

$('#panel-settings-apply')?.addEventListener('click', async () => {
  if (!panelSettingsLoaded || panelSettingsManaged) return;
  const apply = $('#panel-settings-apply');
  apply.disabled = true;
  panelSettingsState.textContent = 'Saving…';
  const enabled = panelDraft.filter((panel) => panel.enabled)
    .map((panel) => panel.id);
  try {
    const response = await fetchT('/api/settings/panels', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', Accept: 'application/json'},
      body: JSON.stringify({
        order: panelDraft.map((panel) => panel.id), enabled,
      }),
    }, 15000);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || 'Could not save panels');
    const current = location.hash.replace('#', '') || 'home';
    if (!enabled.includes(current)) history.replaceState(null, '', '#home');
    location.reload();
  } catch (err) {
    panelSettingsState.textContent = err.message || 'Could not save panels';
    syncPanelApply();
  }
});

function syncMusicBackendSelection() {
  $$('.music-backend-profile').forEach((button) => {
    const selected = button.dataset.backend === selectedMusicBackend;
    button.classList.toggle('on', selected);
    button.setAttribute('aria-pressed', String(selected));
    const current = $('.provider-current', button);
    if (current) current.textContent =
      button.dataset.backend === activeMusicBackend ? 'Active' : '';
  });
  const apply = $('#music-backend-apply');
  if (apply) apply.disabled = musicBackendManaged || !selectedMusicBackend ||
    selectedMusicBackend === activeMusicBackend;
}

function renderMusicBackends(backends) {
  musicBackends.replaceChildren();
  backends.forEach((backend) => {
    const button = document.createElement('button');
    button.className = 'provider-profile music-backend-profile';
    button.dataset.backend = backend.id;
    const radio = document.createElement('span');
    radio.className = 'provider-radio';
    radio.setAttribute('aria-hidden', 'true');
    const copy = document.createElement('span');
    copy.className = 'provider-copy';
    const title = document.createElement('b');
    title.textContent = backend.label;
    const detail = document.createElement('small');
    detail.textContent = backend.detail;
    const facts = document.createElement('small');
    facts.className = 'provider-facts';
    facts.textContent = backend.driver;
    copy.append(title, detail, facts);
    const current = document.createElement('span');
    current.className = 'provider-current';
    button.append(radio, copy, current);
    button.addEventListener('click', () => {
      selectedMusicBackend = backend.id;
      syncMusicBackendSelection();
    });
    musicBackends.append(button);
  });
  syncMusicBackendSelection();
}

async function loadMusicBackendSettings(force = false) {
  if (musicBackendLoaded && !force) return;
  musicBackendState.textContent = 'Loading…';
  try {
    const response = await fetchT('/api/settings/music', null, 15000);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || 'Could not load music backends');
    activeMusicBackend = String(data.active_backend || '');
    selectedMusicBackend = activeMusicBackend;
    musicBackendManaged = Boolean(data.managed);
    renderMusicBackends(Array.isArray(data.backends) ? data.backends : []);
    musicBackendLoaded = true;
    musicBackendState.textContent = musicBackendManaged
      ? 'Managed by environment' : 'Active';
  } catch (err) {
    musicBackendState.textContent = err.message || 'Could not load music backends';
  }
}

$('#music-backend-apply')?.addEventListener('click', async () => {
  if (!selectedMusicBackend || musicBackendManaged) return;
  const apply = $('#music-backend-apply');
  apply.disabled = true;
  musicBackendState.textContent = 'Switching…';
  try {
    const response = await fetchT('/api/settings/music', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', Accept: 'application/json'},
      body: JSON.stringify({backend: selectedMusicBackend}),
    }, 30000);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || 'Could not switch backend');
    activeMusicBackend = String(data.active_backend || selectedMusicBackend);
    musicBackendState.textContent = 'Switched';
    syncMusicBackendSelection();
    location.reload();
  } catch (err) {
    musicBackendState.textContent = err.message || 'Could not switch backend';
    syncMusicBackendSelection();
  }
});

function renderProviderProfiles(profiles) {
  providerProfiles.replaceChildren();
  profiles.forEach((profile) => {
    const button = document.createElement('button');
    button.className = 'provider-profile';
    button.dataset.profile = profile.id;
    const radio = document.createElement('span');
    radio.className = 'provider-radio';
    radio.setAttribute('aria-hidden', 'true');
    const copy = document.createElement('span');
    copy.className = 'provider-copy';
    const title = document.createElement('b');
    title.textContent = profile.label;
    const detail = document.createElement('small');
    detail.textContent = profile.model;
    const facts = document.createElement('small');
    facts.className = 'provider-facts';
    facts.textContent = [profile.driver, profile.service_tier,
      'credential: ' + profile.credential].filter(Boolean).join(' · ');
    copy.append(title, detail, facts);
    const current = document.createElement('span');
    current.className = 'provider-current';
    current.textContent = profile.id === activeProviderProfile ? 'Active' : '';
    button.append(radio, copy, current);
    button.addEventListener('click', () => {
      selectedProviderProfile = profile.id;
      syncProviderSelection();
    });
    providerProfiles.append(button);
  });
  syncProviderSelection();
}

function syncProviderSelection() {
  $$('.provider-profile').forEach((button) => {
    const selected = button.dataset.profile === selectedProviderProfile;
    button.classList.toggle('on', selected);
    button.setAttribute('aria-pressed', String(selected));
    const current = $('.provider-current', button);
    if (current) current.textContent =
      button.dataset.profile === activeProviderProfile ? 'Active' : '';
  });
  const apply = $('#provider-apply');
  if (apply) apply.disabled = providerManaged || !selectedProviderProfile ||
    selectedProviderProfile === activeProviderProfile;
  const test = $('#provider-test');
  if (test) test.disabled = !selectedProviderProfile;
}

async function loadProviderSettings(force = false) {
  if (providerSettingsLoaded && !force) return;
  providerState.textContent = 'Loading…';
  try {
    const response = await fetchT('/api/settings/agent', null, 15000);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || 'Could not load profiles');
    activeProviderProfile = String(data.active_profile || '');
    selectedProviderProfile = activeProviderProfile;
    providerManaged = Boolean(data.managed);
    renderProviderProfiles(Array.isArray(data.profiles) ? data.profiles : []);
    providerSettingsLoaded = true;
    providerState.textContent = providerManaged ? 'Managed by environment' :
      (activeProviderProfile ? 'Active · ' + activeProviderProfile : 'Unavailable');
  } catch (err) {
    providerState.textContent = err.message || 'Could not load profiles';
  }
}

$('#provider-apply')?.addEventListener('click', async () => {
  if (!selectedProviderProfile || providerManaged) return;
  const button = $('#provider-apply');
  button.disabled = true;
  providerState.textContent = 'Applying…';
  try {
    const response = await fetchT('/api/settings/agent', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', Accept: 'application/json'},
      body: JSON.stringify({profile: selectedProviderProfile}),
    }, 15000);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || 'Could not switch provider');
    activeProviderProfile = String(data.active_profile || selectedProviderProfile);
    providerState.textContent = 'Active · ' + activeProviderProfile;
    syncProviderSelection();
    toast('Ask provider', 'The next Ask turn will use ' + activeProviderProfile + '.');
  } catch (err) {
    providerState.textContent = err.message || 'Could not switch provider';
    syncProviderSelection();
  }
});

$('#provider-test')?.addEventListener('click', async () => {
  if (!selectedProviderProfile) return;
  const button = $('#provider-test');
  button.disabled = true;
  providerState.textContent = 'Testing tool calls…';
  try {
    const response = await fetchT('/api/settings/agent/test', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', Accept: 'application/json'},
      body: JSON.stringify({profile: selectedProviderProfile}),
    }, 300000);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || 'Connection test failed');
    providerState.textContent = 'Tools verified · ' +
      Number(data.latency_ms || 0).toLocaleString() + ' ms';
  } catch (err) {
    providerState.textContent = err.message || 'Connection test failed';
  } finally {
    syncProviderSelection();
  }
});

function nativeSettingsBridge() {
  return window.webkit?.messageHandlers?.avctl || null;
}

function requestNativeSettings() {
  const bridge = nativeSettingsBridge();
  const server = $('#native-server');
  if (!bridge) {
    server.value = location.origin;
    server.disabled = true;
    $('#native-token').disabled = true;
    $('#native-token-clear').disabled = true;
    $('#native-settings-save').disabled = true;
    $('#native-cover-warm').disabled = true;
    $('#native-settings-state').textContent =
      'Safari uses the address currently open in this browser.';
    return;
  }
  bridge.postMessage({event: 'settings-request'});
}

window.avctlNativeSettings = function avctlNativeSettings(event) {
  const state = $('#native-settings-state');
  if (event?.error) {
    state.textContent = String(event.error);
    return;
  }
  const server = $('#native-server');
  server.disabled = false;
  server.value = String(event?.server || '');
  const token = $('#native-token');
  token.disabled = false;
  token.value = '';
  token.placeholder = event?.token_configured
    ? 'Configured · leave blank to keep it'
    : 'Empty when the tailnet authenticates';
  $('#native-token-clear').disabled = !event?.token_configured;
  $('#native-settings-save').disabled = false;
  $('#native-cover-warm').disabled = false;
  $('#native-cover-count').textContent = String(event?.cover_count ?? '--');
  $('#native-cover-note').textContent = String(event?.cover_note ||
    'The island can only draw artwork already on this device.');
  state.textContent = event?.message || 'Connection settings live on this device.';
};

$('#native-settings-save')?.addEventListener('click', () => {
  const bridge = nativeSettingsBridge();
  if (!bridge) return;
  const server = $('#native-server').value.trim();
  if (!/^https?:\/\//i.test(server)) {
    $('#native-settings-state').textContent =
      'Use a complete http:// or https:// address.';
    return;
  }
  $('#native-settings-state').textContent = 'Saving…';
  bridge.postMessage({
    event: 'settings-save', server,
    token: $('#native-token').value.trim(), clear_token: false,
  });
});

$('#native-token-clear')?.addEventListener('click', () => {
  const bridge = nativeSettingsBridge();
  if (!bridge) return;
  $('#native-settings-state').textContent = 'Clearing token…';
  bridge.postMessage({
    event: 'settings-save', server: $('#native-server').value.trim(),
    token: '', clear_token: true,
  });
});

$('#native-cover-warm')?.addEventListener('click', () => {
  const bridge = nativeSettingsBridge();
  if (!bridge) return;
  $('#native-cover-note').textContent = 'Warming the artwork cache…';
  bridge.postMessage({event: 'artwork-warm'});
});

// ---- state --------------------------------------------------------------

// fetch with a deadline. Without one, a request into a dead tailnet hangs
// for minutes holding the busy LED on -- the phone must find out it is
// talking to nobody, and say so, on a human timescale.
function fetchT(url, opts, ms) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), ms || 15000);
  return fetch(url, Object.assign({}, opts || {}, { signal: ctl.signal }))
    .finally(() => clearTimeout(timer));
}

// `--` rather than a zero or a guess: an unknown volume and a volume of 0 are
// very different things to be told while standing in front of an amplifier.
const UNKNOWN = '--';

function fmtBool(value, yes, no) {
  if (value === null || value === undefined) return UNKNOWN;
  return value ? yes : no;
}

function paint() {
  if (!snapshot) return;
  const d = snapshot.devices;

  // The scene is inferred from the rack, never remembered from a button.
  // "music" is a claim about the whole chain being ready -- TV on, amp on,
  // DAC on AND passing USB -- so a half-woken rack says so instead of
  // pretending. Everything down is "off"; anything else has no name worth
  // inventing.
  const dacFields = d.dac.fields;
  const musicReady = d.tv.power === true && d.amp.power === true
    && d.dac.power === true && dacFields.input === 'usb';
  const allOff = d.tv.power === false && d.amp.power === false
    && d.dac.power === false;
  $('#scene').textContent = musicReady ? 'music' : (allOff ? 'off' : UNKNOWN);

  const tv = d.tv.fields;
  const music = d.music.fields;
  setRo('tv', tv.input || UNKNOWN, true);
  setRo('amp', d.amp.fields.volume === null
    ? UNKNOWN
    : d.amp.fields.volume + (d.amp.fields.muted ? '% MUTE' : '%'), true);
  // The mini's own output volume -- the level the music panel's knob rides.
  setRo('mac', music.muted
    ? 'MUTE'
    : (music.volume === null || music.volume === undefined
        ? UNKNOWN : music.volume + '%'), true);
  // The DAC: which input it passes, or OFF when we believe it is down.
  // Printed plainly, no ~ -- the value is bookkeeping rather than a
  // readback, but the screen is for reading at a glance and a tilde on the
  // one line that is always right in practice was just noise. The doubt
  // still lives where it can be acted on: Resync, on the DAC / Amp tab.
  setRo('dac', d.dac.power === false
    ? 'OFF'
    : (dacFields.input ? dacFields.input.toUpperCase() : UNKNOWN), true);

  // The volume number appears on both the home and the amp panel.
  const vol = d.amp.fields.volume;
  $$('.js-vol').forEach((el) => { el.textContent = vol === null ? UNKNOWN : String(vol); });
  const slider = $('#amp-slider');
  if (slider && vol !== null && document.activeElement !== slider) {
    slider.value = vol;
  }

  // Selection rings, from state rather than from whichever button was pressed
  // last -- the physical remotes exist and we are not the only thing driving
  // this rack.
  markSelected('tv.input.', tv.input);
  markSelected('amp.input.', d.amp.fields.input);
  markSelected('dac.input.', d.dac.fields.input);
  // The DAC's line says what we believe and admits it is a belief.
  const dacDetail = $('#dac-detail');
  if (dacDetail) {
    dacDetail.textContent = d.dac.fields.input
      ? d.dac.fields.input.toUpperCase() + ' \u00b7 ' + d.dac.detail
      : d.dac.detail;
  }

  $$('.dev-detail').forEach((el) => {
    const dev = d[el.dataset.dev];
    el.textContent = dev ? dev.detail : '';
  });

  paintMusic(d.music);

}

function setRo(id, text, trusted) {
  const el = $('#ro-' + id);
  el.textContent = trusted ? text : (text === UNKNOWN ? text : '~' + text);
  el.classList.toggle('guess', !trusted);
}

function markSelected(prefix, value) {
  $$('[data-cmd^="' + prefix + '"]').forEach((el) => {
    el.classList.toggle('sel', Boolean(value) && el.dataset.cmd === prefix + value);
  });
}

function markComingSoon() {
  $$('[data-cmd]').forEach((el) => {
    el.classList.toggle('soon', !implemented.has(el.dataset.cmd));
  });
}

// The poller's sequence number of the last applied snapshot. A slow
// /api/state response that left the server before a change must not land
// after the SSE event that carried it and repaint the old values (#105).
let lastSeq = 0;

function takeSnapshot(data) {
  if (data.seq) {
    if (data.seq <= lastSeq) return;   // stale: something newer already painted
    lastSeq = data.seq;
  }
  snapshot = data;
  implemented = new Set(snapshot.implemented || []);
  markComingSoon();
  paint();
}

async function refresh() {
  busy(1);
  try {
    const r = await fetchT('/api/state', { headers: { Accept: 'application/json' } });
    if (!r.ok) {
      led.classList.add('bad');
      toast(null, r.status === 401 ? 'session expired -- reload'
        : 'state error ' + r.status);
      return;
    }
    led.classList.remove('bad');
    takeSnapshot(await r.json());
  } catch (err) {
    led.classList.add('bad');
  } finally {
    busy(-1);
  }
}

// ---- commands -----------------------------------------------------------

async function send(cmd, args) {
  busy(1);
  try {
    // 90s deadline: a scene legitimately runs to ~60s of TV wake, and the
    // point is to catch the request that will NEVER answer, not a slow one.
    const r = await fetchT('/api/cmd', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ cmd: cmd, args: args || {} }),
    }, 90000);
    const data = await r.json().catch(() => ({}));
    const label = data.label || cmd;

    if (r.status === 501) {
      // The normal answer today. The note is the reason this one is not just
      // a matter of writing the handler.
      toast('coming soon', label + (data.note ? ' -- ' + data.note : ''));
      return false;
    }
    if (!r.ok) {
      toast('failed', label + ' -- ' + (data.detail || 'error ' + r.status));
      return false;
    }
    if (data.message) toast(null, data.message);
    if (data.state) {
      // A command that carries its own snapshot (resync). It has no seq and
      // an empty implemented stub -- keep the set the page already knows.
      takeSnapshot(Object.assign({}, data.state, {
        implemented: (snapshot && snapshot.implemented) || [],
      }));
    } else {
      refresh();
    }
    return true;
  } catch (err) {
    toast('failed', err && err.name === 'AbortError'
      ? 'no answer from the mini' : 'mini unreachable');
    return false;
  } finally {
    busy(-1);
  }
}

// One listener for the whole document. Every button carries its own id, so
// there is no per-panel wiring to keep in step with the markup.
document.addEventListener('click', (event) => {
  const el = event.target.closest('[data-cmd]');
  if (!el) return;
  // A hold has already been firing this button; the click that ends it is not
  // a separate press.
  if (el.dataset.repeat === 'fired') return;
  send(el.dataset.cmd, JSON.parse(el.dataset.args || '{}'));
});

// Press-and-hold repeats, for volume and for stepping the D900 round its
// cycle: seven taps to get from OPT1 back to USB is not a remote, it's a
// chore. First press fires immediately, then it accelerates.
// One hold at a time, kept in a single record rather than loose globals: a
// second touch used to overwrite the shared timer handle and orphan the
// first chain, which then repeated its command with no finger on the screen
// until the page was reloaded (#104).
let hold = null;   // { el, timer, pointerId, x, y }
function startHold(el, event) {
  const me = { el: el, timer: null, pointerId: event.pointerId,
               x: event.clientX, y: event.clientY };
  let delay = 420;
  const tick = () => {
    if (hold !== me) return;   // ended while the timeout was in flight
    send(el.dataset.cmd, JSON.parse(el.dataset.args || '{}'));
    el.dataset.repeat = 'fired';
    delay = Math.max(120, delay * 0.72);
    me.timer = setTimeout(tick, delay);
  };
  me.timer = setTimeout(tick, delay);
  hold = me;
}
function endHold() {
  if (!hold) return;
  clearTimeout(hold.timer);
  const el = hold.el;   // always the held element, never event.target's --
  hold = null;          // clearing the wrong one left a dead button behind
  setTimeout(() => delete el.dataset.repeat, 0);
}
document.addEventListener('pointerdown', (event) => {
  const el = event.target.closest('[data-repeat-ok]');
  if (!el) return;
  // A second finger takes over cleanly: the first chain is stopped through
  // its own record, so nothing is left ticking.
  endHold();
  startHold(el, event);
});
// A swipe between panels often starts on top of a button. Moving at all means
// the intent was the swipe, not a held volume key.
document.addEventListener('pointermove', (event) => {
  if (!hold || event.pointerId !== hold.pointerId) return;
  if (Math.abs(event.clientX - hold.x) > 8 ||
      Math.abs(event.clientY - hold.y) > 8) {
    endHold();
  }
}, { passive: true });
['pointerup', 'pointercancel', 'pointerleave'].forEach((type) => {
  document.addEventListener(type, (event) => {
    if (hold && event.pointerId === hold.pointerId) endHold();
  });
});

// Absolute volume. Sent on release only -- one RS-232 write per drag, not
// forty, and (VST) is a set rather than a nudge so intermediate values are
// noise.
const slider = $('#amp-slider');
if (slider) {
  slider.addEventListener('input', () => {
    $$('.js-vol').forEach((el) => { el.textContent = slider.value; });
  });
  slider.addEventListener('change', async () => {
    const ok = await send('amp.vol.set', { level: Number(slider.value) });
    if (!ok) paint();  // put the thumb back where the amp actually is
  });
}

// ---- the D900 resync sheet ----------------------------------------------
//
// The one control here that exists purely because a device cannot be asked
// anything. IR goes out and nothing comes back, so when a press is missed
// the user reads the front panel and tells us what is true.

const sheet = $('#sheet');
$('#dac-resync')?.addEventListener('click', () => sheet.classList.add('on'));
sheet.addEventListener('click', (event) => {
  if (event.target === sheet || event.target.id === 'sheet-cancel') {
    sheet.classList.remove('on');
  }
});
$$('#sheet [data-input]').forEach((el) => {
  el.addEventListener('click', () => sheet.classList.remove('on'));
});

// ---- the rail -----------------------------------------------------------
//
// Panels are laid out side by side and swiped between; CSS scroll snapping
// does the gesture, so the drag tracks the finger and there is no momentum
// maths here to get wrong. This code only keeps the tabs and the URL pointing
// at whichever panel the rail settled on.

const rail = $('#rail');
const railStack = $('#rail-stack');
const PAGES = $$('.page').map((el) => el.id.replace('page-', ''));

function indexOfPage(name) {
  const i = PAGES.indexOf(name);
  return i < 0 ? 0 : i;
}

function showPage(name, smooth) {
  // A saved/bookmarked hash may name a panel that was hidden in Settings.
  // Land on Home instead of showing Home with no selected navigation item.
  if (!PAGES.includes(name)) name = 'home';
  rail.scrollTo({
    left: indexOfPage(name) * rail.clientWidth,
    behavior: smooth ? 'smooth' : 'auto',
  });
  markTab(name);
}

function markTab(name) {
  if (name !== 'agent' && document.activeElement?.id === 'agent-input') {
    document.activeElement.blur();
  }
  $$('.tab').forEach((el) => el.classList.toggle('on', el.dataset.tab === name));
  $('.app').classList.toggle('music-current', name === 'music');
}

$$('.tab').forEach((el) => {
  el.addEventListener('click', (event) => {
    event.preventDefault();
    showPage(el.dataset.tab, true);
    settle(el.dataset.tab);
  });
});

// replaceState rather than location.hash: assigning the hash fires
// hashchange, which would scroll the rail again mid-swipe and fight the
// finger.
function settle(name) {
  markTab(name);
  setMiniActive(name === 'mini');
  if (location.hash !== '#' + name) history.replaceState(null, '', '#' + name);
  // The music grid is fetched the first time the panel is actually visited,
  // so the other panels keep their instant, network-free load.
  if (name === 'music' && !musicLoaded) loadRecent(false);
  if (name === 'music' && activeMusicLayout === 'split-deck') {
    // settle can run from an early scroll event while the rest of this defer
    // script is still initializing; hop once so queue state is ready.
    setTimeout(() => loadQueue(false), 0);
  }
}

let settleTimer = null;
rail.addEventListener('scroll', () => {
  clearTimeout(settleTimer);
  settleTimer = setTimeout(() => {
    const width = rail.clientWidth || 1;
    settle(PAGES[Math.round(rail.scrollLeft / width)] || 'home');
  }, 90);
}, { passive: true });

window.addEventListener('hashchange', () => {
  const name = location.hash.replace('#', '') || 'home';
  showPage(name, true);
  setMiniActive(name === 'mini');
});

// A rotated phone changes the panel width, and a rail measured at the old one
// lands between two panels.
window.addEventListener('resize', () => {
  showPage(location.hash.replace('#', '') || 'home', false);
});

showPage(location.hash.replace('#', '') || 'home', false);

// ---- Mac mini trackpad + keyboard --------------------------------------
// The browser owns gesture recognition; the helper receives only small,
// canonical events. Pointer movement is coalesced once per animation frame so
// a noisy touchscreen cannot turn into hundreds of network writes per frame.
const miniShell = $('#mini-shell');
const miniTrackpad = $('#mini-trackpad');
const miniState = $('#mini-state');
const miniInput = $('#mini-input');
const miniDone = $('#mini-keyboard-done');
let miniSocket = null;
let miniActive = false;
let miniReady = false;
let miniRetry = 800;
let miniRetryTimer = null;
let miniComposing = false;
const miniModifiers = new Set();

function miniSetStatus(kind, message) {
  if (!miniState) return;
  miniState.classList.toggle('ready', kind === 'ready');
  miniState.classList.toggle('bad', kind === 'bad');
  $('span', miniState).textContent = message;
}

function miniSocketURL() {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return protocol + '//' + location.host + '/api/mini/input';
}

function miniSend(event) {
  if (!miniReady || !miniSocket || miniSocket.readyState !== WebSocket.OPEN) {
    return false;
  }
  miniSocket.send(JSON.stringify(event));
  return true;
}

function miniClearModifiers(send = false) {
  if (send) {
    miniModifiers.forEach((code) => miniSend({ type: 'key', code, state: 'up' }));
  }
  miniModifiers.clear();
  $$('[data-mini-mod]').forEach((button) => {
    button.classList.remove('on');
    button.setAttribute('aria-pressed', 'false');
  });
}

function miniDisconnect() {
  clearTimeout(miniRetryTimer);
  miniRetryTimer = null;
  miniReady = false;
  miniClearModifiers(false);
  const socket = miniSocket;
  miniSocket = null;
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ type: 'release_all' }));
    socket.close(1000, 'panel inactive');
  } else if (socket && socket.readyState === WebSocket.CONNECTING) {
    socket.close();
  }
}

function miniScheduleReconnect() {
  if (!miniActive || document.hidden || miniRetryTimer) return;
  miniRetryTimer = setTimeout(() => {
    miniRetryTimer = null;
    miniConnect();
  }, miniRetry);
  miniRetry = Math.min(Math.round(miniRetry * 1.7), 8000);
}

function miniConnect() {
  if (!miniShell || !miniActive || document.hidden || miniSocket) return;
  miniSetStatus('waiting', 'Connecting…');
  const socket = new WebSocket(miniSocketURL());
  miniSocket = socket;
  socket.onmessage = (message) => {
    if (miniSocket !== socket) return;
    let status;
    try { status = JSON.parse(message.data); }
    catch (err) { return; }
    if (status.type !== 'status') return;
    const available = status.available === true;
    const permission = status.permission === true;
    miniReady = available && permission && status.busy !== true;
    if (miniReady) {
      miniRetry = 800;
      miniSetStatus('ready', 'Connected');
    } else {
      miniSetStatus('bad', String(status.message ||
        (available ? 'Accessibility permission required' : 'Helper unavailable')));
    }
  };
  socket.onerror = () => {
    if (miniSocket === socket) miniSetStatus('bad', 'Connection failed');
  };
  socket.onclose = () => {
    if (miniSocket !== socket) return;
    miniSocket = null;
    miniReady = false;
    miniClearModifiers(false);
    if (miniActive) {
      if (!miniState.classList.contains('bad')) {
        miniSetStatus('bad', 'Disconnected');
      }
      miniScheduleReconnect();
    }
  };
}

function setMiniActive(active) {
  miniActive = Boolean(active && miniShell);
  if (miniActive) miniConnect();
  else {
    miniInput?.blur();
    miniDisconnect();
  }
}

function miniButton(button, state, clicks = 1) {
  miniSend({ type: 'button', button, state, clicks });
}

function miniTapKey(code) {
  if (!miniSend({ type: 'key', code, state: 'down' })) return;
  miniSend({ type: 'key', code, state: 'up' });
}

function miniSendText(value) {
  // Dictation and paste can commit a paragraph in one event. Keep every wire
  // message within the server's bounded protocol without dropping the tail.
  const characters = Array.from(String(value || ''));
  for (let start = 0; start < characters.length; start += 128) {
    miniSend({ type: 'text', text: characters.slice(start, start + 128).join('') });
  }
}

if (miniTrackpad) {
  // A phone-sized surface drives a desktop-sized display. Keep fine motion
  // precise while giving a short phone gesture enough reach for a large Mac.
  const pointerGain = 3.0;
  const precisionPointerGain = 1.8;
  const pointerAcceleration = 0.12;
  const pointerAccelerationLimit = 1.8;
  const spaceSwipeThreshold = 42;
  const points = new Map();
  let gesture = 'idle';
  let gestureStarted = 0;
  let startX = 0;
  let startY = 0;
  let lastX = 0;
  let lastY = 0;
  let travelled = 0;
  let scrollTravelled = 0;
  let dragging = false;
  let secondTap = false;
  let lastTap = { at: 0, x: 0, y: 0 };
  let moveX = 0;
  let moveY = 0;
  let moveFrame = null;

  function flushMove() {
    moveFrame = null;
    if (moveX || moveY) {
      miniSend({ type: 'move', dx: moveX, dy: moveY });
      moveX = 0;
      moveY = 0;
    }
  }

  function queueMove(dx, dy) {
    moveX += dx;
    moveY += dy;
    if (!moveFrame) moveFrame = requestAnimationFrame(flushMove);
  }

  function centroid(count = points.size) {
    const pair = Array.from(points.values()).slice(0, count);
    return {
      x: pair.reduce((sum, point) => sum + point.x, 0) / pair.length,
      y: pair.reduce((sum, point) => sum + point.y, 0) / pair.length,
    };
  }

  function resetGesture() {
    if (moveFrame) cancelAnimationFrame(moveFrame);
    moveFrame = null;
    moveX = 0;
    moveY = 0;
    if (dragging) miniButton('left', 'up', secondTap ? 2 : 1);
    dragging = false;
    gesture = 'idle';
    travelled = 0;
    scrollTravelled = 0;
    secondTap = false;
    points.clear();
    miniTrackpad.classList.remove('active', 'space-switch');
  }

  miniTrackpad.addEventListener('pointerdown', (event) => {
    event.preventDefault();
    event.stopPropagation();
    miniTrackpad.setPointerCapture(event.pointerId);
    points.set(event.pointerId, { x: event.clientX, y: event.clientY });
    miniTrackpad.classList.add('active');
    if (points.size === 1) {
      gesture = 'pointer';
      gestureStarted = performance.now();
      startX = lastX = event.clientX;
      startY = lastY = event.clientY;
      travelled = 0;
      secondTap = Date.now() - lastTap.at < 360 &&
        Math.hypot(event.clientX - lastTap.x, event.clientY - lastTap.y) < 28;
    } else if (points.size === 2) {
      gesture = 'scroll';
      gestureStarted = performance.now();
      scrollTravelled = 0;
      const center = centroid();
      lastX = center.x;
      lastY = center.y;
    } else if (points.size === 3) {
      // The third finger decisively cancels pointer/scroll interpretation.
      // One semantic event crosses the wire after direction lock, so a lost
      // connection cannot strand a synthetic Control key on the Mac.
      if (moveFrame) cancelAnimationFrame(moveFrame);
      moveFrame = null;
      moveX = 0;
      moveY = 0;
      if (dragging) miniButton('left', 'up', secondTap ? 2 : 1);
      dragging = false;
      secondTap = false;
      gesture = 'space';
      gestureStarted = performance.now();
      const center = centroid(3);
      startX = lastX = center.x;
      startY = lastY = center.y;
    }
  });

  miniTrackpad.addEventListener('pointermove', (event) => {
    if (!points.has(event.pointerId)) return;
    event.preventDefault();
    event.stopPropagation();
    points.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (gesture === 'space' && points.size >= 3) {
      const center = centroid(3);
      const dx = center.x - startX;
      const dy = center.y - startY;
      if (Math.abs(dx) >= spaceSwipeThreshold &&
          Math.abs(dx) > Math.abs(dy) * 1.25) {
        if (miniSend({ type: 'space',
          direction: dx < 0 ? 'next' : 'previous' })) {
          gesture = 'space-fired';
          miniTrackpad.classList.add('space-switch');
        }
      }
      return;
    }
    if (gesture === 'space-fired') return;
    if (gesture === 'scroll' && points.size >= 2) {
      const center = centroid();
      const dx = center.x - lastX;
      const dy = center.y - lastY;
      lastX = center.x;
      lastY = center.y;
      scrollTravelled += Math.hypot(dx, dy);
      if (Math.abs(dx) + Math.abs(dy) > .2) {
        miniSend({ type: 'scroll', dx: dx * 1.35, dy: dy * 1.35 });
      }
      return;
    }
    if (gesture !== 'pointer' || points.size !== 1) return;
    const dx = event.clientX - lastX;
    const dy = event.clientY - lastY;
    lastX = event.clientX;
    lastY = event.clientY;
    travelled = Math.max(travelled,
      Math.hypot(event.clientX - startX, event.clientY - startY));
    if (travelled < 2.5) return;
    if (secondTap && !dragging) {
      miniButton('left', 'down', 2);
      dragging = true;
    }
    const baseGain = event.pointerType === 'touch'
      ? pointerGain : precisionPointerGain;
    const gain = baseGain + Math.min(pointerAccelerationLimit,
      Math.hypot(dx, dy) * pointerAcceleration);
    queueMove(dx * gain, dy * gain);
  });

  miniTrackpad.addEventListener('pointerup', (event) => {
    if (!points.has(event.pointerId)) return;
    event.preventDefault();
    event.stopPropagation();
    const wasScroll = gesture === 'scroll';
    const wasSpace = gesture === 'space' || gesture === 'space-fired';
    points.delete(event.pointerId);
    if (wasSpace) {
      if (points.size) gesture = 'ending';
      else resetGesture();
      return;
    }
    if (wasScroll) {
      if (scrollTravelled < 8 && performance.now() - gestureStarted < 360) {
        miniButton('right', 'down');
        miniButton('right', 'up');
      }
      if (points.size) {
        gesture = 'ending';
      } else {
        resetGesture();
      }
      return;
    }
    if (gesture === 'ending') {
      if (!points.size) resetGesture();
      return;
    }
    // A drag's last coalesced movement must land before mouse-up. Otherwise
    // the final frame becomes an ordinary move after the item was released.
    if (moveFrame) {
      cancelAnimationFrame(moveFrame);
      flushMove();
    }
    if (dragging) {
      miniButton('left', 'up', 2);
      dragging = false;
    } else if (travelled < 8 && performance.now() - gestureStarted < 420) {
      const clicks = secondTap ? 2 : 1;
      miniButton('left', 'down', clicks);
      miniButton('left', 'up', clicks);
      lastTap = secondTap ? { at: 0, x: 0, y: 0 } :
        { at: Date.now(), x: event.clientX, y: event.clientY };
    }
    if (!points.size) resetGesture();
  });

  miniTrackpad.addEventListener('pointercancel', resetGesture);
}

$$('[data-mini-mod]').forEach((button) => {
  // Keeping the textarea focused keeps the native keyboard raised while a
  // modifier is armed. The click still commits after pointer-up.
  button.addEventListener('pointerdown', (event) => event.preventDefault());
  button.addEventListener('click', () => {
    const code = button.dataset.miniMod;
    const enabled = !miniModifiers.has(code);
    if (!miniSend({ type: 'key', code, state: enabled ? 'down' : 'up' })) return;
    if (enabled) miniModifiers.add(code); else miniModifiers.delete(code);
    button.classList.toggle('on', enabled);
    button.setAttribute('aria-pressed', String(enabled));
    miniInput?.focus();
  });
});

$$('[data-mini-key]').forEach((button) => {
  button.addEventListener('pointerdown', (event) => event.preventDefault());
  button.addEventListener('click', () => {
    miniTapKey(button.dataset.miniKey);
    if (document.activeElement === miniInput) miniInput.focus();
  });
});

if (miniInput) {
  miniInput.addEventListener('blur', () => {
    miniClearModifiers(true);
    miniInput.value = '';
  });
  miniInput.addEventListener('compositionstart', () => { miniComposing = true; });
  miniInput.addEventListener('compositionend', (event) => {
    miniComposing = false;
    const text = String(event.data || miniInput.value || '');
    if (text) miniSendText(text);
    miniInput.value = '';
  });
  miniInput.addEventListener('beforeinput', (event) => {
    if (miniComposing || event.isComposing) return;
    if (event.inputType === 'deleteContentBackward') {
      event.preventDefault();
      miniTapKey('Backspace');
    } else if (event.inputType === 'deleteContentForward') {
      event.preventDefault();
      miniTapKey('Delete');
    } else if (event.inputType === 'insertLineBreak' ||
               event.inputType === 'insertParagraph') {
      event.preventDefault();
      miniTapKey('Enter');
    } else if (typeof event.data === 'string' && event.data) {
      event.preventDefault();
      miniSendText(event.data);
    }
  });
  // Dictation on some iOS versions emits only `input`. If beforeinput did not
  // consume it, forward the committed value once and immediately clear it.
  miniInput.addEventListener('input', () => {
    if (miniComposing || !miniInput.value) return;
    const text = miniInput.value;
    miniInput.value = '';
    miniSendText(text);
  });
  miniInput.addEventListener('keydown', (event) => {
    const named = {
      Backspace: 'Backspace', Delete: 'Delete', Enter: 'Enter', Escape: 'Escape',
      Tab: 'Tab', ArrowLeft: 'ArrowLeft', ArrowRight: 'ArrowRight',
      ArrowUp: 'ArrowUp', ArrowDown: 'ArrowDown', Home: 'Home', End: 'End',
      PageUp: 'PageUp', PageDown: 'PageDown',
    }[event.key];
    if (named && !miniComposing) {
      event.preventDefault();
      miniTapKey(named);
      return;
    }
    if (miniModifiers.size && /^(Key[A-Z]|Digit[0-9])$/.test(event.code)) {
      event.preventDefault();
      miniTapKey(event.code);
    }
  });
  miniDone?.addEventListener('pointerdown', (event) => event.preventDefault());
  miniDone?.addEventListener('click', () => miniInput.blur());
}

setMiniActive(location.hash === '#mini');

// ---- agent -------------------------------------------------------------
// The session id survives a WebView/app reload. The Mac mini keeps the actual
// conversation in its private local history database; the phone stores only
// this opaque id.
function newAgentSession() {
  return globalThis.crypto?.randomUUID
    ? globalThis.crypto.randomUUID()
    : Math.random().toString(36).slice(2) + Date.now().toString(36);
}
const AGENT_SESSION_KEY = 'avctl-agent-session';
function savedAgentSession() {
  try {
    const value = localStorage.getItem(AGENT_SESSION_KEY) || '';
    return /^[A-Za-z0-9_-]{8,80}$/.test(value) ? value : '';
  } catch (err) { return ''; }
}
function keepAgentSession(value) {
  try { localStorage.setItem(AGENT_SESSION_KEY, value); }
  catch (err) { /* the active in-memory conversation still works */ }
  return value;
}
let agentSession = savedAgentSession() || keepAgentSession(newAgentSession());
const agentForm = $('#agent-form');
const agentInput = $('#agent-input');
const agentLog = $('#agent-log');
const agentUsage = $('#agent-usage');
const agentMode = $('#agent-mode');
const agentHold = $('#agent-hold');
const agentSend = $('#agent-send');
let agentPending = false;
let agentVoicePendingBubble = null;

function updateAgentUsage(usage) {
  if (!agentUsage || !usage) return;
  const cost = Number(usage.estimated_cost_usd);
  const total = Number(usage.total_tokens || 0).toLocaleString();
  const cached = Number(usage.cached_prompt_tokens || 0).toLocaleString();
  if (usage.estimated_cost_usd !== undefined &&
      Number.isFinite(cost) && cost >= 0) {
    agentUsage.textContent = '· usage ≈$' + cost.toFixed(2);
    agentUsage.title = 'Session estimate · ' + total +
      ' tokens · ' + cached + ' cached';
  } else {
    agentUsage.textContent = '· usage ' + total + ' tokens';
    agentUsage.title = cached + ' cached · provider pricing not configured';
  }
}

function resetAgentUsage() {
  if (!agentUsage) return;
  agentUsage.textContent = '· usage ≈$0.00';
  agentUsage.removeAttribute('title');
}

function setAgentComposing(composing) {
  appShell?.classList.toggle('agent-composing', composing);
}

function agentMessage(text, kind, receipt, selection) {
  $('#agent-empty')?.remove();
  const bubble = document.createElement('div');
  bubble.className = 'agent-msg ' + kind + (receipt ? ' receipt' : '');
  const message = document.createElement('div');
  message.className = 'agent-copy';
  const blocks = String(text || '').trim().split(/\n{2,}/);
  (blocks.length ? blocks : ['']).forEach((block) => {
    const paragraph = document.createElement('p');
    block.split('\n').forEach((line, index) => {
      if (index) paragraph.append(document.createElement('br'));
      paragraph.append(document.createTextNode(line));
    });
    message.append(paragraph);
  });
  bubble.append(message);
  if (selection && Array.isArray(selection.items) && selection.items.length) {
    const results = document.createElement('div');
    results.className = 'agent-results';
    selection.items.slice(0, 8).forEach((item) => {
      const row = document.createElement('div');
      row.className = 'agent-result';
      const source = document.createElement('span');
      const domain = String(item.source || '').toLowerCase();
      source.textContent = domain === 'library' ? 'L'
        : domain.includes('qobuz') || domain.includes('roon') ? 'Q'
        : domain.includes('apple') ? 'A' : 'S';
      const copy = document.createElement('div');
      const title = document.createElement('b');
      title.textContent = item.name || 'untitled';
      const detail = document.createElement('small');
      detail.textContent = [item.artist, item.kind].filter(Boolean).join(' · ');
      copy.append(title, detail);
      row.append(source, copy);
      results.append(row);
    });
    bubble.append(results);
  }
  agentLog.append(bubble);
  agentLog.scrollTo({ top: agentLog.scrollHeight, behavior: 'smooth' });
  return bubble;
}

async function restoreAgentHistory() {
  const restoring = agentSession;
  try {
    const response = await fetchT(
      '/api/agent/history?session=' + encodeURIComponent(restoring), null, 15000);
    const data = await response.json().catch(() => ({}));
    if (!response.ok || restoring !== agentSession ||
        agentLog.querySelector('.agent-msg')) return;
    const turns = Array.isArray(data.turns) ? data.turns : [];
    turns.forEach((turn) => {
      if (turn.user) agentMessage(String(turn.user), 'user', false);
      if (turn.assistant) {
        agentMessage(String(turn.assistant), 'agent', Boolean(turn.acted));
      }
    });
    updateAgentUsage(data.usage);
  } catch (err) {
    // History restoration is optional; a new command must remain usable when
    // the local archive is temporarily unavailable.
  }
}

function restoreAgentDraft(message) {
  // Do not overwrite a next command the caller started while this request was
  // running. Otherwise preserve the failed command for one-tap editing/retry.
  if (agentInput.value.trim()) return;
  agentInput.value = message;
  agentInput.dispatchEvent(new Event('input'));
}

function applyAgentUI(directive) {
  if (!directive || typeof directive !== 'object') return;
  if (directive.mode === 'settings') {
    setSettingsOpen(true);
    return;
  }
  const panel = String(directive.panel || '');
  if (!PAGES.includes(panel)) return;
  setSettingsOpen(false);
  showPage(panel, true);
  settle(panel);
  if (panel !== 'music') return;
  const musicView = String(directive.music_view || '');
  if (musicView === 'library') {
    showMView('library');
  } else if (['search', 'explore'].includes(musicView)) {
    mScope = musicView === 'explore' ? 'catalog' : mScope;
    $$('#m-scope .seg-btn').forEach((button) => {
      button.classList.toggle('on', button.dataset.scope === mScope);
    });
    if (musicView === 'explore') $('#m-q').value = '';
    showMView('search', musicView !== 'explore');
  }
}

async function askAgent(raw) {
  const message = raw.trim();
  if (!message || agentPending) return;
  agentPending = true;
  agentInput.value = '';
  agentInput.style.height = '';
  agentSend.disabled = true;
  $('#agent-new').disabled = true;
  agentMessage(message, 'user', false);
  const pending = agentMessage('Working…', 'agent pending', false);
  busy(1);
  try {
    const response = await fetchT('/api/agent', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({
        message: message,
        session: agentSession,
        request_id: newAgentSession(),
      }),
    }, 300000);
    const data = await response.json().catch(() => ({}));
    pending.remove();
    if (!response.ok) {
      restoreAgentDraft(message);
      agentMessage(data.detail || 'The agent could not answer.', 'agent', false);
    } else {
      updateAgentUsage(data.usage);
      agentMessage(data.message, 'agent', Boolean(data.acted), data.selection);
      if (data.acted) refresh();
      applyAgentUI(data.ui);
    }
  } catch (err) {
    pending.remove();
    restoreAgentDraft(message);
    agentMessage(err && err.name === 'AbortError'
      ? 'Ask timed out. The action status is unknown, so check before retrying.'
      : 'The connection ended without a reply. The action status is unknown, so check before retrying.',
    'agent', false);
  } finally {
    busy(-1);
    agentPending = false;
    agentSend.disabled = false;
    $('#agent-new').disabled = false;
    if (location.hash === '#agent' &&
        !agentForm.classList.contains('voice-mode')) agentInput.focus();
  }
}

function nativeVoiceBridge() {
  return window.webkit?.messageHandlers?.avctl || null;
}

function setVoiceMode(voice, focusKeyboard = true) {
  if (!nativeVoiceBridge()) voice = false;
  agentForm.classList.toggle('voice-mode', voice);
  agentInput.hidden = voice;
  agentHold.hidden = !voice;
  agentSend.hidden = voice;
  agentMode.setAttribute('aria-label', voice ? 'Use keyboard' : 'Use voice input');
  try { localStorage.setItem('avctl-agent-input-mode', voice ? 'voice' : 'text'); }
  catch (err) { /* mode still works when storage is unavailable */ }
  if (!voice && focusKeyboard) agentInput.focus();
}

function resetVoiceInteraction() {
  agentForm.classList.remove(
    'voice-recording', 'voice-cancelling', 'voice-processing');
  agentHold.textContent = 'Hold to talk';
  agentHold.disabled = false;
  agentMode.disabled = false;
  $('#agent-new').disabled = false;
}

function finishVoicePending() {
  if (agentVoicePendingBubble) {
    agentVoicePendingBubble.remove();
    agentVoicePendingBubble = null;
    busy(-1);
  }
}

// Called only by the native shell. JSON is produced with
// JSONSerialization, and every visible value still lands through textContent.
window.avctlVoiceEvent = function avctlVoiceEvent(event) {
  const state = String(event?.state || '');
  if (state === 'starting') {
    agentHold.textContent = 'Starting…';
    return;
  }
  if (state === 'listening') {
    agentForm.classList.add('voice-recording');
    agentHold.textContent = 'Release to send';
    return;
  }
  if (state === 'transcribing') {
    agentForm.classList.remove('voice-recording', 'voice-cancelling');
    agentForm.classList.add('voice-processing');
    agentHold.textContent = 'Transcribing…';
    // Keep the held button enabled until pointerup. Disabling an element with
    // pointer capture can synthesize lostpointercapture, which used to cancel
    // the upload when the native 30-second recorder limit fired first.
    busy(1);
    agentVoicePendingBubble = agentMessage('Listening…', 'agent pending', false);
    return;
  }
  finishVoicePending();
  agentPending = false;
  resetVoiceInteraction();
  if (state === 'result') {
    const transcript = String(event.transcript || '').trim();
    const answer = String(event.message || '').trim();
    if (transcript) agentMessage(transcript, 'user', false);
    updateAgentUsage(event.usage);
    if (answer) {
      agentMessage(answer, 'agent', Boolean(event.acted), event.selection);
    }
    if (event.acted) refresh();
    applyAgentUI(event.ui);
  } else if (state === 'error') {
    const transcript = String(event.transcript || '').trim();
    const message = String(event.message || 'Voice input failed.');
    if (transcript) {
      agentMessage(transcript, 'user', false);
      // Preserve the recognized command behind the keyboard-mode toggle so
      // Provider/network failures can be retried without speaking again.
      agentInput.value = transcript;
      agentInput.dispatchEvent(new Event('input'));
    }
    // A disappearing toast is not enough for timeout/cancellation warnings:
    // the caller must still see “status unknown” before deciding to retry.
    agentMessage(message, 'agent', false);
    toast('Voice', message);
  }
};

if (agentForm) {
  agentInput.addEventListener('focus', () => setAgentComposing(true));
  agentInput.addEventListener('blur', () => setAgentComposing(false));
  agentForm.addEventListener('submit', (event) => {
    event.preventDefault();
    askAgent(agentInput.value);
  });
  agentInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      askAgent(agentInput.value);
    }
  });
  agentInput.addEventListener('input', () => {
    agentInput.style.height = '';
    agentInput.style.height = Math.min(agentInput.scrollHeight, 96) + 'px';
  });
  $$('[data-agent-example]').forEach((button) => {
    button.addEventListener('click', () => askAgent(button.dataset.agentExample));
  });
  if (nativeVoiceBridge()) {
    agentMode.hidden = false;
    agentForm.classList.add('voice-capable');
    let preferVoice = false;
    try { preferVoice = localStorage.getItem('avctl-agent-input-mode') === 'voice'; }
    catch (err) { /* text is the safe default */ }
    setVoiceMode(preferVoice, false);
    agentMode.addEventListener('click', () => {
      if (!agentPending) setVoiceMode(!agentForm.classList.contains('voice-mode'));
    });

    let voicePointer = null;
    let voiceStartY = 0;
    let cancelVoice = false;
    agentHold.addEventListener('pointerdown', (event) => {
      if (agentPending || event.button > 0) return;
      event.preventDefault();
      voicePointer = event.pointerId;
      voiceStartY = event.clientY;
      cancelVoice = false;
      agentPending = true;
      agentMode.disabled = true;
      $('#agent-new').disabled = true;
      try {
        agentHold.setPointerCapture(event.pointerId);
        agentForm.classList.add('voice-recording');
        agentHold.textContent = 'Release to send';
        nativeVoiceBridge().postMessage({
          event: 'voice-start', session: agentSession,
        });
      } catch (err) {
        voicePointer = null;
        agentPending = false;
        resetVoiceInteraction();
        toast('Voice', 'Voice input could not start.');
      }
    });
    agentHold.addEventListener('pointermove', (event) => {
      if (event.pointerId !== voicePointer) return;
      const nextCancel = voiceStartY - event.clientY > 64;
      if (nextCancel === cancelVoice) return;
      cancelVoice = nextCancel;
      agentForm.classList.toggle('voice-cancelling', cancelVoice);
      agentHold.textContent = cancelVoice ? 'Release to cancel' : 'Release to send';
    });
    function endVoice(event, forceCancel) {
      if (event.pointerId !== voicePointer) return;
      event.preventDefault();
      const shouldCancel = forceCancel || cancelVoice;
      voicePointer = null;
      agentForm.classList.remove('voice-recording', 'voice-cancelling');
      try {
        nativeVoiceBridge().postMessage({
          event: shouldCancel ? 'voice-cancel' : 'voice-stop',
        });
      } catch (err) {
        agentPending = false;
        resetVoiceInteraction();
        toast('Voice', 'Voice input lost its native connection.');
      }
    }
    agentHold.addEventListener('pointerup', (event) => endVoice(event, false));
    agentHold.addEventListener('pointercancel', (event) => endVoice(event, true));
    agentHold.addEventListener('lostpointercapture', (event) => endVoice(event, true));
    agentHold.addEventListener('contextmenu', (event) => event.preventDefault());
    agentHold.addEventListener('click', (event) => event.preventDefault());
  }
  $('#agent-new').addEventListener('click', () => {
    if (agentPending) return;
    const old = agentSession;
    agentSession = keepAgentSession(newAgentSession());
    fetchT('/api/agent/reset', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session: old }),
    }).catch(() => {});
    agentLog.replaceChildren();
    resetAgentUsage();
    const empty = document.createElement('div');
    empty.className = 'agent-empty';
    empty.id = 'agent-empty';
    const title = document.createElement('b');
    title.textContent = 'New conversation';
    empty.append(title, document.createTextNode('What should the rack do?'));
    agentLog.append(empty);
    if (!agentForm.classList.contains('voice-mode')) agentInput.focus();
  });
  restoreAgentHistory();
}

// ---- music --------------------------------------------------------------
//
// The one panel whose contents are data rather than a fixed keypad. Tiles
// and song rows deliberately carry no data-cmd: their taps mean different
// things by count and duration (tap / double-tap / long-press), so a gesture
// recogniser interprets them and calls send() itself. Everything rendered
// here is built with createElement/textContent -- track names are exactly
// the kind of string this file promises never to innerHTML.

let musicLoaded = false;
let albumOpen = false;
// Recently Added pages through the WHOLE library, the way Music's own shelf
// does -- these three drive the infinite scroll.
let mOffset = 0;
let mDone = false;
let mLoading = false;
// How deep the grid was when an album was opened -- the album view starts at
// its own top, and coming back lands exactly where the browsing left off.
let mGridScroll = 0;

function fmtDur(seconds) {
  if (!seconds && seconds !== 0) return '';
  const s = Math.round(seconds);
  return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
}

function cover(pid, artUrl, eager = false) {
  const box = document.createElement('span');
  box.className = 'cover';
  const src = artUrl || (pid ? '/api/music/artwork/' + pid : '');
  if (src) {
    const img = document.createElement('img');
    // Queue scrollers are transformed and sometimes hidden, which makes
    // WebKit's lazy-load threshold unreliable. Their cache-only URLs can be
    // requested eagerly without starting artwork extraction on the mini.
    img.loading = eager ? 'eager' : 'lazy';
    img.alt = '';
    img.onerror = () => img.remove();  // 404 -> the box's own placeholder note
    img.src = src;
    // Art that arrives late fades in over the placeholder instead of
    // popping; art already in cache paints immediately, no fade to wait on.
    if (!img.complete) {
      img.style.opacity = '0';
      img.addEventListener('load', () => { img.style.opacity = '1'; });
    }
    box.append(img);
  }
  return box;
}

function appendTiles(albums) {
  const grid = $('#m-grid');
  grid.append(...albums.map((a) => {
    const el = document.createElement('button');
    el.className = 'tile';
    el.dataset.album = a.album;
    el.dataset.artist = a.artist;
    el.dataset.pid = a.pid;
    const title = document.createElement('b');
    title.textContent = a.album || 'unknown album';
    const artist = document.createElement('small');
    artist.textContent = a.artist || '';
    el.append(cover(a.pid), title, artist);
    return el;
  }));
  $('#m-empty').hidden = grid.children.length > 0;
}

// Cover Flow is a second view of the first few real Recently Added records,
// never a second library. Its cards carry the exact tile datasets, so the
// shared tap/double-tap/hold recogniser remains authoritative.
const coverFlowAlbums = [];
let coverFlowFrame = 0;

function selectCoverFlow(card, scroll) {
  if (!card) return;
  const cards = $$('.m-coverflow-card');
  const index = cards.indexOf(card);
  if (index < 0) return;
  cards.forEach((el) => el.classList.toggle('selected', el === card));
  $$('#m-coverflow-dots i').forEach((el, i) => {
    el.classList.toggle('on', i === index);
  });
  $('#m-coverflow-title').textContent = card.dataset.album || 'unknown album';
  $('#m-coverflow-artist').textContent = card.dataset.artist || '';
  const play = $('#m-coverflow-play');
  play.disabled = false;
  play.dataset.cmd = 'music.play_album';
  play.dataset.args = JSON.stringify({
    album: card.dataset.album, artist: card.dataset.artist,
  });
  if (scroll) card.scrollIntoView({
    behavior: 'smooth', block: 'nearest', inline: 'center',
  });
}

function appendCoverFlow(albums, reset) {
  const track = $('#m-coverflow-track');
  const dots = $('#m-coverflow-dots');
  if (reset) {
    coverFlowAlbums.length = 0;
    track.replaceChildren();
    dots.replaceChildren();
    const play = $('#m-coverflow-play');
    play.disabled = true;
    delete play.dataset.cmd;
    delete play.dataset.args;
  }
  albums.forEach((album) => {
    if (coverFlowAlbums.length >= 8) return;
    const identity = [album.pid, album.album, album.artist].join('\u0000');
    if (coverFlowAlbums.some((item) => item.identity === identity)) return;
    coverFlowAlbums.push({ identity: identity, album: album });
    const card = document.createElement('button');
    card.className = 'tile m-coverflow-card';
    card.dataset.album = album.album || '';
    card.dataset.artist = album.artist || '';
    card.dataset.pid = album.pid || '';
    card.append(cover(album.pid));
    track.append(card);
    dots.append(document.createElement('i'));
  });
  if (!track.querySelector('.selected')) selectCoverFlow(track.firstElementChild, false);
}

$('#m-coverflow-track')?.addEventListener('scroll', () => {
  if (coverFlowFrame) return;
  coverFlowFrame = requestAnimationFrame(() => {
    coverFlowFrame = 0;
    const track = $('#m-coverflow-track');
    const center = track.getBoundingClientRect().left + track.clientWidth / 2;
    const nearest = $$('.m-coverflow-card').reduce((best, card) => {
      const rect = card.getBoundingClientRect();
      const distance = Math.abs(rect.left + rect.width / 2 - center);
      return !best || distance < best.distance ? { card: card, distance: distance } : best;
    }, null);
    selectCoverFlow(nearest?.card, false);
  });
}, { passive: true });

// Bumped whenever the listing is reset. A page fetch that was in flight
// when the bump happened belongs to a listing that no longer exists: it must
// drop its results, not append old tiles and corrupt mOffset (#107).
let mGen = 0;

async function loadPage(force) {
  if (mLoading || (mDone && !force)) return;
  const gen = mGen;
  mLoading = true;
  busy(1);
  try {
    const r = await fetchT('/api/music/recent?offset=' + mOffset
      + (force ? '&refresh=1' : ''), null, 30000);
    const data = await r.json().catch(() => ({}));
    if (gen !== mGen) return;   // a refresh reset the listing mid-flight
    if (!r.ok) {
      toast('failed', 'library scan -- ' + (data.detail || 'error ' + r.status));
      return;
    }
    if (force) $('#m-grid').replaceChildren();
    const albums = data.albums || [];
    appendCoverFlow(albums, force);
    appendTiles(albums);
    mOffset += albums.length;
    mDone = mOffset >= (data.total || 0) || albums.length === 0;
    musicLoaded = true;
  } catch (err) {
    if (gen === mGen) toast('failed', 'mini unreachable');
  } finally {
    // The latch belongs to the current generation; a superseded page must
    // not release the one the new load is holding.
    if (gen === mGen) mLoading = false;
    busy(-1);
  }
}

function loadRecent(force) {
  mGen += 1;          // discard any page still in flight...
  mLoading = false;   // ...and take the latch from it
  mOffset = 0;
  mDone = false;
  loadPlaylists();
  return loadPage(force);
}

// The library's second shelf. Playlist tiles ride the album tiles' whole
// gesture grammar; dataset.plpid is what marks them as playlists, and the
// first track's pid stands in as artwork.
async function loadPlaylists() {
  try {
    const r = await fetchT('/api/music/playlists', null, 30000);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) return;   // a 501 source without playlists: shelf stays empty
    const grid = $('#m-playlists');
    grid.replaceChildren(...(data.playlists || []).map((p) => {
      const el = document.createElement('button');
      el.className = 'tile';
      el.dataset.plpid = p.pid;
      el.dataset.pid = p.art || p.pid;   // artwork + gesture entry ticket
      el.dataset.name = p.name || '';
      const title = document.createElement('b');
      title.textContent = p.name || '';
      const sub = document.createElement('small');
      sub.textContent = p.count + (p.count === 1 ? ' song' : ' songs');
      el.append(cover(p.art), title, sub);
      return el;
    }));
    $('#m-pl-empty').hidden = grid.children.length > 0;
  } catch (err) { /* the tab is optional; the recent grid still loads */ }
}

// The library's own scope toggle, the search seg's twin.
let mLibraryScope = 'recent';
$$('#m-lib-scope .seg-btn').forEach((el) => {
  el.addEventListener('click', () => {
    $$('#m-lib-scope .seg-btn').forEach((b) => {
      b.classList.toggle('on', b === el);
    });
    const scope = el.dataset.lib;
    mLibraryScope = scope;
    $('#page-music').classList.toggle('music-recent', scope === 'recent');
    $('#m-recent').hidden = scope !== 'recent';
    $('#m-songs-wrap').hidden = scope !== 'songs';
    $('#m-playlists-wrap').hidden = scope !== 'playlists';
    if (scope === 'playlists') loadPlaylists();
    if (scope === 'songs' && !sLoaded) loadSongs(false);
  });
});

// The sentinel below the grid: whenever it comes into view, fetch the next
// page. This is the whole infinite scroll -- no scroll math, no thresholds.
const moreAlbums = $('#m-more');
if (moreAlbums) {
  new IntersectionObserver((entries) => {
    if (entries[0].isIntersecting && musicLoaded) loadPage(false);
  }).observe(moreAlbums);
}

// -- the song view: the library's other grain (#139) ------------------------
// Same shelf, per-track: newest-added first, same sentinel scroll, same
// generation guard as the album pager. Tap plays from here (shuffle-aware,
// server side); hold queues via the shared gesture recogniser.
let sOffset = 0;
let sDone = false;
let sLoading = false;
let sLoaded = false;
let sGen = 0;

// "today / 3d / 2w / 5mo" -- enough precision to say how new, no more.
function fmtAge(ms) {
  if (!ms) return '';
  const days = Math.floor((Date.now() - ms) / 86400000);
  if (days < 1) return 'today';
  if (days < 14) return days + 'd';
  if (days < 60) return Math.floor(days / 7) + 'w';
  if (days < 365) return Math.floor(days / 30) + 'mo';
  return Math.floor(days / 365) + 'y';
}

function recentSongRow(t) {
  const el = document.createElement('button');
  el.className = 'song';
  el.dataset.pid = t.pid;
  el.dataset.name = t.name || '';
  el.dataset.artist = t.artist || '';
  el.dataset.album = t.album || '';
  const text = document.createElement('span');
  text.className = 't';
  const name = document.createElement('b');
  name.textContent = t.name || '';
  const sub = document.createElement('small');
  sub.textContent = (t.artist || '') + (t.album ? ' \u2014 ' + t.album : '');
  text.append(name, sub);
  const when = document.createElement('span');
  when.className = 'd';
  when.textContent = fmtAge(t.added);
  el.append(cover(t.pid), text, when);
  return el;
}

async function loadSongsPage(force) {
  if (sLoading || (sDone && !force)) return;
  const gen = sGen;
  sLoading = true;
  busy(1);
  try {
    const r = await fetchT('/api/music/recent-songs?offset=' + sOffset
      + (force ? '&refresh=1' : ''), null, 30000);
    const data = await r.json().catch(() => ({}));
    if (gen !== sGen) return;   // a refresh reset the listing mid-flight
    if (!r.ok) {
      toast('failed', 'song list -- ' + (data.detail || 'error ' + r.status));
      return;
    }
    if (force) $('#m-songs').replaceChildren();
    const songs = data.songs || [];
    $('#m-songs').append(...songs.map(recentSongRow));
    sOffset += songs.length;
    sDone = sOffset >= (data.total || 0) || songs.length === 0;
    sLoaded = true;
    $('#m-songs-empty').hidden = $('#m-songs').children.length > 0;
  } catch (err) {
    if (gen === sGen) toast('failed', 'mini unreachable');
  } finally {
    if (gen === sGen) sLoading = false;
    busy(-1);
  }
}

function loadSongs(force) {
  sGen += 1;          // discard any page still in flight...
  sLoading = false;   // ...and take the latch from it
  sOffset = 0;
  sDone = false;
  return loadSongsPage(force);
}

const moreSongs = $('#m-songs-more');
if (moreSongs) {
  new IntersectionObserver((entries) => {
    if (entries[0].isIntersecting && sLoaded) loadSongsPage(false);
  }).observe(moreSongs);
}

function songRow(t, ordinal) {
  const el = document.createElement('button');
  el.className = 'song';
  el.dataset.pid = t.pid;
  el.dataset.name = t.name || '';
  el.dataset.artist = t.artist || '';
  const n = document.createElement('span');
  n.className = 'n';
  n.textContent = String(t.num || ordinal);
  const text = document.createElement('span');
  text.className = 't';
  const name = document.createElement('b');
  name.textContent = t.name || '';
  const artist = document.createElement('small');
  artist.textContent = t.artist || '';
  text.append(name, artist);
  const dur = document.createElement('span');
  dur.className = 'd';
  dur.textContent = fmtDur(t.duration);
  el.append(n, text, dur);
  return el;
}

let mView = 'library';        // which m-view is current
let mAlbumFrom = 'library';   // where the open album sheet returns to
let musicService = {
  name: 'Music service', source: 'service', available: false,
  authorized: false, can_add_to_library: false, authorization_url: null,
};

function applyMusicService(info) {
  if (!info || typeof info !== 'object') return;
  musicService = {...musicService, ...info};
  const providerLabel = $('#m-provider-label');
  if (providerLabel) {
    providerLabel.textContent = musicService.source === 'apple_music'
      ? 'Apple Music' : musicService.source === 'roon'
        ? 'Roon' : (musicService.name || 'Music');
    providerLabel.title = musicService.name || '';
  }
  const button = $('#m-service-scope');
  if (button) button.textContent = musicService.name;
  const toggle = $('#m-view-toggle');
  if (toggle && mView !== 'search') {
    toggle.setAttribute('aria-label',
      'Search library and ' + musicService.name);
  }
}

fetchT('/api/music/info', null, 10000).then(async (response) => {
  if (response.ok) applyMusicService(await response.json());
}).catch(() => {});

function syncMusicContext() {
  const overview = mView === 'library' && !albumOpen;
  $('#page-music').classList.toggle('music-overview', overview);
  $('#page-music').classList.toggle('music-recent', mLibraryScope === 'recent');
  $('#page-music').classList.toggle(
    'music-search', mView === 'search' && !albumOpen);
  $('#page-music').classList.toggle('music-album', albumOpen);
}

function showMView(name, focusSearch = true) {
  mView = name;
  // The album sheet overlays whichever view opened it -- search tiles open
  // albums too now, so the sheet is no longer the library's private room.
  $('#m-library').hidden = name !== 'library' || albumOpen;
  $('#m-search').hidden = name !== 'search' || albumOpen;
  $('#m-album').hidden = !albumOpen;
  syncMusicContext();
  const viewToggle = $('#m-view-toggle');
  viewToggle.setAttribute('aria-label', name === 'search'
    ? 'Back to music library' : 'Search library and ' + musicService.name);
  if (name === 'search' && !albumOpen) {
    if (focusSearch) $('#m-q').focus();
    doSearch();
  }
}
syncMusicContext();

async function openAlbum(album, artist, pid) {
  busy(1);
  try {
    const r = await fetchT('/api/music/album?album=' + encodeURIComponent(album)
      + '&artist=' + encodeURIComponent(artist || ''), null, 30000);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      toast('failed', 'album -- ' + (data.detail || 'error ' + r.status));
      return;
    }
    const head = $('#m-album-head');
    head.replaceChildren(cover(pid));
    const text = document.createElement('div');
    const title = document.createElement('b');
    title.textContent = album;
    const sub = document.createElement('small');
    const tracks = data.tracks || [];
    sub.textContent = (artist ? artist + ' -- ' : '')
      + tracks.length + (tracks.length === 1 ? ' song' : ' songs');
    text.append(title, sub);
    head.append(text);

    const list = $('#m-tracks');
    list.dataset.album = album;
    list.dataset.artist = artist || '';
    delete list.dataset.plpid;   // an album sheet, not a playlist's
    list.replaceChildren(...tracks.map((t, i) => songRow(t, i + 1)));

    mGridScroll = musicPage.scrollTop;
    mAlbumFrom = mView;
    albumOpen = true;
    showMView(mView);
    musicPage.scrollTop = 0;
  } catch (err) {
    toast('failed', 'mini unreachable');
  } finally {
    busy(-1);
  }
}

// A playlist: the album sheet wearing a playlist's name. The rows play
// "from here onward" in PLAYLIST order -- the list's plpid is what tells
// the song click which order that is.
async function openPlaylist(plpid, name, artPid) {
  busy(1);
  try {
    const r = await fetchT('/api/music/playlist?pid='
      + encodeURIComponent(plpid), null, 30000);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      toast('failed', 'playlist -- ' + (data.detail || 'error ' + r.status));
      return;
    }
    const head = $('#m-album-head');
    head.replaceChildren(cover(artPid));
    const text = document.createElement('div');
    const title = document.createElement('b');
    title.textContent = data.name || name || 'playlist';
    const sub = document.createElement('small');
    const tracks = data.tracks || [];
    sub.textContent = 'playlist -- '
      + tracks.length + (tracks.length === 1 ? ' song' : ' songs');
    text.append(title, sub);
    head.append(text);

    const list = $('#m-tracks');
    delete list.dataset.album;
    delete list.dataset.artist;
    list.dataset.plpid = plpid;
    list.replaceChildren(...tracks.map((t, i) => songRow(t, i + 1)));

    mGridScroll = musicPage.scrollTop;
    mAlbumFrom = mView;
    albumOpen = true;
    showMView(mView);
    musicPage.scrollTop = 0;
  } catch (err) {
    toast('failed', 'mini unreachable');
  } finally {
    busy(-1);
  }
}

// A catalog album: same sheet, different verbs. Nothing here plays --
// everything adds, whole album or one cherry-picked song at a time, and
// what is added arrives in Recently Added after iCloud sync.
async function openCatalogAlbum(id, art, name = '', artist = '') {
  // Roon Browse is hierarchical, so the first expansion still needs a few
  // Core round trips. Open the sheet synchronously and show the metadata we
  // already have from search instead of leaving the tap looking dead.
  const head = $('#m-album-head');
  head.replaceChildren(cover(null, art));
  const loadingText = document.createElement('div');
  const loadingTitle = document.createElement('b');
  loadingTitle.textContent = name || 'album';
  const loadingSub = document.createElement('small');
  loadingSub.textContent = [artist, 'loading songs…'].filter(Boolean).join(' -- ');
  loadingText.append(loadingTitle, loadingSub);
  head.append(loadingText);
  const loadingList = $('#m-tracks');
  delete loadingList.dataset.album;
  delete loadingList.dataset.artist;
  delete loadingList.dataset.plpid;
  loadingList.replaceChildren(searchNote('loading album from '
    + musicService.name + '…', false));
  mGridScroll = musicPage.scrollTop;
  mAlbumFrom = mView;
  albumOpen = true;
  showMView(mView);
  musicPage.scrollTop = 0;

  busy(1);
  try {
    const r = await fetchT('/api/music/search/album?id='
      + encodeURIComponent(id), null, 30000);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      loadingList.replaceChildren(searchNote(
        data.detail || 'album could not be loaded', false));
      toast('failed', 'album -- ' + (data.detail || 'error ' + r.status));
      return;
    }
    head.replaceChildren(cover(null, data.art || art));
    const text = document.createElement('div');
    const title = document.createElement('b');
    title.textContent = data.album || '';
    const sub = document.createElement('small');
    const tracks = data.tracks || [];
    sub.textContent = [data.artist, data.year,
      tracks.length + (tracks.length === 1 ? ' song' : ' songs')]
      .filter(Boolean).join(' -- ');
    text.append(title, sub);
    applyMusicService(data.service);
    if (musicService.can_add_to_library) {
      const addAll = document.createElement('button');
      addAll.className = 'add wide-add';
      addAll.textContent = '+ Lib';
      addAll.dataset.cmd = 'music.add';
      addAll.dataset.args = JSON.stringify({ kind: 'albums', id: data.id || id });
      text.append(addAll);
    }
    if (!musicService.can_add_to_library
        || musicService.library_kind === 'avctl virtual library') {
      const play = document.createElement('button');
      play.className = 'add wide-add';
      play.textContent = 'Play';
      play.dataset.cmd = 'music.service.play';
      play.dataset.args = JSON.stringify({kind: 'album', id: data.id || id,
        name: data.album || ''});
      const queue = document.createElement('button');
      queue.className = 'add';
      queue.textContent = '+ Q';
      queue.dataset.cmd = 'music.service.queue';
      queue.dataset.args = play.dataset.args;
      text.append(play, queue);
    }
    head.append(text);

    const list = $('#m-tracks');
    delete list.dataset.album;    // no play context: these are not here yet
    delete list.dataset.artist;
    delete list.dataset.plpid;
    list.replaceChildren(...tracks.map((t, i) => catalogRow(t, i + 1)));

    if (!data.authorized && musicService.authorization_url) {
      list.append(searchNote('adding needs a one-time sign-in:', true));
    }

  } catch (err) {
    loadingList.replaceChildren(searchNote('mini unreachable', false));
    toast('failed', 'mini unreachable');
  } finally {
    busy(-1);
  }
}

function catalogRow(t, ordinal) {
  const el = document.createElement('div');   // div: it holds a real button
  el.className = 'song';
  const n = document.createElement('span');
  n.className = 'n';
  n.textContent = String(ordinal);
  const text = document.createElement('span');
  text.className = 't';
  const name = document.createElement('b');
  name.textContent = t.name || '';
  const artist = document.createElement('small');
  artist.textContent = t.artist || '';
  text.append(name, artist);
  const dur = document.createElement('span');
  dur.className = 'd';
  dur.textContent = fmtDur(t.duration);
  const actions = document.createElement('span');
  actions.className = 'song-actions';
  const action = document.createElement('button');
  action.className = 'add';
  const serviceArgs = {kind: 'song', id: t.id, name: t.name || '',
    artist: t.artist || '', album: t.album || '', art: t.art || ''};
  action.textContent = musicService.can_add_to_library ? '+' : 'Play';
  action.dataset.cmd = musicService.can_add_to_library
    ? 'music.add' : 'music.service.play';
  action.dataset.args = JSON.stringify(musicService.can_add_to_library
    ? {kind: 'songs', id: t.id} : serviceArgs);
  actions.append(action);
  if (!musicService.can_add_to_library
      || musicService.library_kind === 'avctl virtual library') {
    if (musicService.can_add_to_library) {
      const play = document.createElement('button');
      play.className = 'add';
      play.textContent = 'Play';
      play.dataset.cmd = 'music.service.play';
      play.dataset.args = JSON.stringify(serviceArgs);
      actions.append(play);
    }
    const queue = document.createElement('button');
    queue.className = 'add';
    queue.textContent = '+Q';
    queue.dataset.cmd = 'music.service.queue';
    queue.dataset.args = JSON.stringify(serviceArgs);
    actions.append(queue);
  }
  el.append(n, text, dur, actions);
  return el;
}

$('#m-back')?.addEventListener('click', () => {
  albumOpen = false;
  showMView(mAlbumFrom);
  musicPage.scrollTop = mGridScroll;
});

$('#m-view-toggle')?.addEventListener('click', () => {
  showMView(mView === 'search' ? 'library' : 'search');
});

// Refresh follows the resync precedent: its own listener, so the repaint can
// wait for the server to actually drop its cache first.
$('#m-refresh')?.addEventListener('click', async () => {
  const ok = await send('music.refresh');
  if (ok) {
    loadRecent(true);
    if (sLoaded) loadSongs(true);
    exploreLoaded = false;
    if (mView === 'search' && mScope === 'catalog' && !$('#m-q').value.trim()) {
      loadExplore(true);
    }
    // The second door: when this page runs inside the iOS app, tell the
    // native side the library just changed so it can warm the cover cache
    // (the island may only draw covers already on the phone's disk).
    // Optional chaining keeps Safari -- the first door -- oblivious.
    window.webkit?.messageHandlers?.avctl?.postMessage(
      {event: 'library-refreshed'});
  }
});

// -- gestures --
//
// One recogniser for tiles and album songs. Same 8px move-cancel threshold
// as the hold-repeat code above: moving at all means the intent was the rail
// swipe or a scroll, never the tap.
//   tile:  tap -> open album   double-tap -> play album   hold -> queue album
//   song:  tap -> play (song + rest of album)             hold -> queue song
const musicPage = $('#page-music');
let mDown = null;                       // the current press, if any
let mSuppress = { el: null, until: 0 };  // release-scoped trailing-click guard
let mTap = { el: null, t: 0, timer: null };  // double-tap discrimination

function mQueueArgs(el) {
  if (el.classList.contains('song')) {
    const list = el.closest('#m-tracks');
    return {
      pid: el.dataset.pid,
      name: el.dataset.name || '',
      artist: el.dataset.artist || list?.dataset.artist || '',
      album: el.dataset.album || list?.dataset.album || '',
    };
  }
  if (el.dataset.plpid) return { playlist: el.dataset.plpid };
  return { album: el.dataset.album, artist: el.dataset.artist };
}

musicPage?.addEventListener('pointerdown', (event) => {
  const el = event.target.closest('.tile, .song');
  if (!el || !el.dataset.pid) return;   // search rows have their own Add key
  mDown = {
    el: el, pointerId: event.pointerId,
    x: event.clientX, y: event.clientY, fired: false,
    timer: setTimeout(() => {
      if (!mDown || mDown.el !== el) return;
      mDown.fired = true;
      el.classList.add('held');
      send('music.queue_add', mQueueArgs(el));
    }, 500),
  };
});
musicPage?.addEventListener('pointermove', (event) => {
  if (!mDown || mDown.pointerId !== event.pointerId || mDown.fired) return;
  if (Math.abs(event.clientX - mDown.x) > 10 ||
      Math.abs(event.clientY - mDown.y) > 10) {
    clearTimeout(mDown.timer);
    mDown = null;
  }
}, { passive: true });

function finishMusicHold(event) {
  if (!mDown || mDown.pointerId !== event.pointerId) return;
  clearTimeout(mDown.timer);
  if (mDown.fired) {
    // Start the buffer at RELEASE, not when the 500ms hold fires. A long
    // relaxed hold can last seconds on iPad; its synthetic click still must
    // not turn into an album play after the queue command has landed.
    mSuppress = { el: mDown.el, until: performance.now() + 1000 };
    const held = mDown.el;
    setTimeout(() => held.classList.remove('held'), 220);
  }
  mDown = null;
}
musicPage?.addEventListener('pointerup', finishMusicHold);
musicPage?.addEventListener('pointercancel', finishMusicHold);

musicPage?.addEventListener('click', (event) => {
  if (event.target.closest('[data-cmd]')) return;  // a real command button
  const cat = event.target.closest('.tile.catalog');
  if (cat) {
    // Catalog albums are not here yet: one verb, open. The album sheet is
    // where add-all and cherry-picking live.
    openCatalogAlbum(cat.dataset.catid, cat.dataset.art,
      cat.dataset.name, cat.dataset.artist);
    return;
  }
  const el = event.target.closest('.tile, .song');
  if (!el || !el.dataset.pid) return;
  // The click that trails a long-press is not a tap.
  if (mSuppress.el === el && performance.now() < mSuppress.until) {
    event.preventDefault();
    return;
  }

  if (el.classList.contains('song')) {
    // The recently-added song view: tap plays from here -- newest onward,
    // or the rest shuffled when shuffle is on (the server decides, #139).
    if (el.closest('#m-songs')) {
      send('music.play_recent', { pid: el.dataset.pid });
      return;
    }
    const list = el.closest('#m-tracks');
    if (list) {
      // The open sheet is the context: a playlist plays from here in
      // playlist order, an album in album order. Same finger, same rule.
      if (list.dataset.plpid) {
        send('music.play_playlist', {
          pid: list.dataset.plpid, from: el.dataset.pid,
        });
      } else {
        send('music.play_track', {
          album: list.dataset.album, artist: list.dataset.artist,
          pid: el.dataset.pid,
        });
      }
      return;
    }
    // A local search hit: tap plays its WHOLE album from track 1,
    // double-tap clears the queue down to just the song. (Decided
    // 2026-08-02 -- the search row stands for the album it belongs to;
    // "from this song onward" remains the album view's tap.)
    const now = Date.now();
    if (mTap.el === el && now - mTap.t < 300) {
      clearTimeout(mTap.timer);
      mTap = { el: null, t: 0, timer: null };
      send('music.play_song', { pid: el.dataset.pid, name: el.dataset.name });
      return;
    }
    clearTimeout(mTap.timer);
    mTap = {
      el: el, t: now,
      timer: setTimeout(() => {
        send('music.play_album', {
          album: el.dataset.album, artist: el.dataset.artist,
        });
      }, 300),
    };
    return;
  }
  // A tile: wait one double-tap window before navigating. Playback feedback
  // is latency-critical; opening an album is not. Playlist tiles speak the
  // same grammar with playlist verbs.
  const now = Date.now();
  if (mTap.el === el && now - mTap.t < 300) {
    clearTimeout(mTap.timer);
    mTap = { el: null, t: 0, timer: null };
    if (el.dataset.plpid) {
      send('music.play_playlist', { pid: el.dataset.plpid });
    } else {
      send('music.play_album', { album: el.dataset.album, artist: el.dataset.artist });
    }
    return;
  }
  clearTimeout(mTap.timer);
  mTap = {
    el: el, t: now,
    timer: setTimeout(() => {
      if (el.dataset.plpid) {
        openPlaylist(el.dataset.plpid, el.dataset.name, el.dataset.pid);
      } else {
        openAlbum(el.dataset.album, el.dataset.artist, el.dataset.pid);
      }
    }, 300),
  };
});

// -- search: one box, two scopes, ONE answer shape: albums --
//
// Simplified 2026-08-08. Library tiles are the recently-added grid's twins
// (pid/album/artist aboard), so the one gesture recogniser gives them
// tap-opens / double-tap-plays / hold-queues for free. Catalog tiles carry
// a catalog id instead of a pid: tap opens the same album sheet in add
// mode -- whole album or cherry-picked songs.

let mScope = 'library';
let exploreLoaded = false;
let exploreLoading = false;

function exploreCard(item) {
  const card = document.createElement('article');
  card.className = 'explore-card';
  const artwork = cover(
    item.source === 'library' ? item.pid : null, item.art || '');
  const copy = document.createElement('div');
  copy.className = 'explore-copy';
  const title = document.createElement('b');
  title.textContent = item.name || 'untitled';
  const sub = document.createElement('small');
  sub.textContent = [item.artist, item.kind].filter(Boolean).join(' · ');
  copy.append(title, sub);
  const actions = document.createElement('div');
  actions.className = 'explore-actions';

  if (item.source === 'library') {
    const play = document.createElement('button');
    play.type = 'button';
    play.textContent = 'Play';
    play.addEventListener('click', () => send('music.play_song', {
      pid: item.pid, name: item.name || '', artist: item.artist || '',
      album: item.album || '',
    }));
    const queue = document.createElement('button');
    queue.type = 'button';
    queue.textContent = '+ Q';
    queue.addEventListener('click', () => send('music.queue_add', {
      pid: item.pid, name: item.name || '', artist: item.artist || '',
      album: item.album || '',
    }));
    actions.append(play, queue);
  } else if (item.kind === 'album') {
    const view = document.createElement('button');
    view.type = 'button';
    view.textContent = 'View';
    view.addEventListener('click', () => openCatalogAlbum(
      item.id, item.art, item.name, item.artist));
    actions.append(view);
  } else if (musicService.can_add_to_library) {
    const add = document.createElement('button');
    add.type = 'button';
    add.textContent = '+ Lib';
    add.addEventListener('click', () => send('music.add', {
      kind: item.kind + 's', id: item.id,
    }));
    actions.append(add);
    if (musicService.library_kind !== 'avctl virtual library') {
      card.append(artwork, copy, actions);
      return card;
    }
    const play = document.createElement('button');
    play.type = 'button';
    play.textContent = 'Play';
    play.addEventListener('click', () => send('music.service.play', {
      kind: item.kind, id: item.id, name: item.name || '',
      artist: item.artist || '', album: item.album || '', art: item.art || '',
    }));
    const queue = document.createElement('button');
    queue.type = 'button';
    queue.textContent = '+ Q';
    queue.addEventListener('click', () => send('music.service.queue', {
      kind: item.kind, id: item.id, name: item.name || '',
      artist: item.artist || '', album: item.album || '', art: item.art || '',
    }));
    actions.append(play, queue);
  } else {
    const play = document.createElement('button');
    play.type = 'button';
    play.textContent = 'Play';
    play.addEventListener('click', () => send('music.service.play', {
      kind: item.kind, id: item.id, name: item.name || '',
      artist: item.artist || '', album: item.album || '', art: item.art || '',
    }));
    const queue = document.createElement('button');
    queue.type = 'button';
    queue.textContent = '+ Q';
    queue.addEventListener('click', () => send('music.service.queue', {
      kind: item.kind, id: item.id, name: item.name || '',
      artist: item.artist || '', album: item.album || '', art: item.art || '',
    }));
    actions.append(play, queue);
  }
  card.append(artwork, copy, actions);
  return card;
}

function renderExplore(data) {
  applyMusicService(data.service);
  const box = $('#m-explore');
  const contents = [];
  if (!data.authorized && data.catalog_available) {
    contents.push(searchNote(
      'Discovery is live. Sign in for personal ' + musicService.name
      + ' recommendations:', true));
  } else if (data.personal_error) {
    contents.push(searchNote(data.personal_error + '; showing charts.', true));
  }
  (data.sections || []).forEach((section) => {
    if (!Array.isArray(section.items) || !section.items.length) return;
    const shell = document.createElement('section');
    shell.className = 'explore-section';
    const head = document.createElement('div');
    head.className = 'explore-head';
    const title = document.createElement('h3');
    title.textContent = section.title || 'Explore';
    const source = document.createElement('span');
    source.textContent = section.kind === 'library' ? 'Library' : musicService.name;
    head.append(title, source);
    const row = document.createElement('div');
    row.className = 'explore-row';
    row.append(...section.items.map(exploreCard));
    shell.append(head, row);
    contents.push(shell);
  });
  if (!contents.length) {
    contents.push(searchNote('no recommendations available right now', false));
  }
  box.replaceChildren(...contents);
}

async function loadExplore(force = false) {
  const box = $('#m-explore');
  box.hidden = false;
  if (exploreLoaded && !force || exploreLoading) return;
  exploreLoading = true;
  box.replaceChildren(searchNote('loading recommendations…', false));
  busy(1);
  try {
    const response = await fetchT('/api/music/explore?limit=12', null, 30000);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      box.replaceChildren(searchNote(data.detail || 'Explore unavailable', false));
      return;
    }
    exploreLoaded = true;
    renderExplore(data);
  } catch (err) {
    box.replaceChildren(searchNote('mini unreachable', false));
  } finally {
    exploreLoading = false;
    busy(-1);
  }
}

function libraryTile(a) {
  const el = document.createElement('button');
  el.className = 'tile';
  el.dataset.album = a.album;
  el.dataset.artist = a.artist;
  el.dataset.pid = a.pid;
  const title = document.createElement('b');
  title.textContent = a.album || 'unknown album';
  const artist = document.createElement('small');
  artist.textContent = a.artist || '';
  el.append(cover(a.pid), title, artist);
  return el;
}

function catalogTile(a) {
  const el = document.createElement('button');
  el.className = 'tile catalog';
  el.dataset.catid = a.id;
  el.dataset.art = a.art || '';
  el.dataset.name = a.album || '';
  el.dataset.artist = a.artist || '';
  const title = document.createElement('b');
  title.textContent = a.album || '';
  const artist = document.createElement('small');
  artist.textContent = [a.artist, a.year].filter(Boolean).join(' -- ');
  el.append(cover(null, a.art), title, artist);
  return el;
}

function searchNote(text, withAuthLink) {
  const note = document.createElement('p');
  note.className = 'note';
  note.textContent = text;
  if (withAuthLink) {
    note.append(document.createTextNode(' '));
    const link = document.createElement('a');
    link.href = musicService.authorization_url || '/music/auth';
    link.target = '_blank';
    link.textContent = 'authorize ' + musicService.name;
    note.append(link);
  }
  return note;
}

let searchTimer = null;
let searchSeq = 0;
$('#m-q')?.addEventListener('input', () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(doSearch, 400);
});

$$('#m-scope .seg-btn').forEach((el) => {
  el.addEventListener('click', () => {
    mScope = el.dataset.scope;
    $$('#m-scope .seg-btn').forEach((b) => {
      b.classList.toggle('on', b === el);
    });
    $('#m-q').placeholder = mScope === 'library'
      ? 'search the library' : 'search or explore ' + musicService.name;
    doSearch();   // same term, new scope, immediately
  });
});

async function doSearch() {
  const term = $('#m-q').value.trim();
  const box = $('#m-results');
  const explore = $('#m-explore');
  $('#m-search-note').hidden = !term;
  if (!term) {
    box.replaceChildren();
    if (mScope === 'catalog') {
      explore.hidden = false;
      loadExplore();
    } else {
      explore.hidden = true;
    }
    return;
  }
  explore.hidden = true;
  const seq = ++searchSeq;
  busy(1);
  try {
    const url = mScope === 'library'
      ? '/api/music/library/search?q=' : '/api/music/search?q=';
    const r = await fetchT(url + encodeURIComponent(term), null, 30000);
    const data = await r.json().catch(() => ({}));
    if (seq !== searchSeq) return;   // a newer search superseded this one
    if (!r.ok) {
      box.replaceChildren(searchNote(data.detail || 'search failed', false));
      return;
    }
    applyMusicService(data.service);
    const albums = data.albums || [];
    const tiles = mScope === 'library'
      ? albums.map(libraryTile)
      : albums.map(catalogTile);
    if (!tiles.length) {
      box.replaceChildren(searchNote(
        (mScope === 'library' ? 'nothing in the library for '
                              : 'nothing on ' + musicService.name + ' for ')
        + term, false));
      return;
    }
    if (mScope === 'catalog' && !data.authorized
        && musicService.authorization_url) {
      tiles.unshift(searchNote('adding needs a one-time sign-in:', true));
    }
    box.replaceChildren(...tiles);
  } catch (err) {
    if (seq === searchSeq) box.replaceChildren(searchNote('mini unreachable', false));
  } finally {
    busy(-1);
  }
}

// The volume button: one round target, split on its midline -- the top half
// turns the mini up, the bottom half down. No data-cmd, because one element
// carrying two commands is exactly what the global dispatcher cannot say.
// Delegated, because the transport row appears on both home and the music
// panel and every copy must behave identically.
document.addEventListener('click', (event) => {
  const dial = event.target.closest('.mvol');
  if (!dial) return;
  const box = dial.getBoundingClientRect();
  send(event.clientY < box.top + box.height / 2
    ? 'music.vol.up' : 'music.vol.down');
});

// Focus queue: the count is navigation now, not a destructive control. The
// queue belongs to the shell transport, so it replaces the current control
// rail without navigating to Music. On iPad it stays in that left column
// while the permanent Music library remains untouched on the right.
let mqRevision = -1;
let mqRequest = 0;
let mqCloseTimer = 0;
let mqClearArm = 0;

function queueTrackRow(item, playing) {
  const row = document.createElement('div');
  row.className = playing ? 'mq-playing' : 'mq-item';
  const copy = document.createElement('span');
  copy.className = 'mq-copy';
  const title = document.createElement('b');
  title.textContent = item.name || 'Unknown track';
  const sub = document.createElement('small');
  sub.textContent = [item.artist, item.album].filter(Boolean).join(' · ');
  copy.append(title, sub);
  const duration = document.createElement('span');
  duration.className = 'mq-duration';
  duration.textContent = fmtDur(item.duration);
  const artwork = item.art || (item.pid
    ? '/api/music/artwork/' + item.pid + '?cached=1' : '');
  row.append(cover(item.pid || item.catalog_id, artwork, true), copy, duration);
  return row;
}

function paintQueue(data) {
  mqRevision = Number(data.revision ?? mqRevision);
  const items = data.items || [];
  let playing = data.playing || null;
  const fields = snapshot?.devices?.music?.fields;
  if (playing && (fields?.pid || fields?.catalog_id)
      === (playing.pid || playing.catalog_id)) {
    playing = Object.assign({}, playing, {
      name: playing.name || fields.track,
      artist: playing.artist || fields.artist,
      album: playing.album || fields.album,
      duration: playing.duration || fields.duration,
      art: playing.art || fields.art,
    });
  }
  $$('.js-mq-playing-wrap').forEach((el) => { el.hidden = !playing; });
  $$('.js-mq-playing').forEach((el) => {
    el.replaceChildren(...(playing ? [queueTrackRow(playing, true)] : []));
  });
  $$('.js-mq-list').forEach((el) => {
    el.replaceChildren(...items.map((item) => queueTrackRow(item, false)));
  });
  $$('.js-mq-count').forEach((el) => { el.textContent = String(items.length); });
  $$('.js-mq-empty').forEach((el) => { el.hidden = items.length > 0; });
  $$('.js-mq-clear').forEach((el) => {
    el.hidden = !playing && items.length === 0;
  });
}

async function loadQueue(force) {
  if (!force && !railStack.classList.contains('queue-open') &&
      activeMusicLayout !== 'split-deck') return;
  const seq = ++mqRequest;
  busy(1);
  try {
    const r = await fetchT('/api/music/queue', {
      headers: { Accept: 'application/json' },
    });
    const data = await r.json().catch(() => ({}));
    if (seq !== mqRequest) return;
    if (!r.ok) {
      toast('failed', data.detail || 'queue unavailable');
      return;
    }
    paintQueue(data);
  } catch (err) {
    if (seq === mqRequest) toast('failed', 'mini unreachable');
  } finally {
    busy(-1);
  }
}

function disarmQueueClear() {
  clearTimeout(mqClearArm);
  mqClearArm = 0;
  $$('.js-mq-clear').forEach((button) => {
    button.classList.remove('ask');
    button.textContent = button.dataset.clearLabel || 'Stop & clear queue';
  });
}

function openMusicQueue() {
  if (activeMusicLayout === 'split-deck') {
    const split = $('.mq-split');
    split.querySelector('.mq-split-scroll').scrollTop = 0;
    loadQueue(true);
    return;
  }
  clearTimeout(mqCloseTimer);
  const view = $('#m-queue');
  view.hidden = false;
  requestAnimationFrame(() => railStack.classList.add('queue-open'));
  $('#mq-scroll').scrollTop = 0;
  loadQueue(true);
}

function closeMusicQueue() {
  disarmQueueClear();
  railStack.classList.remove('queue-open');
  clearTimeout(mqCloseTimer);
  mqCloseTimer = setTimeout(() => {
    if (!railStack.classList.contains('queue-open')) $('#m-queue').hidden = true;
  }, 230);
}

document.addEventListener('click', (event) => {
  if (event.target.closest('.mq')) openMusicQueue();
});
$('#mq-back').addEventListener('click', closeMusicQueue);
document.addEventListener('click', async (event) => {
  const button = event.target.closest('.js-mq-clear');
  if (!button) return;
  if (!mqClearArm) {
    $$('.js-mq-clear').forEach((el) => {
      el.classList.add('ask');
      el.textContent = el.dataset.confirmLabel || 'Tap again to stop & clear';
    });
    mqClearArm = setTimeout(disarmQueueClear, 3500);
    return;
  }
  disarmQueueClear();
  if (await send('music.clear')) loadQueue(true);
});
document.addEventListener('avctlappearancechange', (event) => {
  if (event.detail?.layout === 'split-deck') {
    if (railStack.classList.contains('queue-open')) closeMusicQueue();
    loadQueue(true);
  } else disarmQueueClear();
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && railStack.classList.contains('queue-open')) {
    closeMusicQueue();
  }
});

// -- the transport bar, painted from the same snapshot as everything else --

function paintMusic(mu) {
  if (!mu) return;
  const f = mu.fields;
  let track = f.track || UNKNOWN;
  if (!f.track) {
    if (f.state === 'not_running') track = 'Music is not running';
    else if (f.state === 'stopped') track = 'nothing playing';
    else if (!mu.online) track = 'Music unreachable';
  }
  // The mini-player line, on every bar: cover, then track / album - artist.
  $$('.js-mnow-track').forEach((el) => { el.textContent = track; });
  const sub = [f.album, f.artist].filter(Boolean).join(' — ');
  $$('.js-mnow-sub').forEach((el) => { el.textContent = sub; });
  // The cover swaps only when the track actually changes -- resetting the
  // img src every poll would flicker for nothing.
  const pid = f.pid || '';
  $$('.js-mnow-cover').forEach((box) => {
    if (box.dataset.pid === pid) return;
    box.dataset.pid = pid;
    box.replaceChildren();
    if (pid) {
      const img = document.createElement('img');
      img.alt = '';
      img.onerror = () => img.remove();
      img.src = '/api/music/artwork/' + pid;
      box.append(img);
    }
  });
  // The queue chip is the door to Focus, and its number is the logical tail
  // from avctl -- never Music's played-but-still-in-the-playlist rows.
  const queued = f.queued || 0;
  $$('.js-mq-n').forEach((el) => { el.textContent = String(queued); });
  // A one-song queue has zero tracks NEXT but still has a Playing row and a
  // useful clear action. Keep its door visible until playback truly ends.
  const queueAvailable = queued > 0 ||
    (Boolean(f.pid) && (f.state === 'playing' || f.state === 'paused'));
  $$('.js-mq').forEach((el) => { el.hidden = !queueAvailable; });
  if ((railStack.classList.contains('queue-open') ||
       activeMusicLayout === 'split-deck') &&
      f.queue_revision !== null && f.queue_revision !== undefined &&
      Number(f.queue_revision) !== mqRevision) {
    loadQueue(false);
  }
  // The round volume buttons (home and music panel) show the mini's level.
  const level = f.muted
    ? 'MUTE'
    : (f.volume === null || f.volume === undefined ? UNKNOWN : String(f.volume));
  $$('.js-mvol').forEach((el) => { el.textContent = level; });
  $$('[data-cmd="music.play_pause"] .g').forEach((el) => {
    el.textContent = f.state === 'playing' ? '❚❚' : '▶';
  });
  $$('[data-cmd="music.shuffle"]').forEach((el) => {
    el.classList.toggle('sel', f.shuffle === true);
  });
  $$('[data-cmd="music.repeat_one"]').forEach((el) => {
    el.classList.toggle('sel', f.repeat === 'one');
  });
  const duration = Number(f.duration) || 0;
  const position = Math.max(0, Number(f.position) || 0);
  const progress = duration > 0 ? Math.min(100, position / duration * 100) : 0;
  $$('.js-mprogress').forEach((el) => { el.style.width = progress + '%'; });
  $$('.js-mtime-now').forEach((el) => { el.textContent = fmtDur(position) || '--:--'; });
  $$('.js-mtime-total').forEach((el) => { el.textContent = fmtDur(duration) || '--:--'; });
}

// ---- staying current ----------------------------------------------------
//
// The server pushes: /api/events sends a `state` event whenever the
// background poller sees the rack change, so a button press -- or someone
// picking up a physical remote -- reaches the screen in under a second
// without the phone asking anything.
//
// Polling remains, HONESTLY: it runs until the stream's first event proves
// the push path works, and it resumes the moment the stream errors, so the
// screen never silently depends on a connection that died. Both paths land
// in takeSnapshot, so what the screen says never depends on which one fed it.
let es = null;
let esRetry = 5000;
let esSeenAt = 0;    // when the stream last proved it was alive
let pollTimer = null;

function startPolling() {
  if (!pollTimer) {
    pollTimer = setInterval(() => { if (!document.hidden) refresh(); }, 15000);
  }
}
function stopPolling() {
  clearInterval(pollTimer);
  pollTimer = null;
}

function connectEvents() {
  if (es || document.hidden || !window.EventSource) return;
  es = new EventSource('/api/events');
  esSeenAt = Date.now();
  es.addEventListener('state', (event) => {
    stopPolling();               // the push path just proved itself
    esRetry = 5000;
    esSeenAt = Date.now();
    led.classList.remove('bad');
    takeSnapshot(JSON.parse(event.data));
  });
  // The server's quiet-stream heartbeat. Its only job is to feed the
  // watchdog below: a stream with neither states nor pings is dead.
  es.addEventListener('ping', () => {
    esRetry = 5000;
    esSeenAt = Date.now();
  });
  es.onerror = () => {
    // EventSource would retry by itself, but silently -- close it, fall
    // back to polling so the screen stays live, and retry with backoff.
    es.close();
    es = null;
    startPolling();
    setTimeout(connectEvents, esRetry);
    esRetry = Math.min(esRetry * 2, 60000);
  };
}

// A connection that dies without a FIN reaching the phone -- a network path
// change, a NAT timeout -- fires no onerror, ever. The screen would keep
// showing week-old state with a green LED (#106). The server pings every
// 15s (sync.sse_heartbeat); double it plus slack is proof of death.
setInterval(() => {
  if (!es || document.hidden) return;
  if (Date.now() - esSeenAt > 35000) {
    es.close();
    es = null;
    startPolling();
    connectEvents();
  }
}, 10000);

// ---- the wide stage (full-width iPad / any window ≥740px) ----------------
//
// Layout C: the Music panel leaves the rail and stands permanently on the
// right; the rail keeps snapping between the three button panels. Done by
// MOVING the node at the breakpoint -- its listeners, scroll position and
// lazy-load state ride along, and the narrow path stays byte-identical.
// PAGES is recomputed from the rail's actual children so the snap index
// never counts a panel that is no longer inside it.
const WIDE = matchMedia('(min-width: 740px)');
// Where music returns when the window narrows: its original right-hand
// neighbor in the rail (captured before any move).
const musicRailNeighbor = musicPage ? musicPage.nextElementSibling : null;

function recomputePages() {
  PAGES.length = 0;
  $$('.page', rail).forEach((el) => PAGES.push(el.id.replace('page-', '')));
}

function placeMusic() {
  if (!musicPage) return;
  if (WIDE.matches && musicPage.parentElement === rail) {
    $('.app').insertBefore(musicPage, $('.mbar'));
    recomputePages();
    // settle() owns the first library load, and it never fires for a panel
    // that is permanently visible -- load here instead.
    if (!musicLoaded) loadRecent(false);
    // A #music hash has no rail panel to mean in wide mode.
    if (location.hash === '#music') history.replaceState(null, '', '#home');
  } else if (!WIDE.matches && musicPage.parentElement !== rail) {
    rail.insertBefore(musicPage, musicRailNeighbor);
    recomputePages();
  }
}
WIDE.addEventListener('change', () => {
  placeMusic();
  showPage(location.hash.replace('#', '') || 'home', false);
});
placeMusic();
showPage(location.hash.replace('#', '') || 'home', false);

// ---- tap the clock: double-tap the top chrome to fly home ----------------
// The iOS status-bar gesture, for a panel that owns its own scrolling: two
// taps within 300ms in the safe-area chrome, brand row, or readout screen send
// the active page back to the top (and the music pane too, on the wide stage).
// The resync button shares the row and is excluded -- a double-tap must never
// mean two resyncs.
(() => {
  const brand = $('.brand');
  let last = 0;
  document.addEventListener('click', (event) => {
    if (settingsOpen || event.target.closest('button')) return;
    // In the native shell the WKWebView now extends behind the status bar.
    // Its safe-area padding is empty chrome, so include everything above the
    // brand as the iPhone/iPad equivalent of tapping the system clock.
    const inTopSafeArea = brand &&
      event.clientY <= brand.getBoundingClientRect().top;
    if ((!inTopSafeArea && !event.target.closest('.brand, .screen'))
        || event.target.closest('[data-cmd]')) return;
    const now = Date.now();
    if (now - last < 300) {
      last = 0;
      if (railStack.classList.contains('queue-open')) {
        $('#mq-scroll').scrollTo({ top: 0, behavior: 'smooth' });
        return;
      }
      const name = PAGES[Math.round(rail.scrollLeft / (rail.clientWidth || 1))]
        || 'home';
      if (name === 'agent') {
        agentLog?.scrollTo({ top: 0, behavior: 'smooth' });
      }
      $('#page-' + name)?.scrollTo({ top: 0, behavior: 'smooth' });
      if (musicPage && musicPage.parentElement !== rail) {
        musicPage.scrollTo({ top: 0, behavior: 'smooth' });
      }
    } else {
      last = now;
    }
  });
})();

refresh();
startPolling();       // until the stream's first event arrives
connectEvents();
if (activeMusicLayout === 'split-deck') loadQueue(true);
if (new URLSearchParams(location.search).get('setup') === '1') {
  history.replaceState(null, '', location.pathname + location.hash);
  window.avctlOpenSetup();
}

// A hidden phone drops both paths -- a pocket should not hold a socket open
// to the mini -- and picks them straight back up on return.
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    miniDisconnect();
    if (es) { es.close(); es = null; }
    return;
  }
  if (miniActive) miniConnect();
  refresh();
  startPolling();
  connectEvents();
});
