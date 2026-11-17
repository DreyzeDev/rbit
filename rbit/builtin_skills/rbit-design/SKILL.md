---
name: rbit-design
description: Use when building or restyling mobile-first web UI, Telegram Mini Apps, wallets, fintech dashboards, or landing pages in a minimalist monochrome dark style — near-black canvas, iOS-like rounded cards, liquid-glass floating tab bar, spring animations, one SVG icon set, TGS/Lottie sticker illustrations. Triggers on "minimal dark UI", "iOS style", "liquid glass", "like Striver Wallet", "красиво и минималистично", "сделай дизайн".
---

# Rbit Design

A complete monochrome dark UI system distilled from the Striver Wallet Telegram Mini App. The goal: interfaces that look expensive because of restraint — one accent, big typography, generous air, soft spring motion, and a single illustration language.

Use this skill for Telegram Mini Apps, mobile wallets, fintech dashboards, landing pages, and any mobile-first web UI that should feel premium and minimal.

## Non-negotiables

1. **Near-black canvas, grayscale hierarchy.** Background `#0a0a0a`, cards `#161616`, raised `#202020`, `#2b2b2b`. Text `#f5f5f5`, muted `#989898`. Boundaries `rgba(255,255,255,.07)`.
2. **White is the only accent.** Primary buttons, active pills, progress fills — white on near-black. No brand blue/violet/red for decoration.
3. **Color only carries meaning.** Green `#34d399` = growth/positive, red `#f4675c` = loss/negative, asset colors = coins only. Nowhere else.
4. **One font family.** System stack (`-apple-system, "SF Pro Display", Inter, "Segoe UI", Roboto`). Weight and size create hierarchy, not color.
5. **Geometry:** cards 24px radius, buttons 16px, chips/pills 999px, sheets 28px top corners, coins circular. Nothing sharp, nothing at 8–12px radius.
6. **Air over density.** 16px page padding, 12px card gaps, 44–52px touch targets, max content width 520px.
7. **Motion confirms, never entertains.** Spring easings, short distances, staggered entrance, tactile press scale. Everything respects `prefers-reduced-motion`.
8. **No emoji.** Icons are a single 24×24 stroke set (stroke 1.9, round caps). Illustrations are TGS/Lottie stickers or pure SVG.
9. **No text shadows, no neon glows, no glassmorphism except the floating nav and sheets scrim.**
10. **Mobile first.** Design at 390–430px width; desktop is the same column centered.

## Tokens

Copy from `reference/theme.css` (variables + base components). Core values:

```css
:root {
  --bg: #0a0a0a;
  --card: #161616;
  --card2: #202020;
  --card3: #2b2b2b;
  --line: rgba(255, 255, 255, .07);
  --text: #f5f5f5;
  --muted: #989898;
  --up: #34d399;
  --down: #f4675c;
  --brand: linear-gradient(180deg, #ffffff 0%, #cfcfcf 100%);
  --safe-top: calc(env(safe-area-inset-top, 0px) + var(--tg-content-safe-area-inset-top, 0px) + var(--tg-safe-area-inset-top, 0px));
  --safe-bottom: calc(env(safe-area-inset-bottom, 0px) + var(--tg-safe-area-inset-bottom, 0px));
  --ease-spring: cubic-bezier(.3, 1.6, .5, 1);
  --ease-soft: cubic-bezier(.2, .9, .3, 1);
}
```

## Typography scale

| Role | Size / weight | Notes |
|---|---|---|
| Hero balance | 52px / 750 | tracking −0.02em; decimals 32px and 60% opacity |
| Sheet title | 26px / 750 | centered, line-height 1.15 |
| Screen title | 28px / 750 | hero pages |
| Section title | 17px / 700 | color `#c2c2c2`, right side total in 14px muted |
| Row title | 17px / 600 | single line, ellipsis |
| Row subtitle | 14px / 400 | `--muted`, 3px top margin |
| Button | 17px / 650 | min-height 52px |
| Caption / hint | 13–14px | muted, line-height 1.4 |

Numbers use `Intl.NumberFormat('ru-RU')` style grouping with thin spaces; currency symbol in muted 30px after the amount.

## Screen anatomy

**Top bar** — 44px round avatar button (photo or gradient initials), centered `DEMO` pill (12px, 600, `--card2`), 44px round secondary action on the right. Height 46px, `space-between`.

**Wallet screen**
1. Balance centered: label 15px/600 `#d0d0d0`, amount with count-up animation.
2. 4-up action grid: cards 20px radius, icon 26–36px, label 12.5px/650. Icons either stroke SVG or sticker.
3. Optional promo banner: 125° gray gradient `#2a2a2a → #444 → #1c1c1c`, 24px radius, headline max-width 62%, sticker anchored bottom-right, shine sweep, close circle 30px.
4. Grouped list cards: row padding 14px 16px, internal dividers `1px solid var(--line)`, layout icon (42–46px) → grow (title + subtitle) → right value block → chevron `#5d5d5d`.
5. Section title above each group, right-aligned "Всего: …" in muted.

**Trade / market screen** — 52px search field (`--card`, 20px radius), 4-up popular assets with 58px coins and colored percent, treemap heatmap inside a 22px rounded card, product tile grid, asset detail sheet with sparkline + 1Д/1Н/1М segmented control.

**Earn / hero screen** — centered hero: 110–120px looping sticker, 28px title, 17px muted line, text link with chevron. Pill segmented control (`on` = `--card2`). 2-column cards with APY, optional `BONUS` ribbon rotated 40°, top-right.

**Bonuses / campaign screen** — full-bleed gradient header (`#3a3a3a → #262626 → #0a0a0a`) behind the status bar, big counter with sticker, season card with progress pill + 6px bar, 3-column case grid with gap 10px, native `<details>` FAQ.

**Settings sheet** — full-height sheet: 88px centered avatar, name, handle; grouped cards of rows; 34px rounded-square colored icons only here; iOS toggle (50×30, thumb 24, stretches while pressed); danger button = `rgba(244,103,92,.14)` bg with `--down` text; legal text 13px muted.

## Components rules

- **Buttons:** `.btn` 52px, radius 16, weight 650. Primary = white bg, `#0a0a0a` text. Ghost = `--card2`. Danger = translucent red. Small = 40px / 13px radius. Disabled = 40% opacity. Press: `scale(.96)`.
- **Cards:** `.card` 24px radius, `--card`, 12px bottom margin; `.pad` 16px. No borders, no shadows.
- **Rows:** full-width buttons inside cards; active state `rgba(255,255,255,.03)`.
- **Chips:** pill 9px 15px, `--card2`; active = white bg / dark text; press scale .92.
- **Fields:** `--card`, 18px radius, padding 12px 16px, tiny 13px label, 19px/600 input, placeholder `#666`.
- **Badges:** 11px/700, 2px 7px, radius 6, `#3f3f3f` bg.
- **Toggle:** 50×30, track `#404040` → white when on, thumb white → near-black when on, thumb width 30px while held.
- **Empty state:** centered muted text + 100–120px looping sticker.
- **FAQ:** `<details>` rows with custom 9px chevron (border, rotate −45° → 45°), no default marker.
- **Toast:** fixed above nav, white 96% bg, dark text, 16px radius, fade+rise 200ms.

## Liquid-glass tab bar

72px capsule, fixed bottom `calc(14px + var(--safe-bottom))`, width `min(100% - 28px, 440px)`, radius 999. Background `rgba(38,38,38,.62)`, `backdrop-filter: blur(26px) saturate(1.6)`, border `1px solid rgba(255,255,255,.1)`, shadow `0 14px 38px rgba(0,0,0,.55)` + inner top highlight. 4 tabs max, icon 25px + label 11.5px/600, inactive `#9d9d9d`, active white.

Active indicator is a separate absolutely-positioned pill that slides with `transform .5s var(--ease-spring)`. Long-press (~170ms) or 6px horizontal drag "lifts" the glass: indicator scales 1.22/1.16, brightens to `rgba(255,255,255,.24)`, follows the pointer with rubber-band beyond edges, haptic tick per tab, springs back on release. Hide scrollbars everywhere; body `overscroll-behavior-y: none`.

Implementation: `reference/liquid-nav.js`.

## Sheets (bottom modals)

- Wrapper fixed inset 0, scrim `rgba(0,0,0,.55)` fading in over 220ms.
- Sheet: max-width 520, radius 28px top, bg `#121212`, padding 8px 16px calc(24px + safe-bottom), slide-up 280ms `cubic-bezier(.2,.9,.3,1)`.
- 40×5 grabber `#404040` centered; close = 34px circle `--card2` top-right; press rotates 90° and scales .85.
- Drag grabber down to dismiss: translate follows finger, scrim opacity = `1 - dy/500`; release closes if `dy > 110` or velocity > 0.6 px/ms, else snaps back. No transition while dragging, spring snap on release.
- Stack supported; Escape and Telegram BackButton close the top sheet.
- Use sheets for every flow: send, swap, deposit, earn, asset detail, settings, history. Never navigate full pages inside a mini app.

Implementation: `reference/sheet.js`.

## Motion system

- **Entrance:** `rise` = `opacity 0 → 1`, `translateY(18px) → 0`, `scale(.985) → 1`, 550ms `--ease-soft`, stagger 38–45ms per child via `--i` index (cap at 10–14).
- **Screen tab switch:** fade + 6px rise 250ms; scroll to top.
- **Count-up:** 800–900ms, easeOutQuart (`1 - (1-p)^4`); format on every frame.
- **Live values:** update text in place every 3s, never re-render the screen.
- **Press:** `scale(.92–.96)` with `--ease-spring`, 120–250ms; icons nudge up 4px on action tap.
- **Staggered tiles:** heatmap/tiles pop from `scale(.6)` with 22ms per-tile delay.
- **Progress:** bar grows via `scaleX(0 → 1)` 1s; pill fill wipes with `clip-path inset`.
- **Shine:** promo gets a 105° white highlight translating −120% → 120% over 4.5s every ~5s.
- **Nav entry:** from `translateY(90px) scale(.9)` 700ms after 100ms.
- Reduced motion: all animations/transitions to 1ms.

## Telegram Mini App wiring

```html
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,user-scalable=no">
<meta name="theme-color" content="#0a0a0a">
<script src="https://telegram.org/js/telegram-web-app.js"></script>
```

```js
const tg = window.Telegram?.WebApp;
try {
  tg?.ready(); tg?.expand();
  tg?.setHeaderColor?.('#0a0a0a');
  tg?.setBackgroundColor?.('#0a0a0a');
  tg?.setBottomBarColor?.('#0a0a0a');
  tg?.disableVerticalSwipes?.();
} catch {}
```

- Haptics: `impactOccurred('light'|'medium')` on taps/tab switches, `selectionChanged()` on drag ticks, `notificationOccurred('success')` on confirmations.
- `BackButton.onClick` closes the top sheet; show/hide when the stack changes.
- Pad top with `--safe-top` and bottom with `120px + --safe-bottom` so content clears the floating nav.
- Set env colors to the app background so the Telegram header blends invisibly.

## Icons & illustration

- Single stroke icon set, `viewBox="0 0 24 24"`, `fill="none"`, `stroke="currentColor"`, `stroke-width="1.9"`, round caps/joins. Size 19–26px. One function `ic(name, size)` returning inline SVG.
- Coins: 40–58px circles, `radial-gradient(circle at 30% 25%, mix(color, white 70%), color 70%)`, inset bottom shadow, white glyph or SVG at 60%.
- Stickers: Lottie JSON (TGS unpacked). Mount once on visibility via `IntersectionObserver`, play one-shot or loop, pause offscreen, replay on parent tap, destroy when element leaves DOM. Size 28–128px. Use for heroes, action icons, empty states, cases, nav.
- Sticker loading pattern: lazy `fetch` + cache, `lottie.loadAnimation({ renderer: 'svg', autoplay: false })`, clone data per instance.

## Workflow

1. Define the screen list and the one primary action per screen.
2. Lay out tokens first (`theme.css` variables), then base (body, buttons, cards, rows).
3. Build the shell: top bar, scroll area, floating glass nav, sheets root, toast root.
4. Implement screens as pure template functions from state; one delegated click handler with `data-a` actions.
5. Add entrance stagger by indexing direct children (`--i`).
6. Add haptics, BackButton, safe areas, reduced motion.
7. Polish pass: verify below.

## Quality checklist

- [ ] Only grayscale + white accents; color appears solely for growth/loss/assets.
- [ ] No radius below 13px except icon tiles (10px) and badges (6px).
- [ ] Every tappable element ≥ 44px and has a pressed state.
- [ ] Cards have no visible borders or hard shadows.
- [ ] Balance/hero numbers use tabular figures and tight tracking.
- [ ] Animations 120–900ms, spring easing; nothing linear or bouncy-cartoonish.
- [ ] Reduced-motion respected.
- [ ] Scrollbars hidden, overscroll contained, no horizontal scroll at 320px.
- [ ] Content max-width 520px, centered; nav max 440px.
- [ ] Safe areas respected; bottom content never hidden behind nav.
- [ ] No emoji, no mixed icon styles, no default browser focus rings visible on touch.
- [ ] Empty, loading, and error states designed (sticker + one line + action).

## Anti-patterns

- Pure `#000` everywhere with gray borders → looks cheap; use layered near-blacks.
- Multiple accent colors, gradients on buttons, colored shadows.
- Small 12px text everywhere, dense rows, 8px radii.
- Bounce/elastic animations longer than 500ms, or animating every element.
- Emoji as icons, mixing outline/filled/3D icons.
- Full-page transitions inside a Mini App instead of bottom sheets.
- Glass effect on ordinary cards (reserve it for the nav/scrim).

## Reference files

- `reference/theme.css` — tokens, reset, typography, cards, rows, buttons, chips, fields, toggles, FAQ, toast, sheets, nav, motion keyframes.
- `reference/liquid-nav.js` — liquid-glass tab bar with drag, rubber-band, haptics.
- `reference/sheet.js` — bottom-sheet manager with drag-to-close, stacking, BackButton.
- `reference/stickers.js` — lazy TGS/Lottie loader with IntersectionObserver, replay, cleanup.
