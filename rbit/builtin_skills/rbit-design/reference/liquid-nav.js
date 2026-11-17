export function createLiquidNav({ el, items, active, onChange, haptic = true }) {
  const tg = window.Telegram?.WebApp;
  const hap = (kind) => {
    if (!haptic) return;
    try {
      if (kind === 'select') tg?.HapticFeedback?.selectionChanged();
      else tg?.HapticFeedback?.impactOccurred?.(kind || 'light');
    } catch {}
  };

  let current = active ?? items[0].id;
  let drag = null;

  const cell = () => (el.clientWidth - 8) / items.length;
  const setX = (x) => el.querySelector('.ind')?.style.setProperty('--x', x + 'px');
  const idxAt = (clientX) => {
    const r = el.getBoundingClientRect();
    return Math.max(0, Math.min(items.length - 1, Math.floor((clientX - r.left - 4) / cell())));
  };
  const sync = () => {
    el.querySelectorAll('button').forEach((b, i) => b.classList.toggle('on', items[i].id === current));
    setX(items.findIndex((n) => n.id === current) * cell());
  };

  el.innerHTML =
    '<span class="ind"><i></i></span>' +
    items.map((it) => `<button data-t="${it.id}" aria-label="${it.label}"><span class="ni">${it.icon}</span><span>${it.label}</span></button>`).join('');

  function select(id, pop = true) {
    if (id === current) {
      sync();
      if (window.scrollY > 4) window.scrollTo({ top: 0, behavior: 'smooth' });
      return;
    }
    current = id;
    window.scrollTo(0, 0);
    sync();
    const btn = el.querySelector(`button[data-t="${id}"]`);
    if (pop && btn) {
      btn.classList.remove('pop');
      void btn.offsetWidth;
      btn.classList.add('pop');
    }
    hap('light');
    onChange?.(id);
  }

  function move(clientX) {
    const r = el.getBoundingClientRect();
    const cw = cell();
    const max = (items.length - 1) * cw;
    let x = clientX - r.left - 4 - cw / 2;
    if (x < 0) x *= 0.35;
    else if (x > max) x = max + (x - max) * 0.35;
    setX(x);
    const i = Math.round(Math.max(0, Math.min(max, x)) / cw);
    if (i !== drag.hot) {
      drag.hot = i;
      hap('select');
    }
    el.querySelectorAll('button').forEach((b, j) => b.classList.toggle('hot', j === i));
  }

  function begin(clientX) {
    if (!drag || drag.active) return;
    drag.active = true;
    el.classList.add('dragging');
    try { el.setPointerCapture(drag.id); } catch {}
    hap('medium');
    move(clientX);
  }

  function end(clientX, cancel) {
    if (!drag) return;
    clearTimeout(drag.timer);
    const d = drag;
    drag = null;
    el.classList.remove('dragging');
    el.querySelectorAll('button').forEach((b) => b.classList.remove('hot'));
    try { el.releasePointerCapture(d.id); } catch {}
    if (cancel) return sync();
    const i = d.active ? d.hot : idxAt(clientX);
    select(items[i].id);
    sync();
  }

  el.addEventListener('pointerdown', (e) => {
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    drag = {
      id: e.pointerId,
      x0: e.clientX,
      active: false,
      hot: idxAt(e.clientX),
      timer: setTimeout(() => begin(e.clientX), 170),
    };
  });
  el.addEventListener('pointermove', (e) => {
    if (!drag) return;
    if (!drag.active) {
      if (Math.abs(e.clientX - drag.x0) > 6) {
        clearTimeout(drag.timer);
        begin(e.clientX);
      }
      return;
    }
    move(e.clientX);
  });
  el.addEventListener('pointerup', (e) => end(e.clientX, false));
  el.addEventListener('pointercancel', (e) => end(e.clientX, true));
  el.addEventListener('contextmenu', (e) => e.preventDefault());
  el.addEventListener('click', (e) => {
    if (e.detail !== 0) return;
    const b = e.target.closest('button');
    if (b) select(b.dataset.t);
  });
  window.addEventListener('resize', sync);

  sync();
  return { select, setActive: sync, get active() { return current; } };
}
