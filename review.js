  const form = document.querySelector('form');
  const endpoint = new URL(form.getAttribute('action'), document.baseURI);
  const status = document.querySelector('.save-status');
  const errorLine = document.querySelector('.save-error');
  const revision = form.elements.namedItem('revision');
  const undoButton = document.querySelector('.undo');
  const preview = document.querySelector('.preview');
  const clips = Array.from(document.querySelectorAll('.clip'));
  const deleted = [];
  let selected;
  let editVersion = 0;
  let savedVersion = 0;
  let saveTimer;
  let inFlight;
  let submitting = false;

  function announce(message, state = '', error = '') {
    status.textContent = message;
    status.dataset.state = state;
    errorLine.textContent = error;
    errorLine.hidden = !error;
  }

  function readTime(input) {
    const display = input.value.trim();
    if (display === input.defaultValue) return Number(input.dataset.seconds);
    if (/^\d+(\.\d+)?$/.test(display)) return Number(display);
    const timestamp = /^(\d+):([0-5]?\d)(\.\d+)?$/.exec(display);
    return timestamp ? Number(timestamp[1]) * 60 + Number(timestamp[2] + (timestamp[3] || '')) : NaN;
  }

  function snapshot() {
    return clips.filter((clip) => !clip.hidden).map((clip, index) => {
      const start = readTime(clip.querySelector('.start'));
      const end = readTime(clip.querySelector('.end'));
      if (!Number.isFinite(start) || !Number.isFinite(end)) {
        throw new Error('Enter times as minutes:seconds or decimal seconds for phrase ' + (index + 1) + '.');
      }
      return {start, end, text: clip.querySelector('textarea').value};
    });
  }

  function setFrame(image, seconds) {
    const frame = 'frame/' + Math.round(seconds * 48000) + '.jpg';
    if (image.getAttribute('src') !== frame) image.src = frame;
  }

  function selectClip(clip) {
    selected?.classList.remove('selected');
    selected = clip;
    selected?.classList.add('selected');
    if (!preview) return;
    const image = preview.querySelector('img');
    const placeholder = preview.querySelector('.preview-placeholder');
    const input = clip?.querySelector('.start');
    const seconds = input ? readTime(input) : NaN;
    image.hidden = !Number.isFinite(seconds);
    placeholder.hidden = !image.hidden;
    preview.querySelector('.preview-time').textContent = image.hidden ? '' : input.value;
    if (!image.hidden) {
      setFrame(image, seconds);
      setFrame(clip.querySelector('.thumbnail img'), seconds);
    }
  }

  function updateCount() {
    const count = clips.filter((clip) => !clip.hidden).length;
    document.querySelector('.phrase-count').textContent = count + (count === 1 ? ' phrase' : ' phrases');
    document.querySelector('.empty').hidden = count !== 0;
    undoButton.hidden = !deleted.length;
  }

  function scheduleSave() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveDraft, 400);
  }

  function changed() {
    editVersion += 1;
    announce('Saving…', 'pending');
    if (!submitting) scheduleSave();
  }

  function saveDraft() {
    clearTimeout(saveTimer);
    if (inFlight) return inFlight;
    if (editVersion === savedVersion) return Promise.resolve(true);

    let draft;
    try {
      draft = snapshot();
    } catch (error) {
      announce('Unsaved edits', 'error', error.message);
      return Promise.resolve(false);
    }
    const version = editVersion;
    const body = new URLSearchParams({ action: 'save', revision: revision.value, clips: JSON.stringify(draft) });
    announce('Saving…', 'pending');
    inFlight = fetch(endpoint, { method: 'POST', headers: { Accept: 'application/json' }, body })
      .then(async (response) => {
        const result = await response.json();
        if (!response.ok) throw new Error(result.error);
        revision.value = result.revision;
        savedVersion = version;
        announce(savedVersion === editVersion ? 'Saved' : 'Saving…', savedVersion === editVersion ? '' : 'pending');
        if (savedVersion !== editVersion && !submitting) scheduleSave();
        return true;
      })
      .catch((error) => {
        announce('Unsaved edits', 'error', error.message);
        return false;
      })
      .finally(() => { inFlight = undefined; });
    return inFlight;
  }

  for (const clip of clips) {
    clip.addEventListener('focusin', () => selectClip(clip));
    clip.addEventListener('input', changed);
    clip.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && event.target.matches('input')) {
        event.preventDefault();
        event.target.blur();
      }
    });
    clip.addEventListener('change', (event) => {
      if (event.target.matches('.start')) selectClip(clip);
    });
    clip.addEventListener('focusout', (event) => {
      if (event.target.matches('input, textarea') && !submitting) saveDraft();
    });
    clip.querySelector('.thumbnail')?.addEventListener('click', () => clip.querySelector('textarea').focus());
    clip.querySelector('.delete').addEventListener('click', () => {
      clip.hidden = true;
      deleted.push(clip);
      const next = clips.slice(clips.indexOf(clip) + 1).find((row) => !row.hidden);
      const active = next || clips.find((row) => !row.hidden);
      selectClip(active);
      updateCount();
      changed();
      (active?.querySelector('textarea') || undoButton).focus();
    });
  }

  undoButton.addEventListener('click', () => {
    const clip = deleted.pop();
    clip.hidden = false;
    updateCount();
    changed();
    clip.querySelector('textarea').focus();
  });

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (submitting) return;
    const button = event.submitter || form.querySelector('.primary');
    submitting = true;
    if (!(await saveDraft())) {
      submitting = false;
      return;
    }
    let draft;
    try {
      draft = snapshot();
    } catch (error) {
      announce('Unsaved edits', 'error', error.message);
      submitting = false;
      return;
    }
    announce('Rendering…', 'pending');
    for (const [name, value] of [['action', button.value], ['clips', JSON.stringify(draft)]]) {
      const input = document.createElement('input');
      input.type = 'hidden';
      input.name = name;
      input.value = value;
      form.append(input);
    }
    button.disabled = true;
    form.submit();
  });

  window.addEventListener('beforeunload', (event) => {
    if (!submitting && savedVersion !== editVersion) event.preventDefault();
  });

  updateCount();
  selectClip(clips[0]);
