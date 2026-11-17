export function createStickerSystem({ base = 'stickers/', lottie } = {}) {
  const player = lottie ?? window.lottie;
  const cache = new Map();
  const anims = new Map();

  const load = (name) => {
    if (!cache.has(name)) {
      cache.set(
        name,
        fetch(`${base}${name}.json`)
          .then((r) => {
            if (!r.ok) throw new Error(`sticker ${name}`);
            return r.json();
          })
          .catch(() => null)
      );
    }
    return cache.get(name);
  };

  const io = 'IntersectionObserver' in window
    ? new IntersectionObserver(
        (entries) => {
          for (const en of entries) {
            const a = anims.get(en.target);
            if (!a) continue;
            const loop = en.target.hasAttribute('data-loop');
            if (en.isIntersecting) {
              if (loop) a.play();
              else if (!en.target.dataset.played) {
                en.target.dataset.played = '1';
                a.goToAndPlay(0, true);
              }
            } else if (loop) {
              a.pause();
            }
          }
        },
        { threshold: 0.15 }
      )
    : null;

  const mount = (root = document) => {
    for (const [el, a] of anims) {
      if (!el.isConnected) {
        io?.unobserve(el);
        a.destroy();
        anims.delete(el);
      }
    }

    root.querySelectorAll('[data-sticker]:not([data-m])').forEach((el) => {
      el.dataset.m = '1';
      load(el.dataset.sticker)
        .then((data) => {
          if (!data || !el.isConnected || !player) return;
          const a = player.loadAnimation({
            container: el,
            renderer: 'svg',
            loop: el.hasAttribute('data-loop'),
            autoplay: false,
            animationData: structuredClone(data),
            rendererSettings: { preserveAspectRatio: 'xMidYMid meet' },
          });
          anims.set(el, a);
          io ? io.observe(el) : a.goToAndPlay(0, true);
        })
        .catch(() => {});
    });
  };

  const replay = (root) => {
    root?.querySelectorAll?.('[data-sticker]').forEach((el) => {
      const a = anims.get(el);
      if (a && !el.hasAttribute('data-loop')) a.goToAndPlay(0, true);
    });
  };

  return { mount, replay };
}

export const sticker = (name, size = 40, attrs = '') =>
  `<span class="stk" data-sticker="${name}" ${attrs} style="width:${size}px;height:${size}px"></span>`;
