"use strict";

// One scheduler for all cards; detached cards release observers and media promptly.
const activeCarousels = new Map();
const carouselVisibility = new IntersectionObserver(entries => {
  entries.forEach(entry => activeCarousels.get(entry.target)?.visibility(entry.isIntersecting && entry.intersectionRatio >= .35));
}, { threshold: [0, .35] });
setInterval(() => {
  activeCarousels.forEach((controller, root) => {
    if (!root.isConnected) {
      controller.visibility(false);
      carouselVisibility.unobserve(root);
      activeCarousels.delete(root);
    } else controller.tick();
  });
}, 250);
document.addEventListener("visibilitychange", () => {
  activeCarousels.forEach(controller => controller.tick());
});

function carousel(item, open) {
  const root = node("div", "card-cover media-carousel");
  const stage = node("div", "media-stage");
  const slides = [null, ...(item.videos || [])];
  const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
  let motionOverride = false;
  const motionAllowed = () => motionOverride || !reducedMotion.matches;
  let index = 0, visible = false, suspended = true, paused = false, remaining = 6000, lastTick = performance.now();
  const counter = node("span", "slide-counter");
  const tabs = [];
  const toggle = node("button", "autoplay-toggle", "Ⅱ");
  toggle.type = "button";
  const updateToggle = () => {
    toggle.textContent = paused || !motionAllowed() ? "▶" : "Ⅱ";
    toggle.setAttribute("aria-label", paused || !motionAllowed() ? "Resume autoplay" : "Pause autoplay");
    toggle.setAttribute("aria-pressed", String(paused));
  };
  const canPlay = () => visible && !document.hidden && !document.querySelector("dialog[open]") && !paused && motionAllowed();
  const play = player => {
    player.play().catch(() => {
      if (!player.isConnected || suspended || stage.querySelector(".video-play")) return;
      const retry = node("button", "video-play", "▶ Play video");
      retry.addEventListener("click", () => { retry.remove(); play(player); });
      stage.append(retry);
    });
  };
  const syncPlayback = () => {
    const nextSuspended = !canPlay();
    if (nextSuspended === suspended) return;
    suspended = nextSuspended;
    const player = stage.querySelector("video");
    if (player) {
      player.autoplay = !suspended;
      suspended ? player.pause() : play(player);
    }
    const frame = stage.querySelector("iframe");
    if (frame) {
      if (suspended) frame.removeAttribute("src");
      else frame.src = frame.dataset.src;
    }
  };
  const draw = (manual = false) => {
    if (manual) motionOverride = true;
    stage.querySelector("video")?.pause();
    stage.replaceChildren();
    remaining = slides[index] ? 15000 : 6000;
    lastTick = performance.now();
    counter.textContent = `${index + 1} / ${slides.length}`;
    tabs.forEach((tab, position) => tab.setAttribute("aria-pressed", String(position === index)));
    const video = slides[index];
    if (!video) {
      const button = node("button", "hero-button");
      button.setAttribute("aria-label", `View ${item.paper.title}`);
      if (open) button.addEventListener("click", open);
      if (item.cover) {
        const img = localImage(item.thumbnail || item.cover, `Project page first viewport: ${item.paper.title}`);
        // Fall back to the full screenshot when a thumbnail cannot be produced.
        img.addEventListener("error", () => {
          if (item.thumbnail && img.getAttribute("src") === item.thumbnail) img.src = item.cover;
          else button.replaceChildren(placeholder(item.paper));
        });
        button.append(img);
      } else button.append(placeholder(item.paper));
      stage.append(button);
      return;
    }
    let embed = null;
    if (video.provider === "youtube" && /^[A-Za-z0-9_-]{6,64}$/.test(video.provider_id || ""))
      embed = `https://www.youtube-nocookie.com/embed/${video.provider_id}?autoplay=1&mute=1&playsinline=1&origin=${encodeURIComponent(location.origin)}`;
    if (video.provider === "vimeo" && /^\d+$/.test(video.provider_id || ""))
      embed = `https://player.vimeo.com/video/${video.provider_id}?autoplay=1&muted=1&playsinline=1`;
    if (embed) {
      const frame = node("iframe");
      frame.dataset.src = embed;
      frame.title = `Video for ${item.paper.title}`;
      frame.allow = "autoplay; fullscreen; picture-in-picture";
      frame.allowFullscreen = true;
      frame.referrerPolicy = "strict-origin-when-cross-origin";
      stage.append(frame);
    } else if (video.delivery === "direct" && safeUrl(video.url)?.startsWith("https://")) {
      const player = node("video");
      player.controls = true;
      player.muted = true;
      player.defaultMuted = true;
      player.autoplay = canPlay();
      player.playsInline = true;
      player.preload = "metadata";
      player.src = video.url;
      player.addEventListener("ended", () => {
        remaining = 0;
      });
      stage.append(player);
    } else stage.append(node("p", "field-note", "Open this video at its original site."));
    const original = resourceLink({ url: video.url, kind: "video" });
    if (original && video.provider === "youtube") original.textContent = "Open on YouTube ↗";
    if (original) stage.append(original);
    suspended = true;
    if (motionAllowed()) syncPlayback();
  };
  const advance = () => { index = (index + 1) % slides.length; draw(); };
  root.append(stage);
  if (slides.length > 1) {
    const controls = node("div", "carousel-controls");
    const choices = node("div", "media-choices");
    slides.forEach((video, position) => {
      const tab = node("button", "", video ? "Video" : "Page");
      tab.addEventListener("click", () => { index = position; draw(true); });
      tabs.push(tab);
      choices.append(tab);
    });
    controls.append(choices);
    for (const [label, delta, icon] of [["Previous visual", -1, "←"], ["Next visual", 1, "→"]]) {
      const button = node("button", "", icon);
      button.setAttribute("aria-label", label);
      button.addEventListener("click", () => { index = (index + delta + slides.length) % slides.length; draw(true); });
      controls.append(button);
      if (delta === -1) controls.append(counter);
    }
    toggle.addEventListener("click", () => {
      if (!motionAllowed()) { motionOverride = true; paused = false; }
      else paused = !paused;
      updateToggle();
      syncPlayback();
    });
    controls.append(toggle);
    root.append(controls);
    activeCarousels.set(root, {
      visibility(value) { visible = value; syncPlayback(); lastTick = performance.now(); },
      tick() {
        const now = performance.now();
        const elapsed = Math.min(now - lastTick, 1000);
        lastTick = now;
        syncPlayback();
        updateToggle();
        if (!canPlay() || root.matches(":hover") || root.contains(document.activeElement)) return;
        const player = stage.querySelector("video");
        // Direct videos finish before advancing; unavailable media still times out.
        if (player && !player.paused && !player.ended && player.readyState >= 2) return;
        remaining -= elapsed;
        if (remaining <= 0) advance();
      },
    });
    carouselVisibility.observe(root);
  }
  updateToggle();
  draw();
  return root;
}
