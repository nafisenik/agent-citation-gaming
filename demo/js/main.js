// Scene player: Next / Back, or Play all. One scene on screen at a time.

import { scenes } from './scenes.js';

const AUTOPLAY_FALLBACK_MS = 4000;

class Player {
  constructor(data, root, controls, progress) {
    this.data = data;
    // A scene can opt out when the data it needs was not exported, so the demo degrades
    // to the scenes it can actually support instead of rendering an empty one.
    this.scenes = scenes.filter((scene) => !scene.skipIf || !scene.skipIf(data));
    this.root = root;
    this.controls = controls;
    this.progress = progress;
    this.index = 0;
    this.playing = false;
    this.timer = null;
    this.ctx = {};
  }

  get scene() {
    return this.scenes[this.index];
  }

  rerender() {
    const scene = this.scene;
    this.root.innerHTML = scene.render(this.data, this.ctx);
    if (scene.after) scene.after(this.root, this.data, this.ctx, this);
    this.renderControls();
    this.renderProgress();
  }

  go(index) {
    this.index = Math.max(0, Math.min(this.scenes.length - 1, index));
    this.rerender();
    this.root.scrollIntoView({ block: 'start' });
    if (this.playing) this.queueNext();
  }

  next() {
    if (this.index >= this.scenes.length - 1) {
      this.pause();
      return;
    }
    this.go(this.index + 1);
  }

  blocked() {
    const scene = this.scene;
    return Boolean(scene.blocksNext && scene.blocksNext(this.ctx));
  }

  enableNext() {
    this.renderControls();
  }

  queueNext() {
    clearTimeout(this.timer);
    const scene = this.scene;
    // An interactive scene has nothing to wait for during autoplay, so make the choice the
    // scene itself declares and label it, rather than stalling or inventing one.
    if (scene.needsChoice && !this.ctx.choice && scene.autoChoice) {
      this.ctx.choice = scene.autoChoice;
      this.ctx.autoChosen = true;
      this.rerender();
    }
    this.timer = setTimeout(
      () => this.next(),
      scene.autoplayMs || AUTOPLAY_FALLBACK_MS
    );
  }

  play() {
    this.playing = true;
    this.renderControls();
    this.queueNext();
  }

  pause() {
    this.playing = false;
    clearTimeout(this.timer);
    this.renderControls();
  }

  restart() {
    this.pause();
    this.ctx = {};
    this.go(0);
  }

  renderControls() {
    const last = this.index >= this.scenes.length - 1;
    const blocked = this.blocked();
    const label = this.index === 0 ? 'Begin' : 'Next';

    this.controls.innerHTML = `
<button class="btn" id="next" ${last || blocked ? 'disabled' : ''}>
  ${last ? 'Done' : `${label} ▸`}
</button>
<button class="btn ghost" id="back" ${this.index === 0 ? 'disabled' : ''}>◂ Back</button>
<button class="btn ghost" id="play">${this.playing ? '❙❙ Pause' : '▶ Play all'}</button>
${last ? '<button class="btn ghost" id="restart">↺ Start again</button>' : ''}
<div class="progress" role="progressbar" aria-valuenow="${this.index + 1}"
     aria-valuemin="1" aria-valuemax="${this.scenes.length}"
     aria-label="Scene ${this.index + 1} of ${this.scenes.length}"></div>`;

    const click = (id, fn) => {
      const node = this.controls.querySelector(`#${id}`);
      if (node) node.addEventListener('click', fn);
    };
    click('next', () => {
      this.pause();
      this.next();
    });
    click('back', () => {
      this.pause();
      this.go(this.index - 1);
    });
    click('play', () => (this.playing ? this.pause() : this.play()));
    click('restart', () => this.restart());
    this.renderProgress();
  }

  renderProgress() {
    const bar = this.controls.querySelector('.progress');
    if (!bar) return;
    bar.innerHTML = this.scenes
      .map((_, index) => {
        const state = index === this.index ? 'on' : index < this.index ? 'done' : '';
        return `<i class="${state}"></i>`;
      })
      .join('');
  }
}

async function boot() {
  const root = document.getElementById('stage');
  const controls = document.getElementById('controls');
  try {
    const response = await fetch('./data/story.json');
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    const player = new Player(data, root, controls);
    player.rerender();

    document.addEventListener('keydown', (event) => {
      if (event.key === 'ArrowRight' || event.key === ' ') {
        if (!player.blocked()) {
          event.preventDefault();
          player.pause();
          player.next();
        }
      } else if (event.key === 'ArrowLeft') {
        player.pause();
        player.go(player.index - 1);
      }
    });
  } catch (error) {
    // Say what went wrong rather than showing an empty stage.
    root.innerHTML = `
<div class="scene">
  <h2>The recorded runs did not load</h2>
  <p><code>demo/data/story.json</code> could not be read: ${error.message}</p>
  <p class="small">
    Opening this file straight from disk can trip the browser's rules about local files.
    Serving the folder works: <code>python3 -m http.server</code> from the repository root,
    then open <code>/demo/</code>.
  </p>
</div>`;
  }
}

boot();
