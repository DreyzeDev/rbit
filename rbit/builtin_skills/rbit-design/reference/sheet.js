const X_ICON =
  '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18"/></svg>';

export function createSheets({ root, tg } = {}) {
  root ||= document.getElementById('sheets');
  const app = tg ?? window.Telegram?.WebApp;
  const stack = [];

  const syncBack = () => {
    try {
      if (stack.length) app?.BackButton?.show();
      else app?.BackButton?.hide();
    } catch {}
  };

  function open(builder, { full = false, close = true, onClose } = {}) {
    const wrap = document.createElement('div');
    wrap.className = 'sheet-wrap' + (full ? ' full' : '');
    wrap.innerHTML =
      '<div class="scrim"></div>' +
      '<div class="sheet"><div class="grab"></div>' +
      (close ? `<button class="close" data-sheet-close aria-label="Закрыть">${X_ICON}</button>` : '') +
      '<div class="body"></div></div>';

    root.appendChild(wrap);

    const s = {
      el: wrap,
      sheet: wrap.querySelector('.sheet'),
      body: wrap.querySelector('.body'),
      rendered: false,
      onClose,
    };

    s.render = () => {
      s.body.innerHTML = builder(s);
      [...s.body.children].forEach((c, i) => c.style.setProperty('--i', Math.min(i, 10)));
      s.body.classList.toggle('still', s.rendered);
      s.rendered = true;
      s.after?.();
    };

    s.render();
    enableDrag(s);
    wrap.querySelector('.scrim').addEventListener('click', () => close(s));
    wrap.addEventListener('click', (e) => {
      if (e.target.closest('[data-sheet-close]')) close(s);
    });

    stack.push(s);
    syncBack();
    try { app?.HapticFeedback?.impactOccurred?.('light'); } catch {}
    return s;
  }

  function enableDrag(s) {
    const grab = s.el.querySelector('.grab');
    const scrim = s.el.querySelector('.scrim');
    let st = null;

    grab.addEventListener('pointerdown', (e) => {
      st = { y0: e.clientY, t0: performance.now(), dy: 0 };
      s.sheet.classList.add('dragged', 'drag');
      s.sheet.classList.remove('snap');
      try { grab.setPointerCapture(e.pointerId); } catch {}
    });

    grab.addEventListener('pointermove', (e) => {
      if (!st) return;
      st.dy = Math.max(0, e.clientY - st.y0);
      s.sheet.style.transform = `translateY(${st.dy}px)`;
      scrim.style.opacity = String(Math.max(0, 1 - st.dy / 500));
    });

    const done = () => {
      if (!st) return;
      const { dy, t0 } = st;
      const v = dy / Math.max(1, performance.now() - t0);
      st = null;
      s.sheet.classList.remove('drag');
      if (dy > 110 || v > 0.6) {
        scrim.style.opacity = '';
        close(s);
        return;
      }
      s.sheet.classList.add('snap');
      s.sheet.style.transform = '';
      scrim.style.opacity = '';
    };

    grab.addEventListener('pointerup', done);
    grab.addEventListener('pointercancel', done);
  }

  function close(s = stack[stack.length - 1]) {
    if (!s) return;
    const i = stack.indexOf(s);
    if (i < 0) return;
    stack.splice(i, 1);
    s.el.classList.add('out');
    setTimeout(() => s.el.remove(), 200);
    syncBack();
    s.onClose?.();
  }

  const closeAll = () => [...stack].forEach(close);

  try { app?.BackButton?.onClick(() => close()); } catch {}
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') close();
  });

  return { open, close, closeAll, get stack() { return stack; } };
}
