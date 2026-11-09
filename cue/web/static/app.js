// Cue's web interface: search, episodes, watching in the browser and casting to a TV.
// It talks to the JSON API under /api/ and plays videos from the cast server's relay.
"use strict";

const ICON = {
  search: '<svg viewBox="0 0 24 24"><path d="M10 2a8 8 0 0 1 6.32 12.9l5.39 5.4-1.41 1.4-5.4-5.39A8 8 0 1 1 10 2zm0 2a6 6 0 1 0 0 12 6 6 0 0 0 0-12z"/></svg>',
  play: '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>',
  phone: '<svg viewBox="0 0 24 24"><path d="M7 2h10a2 2 0 0 1 2 2v16a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2zm0 3v13h10V5H7zm5 14.5a1 1 0 1 0 0 2 1 1 0 0 0 0-2z"/></svg>',
  tv: '<svg viewBox="0 0 24 24"><path d="M3 5h18a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2h-6v2H9v-2H3a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2zm0 2v10h18V7H3z"/></svg>',
  prev: '<svg viewBox="0 0 24 24"><path d="M6 6h2v12H6zm3.5 6L18 18V6z"/></svg>',
  next: '<svg viewBox="0 0 24 24"><path d="M16 6h2v12h-2zM6 18l8.5-6L6 6z"/></svg>',
};

// -- small helpers --------------------------------------------------------------------------------

function h(tag, props, ...children) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value == null || value === false) continue;
    if (key === "class") element.className = value;
    else if (key === "icon") element.innerHTML = ICON[value];  // our own markup only
    else if (key.startsWith("on")) element.addEventListener(key.slice(2), value);
    else element.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child != null && child !== false) element.append(child instanceof Node ? child : String(child));
  }
  return element;
}

const saved = {
  get(key, fallback) {
    try {
      const value = localStorage.getItem("cue." + key);
      return value == null ? fallback : JSON.parse(value);
    } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem("cue." + key, JSON.stringify(value)); } catch { /* private mode */ }
  },
};

function clock(seconds) {
  seconds = Math.max(0, Math.floor(seconds || 0));
  const h = Math.floor(seconds / 3600), m = Math.floor(seconds / 60) % 60, s = seconds % 60;
  return (h ? `${h}:${String(m).padStart(2, "0")}` : `${m}`) + `:${String(s).padStart(2, "0")}`;
}

async function api(path, { body, signal } = {}) {
  const init = { headers: { Accept: "application/json" }, signal };
  if (body !== undefined) {
    init.method = "POST";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const response = await fetch(path, init);
  let data = null;
  try { data = await response.json(); } catch { /* not JSON */ }
  if (!response.ok) {
    const error = new Error((data && data.error) || `${response.status} ${response.statusText}`);
    error.status = response.status;
    throw error;
  }
  return data;
}

function toast(message, title = "", severity = "information", seconds = 5) {
  const box = h("div", { class: "toast" + (severity === "error" ? " error" : ""), role: "status" },
    title ? h("strong", {}, title) : null, message);
  document.getElementById("toasts").append(box);
  setTimeout(() => box.remove(), Math.max(2, seconds || 5) * 1000);
}

const aborted = (error) => error && error.name === "AbortError";

// -- state -----------------------------------------------------------------------------------------

const state = {
  info: null,
  target: saved.get("target", null),  // null: this device; else {id, name, kind}
  group: saved.get("group", ""),
  audio: saved.get("audio", {}),       // provider -> "sub" / "dub"
  autoSkip: saved.get("autoSkip", null),
  autoNext: saved.get("autoNext", null),
};

function onTv() {
  return state.target != null;
}

function showTarget() {
  const button = document.getElementById("target");
  button.classList.toggle("on-tv", onTv());
  button.querySelector(".target-icon").innerHTML = onTv() ? ICON.tv : ICON.phone;
  button.querySelector(".target-name").textContent = onTv() ? state.target.name : "This device";
  button.setAttribute("aria-label", `Play on: ${onTv() ? state.target.name : "this device"}. Change`);
}

function chooseTarget(target) {
  state.target = target;
  saved.set("target", target);
  showTarget();
}

// -- routing ------------------------------------------------------------------------------------------

const main = document.getElementById("view");
let leave = null;  // the current view's clean-up

function route() {
  const raw = location.hash.replace(/^#\/?/, "");
  const at = raw.indexOf("?");
  return { path: at < 0 ? raw : raw.slice(0, at), params: new URLSearchParams(at < 0 ? "" : raw.slice(at + 1)) };
}

function link(path, params) {
  const query = params ? new URLSearchParams(params).toString() : "";
  return `#/${path}${query ? "?" + query : ""}`;
}

function go(path, params) {
  location.hash = link(path, params);
}

function render() {
  if (leave) leave();
  leave = null;
  const { path, params } = route();
  const view = { search: resultsView, show: showView, watch: watchView }[path] || homeView;
  main.replaceChildren();
  window.scrollTo(0, 0);
  leave = view(main, params) || null;
}

// -- home ------------------------------------------------------------------------------------------------

function groups() {
  return (state.info && state.info.groups) || [];
}

function currentGroup() {
  const names = groups().map((group) => group.name);
  return names.includes(state.group) ? state.group : (state.info && state.info.default_group) || names[0] || "";
}

function searchForm(query = "") {
  const input = h("input", {
    type: "search", name: "q", placeholder: "Search shows and movies", value: query, enterkeyhint: "search",
    autocomplete: "off", autocapitalize: "off", spellcheck: "false", "aria-label": "Search",
  });
  const form = h("form", {
    class: "search", role: "search",
    onsubmit: (event) => {
      event.preventDefault();
      const text = input.value.trim();
      if (text) go("search", { q: text, group: currentGroup() });
    },
  }, input, h("button", { type: "submit", "aria-label": "Search", icon: "search" }));
  const list = groups();
  const chips = list.length > 1 ? h("div", { class: "chips", role: "group", "aria-label": "Search in" },
    list.map((group) => h("button", {
      type: "button", class: "chip", "aria-pressed": String(group.name === currentGroup()),
      title: group.providers.join(", "),
      onclick: (event) => {
        state.group = group.name;
        saved.set("group", group.name);
        for (const chip of event.currentTarget.parentNode.children) {
          chip.setAttribute("aria-pressed", String(chip === event.currentTarget));
        }
        const text = input.value.trim();
        if (text && route().path === "search") go("search", { q: text, group: group.name });
      },
    }, group.title))) : null;
  return { form, chips, input };
}

function describeHistory(entry) {
  if (entry.position > 0) return `Episode ${entry.label} at ${clock(entry.position)}`;
  return `Watched episode ${entry.label}, next one up`;
}

function showLink(provider, id, title, extra) {
  return link("show", { provider, id, title, ...(extra || {}) });
}

function homeView(root) {
  const controller = new AbortController();
  const { form, chips, input } = searchForm();
  const list = h("div", { class: "list" }, h("p", { class: "hint" }, "Loading…"));
  root.append(form);
  if (chips) root.append(chips);
  root.append(h("h2", {}, "Continue watching"), list);
  if (matchMedia("(pointer: fine)").matches) input.focus();  // not on phones: no keyboard popping up

  api("/api/continue", { signal: controller.signal }).then(({ entries }) => {
    if (!entries.length) {
      list.replaceChildren(h("p", { class: "hint" }, "Nothing here yet. Search for a show above."));
      return;
    }
    list.replaceChildren(...entries.map((entry) => h("div", { class: "row" + (entry.available ? "" : " disabled") },
      h(entry.available ? "a" : "div", { class: "row-main", href: entry.available ? showLink(entry.provider, entry.show, entry.title) : null },
        h("div", { class: "title" }, entry.title),
        h("div", { class: "sub" }, describeHistory(entry), " ", h("span", { class: "tag" }, entry.provider))),
      entry.available ? h("button", {
        type: "button", class: "icon", icon: "play", "aria-label": `Resume ${entry.title}`,
        onclick: () => go("show", { provider: entry.provider, id: entry.show, title: entry.title, resume: "1" }),
      }) : null)));
  }).catch((error) => {
    if (!aborted(error)) list.replaceChildren(h("p", { class: "error" }, error.message));
  });
  return () => controller.abort();
}

// -- search results -------------------------------------------------------------------------------------

function describeShow(show) {
  const parts = [];
  if (show.year) parts.push(String(show.year));
  if (show.media_type === "movie") parts.push("movie");
  else if (show.episode_count != null) parts.push(`${show.episode_count} episode${show.episode_count === 1 ? "" : "s"}`);
  return parts.join(" · ");
}

function resultsView(root, params) {
  const controller = new AbortController();
  const query = params.get("q") || "";
  const groupName = params.get("group") || currentGroup();
  const { form, chips } = searchForm(query);
  const heading = h("h2", {}, "Results");
  const list = h("div", { class: "list" });
  const hint = h("p", { class: "hint" }, h("span", { class: "spinner", "aria-hidden": "true" }), " Searching…");
  root.append(form);
  if (chips) root.append(chips);
  root.append(heading, list, hint);

  const found = [];
  const problems = [];
  let waiting = 0, tagged = false, order = 0;

  function show() {
    found.sort((a, b) => b.score - a.score || a.order - b.order);
    list.replaceChildren(...found.map((show) => h("a", {
      class: "row", href: showLink(show.provider, show.id, show.title),
    }, h("div", { class: "row-main" },
      h("div", { class: "title" }, show.title),
      h("div", { class: "sub" }, describeShow(show), tagged ? h("span", { class: "tag" }, " ", show.provider) : null)))));
    const lines = [];
    if (waiting) lines.push(h("span", { class: "spinner", "aria-hidden": "true" }), ` Still searching ${waiting} more provider${waiting === 1 ? "" : "s"}…`);
    else if (!found.length) lines.push("No results.");
    for (const problem of problems) lines.push(h("br"), h("span", { class: "error" }, problem));
    hint.replaceChildren(...lines);
  }

  function handle(message) {
    if (message.type === "start") {
      waiting = message.providers.length;
      tagged = message.group.providers.length > 1;
      heading.textContent = `${message.group.title} · “${query}”`;
    } else if (message.type === "results") {
      waiting -= 1;
      if (message.error) problems.push(`${message.provider}: ${message.error}`);
      for (const result of message.shows) found.push({ ...result, order: order++ });
    } else if (message.type === "done") {
      waiting = 0;
    }
    show();
  }

  (async () => {
    const response = await fetch("/api/search?" + new URLSearchParams({ q: query, group: groupName }),
      { signal: controller.signal, headers: { Accept: "application/x-ndjson" } });
    if (!response.ok) {
      let message = `${response.status} ${response.statusText}`;
      try { message = (await response.json()).error || message; } catch { /* not JSON */ }
      throw new Error(message);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let end;
      while ((end = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, end).trim();
        buffer = buffer.slice(end + 1);
        if (line) handle(JSON.parse(line));
      }
    }
    waiting = 0;
    show();
  })().catch((error) => {
    if (!aborted(error)) hint.replaceChildren(h("span", { class: "error" }, error.message));
  });
  return () => controller.abort();
}

// -- a show's episodes ---------------------------------------------------------------------------------------

function start(choice) {
  // choice: {provider, show, title, number, audio}
  if (onTv()) castTo(state.target, choice);
  else go("watch", { provider: choice.provider, show: choice.show, title: choice.title, number: choice.number, audio: choice.audio || "" });
}

function showView(root, params) {
  const controller = new AbortController();
  const provider = params.get("provider");
  const id = params.get("id");
  const title = params.get("title") || "";
  const heading = h("h1", {}, title || "…");
  const meta = h("p", { class: "meta" });
  const actions = h("div", { class: "actions" });
  const list = h("div", { class: "list" }, h("p", { class: "hint" }, h("span", { class: "spinner", "aria-hidden": "true" }), " Loading episodes…"));
  root.append(heading, meta, actions, list);

  api("/api/episodes?" + new URLSearchParams({ provider, show: id, title }), { signal: controller.signal }).then((data) => {
    const show = data.show;
    heading.textContent = show.title;
    meta.textContent = [describeShow(show), provider].filter(Boolean).join(" · ");
    let audio = data.audio_modes.includes(state.audio[provider]) ? state.audio[provider] : data.audio;
    const choice = (number) => ({ provider, show: show.id, title: show.title, number, audio });

    actions.replaceChildren();
    const resume = data.resume;
    if (resume) {
      let label;
      if (show.media_type === "movie") label = resume.position > 0 ? `Resume at ${clock(resume.position)}` : "Play";
      else if (resume.caught_up) label = `Caught up · play episode ${resume.label} again`;
      else if (resume.position > 0) label = `Resume episode ${resume.label} at ${clock(resume.position)}`;
      else label = `Play episode ${resume.label}`;
      actions.append(h("button", { type: "button", onclick: () => start(choice(resume.number)) }, label));
    }
    if (data.audio_modes.length) {
      const names = { sub: "SUB", dub: "DUB" };
      const control = h("div", { class: "segmented", role: "group", "aria-label": "Audio" },
        data.audio_modes.map((mode) => h("button", {
          type: "button", "aria-pressed": String(mode === audio), title: mode === "sub" ? "Original audio" : "Dubbed audio",
          onclick: (event) => {
            audio = mode;
            state.audio[provider] = mode;
            saved.set("audio", state.audio);
            for (const button of control.children) button.setAttribute("aria-pressed", String(button === event.currentTarget));
          },
        }, names[mode] || mode.toUpperCase())));
      actions.append(control);
    }

    if (!data.episodes.length) {
      list.replaceChildren(h("p", { class: "hint" }, "No episodes."));
      return;
    }
    const seasons = new Set(data.episodes.map((episode) => episode.season));
    const rows = [];
    let season;
    for (const episode of data.episodes) {
      if (seasons.size > 1 && episode.season !== season) {
        season = episode.season;
        rows.push(h("h2", { class: "season" }, season == null ? "Other" : `Season ${season}`));
      }
      const playing = episode.position > 0;
      const status = playing ? `at ${clock(episode.position)}` : episode.watched ? "watched" : "";
      rows.push(h("button", {
        type: "button", class: "row episode" + (episode.watched ? " watched" : "") + (playing ? " playing" : ""),
        "aria-label": `${show.media_type === "movie" ? show.title : "Episode " + episode.label}${episode.title ? ", " + episode.title : ""}${status ? ", " + status : ""}`,
        onclick: () => start(choice(episode.number)),
      },
      h("span", { class: "number" }, playing ? "▶ " : episode.watched ? "✓ " : "", show.media_type === "movie" ? "" : episode.label),
      h("span", { class: "row-main" }, h("span", { class: "title" }, episode.title || (show.media_type === "movie" ? show.title : `Episode ${episode.label}`))),
      status ? h("span", { class: "state" }, status) : null));
    }
    list.replaceChildren(...rows);
    if (params.get("resume") === "1" && resume && !resume.caught_up) {
      history.replaceState(null, "", showLink(provider, id, show.title));
      start(choice(resume.number));
    }
  }).catch((error) => {
    if (!aborted(error)) list.replaceChildren(h("p", { class: "error" }, error.message));
  });
  return () => controller.abort();
}

// -- watching in the browser -------------------------------------------------------------------------------

function appleNativeHls(video) {
  // iPhone and iPad (and Safari on a Mac) play HLS themselves, with AirPlay and subtitles in full screen.
  const agent = navigator.userAgent;
  const apple = /iPad|iPhone|iPod/.test(agent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1)
    || (/Safari\//.test(agent) && !/Chrome|Chromium|Edg|Android|Firefox/.test(agent));
  return apple && video.canPlayType("application/vnd.apple.mpegurl") !== "";
}

function relayUrl(session, path) {
  // The relay is the cast server on this same machine; reach it by the name this page was opened with.
  return `http://${location.hostname}:${session.relay.port}${path}`;
}

async function streamType(url, signal) {
  const response = await fetch(url, { method: "HEAD", signal });
  if (!response.ok) {
    const again = await fetch(url, { signal });  // HEAD has no body; the error text is in GET's
    const text = (await again.text()).trim();
    throw new Error(text || `The relay answered ${response.status}`);
  }
  return (response.headers.get("content-type") || "").toLowerCase();
}

function watchView(root, params) {
  const controller = new AbortController();
  const choice = {
    provider: params.get("provider"), show: params.get("show"), title: params.get("title") || "",
    number: Number(params.get("number")), audio: params.get("audio") || "",
  };
  const video = h("video", { controls: true, playsinline: true, "webkit-playsinline": true, preload: "auto" });
  const skipButton = h("button", { type: "button" }, "Skip");
  const skipBox = h("div", { class: "overlay", hidden: true }, skipButton);
  const upNext = h("div", { class: "overlay", hidden: true });
  const wrap = h("div", { class: "player-wrap" }, video, skipBox, upNext);
  const heading = h("h1", {}, choice.title || "…");
  const subtitle = h("p", { class: "meta" });
  const status = h("p", { class: "hint", role: "status" }, h("span", { class: "spinner", "aria-hidden": "true" }), " Getting the stream ready…");
  const prev = h("button", { type: "button", class: "secondary", icon: "prev", "aria-label": "Previous episode" });
  const next = h("button", { type: "button", class: "secondary", icon: "next", "aria-label": "Next episode" });
  const captions = h("select", { "aria-label": "Subtitles", hidden: true });
  const autoSkip = h("button", { type: "button", class: "secondary toggle" }, "Auto-skip");
  const autoNext = h("button", { type: "button", class: "secondary toggle" }, "Auto-next");
  const toTv = h("button", { type: "button", class: "secondary", icon: "tv", "aria-label": "Play on a TV" });
  const episodes = h("a", { class: "meta", href: showLink(choice.provider, choice.show, choice.title) }, "All episodes");
  root.append(wrap, heading, subtitle, status, h("div", { class: "toolbar" }, prev, next, captions, autoSkip, autoNext, toTv), episodes);

  const settings = {
    skip: state.autoSkip ?? (state.info ? state.info.auto_skip : true),
    next: state.autoNext ?? (state.info ? state.info.auto_next : true),
  };
  function showToggles() {
    autoSkip.setAttribute("aria-pressed", String(settings.skip));
    autoNext.setAttribute("aria-pressed", String(settings.next));
  }
  showToggles();
  autoSkip.onclick = () => { settings.skip = state.autoSkip = !settings.skip; saved.set("autoSkip", settings.skip); showToggles(); };
  autoNext.onclick = () => { settings.next = state.autoNext = !settings.next; saved.set("autoNext", settings.next); showToggles(); };

  let session = null;   // what /api/play answered
  let index = 0;        // the item playing
  let hls = null;
  let intervals = [];
  let skipped = new Set();
  let countdown = null;
  let republished = false;
  let mediaRecovered = false;
  let loading = 0;      // which load is current, so a late answer for an earlier episode is ignored

  function say(text, error = false) {
    status.className = error ? "error" : "hint";
    status.replaceChildren(text);
    status.hidden = !text;
  }

  function stopEngine() {
    if (hls) { hls.destroy(); hls = null; }
    video.removeAttribute("src");
    video.load();
  }

  function cancelCountdown() {
    if (countdown) { clearInterval(countdown); countdown = null; }
    upNext.hidden = true;
  }

  async function publish() {
    session = await api("/api/play", { body: choice, signal: controller.signal });
    heading.textContent = session.show.title;
    document.title = `${session.show.title} · Cue`;
    return session.index;
  }

  async function play(at, startAt) {
    cancelCountdown();
    const ticket = ++loading;
    index = at;
    const item = session.items[index];
    choice.number = item.number;
    history.replaceState(null, "", link("watch", { ...choice, title: session.show.title }));
    const movie = session.show.media_type === "movie";
    subtitle.textContent = movie ? (item.title || "") : `Episode ${item.label}${item.title ? " · " + item.title : ""}`;
    prev.disabled = index === 0;
    next.disabled = index + 1 >= session.items.length;
    intervals = [];
    skipped = new Set();
    skipBox.hidden = true;
    captions.hidden = true;
    mediaRecovered = false;
    stopEngine();
    say(h("span", {}, h("span", { class: "spinner", "aria-hidden": "true" }), " Getting the stream ready…"));
    if ("mediaSession" in navigator && window.MediaMetadata) {
      navigator.mediaSession.metadata = new MediaMetadata({ title: movie ? session.show.title : `Episode ${item.label}`, artist: session.show.title });
    }
    if (session.skip && !movie) loadSkip(item, ticket);

    const url = relayUrl(session, item.path);
    let type;
    try {
      type = await streamType(url, controller.signal);
    } catch (error) {
      if (!aborted(error) && ticket === loading) say(error.message, true);
      return;
    }
    if (ticket !== loading) return;
    const resume = startAt != null ? startAt : item.resume || 0;
    if (type.includes("mpegurl") && !appleNativeHls(video) && window.Hls && Hls.isSupported()) {
      // The relay saves the resume point from the pieces requested, taking them as 20 seconds
      // ahead of the picture (cue/cast/server.py PREFETCH), so buffer no further ahead than that.
      hls = new Hls({ startPosition: resume > 0 ? resume : -1, maxBufferLength: 15, maxMaxBufferLength: 20 });
      hls.on(Hls.Events.ERROR, (_event, data) => hlsError(data, ticket));
      hls.on(Hls.Events.SUBTITLE_TRACKS_UPDATED, showCaptions);
      hls.on(Hls.Events.SUBTITLE_TRACK_SWITCH, showCaptions);
      hls.on(Hls.Events.MANIFEST_PARSED, () => begin());
      hls.loadSource(url);
      hls.attachMedia(video);
    } else {
      video.src = url;
      video.addEventListener("loadedmetadata", () => {
        if (resume > 0) video.currentTime = resume;
        begin();
      }, { once: true });
    }
  }

  function begin() {
    say("");
    video.play().catch(() => say("Press play to start."));
  }

  function hlsError(data, ticket) {
    if (!data.fatal || ticket !== loading) return;
    const code = data.response && data.response.code;
    if (data.type === Hls.ErrorTypes.NETWORK_ERROR && (code === 404 || code === 410) && !republished) {
      // The relay forgot this cast (Cue restarted, or many others were started since): ask again.
      republished = true;
      const position = video.currentTime;
      publish().then((at) => play(at, position)).catch((error) => say(error.message, true));
      return;
    }
    if (data.type === Hls.ErrorTypes.MEDIA_ERROR && !mediaRecovered) {
      mediaRecovered = true;
      hls.recoverMediaError();
      return;
    }
    say(`Playback stopped: ${data.details}${code ? ` (HTTP ${code})` : ""}`, true);
  }

  async function loadSkip(item, ticket) {
    try {
      const data = await api("/api/skip?" + new URLSearchParams({
        provider: session.provider, show: session.show.id, title: session.show.title, number: item.number,
      }), { signal: controller.signal });
      if (ticket === loading) intervals = data.intervals;
    } catch { /* skipping is a bonus */ }
  }

  function showCaptions() {
    const options = [h("option", { value: "-1" }, "Subtitles off")];
    let selected = -1;
    if (hls) {
      hls.subtitleTracks.forEach((track, number) => options.push(h("option", { value: String(number) }, track.name || track.lang || `Track ${number + 1}`)));
      selected = hls.subtitleDisplay ? hls.subtitleTrack : -1;
    } else {
      Array.from(video.textTracks).forEach((track, number) => {
        if (track.kind !== "subtitles" && track.kind !== "captions") return;
        options.push(h("option", { value: String(number) }, track.label || track.language || `Track ${number + 1}`));
        if (track.mode === "showing") selected = number;
      });
    }
    captions.replaceChildren(...options);
    captions.value = String(selected);
    captions.hidden = options.length < 2;
  }

  captions.onchange = () => {
    const chosen = Number(captions.value);
    if (hls) {
      hls.subtitleDisplay = chosen >= 0;
      hls.subtitleTrack = chosen;
    } else {
      Array.from(video.textTracks).forEach((track, number) => { track.mode = number === chosen ? "showing" : "disabled"; });
    }
  };
  video.textTracks.addEventListener("addtrack", () => { if (!hls) showCaptions(); });
  video.textTracks.addEventListener("change", () => { if (!hls) showCaptions(); });

  video.addEventListener("timeupdate", () => {
    const now = video.currentTime;
    const inside = intervals.find((interval) => interval.start <= now && now < interval.end - 1);
    if (inside && settings.skip && !skipped.has(inside)) {
      skipped.add(inside);  // once: seeking back into it plays it
      video.currentTime = inside.end;
      toast(`Skipped the ${inside.name}`, "", "information", 2);
      return;
    }
    if (inside && !settings.skip) {
      skipButton.textContent = `Skip ${inside.name}`;
      skipButton.onclick = () => { skipped.add(inside); video.currentTime = inside.end; skipBox.hidden = true; };
      skipBox.hidden = false;
    } else {
      skipBox.hidden = true;
    }
  });

  video.addEventListener("ended", () => {
    if (!session || index + 1 >= session.items.length) {
      say(session && session.show.media_type === "movie" ? "Finished." : "That was the last episode.");
      return;
    }
    if (!settings.next) return;
    let left = 5;
    const label = h("span", {}, `Next: episode ${session.items[index + 1].label} in ${left}`);
    upNext.replaceChildren(h("div", { class: "card" }, label),
      h("button", { type: "button", onclick: () => play(index + 1) }, "Play now"),
      h("button", { type: "button", class: "secondary", onclick: cancelCountdown }, "Cancel"));
    upNext.hidden = false;
    countdown = setInterval(() => {
      left -= 1;
      label.textContent = `Next: episode ${session.items[index + 1].label} in ${left}`;
      if (left <= 0) play(index + 1);
    }, 1000);
  });

  video.addEventListener("error", () => {
    if (hls || !video.error || !session) return;
    say(video.error.code === 4 ? "This browser can't play this video's format. Cast it to a TV, or use Cue's terminal app."
      : `Playback stopped (${video.error.message || "error " + video.error.code}).`, true);
  });

  prev.onclick = () => { if (session && index > 0) play(index - 1); };
  next.onclick = () => { if (session && index + 1 < session.items.length) play(index + 1); };
  toTv.onclick = () => openPicker({
    onTv: (target) => {
      video.pause();
      castTo(target, { ...choice, title: session ? session.show.title : choice.title });
    },
  });
  if ("mediaSession" in navigator) {
    try {
      navigator.mediaSession.setActionHandler("previoustrack", () => prev.click());
      navigator.mediaSession.setActionHandler("nexttrack", () => next.click());
    } catch { /* not supported */ }
  }

  publish().then((at) => play(at)).catch((error) => { if (!aborted(error)) say(error.message, true); });

  return () => {
    controller.abort();
    cancelCountdown();
    loading += 1;
    stopEngine();
    document.title = "Cue";
    if ("mediaSession" in navigator) {
      try {
        navigator.mediaSession.setActionHandler("previoustrack", null);
        navigator.mediaSession.setActionHandler("nexttrack", null);
      } catch { /* not supported */ }
    }
  };
}

// -- casting to a TV ------------------------------------------------------------------------------------------

async function castTo(target, choice, retried = false) {
  try {
    const answer = await api("/api/cast", { body: { device: target.id, ...choice } });
    if (answer.vlc) toast(`${answer.message}\n${answer.url}`, "Ready for VLC", "information", 12);
    else toast(`Sending ${choice.title || "it"} to ${target.name}…`, "", "information", 3);
  } catch (error) {
    if (error.status === 404 && !retried && target.id !== "vlc") {
      // The server forgot its TVs (it restarted): look again, then try once more.
      try {
        const found = await api("/api/devices");
        if (found.devices.some((device) => device.id === target.id)) return castTo(target, choice, true);
      } catch { /* fall through */ }
    }
    toast(error.message, `Casting to ${target.name}`, "error", 8);
  }
}

const picker = document.getElementById("picker");
const pickerList = document.getElementById("picker-list");
let pickerOptions = {};

function openPicker(options = {}) {
  pickerOptions = options;
  if (typeof picker.showModal === "function") picker.showModal();
  else picker.setAttribute("open", "");
  scan();
}

function pickerRow(target, label, sub, icon) {
  const checked = target ? onTv() && state.target.id === target.id : !onTv();
  return h("button", {
    type: "button", class: "row", role: "menuitemradio", "aria-checked": String(checked),
    onclick: () => {
      chooseTarget(target);
      picker.close();
      if (target && pickerOptions.onTv) pickerOptions.onTv(target);
    },
  }, h("span", { icon }), h("span", { class: "row-main" }, h("span", { class: "title" }, label), sub ? h("div", { class: "sub" }, sub) : null));
}

async function scan() {
  const searching = h("p", { class: "hint" }, h("span", { class: "spinner", "aria-hidden": "true" }), " Looking for TVs (DLNA, AirPlay, Chromecast)…");
  pickerList.replaceChildren(pickerRow(null, "This device", "Watch in this browser", "phone"), searching);
  try {
    const found = await api("/api/devices");
    if (!found.server.available) {
      searching.replaceChildren(h("span", { class: "error" }, found.server.problem || "The cast server isn't running."));
      return;
    }
    const rows = [pickerRow(null, "This device", "Watch in this browser", "phone"),
      pickerRow(found.vlc, "VLC on the TV", `Opens ${found.server.name} from VLC's Network tab (Xbox)`, "tv")];
    for (const device of found.devices) {
      const note = [device.kind_label, device.model].filter(Boolean).join(", ") + (device.needs_pairing ? " · asks for a code the first time" : "");
      rows.push(pickerRow({ id: device.id, name: device.name, kind: device.kind }, device.name, note, "tv"));
    }
    if (!found.devices.length) rows.push(h("p", { class: "hint" }, "No other TVs found. Is the TV on, and on the same network as Cue?"));
    pickerList.replaceChildren(...rows);
  } catch (error) {
    searching.replaceChildren(h("span", { class: "error" }, error.message));
  }
}

document.getElementById("target").addEventListener("click", () => openPicker());
document.getElementById("rescan").addEventListener("click", scan);

// -- what's playing on TVs, and notices -------------------------------------------------------------------------

const casting = document.getElementById("casting");

function describeJob(job) {
  const episode = job.show.media_type === "movie" || !job.episode ? "" : ` · episode ${job.episode.label}`;
  const states = {
    connecting: "Connecting…", pairing: "Waiting for the code", playing: "Playing",
    finished: "Finished", stopped: "Stopped", failed: job.message || "Stopped with an error",
  };
  return { title: `${job.device.name}: ${job.show.title}${episode}`, sub: states[job.state] || job.state };
}

function showJobs(jobs) {
  const active = jobs.filter((job) => job.active);
  casting.hidden = !active.length;
  casting.replaceChildren(...active.map((job) => {
    const text = describeJob(job);
    const row = h("div", { class: "job" },
      h("span", { icon: "tv", "aria-hidden": "true" }),
      h("div", { class: "what" }, h("div", { class: "title" }, text.title), h("div", { class: "sub" }, text.sub)),
      job.next ? h("button", { type: "button", class: "secondary", onclick: () => api(`/api/cast/${job.id}/next`, { body: {} }).catch((e) => toast(e.message, "", "error")) }, "Next") : null,
      h("button", { type: "button", class: "secondary", onclick: () => api(`/api/cast/${job.id}/stop`, { body: {} }).catch((e) => toast(e.message, "", "error")) }, "Stop"));
    if (job.state === "pairing") {
      const input = h("input", { inputmode: "numeric", autocomplete: "one-time-code", placeholder: "Code on the TV", "aria-label": `Code shown on ${job.device.name}` });
      row.append(h("form", {
        onsubmit: (event) => {
          event.preventDefault();
          api(`/api/cast/${job.id}/code`, { body: { code: input.value } }).catch((e) => toast(e.message, "", "error"));
        },
      }, input, h("button", { type: "submit" }, "Pair")));
    }
    return row;
  }));
}

function listen() {
  if (!window.EventSource) return;
  const events = new EventSource("/api/events");
  events.addEventListener("jobs", (event) => showJobs(JSON.parse(event.data).jobs));
  events.addEventListener("notice", (event) => {
    const notice = JSON.parse(event.data);
    toast(notice.message, notice.title, notice.severity, notice.timeout || 5);
  });
}

// -- start -------------------------------------------------------------------------------------------------

async function boot() {
  showTarget();
  try {
    state.info = await api("/api/info");
  } catch (error) {
    main.replaceChildren(h("p", { class: "error" }, `Can't reach Cue: ${error.message}`));
    return;
  }
  const problems = [];
  if (!state.info.providers.length) problems.push("No providers are set up yet. Set them up in Cue's terminal app (Esc, then p) or in config.toml, then restart cue serve.");
  for (const [name, problem] of Object.entries(state.info.problems)) problems.push(`${name}: ${problem}`);
  if (!state.info.cast.available) problems.push(state.info.cast.problem || "Playback is unavailable: the cast server isn't running.");
  const banner = document.getElementById("banner");
  banner.textContent = problems.join("\n");
  banner.hidden = !problems.length;
  window.addEventListener("hashchange", render);
  render();
  listen();
}

document.addEventListener("DOMContentLoaded", boot);
